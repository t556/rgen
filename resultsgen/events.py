"""Plan dated failure and infrastructure patterns using independent archetype keys."""
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import date, timedelta
import logging

import numpy as np

from .config import ConfigError, config_dict
from .rng import Purpose, generator
from .schedule import Schedule, event_dates

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Event:
    event_id: int
    type: str
    app: str
    start_date: date
    end_date: date | None
    start_run: int
    end_run: int
    targets: tuple[int, ...]
    parameters: dict = field(default_factory=dict)
    source: str = "generated"


def event_id(app_index: int, family: int, ordinal: int) -> int:
    return (app_index << 40) | (family << 32) | (ordinal + 1)


def integer(rng, bounds) -> int:
    return int(rng.integers(int(bounds[0]), int(bounds[1]) + 1))


def resolve_window(schedule: Schedule, start: date, end: date | None = None,
                   duration: int | None = None) -> tuple[int, int]:
    dates = [run.date for run in schedule.runs]
    first = bisect_left(dates, start) + 1
    last = bisect_right(dates, end) if end is not None else len(dates)
    if end is not None and first <= len(dates):
        # A range entirely within an outage resolves to the next executed run.
        last = max(first, last)
    if duration is not None:
        last = min(last, first + duration - 1)
    return first, last


def target_ids(app, spec: dict, rng, default_count: int = 1) -> tuple[int, ...]:
    """Resolve numeric identities; explicit suites and tests form a union."""
    explicit = set(spec.get("test_ids", []))
    if "suite_ids" in spec:
        explicit.update(test.id for test in app.tests if test.suite_id in spec["suite_ids"])
    if explicit or "test_ids" in spec or "suite_ids" in spec:
        choices = sorted(explicit)
    else:
        choices = list(range(app.base_test_count))
    count = spec.get("count", None if explicit else default_count)
    if "count" in spec and count > len(choices):
        raise ConfigError(f"events targets.count: requested {count} tests from a resolved pool "
                          f"of {len(choices)} for {app.name}")
    if count is not None and count < len(choices):
        choices = sorted(int(value) for value in rng.choice(choices, size=count, replace=False))
    return tuple(choices)


def get_partial(app, run, events) -> Event | None:
    return next((event for event in events if event.type == "partial" and event.app == app.name
                 and event.start_run <= run.number <= event.end_run), None)


def plan_events(cfg, schedule: Schedule, suites, personalities, drift) -> list[Event]:
    from .suite import membership, suite_order

    data = config_dict(cfg)
    events = list(drift)
    if not schedule.runs:
        return events
    first_date = date.fromisoformat(data["calendar"]["start_date"])
    last_date = date.fromisoformat(data["calendar"]["end_date"])
    days = (last_date - first_date).days + 1
    years = days / 365.25
    model = data["failure_model"]

    def dated(kind, app, family, ordinal, start, targets, duration=None, parameters=None,
              source="generated", end=None):
        start_run, end_run = resolve_window(schedule, start, end, duration)
        if source != "injected" and (start_run > end_run or not targets and kind != "partial"):
            return None
        params = dict(parameters or {})
        if start_run > end_run:
            params["unfired"] = True
        resolved_end = end if end is not None else schedule.runs[end_run - 1].date
        event = Event(event_id(app.app_index, family, ordinal), kind, app.name, start,
                      resolved_end, start_run, end_run, tuple(targets), params, source)
        events.append(event)
        return event

    for app, personality in zip(suites, personalities):
        multipliers = data["personalities"][personality]
        rng = generator(data["seed"], Purpose.EVENTS, app.app_index, 0)
        settings = model["persistent"]
        count = min(app.base_test_count, round(integer(rng, settings["count"]) *
                                               multipliers["persistent_count"] * years))
        targets = rng.choice(app.base_test_count, count, replace=False)
        for i, test_id in enumerate(targets):
            item = generator(data["seed"], Purpose.EVENTS, app.app_index, 0, i)
            start = first_date if i < round(count / 3) else first_date + timedelta(days=int(item.integers(days)))
            never_probability = max(settings["never_fixed_prob"], multipliers["never_fixed_prob"])
            never = bool(item.random() < never_probability)
            duration = None if never else max(1, round(float(item.lognormal(
                np.log(settings["duration_median"] * multipliers["persistent_duration"]),
                settings["duration_sigma"]))))
            dated("persistent", app, 10, i, start, (int(test_id),), duration,
                  {"duration": duration, "never_fixed": never})

        for family, kind in ((11, "regression"), (12, "correlated")):
            rng = generator(data["seed"], Purpose.EVENTS, app.app_index, family)
            settings = model[kind]
            count = round(integer(rng, settings["events"]) * multipliers[f"{kind}_events"] * years)
            for i in range(count):
                item = generator(data["seed"], Purpose.EVENTS, app.app_index, family, i)
                suite_ids = sorted({test.suite_id for test in app.tests[:app.base_test_count]})
                number = 1 if kind == "correlated" else min(len(suite_ids), int(item.integers(1, 4)))
                chosen = set(int(value) for value in item.choice(suite_ids, number, replace=False))
                candidates = [test.id for test in app.tests[:app.base_test_count] if test.suite_id in chosen]
                targets = candidates if kind == "correlated" else sorted(int(value) for value in item.choice(
                    candidates, min(len(candidates), integer(item, settings["tests"])), replace=False))
                duration = integer(item, settings["duration"])
                start = first_date + timedelta(days=int(item.integers(days)))
                dated(kind, app, family, i, start, targets, duration, {"duration": duration})

    # Select globally, but use per-app/test streams for the four flake parameters.
    selection = generator(data["seed"], Purpose.EVENTS, 0, 99)
    preferred = {"noisy": 0, "typical": 1, "legacy": 2}
    eligible = [app.app_index for app, personality in zip(suites, personalities)
                if data["personalities"][personality]["flaky_eligible"] and personality != "stable"]
    tie = {index: float(generator(data["seed"], Purpose.EVENTS, index, 98).random()) for index in eligible}
    eligible.sort(key=lambda index: (preferred.get(personalities[index], 3), tie[index]))
    for index in eligible[:integer(selection, model["flaky"]["apps"])]:
        app = suites[index]
        rng = generator(data["seed"], Purpose.EVENTS, index, 13)
        count = min(app.base_test_count, integer(rng, model["flaky"]["tests"]))
        targets = sorted(int(value) for value in rng.choice(app.base_test_count, count, replace=False))
        for i, test_id in enumerate(targets):
            item = generator(data["seed"], Purpose.EVENTS, index, 13, test_id)
            params = {key: float(item.uniform(*model["flaky"][key])) for key in (
                "p_calm_to_flare", "p_flare_to_calm", "p_calm", "p_flare")}
            dated("flaky", app, 13, i, first_date, (test_id,), parameters=params)

    # Additions are the sole source of born-failing events.
    for addition in [event for event in drift if event.type == "addition"]:
        app = next(app for app in suites if app.name == addition.app)
        born_family = 15 if addition.source == "injected" else 14
        rng = generator(data["seed"], Purpose.EVENTS, app.app_index, born_family, addition.event_id)
        born = addition.parameters.get("born_failing")
        if born is None:
            born = bool(rng.random() < model["born_failing_prob"])
        if born:
            duration = addition.parameters.get("duration", integer(rng, model["born_failing_nights"]))
            dated("born_failing", app, born_family, addition.event_id & 0xFFFFFFFF,
                  addition.start_date, addition.targets, duration, {"duration": duration})

    # Keep all injections in the manifest. Active injected flake parameters take
    # priority per test in the simulation, without deleting earlier patterns.
    for i, injected in enumerate(data["events"]):
        kind = injected["type"]
        if kind in ("outage", "removal", "addition", "partial"):
            continue
        app = next(app for app in suites if app.name == injected["app"])
        start, end = event_dates(injected)
        end = end if "end_date" in injected else None
        params = dict(injected.get("parameters", {}))
        rng = generator(data["seed"], Purpose.EVENTS, app.app_index, 90, i)
        default = app.base_test_count if kind == "correlated" else 1
        targets = target_ids(app, injected.get("targets", {}), rng, default)
        if kind == "correlated" and not injected.get("targets"):
            chosen_suite = int(rng.choice(app.suite_ids))
            targets = tuple(test.id for test in app.tests if test.suite_id == chosen_suite)
        if kind == "flaky":
            per_test = {}
            for test_id in targets:
                item = generator(data["seed"], Purpose.EVENTS, app.app_index, 91, i, test_id)
                per_test[str(test_id)] = {
                    key: params[key] if key in params else float(item.uniform(*model["flaky"][key]))
                    for key in ("p_calm_to_flare", "p_flare_to_calm", "p_calm", "p_flare")}
            params["per_test"] = per_test
        duration = params.get("duration")
        if kind in ("regression", "correlated") and duration is None and end is None:
            duration = integer(rng, model[kind]["duration"])
            params["duration"] = duration
        dated(kind, app, 90, i, start, targets, duration, params, "injected", end)

    # Partial events are resolved per file, including exact missing membership.
    for app in suites:
        app_config = data["apps"][app.app_index]
        partial_rate = app_config["partial_rate"]
        partial_rate = data["partial"]["rate"] if partial_rate is None else partial_rate
        weights = data["partial"]["mode_weights"]
        modes = list(weights)
        probabilities = np.array(list(weights.values()), dtype=float)
        probabilities /= probabilities.sum()
        injections = []
        for i, injected in enumerate(data["events"]):
            if injected["type"] == "partial" and injected["app"] == app.name:
                start, end = event_dates(injected)
                # A single outage date takes effect on the next executed night.
                end = end if "end_date" in injected else None
                window = resolve_window(schedule, start, end, None if end else 1)
                for previous_i, _, previous_window in injections:
                    overlap_start = max(window[0], previous_window[0])
                    overlap_end = min(window[1], previous_window[1])
                    if overlap_start <= overlap_end:
                        raise ConfigError(f"events[{previous_i}] and events[{i}]: partial injections for "
                                          f"{app.name} both resolve to run {overlap_start}; "
                                          "one file cannot have two infrastructure modes")
                injections.append((i, injected, window))
        for run in schedule.runs:
            rng = generator(data["seed"], Purpose.PARTIAL, app.app_index, run.number)
            injected = next(((i, item) for i, item, (start, end) in injections
                             if start <= run.number <= end), None)
            if injected is None and rng.random() >= partial_rate:
                continue
            params = dict(injected[1].get("parameters", {})) if injected else {}
            mode = params.get("mode") or str(rng.choice(modes, p=probabilities))
            ids = membership(app, run, drift)
            present_suites = suite_order(data, app, run, ids)
            keep = 0 if mode in ("absent", "empty") else int(params.get(
                "keep_suites", rng.integers(0, max(1, len(present_suites)))))
            if injected and mode.startswith("truncated") and "keep_suites" in params and keep >= len(present_suites):
                raise ConfigError(f"events[{injected[0]}].parameters.keep_suites: {keep} complete suites "
                                  f"leave no suite to truncate for {app.name} on {run.date} "
                                  f"({len(present_suites)} suites remain after drift)")
            kept = set(present_suites[:keep])
            missing = tuple(int(test_id) for test_id in ids
                            if app.tests[int(test_id)].suite_id not in kept)
            params.update(mode=mode, keep_suites=keep)
            if injected:
                original_start, original_end = event_dates(injected[1])
                events.append(Event(event_id(app.app_index, 20, run.number), "partial", app.name,
                                    original_start, original_end, run.number, run.number, missing,
                                    params, "injected"))
            else:
                dated("partial", app, 20, run.number, run.date, missing, 1, params)
        for i, injected, (start, end) in injections:
            if start <= end:
                continue
            original_start, original_end = event_dates(injected)
            params = dict(injected.get("parameters", {}), unfired=True)
            events.append(Event(event_id(app.app_index, 90, i), "partial", app.name,
                                original_start, original_end, start, end, (), params, "injected"))

    events.sort(key=lambda event: (event.start_run, event.app, event.event_id))
    for event in events:
        logger.debug("Planned %s event %d for %s, runs %d–%d, %d targets", event.type,
                     event.event_id, event.app, event.start_run, event.end_run, len(event.targets))
    return events
