"""Statistics derived from the integration's own statistics (DESIGN sections 19 and 20).

- **Cost including fixed charges:** the import cost statistics (general and controlled
  load) plus the daily fixed charges, including GST.
- **Adjusted compensation:** the feed-in compensation plus the export penalty refunded
  within the free export allowance.

A derived statistic follows its base statistics row by row, over their whole history
(including history copied from a YAML kit), so it needs no API calls. Its ``sum`` at each
row is the base statistics' sums (carried forward) plus the accumulated extra amounts; its
``state`` is the change from the previous row.

**Sync by difference.** Each sync recomputes the expected rows from the stored base rows
and rewrites the derived statistic from the first row that differs. Any rewrite of a base
statistic (a revision, a tail rewrite, crash recovery, a backfill or a migration) is
therefore followed without hooks in each write path. If the derived statistic holds a row
the base statistics no longer have, it is cleared and written again in full. Every write
is read back.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import math
from typing import Any, Final

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant

from . import importer
from .importer import HOUR, ROUND_DIGITS
from .statistics import StatisticSpec

_EPOCH: Final = datetime(2000, 1, 1, tzinfo=UTC)
_TOLERANCE: Final = 2e-6

Extra = Callable[[datetime | None, datetime], float]
"""The amount added at a row, given the previous row's start (None for the first)."""


@dataclass(frozen=True, slots=True)
class Derived:
    """A derived statistic: its spec, its base statistics, and the extra per row."""

    spec: StatisticSpec
    base_ids: tuple[str, ...]
    extra: Extra


def fixed_extra(schedule: Sequence[Mapping[str, Any]]) -> Extra:
    """Fixed charges accrue with the time between rows: an hourly row adds the daily
    amount ÷ 24, a daily lump (copied YAML history) a whole day, and the first row its own
    hour. The amount is the one in force at the row's start (``schedule``: entries with
    ``from``, None for the start)."""
    steps = [
        (None if e["from"] is None else datetime.fromisoformat(e["from"]), e["daily_incl_gst"])
        for e in schedule
    ]

    def extra(previous: datetime | None, start: datetime) -> float:
        elapsed = HOUR if previous is None else start - previous
        daily = 0.0
        for since, amount in steps:
            if since is None or start >= since:
                daily = amount
        return daily * (elapsed / timedelta(days=1))

    return extra


def refund_extra(by_hour: Mapping[int, float]) -> Extra:
    """The export penalty refunded in each hour (UTC timestamp → AUD)."""

    def extra(_previous: datetime | None, start: datetime) -> float:
        return by_hour.get(round(start.timestamp()), 0.0)

    return extra


def build_rows(base: Mapping[str, Sequence[Mapping[str, Any]]], extra: Extra) -> list[dict]:
    """The derived rows: one per start time of any base statistic."""
    sums = {sid: {round(r["start"]): float(r["sum"]) for r in rows} for sid, rows in base.items()}
    states = {
        sid: {round(r["start"]): float(r["state"] or 0.0) for r in rows}
        for sid, rows in base.items()
    }
    carried = dict.fromkeys(sums, 0.0)
    added = 0.0
    previous: datetime | None = None
    previous_total: float | None = None
    out: list[dict[str, Any]] = []
    for ts in sorted(set().union(*(set(s) for s in sums.values()))):
        start = datetime.fromtimestamp(ts, UTC)
        for sid, by_start in sums.items():
            carried[sid] = by_start.get(ts, carried[sid])
        added += extra(previous, start)
        total = math.fsum(carried.values()) + added
        if previous_total is None:
            state = math.fsum(s.get(ts, 0.0) for s in states.values()) + added
        else:
            state = total - previous_total
        out.append(
            {"start": start, "state": round(state, ROUND_DIGITS), "sum": round(total, ROUND_DIGITS)}
        )
        previous, previous_total = start, total
    return out


async def _async_read_all(hass: HomeAssistant, ids: Sequence[str]) -> dict[str, list]:
    return await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, _EPOCH, None, set(ids), "hour", None, {"state", "sum"}
    )


def _same(want: Mapping[str, Any], got: Mapping[str, Any]) -> bool:
    return (
        abs(got["start"] - want["start"].timestamp()) < 0.5
        and got.get("sum") is not None
        and abs(float(got["sum"]) - want["sum"]) <= _TOLERANCE
        and abs(float(got["state"] or 0.0) - want["state"]) <= _TOLERANCE
    )


async def async_sync(hass: HomeAssistant, derived: Derived) -> dict[str, Any]:
    """Bring the derived statistic in line with its base statistics; return what changed."""
    sid = derived.spec.statistic_id
    rows = await _async_read_all(hass, [*derived.base_ids, sid])
    expected = build_rows({b: rows.get(b, []) for b in derived.base_ids}, derived.extra)
    stored = rows.get(sid, [])
    wanted = {round(r["start"].timestamp()) for r in expected}
    result: dict[str, Any] = {"rows": len(expected), "written": 0, "from": None, "cleared": False}
    if any(round(r["start"]) not in wanted for r in stored):
        recorder = get_instance(hass)
        recorder.async_clear_statistics([sid])
        await recorder.async_block_till_done()
        stored = []
        result["cleared"] = True
    first = next(
        (i for i, want in enumerate(expected) if i >= len(stored) or not _same(want, stored[i])),
        None,
    )
    if first is None:
        return result
    write = expected[first:]
    async_add_external_statistics(hass, derived.spec.metadata(), write)
    await importer.async_verify_range(
        hass, {sid: write}, write[0]["start"], write[-1]["start"] + importer.HOUR
    )
    result.update({"written": len(write), "from": write[0]["start"].isoformat()})
    return result
