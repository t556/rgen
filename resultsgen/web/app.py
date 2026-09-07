"""Flask forms and job pages; all simulation runs through the CLI."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
import re

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

from ..config import AppConfig, ConfigError, config_dict, load_config, preset, save_config
from .jobs import JobBusy, JobManager


def _path(directory: Path, name: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
        raise ConfigError("Config name must be 1–64 letters, digits, underscores, or hyphens, starting with a letter or digit.")
    path = directory / f"{name}.json"
    if path.is_symlink():
        raise ConfigError("A named config must be a regular file, not a symbolic link.")
    return path


def _edited_config(form, baseline: dict) -> dict:
    if form.get("use_raw_json"):
        return config_dict(load_config(form.get("config_json", "")))
    data = config_dict(baseline)

    def assign(target: dict, key: str, field: str, convert=str):
        if field in form:
            value = form[field]
            try:
                value = convert(value)
            except (ValueError, TypeError):
                pass  # The config validator reports every invalid field together.
            target[key] = value

    for key in ("output_dir", "archive_name", "timezone"):
        assign(data, key, key)
    assign(data, "seed", "seed", int)
    data["overwrite"] = "overwrite" in form
    for key in ("start_date", "end_date"):
        assign(data["calendar"], key, key)
    if "run_start_lo" in form:
        data["calendar"]["run_start_window"] = [form["run_start_lo"], form.get("run_start_hi", "")]
    if "days_of_week" in form:
        assign(data["calendar"], "days_of_week", "days_of_week", lambda value: [int(day.strip()) for day in value.split(",") if day.strip()])
    for key in ("outage_night_rate", "outage_extend_prob"):
        assign(data["calendar"], key, key, float)
    for key in ("target_failure_rate", "background_noise_rate"):
        assign(data["failure_model"], key, key, float)
    assign(data["emit"], "nonzero_time_fraction", "nonzero_time_fraction", float)
    assign(data["partial"], "rate", "partial_rate", float)
    for mode in data["partial"]["mode_weights"]:
        assign(data["partial"]["mode_weights"], mode, f"weight_{mode}", float)
    for index, app in enumerate(data["apps"]):
        for key in ("name", "personality"):
            assign(app, key, f"app_{index}_{key}")
        for key in ("test_count", "suite_count"):
            assign(app, key, f"app_{index}_{key}", int)
        for key in ("removals", "additions"):
            assign(app["drift"], key, f"app_{index}_{key}", int)
        assign(app, "partial_rate", f"app_{index}_partial_rate", lambda value: float(value) if value.strip() else None)
        low = form.get(f"app_{index}_duration_lo")
        high = form.get(f"app_{index}_duration_hi")
        if low is not None and high is not None:
            try:
                app["duration_minutes"] = [float(low), float(high)] if low or high else None
            except ValueError:
                app["duration_minutes"] = [low, high]
    errors = []
    for key in ("events", "personalities"):
        if f"{key}_json" in form:
            try:
                data[key] = json.loads(form[f"{key}_json"])
            except json.JSONDecodeError as exc:
                errors.append(f"{key}: invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}")
    if errors:
        raise ConfigError(errors)
    return data


def _change_applications(data: dict, baseline: dict, action: str) -> str:
    """Change draft rows and their valid recovery baseline in the same order."""
    if action == "add_app":
        names = {app["name"] for cfg in (data, baseline) for app in cfg["apps"]}
        number = len(data["apps"]) + 1
        while f"app{number}" in names:
            number += 1
        name = f"app{number}"
        for cfg in (data, baseline):
            cfg["apps"].append(asdict(AppConfig(name=name)))
        return f"Added {name}. Edit its settings below, then save your config."

    try:
        index = int(action.removeprefix("remove_app:"))
    except ValueError:
        raise ConfigError("Choose an application to remove.") from None
    if not 0 <= index < len(data["apps"]):
        raise ConfigError("Choose an existing application to remove.")
    if len(data["apps"]) == 1:
        raise ConfigError("Keep at least one application in the archive.")
    removed_names = {data["apps"][index]["name"], baseline["apps"][index]["name"]}
    removed = data["apps"][index]["name"]
    removed_events = 0
    for cfg in (data, baseline):
        del cfg["apps"][index]
        remaining_names = {app["name"] for app in cfg["apps"]}
        if isinstance(cfg["events"], list):
            previous = len(cfg["events"])
            cfg["events"] = [event for event in cfg["events"]
                             if not isinstance(event, dict)
                             or not isinstance(event.get("app"), str)
                             or event.get("app") not in removed_names - remaining_names]
            if cfg is data:
                removed_events = previous - len(cfg["events"])
    return (f"Removed {removed} and {removed_events} associated event(s) from this draft. "
            "Save your config to keep the change.")


def create_app(configs_dir: str | Path = Path("configs"), jobs_dir: str | Path | None = None, test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=2 * 1024 * 1024)
    if test_config:
        app.config.update(test_config)
    configs = Path(configs_dir).absolute()
    configs.mkdir(parents=True, exist_ok=True)
    manager = JobManager(jobs_dir if jobs_dir is not None else configs.parent / "jobs")
    app.extensions["resultsgen_jobs"] = manager

    def editor(data, name="dev", errors=(), message=None, status=200, baseline=None, form=None):
        names = sorted(path.stem for path in configs.glob("*.json") if not path.is_symlink())
        return render_template("editor.html", cfg=data, base_config=baseline or data, name=name, names=names, errors=errors,
                               message=message, form=request.form if form is None else form), status

    @app.route("/", methods=["GET", "POST"])
    def index():
        data = config_dict(preset("dev"))
        baseline = data
        edited_form = None
        edit_message = None
        name = request.values.get("name", "dev")
        if request.method == "GET":
            try:
                if request.args.get("preset"):
                    name = request.args["preset"]
                    if name not in {"dev", "default"}:
                        raise ConfigError("Choose the dev or default preset.")
                    data = config_dict(preset(name))
                elif request.args.get("name"):
                    data = config_dict(load_config(_path(configs, name)))
                return editor(data, name)
            except (ConfigError, OSError) as exc:
                errors = exc.errors if isinstance(exc, ConfigError) else [str(exc)]
                return editor(data, name, errors=errors, status=400)
        try:
            if "base_config" in request.form:
                data = config_dict(load_config(request.form["base_config"]))
            baseline = config_dict(data)
            data = _edited_config(request.form, data)
            action = request.form.get("action", "validate")
            if action == "add_app" or action.startswith("remove_app:"):
                if request.form.get("use_raw_json"):
                    baseline = config_dict(data)
                edit_message = _change_applications(data, baseline, action)
                edited_form = request.form.to_dict()
                # The submitted JSON still describes the rows before this edit.
                edited_form["events_json"] = json.dumps(data["events"], indent=2)
                edited_form["config_json"] = json.dumps(data, indent=2)
                cfg = load_config(data)
                return editor(config_dict(cfg), name, message=edit_message, form=edited_form)
            cfg = load_config(data)
            if action == "validate":
                return editor(config_dict(cfg), name, message="Configuration is valid.")
            if action not in {"save", "preview", "generate"}:
                raise ConfigError("Choose Save, Validate, Preview, or Generate.")
            path = _path(configs, name)
            save_config(cfg, path)
            if action == "save":
                return editor(config_dict(cfg), name, message=f"Saved {name}.json.")
            job = manager.start(action, cfg, name)
            return redirect(url_for("job_page", identifier=job.id))
        except JobBusy as exc:
            return editor(data, name, errors=[str(exc)], status=409, baseline=baseline)
        except (ConfigError, OSError) as exc:
            errors = exc.errors if isinstance(exc, ConfigError) else [str(exc)]
            return editor(data, name, errors=errors, message=edit_message, status=400,
                          baseline=baseline, form=edited_form)

    @app.get("/jobs/<identifier>")
    def job_page(identifier):
        try:
            state = manager.status(identifier)
        except KeyError:
            abort(404)
        return render_template("job.html", job=state)

    @app.get("/api/jobs/<identifier>")
    def job_status(identifier):
        try:
            return jsonify(manager.status(identifier))
        except KeyError:
            abort(404)

    return app
