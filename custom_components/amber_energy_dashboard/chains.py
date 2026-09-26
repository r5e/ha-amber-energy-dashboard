"""Per-day computations for the secondary chains (DESIGN section 11).

- **Price series** (optional): the hourly mean, min and max of Amber's billed ``perKwh``
  per channel, in AUD/kWh. A mean of prices is *not* a cost rate: cost follows usage,
  which is not spread evenly over an hour.
- **Own-sensor cost**: a user's cumulative energy sensor priced at Amber's billed rate.
  Precise path: each Amber interval's energy from the sensor's 5-minute short-term
  statistics, times that interval's ``perKwh``. Fallback, when the 5-minute data is gone
  (HA purges it after about 10 days): each hour's energy from hourly statistics, times
  the hour's mean ``perKwh``. The fallback is flagged ``lower_precision``.

Amber records carry a one-second ``startTime`` offset (``14:00:01Z``). They are floored
to the minute, so they line up with HA's 5-minute buckets (``14:00:00Z``).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
import math
from typing import Any, Final

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant

from .api import UsageRecord
from .importer import DAY_HOURS, HOUR, ROUND_DIGITS, floor_minute, nem_day_start
from .statistics import MeanSpec

FIVE_MINUTES: Final = timedelta(minutes=5)
BUCKETS_PER_DAY: Final = 288


class SensorDataUnavailable(Exception):
    """The sensor has no complete hourly statistics for the day."""


class PreciseDataUnavailable(Exception):
    """5-minute data is incomplete or gone, and the hourly fallback is not allowed."""


@dataclass(slots=True)
class OwnCostDay:
    """The result of pricing one day of an own sensor."""

    amounts: list[float]
    """24 hourly amounts in AUD (feed-in: positive means earned)."""
    energy_kwh: float
    lower_precision: bool


def price_rows(
    records: Sequence[UsageRecord], specs: Sequence[MeanSpec], day: date
) -> dict[str, list[dict[str, Any]]]:
    """Hourly mean/min/max of perKwh (AUD/kWh) per channel, 24 rows per statistic."""
    day_start = nem_day_start(day)
    rows: dict[str, list[dict[str, Any]]] = {}
    for spec in specs:
        by_hour: list[list[float]] = [[] for _ in range(DAY_HOURS)]
        for r in records:
            if r.channel_identifier == spec.channel:
                index = int((floor_minute(r.start_time) - day_start) / HOUR)
                by_hour[index].append(r.per_kwh / 100)
        rows[spec.statistic_id] = [
            {
                "start": day_start + h * HOUR,
                "mean": round(math.fsum(v) / len(v), ROUND_DIGITS),
                "min": round(min(v), ROUND_DIGITS),
                "max": round(max(v), ROUND_DIGITS),
            }
            for h, v in enumerate(by_hour)
        ]
    return rows


async def async_sensor_energy(
    hass: HomeAssistant, entity_id: str, day: date, *, first_day: bool = False
) -> tuple[list[float] | None, list[float] | None]:
    """Return (288 five-minute energies, 24 hourly energies) in kWh for NEM day ``day``.

    Each list is None when its statistics are incomplete. Energy in a bucket is the
    change of the statistic's ``sum`` over it, which handles meter resets. On the
    sensor's ``first_day`` (its history starts that day), buckets before its first
    statistic count as zero: the sensor did not exist yet, so it measured nothing.
    """
    start = nem_day_start(day)
    end = start + DAY_HOURS * HOUR
    units = {"energy": UnitOfEnergy.KILO_WATT_HOUR}
    recorder = get_instance(hass)
    short = await recorder.async_add_executor_job(
        statistics_during_period,
        hass,
        start - FIVE_MINUTES,
        end,
        {entity_id},
        "5minute",
        units,
        {"sum"},
    )
    hourly = await recorder.async_add_executor_job(
        statistics_during_period, hass, start - HOUR, end, {entity_id}, "hour", units, {"sum"}
    )
    return (
        _deltas(
            short.get(entity_id, []), start - FIVE_MINUTES, FIVE_MINUTES, BUCKETS_PER_DAY, first_day
        ),
        _deltas(hourly.get(entity_id, []), start - HOUR, HOUR, DAY_HOURS, first_day),
    )


def _deltas(
    rows: Sequence[Mapping[str, Any]],
    first: Any,
    step: timedelta,
    count: int,
    leading_gap: bool = False,
) -> list[float] | None:
    by_start = {round(r["start"]): r.get("sum") for r in rows}
    sums: list[Any] = [
        by_start.get(round((first + i * step).timestamp())) for i in range(count + 1)
    ]
    if leading_gap:
        known = [i for i, v in enumerate(sums) if v is not None]
        if not known:
            return None
        sums = [sums[known[0]] if i < known[0] else v for i, v in enumerate(sums)]
    if any(v is None for v in sums):
        return None
    return [float(sums[i + 1]) - float(sums[i]) for i in range(count)]


def own_cost_day(
    records: Sequence[UsageRecord],
    day: date,
    five_minute: Sequence[float] | None,
    hourly: Sequence[float] | None,
    *,
    feed_in: bool,
    allow_fallback: bool,
) -> OwnCostDay:
    """Price one day of a sensor. ``records`` are one channel's usage records for the day.

    Raises SensorDataUnavailable if not even hourly data exists, and
    PreciseDataUnavailable if only hourly data exists and the fallback is off.
    """
    day_start = nem_day_start(day)
    sign = -1.0 if feed_in else 1.0  # Amber: feed-in cost is negative; ours is positive
    buckets: list[list[float]] = [[] for _ in range(DAY_HOURS)]
    if five_minute is not None:
        for r in records:
            offset = int((floor_minute(r.start_time) - day_start) / FIVE_MINUTES)
            n = r.duration // 5
            energy = math.fsum(five_minute[offset : offset + n])
            buckets[offset // 12].append(energy * r.per_kwh / 100)
        amounts = [sign * math.fsum(v) for v in buckets]
        return OwnCostDay(amounts, round(math.fsum(five_minute), ROUND_DIGITS), False)
    if hourly is None:
        raise SensorDataUnavailable(f"no complete hourly statistics for {day}")
    if not allow_fallback:
        raise PreciseDataUnavailable(f"5-minute statistics for {day} are no longer available")
    prices: list[list[float]] = [[] for _ in range(DAY_HOURS)]
    for r in records:
        prices[int((floor_minute(r.start_time) - day_start) / HOUR)].append(r.per_kwh)
    amounts = [
        sign * hourly[h] * (math.fsum(prices[h]) / len(prices[h])) / 100 for h in range(DAY_HOURS)
    ]
    return OwnCostDay(amounts, round(math.fsum(hourly), ROUND_DIGITS), True)
