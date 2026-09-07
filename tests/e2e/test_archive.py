"""The archive must agree with its sidecar, including intentional damage."""
from __future__ import annotations

import importlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import polars as pl
import pytest

from resultsgen.config import OutputError, config_dict, load_config, preset
from resultsgen.generate import generate
from resultsgen.preview import preview


def dev_config(tmp_path, **changes):
    data = config_dict(preset("dev"))
    data.update(output_dir=str(tmp_path), overwrite=True, **changes)
    return load_config(data)


def completed_suites(path: Path, malformed: bool):
    if not malformed:
        return list(ET.parse(path).getroot())
    parser = ET.XMLPullParser(events=("end",))
    completed = []
    with pytest.raises(ET.ParseError):
        parser.feed(path.read_text(encoding="utf-8"))
        for _, element in parser.read_events():
            if element.tag == "testsuite":
                completed.append(element)
        parser.close()
    return completed


def assert_parseback(root: Path, manifest: dict, frame: pl.DataFrame):
    parsed = {}
    statuses = {}
    for run in manifest["schedule"]:
        for app, status in run["apps"].items():
            statuses[(run["run"], app)] = status["status"]
            path = root / str(run["run"]) / "junit" / status["file"]
            if status["status"] == "absent":
                assert not path.exists()
                continue
            suites = completed_suites(path, status["status"] == "truncated_malformed")
            for suite in suites:
                assert set(suite.attrib) == {"name", "time"}
                for case in suite:
                    assert case.tag == "testcase"
                    assert case.attrib["classname"] == suite.attrib["name"]
                    assert set(case.attrib) == {"name", "classname", "time"}
                    assert all(child.tag == "failure" for child in case)
                    key = (run["run"], app, suite.attrib["name"], case.attrib["name"])
                    assert key not in parsed
                    parsed[key] = "fail" if case.find("failure") is not None else "pass"
    expected = {}
    for row in frame.iter_rows(named=True):
        key = (row["run"], row["app"], row["suite"], row["test"])
        if row["outcome"] == "missing":
            assert key not in parsed
            assert statuses[key[:2]] in {"absent", "empty", "truncated_wellformed", "truncated_malformed"}
            assert row["cause"] in {"partial", "truncated"}
        else:
            expected[key] = row["outcome"]
            assert (row["cause"] is None) == (row["outcome"] == "pass")
    assert parsed == expected
    stats = manifest["statistics"]
    assert stats["total_test_runs"] == frame.height
    assert stats["total_failures"] == frame.filter(pl.col("outcome") == "fail").height
    assert stats["total_missing"] == frame.filter(pl.col("outcome") == "missing").height
    ids = {event["event_id"] for event in manifest["events"]}
    assert set(frame["event_id"].drop_nulls().to_list()) <= ids


def test_parseback_shape_and_preview_agree(tmp_path):
    cfg = dev_config(tmp_path)
    report = generate(cfg)
    root, sidecar = Path(report["results_root"]), Path(report["sidecar"])
    manifest = json.loads((sidecar / "manifest.json").read_text())
    frame = pl.read_parquet(sidecar / "truth.parquet")
    assert_parseback(root, manifest, frame)
    assert sorted(int(path.name) for path in root.iterdir()) == list(range(1, report["runs"] + 1))
    crosses_midnight = False
    for run in manifest["schedule"]:
        directory = root / str(run["run"])
        assert directory.stat().st_mtime == run["run_start_epoch"]
        assert (directory / "junit").stat().st_mtime == run["junit_mtime_epoch"]
        assert [path.name for path in directory.iterdir()] == ["junit"]
        for status in run["apps"].values():
            if status["status"] != "absent":
                assert (directory / "junit" / status["file"]).stat().st_mtime == status["mtime_epoch"]
                crosses_midnight |= status["mtime_iso"][:10] > run["date"]
    assert crosses_midnight
    assert manifest["outage_nights"]
    assert {e["parameters"]["mode"] for e in manifest["events"] if e["type"] == "partial"} == {
        "absent", "empty", "truncated_wellformed", "truncated_malformed"}
    assert preview(cfg)["statistics"] == report["statistics"]


def test_same_config_is_byte_and_mtime_identical(tmp_path):
    cfg = dev_config(tmp_path)
    first = generate(cfg)
    root, sidecar = Path(first["results_root"]), Path(first["sidecar"])
    before = {str(path.relative_to(root)): (path.read_bytes() if path.is_file() else None, path.stat().st_mtime)
              for path in root.rglob("*")}
    original = pl.read_parquet(sidecar / "truth.parquet")
    manifest = json.loads((sidecar / "manifest.json").read_text())
    generate(cfg)
    after = {str(path.relative_to(root)): (path.read_bytes() if path.is_file() else None, path.stat().st_mtime)
             for path in root.rglob("*")}
    assert before == after
    assert original.equals(pl.read_parquet(sidecar / "truth.parquet"))
    regenerated = json.loads((sidecar / "manifest.json").read_text())
    manifest.pop("generated_at")
    regenerated.pop("generated_at")
    assert manifest == regenerated
    assert not list(tmp_path.glob(".resultsgen-*"))


def test_additions_born_failures_and_shuffled_partials_parse_back(tmp_path):
    data = config_dict(dev_config(tmp_path))
    data["emit"]["shuffle_per_run"] = True
    data["events"] += [
        {"type": "addition", "date": "2026-08-28", "app": "atlas",
         "parameters": {"suite_id": 0, "count": 3, "born_failing": True, "duration": 2}},
        {"type": "addition", "date": "2026-09-02", "app": "beacon",
         "parameters": {"count": 4, "born_failing": True, "duration": 2}},
    ]
    cfg = load_config(data)
    report = generate(cfg)
    root, sidecar = Path(report["results_root"]), Path(report["sidecar"])
    manifest = json.loads((sidecar / "manifest.json").read_text())
    frame = pl.read_parquet(sidecar / "truth.parquet")
    assert_parseback(root, manifest, frame)
    assert frame.filter(pl.col("cause") == "born_failing").height > 0
    additions = [event for event in manifest["events"] if event["type"] == "addition"]
    assert len(additions) == 2
    for event in additions:
        for target in event["targets"]:
            rows = frame.filter((pl.col("app") == event["app"]) & (pl.col("suite") == target["suite"]) & (pl.col("test") == target["test"]))
            assert rows["run"].min() == event["start_run"]
    assert preview(cfg)["statistics"] == report["statistics"]


def test_failure_cleans_staging_and_preserves_previous_archive(tmp_path, monkeypatch):
    cfg = dev_config(tmp_path)
    first = generate(cfg, max_runs=1)
    original = (Path(first["sidecar"]) / "manifest.json").read_bytes()
    module = importlib.import_module("resultsgen.generate")

    def disk_full(*args, **kwargs):
        raise OSError("simulated disk full")

    monkeypatch.setattr(module, "emit_junit", disk_full)
    with pytest.raises(OutputError, match="disk full"):
        generate(cfg)
    assert (Path(first["sidecar"]) / "manifest.json").read_bytes() == original
    assert not list(tmp_path.glob(".resultsgen-*"))
    refused = load_config({**config_dict(cfg), "overwrite": False})
    with pytest.raises(OutputError, match="already exists"):
        generate(refused)


def test_promotion_failure_rolls_back_both_trees(tmp_path, monkeypatch):
    cfg = dev_config(tmp_path)
    report = generate(cfg, max_runs=1)
    sidecar = Path(report["sidecar"])
    original = (sidecar / "manifest.json").read_bytes()
    rename = Path.rename
    root_name = Path(report["results_root"]).name

    def fail_root_promotion(path, target):
        if path.name == root_name and path.parent.name.startswith(".resultsgen-"):
            raise OSError("simulated promotion failure")
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_root_promotion)
    with pytest.raises(OutputError, match="promotion failure"):
        generate(cfg, max_runs=2)
    assert (sidecar / "manifest.json").read_bytes() == original
    assert [p.name for p in Path(report["results_root"]).iterdir()] == ["1"]
    assert not list(tmp_path.glob(".resultsgen-*"))
