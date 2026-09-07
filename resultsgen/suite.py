"""Stable C++-style test identities, global fixture allocation, and dated drift."""
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np

from .config import config_dict
from .events import Event, event_id, event_dates, integer, resolve_window, target_ids
from .rng import Purpose, generator

# Curated vocabularies are also shipped as plain-text word lists for inspection.
MODULES = "net codec sched storage core io math crypto rpc util media cache db text image audio graph render proto memory fs sync query stream transport metrics auth config process platform".split()
QUALIFIERS = "Tcp Udp Async Atomic Buffered Circular Concurrent Dynamic Fixed Generic Indexed Incremental Lazy Local LockFree Pooled RandomAccess Shared Sorted Sparse Static ThreadSafe Transactional Versioned".split()
NOUNS = "Socket Decoder Queue Buffer Parser Writer Reader Cache Table Index Frame Channel Stream Scheduler Allocator Iterator Encoder Counter Timer Mutex Packet Pool Vector Map Set Tree Graph Client Server Session Connection Request Response Token Record Message Block File Directory Archive".split()
VERBS = "Handles Rejects Accepts Preserves Returns Detects Reads Writes Clears Resets Reuses Validates Converts Compares Sorts Merges Splits Encodes Decodes Opens Closes Flushes Cancels Retries Restores".split()
CONDITIONS = "WhenEmpty WhenFull AfterClose OnTimeout WithZeroLength AtBoundary UnderLoad InOrder WithoutData AfterReset WithInvalidInput OnOverflow WithDuplicates DuringShutdown AfterReconnect WhenInterrupted WithUnicode WithNullValue AcrossThreads OnFailure AfterRetry WithoutAllocation WithLargeInput WithSmallInput AtEnd OfSameSize WithMissingKey WhenReadOnly AfterMove WithNegativeValue OnFirstCall DuringRecovery AtCapacity WithNestedValues AfterCancellation".split()


@dataclass(frozen=True)
class Test:
    id: int
    suite_id: int
    suite: str
    name: str


@dataclass
class AppSuite:
    app_index: int
    name: str
    tests: list[Test]
    base_test_count: int

    @property
    def suite_ids(self) -> list[int]:
        return sorted({test.suite_id for test in self.tests})


def _test_names(seed, app_index, suite_id, count, used=()):
    rng = generator(seed, Purpose.SUITE, app_index, 2, suite_id)
    names, seen = [], set(used)
    draws = rng.integers(0, [len(VERBS), len(QUALIFIERS), len(NOUNS), len(CONDITIONS)], size=(count, 4))
    for verb, qualifier, noun, condition in draws:
        base = VERBS[verb] + QUALIFIERS[qualifier] + NOUNS[noun] + CONDITIONS[condition]
        name, suffix = base, 2
        while name in seen:
            name = f"{base}{suffix}"
            suffix += 1
        names.append(name)
        seen.add(name)
    return names


def build_suites(cfg, schedule) -> tuple[list[AppSuite], list[Event]]:
    data = config_dict(cfg)
    fixture_names = [f"{module}.{qualifier}{noun}Test" for module in MODULES
                     for qualifier in QUALIFIERS for noun in NOUNS]
    allocator = generator(data["seed"], Purpose.SUITE_NAMESPACE)
    allocation = allocator.permutation(len(fixture_names))
    apps, drift = [], []
    first = date.fromisoformat(data["calendar"]["start_date"])
    last = date.fromisoformat(data["calendar"]["end_date"])
    days = (last - first).days + 1
    for index, settings in enumerate(data["apps"]):
        # A fixed partition prevents cross-app collisions, regardless of test counts.
        namespace = [fixture_names[int(i)] for i in allocation[index::len(data["apps"])]]

        def fixture(suite_id):
            base = namespace[suite_id % len(namespace)]
            return base if suite_id < len(namespace) else f"{base}{suite_id // len(namespace) + 1}"

        rng = generator(data["seed"], Purpose.SUITE, index, 0)
        count, number = settings["test_count"], settings["suite_count"]
        weights = rng.uniform(.6, 1.4, size=number)
        ideal = (count - number) * weights / weights.sum()
        sizes = np.floor(ideal).astype(int) + 1
        remaining = count - int(sizes.sum())
        sizes[np.argsort(-(ideal - np.floor(ideal)), kind="stable")[:remaining]] += 1
        tests = []
        for suite_id, size in enumerate(sizes):
            suite_name = fixture(suite_id)
            for name in _test_names(data["seed"], index, suite_id, int(size)):
                tests.append(Test(len(tests), suite_id, suite_name, name))
        app = AppSuite(index, settings["name"], tests, count)
        apps.append(app)
        plans = []
        for family, kind, count_key in ((1, "removal", "removals"), (2, "addition", "additions")):
            for ordinal in range(settings["drift"][count_key]):
                rng = generator(data["seed"], Purpose.DRIFT, index, family, ordinal)
                start = first + timedelta(days=int(rng.integers(days)))
                plans.append((start, family, ordinal, kind, {}, "generated"))
        for ordinal, injected in enumerate(data["events"]):
            if injected["type"] in ("removal", "addition") and injected["app"] == app.name:
                start, _ = event_dates(injected)
                plans.append((start, 90, ordinal, injected["type"], injected, "injected"))
        for start, family, ordinal, kind, injected, source in sorted(plans):
            start_run, end_run = resolve_window(schedule, start)
            if start_run > end_run and source != "injected":
                continue
            rng = generator(data["seed"], Purpose.DRIFT, index, family, ordinal)
            params = dict(injected.get("parameters", {}))
            if kind == "addition":
                suite_id = params.get("suite_id")
                if suite_id is None:
                    suite_id = max(app.suite_ids, default=-1) + 1
                suite_name = fixture(suite_id)
                existing = [test.name for test in tests if test.suite_id == suite_id]
                added = params.get("count", injected.get("targets", {}).get("count", integer(rng, [1, 30])))
                names = _test_names(data["seed"], index, suite_id, added, existing)
                targets = []
                for name in names:
                    targets.append(len(tests))
                    tests.append(Test(len(tests), suite_id, suite_name, name))
                params.update(suite_id=suite_id, count=added)
            else:
                active = membership(app, schedule.runs[min(start_run, len(schedule.runs)) - 1], drift)
                # Future additions are already absent through membership; never remove
                # tests before they were introduced.
                if injected.get("targets"):
                    targets = list(target_ids(app, injected["targets"], rng))
                elif len(active):
                    available_suites = sorted({tests[int(i)].suite_id for i in active})
                    selected_suite = int(rng.choice(available_suites))
                    candidates = [int(i) for i in active if tests[int(i)].suite_id == selected_suite]
                    whole = bool(rng.random() < .5)
                    size = len(candidates) if whole else min(len(candidates), integer(rng, [1, 30]))
                    offset = 0 if whole else int(rng.integers(len(candidates) - size + 1))
                    targets = candidates[offset:offset + size]
                    params.update(suite_id=selected_suite, whole_suite=whole)
                else:
                    targets = []
            if targets:
                if start_run > end_run:
                    params["unfired"] = True
                drift.append(Event(event_id(index, family, ordinal), kind, app.name, start, None,
                                   start_run, end_run, tuple(targets), params, source))
    drift.sort(key=lambda event: (event.start_run, event.app, event.event_id))
    return apps, drift


def membership(app: AppSuite, run, events) -> np.ndarray:
    present = np.zeros(len(app.tests), dtype=bool)
    present[:app.base_test_count] = True
    # Membership applies the permanent chronological history, including on limited
    # schedules, rather than relying on an event's failure-window end.
    changes = [event for event in events if event.app == app.name and event.type in ("removal", "addition")]
    for event in sorted(changes, key=lambda event: (event.start_date, event.event_id)):
        if event.start_date <= run.date:
            present[list(event.targets)] = event.type == "addition"
    ids = np.flatnonzero(present)
    if len(app.tests) != app.base_test_count:
        ids = np.array(sorted(ids, key=lambda i: (app.tests[int(i)].suite_id, int(i))), dtype=np.int64)
    return ids


def suite_order(cfg, app: AppSuite, run, test_ids) -> list[int]:
    data = cfg if isinstance(cfg, dict) else config_dict(cfg)
    ordered = sorted({app.tests[int(test_id)].suite_id for test_id in test_ids})
    if data["emit"]["shuffle_per_run"]:
        generator(data["seed"], Purpose.SHUFFLE, app.app_index, run.number, 0).shuffle(ordered)
    return ordered
