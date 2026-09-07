"""Stage a complete archive, then promote its two sibling trees."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import shutil
import tempfile
import time

from . import __version__
from .config import OutputError, config_dict, config_hash
from .emit_junit import emit_junit
from .emit_truth import TruthWriter
from .events import get_partial
from .failure_text import build_failure_texts
from .preview import prepare, describe_plan, budget_report
from .simulate import create_markov_state, simulate_run
from .timestamps import compute_timestamps, apply_timestamps

logger = logging.getLogger(__name__)


def _promote(staging: Path, root: Path, sidecar: Path) -> None:
    """Each complete tree becomes visible with one rename; roll back pair failures."""
    backups = []
    installed = []
    try:
        for destination in (sidecar, root):
            if destination.exists():
                backup = staging / (destination.name + ".previous")
                destination.rename(backup)
                backups.append((backup, destination))
        for destination in (sidecar, root):
            (staging / destination.name).rename(destination)
            installed.append(destination)
    except BaseException:
        for destination in reversed(installed):
            destination.rename(staging / destination.name)
        for backup, destination in reversed(backups):
            backup.rename(destination)
        raise


def generate(cfg, max_runs: int | None = None, progress=None, log_path: Path | None = None) -> dict:
    data = config_dict(cfg)
    output = Path(data["output_dir"]).expanduser().absolute()
    root = output / data["archive_name"]
    sidecar = output / (data["archive_name"] + ".truth")
    staging = None
    started = time.monotonic()
    try:
        for path in (root, sidecar):
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                raise OutputError(f"Destination is not a regular directory: {path}")
            if path.exists() and not data["overwrite"]:
                raise OutputError(f"Destination already exists: {path}; use --force to replace it")
        output.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".resultsgen-", dir=output))
        staged_root, staged_truth = staging / root.name, staging / sidecar.name
        staged_root.mkdir()
        staged_truth.mkdir()
        plan = prepare(cfg)
        runs = plan.schedule.runs[:max_runs]
        truth = TruthWriter(cfg)
        states = [create_markov_state(app, plan.events) for app in plan.apps]
        failure_texts = [build_failure_texts(cfg, app) for app in plan.apps]
        recorded_runs = []
        if progress:
            progress(0, len(runs), None, 0.0)
        for done, run in enumerate(runs, 1):
            run_dir = staged_root / str(run.number)
            junit_dir = run_dir / "junit"
            junit_dir.mkdir(parents=True)
            partials = {}
            memberships = {}
            for app, personality, state, texts in zip(plan.apps, plan.personalities, states, failure_texts):
                partial = get_partial(app, run, plan.events)
                partials[app.name] = partial
                outcome = simulate_run(cfg, app, run, plan.events, personality, state, partial=partial)
                tests = [app.tests[int(i)] for i in outcome.test_ids]
                memberships[app.name] = tests
                xml, missing = emit_junit(cfg, app, run, tests, outcome, texts, partial)
                if xml is not None:
                    filename = data["emit"]["file_name_template"].format(app=app.name)
                    (junit_dir / filename).write_text(xml, encoding="utf-8", newline="")
                del xml
                truth.add(run, app, tests, outcome)
            times = compute_timestamps(cfg, run, plan.apps, partials, memberships)
            apply_timestamps(run_dir, times)
            recorded_runs.append({"run": run.number, "date": str(run.date),
                                  "run_start": run.run_start.isoformat(),
                                  "run_start_epoch": times.run_start,
                                  "junit_mtime_epoch": times.junit, "apps": times.apps})
            if done % 10 == 0 or done == len(runs):
                logger.info("Generated runs %s/%s", done, len(runs))
                if progress:
                    progress(done, len(runs), run.number, time.monotonic() - started)
        statistics = truth.summary()
        manifest = {"generator_version": __version__,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "seed": data["seed"], "config": data, "config_sha256": config_hash(cfg),
                    **describe_plan(cfg, plan), "runs": len(runs), "schedule": recorded_runs,
                    "statistics": statistics, "budget": budget_report(cfg, statistics)}
        if data["truth"]["write_parquet"]:
            truth.write(staged_truth / "truth.parquet")
        (staged_truth / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str) + "\n", encoding="utf-8")
        (staged_truth / "config.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        if log_path is not None:
            for handler in logging.getLogger().handlers:
                handler.flush()
            shutil.copyfile(log_path, staged_truth / "generate.log")
        else:
            lines = [f"Event {e.event_id}: {e.type} app={e.app} date={e.start_date} targets={len(e.targets)}" for e in plan.events]
            lines.append(f"Generated {len(runs)} runs; {statistics['total_test_runs']} test-runs")
            (staged_truth / "generate.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
        _promote(staging, root, sidecar)
        return {"results_root": str(root), "sidecar": str(sidecar), "runs": len(runs),
                "statistics": statistics, "budget": manifest["budget"]}
    except OSError as exc:
        raise OutputError(f"Cannot write archive: {exc}") from exc
    finally:
        if staging is not None:
            shutil.rmtree(staging)
