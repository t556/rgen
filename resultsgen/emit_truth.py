"""Compact categorical ground truth and statistics from the same selected rows."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

import numpy as np
import polars as pl

from .config import config_dict

if TYPE_CHECKING:
    from .schedule import Run
    from .simulate import Outcome
    from .suite import AppSuite, Test


FAILURE_CAUSES = ("persistent", "born_failing", "regression", "correlated", "flaky", "noise")
MISSING_CAUSES = ("partial", "truncated")
SCHEMA = {
    "run": pl.UInt32,
    "run_date": pl.Date,
    "app": pl.Categorical,
    "suite": pl.Categorical,
    "test": pl.Categorical,
    "outcome": pl.Enum(["pass", "fail", "missing"]),
    "cause": pl.Enum([*FAILURE_CAUSES, *MISSING_CAUSES]),
    "event_id": pl.Int64,
}


class Stats:
    """Lightweight counters usable by preview without retaining truth frames."""

    def __init__(self, include_missing_rows: bool = True):
        self.include_missing_rows = include_missing_rows
        self._apps: dict[str, Counter] = {}

    def add(self, app_name: str, outcome: Outcome) -> None:
        counts = self._apps.setdefault(app_name, Counter())
        outcomes = np.asarray(outcome.outcome)
        causes = np.asarray(outcome.cause, dtype=object)
        selected = outcomes != "missing" if not self.include_missing_rows else np.ones(len(outcomes), dtype=bool)
        counts["total_test_runs"] += int(np.count_nonzero(selected))
        counts["executed_test_runs"] += int(np.count_nonzero(outcomes != "missing"))
        counts["total_failures"] += int(np.count_nonzero(outcomes == "fail"))
        counts["total_missing"] += int(np.count_nonzero((outcomes == "missing") & selected))
        for cause in FAILURE_CAUSES:
            counts[f"fail:{cause}"] += int(np.count_nonzero((outcomes == "fail") & (causes == cause)))
        for cause in MISSING_CAUSES:
            counts[f"missing:{cause}"] += int(np.count_nonzero((outcomes == "missing") & (causes == cause) & selected))

    @staticmethod
    def _summarize(counts: Counter) -> dict:
        executed = counts["executed_test_runs"]
        return {
            "total_test_runs": counts["total_test_runs"],
            "executed_test_runs": executed,
            "total_failures": counts["total_failures"],
            "total_missing": counts["total_missing"],
            "failure_rate": counts["total_failures"] / executed if executed else 0.0,
            "failures_by_cause": {cause: counts[f"fail:{cause}"] for cause in FAILURE_CAUSES},
            "failure_rates_by_cause": {cause: counts[f"fail:{cause}"] / executed if executed else 0.0 for cause in FAILURE_CAUSES},
            "missing_by_cause": {cause: counts[f"missing:{cause}"] for cause in MISSING_CAUSES},
        }

    def summary(self) -> dict:
        total: Counter = Counter()
        for counts in self._apps.values():
            total.update(counts)
        return {
            **self._summarize(total),
            "per_app": {name: self._summarize(counts) for name, counts in self._apps.items()},
        }


class TruthWriter:
    """Accumulate per-app/run frames and concatenate once for parquet output."""

    def __init__(self, cfg):
        options = config_dict(cfg)["truth"]
        self.write_parquet = options["write_parquet"]
        self.include_missing_rows = options["include_missing_rows"]
        self.stats = Stats(self.include_missing_rows)
        self.frames: list[pl.DataFrame] = []

    def add(self, run: Run, app: AppSuite, tests: Sequence[Test], outcome: Outcome) -> None:
        self.stats.add(app.name, outcome)
        if not self.write_parquet:
            return
        positions = [i for i in range(len(tests)) if self.include_missing_rows or outcome.outcome[i] != "missing"]
        size = len(positions)
        if not size:
            return
        self.frames.append(pl.DataFrame({
            "run": pl.Series([run.number] * size, dtype=pl.UInt32),
            "run_date": pl.Series([run.date] * size, dtype=pl.Date),
            "app": pl.Series([app.name] * size, dtype=pl.Categorical),
            "suite": pl.Series([tests[i].suite for i in positions], dtype=pl.Categorical),
            "test": pl.Series([tests[i].name for i in positions], dtype=pl.Categorical),
            "outcome": pl.Series([str(outcome.outcome[i]) for i in positions], dtype=SCHEMA["outcome"]),
            "cause": pl.Series([outcome.cause[i] for i in positions], dtype=SCHEMA["cause"]),
            "event_id": pl.Series([int(outcome.event_id[i]) if outcome.event_id[i] >= 0 else None for i in positions], dtype=pl.Int64),
        }))

    def summary(self) -> dict:
        return self.stats.summary()

    def write(self, path: str | Path) -> None:
        if self.write_parquet:
            frame = pl.concat(self.frames, rechunk=False) if self.frames else pl.DataFrame(schema=SCHEMA)
            frame.write_parquet(path, compression="zstd", statistics=True)


def write_manifest(path: str | Path, manifest: dict) -> None:
    Path(path).write_text(json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
