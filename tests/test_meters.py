"""Milestone 17 (2.2): site identity, grid meters, appliances, the calibration offset, and
converting 2.1 own-sensor mappings (DESIGN section 21). All data is synthetic."""

from datetime import UTC, date, datetime, timedelta
import math
from typing import Any
from unittest.mock import patch

from homeassistant.components.recorder.statistics import get_metadata
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.amber_energy_dashboard import calibration, meters
from custom_components.amber_energy_dashboard.const import (
    CONF_CHANNELS,
    DOMAIN,
    SUBENTRY_APPLIANCE,
    SUBENTRY_GRID_METER,
)
from custom_components.amber_energy_dashboard.devices import site_model
from custom_components.amber_energy_dashboard.meters import ConversionFailed, ConversionPlan

from .fake_amber import FakeAmber
from .synthetic import SITE_ID, make_usage_day, site_json
from .test_manager import (  # noqa: F401 - fixtures are used by name
    ENTRY_ID,
    PREFIX,
    TODAY,
    YESTERDAY,
    Clock,
    _fast_verify,
    _no_real_timers,
    _preload_store,
    _run,
    _setup,
    _setup_entry,
    _store_key,
    clock,
)
from .test_own import EXPORT, OWN, OWN_EXPORT, SENSOR, _energy_like, _import_sensor, _own_rows, _sub

DAY_BEFORE = TODAY - timedelta(days=2)
ISSUE = f"convert_own_sensors_{ENTRY_ID}"
HEAT_PUMP = "sensor.heat_pump_energy"
OWN_HEAT_PUMP = f"{PREFIX}_own_heat_pump_energy_cost"
CL_CHANNELS = [
    {"identifier": "E1", "type": "general", "tariff": "EA116"},
    {"identifier": "E2", "type": "controlledLoad", "tariff": "EA030"},
    {"identifier": "B1", "type": "feedIn", "tariff": None},
]


def _amber_kwh(day: date, channel: str) -> float:
    return math.fsum(r["kwh"] for r in make_usage_day(day) if r["channelIdentifier"] == channel)


async def _add(hass: HomeAssistant, entry, subentry_type: str, user_input: dict) -> dict:
    flow = await hass.config_entries.subentries.async_init(
        (entry.entry_id, subentry_type), context={"source": "user"}
    )
    return await hass.config_entries.subentries.async_configure(flow["flow_id"], user_input)


def _mapping_states(hass: HomeAssistant, entry, subentry_id: str) -> dict[str, Any]:
    """The mapping's entity states, by translation key."""
    entities = er.async_get(hass)
    return {
        e.translation_key: hass.states.get(e.entity_id)
        for e in er.async_entries_for_config_entry(entities, entry.entry_id)
        if e.config_subentry_id == subentry_id
    }


# --- site identity ------------------------------------------------------------------------


def test_site_model_line() -> None:
    """ "<network> · NMI …<last 4>"; never the full NMI; parts left out when missing."""
    assert site_model("Endeavour Energy", "4000001234") == "Endeavour Energy · NMI …1234"
    assert site_model(None, "4000001234") == "NMI …1234"
    assert site_model("Endeavour Energy", "") == "Endeavour Energy"
    assert site_model(None, None) is None


async def test_site_device_shows_network_and_nmi_tail(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """The site device's model comes from /sites; its name and entity IDs are unchanged."""
    _preload_store(hass_storage, retention_days=2)
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    entry = await _setup_entry(hass, aioclient_mock, fake)
    device = dr.async_get(hass).async_get(entry.runtime_data.device_id)
    assert device.model == "Example Network · NMI …I000"
    assert device.name == "Amber Energy Dashboard (site)"
    assert "FAKENMI000" not in device.model
    # Entities that name the device without a model leave it in place.
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert dr.async_get(hass).async_get(entry.runtime_data.device_id).model == device.model
    assert hass.states.get("sensor.amber_energy_dashboard_import_status") is not None


SITE_ENTITY_IDS = [
    "button.amber_energy_dashboard_re_check_retention",
    "button.amber_energy_dashboard_run_now",
    "sensor.amber_energy_dashboard_days_behind",
    "sensor.amber_energy_dashboard_import_status",
    "sensor.amber_energy_dashboard_last_imported_date",
    "sensor.amber_energy_dashboard_next_scheduled_run",
    "sensor.amber_energy_dashboard_yesterday_b1_compensation",
    "sensor.amber_energy_dashboard_yesterday_b1_energy",
    "sensor.amber_energy_dashboard_yesterday_e1_cost",
    "sensor.amber_energy_dashboard_yesterday_e1_energy",
]


def _entity_ids(hass: HomeAssistant, entry) -> list[str]:
    return sorted(
        e.entity_id for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    )


async def test_site_name_names_the_device(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """A new install's site name is the device's name; entity IDs stay pinned."""
    _preload_store(hass_storage, retention_days=2)
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    entry = await _setup_entry(hass, aioclient_mock, fake, data={"site_name": "Home"})
    device = dr.async_get(hass).async_get(entry.runtime_data.device_id)
    assert (device.name, device.model) == ("Home", "Example Network · NMI …I000")
    assert _entity_ids(hass, entry) == SITE_ENTITY_IDS
    state = hass.states.get("sensor.amber_energy_dashboard_days_behind")
    assert state.attributes["friendly_name"] == "Home Days behind"
    assert entry.title == "Amber Energy Dashboard"


async def test_site_name_upgrade_and_device_page_rename(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """An existing install keeps "Amber Energy Dashboard (site)". A name given on Home
    Assistant's device page is the site's name: a default multi-site title follows it, a
    title the user chose is kept, and entity IDs never change. The options have no site
    name field."""
    _preload_store(hass_storage, retention_days=2)
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    other = MockConfigEntry(
        domain=DOMAIN,
        unique_id="01OTHERSITE",
        title="Amber Energy Dashboard (…9999)",
        data={"nmi": "6000009999"},
    )
    other.add_to_hass(hass)
    entry = await _setup_entry(hass, aioclient_mock, fake)
    devices = dr.async_get(hass)
    site_device = entry.runtime_data.device_id
    assert devices.async_get(site_device).name == "Amber Energy Dashboard (site)"
    assert entry.title == "Amber Energy Dashboard (…I000)"
    assert _entity_ids(hass, entry) == SITE_ENTITY_IDS
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    assert "site_name" not in flow["menu_options"]

    async def rename(name: str | None) -> None:
        devices.async_update_device(site_device, name_by_user=name)
        await hass.async_block_till_done()

    await rename("Beach house")
    assert entry.title == "Amber Energy Dashboard (Beach house)"
    await rename("Shack")
    assert entry.title == "Amber Energy Dashboard (Shack)"
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert devices.async_get(site_device).name_by_user == "Shack"
    assert entry.title == "Amber Energy Dashboard (Shack)"
    devices.async_update_device(site_device, model="changed elsewhere")  # not a rename
    await hass.async_block_till_done()
    await rename(None)
    assert entry.title == "Amber Energy Dashboard (…I000)"
    hass.config_entries.async_update_entry(entry, title="My place")  # the user's own
    await rename("Home")
    assert entry.title == "My place"
    assert _entity_ids(hass, entry) == SITE_ENTITY_IDS


# --- the sub-entry flows --------------------------------------------------------------------


async def test_grid_meter_flow(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Name, import (general or controlled load) and optional export (feed-in); each
    sensor must be a cumulative energy sensor that no other mapping uses."""
    _preload_store(hass_storage, retention_days=2)
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    entry = await _setup_entry(
        hass,
        aioclient_mock,
        fake,
        data={CONF_CHANNELS: CL_CHANNELS},
        site=site_json(
            (("E1", "general", "EA116"), ("E2", "controlledLoad", "EA030"), ("B1", "feedIn", None))
        ),
        subentries=[_sub(HEAT_PUMP, "E1", sub_id="sub_pump", name="Heat pump")],
    )
    for entity_id in (SENSOR, EXPORT, HEAT_PUMP):
        await _import_sensor(hass, entity_id, {YESTERDAY: _energy_like(YESTERDAY, "E1")})
    flow = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_GRID_METER), context={"source": "user"}
    )
    assert flow["step_id"] == "user"
    schema = {str(k): k for k in flow["data_schema"].schema}
    assert list(schema) == ["name", "import_entity_id", "import_channel", "export_entity_id"]
    assert schema["import_channel"].default() == "E1"  # general first

    async def submit(user_input: dict) -> dict:
        return await hass.config_entries.subentries.async_configure(flow["flow_id"], user_input)

    flow = await submit({"name": "Mains", "import_entity_id": SENSOR, "export_entity_id": SENSOR})
    assert flow["errors"] == {"export_entity_id": "same_sensor"}
    flow = await submit(
        {"name": " ", "import_entity_id": HEAT_PUMP, "export_entity_id": "sensor.missing"}
    )
    assert flow["errors"] == {
        "import_entity_id": "sensor_in_use",
        "export_entity_id": "sensor_not_found",
        "name": "name_required",
    }
    flow = await submit(
        {
            "name": " Hot water meter ",
            "import_entity_id": SENSOR,
            "import_channel": "E2",
            "export_entity_id": EXPORT,
        }
    )
    assert flow["type"] is FlowResultType.CREATE_ENTRY
    assert flow["title"] == "Hot water meter · grid meter"
    assert flow["data"] == {
        "site_id": SITE_ID,
        "name": "Hot water meter",
        "import_entity_id": SENSOR,
        "import_channel": "E2",
        "export_entity_id": EXPORT,
        "export_channel": "B1",
    }
    await hass.async_block_till_done()
    [grid] = [s for s in entry.subentries.values() if s.subentry_type == SUBENTRY_GRID_METER]
    assert grid.unique_id == f"grid_meter:{SENSOR}"
    sensors = {s.role: s for s in entry.runtime_data.manager.own_sensors if s.role}
    assert sensors["import"].channel.identifier == "E2"
    assert sensors["import"].spec.statistic_id == OWN
    assert sensors["import"].spec.name == "Hot water meter import cost (Amber prices)"
    assert sensors["export"].spec.statistic_id == OWN_EXPORT
    assert sensors["export"].spec.name == "Hot water meter export compensation (Amber prices)"

    # The appliance flow offers both import channels, general first.
    flow = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_APPLIANCE), context={"source": "user"}
    )
    schema = {str(k): k for k in flow["data_schema"].schema}
    assert schema["channel"].default() == "E1"
    flow = await hass.config_entries.subentries.async_configure(
        flow["flow_id"], {"name": "Hot water", "entity_id": EXPORT, "channel": "E2"}
    )
    assert flow["errors"] == {"entity_id": "sensor_in_use"}


async def test_grid_meter_without_export_or_feed_in(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """A site without a feed-in channel offers no export sensor, and the meter has no
    export reconciliation and no calibration offset."""
    _preload_store(hass_storage, retention_days=2)
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    entry = await _setup_entry(
        hass,
        aioclient_mock,
        fake,
        data={CONF_CHANNELS: [CL_CHANNELS[0]]},
        site=site_json((("E1", "general", "EA116"),)),
    )
    await _import_sensor(hass, SENSOR, {YESTERDAY: _energy_like(YESTERDAY, "E1")})
    flow = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_GRID_METER), context={"source": "user"}
    )
    assert [str(k) for k in flow["data_schema"].schema] == ["name", "import_entity_id"]
    flow = await hass.config_entries.subentries.async_configure(
        flow["flow_id"], {"name": "Mains", "import_entity_id": SENSOR}
    )
    assert flow["data"] == {
        "site_id": SITE_ID,
        "name": "Mains",
        "import_entity_id": SENSOR,
        "import_channel": "E1",
    }
    await hass.async_block_till_done()
    [subentry_id] = list(entry.subentries)
    assert list(_mapping_states(hass, entry, subentry_id)) == ["import_reconciliation"]


async def test_flows_need_an_import_channel(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """A site with only a feed-in channel has nothing to price an import on."""
    _preload_store(hass_storage, retention_days=2)
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    entry = await _setup_entry(
        hass,
        aioclient_mock,
        fake,
        data={CONF_CHANNELS: [CL_CHANNELS[2]]},
        site=site_json((("B1", "feedIn", None),)),
    )
    for subentry_type in (SUBENTRY_GRID_METER, SUBENTRY_APPLIANCE):
        flow = await hass.config_entries.subentries.async_init(
            (entry.entry_id, subentry_type), context={"source": "user"}
        )
        assert flow["type"] is FlowResultType.ABORT
        assert flow["reason"] == "no_import_channel"
    assert set(entry.supported_subentry_types) == {SUBENTRY_GRID_METER, SUBENTRY_APPLIANCE}


# --- grid meter sensors and the calibration offset -----------------------------------------


async def _grid_meter_entry(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict,
    *,
    export_days: list[date] | None = None,
    outlier: bool = False,
):
    """A grid meter whose import matches Amber's and whose export reads 14 Wh more each
    hour, so the meter's net is 14 W below Amber's (one hour 1 kWh more, if ``outlier``)."""
    _preload_store(hass_storage, retention_days=2)
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    days = [DAY_BEFORE, YESTERDAY]

    def export(day: date) -> list[float]:
        extra = [0.014 / 12] * 288
        if outlier and day == YESTERDAY:
            extra[3 * 12 : 4 * 12] = [1.0 / 12] * 12
        return [e + x for e, x in zip(_energy_like(day, "B1"), extra, strict=True)]

    await _import_sensor(hass, SENSOR, {d: _energy_like(d, "E1") for d in days})
    await _import_sensor(hass, EXPORT, {d: export(d) for d in export_days or days})
    entry = await _setup_entry(
        hass,
        aioclient_mock,
        fake,
        subentries=[
            {
                "data": {
                    "name": "Whole house meter",
                    "import_entity_id": SENSOR,
                    "import_channel": "E1",
                    "export_entity_id": EXPORT,
                    "export_channel": "B1",
                },
                "subentry_id": "sub_grid",
                "subentry_type": SUBENTRY_GRID_METER,
                "title": "Whole house meter · grid meter",
                "unique_id": f"grid_meter:{SENSOR}",
            }
        ],
    )
    return entry, fake


async def test_grid_meter_reconciliation_and_calibration_offset(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Both sides are priced and reconciled on the meter's device, and the calibration
    offset is the median of the hourly differences: one wild hour doesn't move it."""
    entry, _ = await _grid_meter_entry(hass, aioclient_mock, hass_storage, outlier=True)
    result = await _run(hass)
    await hass.async_block_till_done()
    assert result["chains"]["own:sub_grid:import"]["written"] == [
        DAY_BEFORE.isoformat(),
        YESTERDAY.isoformat(),
    ]
    assert len(await _own_rows(hass, OWN)) == 48
    assert len(await _own_rows(hass, OWN_EXPORT)) == 48

    states = _mapping_states(hass, entry, "sub_grid")
    assert set(states) == {"import_reconciliation", "export_reconciliation", "calibration_offset"}
    assert {s.entity_id for s in states.values()} == {
        "sensor.whole_house_meter_import_reconciliation",
        "sensor.whole_house_meter_export_reconciliation",
        "sensor.whole_house_meter_calibration_offset",
    }
    assert float(states["import_reconciliation"].state) == pytest.approx(0, abs=1e-3)
    export_extra = 0.014 * 23 + 1.0  # 23 hours of 14 Wh, and the outlier hour
    assert float(states["export_reconciliation"].state) == pytest.approx(export_extra, abs=1e-3)
    assert states["export_reconciliation"].attributes["amber_kwh"] == pytest.approx(
        _amber_kwh(YESTERDAY, "B1"), abs=1e-3
    )

    offset = states["calibration_offset"]
    assert float(offset.state) == pytest.approx(14.0, abs=0.05)
    assert offset.attributes["unit_of_measurement"] == "W"
    assert offset.attributes["device_class"] == "power"
    assert "state_class" not in offset.attributes
    assert offset.attributes["hours_used"] == 48
    assert offset.attributes["days_used"] == 2
    assert offset.attributes["first_day"] == DAY_BEFORE.isoformat()
    assert offset.attributes["last_day"] == YESTERDAY.isoformat()
    assert offset.attributes["iqr_w"] == pytest.approx(0, abs=0.05)
    assert offset.attributes["p75_w"] == pytest.approx(14.0, abs=0.05)
    assert offset.attributes["last_updated"] == clock.now.isoformat(timespec="seconds")

    device = dr.async_get(hass).async_get(er.async_get(hass).async_get(offset.entity_id).device_id)
    assert device.name == "Whole house meter"
    assert device.model == "Grid meter"
    assert device.via_device_id == entry.runtime_data.device_id
    meta = await hass.async_add_executor_job(get_metadata, hass)
    assert meta[OWN][1]["name"] == "Whole house meter import cost (Amber prices)"


async def test_calibration_leaves_out_meter_gaps(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Hours where the meter's statistics have a gap are left out; restarting Home
    Assistant recomputes the offset."""
    entry, _ = await _grid_meter_entry(hass, aioclient_mock, hass_storage, export_days=[YESTERDAY])
    await _run(hass)
    offset = _mapping_states(hass, entry, "sub_grid")["calibration_offset"]
    assert offset.attributes["hours_used"] == 24  # the export has no data the day before
    assert float(offset.state) == pytest.approx(14.0, abs=0.05)
    entry.runtime_data.manager.calibration = {}
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    offset = _mapping_states(hass, entry, "sub_grid")["calibration_offset"]
    assert offset.attributes["hours_used"] == 24


async def test_calibration_needs_billed_days(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Before anything is imported the offset is unknown; a failure to compute it is
    logged and never stops a run."""
    entry, _ = await _grid_meter_entry(hass, aioclient_mock, hass_storage)
    offset = _mapping_states(hass, entry, "sub_grid")["calibration_offset"]
    assert offset.state == "unknown"
    assert offset.attributes["hours_used"] == 0
    assert offset.attributes["first_day"] is None
    with patch.object(calibration, "async_offset", side_effect=RuntimeError("boom")):
        result = await _run(hass)
    assert result["status"] == "caught_up"
    assert entry.runtime_data.manager.calibration == {}
    assert _mapping_states(hass, entry, "sub_grid")["calibration_offset"].state == "unknown"


def test_billed_days_and_summary() -> None:
    """Only imported days without estimated intervals count, within 14 days of the last
    imported day; fewer than 24 hours give no value."""
    last = date(2026, 10, 20)
    records = {
        last: {"status": "imported", "estimated_records": 0},
        last - timedelta(days=1): {"status": "imported", "estimated_records": 3},
        last - timedelta(days=2): {"status": "skipped_gap"},
        last - timedelta(days=13): {"status": "imported"},
        last - timedelta(days=14): {"status": "imported"},
    }
    assert calibration.billed_days(records.get, last) == [last - timedelta(days=13), last]
    now = datetime(2026, 10, 21, tzinfo=UTC)
    short = calibration.summarise([10.0] * 23, [last], now)
    assert short["offset_w"] is None and short["hours_used"] == 23
    full = calibration.summarise([float(h) for h in range(25)], [last], now)
    assert full["offset_w"] == 12.0
    assert (full["p25_w"], full["p75_w"], full["iqr_w"]) == (6.0, 18.0, 12.0)


# --- appliances ---------------------------------------------------------------------------


async def test_appliance_cost_and_yesterday_sensors(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """An appliance is priced like any own sensor, has yesterday's energy and cost on its
    device, and no reconciliation."""
    _preload_store(hass_storage, retention_days=2)
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    days = [DAY_BEFORE, YESTERDAY]
    await _import_sensor(hass, HEAT_PUMP, {d: _energy_like(d, "E1", 0.25) for d in days})
    entry = await _setup_entry(
        hass,
        aioclient_mock,
        fake,
        subentries=[
            {
                "data": {"name": "Pool heat pump", "entity_id": HEAT_PUMP, "channel": "E1"},
                "subentry_id": "sub_pump",
                "subentry_type": SUBENTRY_APPLIANCE,
                "title": "Pool heat pump · appliance (general E1)",
                "unique_id": f"appliance:{HEAT_PUMP}",
            }
        ],
    )
    states = _mapping_states(hass, entry, "sub_pump")
    assert states["appliance_yesterday_energy"].state == "unknown"
    await _run(hass)
    await hass.async_block_till_done()
    states = _mapping_states(hass, entry, "sub_pump")
    assert set(states) == {"appliance_yesterday_energy", "appliance_yesterday_cost"}
    energy, cost = states["appliance_yesterday_energy"], states["appliance_yesterday_cost"]
    assert energy.entity_id == "sensor.pool_heat_pump_yesterday_energy"
    assert energy.attributes["friendly_name"] == "Pool heat pump Yesterday energy"
    assert float(energy.state) == pytest.approx(_amber_kwh(YESTERDAY, "E1") * 0.25, abs=1e-3)
    rows = await _own_rows(hass, OWN_HEAT_PUMP)
    assert float(cost.state) == pytest.approx(math.fsum(r["state"] for r in rows[24:]), abs=1e-4)
    assert cost.attributes["unit_of_measurement"] == "AUD"
    assert cost.attributes["date"] == YESTERDAY.isoformat()
    assert cost.attributes["lower_precision"] is False
    assert "state_class" not in cost.attributes
    device = dr.async_get(hass).async_get(er.async_get(hass).async_get(cost.entity_id).device_id)
    assert (device.name, device.model) == ("Pool heat pump", "Appliance (general E1)")
    assert "own:sub_pump" not in (entry.runtime_data.manager.snapshot()["reconciliation"])


# --- converting 2.1 own-sensor mappings ----------------------------------------------------


async def _fix_flow(hass: HomeAssistant) -> dict[str, Any]:
    assert await async_setup_component(hass, "repairs", {})
    manager = repairs_flow_manager(hass)
    assert manager is not None
    return await manager.async_init(DOMAIN, data={"issue_id": ISSUE})


async def _configure(hass: HomeAssistant, flow: dict[str, Any], data: dict) -> dict[str, Any]:
    manager = repairs_flow_manager(hass)
    assert manager is not None
    return await manager.async_configure(flow["flow_id"], data)


async def _legacy_entry(hass, aioclient_mock, hass_storage, subentries: list[dict]):
    _preload_store(hass_storage, retention_days=2)
    fake = FakeAmber(TODAY - timedelta(days=30), TODAY + timedelta(days=3))
    days = [DAY_BEFORE, YESTERDAY, TODAY]
    await _import_sensor(hass, SENSOR, {d: _energy_like(d, "E1") for d in days})
    await _import_sensor(hass, EXPORT, {d: _energy_like(d, "B1") for d in days})
    await _import_sensor(hass, HEAT_PUMP, {d: _energy_like(d, "E1", 0.3) for d in days})
    entry = await _setup_entry(hass, aioclient_mock, fake, subentries=subentries)
    await _run(hass)
    await hass.async_block_till_done()
    return entry


async def test_convert_import_and_export_to_a_grid_meter(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """A general and a feed-in mapping become one grid meter: the statistics and their
    chain state are kept, the reconciliation sensors keep their entity IDs, and the next
    day continues the same statistics."""
    entry = await _legacy_entry(
        hass,
        aioclient_mock,
        hass_storage,
        [
            _sub(SENSOR, "E1", name="Whole House Energy Consumed"),
            _sub(EXPORT, "B1", sub_id="sub_export", name="Whole House Energy Produced"),
        ],
    )
    issue = ir.async_get(hass).async_get_issue(DOMAIN, ISSUE)
    assert issue is not None and issue.is_fixable
    assert issue.translation_placeholders["mappings"] == f"- {SENSOR}\n- {EXPORT}"
    entities = er.async_get(hass)
    old_ids = {
        sid: entities.async_get_entity_id("sensor", DOMAIN, f"{SITE_ID}_reconciliation_{sid}")
        for sid in ("sub_house", "sub_export")
    }
    assert all(old_ids.values())
    before = {sid: await _own_rows(hass, sid) for sid in (OWN, OWN_EXPORT)}
    chains_before = hass_storage[_store_key()]["data"]["chains"]
    house_days = chains_before["own:sub_house"]["days"]

    flow = await _fix_flow(hass)
    assert flow["step_id"] == "plan"
    schema = {str(k): k for k in flow["data_schema"].schema}
    assert schema["grid_import"].default() == "sub_house"
    assert schema["grid_export"].default() == "sub_export"
    flow = await _configure(
        hass,
        flow,
        {"grid_import": "sub_house", "grid_export": "sub_export", "grid_name": "Whole house meter"},
    )
    assert flow["step_id"] == "confirm"
    plan = flow["description_placeholders"]["plan"]
    assert "Grid meter **Whole house meter**" in plan
    assert "import: sensor.house_energy" in plan
    flow = await _configure(hass, flow, {})
    assert flow["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    [grid] = entry.subentries.values()
    assert grid.subentry_type == SUBENTRY_GRID_METER
    assert grid.title == "Whole house meter · grid meter"
    assert dict(grid.data) == {
        "site_id": SITE_ID,
        "name": "Whole house meter",
        "import_entity_id": SENSOR,
        "import_channel": "E1",
        "export_entity_id": EXPORT,
        "export_channel": "B1",
        "entity_ids": {
            "import_reconciliation": old_ids["sub_house"],
            "export_reconciliation": old_ids["sub_export"],
        },
    }
    chains = hass_storage[_store_key()]["data"]["chains"]
    assert set(chains) == {f"own:{grid.subentry_id}:import", f"own:{grid.subentry_id}:export"}
    assert chains[f"own:{grid.subentry_id}:import"]["days"] == house_days
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE) is None
    states = _mapping_states(hass, entry, grid.subentry_id)
    assert states["import_reconciliation"].entity_id == old_ids["sub_house"]
    assert states["export_reconciliation"].entity_id == old_ids["sub_export"]
    assert states["calibration_offset"] is not None
    assert {s.subentry_id for s in entry.runtime_data.manager.own_sensors} == {grid.subentry_id}
    devices = dr.async_get(hass)
    assert [d.name for d in dr.async_entries_for_config_entry(devices, entry.entry_id)] == [
        "Amber Energy Dashboard (site)",
        "Whole house meter",
    ]
    for sid in (OWN, OWN_EXPORT):
        assert await _own_rows(hass, sid) == before[sid]

    clock.now += timedelta(days=1)
    result = await _run(hass)
    assert result["chains"][f"own:{grid.subentry_id}:import"]["written"] == [TODAY.isoformat()]
    rows = await _own_rows(hass, OWN)
    assert rows[:48] == before[OWN]
    assert rows[48]["sum"] == pytest.approx(before[OWN][-1]["sum"] + rows[48]["state"], abs=1e-6)
    assert result["chains"][f"own:{grid.subentry_id}:export"]["written"] == [TODAY.isoformat()]


async def test_convert_choices_and_appliances(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """With two general mappings there is no default grid meter: an export needs an
    import, a grid meter needs a name, and the mappings left over become appliances."""
    entry = await _legacy_entry(
        hass,
        aioclient_mock,
        hass_storage,
        [
            _sub(SENSOR, "E1", name="House"),
            _sub(HEAT_PUMP, "E1", sub_id="sub_pump", name="Heat pump"),
            _sub(EXPORT, "B1", sub_id="sub_export", name="Solar"),
        ],
    )
    pump_rows = await _own_rows(hass, OWN_HEAT_PUMP)
    flow = await _fix_flow(hass)
    schema = {str(k): k for k in flow["data_schema"].schema}
    assert schema["grid_import"].default() == "none"
    assert schema["grid_export"].default() == "none"
    flow = await _configure(hass, flow, {"grid_import": "none", "grid_export": "sub_export"})
    assert flow["errors"] == {"grid_export": "export_needs_import"}
    flow = await _configure(
        hass, flow, {"grid_import": "sub_house", "grid_export": "none", "grid_name": " "}
    )
    assert flow["errors"] == {"grid_name": "name_required"}
    flow = await _configure(
        hass, flow, {"grid_import": "sub_house", "grid_export": "none", "grid_name": "Mains"}
    )
    assert "Appliance **Heat pump**" in flow["description_placeholders"]["plan"]
    assert "Appliance **Solar**" in flow["description_placeholders"]["plan"]
    flow = await _configure(hass, flow, {})
    assert flow["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    by_type: dict[str, list] = {}
    for sub in entry.subentries.values():
        by_type.setdefault(sub.subentry_type, []).append(sub)
    [grid] = by_type[SUBENTRY_GRID_METER]
    assert "export_entity_id" not in grid.data
    assert sorted(s.title for s in by_type[SUBENTRY_APPLIANCE]) == [
        "Heat pump · appliance (general E1)",
        "Solar · appliance (feed-in B1)",
    ]
    solar = next(s for s in by_type[SUBENTRY_APPLIANCE] if s.data["name"] == "Solar")
    states = _mapping_states(hass, entry, solar.subentry_id)
    assert set(states) == {"appliance_yesterday_energy", "appliance_yesterday_compensation"}
    assert float(states["appliance_yesterday_compensation"].state) > 0  # earned
    assert await _own_rows(hass, OWN_HEAT_PUMP) == pump_rows


async def test_convert_feed_in_only_skips_the_plan(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """With no import mapping there is no grid meter to choose: straight to confirm."""
    entry = await _legacy_entry(
        hass, aioclient_mock, hass_storage, [_sub(EXPORT, "B1", sub_id="sub_export")]
    )
    flow = await _fix_flow(hass)
    assert flow["step_id"] == "confirm"
    assert flow["description_placeholders"]["plan"] == (
        f"- Appliance **{EXPORT}**: {EXPORT}"  # no stored name: the entity ID
    )
    await _configure(hass, flow, {})
    await hass.async_block_till_done()
    [appliance] = entry.subentries.values()
    assert appliance.data["name"] == EXPORT


async def test_convert_aborts(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """A Store move that doesn't read back changes nothing; nothing left to convert; not
    loaded."""
    entry = await _legacy_entry(hass, aioclient_mock, hass_storage, [_sub(SENSOR, "E1")])
    sensors = list(entry.runtime_data.manager.own_sensors)
    with patch.object(meters, "async_rename_chains"):
        flow = await _fix_flow(hass)
        flow = await _configure(hass, flow, {"grid_import": "sub_house", "grid_name": "Mains"})
        flow = await _configure(hass, flow, {})
    assert flow["type"] is FlowResultType.ABORT
    assert flow["reason"] == "convert_failed"
    assert "did not move" in flow["description_placeholders"]["reason"]
    await hass.async_block_till_done()
    assert list(entry.subentries) == ["sub_house"]
    assert entry.runtime_data.manager.own_sensors == sensors
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE) is not None

    flow = await _fix_flow(hass)
    flow = await _configure(hass, flow, {"grid_import": "sub_house", "grid_name": "Mains"})
    flow = await _configure(hass, flow, {})
    assert flow["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    [grid] = entry.subentries.values()
    # A mapping that is not a 2.1 one is never converted.
    with pytest.raises(ConversionFailed, match=r"only 2\.1"):
        await meters.async_convert(hass, entry, ConversionPlan(appliances=[grid.subentry_id]))
    meters.async_update_convert_issue(hass, entry, ())  # no channel: nothing to offer
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE) is None
    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE,
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="convert_own_sensors",
        data={"entry_id": ENTRY_ID},
    )  # a stale issue
    flow = await _fix_flow(hass)
    assert (flow["type"], flow["reason"]) == (FlowResultType.ABORT, "nothing_to_convert")
    await hass.config_entries.async_unload(entry.entry_id)
    flow = await _fix_flow(hass)
    assert (flow["type"], flow["reason"]) == (FlowResultType.ABORT, "not_loaded")


async def test_convert_sub_entries_read_back(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Sub-entries that don't read back as changed are reported."""
    entry = await _legacy_entry(hass, aioclient_mock, hass_storage, [_sub(SENSOR, "E1")])
    with (
        patch.object(hass.config_entries, "async_remove_subentry"),
        patch.object(hass.config_entries, "async_add_subentry"),
        pytest.raises(ConversionFailed) as err,
    ):
        await meters.async_convert(hass, entry, ConversionPlan(appliances=["sub_house"]))
    assert "sub_house is still configured" in str(err.value)
    assert "House" not in str(err.value) and "was not added" in str(err.value)


async def test_unconverted_mappings_keep_working(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """2.1 mappings left as they are keep their statistic, entity ID and reconciliation;
    the issue stays until they are converted (it can be ignored in the UI)."""
    entry = await _legacy_entry(
        hass, aioclient_mock, hass_storage, [_sub(SENSOR, "E1", name="House meter")]
    )
    await async_wait_recording_done(hass)
    assert len(await _own_rows(hass, OWN)) == 48
    state = hass.states.get("sensor.house_meter_own_sensor_reconciliation")
    assert state is not None and state.state != "unknown"
    meta = await hass.async_add_executor_job(get_metadata, hass)
    assert meta[OWN][1]["name"] == "Amber own house_energy cost"
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE) is not None
