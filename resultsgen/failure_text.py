"""Stable GoogleTest-shaped diagnostics; XML escaping belongs to the writer."""
from dataclasses import dataclass
import re

from .config import config_dict
from .rng import Purpose, generator


@dataclass(frozen=True)
class FailureText:
    message: str
    body: str
    type: str = ""


def build_failure_texts(cfg, app) -> dict[int, FailureText]:
    data = config_dict(cfg)
    texts = {}
    # Per-test keys also preserve old diagnostics when new tests are appended.
    for test in app.tests:
        rng = generator(data["seed"], Purpose.FAILURE_TEXT, app.app_index, test.id)
        module, fixture = test.suite.split(".", 1)
        snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", fixture).lower()
        path = f"{module}/{snake}.cpp"
        line = int(rng.integers(20, 901))
        expected, actual = int(rng.integers(1, 500)), int(rng.integers(501, 1000))
        choices = [
            f"Expected equality of these values: expected and actual\n  Expected: {expected}, actual: {actual}",
            "Value of: result.ok()\n  Actual: false, expected: true",
            f"Expected: (actual) < (limit), actual: {actual} vs {expected}",
            "Timed out waiting for completion\n  Expected completion within 5000 ms",
            "Death test: operation()\n  Result: failed to die",
        ]
        detail = choices[int(rng.integers(len(choices)))]
        if rng.random() < .02:
            detail = 'Expected: payload < limit & label == "café"\n  Actual: "overflow & retry"'
        location = f"{path}:{line}"
        texts[test.id] = FailureText(f"{location}: {detail.splitlines()[0]}",
                                    f"{location}\n{detail}", data["emit"]["failure_type_literal"])
    return texts
