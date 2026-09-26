"""Attempt times (DESIGN section 10). Local wall-clock time is used only here.

Automatic: a first attempt at a stable offset (seeded from the entry ID) inside a morning
window, then retries at fixed offsets after it. Fixed: a user-supplied list of times.
Times are resolved per local calendar day, so daylight saving changes shift the UTC
instant, never the local time. A time inside a spring-forward gap maps to the instant
just after the gap (zoneinfo's fold=0 rule).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
import re

from .const import (
    AUTO_RETRY_OFFSETS_MINUTES,
    AUTO_WINDOW_LENGTH_MINUTES,
    AUTO_WINDOW_START_MINUTES,
    MAX_FIXED_TIMES,
    SCHEDULE_AUTOMATIC,
    SCHEDULE_FIXED,
)

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


@dataclass(frozen=True, slots=True)
class ScheduleConfig:
    """Schedule as configured in the entry options."""

    mode: str = SCHEDULE_AUTOMATIC
    fixed_times: tuple[time, ...] = ()


def parse_times(text: str | Sequence[str]) -> tuple[time, ...]:
    """Parse "07:15, 10:15" (or a list) into sorted unique times. Raises ValueError."""
    parts = text.replace(";", ",").split(",") if isinstance(text, str) else list(text)
    times = set()
    for raw in parts:
        item = raw.strip()
        if not item:
            continue
        match = _TIME_RE.match(item)
        if not match:
            raise ValueError(f"not a HH:MM time: {item!r}")
        times.add(time(int(match.group(1)), int(match.group(2))))
    if not times:
        raise ValueError("at least one time is required")
    if len(times) > MAX_FIXED_TIMES:
        raise ValueError(f"at most {MAX_FIXED_TIMES} times")
    return tuple(sorted(times))


def format_times(times: Sequence[time]) -> str:
    """Format times as "07:15, 10:15"."""
    return ", ".join(t.strftime("%H:%M") for t in times)


def local_attempt_times(config: ScheduleConfig, seed: int) -> tuple[time, ...]:
    """Return the day's attempt times in local wall-clock time, in order."""
    if config.mode == SCHEDULE_FIXED:
        return config.fixed_times
    first = AUTO_WINDOW_START_MINUTES + seed % AUTO_WINDOW_LENGTH_MINUTES
    minutes = [first, *(first + offset for offset in AUTO_RETRY_OFFSETS_MINUTES)]
    return tuple(time(m // 60 % 24, m % 60) for m in minutes)


def attempts_on(day: date, config: ScheduleConfig, seed: int, tz: tzinfo) -> list[datetime]:
    """Return the UTC instants of the attempts on local calendar day ``day``."""
    return [
        datetime.combine(day, t, tzinfo=tz).astimezone(UTC)
        for t in local_attempt_times(config, seed)
    ]


def next_attempt(now: datetime, config: ScheduleConfig, seed: int, tz: tzinfo) -> datetime:
    """Return the first attempt strictly after ``now`` (UTC)."""
    today = now.astimezone(tz).date()
    for offset in range(3):
        for when in attempts_on(today + timedelta(days=offset), config, seed, tz):
            if when > now:
                return when
    raise ValueError("no attempt time found")  # pragma: no cover - config always has times


def is_final_attempt(when: datetime, config: ScheduleConfig, seed: int, tz: tzinfo) -> bool:
    """Return True if ``when`` is the last attempt of its local day."""
    day_attempts = attempts_on(when.astimezone(tz).date(), config, seed, tz)
    return bool(day_attempts) and when >= day_attempts[-1]
