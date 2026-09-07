from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from resultsgen.config import ConfigError, config_dict, load_config, preset
from resultsgen.events import Event, get_partial, plan_events, resolve_window
from resultsgen.failure_text import build_failure_texts
from resultsgen.personality import assign_personalities
from resultsgen.rng import Purpose, generator
from resultsgen.schedule import Run, build_schedule
from resultsgen.simulate import create_markov_state, simulate_run
from resultsgen.suite import AppSuite, Test as Case, build_suites, membership, suite_order


def small_config():
    data = config_dict(preset("dev"))
    data["events"] = []
    data["calendar"].update(outage_night_rate=0, outage_extend_prob=0)
    data["partial"]["rate"] = 0
    for app in data["apps"]:
        app["drift"] = {"removals": 0, "additions": 0}
    return data


def prepare(data):
    schedule = build_schedule(data)
    apps, drift = build_suites(data, schedule)
    events = plan_events(data, schedule, apps, assign_personalities(data), drift)
    return schedule, apps, events


def test_keyed_randomness_and_unknown_purpose():
    expected = generator(71, Purpose.SUITE, 2).random(20)
    generator(71, Purpose.TIMING, 2).random(10000)
    np.testing.assert_array_equal(expected, generator(71, Purpose.SUITE, 2).random(20))
    assert not np.array_equal(expected, generator(71, Purpose.SUITE, 3).random(20))
    with pytest.raises(ValueError):
        generator(71, 999)


def test_schedule_contiguous_injected_outages_and_fixed_clock():
    data = small_config()
    data["events"] = [{"type": "outage", "start_date": "2026-08-29", "end_date": "2026-08-31"}]
    schedule = build_schedule(data)
    assert schedule == build_schedule(data)
    assert [run.number for run in schedule.runs] == list(range(1, 10))
    assert [item.date for item in schedule.outages] == [date(2026, 8, day) for day in (29, 30, 31)]
    assert all(item.cause == "injected" for item in schedule.outages)
    assert all(run.date == run.run_start.date() for run in schedule.runs)
    assert all("21:30" <= run.run_start.strftime("%H:%M") <= "23:45" for run in schedule.runs)


def test_outage_extensions_and_no_executed_nights():
    data = small_config()
    data["calendar"]["outage_extend_prob"] = 1
    data["events"] = [{"type": "outage", "date": "2026-08-29"}]
    schedule = build_schedule(data)
    assert [run.date for run in schedule.runs] == [date(2026, 8, 27), date(2026, 8, 28)]
    data["calendar"]["outage_night_rate"] = 1
    with pytest.raises(ConfigError, match="zero executed runs"):
        build_schedule(data)


def test_suite_uniqueness_order_and_other_app_invariance():
    data = small_config()
    schedule, apps, events = prepare(data)
    fixtures = [{test.suite for test in app.tests} for app in apps]
    assert fixtures[0].isdisjoint(fixtures[1])
    for app in apps:
        for suite_id in app.suite_ids:
            names = [test.name for test in app.tests if test.suite_id == suite_id]
            assert len(names) == len(set(names))
        assert list(membership(app, schedule.runs[0], events)) == list(range(app.base_test_count))
    changed = deepcopy(data)
    changed["apps"][0]["test_count"] += 70
    _, changed_apps, _ = prepare(changed)
    assert apps[1] == changed_apps[1]
    assert apps == prepare(data)[1]


def test_drift_removals_are_subsequence_and_additions_append():
    data = small_config()
    data["events"] = [
        {"type": "removal", "date": "2026-08-28", "app": "atlas", "targets": {"test_ids": [2, 3]}},
        {"type": "addition", "date": "2026-08-30", "app": "atlas", "parameters": {"count": 4, "born_failing": True, "duration": 2}},
    ]
    schedule, apps, events = prepare(data)
    before, removed, added = [list(membership(apps[0], schedule.runs[i], events)) for i in (0, 1, 3)]
    assert removed == [test for test in before if test not in (2, 3)]
    assert added == removed + list(range(600, 604))
    born = next(event for event in events if event.type == "born_failing")
    assert born.targets == (600, 601, 602, 603)
    assert born.end_run - born.start_run + 1 == 2


def test_generated_flake_count_does_not_perturb_persistent_events():
    data = small_config()
    data["calendar"].update(start_date="2025-09-08", end_date="2026-09-07")
    _, _, first = prepare(data)
    data["failure_model"]["flaky"]["tests"] = [20, 20]
    _, _, second = prepare(data)
    assert [e for e in first if e.type == "persistent"] == [e for e in second if e.type == "persistent"]
    assert [e for e in first if e.type == "regression"] == [e for e in second if e.type == "regression"]


def test_outage_injection_resolution_and_unfired_events_retained():
    data = small_config()
    data["events"] = [
        {"type": "outage", "date": "2026-08-29"},
        {"type": "outage", "date": "2026-09-07"},
        {"type": "regression", "date": "2026-08-29", "app": "beacon", "targets": {"test_ids": [4]}, "parameters": {"duration": 2}},
        {"type": "partial", "date": "2026-08-29", "app": "atlas", "parameters": {"mode": "empty"}},
        {"type": "persistent", "date": "2026-09-07", "app": "beacon", "targets": {"test_ids": [7]}},
        {"type": "removal", "date": "2026-09-07", "app": "atlas", "targets": {"test_ids": [2]}},
        {"type": "partial", "date": "2026-09-07", "app": "atlas", "parameters": {"mode": "absent"}},
    ]
    schedule, apps, events = prepare(data)
    regression = next(e for e in events if e.source == "injected" and e.type == "regression")
    assert schedule.runs[regression.start_run - 1].date == date(2026, 8, 30)
    partial = get_partial(apps[0], schedule.runs[regression.start_run - 1], events)
    assert partial.start_date == date(2026, 8, 29)
    unfired = [event for event in events if event.parameters.get("unfired")]
    assert {event.type for event in unfired} == {"persistent", "removal", "partial"}
    assert resolve_window(schedule, date(2026, 8, 29), date(2026, 8, 29)) == (3, 3)


def one_case():
    data = small_config()
    data["failure_model"]["background_noise_rate"] = 1
    app = AppSuite(0, "atlas", [Case(0, 0, "net.TcpSocketTest", "Connects")], 1)
    run = Run(1, date(2026, 8, 27), datetime(2026, 8, 27, 22, tzinfo=timezone.utc))
    params = dict(p_calm_to_flare=1, p_flare_to_calm=1, p_calm=0, p_flare=1)
    types = ["persistent", "born_failing", "regression", "correlated", "flaky"]
    events = [Event(index, kind, app.name, run.date, run.date, 1, 1000, (0,), params if kind == "flaky" else {})
              for index, kind in enumerate(types, 1)]
    return data, app, run, events


@pytest.mark.parametrize("winning", ["persistent", "born_failing", "regression", "correlated", "flaky", "noise", None])
def test_fixed_cause_precedence(winning):
    data, app, run, events = one_case()
    order = ["persistent", "born_failing", "regression", "correlated", "flaky", "noise", None]
    events = [event for event in events if order.index(event.type) >= order.index(winning)]
    if winning is None:
        data["failure_model"]["background_noise_rate"] = 0
    outcome = simulate_run(data, app, run, events, "typical", create_markov_state(app, events))
    assert outcome.cause.tolist() == [winning]
    assert outcome.outcome.tolist() == ["pass" if winning is None else "fail"]
    assert outcome.event_id[0] == (-1 if winning in ("noise", None) else order.index(winning) + 1)


@pytest.mark.parametrize("mode,cause", [("absent", "partial"), ("empty", "partial"),
                                        ("truncated_wellformed", "truncated"), ("truncated_malformed", "truncated")])
def test_infrastructure_wins_and_markov_still_advances(mode, cause):
    data, app, run, events = one_case()
    partial = Event(99, "partial", app.name, run.date, run.date, 1, 1, (0,), {"mode": mode})
    state = create_markov_state(app, events)
    outcome = simulate_run(data, app, run, events, "typical", state, partial)
    assert outcome.outcome.tolist() == ["missing"]
    assert outcome.cause.tolist() == [cause]
    assert outcome.event_id.tolist() == [99]
    assert state.flaring.tolist() == [True]


def test_markov_outages_do_not_advance_and_transition_precedes_roll():
    data, app, run, events = one_case()
    events = [event for event in events if event.type == "flaky"]
    data["failure_model"]["background_noise_rate"] = 0
    state = create_markov_state(app, events)
    states, outcomes = [], []
    for number, day in enumerate((27, 29, 30), 1):
        current = replace(run, number=number, date=date(2026, 8, day))
        result = simulate_run(data, app, current, events, "typical", state)
        states.append(bool(state.flaring[0]))
        outcomes.append(result.outcome[0])
    assert states == [True, False, True]
    assert outcomes == ["fail", "pass", "fail"]


def test_markov_transition_statistics():
    data, app, run, events = one_case()
    app = replace(app, tests=[Case(i, 0, "net.TcpSocketTest", f"Case{i}") for i in range(1000)], base_test_count=1000)
    event = replace(events[-1], targets=tuple(range(1000)), parameters=dict(
        p_calm_to_flare=.05, p_flare_to_calm=.35, p_calm=0, p_flare=0))
    data["failure_model"]["background_noise_rate"] = 0
    state = create_markov_state(app, [event])
    calm_count = flare_count = became_flare = became_calm = 0
    for number in range(1, 121):
        previous = state.flaring.copy()
        simulate_run(data, app, replace(run, number=number), [event], "typical", state)
        calm_count += int((~previous).sum())
        flare_count += int(previous.sum())
        became_flare += int((~previous & state.flaring).sum())
        became_calm += int((previous & ~state.flaring).sum())
    assert abs(became_flare / calm_count - .05) < .005
    assert abs(became_calm / flare_count - .35) < .015


def test_failure_texts_stable_and_exercise_xml_characters():
    data = small_config()
    _, apps, _ = prepare(data)
    texts = build_failure_texts(data, apps[0])
    assert texts == build_failure_texts(data, apps[0])
    special = [text for text in texts.values() if "café" in text.message]
    assert 2 <= len(special) <= 30
    assert all("<" in text.message and "&" in text.message and '"' in text.message for text in special)
    assert all(text.type == "" for text in texts.values())


def test_injected_flaky_parameters_are_individual_and_explicit_values_honored():
    data = small_config()
    data["events"] = [{"type": "flaky", "date": "2026-08-27", "app": "beacon",
                       "targets": {"test_ids": [0, 1]}, "parameters": {"p_calm": .01}}]
    _, apps, events = prepare(data)
    event = next(e for e in events if e.type == "flaky" and e.source == "injected")
    assert event.parameters["per_test"]["0"]["p_calm"] == .01
    assert event.parameters["per_test"]["0"] != event.parameters["per_test"]["1"]
    state = create_markov_state(apps[1], events)
    assert state.p_calm[0] == state.p_calm[1] == .01


def test_shuffled_partial_targets_match_emission_suite_prefix():
    data = small_config()
    data["emit"]["shuffle_per_run"] = True
    data["events"] = [{"type": "partial", "date": "2026-08-27", "app": "atlas",
                       "parameters": {"mode": "truncated_wellformed", "keep_suites": 3}}]
    schedule, apps, events = prepare(data)
    app, run = apps[0], schedule.runs[0]
    partial = get_partial(app, run, events)
    kept = set(suite_order(data, app, run, range(app.base_test_count))[:3])
    assert set(partial.targets) == {test.id for test in app.tests if test.suite_id not in kept}


def test_born_failing_events_distinguish_generated_and_injected_additions():
    data = small_config()
    data["apps"][0]["drift"]["additions"] = 1
    data["failure_model"]["born_failing_prob"] = 1
    data["events"] = [{"type": "addition", "date": "2026-08-27", "app": "atlas",
                       "parameters": {"count": 2, "born_failing": True}}]
    _, _, events = prepare(data)
    born = [event for event in events if event.type == "born_failing"]
    assert len(born) == 2
    assert len({event.event_id for event in events}) == len(events)
    assert set(born[0].targets).isdisjoint(born[1].targets)


def test_overlapping_flake_injections_retained_and_windows_apply_per_test():
    data = small_config()
    data["failure_model"]["background_noise_rate"] = 0
    data["failure_model"]["flaky"]["apps"] = [0, 0]
    fail = dict(p_calm_to_flare=0, p_flare_to_calm=0, p_calm=1, p_flare=1)
    passing = dict(fail, p_calm=0, p_flare=0)
    data["events"] = [
        {"type": "flaky", "start_date": "2026-08-27", "end_date": "2026-08-30", "app": "beacon",
         "targets": {"test_ids": [0, 1]}, "parameters": fail},
        {"type": "flaky", "start_date": "2026-08-28", "end_date": "2026-08-29", "app": "beacon",
         "targets": {"test_ids": [0]}, "parameters": passing},
        {"type": "flaky", "start_date": "2026-08-31", "end_date": "2026-09-01", "app": "beacon",
         "targets": {"test_ids": [0]}, "parameters": fail},
    ]
    schedule, apps, events = prepare(data)
    flakes = [event for event in events if event.type == "flaky"]
    assert len(flakes) == 3
    assert flakes[0].targets == (0, 1)
    app = apps[1]
    state = create_markov_state(app, events)
    result = [simulate_run(data, app, run, events, "typical", state) for run in schedule.runs]
    assert result[0].outcome[:2].tolist() == ["fail", "fail"]
    assert result[1].outcome[:2].tolist() == ["pass", "fail"]
    assert result[1].event_id[1] == flakes[0].event_id
    assert result[3].outcome[:2].tolist() == ["fail", "fail"]
    assert result[4].outcome[:2].tolist() == ["fail", "pass"]
    assert result[4].event_id[0] == flakes[2].event_id
    assert result[6].outcome[:2].tolist() == ["pass", "pass"]


def test_generated_flake_resumes_after_injected_override():
    data, app, run, events = one_case()
    data["failure_model"]["background_noise_rate"] = 0
    generated = replace(events[-1], parameters=dict(
        p_calm_to_flare=0, p_flare_to_calm=0, p_calm=1, p_flare=1))
    injected = replace(generated, event_id=999, start_run=2, end_run=2, source="injected",
                       parameters=dict(generated.parameters, p_calm=0, p_flare=0))
    state = create_markov_state(app, [generated, injected])
    result = [simulate_run(data, app, replace(run, number=i), [generated, injected], "typical", state)
              for i in (1, 2, 3)]
    assert [outcome.outcome[0] for outcome in result] == ["fail", "pass", "fail"]
    assert result[0].event_id[0] == result[2].event_id[0] == generated.event_id


def test_explicit_target_count_cannot_exceed_resolved_suite_pool():
    data = small_config()
    data["events"] = [{"type": "regression", "date": "2026-08-27", "app": "atlas",
                       "targets": {"suite_ids": [0], "count": 100}}]
    with pytest.raises(ConfigError, match="resolved pool"):
        prepare(data)


@pytest.mark.parametrize("outage", [False, True])
def test_conflicting_partial_injections_rejected_after_outage_resolution(outage):
    data = small_config()
    data["events"] = [
        {"type": "partial", "date": "2026-08-28", "app": "atlas", "parameters": {"mode": "absent"}},
        {"type": "partial", "date": "2026-08-29" if outage else "2026-08-28", "app": "atlas",
         "parameters": {"mode": "empty"}},
    ]
    if outage:
        data["events"].append({"type": "outage", "date": "2026-08-28"})
    with pytest.raises(ConfigError, match=r"events\[0\] and events\[1\].*atlas.*run 2"):
        prepare(data)
