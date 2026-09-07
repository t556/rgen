"""Independently parse a deterministic sample and compare it with parquet truth."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import polars as pl


def verify(root: Path, sample: int = 20) -> dict:
    sidecar = root.with_name(root.name + ".truth")
    manifest = json.loads((sidecar / "manifest.json").read_text())
    ordered = sorted(manifest["schedule"], key=lambda run: hashlib.sha256(
        f"{manifest['seed']}:{run['run']}".encode()).digest())
    selected = ordered[:sample]
    numbers = [run["run"] for run in selected]
    truth = pl.scan_parquet(sidecar / "truth.parquet").filter(pl.col("run").is_in(numbers)).collect()
    actual = {}
    malformed_count = 0
    for run in selected:
        directory = root / str(run["run"])
        assert directory.stat().st_mtime == run["run_start_epoch"]
        for app, status in run["apps"].items():
            file = directory / "junit" / status["file"]
            if status["status"] == "absent":
                assert not file.exists()
                continue
            assert file.stat().st_mtime == status["mtime_epoch"]
            suites = []
            if status["status"] == "truncated_malformed":
                parser = ET.XMLPullParser(events=("end",))
                try:
                    parser.feed(file.read_text(encoding="utf-8"))
                    for _, element in parser.read_events():
                        if element.tag == "testsuite":
                            suites.append(element)
                    parser.close()
                except ET.ParseError:
                    malformed_count += 1
                else:
                    raise AssertionError(f"Expected malformed XML: {file}")
            else:
                suites = list(ET.parse(file).getroot())
            for suite in suites:
                for case in suite:
                    key = (run["run"], app, suite.attrib["name"], case.attrib["name"])
                    assert key not in actual
                    actual[key] = "fail" if case.find("failure") is not None else "pass"
    expected = {(row["run"], row["app"], row["suite"], row["test"]): row["outcome"]
                for row in truth.filter(pl.col("outcome") != "missing").iter_rows(named=True)}
    assert actual == expected, "Parsed outcomes differ from truth"
    return {"sampled_runs": len(selected), "test_rows": truth.height,
            "parsed_testcases": len(actual), "malformed_files": malformed_count}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results_root", type=Path)
    parser.add_argument("--sample", type=int, default=20)
    args = parser.parse_args()
    if args.sample < 1:
        parser.error("--sample must be positive")
    return verify(args.results_root, args.sample)


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.info(json.dumps(main(), indent=2))
