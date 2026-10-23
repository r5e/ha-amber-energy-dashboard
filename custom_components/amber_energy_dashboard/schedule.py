"""Attempt times (DESIGN section 10). Local wall-clock time is used only here.

Automatic: a first attempt at a stable offset (seeded from the entry ID) inside a morning
window, then retries at fixed offsets after it. Fixed: a user-supplied list of times.
Evening retries (2.2.0-dev2): while the day is still waiting for Amber's data after the
last daytime attempt, every 3 hours after it until 22:00 (for example 16:22 and 19:22
after 07:22, 10:22 and 13:22). The manager asks for them only while it is waiting.
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

EVENING_RETRY_MINUTES = 180
EVENING_RETRY_END_MINUTES = 22 * 60
"""Evening retries every 3 hours after the last daytime attempt, the last by 22:00."""

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


def local_attempt_times(
    config: ScheduleConfig, seed: int, *, evening: bool = False
) -> tuple[time, ...]:
    """Return the day's attempt times in local wall-clock time, in order; with
    ``evening``, followed by the evening retries."""
    if config.mode == SCHEDULE_FIXED:
        minutes = [t.hour * 60 + t.minute for t in config.fixed_times]
    else:
        first = AUTO_WINDOW_START_MINUTES + seed % AUTO_WINDOW_LENGTH_MINUTES
        minutes = [first, *(first + offset for offset in AUTO_RETRY_OFFSETS_MINUTES)]
    if evening:
        while minutes[-1] + EVENING_RETRY_MINUTES <= EVENING_RETRY_END_MINUTES:
            minutes.append(minutes[-1] + EVENING_RETRY_MINUTES)
    return tuple(time(m // 60 % 24, m % 60) for m in minutes)


def attempts_on(
    day: date, config: ScheduleConfig, seed: int, tz: tzinfo, *, evening: bool = False
) -> list[datetime]:
    """Return the UTC instants of the attempts on local calendar day ``day``."""
    return [
        datetime.combine(day, t, tzinfo=tz).astimezone(UTC)
        for t in local_attempt_times(config, seed, evening=evening)
    ]


def next_attempt(
    now: datetime, config: ScheduleConfig, seed: int, tz: tzinfo, *, evening: bool = False
) -> datetime:
    """Return the first attempt strictly after ``now`` (UTC); evening retries count only
    on today's date."""
    today = now.astimezone(tz).date()
    for offset in range(3):
        for when in attempts_on(
            today + timedelta(days=offset), config, seed, tz, evening=evening and not offset
        ):
            if when > now:
                return when
    raise ValueError("no attempt time found")  # pragma: no cover - config always has times


def is_final_attempt(when: datetime, config: ScheduleConfig, seed: int, tz: tzinfo) -> bool:
    """Return True if ``when`` is the last daytime attempt of its local day (before any
    evening retry), or the last evening retry."""
    day = when.astimezone(tz).date()
    daytime = attempts_on(day, config, seed, tz)
    all_ = attempts_on(day, config, seed, tz, evening=True)
    if when >= all_[-1]:
        return True
    return when >= daytime[-1] and when < all_[len(daytime)]


def evening_retry_after(when: datetime, config: ScheduleConfig, seed: int, tz: tzinfo) -> bool:
    """True if an evening retry follows ``when`` on its local day."""
    day = when.astimezone(tz).date()
    return any(t > when for t in attempts_on(day, config, seed, tz, evening=True))
