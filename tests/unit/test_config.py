"""Contract tests for edge validation and reproducible configuration."""

from copy import deepcopy
import json

import pytest

from resultsgen.config import ConfigError, OutputError, config_dict, config_hash, load_config, preset, save_config, validate_config


@pytest.mark.parametrize("name", ["default", "dev"])
def test_presets_resolve_and_round_trip(name, tmp_path):
    config = preset(name)
    assert validate_config(config) == []
    destination = tmp_path / "nested" / f"{name}.json"
    save_config(config, destination)
    assert load_config(destination) == config
    assert load_config(destination.read_text()) == config
    assert config_hash(load_config(destination)) == config_hash(config)
    assert all(app.duration_minutes is not None for app in config.apps)


def test_collects_three_independent_errors():
    with pytest.raises(ConfigError) as caught:
        load_config({"seed": -1, "overwrite": "yes", "calendar": {"outage_night_rate": 2}})
    assert len(caught.value.errors) == 3
    for field in ("seed", "overwrite", "calendar.outage_night_rate"):
        assert field in str(caught.value)


def test_unknown_keys_rejected_at_every_depth():
    source = config_dict(preset("dev"))
    source["typo"] = 0
    source["calendar"]["typo"] = 0
    source["apps"][0]["drift"]["typo"] = 0
    source["failure_model"]["flaky"]["typo"] = 0
    source["personalities"]["legacy"]["typo"] = 0
    source["partial"]["mode_weights"]["typo"] = 0
    source["events"][1]["parameters"]["typo"] = 0
    source["events"][5]["targets"]["typo"] = 0
    source["events"][5]["run_number"] = 1
    with pytest.raises(ConfigError) as caught:
        load_config(source)
    assert len(caught.value.errors) == 9
    assert all("unknown key" in error for error in caught.value.errors)


def test_invalid_nested_types_are_collected_without_crashing():
    with pytest.raises(ConfigError) as caught:
        load_config({"calendar": [], "apps": [None, {"name": [], "test_count": True, "suite_count": "x", "drift": []}], "failure_model": None, "events": [False]})
    assert len(caught.value.errors) >= 8


def test_dates_app_references_and_cross_field_errors_are_collected():
    source = config_dict(preset("dev"))
    source["apps"][0]["suite_count"] = 1000
    source["events"][0]["date"] = "2025-01-01"
    source["events"][1]["app"] = "unknown"
    source["events"][5]["targets"]["test_ids"] = [99999]
    source["partial"]["mode_weights"]["absent"] = 0.8
    with pytest.raises(ConfigError) as caught:
        load_config(source)
    message = str(caught.value)
    assert "cannot exceed test_count" in message
    assert "within calendar range" in message
    assert "existing app name" in message
    assert "outside app's base" in message
    assert "sum to 1" in message


@pytest.mark.parametrize("patch", [
    {"seed": True}, {"schema_version": 2}, {"timezone": "Imaginary/Nowhere"},
    {"calendar": {"days_of_week": [0, 0]}},
    {"calendar": {"start_date": "2026-09-07", "end_date": "2026-09-06"}},
    {"calendar": {"run_start_window": ["23:45", "21:30"]}},
    {"calendar": {"outage_night_rate": float("nan")}},
    {"failure_model": {"persistent": {"count": [8, 2]}}},
    {"archive_name": "../results"}, {"emit": {"file_name_template": "../{app}.xml"}},
    {"emit": {"file_name_template": "{other}.xml"}},
    {"emit": {"file_name_template": "{app:.0}.xml"}},
    {"emit": {"failure_type_literal": "failure\u0000type"}},
    {"emit": {"time_zero_literal": "1"}},
    {"personalities": {"legacy": {"persistent_duration": 0}}},
    {"output_dir": "out\u0000side"},
])
def test_invalid_scalar_and_range_inputs(patch):
    with pytest.raises(ConfigError):
        load_config(patch)


def test_mutation_does_not_affect_input_or_other_configs():
    supplied = {"apps": [{"name": "tiny", "test_count": 10, "suite_count": 2}]}
    original = deepcopy(supplied)
    config = load_config(supplied)
    assert supplied == original
    exported = config_dict(config)
    exported["apps"][0]["test_count"] = 999
    assert config.apps[0].test_count == 10
    other = load_config(supplied)
    config.calendar.days_of_week.pop()
    assert len(other.calendar.days_of_week) == 7


def test_hash_is_key_order_independent_and_changes_with_seed():
    a = load_config({"seed": 1, "archive_name": "test"})
    b = load_config({"archive_name": "test", "seed": 1})
    c = load_config({"archive_name": "test", "seed": 2})
    assert config_hash(a) == config_hash(b)
    assert config_hash(a) != config_hash(c)


def test_dev_contains_all_required_injected_patterns():
    config = config_dict(preset("dev"))
    assert [app["test_count"] for app in config["apps"]] == [600, 120]
    events = config["events"]
    assert sum(event["type"] == "outage" for event in events) == 1
    assert {event["parameters"]["mode"] for event in events if event["type"] == "partial"} == {"absent", "empty", "truncated_wellformed", "truncated_malformed"}
    assert sum(event["type"] == "regression" for event in events) == 1
    assert sum(len(event["targets"]["test_ids"]) for event in events if event["type"] == "flaky") == 2
    assert sum(event["type"] == "removal" for event in events) == 1


def test_addition_and_date_range_schema():
    source = config_dict(preset("dev"))
    source["events"] = [
        {"type": "outage", "start_date": "2026-08-28", "end_date": "2026-08-29"},
        {"type": "addition", "date": "2026-09-01", "app": "beacon", "parameters": {"count": 2, "suite_id": 0, "born_failing": True, "duration": 3}},
    ]
    assert load_config(source).events == source["events"]


def test_target_selector_union_and_subselection():
    source = config_dict(preset("dev"))
    source["events"] = [{"type": "regression", "date": "2026-08-27", "app": "atlas",
                         "targets": {"test_ids": [0, 1], "suite_ids": [1], "count": 3}}]
    assert load_config(source).events == source["events"]
    source["events"][0]["targets"] = {"test_ids": [], "suite_ids": [0]}
    assert validate_config(source) == []


@pytest.mark.parametrize("targets", [
    {"test_ids": []}, {"test_ids": [], "suite_ids": [], "count": 1},
    {"count": 601}, {"test_ids": [0, 1], "count": 3},
    {"suite_ids": [12]}, {"test_ids": [600]},
])
def test_impossible_target_selectors_are_errors(targets):
    source = config_dict(preset("dev"))
    source["events"] = [{"type": "regression", "date": "2026-08-27", "app": "atlas", "targets": targets}]
    with pytest.raises(ConfigError, match="targets"):
        load_config(source)


def test_explicit_partial_instruction_must_be_possible_for_base_suite():
    source = config_dict(preset("dev"))
    source["events"] = [{"type": "partial", "date": "2026-08-27", "app": "atlas",
                         "parameters": {"mode": "truncated_wellformed", "keep_suites": 12}}]
    with pytest.raises(ConfigError, match="keep_suites"):
        load_config(source)


def test_local_and_iana_timezones_are_preserved():
    for timezone in ("local", "America/New_York", "Europe/London"):
        assert load_config({"timezone": timezone}).timezone == timezone


def test_bad_json_and_missing_paths_are_config_errors(tmp_path):
    with pytest.raises(ConfigError, match="invalid JSON"):
        load_config('{"seed":')
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "missing.json")
    with pytest.raises(ConfigError, match="expected an object"):
        load_config("[]")


def test_save_filesystem_failure_is_output_error(tmp_path):
    destination = tmp_path / "directory.json"
    destination.mkdir()
    with pytest.raises(OutputError):
        save_config(preset("dev"), destination)


def test_resolved_json_has_every_default():
    config = load_config({})
    resolved = config_dict(config)
    assert resolved["apps"][0]["duration_minutes"] == [180, 360]
    assert resolved["apps"][1]["duration_minutes"] == [40, 120]
    assert resolved["apps"][-1]["duration_minutes"] == [8, 45]
    assert resolved["personalities"]["legacy"]["never_fixed_prob"] == 0.3
    assert load_config(json.dumps(resolved)) == config


def test_partial_rate_inheritance_survives_save_and_global_edits(tmp_path):
    config = load_config({"partial": {"rate": 0.01}, "apps": [
        {"name": "inherited"}, {"name": "overridden", "partial_rate": 0.2},
    ]})
    destination = tmp_path / "config.json"
    save_config(config, destination)
    reloaded = config_dict(load_config(destination))
    assert reloaded["apps"][0]["partial_rate"] is None
    assert reloaded["apps"][1]["partial_rate"] == 0.2
    reloaded["partial"]["rate"] = 0.5
    changed = load_config(reloaded)
    assert changed.apps[0].partial_rate is None
    assert changed.partial.rate == 0.5
    assert changed.apps[1].partial_rate == 0.2
    # Duration defaults are resolved from the initial suite size and remain
    # explicit after saving; null can be used to request a fresh size default.
    assert changed.apps[0].duration_minutes == [8, 45]


def test_failure_model_default_ranges_are_exposed():
    model = config_dict(load_config({}))["failure_model"]
    assert model["persistent"]["count"] == [2, 8]
    assert model["persistent"]["duration_median"] == 60
    assert model["regression"] == {"events": [1, 3], "tests": [3, 40], "duration": [3, 25]}
    assert model["correlated"] == {"events": [0, 2], "duration": [1, 2]}
    assert model["flaky"]["p_calm_to_flare"] == [0.02, 0.06]
    assert model["flaky"]["p_flare_to_calm"] == [0.25, 0.5]
