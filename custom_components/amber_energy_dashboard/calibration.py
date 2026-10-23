"""A grid meter's calibration offset (DESIGN section 21).

For each hour of the complete billed days in a rolling window, Amber's net energy
(import - export, from the integration's own hourly statistics) minus the meter's net
energy (import - export, from the mapped sensors' hourly statistics), in watts (kWh per
hour x 1000). The published value is the median over the window, so a few odd hours
(a sensor restart, a missed reading) don't move it.

Only the mapped sensors are used, so they must be the raw, uncorrected ones: a sensor
already corrected by this offset would feed the correction back into itself.
"""

from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta
import statistics
from typing import Any, Final

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant

from .importer import DAY_HOURS, HOUR, nem_day_start
from .storage import STATUS_IMPORTED

WINDOW_DAYS: Final = 14
MIN_HOURS: Final = 24
"""Fewer usable hours than this publish no value (the attributes still say why)."""


def billed_days(record_for: Callable[[date], Mapping[str, Any] | None], last: date) -> list[date]:
    """The complete billed days in the window ending at ``last`` (the last imported day):
    imported, with no estimated interval (an estimated day may still be revised)."""
    window = [last - timedelta(days=n) for n in range(WINDOW_DAYS - 1, -1, -1)]
    return [
        day
        for day in window
        if (record := record_for(day))
        and record.get("status") == STATUS_IMPORTED
        and not record.get("estimated_records")
    ]


async def async_offset(
    hass: HomeAssistant,
    days: Sequence[date],
    meter: tuple[str, str],
    amber: tuple[str, str],
    now: datetime,
) -> dict[str, Any]:
    """The offset over ``days``: ``meter`` and ``amber`` are (import, export) statistic
    IDs. Hours where either meter statistic has a gap are left out."""
    hourly: list[float] = []
    if days:
        start = nem_day_start(days[0]) - HOUR
        end = nem_day_start(days[-1]) + DAY_HOURS * HOUR
        rows = await get_instance(hass).async_add_executor_job(
            statistics_during_period,
            hass,
            start,
            end,
            {*meter, *amber},
            "hour",
            {"energy": UnitOfEnergy.KILO_WATT_HOUR},
            {"state", "sum"},
        )
        sums = {sid: _by_start(rows.get(sid, []), "sum") for sid in meter}
        states = {sid: _by_start(rows.get(sid, []), "state") for sid in amber}
        for day in days:
            for h in range(DAY_HOURS):
                hour = nem_day_start(day) + h * HOUR
                values = [
                    _change(sums[meter[0]], hour),
                    _change(sums[meter[1]], hour),
                    states[amber[0]].get(round(hour.timestamp())),
                    states[amber[1]].get(round(hour.timestamp())),
                ]
                if any(v is None for v in values):
                    continue
                meter_in, meter_out, amber_in, amber_out = values
                hourly.append(((amber_in - amber_out) - (meter_in - meter_out)) * 1000)
    return summarise(hourly, days, now)


def _by_start(rows: Sequence[Mapping[str, Any]], key: str) -> dict[int, float | None]:
    return {round(r["start"]): r.get(key) for r in rows}


def _change(sums: Mapping[int, float | None], hour: datetime) -> float | None:
    """The meter's energy in ``hour``: the change of its sum, None on a gap."""
    before = sums.get(round((hour - HOUR).timestamp()))
    after = sums.get(round(hour.timestamp()))
    if before is None or after is None:
        return None
    return after - before


def summarise(hourly: Sequence[float], days: Sequence[date], now: datetime) -> dict[str, Any]:
    """The median offset (W) with its spread; no value below ``MIN_HOURS`` hours."""
    result: dict[str, Any] = {
        "offset_w": None,
        "hours_used": len(hourly),
        "days_used": len(days),
        "first_day": days[0].isoformat() if days else None,
        "last_day": days[-1].isoformat() if days else None,
        "p25_w": None,
        "p75_w": None,
        "iqr_w": None,
        "last_updated": now.isoformat(timespec="seconds"),
    }
    if len(hourly) < MIN_HOURS:
        return result
    p25, _, p75 = statistics.quantiles(hourly, n=4, method="inclusive")
    result.update(
        offset_w=round(statistics.median(hourly), 1),
        p25_w=round(p25, 1),
        p75_w=round(p75, 1),
        iqr_w=round(p75 - p25, 1),
    )
    return result
