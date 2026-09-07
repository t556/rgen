"""Compute deterministic wall-clock times, then apply files before directories."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Mapping, Sequence

from .config import config_dict
from .rng import Purpose, generator
from .suite import suite_order

if TYPE_CHECKING:
    from .events import Event
    from .schedule import Run
    from .suite import AppSuite, Test


@dataclass(frozen=True)
class RunTimestamps:
    run_start: int
    junit: int
    files: dict[str, int]
    apps: dict[str, dict]


def compute_timestamps(
    cfg,
    run: Run,
    apps: Sequence[AppSuite],
    partials: Mapping[str, Event | None],
    memberships: Mapping[str, Sequence[Test]] | None = None,
) -> RunTimestamps:
    """Compute times in epoch seconds with independent per-app timing keys."""
    data = config_dict(cfg)
    app_configs = {app["name"]: app for app in data["apps"]}
    start = int(run.run_start.timestamp())
    file_times: dict[str, int] = {}
    records: dict[str, dict] = {}
    for app in apps:
        partial = partials.get(app.name)
        mode = partial.parameters["mode"] if partial is not None else "complete"
        filename = data["emit"]["file_name_template"].format(app=app.name)
        if mode == "absent":
            records[app.name] = {"status": mode, "file": filename, "mtime_epoch": None, "mtime_iso": None}
            continue
        rng = generator(data["seed"], Purpose.TIMING, app.app_index, run.number, 2)
        stagger = float(rng.uniform(0, 15 * 60))
        lo, hi = app_configs[app.name]["duration_minutes"]
        duration = float(rng.uniform(lo, hi)) * 60
        if mode in {"truncated_wellformed", "truncated_malformed"}:
            tests = memberships[app.name] if memberships is not None else app.tests
            sizes: dict[int, int] = {}
            for test in tests:
                sizes[test.suite_id] = sizes.get(test.suite_id, 0) + 1
            ordered_ids = suite_order(data, app, run, [test.id for test in tests])
            counts = [sizes[suite_id] for suite_id in ordered_ids]
            keep = int(partial.parameters["keep_suites"])
            completed = sum(counts[:keep])
            if mode == "truncated_malformed" and keep < len(counts):
                completed += counts[keep] / 2
            duration *= completed / len(tests) if tests else 0
        epoch = int(start + stagger + duration)
        file_times[filename] = epoch
        records[app.name] = {
            "status": mode,
            "file": filename,
            "mtime_epoch": epoch,
            "mtime_iso": datetime.fromtimestamp(epoch, tz=run.run_start.tzinfo).isoformat(),
        }
    return RunTimestamps(start, start + 10, file_times, records)


def apply_timestamps(run_dir: str | Path, times: RunTimestamps) -> None:
    """Apply after all run writes: files, their directory, then the run itself."""
    run_dir = Path(run_dir)
    junit_dir = run_dir / "junit"
    for filename, epoch in times.files.items():
        os.utime(junit_dir / filename, (epoch, epoch))
    os.utime(junit_dir, (times.junit, times.junit))
    os.utime(run_dir, (times.run_start, times.run_start))
