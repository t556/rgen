"""Pure JUnit string emission, including deliberate infrastructure damage."""

from __future__ import annotations

from collections import OrderedDict
from typing import TYPE_CHECKING, Mapping, Sequence
from xml.sax.saxutils import escape, quoteattr

import numpy as np

from .config import config_dict
from .rng import Purpose, generator
from .suite import suite_order

if TYPE_CHECKING:
    from .events import Event
    from .failure_text import FailureText
    from .schedule import Run
    from .simulate import Outcome
    from .suite import AppSuite, Test


def missing_test_ids(tests: Sequence[Test], partial: Event | None) -> tuple[int, ...]:
    """Return missing identities in membership order, shared with truth checks."""
    if partial is None:
        return ()
    missing = set(partial.targets)
    return tuple(test.id for test in tests if test.id in missing)


def testcase_times(cfg, app: AppSuite, run: Run, tests: Sequence[Test]) -> list[str]:
    """Draw durations independently of file timestamps and emitted membership.

    Round once to integer milliseconds; suite/root totals then sum these exact
    displayed values rather than the unrounded random variates.
    """
    data = config_dict(cfg)
    emit = data["emit"]
    rng = generator(data["seed"], Purpose.TIMING, app.app_index, run.number, 0)
    count = len(app.tests)
    nonzero = rng.random(count) < emit["nonzero_time_fraction"]
    millis = np.rint(rng.lognormal(mean=-2.0, sigma=1.2, size=count) * 1000).astype(np.int64)
    return [
        f"{int(millis[test.id]) / 1000:.3f}" if nonzero[test.id] else emit["time_zero_literal"]
        for test in tests
    ]


def _attribute(value: object) -> str:
    # Always use double quotes, even when the value itself contains quotes.
    return quoteattr(str(value), {'"': "&quot;"})


def emit_junit(
    cfg,
    app: AppSuite,
    run: Run,
    tests: Sequence[Test],
    outcome: Outcome,
    failure_texts: Mapping[int, FailureText],
    partial: Event | None = None,
    *,
    durations: Sequence[str | float] | None = None,
) -> tuple[str | None, tuple[int, ...]]:
    """Render one app/run and return text plus its missing test identities.

    For malformed truncation, every test in the cut suite is missing in truth,
    even when a complete testcase happened to reach disk before the cut.
    """
    data = config_dict(cfg)
    options = data["emit"]
    mode = partial.parameters["mode"] if partial is not None else None
    missing = missing_test_ids(tests, partial)
    if mode == "absent":
        return None, missing

    times = testcase_times(cfg, app, run, tests) if durations is None else [
        value if isinstance(value, str) else f"{value:.3f}" for value in durations
    ]
    groups: OrderedDict[int, list[int]] = OrderedDict()
    for position, test in enumerate(tests):
        groups.setdefault(test.suite_id, []).append(position)
    ordered_ids = suite_order(data, app, run, [test.id for test in tests])
    suites = [groups[suite_id] for suite_id in ordered_ids]
    if options["shuffle_per_run"]:
        for position, suite_id in enumerate(ordered_ids):
            rng = generator(data["seed"], Purpose.SHUFFLE, app.app_index, run.number, 1, suite_id)
            group = suites[position]
            suites[position] = [group[int(i)] for i in rng.permutation(len(group))]

    malformed = mode == "truncated_malformed" and bool(suites)
    if mode == "empty":
        suites = []
    elif mode in {"truncated_wellformed", "truncated_malformed"}:
        keep = int(partial.parameters["keep_suites"])
        suites = suites[: keep + int(malformed)]

    milliseconds = [int(round(float(value) * 1000)) for value in times]
    suite_times = [sum(milliseconds[i] for i in group) for group in suites]
    indent = " " * options["indent"]

    def optional_attributes(positions: Sequence[int]) -> str:
        result = ""
        if options["counts_attrs"]:
            failed = sum(outcome.outcome[i] == "fail" for i in positions)
            result += f' tests="{len(positions)}" failures="{failed}"'
        if options["timestamp_attr"]:
            result += f" timestamp={_attribute(run.run_start.isoformat())}"
        return result

    positions = [i for group in suites for i in group]
    lines = ['<?xml version="1.0" encoding="UTF-8"?>']
    lines.append(f'<testsuites time="{sum(suite_times) / 1000:.3f}"{optional_attributes(positions)}>')
    if mode == "truncated_malformed" and not suites:
        # Drift can remove the final suite; a crashed writer is still malformed.
        return "\n".join(lines) + "\n", missing

    def append_test(position: int) -> None:
        test = tests[position]
        attrs = f"name={_attribute(test.name)} classname={_attribute(test.suite)} time={_attribute(times[position])}"
        if outcome.outcome[position] == "fail":
            failure = failure_texts[test.id]
            lines.append(f"{indent * 2}<testcase {attrs}>")
            lines.append(f"{indent * 3}<failure message={_attribute(failure.message)} type={_attribute(failure.type)}>")
            lines.extend(f"{indent * 4}{escape(line)}" for line in failure.body.splitlines())
            lines.append(f"{indent * 3}</failure>")
            lines.append(f"{indent * 2}</testcase>")
        else:
            lines.append(f"{indent * 2}<testcase {attrs} />")

    for suite_position, (group, duration) in enumerate(zip(suites, suite_times)):
        name = tests[group[0]].suite
        lines.append(f'{indent}<testsuite name={_attribute(name)} time="{duration / 1000:.3f}"{optional_attributes(group)}>')
        if malformed and suite_position == len(suites) - 1:
            # A prefix of complete testcases followed by an interrupted write.
            for position in group[: len(group) // 2]:
                append_test(position)
            cut_test = tests[group[len(group) // 2]]
            lines.append(f"{indent * 2}<testcase name={_attribute(cut_test.name)} classname=")
            return "\n".join(lines), missing
        for position in group:
            append_test(position)
        lines.append(f"{indent}</testsuite>")

    lines.append("</testsuites>")
    return "\n".join(lines) + "\n", missing
