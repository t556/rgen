"""Vectorized outcomes with fixed precedence and sequential per-test Markov state."""
from dataclasses import dataclass

import numpy as np

from .config import config_dict
from .rng import Purpose, generator
from .suite import membership


@dataclass
class Outcome:
    test_ids: np.ndarray
    outcome: np.ndarray
    cause: np.ndarray
    event_id: np.ndarray


@dataclass
class MarkovState:
    flaring: np.ndarray
    eligible: np.ndarray
    p_calm_to_flare: np.ndarray
    p_flare_to_calm: np.ndarray
    p_calm: np.ndarray
    p_flare: np.ndarray
    event_ids: np.ndarray
    start_runs: np.ndarray
    end_runs: np.ndarray
    flake_events: tuple = ()


def _assign_flake_parameters(state, event):
    ids = list(event.targets)
    state.eligible[ids] = True
    for key in ("p_calm_to_flare", "p_flare_to_calm", "p_calm", "p_flare"):
        if "per_test" in event.parameters:
            getattr(state, key)[ids] = [event.parameters["per_test"][str(i)][key] for i in ids]
        else:
            getattr(state, key)[ids] = event.parameters[key]
    state.event_ids[ids] = event.event_id
    state.start_runs[ids] = event.start_run
    state.end_runs[ids] = event.end_run


def create_markov_state(app, events) -> MarkovState:
    count = len(app.tests)
    state = MarkovState(np.zeros(count, dtype=bool), np.zeros(count, dtype=bool),
                        *(np.zeros(count) for _ in range(4)),
                        np.full(count, -1, dtype=np.int64),
                        np.zeros(count, dtype=np.int64), np.zeros(count, dtype=np.int64))
    state.flake_events = tuple(sorted(
        (event for event in events if event.type == "flaky" and event.app == app.name),
        key=lambda event: (event.source == "injected", event.start_run, event.event_id)))
    for event in state.flake_events:
        _assign_flake_parameters(state, event)
    return state


def simulate_run(cfg, app, run, events, personality, state: MarkovState,
                 partial=None) -> Outcome:
    data = config_dict(cfg)
    ids = membership(app, run, events)
    count, total = len(ids), len(app.tests)
    outcomes = np.full(count, "pass", dtype="<U7")
    causes = np.full(count, None, dtype=object)
    winners = np.full(count, -1, dtype=np.int64)

    def draws(key):
        return generator(data["seed"], Purpose.RUN_DRAWS, app.app_index, run.number, key).random(total)[ids]

    transition, flaky_draw, noise_draw = draws(0), draws(1), draws(2)
    # A later injected window wins only for its overlapping targets and runs.
    # The standing generated property resumes when that window ends.
    state.eligible[:] = False
    for event in state.flake_events:
        if event.start_run <= run.number <= event.end_run:
            _assign_flake_parameters(state, event)
    active_flakes = state.eligible[ids] & (state.start_runs[ids] <= run.number) & (state.end_runs[ids] >= run.number)
    previous = state.flaring[ids].copy()
    flaring = np.where(previous, transition >= state.p_flare_to_calm[ids],
                       transition < state.p_calm_to_flare[ids])
    # Advance on every executed run while in membership, including when an
    # infrastructure problem or a higher-priority cause hides the flake.
    state.flaring[ids[active_flakes]] = flaring[active_flakes]
    flaky_failure = active_flakes & (flaky_draw < np.where(
        state.flaring[ids], state.p_flare[ids], state.p_calm[ids]))

    if partial is not None:
        missing = np.isin(ids, partial.targets)
        outcomes[missing] = "missing"
        causes[missing] = "partial" if partial.parameters["mode"] in ("absent", "empty") else "truncated"
        winners[missing] = partial.event_id
    for kind in ("persistent", "born_failing", "regression", "correlated"):
        for event in events:
            if event.type != kind or event.app != app.name or not event.start_run <= run.number <= event.end_run:
                continue
            failed = (outcomes == "pass") & np.isin(ids, event.targets)
            outcomes[failed] = "fail"
            causes[failed] = kind
            winners[failed] = event.event_id
    failed = (outcomes == "pass") & flaky_failure
    outcomes[failed] = "fail"
    causes[failed] = "flaky"
    winners[failed] = state.event_ids[ids[failed]]
    noise = data["failure_model"]["background_noise_rate"] * data["personalities"][personality]["noise"]
    failed = (outcomes == "pass") & (noise_draw < noise)
    outcomes[failed] = "fail"
    causes[failed] = "noise"
    return Outcome(ids, outcomes, causes, winners)
