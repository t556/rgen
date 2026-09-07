"""Independent, reproducible random streams keyed by their purpose and identities."""
from enum import IntEnum

import numpy as np


class Purpose(IntEnum):
    SCHEDULE = 1
    SUITE = 2
    SUITE_NAMESPACE = 3
    PERSONALITY = 4
    EVENTS = 5
    DRIFT = 6
    RUN_DRAWS = 7
    TIMING = 8
    FAILURE_TEXT = 9
    PARTIAL = 10
    SHUFFLE = 11


def generator(seed: int, purpose: Purpose, *ids: int) -> np.random.Generator:
    """Construct a fresh stream; consuming another key never changes this one."""
    purpose = Purpose(purpose)
    sequence = np.random.SeedSequence(int(seed), spawn_key=(int(purpose), *(int(i) for i in ids)))
    return np.random.default_rng(sequence)
