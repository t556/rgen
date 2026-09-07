"""Configuration schema, resolution, and aggregated edge validation.

All configuration is JSON-compatible. The loader fills defaults and validates
once; downstream stages consume ``config_dict(config)`` without revalidation.
Preset dates are fixed at creation so loading a preset never changes its hash.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import date, time, timedelta
import hashlib
from importlib.resources import files
import json
import math
from pathlib import Path
import re
from string import Formatter
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigError(ValueError):
    """One or more invalid user inputs, collected in ``errors``."""

    def __init__(self, errors: list[str] | str):
        self.errors = [errors] if isinstance(errors, str) else list(errors)
        super().__init__("Invalid configuration:\n" + "\n".join(f"- {e}" for e in self.errors))


class OutputError(OSError):
    """An output destination or filesystem operation failed."""


PARTIAL_MODES = ("absent", "empty", "truncated_wellformed", "truncated_malformed")
PERSONALITY_NAMES = ("stable", "typical", "noisy", "legacy")
EVENT_TYPES = ("outage", "partial", "persistent", "regression", "correlated", "flaky", "removal", "addition")
PRESET_DATE = date(2026, 9, 7)


@dataclass
class CalendarConfig:
    start_date: str = (PRESET_DATE - timedelta(days=410)).isoformat()
    end_date: str = PRESET_DATE.isoformat()
    days_of_week: list[int] = field(default_factory=lambda: list(range(7)))
    run_start_window: list[str] = field(default_factory=lambda: ["21:30", "23:45"])
    outage_night_rate: float = 0.01
    outage_extend_prob: float = 0.3


@dataclass
class DriftConfig:
    removals: int = 0
    additions: int = 0


@dataclass
class AppConfig:
    name: str = "app"
    test_count: int = 100
    suite_count: int = 1
    personality: str = "random"
    duration_minutes: list[float] | None = None
    # Keep inheritance explicit when saving so changing the global knob still
    # affects apps that have no individual override.
    partial_rate: float | None = None
    drift: DriftConfig = field(default_factory=DriftConfig)


@dataclass
class PersistentConfig:
    count: list[int] = field(default_factory=lambda: [2, 8])
    duration_median: float = 60.0
    duration_sigma: float = 0.7
    never_fixed_prob: float = 0.0


@dataclass
class RegressionConfig:
    events: list[int] = field(default_factory=lambda: [1, 3])
    tests: list[int] = field(default_factory=lambda: [3, 40])
    duration: list[int] = field(default_factory=lambda: [3, 25])


@dataclass
class CorrelatedConfig:
    events: list[int] = field(default_factory=lambda: [0, 2])
    duration: list[int] = field(default_factory=lambda: [1, 2])


@dataclass
class FlakyConfig:
    apps: list[int] = field(default_factory=lambda: [1, 2])
    tests: list[int] = field(default_factory=lambda: [3, 15])
    p_calm_to_flare: list[float] = field(default_factory=lambda: [0.02, 0.06])
    p_flare_to_calm: list[float] = field(default_factory=lambda: [0.25, 0.5])
    p_calm: list[float] = field(default_factory=lambda: [0.005, 0.02])
    p_flare: list[float] = field(default_factory=lambda: [0.15, 0.4])


@dataclass
class FailureModelConfig:
    target_failure_rate: float = 0.0025
    background_noise_rate: float = 0.0002
    persistent: PersistentConfig = field(default_factory=PersistentConfig)
    regression: RegressionConfig = field(default_factory=RegressionConfig)
    correlated: CorrelatedConfig = field(default_factory=CorrelatedConfig)
    flaky: FlakyConfig = field(default_factory=FlakyConfig)
    born_failing_prob: float = 0.3
    born_failing_nights: list[int] = field(default_factory=lambda: [1, 10])


@dataclass
class PersonalityConfig:
    persistent_count: float = 1.0
    persistent_duration: float = 1.0
    regression_events: float = 1.0
    correlated_events: float = 1.0
    noise: float = 1.0
    flaky_eligible: bool = True
    never_fixed_prob: float = 0.0


def _personalities() -> dict[str, PersonalityConfig]:
    return {
        "stable": PersonalityConfig(0.25, 1.0, 0.5, 0.5, 0.2, False),
        "typical": PersonalityConfig(),
        "noisy": PersonalityConfig(2.0, 1.0, 2.0, 2.0, 3.0),
        "legacy": PersonalityConfig(4.0, 3.0, 2.0, 1.0, 1.0, True, 0.3),
    }


@dataclass
class PartialConfig:
    rate: float = 0.005
    mode_weights: dict[str, float] = field(default_factory=lambda: dict(zip(PARTIAL_MODES, [0.7, 0.1, 0.1, 0.1])))


@dataclass
class EmitConfig:
    indent: int = 4
    time_zero_literal: str = "0"
    nonzero_time_fraction: float = 0.1
    failure_type_literal: str = ""
    counts_attrs: bool = False
    timestamp_attr: bool = False
    file_name_template: str = "{app}.xml"
    shuffle_per_run: bool = False


@dataclass
class TruthConfig:
    write_parquet: bool = True
    include_missing_rows: bool = True


def _default_apps() -> list[AppConfig]:
    counts = [10400, 2950, 2600, 2200, 1800, 1500, 1200, 900, 700, 520]
    suites = [320, 60, 45, 40, 30, 20, 15, 5, 1, 1]
    names = ["atlas", "beacon", "cipher", "delta", "ember", "forge", "glacier", "harbor", "ion", "juno"]
    return [AppConfig(name, count, suite_count, "legacy" if i == 0 else "random", drift=DriftConfig(4 if i == 0 else 0, 0))
            for i, (name, count, suite_count) in enumerate(zip(names, counts, suites))]


@dataclass
class Config:
    schema_version: int = 1
    seed: int = 20260907
    output_dir: str = "./out"
    archive_name: str = "results"
    overwrite: bool = False
    timezone: str = "local"
    calendar: CalendarConfig = field(default_factory=CalendarConfig)
    apps: list[AppConfig] = field(default_factory=_default_apps)
    failure_model: FailureModelConfig = field(default_factory=FailureModelConfig)
    personalities: dict[str, PersonalityConfig] = field(default_factory=_personalities)
    personality_weights: dict[str, float] = field(default_factory=lambda: {"stable": 0.3, "typical": 0.5, "noisy": 0.2})
    partial: PartialConfig = field(default_factory=PartialConfig)
    events: list[dict[str, Any]] = field(default_factory=list)
    emit: EmitConfig = field(default_factory=EmitConfig)
    truth: TruthConfig = field(default_factory=TruthConfig)


def config_dict(config: Config | dict[str, Any]) -> dict[str, Any]:
    """Return an independent JSON-compatible copy of the resolved config."""
    return deepcopy(config) if isinstance(config, dict) else asdict(config)


def _merge(defaults: dict, supplied: Any, path: str, errors: list[str]) -> dict:
    if not isinstance(supplied, dict):
        errors.append(f"{path or 'config'}: expected an object")
        return deepcopy(defaults)
    result = deepcopy(defaults)
    for key, value in supplied.items():
        key_path = f"{path}.{key}" if path else str(key)
        if key not in defaults:
            errors.append(f"{key_path}: unknown key")
        elif isinstance(defaults[key], dict):
            result[key] = _merge(defaults[key], value, key_path, errors)
        else:
            result[key] = deepcopy(value)
    return result


def _number(value: Any, path: str, errors: list[str], *, minimum: float = 0, maximum: float | None = None, integer: bool = False) -> bool:
    valid_type = type(value) is int if integer else type(value) in (int, float)
    if not valid_type or (isinstance(value, float) and not math.isfinite(value)):
        errors.append(f"{path}: expected a finite {'integer' if integer else 'number'}")
        return False
    if value < minimum or (maximum is not None and value > maximum):
        bound = f"between {minimum} and {maximum}" if maximum is not None else f"at least {minimum}"
        errors.append(f"{path}: must be {bound}")
        return False
    return True


def _prob(value: Any, path: str, errors: list[str]) -> bool:
    return _number(value, path, errors, maximum=1)


def _boolean(value: Any, path: str, errors: list[str]) -> None:
    if type(value) is not bool:
        errors.append(f"{path}: expected a boolean")


def _string(value: Any, path: str, errors: list[str], *, nonempty: bool = True) -> bool:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        errors.append(f"{path}: expected a {'nonempty ' if nonempty else ''}string")
        return False
    return True


def _range(value: Any, path: str, errors: list[str], *, minimum: float = 0, maximum: float | None = None, integer: bool = False) -> None:
    if not isinstance(value, list) or len(value) != 2:
        errors.append(f"{path}: expected a two-element range [minimum, maximum]")
        return
    valid = [_number(v, f"{path}[{i}]", errors, minimum=minimum, maximum=maximum, integer=integer) for i, v in enumerate(value)]
    if all(valid) and value[0] > value[1]:
        errors.append(f"{path}: minimum must not exceed maximum")


def _date(value: Any, path: str, errors: list[str]) -> date | None:
    if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        errors.append(f"{path}: expected an ISO date (YYYY-MM-DD)")
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        errors.append(f"{path}: invalid calendar date")
        return None


def _safe_name(value: Any, path: str, errors: list[str]) -> bool:
    if not _string(value, path, errors):
        return False
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value) is None:
        errors.append(f"{path}: use a filename-safe name starting with a letter or digit")
        return False
    return True


def _validate_events(data: dict, start: date | None, end: date | None, apps: dict[str, dict], errors: list[str]) -> None:
    events = data["events"]
    if not isinstance(events, list):
        errors.append("events: expected a list")
        return
    allowed = {"type", "date", "start_date", "end_date", "app", "targets", "parameters"}
    parameters = {
        "outage": set(), "partial": {"mode", "keep_suites"},
        "persistent": {"duration"}, "regression": {"duration"},
        "correlated": {"duration"},
        "flaky": {"p_calm_to_flare", "p_flare_to_calm", "p_calm", "p_flare"},
        "removal": set(), "addition": {"count", "suite_id", "born_failing", "duration"},
    }
    for i, event in enumerate(events):
        path = f"events[{i}]"
        if not isinstance(event, dict):
            errors.append(f"{path}: expected an object")
            continue
        for key in event.keys() - allowed:
            errors.append(f"{path}.{key}: unknown key")
        kind = event.get("type")
        if not isinstance(kind, str) or kind not in EVENT_TYPES:
            errors.append(f"{path}.type: expected one of {', '.join(EVENT_TYPES)}")
            kind = None
        has_date = "date" in event
        has_range = "start_date" in event or "end_date" in event
        if has_date == has_range:
            errors.append(f"{path}: supply date or both start_date and end_date")
        dates = {}
        for key in ("date", "start_date", "end_date"):
            if key in event:
                parsed = _date(event[key], f"{path}.{key}", errors)
                dates[key] = parsed
                if parsed is not None and start is not None and end is not None and not start <= parsed <= end:
                    errors.append(f"{path}.{key}: date must be within calendar range")
        if has_range and ("start_date" not in event or "end_date" not in event):
            errors.append(f"{path}: a date range requires both start_date and end_date")
        if dates.get("start_date") is not None and dates.get("end_date") is not None and dates["start_date"] > dates["end_date"]:
            errors.append(f"{path}: start_date must not be after end_date")
        app_name = event.get("app")
        app = apps.get(app_name) if isinstance(app_name, str) else None
        if kind == "outage":
            if "app" in event:
                errors.append(f"{path}.app: outages apply to the whole run; omit app")
        elif kind is not None and app is None:
            errors.append(f"{path}.app: must reference an existing app name")
        targets = event.get("targets", {})
        if not isinstance(targets, dict):
            errors.append(f"{path}.targets: expected an object")
        else:
            for key in targets.keys() - {"test_ids", "suite_ids", "count"}:
                errors.append(f"{path}.targets.{key}: unknown key")
            if targets and kind in ("outage", "partial", "addition"):
                errors.append(f"{path}.targets: not supported for {kind} events")
            if "count" in targets:
                good = _number(targets["count"], f"{path}.targets.count", errors, minimum=1, integer=True)
                if good and app and type(app["test_count"]) is int and targets["count"] > app["test_count"]:
                    errors.append(f"{path}.targets.count: cannot exceed the app's test_count")
            for field_name, size_name in (("test_ids", "test_count"), ("suite_ids", "suite_count")):
                if field_name not in targets:
                    continue
                ids = targets[field_name]
                p = f"{path}.targets.{field_name}"
                if not isinstance(ids, list):
                    errors.append(f"{p}: expected a list of integer identities")
                    continue
                for j, identity in enumerate(ids):
                    good = _number(identity, f"{p}[{j}]", errors, integer=True)
                    if good and app and type(app[size_name]) is int and identity >= app[size_name]:
                        errors.append(f"{p}[{j}]: identity is outside app's base {size_name}")
                if all(type(v) is int for v in ids) and len(set(ids)) != len(ids):
                    errors.append(f"{p}: identities must be unique")
            explicit = "test_ids" in targets or "suite_ids" in targets
            if explicit and targets.get("test_ids", []) == [] and targets.get("suite_ids", []) == []:
                errors.append(f"{path}.targets: explicit selectors must contain at least one identity")
            test_ids = targets.get("test_ids")
            if (isinstance(test_ids, list) and not targets.get("suite_ids")
                    and type(targets.get("count")) is int and targets["count"] > len(test_ids)):
                errors.append(f"{path}.targets.count: cannot exceed the number of selected tests")
        params = event.get("parameters", {})
        if not isinstance(params, dict):
            errors.append(f"{path}.parameters: expected an object")
            continue
        if kind is not None:
            for key in params.keys() - parameters[kind]:
                errors.append(f"{path}.parameters.{key}: unknown key for {kind}")
        for key in ("duration", "count"):
            if key in params:
                _number(params[key], f"{path}.parameters.{key}", errors, minimum=1, integer=True)
        if "suite_id" in params:
            good = _number(params["suite_id"], f"{path}.parameters.suite_id", errors, integer=True)
            if good and app and type(app["suite_count"]) is int and params["suite_id"] >= app["suite_count"]:
                errors.append(f"{path}.parameters.suite_id: identity is outside app's base suite_count")
        if "born_failing" in params:
            _boolean(params["born_failing"], f"{path}.parameters.born_failing", errors)
        for key in ("p_calm_to_flare", "p_flare_to_calm", "p_calm", "p_flare"):
            if key in params:
                _prob(params[key], f"{path}.parameters.{key}", errors)
        if kind == "partial":
            if params.get("mode") not in PARTIAL_MODES:
                errors.append(f"{path}.parameters.mode: expected one of {', '.join(PARTIAL_MODES)}")
            if "keep_suites" in params:
                good = _number(params["keep_suites"], f"{path}.parameters.keep_suites", errors, integer=True)
                if good and app and type(app["suite_count"]) is int and params["keep_suites"] >= app["suite_count"]:
                    errors.append(f"{path}.parameters.keep_suites: must be less than the app's suite_count")
                if params.get("mode") not in ("truncated_wellformed", "truncated_malformed"):
                    errors.append(f"{path}.parameters.keep_suites: only meaningful for truncated modes")


def _resolve(source: Any) -> tuple[dict, list[str]]:
    errors: list[str] = []
    data = _merge(asdict(Config()), source, "", errors)
    _number(data["schema_version"], "schema_version", errors, minimum=1, maximum=1, integer=True)
    _number(data["seed"], "seed", errors, integer=True)
    if _string(data["output_dir"], "output_dir", errors) and "\0" in data["output_dir"]:
        errors.append("output_dir: path must not contain a null character")
    _safe_name(data["archive_name"], "archive_name", errors)
    _boolean(data["overwrite"], "overwrite", errors)
    if _string(data["timezone"], "timezone", errors) and data["timezone"] != "local":
        try:
            ZoneInfo(data["timezone"])
        except (ZoneInfoNotFoundError, ValueError):
            errors.append("timezone: expected 'local' or a valid IANA timezone name")
    calendar = data["calendar"]
    start = _date(calendar["start_date"], "calendar.start_date", errors)
    end = _date(calendar["end_date"], "calendar.end_date", errors)
    if start and end and start > end:
        errors.append("calendar.start_date: must not be after end_date")
    days = calendar["days_of_week"]
    if not isinstance(days, list) or not days:
        errors.append("calendar.days_of_week: expected a nonempty list of weekdays")
    else:
        valid_days = [_number(v, f"calendar.days_of_week[{i}]", errors, maximum=6, integer=True) for i, v in enumerate(days)]
        if all(valid_days):
            if len(set(days)) != len(days):
                errors.append("calendar.days_of_week: weekdays must be unique")
            if start and end and start <= end and not any((start + timedelta(days=i)).weekday() in days for i in range(min(7, (end - start).days + 1))):
                errors.append("calendar.days_of_week: calendar produces no candidate nights")
    window = calendar["run_start_window"]
    if not isinstance(window, list) or len(window) != 2:
        errors.append("calendar.run_start_window: expected [HH:MM, HH:MM]")
    else:
        times = []
        for i, value in enumerate(window):
            try:
                if not isinstance(value, str) or re.fullmatch(r"\d{2}:\d{2}", value) is None:
                    raise ValueError
                times.append(time.fromisoformat(value))
            except ValueError:
                errors.append(f"calendar.run_start_window[{i}]: expected a valid HH:MM time")
        if len(times) == 2 and times[0] > times[1]:
            errors.append("calendar.run_start_window: start must not be after end")
    _prob(calendar["outage_night_rate"], "calendar.outage_night_rate", errors)
    _prob(calendar["outage_extend_prob"], "calendar.outage_extend_prob", errors)
    partial = data["partial"]
    _prob(partial["rate"], "partial.rate", errors)
    weights = partial["mode_weights"]
    good_weights = [_prob(v, f"partial.mode_weights.{k}", errors) for k, v in weights.items()]
    if all(good_weights) and not math.isclose(sum(weights.values()), 1.0, abs_tol=1e-9):
        errors.append("partial.mode_weights: weights must sum to 1")
    for name, row in data["personalities"].items():
        for key, value in row.items():
            path = f"personalities.{name}.{key}"
            if key == "flaky_eligible":
                _boolean(value, path, errors)
            elif key == "never_fixed_prob":
                _prob(value, path, errors)
            else:
                _number(value, path, errors, minimum=1e-12 if key == "persistent_duration" else 0)
    weights = data["personality_weights"]
    good_weights = [_prob(v, f"personality_weights.{k}", errors) for k, v in weights.items()]
    if all(good_weights) and not math.isclose(sum(weights.values()), 1.0, abs_tol=1e-9):
        errors.append("personality_weights: weights must sum to 1")
    app_lookup = {}
    if not isinstance(data["apps"], list) or not data["apps"]:
        errors.append("apps: expected a nonempty list")
    else:
        for i, supplied in enumerate(data["apps"]):
            path = f"apps[{i}]"
            app_defaults = asdict(AppConfig())
            if isinstance(supplied, dict) and type(supplied.get("test_count")) is int and supplied["test_count"] >= 10000:
                app_defaults["personality"] = "legacy"
                app_defaults["drift"]["removals"] = 4
            app = _merge(app_defaults, supplied, path, errors)
            data["apps"][i] = app
            if _safe_name(app["name"], f"{path}.name", errors):
                if app["name"] in app_lookup:
                    errors.append(f"{path}.name: duplicate app name {app['name']!r}")
                app_lookup[app["name"]] = app
            tests_ok = _number(app["test_count"], f"{path}.test_count", errors, minimum=1, integer=True)
            suites_ok = _number(app["suite_count"], f"{path}.suite_count", errors, minimum=1, integer=True)
            if tests_ok and suites_ok and app["suite_count"] > app["test_count"]:
                errors.append(f"{path}.suite_count: cannot exceed test_count")
            if app["personality"] not in (*PERSONALITY_NAMES, "random"):
                errors.append(f"{path}.personality: expected stable, typical, noisy, legacy, or random")
            if app["duration_minutes"] is None and tests_ok:
                app["duration_minutes"] = [180, 360] if app["test_count"] >= 10000 else [40, 120] if app["test_count"] >= 2000 else [8, 45]
            if app["duration_minutes"] is not None:
                _range(app["duration_minutes"], f"{path}.duration_minutes", errors)
            if app["partial_rate"] is not None:
                _prob(app["partial_rate"], f"{path}.partial_rate", errors)
            for key in ("removals", "additions"):
                _number(app["drift"][key], f"{path}.drift.{key}", errors, integer=True)
    failure = data["failure_model"]
    for key in ("target_failure_rate", "background_noise_rate", "born_failing_prob"):
        _prob(failure[key], f"failure_model.{key}", errors)
    _range(failure["born_failing_nights"], "failure_model.born_failing_nights", errors, minimum=1, integer=True)
    persistent = failure["persistent"]
    _range(persistent["count"], "failure_model.persistent.count", errors, integer=True)
    _number(persistent["duration_median"], "failure_model.persistent.duration_median", errors, minimum=1e-12)
    _number(persistent["duration_sigma"], "failure_model.persistent.duration_sigma", errors)
    _prob(persistent["never_fixed_prob"], "failure_model.persistent.never_fixed_prob", errors)
    for kind in ("regression", "correlated"):
        for key, value in failure[kind].items():
            _range(value, f"failure_model.{kind}.{key}", errors, minimum=0 if key == "events" else 1, integer=True)
    for key, value in failure["flaky"].items():
        _range(value, f"failure_model.flaky.{key}", errors, maximum=1 if key.startswith("p_") else None, integer=not key.startswith("p_"))
    emit = data["emit"]
    _number(emit["indent"], "emit.indent", errors, maximum=16, integer=True)
    if _string(emit["time_zero_literal"], "emit.time_zero_literal", errors):
        try:
            if float(emit["time_zero_literal"]) != 0:
                raise ValueError
        except ValueError:
            errors.append("emit.time_zero_literal: must represent numeric zero")
    _prob(emit["nonzero_time_fraction"], "emit.nonzero_time_fraction", errors)
    if _string(emit["failure_type_literal"], "emit.failure_type_literal", errors, nonempty=False):
        if any(not (char in "\t\n\r" or 0x20 <= ord(char) <= 0xD7FF or 0xE000 <= ord(char) <= 0xFFFD or 0x10000 <= ord(char) <= 0x10FFFF)
               for char in emit["failure_type_literal"]):
            errors.append("emit.failure_type_literal: contains a character forbidden in XML 1.0")
    for key in ("counts_attrs", "timestamp_attr", "shuffle_per_run"):
        _boolean(emit[key], f"emit.{key}", errors)
    if _string(emit["file_name_template"], "emit.file_name_template", errors):
        try:
            fields = [(name, spec, conversion) for _, name, spec, conversion in Formatter().parse(emit["file_name_template"]) if name is not None]
            if fields != [("app", "", None)]:
                raise ValueError("must contain exactly one plain {app} field without formatting or conversion")
            rendered = emit["file_name_template"].format(app="example")
            if Path(rendered).name != rendered or "/" in rendered or "\\" in rendered or not rendered.endswith(".xml"):
                raise ValueError("must produce a plain .xml filename")
        except (ValueError, KeyError, IndexError, AttributeError) as exc:
            errors.append(f"emit.file_name_template: {exc}")
    for key in ("write_parquet", "include_missing_rows"):
        _boolean(data["truth"][key], f"truth.{key}", errors)
    _validate_events(data, start, end, app_lookup, errors)
    return data, errors


def _construct(data: dict) -> Config:
    data = deepcopy(data)
    data["calendar"] = CalendarConfig(**data["calendar"])
    data["apps"] = [AppConfig(**(app | {"drift": DriftConfig(**app["drift"])})) for app in data["apps"]]
    failure = data["failure_model"]
    for key, cls in (("persistent", PersistentConfig), ("regression", RegressionConfig), ("correlated", CorrelatedConfig), ("flaky", FlakyConfig)):
        failure[key] = cls(**failure[key])
    data["failure_model"] = FailureModelConfig(**failure)
    data["personalities"] = {name: PersonalityConfig(**row) for name, row in data["personalities"].items()}
    data["partial"] = PartialConfig(**data["partial"])
    data["emit"] = EmitConfig(**data["emit"])
    data["truth"] = TruthConfig(**data["truth"])
    return Config(**data)


def load_config(source: str | Path | dict[str, Any]) -> Config:
    """Load a JSON path, JSON document string, or dictionary and fill defaults."""
    if isinstance(source, (str, Path)):
        try:
            if isinstance(source, Path):
                raw = source.read_text(encoding="utf-8")
            elif source.lstrip().startswith(("{", "[")):
                raw = source
            else:
                raw = Path(source).read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:
            raise ConfigError(f"config: cannot read {source!s}: {exc}") from exc
        try:
            source = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"config: invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc
    data, errors = _resolve(source)
    if errors:
        raise ConfigError(errors)
    return _construct(data)


def validate_config(config: Config | dict[str, Any]) -> list[str]:
    """Return every validation error without raising."""
    return _resolve(config_dict(config) if isinstance(config, Config) else config)[1]


def save_config(config: Config, path: str | Path) -> None:
    """Save the complete resolved configuration as readable JSON."""
    destination = Path(path)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(config_dict(config), indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    except OSError as exc:
        raise OutputError(f"Cannot save configuration to {destination}: {exc}") from exc


def config_hash(config: Config) -> str:
    """SHA-256 of canonical resolved configuration JSON."""
    encoded = json.dumps(config_dict(config), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def preset(name: str) -> Config:
    """Load one of the two bundled, reproducible presets."""
    if name not in ("default", "dev"):
        raise ConfigError(f"preset: unknown preset {name!r}; choose default or dev")
    return load_config(files("resultsgen").joinpath("presets", f"{name}.json").read_text(encoding="utf-8"))
