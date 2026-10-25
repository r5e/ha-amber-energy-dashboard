"""Milestone 19 (2.2.0-dev3): site-wide sources (live prices detected from Amber Express
or Amber Electric, solar and battery sensors) and Today so far (DESIGN section 22). All
data is synthetic."""

from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.amber_energy_dashboard import sources, today as today_mod
from custom_components.amber_energy_dashboard.const import DOMAIN, SUBENTRY_GRID_METER
from custom_components.amber_energy_dashboard.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.amber_energy_dashboard.today import Side, TodayTracker, compare

from .fake_amber import FakeAmber
from .synthetic import SITE_ID
from .test_manager import (  # noqa: F401 - fixtures are used by name
    NOW,
    PREFIX,
    SITE_KEY,
    TODAY,
    YESTERDAY,
    Clock,
    _fast_verify,
    _no_real_timers,
    _preload_store,
    _setup,
    _setup_entry,
    _site_options,
    clock,
)

GENERAL = "sensor.amber_express_home_general_price"
FEED_IN = "sensor.amber_express_home_feed_in_price"
CORE_GENERAL = "sensor.home_general_price"
CORE_FEED_IN = "sensor.home_feed_in_price"
IMPORT = "sensor.grid_import"
EXPORT = "sensor.grid_export"
COST = "sensor.amber_energy_dashboard_today_cost_estimated"
CREDIT = "sensor.amber_energy_dashboard_today_feed_in_estimated"
BILL = "sensor.amber_energy_dashboard_bill_to_date"
TODAY_STORE = f"{DOMAIN}.{SITE_KEY}_today"
E1_COST, E1_ENERGY = f"{PREFIX}_e1_cost", f"{PREFIX}_e1_energy"
B1_CREDIT, B1_ENERGY = f"{PREFIX}_b1_compensation", f"{PREFIX}_b1_energy"
FIVE = timedelta(minutes=5)
CL_CHANNELS = [
    {"identifier": "E1", "type": "general", "tariff": "EA116"},
    {"identifier": "E2", "type": "controlledLoad", "tariff": "EA030"},
    {"identifier": "B1", "type": "feedIn", "tariff": None},
]


def _energy(hass: HomeAssistant, entity_id: str, value: Any, unit: str = "kWh") -> None:
    hass.states.async_set(
        entity_id,
        str(value),
        {"device_class": "energy", "state_class": "total_increasing", "unit_of_measurement": unit},
    )


def _price(hass: HomeAssistant, entity_id: str, value: Any, unit: str = "$/kWh") -> None:
    hass.states.async_set(entity_id, str(value), {"unit_of_measurement": unit})


def _register(hass: HomeAssistant, platform: str, unique_id: str, entity_id: str, **kw) -> None:
    er.async_get(hass).async_get_or_create(
        "sensor", platform, unique_id, suggested_object_id=entity_id.split(".")[1], **kw
    )


def _amber_express(hass: HomeAssistant, site: str = SITE_ID, *, feed_in: bool = True) -> None:
    _register(hass, "amber_express", f"{site}_general_price", GENERAL)
    if feed_in:
        _register(hass, "amber_express", f"{site}_feed_in_price", FEED_IN)


def _grid_meter(export: bool = True, sub_id: str = "sub_grid", sensor: str = IMPORT) -> dict:
    data = {"name": "Whole house meter", "import_entity_id": sensor, "import_channel": "E1"}
    if export:
        data |= {"export_entity_id": EXPORT, "export_channel": "B1"}
    return {
        "data": data,
        "subentry_id": sub_id,
        "subentry_type": SUBENTRY_GRID_METER,
        "title": "Whole house meter · grid meter",
        "unique_id": f"grid_meter:{sensor}",
    }


def _billed(cost: float, kwh: float, credit: float, export_kwh: float) -> dict:
    return {
        "status": "imported",
        "totals": {E1_COST: cost, E1_ENERGY: kwh, B1_CREDIT: credit, B1_ENERGY: export_kwh},
    }


async def _entry(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict,
    *,
    options: dict | None = None,
    subentries: list[dict] | None = None,
    data: dict | None = None,
):
    """A site with a grid meter, Amber Express's prices, and two billed days."""
    _preload_store(
        hass_storage,
        retention_days=2,
        marker=YESTERDAY.isoformat(),
        last_written=YESTERDAY.isoformat(),
        days={
            (TODAY - timedelta(days=2)).isoformat(): _billed(3.0, 12.0, 0.4, 4.0),
            YESTERDAY.isoformat(): _billed(2.0, 10.0, 0.5, 5.0),
        },
    )
    _price(hass, GENERAL, 0.30)
    _price(hass, FEED_IN, 0.05)
    _energy(hass, IMPORT, 100.0)
    _energy(hass, EXPORT, 50.0)
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    return await _setup_entry(
        hass,
        aioclient_mock,
        fake,
        options={"schedule_mode": "automatic", **(options or {})},
        subentries=subentries if subentries is not None else [_grid_meter()],
        data=data,
    )


async def _tick(hass: HomeAssistant, freezer: FrozenDateTimeFactory, tracker, at: datetime) -> None:
    freezer.move_to(at)
    tracker.async_tick(at)
    await hass.async_block_till_done()


# --- prices and detection -----------------------------------------------------------------


def test_price_units() -> None:
    """$/kWh as is; c/kWh and $/MWh converted; anything else isn't a price."""
    assert sources.price_factor("$/kWh") == 1.0
    assert sources.price_factor("AUD/kWh") == 1.0
    assert sources.price_factor("c/kWh") == 0.01
    assert sources.price_factor("¢ / kWh") == 0.01
    assert sources.price_factor("$/MWh") == 0.001
    assert sources.price_factor("kWh") is None
    assert sources.price_factor(None) is None


async def test_price_from_state(hass: HomeAssistant) -> None:
    """A price state in $/kWh, or None when it can't be used."""
    assert sources.price_from_state(None) is None
    for value, unit, expected in (
        ("30", "c/kWh", 0.3),
        ("0.25", "$/kWh", 0.25),
        ("unavailable", "$/kWh", None),
        ("unknown", "$/kWh", None),
        ("high", "$/kWh", None),
        ("0.3", "W", None),
    ):
        _price(hass, "sensor.p", value, unit)
        result = sources.price_from_state(hass.states.get("sensor.p"))
        assert result == (pytest.approx(expected) if expected is not None else None)


async def test_detect_prices(hass: HomeAssistant) -> None:
    """Amber Express first, then the core integration, matched by this site's ID (in any
    case); another site's sensors and disabled ones are left out."""
    assert sources.detect_prices(hass, SITE_ID) == {}
    _register(hass, "amberelectric", f"{SITE_ID.lower()}-current-general", CORE_GENERAL)
    _register(hass, "amberelectric", f"{SITE_ID}-current-feed_in", CORE_FEED_IN)
    _register(hass, "amberelectric", "01OTHERSITE-current-general", "sensor.other_price")
    found = sources.detect_prices(hass, SITE_ID)
    assert found == {
        "general": sources.PriceSource(CORE_GENERAL, "amberelectric"),
        "feedIn": sources.PriceSource(CORE_FEED_IN, "amberelectric"),
    }
    _amber_express(hass, feed_in=False)
    _register(
        hass,
        "amber_express",
        f"{SITE_ID}_feed_in_price",
        FEED_IN,
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    found = sources.detect_prices(hass, SITE_ID)
    assert found["general"] == sources.PriceSource(GENERAL, "amber_express")
    assert found["feedIn"] == sources.PriceSource(CORE_FEED_IN, "amberelectric")


async def test_resolve(hass: HomeAssistant) -> None:
    """A price set in the options wins; set to none it stays off; unset it is detected.
    A channel the site doesn't have gets no price."""
    _amber_express(hass)
    both = {"general", "feedIn"}
    found = sources.resolve(hass, SITE_ID, {}, both)
    assert found.prices == {
        "general": sources.PriceSource(GENERAL, "amber_express"),
        "feedIn": sources.PriceSource(FEED_IN, "amber_express"),
    }
    options = {
        "live_price_general": "sensor.mine",
        "live_price_feed_in": None,
        "solar_power": "sensor.pv_power",
        "battery_charge": "sensor.bat_in",
    }
    found = sources.resolve(hass, SITE_ID, options, both)
    assert found.prices == {"general": sources.PriceSource("sensor.mine", "configured")}
    assert found.as_dict() == {
        "prices": {"general": {"entity_id": "sensor.mine", "origin": "configured"}},
        "solar_power": "sensor.pv_power",
        "solar_energy": None,
        "battery_charge": "sensor.bat_in",
        "battery_discharge": None,
    }
    assert list(sources.resolve(hass, SITE_ID, {}, {"general"}).prices) == ["general"]


# --- the tracker --------------------------------------------------------------------------


def test_interval_end_and_nem_date() -> None:
    """Boundaries every 5 minutes (UTC); NEM days are UTC+10."""
    at = datetime(2026, 9, 26, 3, 7, 41, tzinfo=UTC)
    assert today_mod.interval_end(at) == datetime(2026, 9, 26, 3, 5, tzinfo=UTC)
    assert today_mod.nem_date(datetime(2026, 9, 25, 14, 0, tzinfo=UTC)) == date(2026, 9, 26)
    assert today_mod.nem_date(datetime(2026, 9, 25, 13, 59, tzinfo=UTC)) == date(2026, 9, 25)


def _tracker(hass: HomeAssistant, export: bool = True, calls: list | None = None):
    sides = [Side("import", IMPORT, GENERAL, "E1")]
    if export:
        sides.append(Side("export", EXPORT, FEED_IN, "B1"))
    return TodayTracker(
        hass,
        SITE_KEY,
        sides,
        "Whole house meter",
        lambda: calls.append(1) if calls is not None else None,
    )


async def test_tracker_prices_each_interval(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Each interval's energy is priced at the price in effect at its end (the last price
    before the boundary, not one that arrives just after it); units are converted."""
    freezer.move_to(NOW)
    _price(hass, GENERAL, 30, "c/kWh")
    _price(hass, FEED_IN, 0.05)
    _energy(hass, IMPORT, 100_000, "Wh")
    _energy(hass, EXPORT, 50.0)
    calls: list = []
    tracker = _tracker(hass, calls=calls)
    await tracker.async_load()
    tracker.async_start()
    await _tick(hass, freezer, tracker, NOW)  # the first readings
    assert tracker.summary(NOW)["import"]["intervals"] == 0
    freezer.move_to(NOW + timedelta(minutes=4))
    _price(hass, GENERAL, 40, "c/kWh")
    _energy(hass, IMPORT, 100_500, "Wh")
    _energy(hass, EXPORT, 50.2)
    freezer.move_to(NOW + FIVE + timedelta(seconds=2))
    _price(hass, GENERAL, 99, "c/kWh")  # the next interval's price
    await hass.async_block_till_done()
    await _tick(hass, freezer, tracker, NOW + FIVE + timedelta(seconds=3))
    summary = tracker.summary(NOW + FIVE)
    assert summary["import"]["amount"] == pytest.approx(0.5 * 0.40)
    assert summary["import"]["kwh"] == pytest.approx(0.5)
    assert summary["export"]["amount"] == pytest.approx(0.2 * 0.05)
    assert summary["last_interval_end"] == (NOW + FIVE).isoformat()
    assert summary["expected_intervals"] == 121  # 10:05 NEM
    assert summary["meter"] == "Whole house meter"
    assert summary["import"]["price_entity"] == GENERAL
    assert len(calls) == 2

    # The next interval: the 99 c price, which arrived just after the last boundary.
    _energy(hass, IMPORT, 101_500, "Wh")
    await _tick(hass, freezer, tracker, NOW + 2 * FIVE)
    assert tracker.summary(NOW + 2 * FIVE)["import"]["amount"] == pytest.approx(0.2 + 0.99)

    # No usable price: counted as unpriced energy. A sensor without a reading adds
    # nothing until it reports again; a reading going down (a reset) drops the interval.
    _price(hass, GENERAL, "unavailable")
    _energy(hass, IMPORT, 102_000, "Wh")
    _energy(hass, EXPORT, "unavailable")
    await _tick(hass, freezer, tracker, NOW + 3 * FIVE)
    summary = tracker.summary(NOW + 3 * FIVE)
    assert summary["import"]["unpriced_kwh"] == pytest.approx(0.5)
    assert summary["import"]["amount"] == pytest.approx(1.19)
    assert summary["export"]["intervals"] == 2  # an interval without energy counts
    _energy(hass, IMPORT, 5, "Wh")
    _energy(hass, EXPORT, 50.4)
    _price(hass, GENERAL, 0.3)
    await _tick(hass, freezer, tracker, NOW + 4 * FIVE)
    summary = tracker.summary(NOW + 4 * FIVE)
    assert summary["import"]["kwh"] == pytest.approx(2.0)  # the reset interval is dropped
    assert summary["export"]["kwh"] == pytest.approx(0.4)
    assert summary["export"]["intervals"] == 3
    tracker.async_stop()
    assert tracker._unsubs == []


async def test_tracker_timer_and_odd_states(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The 5-minute timer ticks the tracker; a reading that isn't a number, or is in a
    unit that isn't energy, adds nothing; a price sensor that is removed is ignored."""
    freezer.move_to(NOW)
    _price(hass, GENERAL, 0.3)
    _energy(hass, IMPORT, 1.0)
    _energy(hass, EXPORT, 1.0)
    tracker = _tracker(hass)
    await tracker.async_load()
    with patch.object(today_mod, "async_track_utc_time_change") as timer:
        tracker.async_start()
    tick = timer.call_args.args[1]
    assert timer.call_args.kwargs == {"minute": range(0, 60, 5), "second": 0}
    tick(NOW)
    _energy(hass, IMPORT, "not a number")
    hass.states.async_set(EXPORT, "2", {"unit_of_measurement": "parsecs"})
    hass.states.async_remove(GENERAL)
    await hass.async_block_till_done()
    tick(NOW + FIVE)
    summary = tracker.summary(NOW + FIVE)
    assert summary["import"]["kwh"] == summary["export"]["kwh"] == 0.0
    assert summary["last_interval_end"] == (NOW + FIVE).isoformat()
    tracker.async_stop()


def test_today_sensor_without_its_side() -> None:
    """A side the snapshot doesn't have shows no value and no attributes."""
    from custom_components.amber_energy_dashboard.sensor import TODAY_SENSORS  # noqa: PLC0415

    description = TODAY_SENSORS["export"]
    assert description.value_fn({"today": {"import": {}}}) is None
    assert description.attrs_fn({"today": None}) is None


async def test_tracker_without_price_samples(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A price sensor that doesn't exist yet prices nothing; its first state is used
    for the interval it arrives in."""
    freezer.move_to(NOW)
    _energy(hass, IMPORT, 100.0)
    tracker = _tracker(hass, export=False)
    await tracker.async_load()
    tracker.async_start()
    await _tick(hass, freezer, tracker, NOW)
    _energy(hass, IMPORT, 101.0)
    await _tick(hass, freezer, tracker, NOW + FIVE)
    assert tracker.summary(NOW + FIVE)["import"]["unpriced_kwh"] == pytest.approx(1.0)
    freezer.move_to(NOW + 2 * FIVE + timedelta(seconds=1))
    _price(hass, GENERAL, 0.2)
    _energy(hass, IMPORT, 102.0)
    await _tick(hass, freezer, tracker, NOW + 2 * FIVE + timedelta(seconds=2))
    assert tracker.summary(NOW + 2 * FIVE)["import"]["amount"] == pytest.approx(0.2)
    assert "export" not in tracker.summary(NOW)
    tracker.async_stop()


async def test_tracker_days_history_and_restart(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, hass_storage: dict
) -> None:
    """At NEM midnight the day's estimate goes into the history (14 days kept) and the
    readings carry over; the state is saved and restored after a restart, unless the
    sensor changed. After downtime across midnight the readings start afresh."""
    midnight = datetime(2026, 9, 26, 14, 0, tzinfo=UTC)  # 00:00 NEM on the 27th
    hass_storage[TODAY_STORE] = {
        "version": 1,
        "key": TODAY_STORE,
        "data": {
            "day": None,
            "history": {
                (TODAY - timedelta(days=n)).isoformat(): {"import": {"amount": 1.0}}
                for n in range(1, 15)
            },
        },
    }
    freezer.move_to(midnight - FIVE)
    _price(hass, GENERAL, 0.3)
    _energy(hass, IMPORT, 10.0)
    tracker = _tracker(hass, export=False)
    await tracker.async_load()
    tracker.async_start()
    await _tick(hass, freezer, tracker, midnight - FIVE)  # 23:50 to 23:55: readings only
    _energy(hass, IMPORT, 11.0)
    await _tick(hass, freezer, tracker, midnight)  # 23:55 to 00:00 belongs to the 26th
    assert tracker.summary(midnight - FIVE)["import"]["kwh"] == pytest.approx(1.0)
    assert tracker.summary(midnight)["import"]["kwh"] == 0.0  # the 27th has nothing yet
    _energy(hass, IMPORT, 12.0)
    await _tick(hass, freezer, tracker, midnight + FIVE)
    assert tracker.summary(midnight + FIVE)["import"]["kwh"] == pytest.approx(1.0)
    assert sorted(tracker.history)[-1] == TODAY.isoformat()
    assert tracker.history[TODAY.isoformat()]["import"]["kwh"] == pytest.approx(1.0)
    assert len(tracker.history) == 14
    assert (TODAY - timedelta(days=14)).isoformat() not in tracker.history

    # Saved within 30 s; a restart the same day keeps the reading.
    freezer.tick(timedelta(seconds=31))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    saved = hass_storage[TODAY_STORE]["data"]
    assert saved["sides"]["import"]["reading"] == 12.0
    tracker.async_stop()
    restarted = _tracker(hass, export=False)
    await restarted.async_load()
    _energy(hass, IMPORT, 13.0)
    await _tick(hass, freezer, restarted, midnight + 3 * FIVE)
    assert restarted.summary(midnight + 3 * FIVE)["import"]["kwh"] == pytest.approx(2.0)

    # Another sensor: the day's totals stay, the readings don't carry over.
    other = TodayTracker(hass, SITE_KEY, [Side("import", EXPORT, GENERAL, "E1")], "M", list)
    await restarted._store.async_save(restarted._data)
    await other.async_load()
    assert other.summary(midnight + 3 * FIVE)["import"]["kwh"] == pytest.approx(2.0)
    assert other._data["sides"]["import"]["reading"] is None

    # Down across midnight: the readings start afresh on the new day.
    later = midnight + timedelta(days=1, minutes=30)
    _energy(hass, IMPORT, 20.0)
    await _tick(hass, freezer, restarted, later)
    assert restarted.summary(later)["import"]["kwh"] == 0.0
    assert restarted._data["sides"]["import"]["reading"] == 20.0
    await restarted.async_remove()
    assert TODAY_STORE not in hass_storage


def test_compare() -> None:
    """The latest estimated day before today with billed data, against the bill."""
    day = YESTERDAY.isoformat()
    history = {
        day: {"import": {"amount": 2.2, "kwh": 10.5, "unpriced_kwh": 0.0, "intervals": 288}},
        (YESTERDAY - timedelta(days=1)).isoformat(): {
            "import": {"amount": 1.0, "kwh": 5.0, "unpriced_kwh": 0.1, "intervals": 100}
        },
        TODAY.isoformat(): {"import": {"amount": 9.0}},
    }
    billed = {YESTERDAY: _billed(2.0, 10.0, 0.5, 5.0)}
    args = {"role": "import", "amount_sid": E1_COST, "energy_sid": E1_ENERGY, "before": TODAY}
    # Saved without an offset (as by 2.2.0b2): the estimate is the raw one.
    assert compare(history, billed=billed.get, **args) == {
        "date": day,
        "billed": 2.0,
        "billed_kwh": 10.0,
        "estimated_corrected": 2.2,
        "error_corrected": 0.2,
        "error_corrected_percent": 10.0,
        "estimated_corrected_kwh": 10.5,
        "estimated_raw": 2.2,
        "error_raw": 0.2,
        "error_raw_percent": 10.0,
        "estimated_raw_kwh": 10.5,
        "unpriced_kwh": 0.0,
        "intervals": 288,
        "offset_corrected_intervals": 0,
        "complete": True,
    }
    # Yesterday not billed yet: the day before (partly covered) is compared instead.
    earlier = {YESTERDAY - timedelta(days=1): _billed(0.0, 0.0, 0.0, 0.0)}
    result = compare(history, billed=earlier.get, **args)
    assert result["date"] == (YESTERDAY - timedelta(days=1)).isoformat()
    assert result["error_corrected_percent"] is None
    assert result["error_raw_percent"] is None
    assert result["complete"] is False
    assert compare(history, billed=lambda _: None, **args) is None
    assert compare(history, billed=billed.get, **{**args, "role": "export"}) is None
    assert compare(history, billed=billed.get, **{**args, "amount_sid": None}) is None
    no_kwh = {YESTERDAY: {"status": "imported", "totals": {E1_COST: 2.0}}}
    assert compare(history, billed=no_kwh.get, **args)["billed_kwh"] is None


# --- Today so far on a site ---------------------------------------------------------------


async def test_today_so_far_sensors(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    clock: Clock,
    hass_storage: dict,
    freezer: FrozenDateTimeFactory,
) -> None:
    """With a grid meter and Amber Express's prices (found by themselves), the site has
    Today's cost and feed-in so far (estimated): display only, on the site's device, with
    yesterday's estimate against the bill once both are there; the bill to date gets an
    "including today" attribute."""
    freezer.move_to(NOW)
    _amber_express(hass)
    entry = await _entry(
        hass,
        aioclient_mock,
        hass_storage,
        options={"billing_day": 20, "daily_supply": 1.0, "amber_subscription": 0.0},
    )
    manager = entry.runtime_data.manager
    tracker = manager.today
    assert set(tracker.sides) == {"import", "export"}
    cost, credit = hass.states.get(COST), hass.states.get(CREDIT)
    assert float(cost.state) == 0.0
    assert cost.attributes["unit_of_measurement"] == "AUD"
    assert cost.attributes["device_class"] == "monetary"
    assert "state_class" not in cost.attributes
    assert cost.attributes["friendly_name"].endswith("Today's cost so far (estimated)")
    assert credit.attributes["friendly_name"].endswith("Today's feed-in so far (estimated)")
    assert cost.attributes["yesterday_estimate_vs_billed"] is None
    registry = er.async_get(hass)
    assert registry.async_get(COST).device_id == entry.runtime_data.device_id

    await _tick(hass, freezer, tracker, NOW)
    _energy(hass, IMPORT, 100.5)
    _energy(hass, EXPORT, 50.2)
    await _tick(hass, freezer, tracker, NOW + FIVE)
    cost, credit = hass.states.get(COST), hass.states.get(CREDIT)
    assert float(cost.state) == pytest.approx(0.15)
    assert float(credit.state) == pytest.approx(0.01)
    assert cost.attributes["energy_kwh"] == pytest.approx(0.5)
    assert cost.attributes["intervals"] == 1
    assert cost.attributes["date"] == TODAY.isoformat()
    assert cost.attributes["energy_entity"] == IMPORT
    assert credit.attributes["price_entity"] == FEED_IN

    # Bill to date (24 and 25 Sep) plus today: cost - feed-in + a day of fixed charges.
    bill = hass.states.get(BILL)
    to_date = float(bill.state)
    assert to_date == pytest.approx(3.0 + 2.0 - 0.4 - 0.5 + 2 * 1.1)
    assert bill.attributes["including_today_estimated"] == pytest.approx(
        round(to_date + 0.15 - 0.01 + 1.1, 2)
    )
    assert bill.attributes["including_today_leaves_out"] == []

    # Yesterday's estimate, once in the history, against the billed day.
    tracker.history[YESTERDAY.isoformat()] = {
        "import": {"amount": 2.1, "kwh": 10.2, "unpriced_kwh": 0.0, "intervals": 288},
        "export": {"amount": 0.45, "kwh": 5.1, "unpriced_kwh": 0.0, "intervals": 288},
    }
    manager.publish()
    await hass.async_block_till_done()
    versus = hass.states.get(COST).attributes["yesterday_estimate_vs_billed"]
    assert (versus["date"], versus["estimated_corrected"], versus["billed"]) == (
        YESTERDAY.isoformat(),
        2.1,
        2.0,
    )
    assert versus["error_corrected"] == pytest.approx(0.1)
    versus = hass.states.get(CREDIT).attributes["yesterday_estimate_vs_billed"]
    assert (versus["billed"], versus["billed_kwh"]) == (0.5, 5.0)

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    site = next(iter(diagnostics["sites"].values()))
    assert site["sources"]["prices"]["general"] == {
        "entity_id": GENERAL,
        "origin": "amber_express",
    }
    assert site["today"]["import"]["kwh"] == pytest.approx(0.5)
    assert site["today"]["meter"] == "**REDACTED**"
    assert "Whole house meter" not in repr(diagnostics)

    # Never in long-term statistics: no state class, and the tracker stops on unload.
    await hass.config_entries.async_unload(entry.entry_id)
    assert tracker._unsubs == []


async def test_bill_including_today(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    clock: Clock,
    hass_storage: dict,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Including today needs yesterday imported (or today to be the cycle's first day);
    it lists what the estimate can't see: controlled load, and feed-in without an
    export."""
    freezer.move_to(NOW)
    _amber_express(hass)
    entry = await _entry(
        hass,
        aioclient_mock,
        hass_storage,
        options={"billing_day": 26, "daily_supply": 1.0, "amber_subscription": 0.0},
        subentries=[_grid_meter(export=False)],
        data={"channels": CL_CHANNELS},
    )
    manager = entry.runtime_data.manager
    assert set(manager.today.sides) == {"import"}
    assert hass.states.get(CREDIT) is None
    await _tick(hass, freezer, manager.today, NOW)
    _energy(hass, IMPORT, 101.0)
    await _tick(hass, freezer, manager.today, NOW + FIVE)
    bill = hass.states.get(BILL)
    # Today is the cycle's first day: nothing imported in it yet.
    assert bill.attributes["including_today_estimated"] == pytest.approx(0.3 + 1.1)
    assert bill.attributes["including_today_leaves_out"] == ["controlled load", "feed-in"]
    # Yesterday is missing (the 25th of a cycle from the 20th): no value.
    with patch.object(manager, "bill", replace_billing_day(manager.bill, 20)):
        result = manager._bill_including_today(
            {"data_through": (TODAY - timedelta(days=2)).isoformat(), "bill_to_date": 5.0},
            manager.today_snapshot(),
        )
        assert result["including_today_estimated"] is None
        result = manager._bill_including_today(
            {"data_through": None, "bill_to_date": None}, manager.today_snapshot()
        )
        assert result["including_today_estimated"] is None
        assert result["including_today_estimated_raw"] is None
    assert manager._bill_including_today({}, None) == {
        "including_today_estimated": None,
        "including_today_estimated_raw": None,
        "including_today_leaves_out": [],
    }


def replace_billing_day(bill, day: int):
    from dataclasses import replace  # noqa: PLC0415

    return replace(bill, billing_day=day)


async def test_no_today_without_a_price_or_a_meter(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    clock: Clock,
    hass_storage: dict,
) -> None:
    """No live price (none detected, or set to none), or no grid meter on the general
    channel: no Today so far, and its sensors are removed."""
    entry = await _entry(hass, aioclient_mock, hass_storage)
    assert entry.runtime_data.manager.today is None
    assert hass.states.get(COST) is None
    assert entry.runtime_data.manager.snapshot()["today"] is None

    _amber_express(hass)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(COST) is not None
    sub = next(s for s in entry.subentries.values() if s.subentry_type == "site")
    hass.config_entries.async_update_subentry(
        entry,
        sub,
        data={**sub.data, "options": {**_site_options(entry), "live_price_general": None}},
    )
    await hass.async_block_till_done()
    assert entry.runtime_data.manager.today is None
    assert er.async_get(hass).async_get(COST) is None
    assert er.async_get(hass).async_get(CREDIT) is None


async def test_today_on_a_controlled_load_meter_only(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    clock: Clock,
    hass_storage: dict,
) -> None:
    """A grid meter whose import is on the controlled load gives no Today so far."""
    _amber_express(hass)
    meter = _grid_meter(export=False)
    meter["data"]["import_channel"] = "E2"
    entry = await _entry(
        hass, aioclient_mock, hass_storage, subentries=[meter], data={"channels": CL_CHANNELS}
    )
    assert entry.runtime_data.manager.today is None


# --- the Sources step ---------------------------------------------------------------------


async def _sources_flow(hass: HomeAssistant, entry) -> dict:
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "sources"}
    )


def _suggested(flow: dict) -> dict[str, Any]:
    return {
        str(key): (key.description or {}).get("suggested_value")
        for key in flow["data_schema"].schema
    }


async def test_sources_step(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    clock: Clock,
    hass_storage: dict,
) -> None:
    """The step suggests the detected prices, checks the sensors, and saves: a price
    cleared is none (kept off), solar and battery sensors are stored; the site reloads."""
    _amber_express(hass)
    entry = await _entry(hass, aioclient_mock, hass_storage)
    hass.states.async_set("sensor.pv_power", "1200", {"device_class": "power"})
    _energy(hass, "sensor.pv_energy", 10)
    _energy(hass, "sensor.bat_in", 3)
    _energy(hass, "sensor.bat_out", 2)
    hass.states.async_set("sensor.temperature", "21", {"device_class": "temperature"})
    _price(hass, "sensor.price_mwh", 120, "$/MWh")

    flow = await _sources_flow(hass, entry)
    assert flow["step_id"] == "sources"
    assert _suggested(flow) == {
        "live_price_general": GENERAL,
        "live_price_feed_in": FEED_IN,
        "solar_power": None,
        "solar_energy": None,
        "battery_charge": None,
        "battery_discharge": None,
    }
    placeholders = flow["description_placeholders"]
    assert placeholders["detected"] == (
        f"- General: {GENERAL} (Amber Express)\n- Feed-in: {FEED_IN} (Amber Express)"
    )
    assert placeholders["meter"] == "Whole house meter · grid meter"

    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {
            "live_price_general": "sensor.temperature",
            "live_price_feed_in": "sensor.missing",
            "solar_power": "sensor.pv_energy",
            "solar_energy": "sensor.pv_power",
            "battery_charge": "sensor.bat_in",
            "battery_discharge": "sensor.bat_in",
        },
    )
    assert flow["errors"] == {
        "live_price_general": "price_unit",
        "live_price_feed_in": "sensor_not_found",
        "solar_power": "not_power",
        "solar_energy": "not_cumulative_energy",
        "battery_discharge": "same_sensor",
    }
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        flow = await hass.config_entries.options.async_configure(
            flow["flow_id"],
            {
                "live_price_general": "sensor.price_mwh",
                "solar_power": "sensor.pv_power",
                "solar_energy": "sensor.pv_energy",
                "battery_charge": "sensor.bat_in",
                "battery_discharge": "sensor.bat_out",
            },
        )
        await hass.async_block_till_done()
    assert flow["type"] is FlowResultType.CREATE_ENTRY
    reload.assert_called_once_with(entry.entry_id)
    options = _site_options(entry)
    assert {k: options[k] for k in sources.SOURCE_KEYS if k in options} == {
        "live_price_general": "sensor.price_mwh",
        "live_price_feed_in": None,
        "solar_power": "sensor.pv_power",
        "solar_energy": "sensor.pv_energy",
        "battery_charge": "sensor.bat_in",
        "battery_discharge": "sensor.bat_out",
    }
    # Import settings keep the sources; the step shows what was saved.
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "settings"}
    )
    await hass.config_entries.options.async_configure(
        flow["flow_id"], {"schedule_mode": "automatic", "patience_days": 5}
    )
    await hass.async_block_till_done()
    assert _site_options(entry)["live_price_feed_in"] is None
    assert _site_options(entry)["solar_power"] == "sensor.pv_power"
    flow = await _sources_flow(hass, entry)
    assert _suggested(flow)["live_price_general"] == "sensor.price_mwh"
    assert _suggested(flow)["live_price_feed_in"] is None
    assert _suggested(flow)["battery_discharge"] == "sensor.bat_out"


async def test_sources_step_without_feed_in_or_meter(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    clock: Clock,
    hass_storage: dict,
) -> None:
    """A site without a feed-in channel has no feed-in price field; without any price
    integration nothing is found; without a grid meter the step says so."""
    entry = await _entry(
        hass,
        aioclient_mock,
        hass_storage,
        subentries=[],
        data={"channels": [{"identifier": "E1", "type": "general", "tariff": "EA116"}]},
    )
    flow = await _sources_flow(hass, entry)
    assert "live_price_feed_in" not in _suggested(flow)
    assert flow["description_placeholders"]["detected"].startswith("- none")
    assert flow["description_placeholders"]["meter"] == "none: add a grid meter"
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {})
    assert flow["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert _site_options(entry)["live_price_general"] is None
    assert "live_price_feed_in" not in _site_options(entry)


async def test_today_meter_choice(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    clock: Clock,
    hass_storage: dict,
) -> None:
    """With several grid meters, the step asks which one Today so far reads (default
    the first); the choice is applied after the reload."""
    _amber_express(hass)
    _energy(hass, "sensor.inverter_import", 7.0)
    second = _grid_meter(export=False, sub_id="sub_inverter", sensor="sensor.inverter_import")
    second["title"] = "Inverter meter · grid meter"
    second["data"]["name"] = "Inverter meter"
    entry = await _entry(hass, aioclient_mock, hass_storage, subentries=[_grid_meter(), second])
    assert entry.runtime_data.manager.today.meter_name == "Whole house meter"
    flow = await _sources_flow(hass, entry)
    assert _suggested(flow)["today_grid_meter"] == "sub_grid"
    assert flow["description_placeholders"]["meter"] == "choose one below"
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {
            "live_price_general": GENERAL,
            "live_price_feed_in": FEED_IN,
            "today_grid_meter": "sub_inverter",
        },
    )
    await hass.async_block_till_done()
    assert _site_options(entry)["today_grid_meter"] == "sub_inverter"
    today = entry.runtime_data.manager.today
    assert today.meter_name == "Inverter meter"
    assert set(today.sides) == {"import"}  # the inverter meter has no export
    assert hass.states.get(CREDIT) is None
    assert entry.state is ConfigEntryState.LOADED


async def test_removing_the_site_removes_today_store(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    clock: Clock,
    hass_storage: dict,
) -> None:
    """Today so far's Store goes with the site."""
    _amber_express(hass)
    hass_storage[TODAY_STORE] = {"version": 1, "key": TODAY_STORE, "data": {"day": None}}
    entry = await _entry(hass, aioclient_mock, hass_storage)
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert TODAY_STORE not in hass_storage


# --- a live price found after setup (2.2.0-dev4) -----------------------------------------


def _unregistered_states(hass: HomeAssistant) -> None:
    """Remove the price states set without a registry entry (a state takes its entity
    ID, so a later registration would get another)."""
    hass.states.async_remove(GENERAL)
    hass.states.async_remove(FEED_IN)


async def test_price_registered_after_setup_starts_today(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    clock: Clock,
    hass_storage: dict,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Amber Express's entities registered after this integration was set up (the
    start-up race, or Amber Express installed later): Today so far starts in place, with
    its sensors, without a reload."""
    freezer.move_to(NOW)
    entry = await _entry(hass, aioclient_mock, hass_storage)
    manager = entry.runtime_data.manager
    assert manager.today is None and hass.states.get(COST) is None

    # Other integrations' entities, and removals, are ignored.
    _register(hass, "template", "other", "sensor.other_price")
    er.async_get(hass).async_remove("sensor.other_price")
    await hass.async_block_till_done()
    assert manager.today is None

    _unregistered_states(hass)
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        _amber_express(hass)
        await hass.async_block_till_done()
    reload.assert_not_called()
    _price(hass, GENERAL, 0.30)
    _price(hass, FEED_IN, 0.05)
    tracker = manager.today
    assert set(tracker.sides) == {"import", "export"}
    assert manager.sources.prices["general"].origin == "amber_express"
    assert hass.states.get(COST) is not None and hass.states.get(CREDIT) is not None
    registry = er.async_get(hass)
    assert registry.async_get(COST).device_id == entry.runtime_data.device_id

    await _tick(hass, freezer, tracker, NOW)
    _energy(hass, IMPORT, 101.0)
    await _tick(hass, freezer, tracker, NOW + FIVE)
    assert float(hass.states.get(COST).state) == pytest.approx(0.30)

    # Everything found: no more checks (a later registration changes nothing).
    _register(hass, "amber_express", f"{SITE_ID}_general_forecast", "sensor.forecast")
    await hass.async_block_till_done()
    assert manager.today is tracker
    await hass.config_entries.async_unload(entry.entry_id)
    assert tracker._unsubs == []


async def test_price_found_by_the_retries(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    clock: Clock,
    hass_storage: dict,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A price not found at setup is looked for every minute for 15 minutes after Home
    Assistant has started; after that only a registry change triggers a check."""
    freezer.move_to(NOW)
    _amber_express(hass)
    with patch.object(sources, "detect_prices", return_value={}):
        entry = await _entry(hass, aioclient_mock, hass_storage)
        manager = entry.runtime_data.manager
        assert manager.today is None
        freezer.tick(timedelta(minutes=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        assert manager.today is None
    freezer.tick(timedelta(minutes=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert manager.today is not None and hass.states.get(COST) is not None

    # Past the 15 minutes, the retries have ended.
    await hass.config_entries.async_unload(entry.entry_id)
    with patch.object(sources, "detect_prices", return_value={}):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        manager = entry.runtime_data.manager
        freezer.tick(timedelta(minutes=16))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert manager.today is None


async def test_feed_in_price_found_later_extends_today(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    clock: Clock,
    hass_storage: dict,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A feed-in price found after the general one adds the export side and its sensor;
    the day so far carries over. A feed-in price alone starts nothing."""
    freezer.move_to(NOW)
    _register(hass, "amber_express", f"{SITE_ID}_feed_in_price", FEED_IN)
    entry = await _entry(hass, aioclient_mock, hass_storage)
    manager = entry.runtime_data.manager
    assert manager.today is None
    assert set(manager.sources.prices) == {"feedIn"}

    with patch.object(sources, "detect_prices", return_value={}):
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    manager = entry.runtime_data.manager
    hass.states.async_remove(GENERAL)  # a state would take the entity ID
    _register(hass, "amber_express", f"{SITE_ID}_general_price", GENERAL)
    await hass.async_block_till_done()
    assert manager.today is not None  # both found together
    assert manager.sources.prices["general"].entity_id == GENERAL
    _price(hass, GENERAL, 0.30)

    # General first (feed-in left undetected), then the feed-in.
    er.async_get(hass).async_update_entity(FEED_IN, disabled_by=er.RegistryEntryDisabler.USER)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    manager = entry.runtime_data.manager
    tracker = manager.today
    assert set(tracker.sides) == {"import"}
    # Kept in the registry while the feed-in price may still turn up (not provided).
    assert hass.states.get(CREDIT).state == "unavailable"
    await _tick(hass, freezer, tracker, NOW)
    _energy(hass, IMPORT, 101.0)
    await _tick(hass, freezer, tracker, NOW + FIVE)

    er.async_get(hass).async_update_entity(FEED_IN, disabled_by=None)
    await hass.async_block_till_done()
    assert manager.today is not tracker
    assert set(manager.today.sides) == {"import", "export"}
    assert tracker._unsubs == []
    assert manager.today.summary(NOW + FIVE)["import"]["amount"] == pytest.approx(0.30)
    assert float(hass.states.get(CREDIT).state) == 0.0
    assert float(hass.states.get(COST).state) == pytest.approx(0.30)


# --- the offset-corrected value (2.2.0-dev4) ----------------------------------------------


def test_offset_corrected() -> None:
    """The correction goes on the import while importing, comes off the export while
    exporting; a side driven below zero passes the rest to the other side."""
    corrected = today_mod.offset_corrected
    assert corrected({"import": 0.5, "export": 0.2}, 0.01) == pytest.approx(
        {"import": 0.51, "export": 0.2}
    )
    assert corrected({"import": 0.0, "export": 0.3}, 0.01) == pytest.approx(
        {"import": 0.0, "export": 0.29}
    )
    assert corrected({"import": 0.01, "export": 0.0}, -0.02) == pytest.approx(
        {"import": 0.0, "export": 0.01}
    )
    assert corrected({"import": 0.0, "export": 0.005}, 0.01) == pytest.approx(
        {"import": 0.005, "export": 0.0}
    )
    assert corrected({"import": 0.5}, -1.0) == {"import": 0.0}
    assert corrected({"export": 0.3}, 0.01) == pytest.approx({"export": 0.29})
    assert corrected({"export": 0.3}, 1.0) == {"export": 0.0}
    assert corrected({}, 0.01) == {}


async def test_tracker_offset_corrected(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, hass_storage: dict
) -> None:
    """With the meter's calibration offset, the estimate is offset-corrected (an interval
    without an offset counts as measured), and the raw estimate is kept; after a restart
    the correction covers the time since the last boundary."""
    freezer.move_to(NOW)
    _price(hass, GENERAL, 0.40)
    _price(hass, FEED_IN, 0.05)
    _energy(hass, IMPORT, 100.0)
    _energy(hass, EXPORT, 50.0)
    offset: list[float | None] = [None]
    tracker = TodayTracker(
        hass,
        SITE_KEY,
        [Side("import", IMPORT, GENERAL, "E1"), Side("export", EXPORT, FEED_IN, "B1")],
        "Whole house meter",
        lambda: None,
        offset_w=lambda: offset[0],
    )
    await tracker.async_load()
    tracker.async_start()
    await _tick(hass, freezer, tracker, NOW)
    _energy(hass, IMPORT, 100.5)
    await _tick(hass, freezer, tracker, NOW + FIVE)
    summary = tracker.summary(NOW + FIVE)
    assert summary["offset_w"] is None
    assert summary["import"]["amount"] == pytest.approx(0.5 * 0.40)  # the raw energy
    assert summary["import"]["raw_estimate"] == pytest.approx(0.5 * 0.40)
    assert summary["import"]["offset_corrected_intervals"] == 0

    offset[0] = 120.0  # 0.01 kWh per 5 minutes
    _energy(hass, IMPORT, 101.0)
    _energy(hass, EXPORT, 50.2)
    await _tick(hass, freezer, tracker, NOW + 2 * FIVE)
    _energy(hass, EXPORT, 50.5)
    await _tick(hass, freezer, tracker, NOW + 3 * FIVE)
    summary = tracker.summary(NOW + 3 * FIVE)
    assert summary["offset_w"] == 120.0
    assert summary["import"]["raw_estimate"] == pytest.approx(1.0 * 0.40)
    assert summary["import"]["raw_energy_kwh"] == pytest.approx(1.0)
    assert summary["import"]["amount"] == pytest.approx((0.5 + 0.51) * 0.40)
    assert summary["import"]["kwh"] == pytest.approx(0.5 + 0.51)
    assert summary["import"]["offset_corrected_intervals"] == 2
    assert summary["export"]["amount"] == pytest.approx((0.2 + 0.29) * 0.05)
    assert summary["export"]["raw_estimate"] == pytest.approx(0.5 * 0.05)
    assert summary["export"]["offset_corrected_intervals"] == 2
    await tracker.async_save()
    tracker.async_stop()

    # A restart 15 minutes later: the correction covers the 15 minutes.
    later = NOW + 6 * FIVE
    freezer.move_to(later)
    _energy(hass, IMPORT, 102.0)
    tracker = TodayTracker(
        hass,
        SITE_KEY,
        [Side("import", IMPORT, GENERAL, "E1"), Side("export", EXPORT, FEED_IN, "B1")],
        "Whole house meter",
        lambda: None,
        offset_w=lambda: 120.0,
    )
    await tracker.async_load()
    tracker.async_start()
    await _tick(hass, freezer, tracker, later)
    summary = tracker.summary(later)
    assert summary["import"]["kwh"] == pytest.approx(0.5 + 0.51 + 1.03)
    assert summary["import"]["raw_energy_kwh"] == pytest.approx(2.0)
    tracker.async_stop()


async def test_tracker_loads_older_states(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, hass_storage: dict
) -> None:
    """A state saved by 2.2.0rc1 (offset-corrected totals of the intervals with an offset
    only) gets the other intervals at the raw rate; one saved by 2.2.0b2 (none) gets the
    raw estimate."""
    later = NOW + 6 * FIVE
    freezer.move_to(later)
    _energy(hass, IMPORT, 102.0)
    side = {
        "kwh": 2.0,
        "amount": 0.8,
        "unpriced_kwh": 0.0,
        "intervals": 4,
        "corrected_kwh": 1.56,
        "corrected_amount": 0.624,
        "corrected_intervals": 3,
        "reading": 102.0,
        "entity_id": IMPORT,
    }
    data = {"day": TODAY.isoformat(), "last_tick": later.isoformat(), "history": {}}
    hass_storage[TODAY_STORE] = {
        "version": 1,
        "key": TODAY_STORE,
        "data": {**data, "sides": {"import": side}},
    }
    tracker = _tracker(hass, export=False)
    await tracker.async_load()
    summary = tracker.summary(later)["import"]
    assert summary["kwh"] == pytest.approx(1.56 + 2.0 / 4)
    assert summary["amount"] == pytest.approx(0.624 + 0.8 / 4)
    assert summary["raw_estimate"] == pytest.approx(0.8)
    assert summary["offset_corrected_intervals"] == 3
    assert "corrected_kwh" not in tracker._data["sides"]["import"]

    for key in ("corrected_kwh", "corrected_amount", "corrected_intervals"):
        del side[key]
    tracker = _tracker(hass, export=False)
    await tracker.async_load()
    summary = tracker.summary(later)["import"]
    assert summary["offset_corrected_intervals"] == 0
    assert summary["amount"] == pytest.approx(0.80)  # as saved
    assert summary["kwh"] == pytest.approx(2.0)


def test_compare_offset_corrected() -> None:
    """The comparison shows the offset-corrected estimate and the raw one, each with its
    error; a day saved by 2.2.0rc1 (corrected totals) is compared too."""
    billed = {"status": "imported", "totals": {E1_COST: 2.0, E1_ENERGY: 10.0}}
    day = {
        "amount": 1.9,
        "kwh": 9.6,
        "unpriced_kwh": 0.0,
        "intervals": 288,
        "estimate_amount": 1.98,
        "estimate_kwh": 9.84,
        "corrected_intervals": 288,
    }
    args = {
        "billed": lambda _: billed,
        "role": "import",
        "amount_sid": E1_COST,
        "energy_sid": E1_ENERGY,
        "before": TODAY,
    }
    result = compare({YESTERDAY.isoformat(): {"import": day}}, **args)
    assert result["estimated_corrected"] == 1.98
    assert result["estimated_corrected_kwh"] == 9.84
    assert result["error_corrected"] == pytest.approx(-0.02)
    assert result["error_corrected_percent"] == -1.0
    assert result["offset_corrected_intervals"] == 288
    assert result["estimated_raw"] == 1.9
    assert result["error_raw"] == pytest.approx(-0.1)
    assert result["error_raw_percent"] == -5.0
    rc1 = {k: v for k, v in day.items() if not k.startswith("estimate_")}
    rc1 |= {"corrected_amount": 1.98, "corrected_kwh": 9.84}
    assert compare({YESTERDAY.isoformat(): {"import": rc1}}, **args) == result


async def test_today_sensor_offset_attributes(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    clock: Clock,
    hass_storage: dict,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The sensors' value is offset-corrected, from the grid meter's calibration offset,
    with the raw estimate as an attribute."""
    freezer.move_to(NOW)
    _amber_express(hass)
    entry = await _entry(
        hass,
        aioclient_mock,
        hass_storage,
        options={"billing_day": 20, "daily_supply": 1.0, "amber_subscription": 0.0},
    )
    manager = entry.runtime_data.manager
    manager.calibration = {"sub_grid": {"offset_w": 120.0}}
    await _tick(hass, freezer, manager.today, NOW)
    _energy(hass, IMPORT, 100.5)
    await _tick(hass, freezer, manager.today, NOW + FIVE)
    cost = hass.states.get(COST)
    assert float(cost.state) == pytest.approx(0.51 * 0.30)
    assert cost.attributes["energy_kwh"] == pytest.approx(0.51)
    assert cost.attributes["raw_estimate"] == pytest.approx(0.15)
    assert cost.attributes["raw_energy_kwh"] == pytest.approx(0.5)
    assert cost.attributes["offset_w"] == 120.0
    assert cost.attributes["offset_corrected_intervals"] == 1
    credit = hass.states.get(CREDIT)
    assert float(credit.state) == 0.0
    assert credit.attributes["raw_estimate"] == 0.0
    bill = hass.states.get(BILL)
    to_date = float(bill.state)
    assert bill.attributes["including_today_estimated"] == pytest.approx(
        round(to_date + 0.51 * 0.30 + 1.1, 2)
    )
    assert bill.attributes["including_today_estimated_raw"] == pytest.approx(
        round(to_date + 0.15 + 1.1, 2)
    )


async def test_reload_keeps_the_last_interval(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    clock: Clock,
    hass_storage: dict,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A reload right after an interval keeps it: the state is saved on unload, not only
    after the 30 s delay."""
    freezer.move_to(NOW)
    _amber_express(hass)
    entry = await _entry(hass, aioclient_mock, hass_storage)
    tracker = entry.runtime_data.manager.today
    await _tick(hass, freezer, tracker, NOW)
    _energy(hass, IMPORT, 101.0)
    await _tick(hass, freezer, tracker, NOW + FIVE)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    summary = entry.runtime_data.manager.today.summary(NOW + FIVE)
    assert summary["last_interval_end"] == (NOW + FIVE).isoformat()
    assert summary["import"]["amount"] == pytest.approx(0.30)
