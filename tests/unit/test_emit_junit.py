from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import numpy as np
import polars as pl
import pytest

from resultsgen.emit_junit import emit_junit, testcase_times as draw_times
from resultsgen.emit_truth import Stats, TruthWriter


@pytest.fixture
def sample():
    cfg = {
        "seed": 42,
        "emit": {
            "indent": 4, "time_zero_literal": "0", "nonzero_time_fraction": 0.1,
            "shuffle_per_run": False, "counts_attrs": False, "timestamp_attr": False,
            "failure_type_literal": "", "file_name_template": "{app}.xml",
        },
        "truth": {"write_parquet": True, "include_missing_rows": True},
    }
    tests = [
        SimpleNamespace(id=0, suite_id=0, suite="Tests.Registration", name="testCase1"),
        SimpleNamespace(id=1, suite_id=0, suite="Tests.Registration", name="testCase2"),
        SimpleNamespace(id=2, suite_id=1, suite="Tests.Authentication", name="testCase3"),
    ]
    app = SimpleNamespace(app_index=0, name="example", tests=tests, base_test_count=3)
    run = SimpleNamespace(number=1, date=date(2026, 9, 7), run_start=datetime(2026, 9, 7, 22, tzinfo=timezone.utc))
    outcome = SimpleNamespace(
        test_ids=np.array([0, 1, 2]), outcome=np.array(["pass", "fail", "pass"], dtype="U7"),
        cause=np.array([None, "persistent", None], dtype=object), event_id=np.array([-1, 2**40, -1]),
    )
    failures = {1: SimpleNamespace(message="Assertion error message", body="Expected equality", type="")}
    return cfg, app, run, tests, outcome, failures


def test_reference_formatting_golden(sample):
    text, missing = emit_junit(*sample, durations=["0", "0.125", "0.250"])
    expected = Path(__file__).parents[1] / "golden" / "junit.xml"
    assert text == expected.read_text(encoding="utf-8")
    assert missing == ()


def test_escaping_round_trips_names_and_failure_content(sample):
    cfg, app, run, tests, outcome, failures = sample
    tests[0].name = 'Handles <>& " café'
    tests[0].suite = 'net.<Socket&"Test'
    tests[1].suite = tests[0].suite
    failures[1] = SimpleNamespace(message='actual < expected & "café"', body='A < B\nC & D "é"', type='"<&')
    text, _ = emit_junit(*sample, durations=["0", "0.100", "0"])
    root = ET.fromstring(text)
    cases = root.findall(".//testcase")
    assert cases[0].attrib["name"] == tests[0].name
    assert cases[0].attrib["classname"] == tests[0].suite
    failure = cases[1].find("failure")
    assert failure.attrib["message"] == failures[1].message
    assert failure.attrib["type"] == failures[1].type
    assert [line.strip() for line in failure.text.splitlines() if line.strip()] == failures[1].body.splitlines()
    assert "&lt;" in text and "&amp;" in text and "&quot;" in text and "café" in text


@pytest.mark.parametrize("mode,keep,missing", [
    ("absent", 0, (0, 1, 2)),
    ("empty", 0, (0, 1, 2)),
    ("truncated_wellformed", 1, (2,)),
    ("truncated_malformed", 1, (2,)),
    ("truncated_wellformed", 0, (0, 1, 2)),
    ("truncated_malformed", 0, (0, 1, 2)),
])
def test_partial_modes_document_shape(sample, mode, keep, missing):
    partial = SimpleNamespace(parameters={"mode": mode, "keep_suites": keep}, targets=missing)
    text, actual_missing = emit_junit(*sample, partial=partial, durations=["0", "0.125", "0.250"])
    assert actual_missing == missing
    if mode == "absent":
        assert text is None
    elif mode == "truncated_malformed":
        with pytest.raises(ET.ParseError):
            ET.fromstring(text)
        # Complete preceding suites survive even though the document is invalid.
        assert text.count("</testsuite>") == keep
        assert not text.endswith("</testsuites>\n")
    else:
        root = ET.fromstring(text)
        assert len(root.findall("testsuite")) == keep
        assert {case.attrib["name"] for case in root.findall(".//testcase")} == (
            {"testCase1", "testCase2"} if keep else set()
        )


def test_timing_sums_displayed_milliseconds_and_is_independent_of_membership(sample):
    cfg, app, run, tests, *_ = sample
    cfg["emit"]["nonzero_time_fraction"] = 1.0
    all_times = draw_times(cfg, app, run, tests)
    assert draw_times(cfg, app, run, tests[1:]) == all_times[1:]
    text, _ = emit_junit(*sample)
    root = ET.fromstring(text)
    from decimal import Decimal
    total = Decimal(0)
    for suite in root:
        value = sum((Decimal(case.attrib["time"]) for case in suite), Decimal(0))
        assert Decimal(suite.attrib["time"]) == value
        total += value
    assert Decimal(root.attrib["time"]) == total


def test_optional_attributes_only_when_enabled(sample):
    cfg, *_ = sample
    text, _ = emit_junit(*sample)
    root = ET.fromstring(text)
    assert set(root.attrib) == {"time"}
    assert set(root[0].attrib) == {"name", "time"}
    cfg["emit"].update(counts_attrs=True, timestamp_attr=True)
    text, _ = emit_junit(*sample)
    root = ET.fromstring(text)
    assert root.attrib["tests"] == "3"
    assert root.attrib["failures"] == "1"
    assert root.attrib["timestamp"] == sample[2].run_start.isoformat()


@pytest.mark.parametrize("include_missing", [False, True])
def test_truth_frames_stats_and_nullable_large_event_ids_match(tmp_path, sample, include_missing):
    cfg, app, run, tests, outcome, _ = sample
    cfg["truth"]["include_missing_rows"] = include_missing
    outcome.outcome[2] = "missing"
    outcome.cause[2] = "truncated"
    writer = TruthWriter(cfg)
    writer.add(run, app, tests, outcome)
    path = tmp_path / "truth.parquet"
    writer.write(path)
    rows = pl.read_parquet(path)
    summary = writer.summary()
    assert rows.height == summary["total_test_runs"] == 2 + int(include_missing)
    assert summary["total_failures"] == 1
    assert summary["failure_rate"] == 0.5
    assert summary["total_missing"] == int(include_missing)
    assert summary["missing_by_cause"]["truncated"] == int(include_missing)
    assert rows["event_id"].to_list() == [None, 2**40] + ([None] if include_missing else [])
    assert rows.schema["app"] == pl.Categorical
    assert rows.schema["outcome"] == pl.Enum(["pass", "fail", "missing"])
    stats = Stats(include_missing)
    stats.add(app.name, outcome)
    assert stats.summary() == summary


def test_truth_disabled_still_counts_without_retaining_frames(tmp_path, sample):
    cfg, app, run, tests, outcome, _ = sample
    cfg["truth"]["write_parquet"] = False
    writer = TruthWriter(cfg)
    writer.add(run, app, tests, outcome)
    writer.write(tmp_path / "truth.parquet")
    assert writer.frames == []
    assert not (tmp_path / "truth.parquet").exists()
    assert writer.summary()["total_test_runs"] == 3


def test_malformed_partial_stays_invalid_after_last_suite_removed(sample):
    cfg, app, run, _, outcome, failures = sample
    partial = SimpleNamespace(parameters={"mode": "truncated_malformed", "keep_suites": 0}, targets=())
    text, missing = emit_junit(cfg, app, run, [], outcome, failures, partial)
    assert missing == ()
    with pytest.raises(ET.ParseError):
        ET.fromstring(text)
