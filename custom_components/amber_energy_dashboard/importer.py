"""Write NEM days of Amber usage into external statistics (DESIGN section 7).

This module holds the per-day path shared by the catch-up walk and the import_day
service: Guard 3 (completeness), grouping, baseline, write and read-back verification,
plus the append-only planner that import_day applies after Guard 1 (manager.py). Callers
hold the entry's lock.

Time handling: a NEM day D runs 00:00 to 24:00 UTC+10, which is always exactly 24 UTC
hours (D-1 14:00Z to D 14:00Z). Records are bucketed by the UTC hour of their floored
``startTime``. Local wall-clock time is never used.
"""

import asyncio
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
import logging
import math
from typing import Any, Final, Literal

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .api import AmberClient, UsageRecord
from .const import NEM_TZ
from .statistics import ChannelConfig, Metric, StatisticSpec

_LOGGER = logging.getLogger(__name__)

HOUR: Final = timedelta(hours=1)
DAY_HOURS: Final = 24
ROUND_DIGITS: Final = 6
"""Values are summed at full precision and rounded only when written."""
VERIFY_TIMEOUT: Final = 60.0
VERIFY_INTERVAL: Final = 0.5
_TOLERANCE: Final = 1e-9

ImportMode = Literal["first", "append", "reimport"]
"""Modes chosen by async_plan_day for the import_day service."""


class ImportDayError(HomeAssistantError):
    """An import stopped. Nothing is written unless the reason says otherwise."""

    def __init__(self, reason: str, message: str) -> None:
        """Store a machine-readable reason with the message."""
        super().__init__(message)
        self.reason = reason


class ImportRefusedError(ImportDayError, ServiceValidationError):
    """The requested day may not be imported now (append-only rule). Nothing written.

    Also a ServiceValidationError: the request itself is not allowed, so HA reports it
    to the caller as a validation error rather than an internal failure.
    """


class IncompleteDataError(ImportDayError):
    """Guard 3: the fetched data is not one complete, clean NEM day. Nothing written."""


class VerificationFailedError(ImportDayError):
    """The write was issued but the read-back did not match."""


# --- time helpers --------------------------------------------------------------------


def nem_day_start(day: date) -> datetime:
    """Return the UTC start of NEM day ``day`` (its first hourly bucket)."""
    return datetime.combine(day, time(0), NEM_TZ).astimezone(UTC)


def nem_date(moment: datetime) -> date:
    """Return the NEM date that contains ``moment``."""
    return moment.astimezone(NEM_TZ).date()


def floor_minute(moment: datetime) -> datetime:
    """Floor to the minute in UTC (drops Amber's one-second startTime offset)."""
    return moment.astimezone(UTC).replace(second=0, microsecond=0)


def floor_hour(moment: datetime) -> datetime:
    """Floor to the hour in UTC."""
    return moment.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


# --- Guard 3 ---------------------------------------------------------------------------


def check_completeness(
    records: Sequence[UsageRecord], day: date, channels: Iterable[ChannelConfig]
) -> dict[str, list[UsageRecord]]:
    """Return records per channel identifier, or raise IncompleteDataError.

    Per channel: every record has ``date == day``; one uniform ``duration`` that divides
    a day; exactly ``1440 / duration`` records; no duplicate interval; every interval on
    the ``duration`` grid inside the day. Every configured channel must be present and
    no other channel may appear.
    """
    configured = {c.identifier: c for c in channels}
    if not records:
        raise IncompleteDataError(
            "no_data",
            f"Amber returned no usage for {day} (not published yet, or outside retention)",
        )
    by_channel: dict[str, list[UsageRecord]] = defaultdict(list)
    for record in records:
        by_channel[record.channel_identifier].append(record)

    unexpected = sorted(set(by_channel) - set(configured))
    if unexpected:
        raise IncompleteDataError(
            "unexpected_channel",
            f"{day}: usage contains channels not in this configuration: {unexpected}",
        )
    missing = sorted(set(configured) - set(by_channel))
    if missing:
        raise IncompleteDataError("missing_channel", f"{day}: no usage for channels {missing}")

    day_start = nem_day_start(day)
    for ident, recs in sorted(by_channel.items()):
        _check_channel(recs, day, day_start, configured[ident])
    return dict(by_channel)


def _check_channel(
    recs: Sequence[UsageRecord], day: date, day_start: datetime, channel: ChannelConfig
) -> None:
    """Guard 3 checks for one channel's records."""
    ident = channel.identifier
    where = f"{day} channel {ident}"
    if types := {r.channel_type for r in recs} - {channel.type}:
        raise IncompleteDataError(
            "channel_type_mismatch",
            f"{where}: channel type {sorted(types)} does not match configured {channel.type!r}",
        )
    if wrong := sorted({str(r.date) for r in recs if r.date != day}):
        raise IncompleteDataError("wrong_date", f"{where}: records dated {wrong}")
    durations = {r.duration for r in recs}
    if len(durations) != 1:
        raise IncompleteDataError("mixed_duration", f"{where}: mixed durations {sorted(durations)}")
    duration = durations.pop()
    if duration <= 0 or 1440 % duration:
        raise IncompleteDataError("invalid_duration", f"{where}: duration {duration}")
    starts = Counter(floor_minute(r.start_time) for r in recs)
    if dupes := sorted(s for s, n in starts.items() if n > 1):
        raise IncompleteDataError(
            "duplicate_interval",
            f"{where}: {len(dupes)} duplicate intervals, first at {dupes[0].isoformat()}",
        )
    expected = 1440 // duration
    if len(recs) != expected:
        raise IncompleteDataError(
            "incomplete_day",
            f"{where}: {len(recs)} records, expected {expected} for {duration}-minute intervals",
        )
    for start in starts:
        offset = (start - day_start).total_seconds() / 60
        if not 0 <= offset < 1440 or offset % duration:
            raise IncompleteDataError(
                "interval_outside_day",
                f"{where}: interval starting {start.isoformat()} is not on the "
                f"{duration}-minute grid of the day",
            )


# --- grouping --------------------------------------------------------------------------


def hourly_amounts(
    by_channel: Mapping[str, Sequence[UsageRecord]],
    specs: Sequence[StatisticSpec],
    day: date,
) -> dict[str, list[float]]:
    """Return 24 hourly amounts per statistic ID, at full precision, in final units.

    Energy in kWh. Cost and net cost in AUD (Amber cents / 100, sign kept, so paid is
    positive). Compensation in AUD with the sign flipped, so earned is positive.
    """
    day_start = nem_day_start(day)
    kwh: dict[str, list[list[float]]] = {}
    cents: dict[str, list[list[float]]] = {}
    for ident, recs in by_channel.items():
        kwh[ident] = [[] for _ in range(DAY_HOURS)]
        cents[ident] = [[] for _ in range(DAY_HOURS)]
        for r in recs:
            index = int((floor_hour(r.start_time) - day_start) / HOUR)
            kwh[ident][index].append(r.kwh)
            cents[ident][index].append(r.cost)

    amounts: dict[str, list[float]] = {}
    for spec in specs:
        if spec.metric is Metric.ENERGY:
            values = [math.fsum(v) for v in kwh[spec.channel]]
        elif spec.metric is Metric.COST:
            values = [math.fsum(v) / 100 for v in cents[spec.channel]]
        elif spec.metric is Metric.COMPENSATION:
            values = [-math.fsum(v) / 100 for v in cents[spec.channel]]
        else:  # NET_COST: all channels, Amber's signed convention
            values = [
                math.fsum(c for ident in cents for c in cents[ident][h]) / 100
                for h in range(DAY_HOURS)
            ]
        amounts[spec.statistic_id] = values
    return amounts


def build_rows(day: date, amounts: Sequence[float], baseline: float) -> list[dict[str, Any]]:
    """Return 24 statistic rows: state is the hour's amount, sum is cumulative."""
    day_start = nem_day_start(day)
    rows = []
    running = [baseline]
    for hour, amount in enumerate(amounts):
        running.append(amount)
        rows.append(
            {
                "start": day_start + hour * HOUR,
                "state": round(amount, ROUND_DIGITS),
                "sum": round(math.fsum(running), ROUND_DIGITS),
            }
        )
    return rows


# --- the import ------------------------------------------------------------------------


@dataclass(slots=True)
class ImportContext:
    """What the importer needs from a config entry."""

    client: AmberClient
    site_id: str
    channels: tuple[ChannelConfig, ...]
    specs: tuple[StatisticSpec, ...]
    lock: asyncio.Lock


async def async_write_day(
    hass: HomeAssistant,
    ctx: ImportContext,
    day: date,
    records: Sequence[UsageRecord],
    baselines: Mapping[str, float],
    *,
    mode: str,
) -> dict[str, Any]:
    """Guard 3, group, write and verify one NEM day. Returns a summary.

    The caller holds ``ctx.lock`` and has already decided that ``day`` may be written
    and what each statistic's baseline is. Raises IncompleteDataError (nothing written)
    or VerificationFailedError.
    """
    by_channel = check_completeness(records, day, ctx.channels)
    amounts = hourly_amounts(by_channel, ctx.specs, day)
    expected = {
        spec.statistic_id: build_rows(day, amounts[spec.statistic_id], baselines[spec.statistic_id])
        for spec in ctx.specs
    }
    for spec in ctx.specs:
        async_add_external_statistics(hass, spec.metadata(), expected[spec.statistic_id])

    await _async_verify(hass, day, expected)

    estimated = sum(1 for r in records if r.quality == "estimated")
    summary = {
        "date": day.isoformat(),
        "mode": mode,
        "records": len(records),
        "estimated_records": estimated,
        "durations": sorted({r.duration for r in records}),
        "statistics": {
            sid: {
                "day_total": round(math.fsum(amounts[sid]), ROUND_DIGITS),
                "baseline": round(baselines[sid], ROUND_DIGITS),
                "sum": rows[-1]["sum"],
            }
            for sid, rows in expected.items()
        },
    }
    _LOGGER.info("Imported %s (%s): %d records, %d estimated", day, mode, len(records), estimated)
    return summary


async def async_latest_hours(hass: HomeAssistant, ids: Iterable[str]) -> dict[str, datetime | None]:
    """Return the start of the last stored hour for each statistic (None if none)."""
    recorder = get_instance(hass)
    latest: dict[str, datetime | None] = {}
    for sid in ids:
        result = await recorder.async_add_executor_job(
            get_last_statistics, hass, 1, sid, False, {"sum"}
        )
        latest[sid] = (
            datetime.fromtimestamp(result[sid][0]["start"], UTC) if result.get(sid) else None
        )
    return latest


async def async_baselines_before(
    hass: HomeAssistant, ids: Sequence[str], day: date
) -> dict[str, float]:
    """Return each statistic's sum in the hour just before NEM day ``day``.

    Raises ImportRefusedError if any statistic has no row there.
    """
    before = nem_day_start(day) - HOUR
    rows = await _async_read_rows(hass, ids, before, before + HOUR)
    baselines: dict[str, float] = {}
    for sid in ids:
        found = [r for r in rows.get(sid, []) if abs(r["start"] - before.timestamp()) < 0.5]
        if not found:
            raise ImportRefusedError(
                "partial_history",
                f"{sid} has no row for the hour before {day}; cannot continue the sums.",
            )
        baselines[sid] = float(found[0]["sum"])
    return baselines


async def async_plan_day(
    hass: HomeAssistant, ctx: ImportContext, day: date
) -> tuple[ImportMode, dict[str, float]]:
    """Apply the append-only rule and return the mode and baseline per statistic."""
    ids = [spec.statistic_id for spec in ctx.specs]
    day_start = nem_day_start(day)
    day_last = day_start + (DAY_HOURS - 1) * HOUR
    recorder = get_instance(hass)

    last: dict[str, Mapping[str, Any] | None] = {}
    for sid in ids:
        result = await recorder.async_add_executor_job(
            get_last_statistics, hass, 1, sid, False, {"state", "sum"}
        )
        last[sid] = result[sid][0] if result.get(sid) else None

    present = {sid: row for sid, row in last.items() if row is not None}
    if not present:
        return "first", dict.fromkeys(ids, 0.0)
    if len(present) != len(ids):
        missing = sorted(set(ids) - set(present))
        raise ImportRefusedError(
            "inconsistent_history",
            f"Some statistics have history and others do not ({missing}). "
            "Refusing to import until this is resolved.",
        )
    latest_starts = {datetime.fromtimestamp(row["start"], UTC) for row in present.values()}
    if len(latest_starts) != 1:
        raise ImportRefusedError(
            "inconsistent_history",
            "The statistics end at different hours: "
            f"{sorted(s.isoformat() for s in latest_starts)}. Refusing to import.",
        )
    latest = latest_starts.pop()
    latest_day = nem_date(latest)
    if latest != nem_day_start(latest_day) + (DAY_HOURS - 1) * HOUR:
        raise ImportRefusedError(
            "partial_history",
            f"The latest imported hour ({latest.isoformat()}) is not the last hour of a "
            "NEM day. Refusing to import.",
        )
    allowed = (
        f"Only {latest_day + timedelta(days=1)} (the next day) or {latest_day} "
        "(re-import of the latest day) can be imported now."
    )
    if latest == day_start - HOUR:
        return "append", {sid: float(row["sum"]) for sid, row in present.items()}
    if latest != day_last:
        raise ImportRefusedError(
            "not_next_day",
            f"{day} cannot be imported: the latest imported day is {latest_day}. {allowed}",
        )

    # Re-import of the latest day: its baseline is the cumulative total just before it.
    rows = await _async_read_rows(hass, ids, day_start - HOUR, day_start + DAY_HOURS * HOUR)
    baselines: dict[str, float] = {}
    for sid in ids:
        by_start = {datetime.fromtimestamp(r["start"], UTC): r for r in rows.get(sid, [])}
        day_rows = [by_start.get(day_start + h * HOUR) for h in range(DAY_HOURS)]
        if any(r is None for r in day_rows):
            raise ImportRefusedError(
                "partial_history",
                f"{sid} does not have all 24 hours of {day}. Refusing to re-import.",
            )
        first = day_rows[0]
        implied = float(first["sum"]) - float(first["state"])
        before = by_start.get(day_start - HOUR)
        if before is not None:
            if abs(float(before["sum"]) - implied) > 1e-6:
                raise ImportRefusedError(
                    "inconsistent_history",
                    f"{sid}: the stored first hour of {day} does not continue from the "
                    "hour before it. Refusing to re-import.",
                )
            baselines[sid] = float(before["sum"])
        else:
            baselines[sid] = implied
    return "reimport", baselines


async def _async_read_rows(
    hass: HomeAssistant, ids: Iterable[str], start: datetime, end: datetime
) -> dict[str, list[Mapping[str, Any]]]:
    return await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, end, set(ids), "hour", None, {"state", "sum"}
    )


async def _async_verify(
    hass: HomeAssistant, day: date, expected: Mapping[str, Sequence[Mapping[str, Any]]]
) -> None:
    """Read the day back until it matches what was written, or time out."""
    day_start = nem_day_start(day)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + VERIFY_TIMEOUT
    while True:
        rows = await _async_read_rows(hass, expected, day_start, day_start + DAY_HOURS * HOUR)
        problem = _compare(expected, rows)
        if problem is None:
            problem = await _async_check_nothing_after(hass, expected)
        if problem is None:
            return
        if loop.time() >= deadline:
            raise VerificationFailedError(
                "verification_failed",
                f"{day} was written but the read-back does not match: {problem}",
            )
        await asyncio.sleep(VERIFY_INTERVAL)


def _compare(
    expected: Mapping[str, Sequence[Mapping[str, Any]]],
    actual: Mapping[str, Sequence[Mapping[str, Any]]],
) -> str | None:
    for sid, want in expected.items():
        got = actual.get(sid, [])
        if len(got) != len(want):
            return f"{sid}: {len(got)} rows, expected {len(want)}"
        for w, g in zip(want, got, strict=True):
            if abs(g["start"] - w["start"].timestamp()) > 0.5:
                return f"{sid}: row at {g['start']} expected {w['start'].isoformat()}"
            for key in ("state", "sum"):
                if g.get(key) is None or abs(float(g[key]) - w[key]) > _TOLERANCE:
                    return f"{sid} {w['start'].isoformat()}: {key} {g.get(key)} != {w[key]}"
    return None


async def _async_check_nothing_after(
    hass: HomeAssistant, expected: Mapping[str, Sequence[Mapping[str, Any]]]
) -> str | None:
    recorder = get_instance(hass)
    for sid, want in expected.items():
        result = await recorder.async_add_executor_job(
            get_last_statistics, hass, 1, sid, False, {"sum"}
        )
        last = result.get(sid, [])
        if not last or abs(last[0]["start"] - want[-1]["start"].timestamp()) > 0.5:
            return f"{sid}: latest row is not the last hour of the imported day"
    return None
