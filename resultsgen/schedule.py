"""Build an outage-aware calendar without consuming run numbers for lost nights."""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .config import ConfigError, config_dict
from .rng import Purpose, generator


@dataclass(frozen=True)
class Run:
    number: int
    date: date
    run_start: datetime


@dataclass(frozen=True)
class Outage:
    date: date
    cause: str


@dataclass
class Schedule:
    runs: list[Run]
    outages: list[Outage]


def event_dates(event: dict) -> tuple[date, date]:
    start = date.fromisoformat(event.get("date", event.get("start_date")))
    end = date.fromisoformat(event.get("end_date", start.isoformat()))
    return start, end


def build_schedule(cfg) -> Schedule:
    data = config_dict(cfg)
    calendar = data["calendar"]
    first, last = (date.fromisoformat(calendar[key]) for key in ("start_date", "end_date"))
    candidates = [first + timedelta(days=i) for i in range((last - first).days + 1)
                  if (first + timedelta(days=i)).weekday() in calendar["days_of_week"]]
    injected = [event_dates(event) for event in data["events"] if event["type"] == "outage"]
    rng = generator(data["seed"], Purpose.SCHEDULE, 0)
    # Distinct arrays ensure changing extensions does not change baseline outages.
    starts, extensions = rng.random((2, len(candidates)))
    surviving, outages = [], []
    prior_outage = False
    for i, night in enumerate(candidates):
        forced = any(start <= night <= end for start, end in injected)
        random_outage = starts[i] < calendar["outage_night_rate"]
        extends = prior_outage and extensions[i] < calendar["outage_extend_prob"]
        if forced or random_outage or extends:
            outages.append(Outage(night, "injected" if forced else "random"))
            prior_outage = True
        else:
            surviving.append(night)
            prior_outage = False
    low, high = [time.fromisoformat(value) for value in calendar["run_start_window"]]
    seconds = lambda value: value.hour * 3600 + value.minute * 60 + value.second
    start_second, stop_second = seconds(low), seconds(high)
    zone = None if data["timezone"] == "local" else ZoneInfo(data["timezone"])
    runs = []
    for number, night in enumerate(surviving, start=1):
        clock = generator(data["seed"], Purpose.SCHEDULE, 1, night.toordinal())
        second = int(clock.integers(start_second, stop_second + 1))
        wall = datetime.combine(night, time()) + timedelta(seconds=second)
        stamp = wall.astimezone() if zone is None else wall.replace(tzinfo=zone)
        runs.append(Run(number, night, stamp))
    if not runs:
        raise ConfigError("calendar: the configured weekdays and outages produce zero executed runs")
    return Schedule(runs, outages)
