"""Run the exact simulation without retaining XML or truth frames."""
from __future__ import annotations

from dataclasses import dataclass, asdict
import logging
import time

from .config import config_dict
from .emit_truth import Stats
from .events import plan_events, get_partial
from .personality import assign_personalities
from .schedule import build_schedule
from .simulate import create_markov_state, simulate_run
from .suite import build_suites

logger = logging.getLogger(__name__)


@dataclass
class Plan:
    schedule: object
    apps: list
    personalities: list[str]
    events: list


def prepare(cfg) -> Plan:
    schedule = build_schedule(cfg)
    apps, drift = build_suites(cfg, schedule)
    personalities = assign_personalities(cfg)
    events = plan_events(cfg, schedule, apps, personalities, drift)
    for event in events:
        logger.info("Event %s: %s app=%s date=%s targets=%s", event.event_id,
                    event.type, event.app, event.start_date, len(event.targets))
    return Plan(schedule, apps, personalities, events)


def event_dict(event, apps) -> dict:
    value = asdict(event)
    app = next((app for app in apps if app.name == event.app), None)
    value["targets"] = ([{"test_id": int(i), "suite": app.tests[i].suite,
                          "test": app.tests[i].name} for i in event.targets] if app else [])
    value["start_date"] = str(event.start_date)
    value["end_date"] = str(event.end_date) if event.end_date is not None else None
    return value


def describe_plan(cfg, plan) -> dict:
    data = config_dict(cfg)
    return {
        "runs": len(plan.schedule.runs),
        "outage_nights": [asdict(outage) if hasattr(outage, "__dataclass_fields__") else outage
                           for outage in plan.schedule.outages],
        "apps": [{"name": app.name, "personality": personality,
                  "suite_count": data["apps"][app.app_index]["suite_count"],
                  "base_test_count": app.base_test_count}
                 for app, personality in zip(plan.apps, plan.personalities)],
        "events": [event_dict(event, plan.apps) for event in plan.events],
    }


def budget_report(cfg, stats) -> dict:
    target = config_dict(cfg)["failure_model"]["target_failure_rate"]
    rate = stats["failure_rate"]
    deviation = (rate - target) / target if target else (0.0 if rate == 0 else None)
    outside = deviation is None or abs(deviation) > .30
    return {"target_failure_rate": target, "relative_deviation": deviation,
            "within_budget": not outside,
            "warning": "Failure rate is outside the target ±30% band." if outside else None}


def preview(cfg, progress=None) -> dict:
    started = time.monotonic()
    plan = prepare(cfg)
    stats = Stats(config_dict(cfg)["truth"]["include_missing_rows"])
    states = [create_markov_state(app, plan.events) for app in plan.apps]
    total = len(plan.schedule.runs)
    if progress:
        progress(0, total, None, 0.0)
    for done, run in enumerate(plan.schedule.runs, 1):
        for app, personality, state in zip(plan.apps, plan.personalities, states):
            partial = get_partial(app, run, plan.events)
            outcome = simulate_run(cfg, app, run, plan.events, personality, state, partial=partial)
            stats.add(app.name, outcome)
        if done % 10 == 0 or done == total:
            logger.info("Preview runs %s/%s", done, total)
            if progress:
                progress(done, total, run.number, time.monotonic() - started)
    summary = stats.summary()
    budget = budget_report(cfg, summary)
    if budget["warning"]:
        logger.warning(budget["warning"])
    return {**describe_plan(cfg, plan), "statistics": summary, "budget": budget}
