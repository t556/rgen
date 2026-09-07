from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from resultsgen.timestamps import RunTimestamps, apply_timestamps, compute_timestamps


def test_files_then_junit_then_run_order_and_exact_times(tmp_path, monkeypatch):
    run_dir = tmp_path / "1"
    junit_dir = run_dir / "junit"
    junit_dir.mkdir(parents=True)
    for filename in ("a.xml", "b.xml"):
        (junit_dir / filename).write_text("<testsuites />", encoding="utf-8")
    times = RunTimestamps(1_700_000_000, 1_700_000_010, {"a.xml": 1_700_010_000, "b.xml": 1_700_020_000}, {})
    import resultsgen.timestamps as module
    real_utime = module.os.utime
    order = []

    def record(path, values):
        order.append(Path(path))
        real_utime(path, values)

    monkeypatch.setattr(module.os, "utime", record)
    apply_timestamps(run_dir, times)
    assert order == [junit_dir / "a.xml", junit_dir / "b.xml", junit_dir, run_dir]
    expected = [(run_dir, times.run_start), (junit_dir, times.junit)] + [(junit_dir / name, value) for name, value in times.files.items()]
    for path, epoch in expected:
        assert path.stat().st_mtime == epoch
        assert path.stat().st_atime == epoch


def test_computation_crosses_midnight_and_truncation_reduces_duration():
    cfg = {
        "seed": 42,
        "emit": {"file_name_template": "{app}.xml", "shuffle_per_run": False},
        "apps": [{"name": "large", "duration_minutes": [180, 180]}],
    }
    tests = [SimpleNamespace(id=i, suite_id=i // 2) for i in range(4)]
    app = SimpleNamespace(name="large", app_index=0, tests=tests)
    run = SimpleNamespace(number=1, date=date(2026, 9, 7), run_start=datetime(2026, 9, 7, 23, tzinfo=timezone.utc))
    normal = compute_timestamps(cfg, run, [app], {})
    partial = SimpleNamespace(parameters={"mode": "truncated_wellformed", "keep_suites": 1}, targets=(2, 3))
    truncated = compute_timestamps(cfg, run, [app], {"large": partial}, {"large": tests})
    assert normal.files["large.xml"] - truncated.files["large.xml"] == 90 * 60
    assert normal.apps["large"]["mtime_iso"].startswith("2026-09-08")
    assert normal.run_start == int(run.run_start.timestamp())
    assert normal.junit == normal.run_start + 10
    assert compute_timestamps(cfg, run, [app], {}) == normal
    absent = SimpleNamespace(parameters={"mode": "absent"})
    missing = compute_timestamps(cfg, run, [app], {"large": absent})
    assert missing.files == {}
    assert missing.apps["large"]["mtime_epoch"] is None
