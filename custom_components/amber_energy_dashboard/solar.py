"""Solar-aware appliance cost (DESIGN section 22, 2.2.0-dev4).

An appliance's normal cost statistic prices all of its energy at Amber's billed import
rate. With **solar-aware cost** on, a second statistic values the part of its energy that
the site's own solar covered at the **feed-in price** instead (the export income given
up by using it), and the rest at the import price, as before.

**The split, per interval** (5 minutes; an hour on the lower-precision path), from the
site's net flows: grid import ``I`` and export ``E`` (the grid meter), solar production
``S``, and the battery's charge ``C`` and discharge ``D`` when the site has those
sensors::

    house load      L = I + S + D - E - C   (at least 0)
    from solar      Ls = min(S, L)
    from battery    Lb = min(D, L - Ls)
    from the grid   Lg = L - Ls - Lb

The appliance takes each source in the same proportion as the whole house (its solar
part is its energy x Ls / L). With no load in an interval (L = 0) its energy counts as
grid.

**Limits** (documented in INSTALL.md):
- Flows inside an interval are netted: a 5-minute interval that both imported and
  exported counts only the difference, with solar first. The share is the house's
  average, not the appliance's own (a pool pump running at noon and a kettle at noon get
  the same share).
- The sensors must agree with each other (an inverter's solar reading against a CT
  meter's grid readings). The calibration offset is not applied.
- Battery discharge (2.2.0-dev4) is split out as its own source (``battery_kwh``) but
  still valued at the import price: its cost basis (the average cost of the energy
  charged into it) is planned for the next milestone.
- Only appliances on the general channel: a controlled load is metered separately.

**Prices** come from the day's billed records: the general channel's ``perKwh`` for the
import price, and the feed-in channel's ``perKwh`` (negative when earned) for the feed-in
value. With the export allowance on (section 20), the feed-in value in the penalty window
also gets back the network's penalty, in proportion to the part of that hour's window
export that was still within the allowance (the value an extra exported kWh would have
had). A day without measured export aggregates is valued unadjusted and flagged.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
import math
from typing import Any, Final

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import CONF_NAME, UnitOfPower
from homeassistant.core import HomeAssistant

from .api import UsageRecord
from .chains import (
    BUCKETS_PER_DAY,
    FIVE_MINUTES,
    PreciseDataUnavailable,
    SensorDataUnavailable,
    async_sensor_energy,
)
from .const import CHANNEL_GENERAL, CONF_EXPORT_SENSOR, CONF_IMPORT_SENSOR, CONF_OPTIONS
from .importer import DAY_HOURS, HOUR, floor_minute, nem_day_start
from .sources import CONF_SOLAR_ENERGY, CONF_SOLAR_POWER, Sources, today_meter, today_meters

APPLIANCE: Final = "appliance"
IMPORT: Final = "import"
EXPORT: Final = "export"
SOLAR: Final = "solar"
CHARGE: Final = "battery_charge"
DISCHARGE: Final = "battery_discharge"

Series = tuple[list[float] | None, list[float] | None]
"""(288 five-minute, 24 hourly) energies in kWh; each None when incomplete."""


@dataclass(frozen=True, slots=True)
class SiteFlows:
    """The site's sensors the solar-aware split reads."""

    meter: str
    """The grid meter's name."""
    import_entity: str
    export_entity: str
    solar_energy: str | None = None
    solar_power: str | None = None
    """Used when there is no solar energy sensor."""
    battery_charge: str | None = None
    battery_discharge: str | None = None

    @property
    def battery(self) -> bool:
        """Both battery sensors are set."""
        return self.battery_charge is not None and self.battery_discharge is not None

    def as_dict(self) -> dict[str, Any]:
        """For diagnostics."""
        return {
            "meter": self.meter,
            "import": self.import_entity,
            "export": self.export_entity,
            "solar_energy": self.solar_energy,
            "solar_power": None if self.solar_energy else self.solar_power,
            "battery_charge": self.battery_charge if self.battery else None,
            "battery_discharge": self.battery_discharge if self.battery else None,
        }


def flows_meter(
    entry: ConfigEntry, site: Mapping[str, Any], options: Mapping[str, Any]
) -> ConfigSubentry | None:
    """The grid meter the split reads: Today so far's meter when it has an export, else
    the first general-channel grid meter that has one."""
    chosen = today_meter(entry, site, options)
    if chosen is not None and chosen.data.get(CONF_EXPORT_SENSOR):
        return chosen
    return next((m for m in today_meters(entry, site) if m.data.get(CONF_EXPORT_SENSOR)), None)


def site_flows(
    entry: ConfigEntry, site: Mapping[str, Any], options: Mapping[str, Any], sources: Sources
) -> SiteFlows | None:
    """The site's flows, or None without a solar source or a grid meter with import and
    export."""
    meter = flows_meter(entry, site, options)
    if meter is None or not (sources.solar_energy or sources.solar_power):
        return None
    return SiteFlows(
        meter.data[CONF_NAME],
        meter.data[CONF_IMPORT_SENSOR],
        meter.data[CONF_EXPORT_SENSOR],
        sources.solar_energy,
        sources.solar_power,
        sources.battery_charge,
        sources.battery_discharge,
    )


def solar_aware_problem(
    entry: ConfigEntry, site: Mapping[str, Any], channel_type: str
) -> str | None:
    """Why an appliance on a ``channel_type`` channel can't have solar-aware cost on
    this site (an error key), or None."""
    options = site.get(CONF_OPTIONS) or {}
    if channel_type != CHANNEL_GENERAL:
        return "solar_aware_channel"
    if not (options.get(CONF_SOLAR_ENERGY) or options.get(CONF_SOLAR_POWER)):
        return "solar_aware_no_solar"
    if flows_meter(entry, site, options) is None:
        return "solar_aware_no_grid_meter"
    return None


def flows_missing(
    entry: ConfigEntry, site: Mapping[str, Any], options: Mapping[str, Any], sources: Sources
) -> list[str]:
    """What the site lacks for its flows, in words (for the Repairs issue)."""
    missing = []
    if not (sources.solar_energy or sources.solar_power):
        missing.append("a solar sensor in the site's Sources")
    if flows_meter(entry, site, options) is None:
        missing.append("a grid meter with import and export")
    return missing


async def async_power_energy(hass: HomeAssistant, entity_id: str, day: date) -> Series:
    """A power sensor's energy per 5 minutes and per hour (kWh) for NEM day ``day``, from
    its mean statistics (each None when a bucket has no mean)."""
    start = nem_day_start(day)
    end = start + DAY_HOURS * HOUR
    units = {"power": UnitOfPower.WATT}
    recorder = get_instance(hass)
    result: list[list[float] | None] = []
    for period, step, count in (("5minute", FIVE_MINUTES, BUCKETS_PER_DAY), ("hour", HOUR, 24)):
        rows = await recorder.async_add_executor_job(
            statistics_during_period, hass, start, end, {entity_id}, period, units, {"mean"}
        )
        by_start = {round(r["start"]): r.get("mean") for r in rows.get(entity_id, [])}
        means = [by_start.get(round((start + i * step).timestamp())) for i in range(count)]
        hours = step / HOUR
        result.append(
            None if any(m is None for m in means) else [float(m) * hours / 1000 for m in means]  # type: ignore[arg-type]
        )
    return result[0], result[1]


async def async_series(
    hass: HomeAssistant, flows: SiteFlows, appliance: str, day: date, *, first_day: bool
) -> dict[str, Series]:
    """Every series the split needs for ``day``, by role. On the appliance's
    ``first_day`` its energy before its first statistic counts as zero."""
    series = {
        APPLIANCE: await async_sensor_energy(hass, appliance, day, first_day=first_day),
        IMPORT: await async_sensor_energy(hass, flows.import_entity, day),
        EXPORT: await async_sensor_energy(hass, flows.export_entity, day),
        SOLAR: await async_sensor_energy(hass, flows.solar_energy, day)
        if flows.solar_energy
        else await async_power_energy(hass, flows.solar_power or "", day),
    }
    if flows.battery:
        series[CHARGE] = await async_sensor_energy(hass, flows.battery_charge or "", day)
        series[DISCHARGE] = await async_sensor_energy(hass, flows.battery_discharge or "", day)
    return series


def split(
    grid_import: float,
    grid_export: float,
    solar: float,
    charge: float = 0.0,
    discharge: float = 0.0,
) -> tuple[float, float, float]:
    """The house load's shares (solar, battery, grid) for one interval; all zero with
    no load."""
    load = max(grid_import + solar + discharge - grid_export - charge, 0.0)
    if load <= 0:
        return 0.0, 0.0, 0.0
    from_solar = min(max(solar, 0.0), load)
    from_battery = min(max(discharge, 0.0), load - from_solar)
    return from_solar / load, from_battery / load, (load - from_solar - from_battery) / load


@dataclass(slots=True)
class SolarAwareDay:
    """One day of an appliance's solar-aware cost."""

    amounts: list[float]
    """24 hourly amounts in AUD."""
    energy_kwh: float
    solar_kwh: float
    battery_kwh: float
    grid_kwh: float
    lower_precision: bool


def _resolution(
    series: Mapping[str, Series], allow_fallback: bool, day: date, names: Mapping[str, str]
) -> int:
    """0 for 5-minute data, 1 for hourly; raises when neither is complete for every
    series (naming the first that lacks it)."""
    if all(five is not None for five, _ in series.values()):
        return 0
    missing = [role for role, (_, hourly) in series.items() if hourly is None]
    if missing:
        raise SensorDataUnavailable(
            f"no complete statistics for {day} from {names.get(missing[0], missing[0])}"
        )
    if not allow_fallback:
        raise PreciseDataUnavailable(f"5-minute statistics for {day} are no longer available")
    return 1


def solar_aware_day(
    general: Sequence[UsageRecord],
    feed_in: Sequence[UsageRecord],
    day: date,
    series: Mapping[str, Series],
    *,
    allow_fallback: bool,
    extra_feed_in: Callable[[UsageRecord], float] = lambda _: 0.0,
    names: Mapping[str, str] | None = None,
) -> SolarAwareDay:
    """Value one day of an appliance's energy (see the module docstring). ``general`` and
    ``feed_in`` are the day's records of the appliance's channel and the feed-in channel;
    ``extra_feed_in`` gives a general record's extra feed-in value in c/kWh (the export
    allowance's refund).

    Raises SensorDataUnavailable when a series has not even hourly data, and
    PreciseDataUnavailable when only hourly data exists and the fallback is off.
    """
    resolution = _resolution(series, allow_fallback, day, names or {})
    day_start = nem_day_start(day)
    feed = {floor_minute(r.start_time): r.per_kwh for r in feed_in}
    # (start offset in 5-minute buckets, length, import c/kWh, feed-in value c/kWh)
    prices = [
        (
            int((floor_minute(r.start_time) - day_start) / FIVE_MINUTES),
            r.duration // 5,
            r.per_kwh,
            -feed.get(floor_minute(r.start_time), 0.0) + extra_feed_in(r),
        )
        for r in general
    ]
    width = 1 if resolution == 0 else 12
    count = BUCKETS_PER_DAY // width
    import_price = [0.0] * count
    feed_value = [0.0] * count
    if resolution == 0:
        for offset, length, price, value in prices:
            for i in range(offset, offset + length):
                import_price[i], feed_value[i] = price, value
    else:  # the hour's mean prices, as the own-sensor fallback does
        by_hour: list[list[tuple[float, float]]] = [[] for _ in range(DAY_HOURS)]
        for offset, _length, price, value in prices:
            by_hour[offset // 12].append((price, value))
        for h, values in enumerate(by_hour):
            import_price[h] = math.fsum(p for p, _ in values) / len(values)
            feed_value[h] = math.fsum(v for _, v in values) / len(values)
    data = {role: s[resolution] for role, s in series.items()}
    zeros = [0.0] * count
    amounts = [0.0] * DAY_HOURS
    totals = {SOLAR: 0.0, "battery": 0.0, "grid": 0.0}
    for i in range(count):
        energy = data[APPLIANCE][i]  # type: ignore[index]
        solar, battery, grid = split(
            data[IMPORT][i],  # type: ignore[index]
            data[EXPORT][i],  # type: ignore[index]
            data[SOLAR][i],  # type: ignore[index]
            (data.get(CHARGE) or zeros)[i],
            (data.get(DISCHARGE) or zeros)[i],
        )
        if solar + battery + grid == 0:
            grid = 1.0  # no load measured: as grid
        totals[SOLAR] += energy * solar
        totals["battery"] += energy * battery
        totals["grid"] += energy * grid
        # Battery discharge is valued at the import price until its cost basis (M21).
        cost = energy * (solar * feed_value[i] + (battery + grid) * import_price[i]) / 100
        amounts[i * width // 12] += cost
    return SolarAwareDay(
        amounts,
        math.fsum(data[APPLIANCE]),  # type: ignore[arg-type]
        totals[SOLAR],
        totals["battery"],
        totals["grid"],
        resolution == 1,
    )


def hour_key(moment: datetime) -> int:
    """The UTC timestamp of the hour ``moment`` is in (NEM hours are whole UTC hours)."""
    return round(floor_minute(moment).replace(minute=0).timestamp())


def allowance_extra(
    penalty_period: str, penalty_rate: float | None, fractions: Mapping[int, float]
) -> Callable[[UsageRecord], float]:
    """The export allowance's extra feed-in value (c/kWh) for a general record: in the
    penalty period, the day's penalty rate times the share of the record's hour still
    within the allowance (``export.refund_fractions``)."""

    def extra(record: UsageRecord) -> float:
        if (record.tariff_information or {}).get("period") != penalty_period or not penalty_rate:
            return 0.0
        return penalty_rate * fractions.get(hour_key(record.start_time), 0.0)

    return extra
