"""Milestone 20 (2.2.0-dev4): solar-aware appliance cost (DESIGN section 22). All data is
synthetic: Amber records from ``synthetic.make_usage_day``, sensor statistics imported
straight into the recorder."""

from datetime import date, timedelta
import math
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.db_schema import Statistics, StatisticsShortTerm
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er, issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.amber_energy_dashboard import (
    export,
    importer,
    manager as solar_mod_manager,
    solar,
)
from custom_components.amber_energy_dashboard.api import _parse_usage
from custom_components.amber_energy_dashboard.chains import (
    PreciseDataUnavailable,
    SensorDataUnavailable,
)
from custom_components.amber_energy_dashboard.const import DOMAIN
from custom_components.amber_energy_dashboard.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .fake_amber import FakeAmber
from .synthetic import make_usage_day
from .test_manager import (  # noqa: F401 - fixtures are used by name
    PREFIX,
    TODAY,
    YESTERDAY,
    Clock,
    _fast_verify,
    _no_real_timers,
    _preload_store,
    _rows,
    _run,
    _setup,
    _setup_entry,
    clock,
)
from .test_own import _import_sensor

FIVE = timedelta(minutes=5)
HOUR = timedelta(hours=1)
APPLIANCE = "sensor.pool_pump_energy"
IMPORT = "sensor.grid_import"
EXPORT = "sensor.grid_export"
SOLAR = "sensor.solar_energy"
SOLAR_POWER = "sensor.solar_power"
CHARGE = "sensor.battery_charge"
DISCHARGE = "sensor.battery_discharge"
SOLAR_AWARE = f"{PREFIX}_own_pool_pump_energy_cost_solar_aware"
NORMAL = f"{PREFIX}_own_pool_pump_energy_cost"
SENSOR = "sensor.pool_pump_yesterday_solar_aware_cost"


# --- synthetic site flows -----------------------------------------------------------------


def _flows(day: date) -> dict[str, list[float]]:
    """288 five-minute energies (kWh) for a sunny day: solar, a house load, the grid
    flows they imply, and a pool pump that is 30 % of the load."""
    solar_kwh, load = [], []
    for i in range(288):
        hour = (i + 0.5) / 12
        solar_kwh.append(round(0.4 * max(0.0, math.sin(math.pi * (hour - 6.5) / 11.0)), 6))
        load.append(
            round(0.05 + 0.15 * math.exp(-(((hour - 19.0) / 1.5) ** 2)) + 0.02 * (day.day % 3), 6)
        )
    return {
        "solar": solar_kwh,
        "load": load,
        "import": [max(d - s, 0.0) for d, s in zip(load, solar_kwh, strict=True)],
        "export": [max(s - d, 0.0) for d, s in zip(load, solar_kwh, strict=True)],
        "appliance": [0.3 * d for d in load],
    }


def _expected(day: date, flows: dict[str, list[float]], extra=lambda i: 0.0) -> list[float]:
    """Independent expectation: per interval, the pump's solar part (solar / load) at the
    feed-in price and the rest at the general price, summed per hour (AUD)."""
    records = [_parse_usage(r) for r in make_usage_day(day)]
    general = [r for r in records if r.channel_identifier == "E1"]
    feed = [r for r in records if r.channel_identifier == "B1"]
    hours = [0.0] * 24
    for i in range(288):
        load, sun = flows["load"][i], flows["solar"][i]
        share = min(sun, load) / load
        price, earned = general[i].per_kwh, -feed[i].per_kwh + extra(i)
        hours[i // 12] += flows["appliance"][i] * (share * earned + (1 - share) * price) / 100
    return hours


async def _import_power(hass: HomeAssistant, entity_id: str, kwh: dict[date, list[float]]) -> None:
    """Mean statistics (W) for a power sensor, 5-minute and hourly, from 5-minute energies."""
    hass.states.async_set(
        entity_id,
        "0",
        {"device_class": "power", "state_class": "measurement", "unit_of_measurement": "W"},
    )
    meta = {
        "has_sum": False,
        "mean_type": StatisticMeanType.ARITHMETIC,
        "name": None,
        "source": "recorder",
        "statistic_id": entity_id,
        "unit_class": "power",
        "unit_of_measurement": "W",
    }
    short, hourly = [], []
    for day, values in kwh.items():
        start = importer.nem_day_start(day)
        watts = [v * 12 * 1000 for v in values]
        for i, w in enumerate(watts):
            short.append({"start": start + i * FIVE, "mean": w, "min": w, "max": w})
        for h in range(24):
            mean = math.fsum(watts[h * 12 : h * 12 + 12]) / 12
            hourly.append({"start": start + h * HOUR, "mean": mean, "min": mean, "max": mean})
    recorder = get_instance(hass)
    recorder.async_import_statistics(meta, short, StatisticsShortTerm)
    recorder.async_import_statistics(meta, hourly, Statistics)
    await async_wait_recording_done(hass)


def _grid_meter() -> dict[str, Any]:
    return {
        "data": {
            "name": "Whole house meter",
            "import_entity_id": IMPORT,
            "import_channel": "E1",
            "export_entity_id": EXPORT,
            "export_channel": "B1",
        },
        "subentry_id": "sub_grid",
        "subentry_type": "grid_meter",
        "title": "Whole house meter · grid meter",
        "unique_id": f"grid_meter:{IMPORT}",
    }


def _appliance(solar_aware: bool = True, channel: str = "E1") -> dict[str, Any]:
    return {
        "data": {
            "name": "Pool pump",
            "entity_id": APPLIANCE,
            "channel": channel,
            "solar_aware": solar_aware,
        },
        "subentry_id": "sub_pump",
        "subentry_type": "appliance",
        "title": "Pool pump · appliance (general E1)",
        "unique_id": f"appliance:{APPLIANCE}",
    }


async def _site(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict,
    days: list[date],
    *,
    options: dict | None = None,
    power: bool = False,
    grid: bool = True,
    short: bool = True,
    store: dict | None = None,
):
    """A site with a grid meter, a solar sensor and a solar-aware pool pump, with sensor
    statistics for ``days``."""
    _preload_store(hass_storage, retention_days=len(days), **(store or {}))
    flows = {d: _flows(d) for d in days}
    await _import_sensor(
        hass, APPLIANCE, {d: f["appliance"] for d, f in flows.items()}, short=short
    )
    await _import_sensor(hass, IMPORT, {d: f["import"] for d, f in flows.items()})
    await _import_sensor(hass, EXPORT, {d: f["export"] for d, f in flows.items()})
    if power:
        await _import_power(hass, SOLAR_POWER, {d: f["solar"] for d, f in flows.items()})
        sources = {"solar_power": SOLAR_POWER}
    else:
        await _import_sensor(hass, SOLAR, {d: f["solar"] for d, f in flows.items()})
        sources = {"solar_energy": SOLAR}
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    entry = await _setup_entry(
        hass,
        aioclient_mock,
        fake,
        options={"schedule_mode": "automatic", **sources, **(options or {})},
        subentries=[_grid_meter(), _appliance()] if grid else [_appliance()],
    )
    return entry, flows


# --- the split and one day's valuation ----------------------------------------------------


def test_split() -> None:
    """The house load from the net flows; solar first, then the battery, then the grid."""
    assert solar.split(1.0, 0.0, 1.0) == (0.5, 0.0, 0.5)
    assert solar.split(0.0, 0.5, 1.5) == (1.0, 0.0, 0.0)  # exporting: all solar
    assert solar.split(0.0, 0.0, 0.0) == (0.0, 0.0, 0.0)  # no load
    assert solar.split(0.0, 2.0, 1.0) == (0.0, 0.0, 0.0)  # sensors disagree: no load
    assert solar.split(0.2, 0.0, 0.3, 0.0, 0.5) == pytest.approx((0.3, 0.5, 0.2))
    # Charging the battery from solar: the surplus isn't load.
    assert solar.split(0.0, 0.0, 1.0, 0.6) == pytest.approx((1.0, 0.0, 0.0))
    # Charging from the grid at night: the load is what's left.
    assert solar.split(1.0, 0.0, 0.0, 0.6) == pytest.approx((0.0, 0.0, 1.0))
    assert solar.split(0.5, 0.0, -0.1) == (0.0, 0.0, 1.0)  # a negative reading counts nothing


def _records(day: date) -> tuple[list, list]:
    records = [_parse_usage(r) for r in make_usage_day(day)]
    return (
        [r for r in records if r.channel_identifier == "E1"],
        [r for r in records if r.channel_identifier == "B1"],
    )


def _series(flows: dict[str, list[float]], hourly_only: bool = False) -> dict[str, Any]:
    def both(values: list[float]):
        hours = [math.fsum(values[h * 12 : h * 12 + 12]) for h in range(24)]
        return (None if hourly_only else values, hours)

    return {
        solar.APPLIANCE: both(flows["appliance"]),
        solar.IMPORT: both(flows["import"]),
        solar.EXPORT: both(flows["export"]),
        solar.SOLAR: both(flows["solar"]),
    }


def test_solar_aware_day_matches_independent_sum() -> None:
    """Each interval's solar part at the feed-in price, the rest at the general price."""
    general, feed = _records(YESTERDAY)
    flows = _flows(YESTERDAY)
    day = solar.solar_aware_day(general, feed, YESTERDAY, _series(flows), allow_fallback=False)
    for got, want in zip(day.amounts, _expected(YESTERDAY, flows), strict=True):
        assert got == pytest.approx(want, abs=1e-9)
    assert day.energy_kwh == pytest.approx(math.fsum(flows["appliance"]))
    assert day.solar_kwh + day.grid_kwh == pytest.approx(day.energy_kwh)
    assert day.solar_kwh > 0.2 * day.energy_kwh
    assert day.battery_kwh == 0.0
    assert day.lower_precision is False
    # Cheaper than the billed-rate cost: solar is worth less than the import price here.
    billed = math.fsum(
        a * r.per_kwh / 100 for a, r in zip(flows["appliance"], general, strict=True)
    )
    assert math.fsum(day.amounts) < billed


def test_solar_aware_day_battery_and_no_load() -> None:
    """Battery discharge is its own source, valued at the import price for now; with no
    load the energy counts as grid."""
    general, feed = _records(YESTERDAY)
    zeros = [0.0] * 288
    discharge = [0.01] * 288
    series = {
        solar.APPLIANCE: ([0.003] * 288, None),
        solar.IMPORT: ([0.01] * 288, None),
        solar.EXPORT: (zeros, None),
        solar.SOLAR: (zeros, None),
        solar.CHARGE: (zeros, None),
        solar.DISCHARGE: (discharge, None),
    }
    day = solar.solar_aware_day(general, feed, YESTERDAY, series, allow_fallback=False)
    assert day.battery_kwh == pytest.approx(0.003 * 288 / 2)
    assert day.grid_kwh == pytest.approx(0.003 * 288 / 2)
    billed = math.fsum(0.003 * r.per_kwh / 100 for r in general)
    assert math.fsum(day.amounts) == pytest.approx(billed)
    series[solar.IMPORT] = (zeros, None)
    series[solar.DISCHARGE] = (zeros, None)
    day = solar.solar_aware_day(general, feed, YESTERDAY, series, allow_fallback=False)
    assert day.grid_kwh == pytest.approx(0.003 * 288)
    assert math.fsum(day.amounts) == pytest.approx(billed)


def test_solar_aware_day_hourly_fallback_and_missing_data() -> None:
    """Without 5-minute data: hourly shares and the hour's mean prices, flagged; refused
    when the fallback is off; a series without even hourly data names its sensor."""
    general, feed = _records(YESTERDAY)
    flows = _flows(YESTERDAY)
    series = _series(flows)
    series[solar.SOLAR] = (None, series[solar.SOLAR][1])
    day = solar.solar_aware_day(general, feed, YESTERDAY, series, allow_fallback=True)
    assert day.lower_precision is True
    precise = solar.solar_aware_day(general, feed, YESTERDAY, _series(flows), allow_fallback=False)
    assert math.fsum(day.amounts) == pytest.approx(math.fsum(precise.amounts), rel=0.02)
    with pytest.raises(PreciseDataUnavailable):
        solar.solar_aware_day(general, feed, YESTERDAY, series, allow_fallback=False)
    series[solar.EXPORT] = (None, None)
    with pytest.raises(SensorDataUnavailable, match=EXPORT):
        solar.solar_aware_day(
            general,
            feed,
            YESTERDAY,
            series,
            allow_fallback=True,
            names={solar.EXPORT: EXPORT},
        )


def test_solar_aware_day_thirty_minute_records() -> None:
    """30-minute records (older sites) price each of their six 5-minute buckets."""
    records = [_parse_usage(r) for r in make_usage_day(YESTERDAY, duration=30)]
    general = [r for r in records if r.channel_identifier == "E1"]
    feed = [r for r in records if r.channel_identifier == "B1"]
    flows = _flows(YESTERDAY)
    day = solar.solar_aware_day(general, feed, YESTERDAY, _series(flows), allow_fallback=False)
    expected = 0.0
    for i in range(288):
        share = min(flows["solar"][i], flows["load"][i]) / flows["load"][i]
        g, f = general[i // 6], feed[i // 6]
        expected += flows["appliance"][i] * (share * -f.per_kwh + (1 - share) * g.per_kwh) / 100
    assert math.fsum(day.amounts) == pytest.approx(expected)


def test_refund_fractions_and_allowance_extra() -> None:
    """The share of a further window export still within the allowance, hour by hour;
    the extra feed-in value only in the penalty period."""
    settings = export.AllowanceSettings(2.0, export.TOTALLING_DAILY, "solarSponge", "peak", None)
    hours = [0.0] * 24
    hours[10], hours[11], hours[12] = 1.5, 1.0, 1.0
    days = {YESTERDAY.isoformat(): {"window_kwh_hours": hours, "penalty_rate": 1.86}}
    fractions = export.refund_fractions(settings, days)
    start = importer.nem_day_start(YESTERDAY)
    key = lambda h: round((start + h * HOUR).timestamp())  # noqa: E731
    assert fractions[key(9)] == 1.0  # nothing exported yet
    assert fractions[key(10)] == 1.0
    assert fractions[key(11)] == pytest.approx(0.5)  # 0.5 of 1.0 left
    assert fractions[key(12)] == 0.0
    general, _ = _records(YESTERDAY)
    record = general[11 * 12]
    extra = solar.allowance_extra("offPeak", 1.86, fractions)  # synthetic records: offPeak
    assert extra(record) == pytest.approx(0.93)
    assert solar.allowance_extra("solarSponge", 1.86, fractions)(record) == 0.0
    assert solar.allowance_extra("offPeak", None, fractions)(record) == 0.0


# --- on a site ----------------------------------------------------------------------------


async def test_solar_aware_statistic_and_sensor(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """An appliance with solar-aware cost gets a second statistic next to its normal
    cost, matching the independent sum, and a "Yesterday solar-aware cost" sensor."""
    days = [TODAY - timedelta(days=d) for d in (2, 1)]
    entry, flows = await _site(hass, aioclient_mock, hass_storage, days)
    manager = entry.runtime_data.manager
    assert manager.flows == solar.SiteFlows("Whole house meter", IMPORT, EXPORT, SOLAR)
    result = await _run(hass)
    chain = result["chains"]["own:sub_pump:solar_aware"]
    assert chain["status"] == "caught_up"
    assert chain["written"] == [d.isoformat() for d in days]
    assert result["chains"]["own:sub_pump"]["status"] == "caught_up"

    rows = await _rows(hass, [SOLAR_AWARE, NORMAL])
    expected = [h for d in days for h in _expected(d, flows[d])]
    assert len(rows[SOLAR_AWARE]) == 48
    for row, want in zip(rows[SOLAR_AWARE], expected, strict=True):
        assert row["state"] == pytest.approx(want, abs=1e-6)
    assert rows[SOLAR_AWARE][-1]["sum"] == pytest.approx(math.fsum(expected), abs=1e-5)
    assert rows[SOLAR_AWARE][-1]["sum"] < rows[NORMAL][-1]["sum"]

    state = hass.states.get(SENSOR)
    assert state is not None
    assert float(state.state) == pytest.approx(
        math.fsum(_expected(YESTERDAY, flows[YESTERDAY])), abs=0.006
    )
    attrs = state.attributes
    assert attrs["date"] == YESTERDAY.isoformat()
    assert attrs["energy_kwh"] == pytest.approx(math.fsum(flows[YESTERDAY]["appliance"]))
    assert attrs["solar_kwh"] + attrs["grid_kwh"] == pytest.approx(attrs["energy_kwh"], abs=1e-5)
    assert 0 < attrs["solar_share"] < 1
    assert attrs["battery_kwh"] == 0.0
    assert attrs["allowance_adjusted"] is None
    assert attrs["friendly_name"] == "Pool pump Yesterday solar-aware cost"
    registry = er.async_get(hass)
    normal_cost = registry.async_get("sensor.pool_pump_yesterday_cost")
    assert registry.async_get(SENSOR).device_id == normal_cost.device_id

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    site = next(iter(diagnostics["sites"].values()))
    assert site["flows"] == {
        "meter": "Whole house meter",
        "import": IMPORT,
        "export": EXPORT,
        "solar_energy": SOLAR,
        "solar_power": None,
        "battery_charge": None,
        "battery_discharge": None,
    }

    # Turned off: the sensor goes, the statistic and the chain's state stay.
    sub = entry.subentries["sub_pump"]
    hass.config_entries.async_update_subentry(entry, sub, data={**sub.data, "solar_aware": False})
    await hass.async_block_till_done()
    assert registry.async_get(SENSOR) is None
    assert [s.role for s in entry.runtime_data.manager.own_sensors if s.kind == "appliance"] == [
        None
    ]
    assert "own:sub_pump:solar_aware" in entry.runtime_data.manager.store.as_dict()["chains"]


async def test_solar_power_sensor_and_battery_sensors(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """A solar power sensor's mean statistics give the same result as an energy sensor;
    with battery sensors (none charging or discharging here) nothing changes."""
    days = [YESTERDAY]
    zero = {YESTERDAY: [0.0] * 288}
    await _import_sensor(hass, CHARGE, zero)
    await _import_sensor(hass, DISCHARGE, zero)
    entry, flows = await _site(
        hass,
        aioclient_mock,
        hass_storage,
        days,
        power=True,
        options={"battery_charge": CHARGE, "battery_discharge": DISCHARGE},
    )
    assert entry.runtime_data.manager.flows.battery
    await _run(hass)
    rows = (await _rows(hass, [SOLAR_AWARE]))[SOLAR_AWARE]
    for row, want in zip(rows, _expected(YESTERDAY, flows[YESTERDAY]), strict=True):
        assert row["state"] == pytest.approx(want, abs=1e-6)


async def test_solar_aware_waits_without_flows_and_skips_missing_data(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Without a grid meter the chain waits (nothing skipped); a day whose site sensors
    have no statistics is skipped with the sensor's name."""
    entry, _ = await _site(hass, aioclient_mock, hass_storage, [YESTERDAY], grid=False)
    assert entry.runtime_data.manager.flows is None
    result = await _run(hass)
    chain = result["chains"]["own:sub_pump:solar_aware"]
    assert chain["status"] == "waiting_for_data"
    assert "solar" in chain["reason"]
    assert chain["skipped"] == []


async def test_solar_aware_waiting_raises_an_issue(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Waiting more than 2 days for the site's flows raises a Repairs issue naming what is
    missing; 2 days don't; it clears once the cost is written again."""
    days = [TODAY - timedelta(days=d) for d in (3, 2, 1)]
    entry, _ = await _site(hass, aioclient_mock, hass_storage, days, grid=False)
    manager = entry.runtime_data.manager
    issue_id = f"solar_aware_waiting_{manager.site_key}_own_sub_pump_solar_aware"
    registry = ir.async_get(hass)
    assert manager.flows_missing == ["a grid meter with import and export"]
    with patch.object(solar_mod_manager, "SOLAR_AWARE_WAIT_DAYS", 3):
        await _run(hass)
    assert registry.async_get_issue(DOMAIN, issue_id) is None  # 3 days: not yet
    await _run(hass)
    issue = registry.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_placeholders["name"] == "Pool pump"
    assert issue.translation_placeholders["missing"] == "a grid meter with import and export"
    assert issue.translation_placeholders["days"] == "3"
    assert issue.translation_placeholders["first_day"] == days[0].isoformat()

    hass.config_entries.async_add_subentry(entry, _subentry(_grid_meter()))
    await hass.async_block_till_done()
    result = await _run(hass)
    assert result["chains"]["own:sub_pump:solar_aware"]["written"] == [d.isoformat() for d in days]
    assert registry.async_get_issue(DOMAIN, issue_id) is None


def test_flows_missing() -> None:
    """Both missing pieces are named."""
    entry = SimpleNamespace(unique_id="S", subentries={})
    site = {"site_id": "S", "channels": [{"identifier": "E1", "type": "general"}]}
    sources = solar.Sources()
    assert solar.flows_missing(entry, site, {}, sources) == [
        "a solar sensor in the site's Sources",
        "a grid meter with import and export",
    ]


async def test_solar_aware_day_without_feed_in_records(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """A day without feed-in records (the channel not active yet) is skipped."""
    entry, _ = await _site(hass, aioclient_mock, hass_storage, [YESTERDAY])
    manager = entry.runtime_data.manager
    chain = next(c for c in manager._secondary_chains() if c.key.endswith(":solar_aware"))
    status = await manager._async_solar_write(
        chain, YESTERDAY, [], {"E1": []}, tail=False, first_day=False
    )
    assert status == "skipped_sensor_data"
    assert chain.state.day(YESTERDAY)["reason"] == "no feed-in records"


async def test_solar_aware_skips_a_day_without_site_statistics(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """The appliance has statistics for two days but the solar sensor only for the last:
    the first is skipped, naming the solar sensor."""
    days = [TODAY - timedelta(days=d) for d in (2, 1)]
    _preload_store(hass_storage, retention_days=2)
    flows = {d: _flows(d) for d in days}
    await _import_sensor(hass, APPLIANCE, {d: f["appliance"] for d, f in flows.items()})
    await _import_sensor(hass, IMPORT, {d: f["import"] for d, f in flows.items()})
    await _import_sensor(hass, EXPORT, {d: f["export"] for d, f in flows.items()})
    await _import_sensor(hass, SOLAR, {YESTERDAY: flows[YESTERDAY]["solar"]})
    await _setup_entry(
        hass,
        aioclient_mock,
        FakeAmber(TODAY - timedelta(days=30), YESTERDAY),
        options={"schedule_mode": "automatic", "solar_energy": SOLAR},
        subentries=[_grid_meter(), _appliance()],
    )
    result = await _run(hass)
    chain = result["chains"]["own:sub_pump:solar_aware"]
    assert chain["skipped"] == [days[0].isoformat()]
    assert chain["written"] == [YESTERDAY.isoformat()]


async def test_solar_aware_precision_fallback(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Without the appliance's 5-minute data the day is priced hourly and flagged; with the
    fallback off it is skipped."""
    await _site(hass, aioclient_mock, hass_storage, [YESTERDAY], short=False)
    result = await _run(hass)
    assert result["chains"]["own:sub_pump:solar_aware"]["lower_precision"] == [
        YESTERDAY.isoformat()
    ]
    assert hass.states.get(SENSOR).attributes["lower_precision"] is True


async def test_solar_aware_precision_skipped_without_fallback(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    entry, _ = await _site(
        hass,
        aioclient_mock,
        hass_storage,
        [YESTERDAY],
        short=False,
        options={"own_fallback": False},
    )
    result = await _run(hass)
    chain = result["chains"]["own:sub_pump:solar_aware"]
    assert chain["skipped"] == [YESTERDAY.isoformat()]
    record = entry.runtime_data.manager.store.as_dict()["chains"]["own:sub_pump:solar_aware"]
    assert record["days"][YESTERDAY.isoformat()]["status"] == "skipped_no_short_term"


async def test_solar_aware_with_export_allowance(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """With the export allowance, the feed-in value in the penalty period gets back the
    day's measured penalty in proportion to the hour's window export still within the
    allowance (2.5 kWh: hours 10 and 11 export 1 kWh each, hour 12 one more, half
    within it)."""
    hours = [0.0] * 24
    hours[10] = hours[11] = hours[12] = 1.0
    records = [_parse_usage(r) for r in make_usage_day(YESTERDAY)]
    measured = {
        "window_kwh": 3.0,
        "window_kwh_hours": hours,
        "penalty_rate": 1.86,
        "reward_kwh": 0.0,
        "reward_rate": None,
        "loss_factor": 0.974,
        "penalty": 3.0 * 0.0186,
        "reward": 0.0,
        "measured": True,
        "fingerprint": importer.revision_fingerprint(records),
    }
    entry, flows = await _site(
        hass,
        aioclient_mock,
        hass_storage,
        [YESTERDAY],
        options={
            "billing_day": 20,
            "daily_supply": 1.0,
            "amber_subscription": 0.0,
            "export_allowance": True,
            "allowance_kwh_per_day": 2.5,
            "allowance_totalling": "daily",
            "penalty_period": "offPeak",  # every synthetic general record
        },
        store={"export_days": {YESTERDAY.isoformat(): measured}},
    )
    manager = entry.runtime_data.manager
    assert manager.allowance_active
    result = await _run(hass)
    assert result["export"]["measured"] == []  # already measured, unchanged

    def extra(i: int) -> float:
        hour = i // 12
        return 1.86 * (1.0 if hour < 12 else 0.5 if hour == 12 else 0.0)

    rows = (await _rows(hass, [SOLAR_AWARE]))[SOLAR_AWARE]
    expected = _expected(YESTERDAY, flows[YESTERDAY], extra)
    for row, want in zip(rows, expected, strict=True):
        assert row["state"] == pytest.approx(want, abs=1e-6)
    assert math.fsum(expected) > math.fsum(_expected(YESTERDAY, flows[YESTERDAY]))
    assert hass.states.get(SENSOR).attributes["allowance_adjusted"] is True
    # A day without aggregates is valued unadjusted and says so.
    manager.store.export_days.pop(YESTERDAY.isoformat())
    extra_fn, adjusted = manager._allowance_extra(YESTERDAY)
    assert adjusted is False
    assert extra_fn(records[0]) == 0.0


# --- the appliance flow -------------------------------------------------------------------


async def test_appliance_flow_solar_aware(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """The option needs a general channel, the site's solar sensor and a grid meter with
    import and export; the reconfigure step turns it on and off."""
    _preload_store(hass_storage, retention_days=1)
    await _import_sensor(hass, APPLIANCE, {YESTERDAY: [0.0] * 288})
    await _import_sensor(hass, IMPORT, {YESTERDAY: [0.0] * 288})
    await _import_sensor(hass, EXPORT, {YESTERDAY: [0.0] * 288})
    channels = [
        {"identifier": "E1", "type": "general", "tariff": "EA116"},
        {"identifier": "E2", "type": "controlledLoad", "tariff": "EA030"},
        {"identifier": "B1", "type": "feedIn", "tariff": None},
    ]
    entry = await _setup_entry(
        hass,
        aioclient_mock,
        FakeAmber(TODAY - timedelta(days=30), YESTERDAY),
        data={"channels": channels},
    )

    async def add(user_input: dict) -> dict:
        flow = await hass.config_entries.subentries.async_init(
            (entry.entry_id, "appliance"), context={"source": "user"}
        )
        assert "solar_aware" in flow["data_schema"].schema
        return await hass.config_entries.subentries.async_configure(flow["flow_id"], user_input)

    base = {"name": "Pool pump", "entity_id": APPLIANCE, "solar_aware": True}
    result = await add({**base, "channel": "E2"})
    assert result["errors"] == {"solar_aware": "solar_aware_channel"}
    result = await add({**base, "channel": "E1"})
    assert result["errors"] == {"solar_aware": "solar_aware_no_solar"}
    site = next(s for s in entry.subentries.values() if s.subentry_type == "site")
    hass.config_entries.async_update_subentry(
        entry,
        site,
        data={**site.data, "options": {**site.data["options"], "solar_energy": SOLAR}},
    )
    await hass.async_block_till_done()
    result = await add({**base, "channel": "E1"})
    assert result["errors"] == {"solar_aware": "solar_aware_no_grid_meter"}
    hass.config_entries.async_add_subentry(
        entry,
        _subentry(_grid_meter()),
    )
    await hass.async_block_till_done()
    result = await add({**base, "channel": "E1"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["solar_aware"] is True
    await hass.async_block_till_done()
    manager = entry.runtime_data.manager
    assert [s.role for s in manager.own_sensors if s.kind == "appliance"] == [None, "solar_aware"]
    assert hass.states.get(SENSOR) is not None
    sub_id = next(s.subentry_id for s in manager.own_sensors if s.kind == "appliance")
    await _check_reconfigure(hass, entry, sub_id)


async def _check_reconfigure(hass: HomeAssistant, entry, sub_id: str) -> None:
    """Reconfigure: off, then on again; with the site's solar sensor removed, an error;
    an appliance whose channel is gone can't be reconfigured."""

    async def reconfigure(value: bool) -> dict:
        flow = await hass.config_entries.subentries.async_init(
            (entry.entry_id, "appliance"),
            context={"source": "reconfigure", "subentry_id": sub_id},
        )
        assert flow["step_id"] == "reconfigure"
        assert flow["description_placeholders"] == {"name": "Pool pump"}
        return await hass.config_entries.subentries.async_configure(
            flow["flow_id"], {"solar_aware": value}
        )

    result = await reconfigure(False)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert entry.subentries[sub_id].data["solar_aware"] is False
    assert hass.states.get(SENSOR) is None
    result = await reconfigure(True)
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert hass.states.get(SENSOR) is not None
    site = next(s for s in entry.subentries.values() if s.subentry_type == "site")
    options = {k: v for k, v in site.data["options"].items() if k != "solar_energy"}
    hass.config_entries.async_update_subentry(entry, site, data={**site.data, "options": options})
    await hass.async_block_till_done()
    result = await reconfigure(True)
    assert result["errors"] == {"solar_aware": "solar_aware_no_solar"}
    sub = entry.subentries[sub_id]
    hass.config_entries.async_update_subentry(entry, sub, data={**sub.data, "channel": "X9"})
    await hass.async_block_till_done()
    flow = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "appliance"), context={"source": "reconfigure", "subentry_id": sub_id}
    )
    assert flow["reason"] == "appliance_site_missing"


def _subentry(spec: dict[str, Any]):
    from homeassistant.config_entries import ConfigSubentry  # noqa: PLC0415

    return ConfigSubentry(
        data=spec["data"],
        subentry_type=spec["subentry_type"],
        title=spec["title"],
        unique_id=spec["unique_id"],
    )


def test_flows_meter_prefers_a_meter_with_export() -> None:
    """Today so far's meter without an export: the first general meter with one."""

    def meter(sub_id: str, export: bool) -> SimpleNamespace:
        data = {"site_id": "S", "import_entity_id": f"sensor.{sub_id}", "import_channel": "E1"}
        if export:
            data["export_entity_id"] = "sensor.x"
        return SimpleNamespace(subentry_id=sub_id, subentry_type="grid_meter", data=data)

    site = {"site_id": "S", "channels": [{"identifier": "E1", "type": "general"}]}
    entry = SimpleNamespace(
        unique_id="S", subentries={"a": meter("a", False), "b": meter("b", True)}
    )
    assert solar.flows_meter(entry, site, {}).subentry_id == "b"
    entry.subentries = {"a": meter("a", False)}
    assert solar.flows_meter(entry, site, {}) is None
