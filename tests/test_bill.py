"""Milestone 10: the bill estimate (DESIGN section 19)."""

from datetime import UTC, date, datetime, timedelta
import math
from typing import Any

from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.amber_energy_dashboard import bill as bill_mod, importer
from custom_components.amber_energy_dashboard.const import (
    CONF_BILLING_DAY,
    CONF_FIXED_STATISTIC,
    CONF_GST_PERCENT,
    CONF_SCHEDULE_MODE,
    CONF_USAGE_MODE,
    DOMAIN,
)
from custom_components.amber_energy_dashboard.importer import HOUR
from custom_components.amber_energy_dashboard.statistics import ChannelConfig, build_specs

from .fake_amber import FakeAmber
from .synthetic import SITE_ID, expected_totals, make_usage_day, site_json
from .test_manager import (  # noqa: F401 - fixtures are used by name
    ALL_IDS,
    ENTRY_ID,
    NOW,
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
    sydney,
)

E1, E1_COST, B1, B1_COMP, NET = ALL_IDS
FIXED = f"{DOMAIN}:{SITE_ID.lower()}_e1_cost_incl_fixed"
CHARGES = {"daily_supply": 1.0871, "amber_subscription": 0.7471}
BILL = {CONF_BILLING_DAY: 20, **CHARGES, "other_daily": 0.0, CONF_GST_PERCENT: 10.0}
DAILY_FIXED = sum(CHARGES.values()) * 1.1
SENSOR = "sensor.amber_energy_dashboard_"
CHANNELS = (ChannelConfig("E1", "general", "EA116"), ChannelConfig("B1", "feedIn"))


@pytest.fixture(autouse=True)
async def _sydney(sydney: None) -> None:
    """Every test runs in Australia/Sydney."""


def _day_money(day: date) -> tuple[float, float]:
    """Independent (import cost, compensation) in AUD for a synthetic day."""
    totals = expected_totals(make_usage_day(day))
    return totals["E1"]["cents"] / 100, -totals["B1"]["cents"] / 100


async def _entry(
    hass, aioclient_mock, hass_storage, *, days: int = 10, options: dict | None = None
) -> Any:
    _preload_store(hass_storage, retention_days=days)
    entry = await _setup_entry(
        hass,
        aioclient_mock,
        FakeAmber(TODAY - timedelta(days=60), YESTERDAY),
        options={CONF_SCHEDULE_MODE: "automatic", **BILL, **(options or {})},
    )
    await _run(hass)
    await hass.async_block_till_done()
    return entry


# --- cycles and the pure estimate ------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "billing_day", "start", "end"),
    [
        (date(2026, 9, 27), 28, date(2026, 8, 28), date(2026, 9, 27)),  # the acceptance cycle
        (date(2026, 9, 28), 28, date(2026, 9, 28), date(2026, 10, 27)),
        (date(2026, 1, 5), 28, date(2025, 12, 28), date(2026, 1, 27)),  # across a year
        (date(2026, 2, 28), 28, date(2026, 2, 28), date(2026, 3, 27)),  # February
        (date(2028, 2, 29), 1, date(2028, 2, 1), date(2028, 2, 29)),  # a leap day
        (date(2026, 3, 1), 1, date(2026, 3, 1), date(2026, 3, 31)),
        (date(2026, 12, 31), 15, date(2026, 12, 15), date(2027, 1, 14)),
    ],
)
def test_cycle_for(day: date, billing_day: int, start: date, end: date) -> None:
    assert bill_mod.cycle_for(day, billing_day) == (start, end)


def _record(cost: float, comp: float) -> dict[str, Any]:
    return {"status": "imported", "totals": {E1: 1.0, E1_COST: cost, B1: 1.0, B1_COMP: comp}}


def test_estimate_from_day_records() -> None:
    """Usage plus fixed charges with GST minus export credit, over the imported days; a
    day missing inside the covered range still bears its fixed charges; the projection
    scales usage per imported day to the whole cycle."""
    settings = bill_mod.BillSettings(20, CHARGES)
    days: dict[date, Any] = {date(2026, 9, d): _record(3.0, 0.5) for d in (21, 22, 24)}
    days[date(2026, 9, 23)] = {"status": "skipped_gap"}
    specs = build_specs(SITE_ID, CHANNELS)

    result = bill_mod.estimate(
        settings, days, specs, CHANNELS, cycle_day=date(2026, 9, 25), today=date(2026, 9, 25)
    )

    fixed_line = {name: round(rate * 4 * 1.1, 4) for name, rate in CHARGES.items()}
    assert result == {
        "cycle_start": "2026-09-20",
        "cycle_end": "2026-10-19",
        "days_in_cycle": 30,
        "days_into_cycle": 6,
        "gst_percent": 10.0,
        "data_from": "2026-09-21",
        "data_through": "2026-09-24",
        "days_elapsed": 4,
        "days_imported": 3,
        "missing_days": ["2026-09-23"],
        "complete_from_cycle_start": False,
        "lines": {"usage": 9.0, "export_credit": -1.5, **fixed_line},
        "bill_to_date": round(9.0 - 1.5 + DAILY_FIXED * 4, 2),
        "projected_bill": round((7.5 / 3) * 30 + DAILY_FIXED * 30, 2),
        "average_cost_per_day": round((7.5 + DAILY_FIXED * 4) / 4, 2),
    }


def test_estimate_without_data_and_after_the_cycle() -> None:
    settings = bill_mod.BillSettings(20, {}, gst_percent=0.0)
    specs = build_specs(SITE_ID, CHANNELS)
    empty = bill_mod.estimate(
        settings, {}, specs, CHANNELS, cycle_day=date(2026, 9, 20), today=date(2026, 9, 20)
    )
    assert (empty["bill_to_date"], empty["projected_bill"], empty["days_into_cycle"]) == (
        None,
        None,
        1,
    )
    assert empty["lines"] == {}
    past = bill_mod.estimate(
        settings,
        {date(2026, 8, 20): _record(2.0, 0.0)},
        specs,
        CHANNELS,
        cycle_day=date(2026, 8, 25),
        today=date(2026, 10, 1),
    )
    assert past["days_into_cycle"] == 31  # capped at the cycle's length
    assert past["lines"] == {"usage": 2.0, "export_credit": -0.0}  # no fixed charges entered
    assert past["complete_from_cycle_start"] is True
    future = bill_mod.estimate(
        settings, {}, specs, CHANNELS, cycle_day=date(2026, 11, 25), today=date(2026, 10, 1)
    )
    assert future["days_into_cycle"] == 0


# --- sensors and the service -----------------------------------------------------------


async def test_bill_sensors_match_the_imported_days(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Billing day 20, 10 imported days: the cycle 2026-09-20..10-19 has 6 of them. The
    figures equal an independent sum of the synthetic records plus the charges."""
    await _entry(hass, aioclient_mock, hass_storage)
    days = [date(2026, 9, d) for d in range(20, 26)]
    cost = math.fsum(_day_money(d)[0] for d in days)
    comp = math.fsum(_day_money(d)[1] for d in days)

    state = hass.states.get(f"{SENSOR}bill_to_date")
    assert float(state.state) == pytest.approx(cost - comp + DAILY_FIXED * 6, abs=0.006)
    attrs = state.attributes
    assert attrs["lines"]["usage"] == pytest.approx(cost, abs=1e-4)
    assert attrs["lines"]["export_credit"] == pytest.approx(-comp, abs=1e-4)
    assert attrs["lines"]["daily_supply"] == pytest.approx(1.0871 * 6 * 1.1, abs=1e-4)
    assert (attrs["cycle_start"], attrs["cycle_end"], attrs["days_in_cycle"]) == (
        "2026-09-20",
        "2026-10-19",
        30,
    )
    assert (attrs["data_from"], attrs["data_through"], attrs["days_elapsed"]) == (
        "2026-09-20",
        "2026-09-25",
        6,
    )
    assert attrs["complete_from_cycle_start"] is True
    assert attrs["unit_of_measurement"] == "AUD"
    projected = hass.states.get(f"{SENSOR}projected_bill")
    assert float(projected.state) == pytest.approx(
        (cost - comp) / 6 * 30 + DAILY_FIXED * 30, abs=0.006
    )
    assert hass.states.get(f"{SENSOR}days_into_billing_cycle").state == "7"
    average = hass.states.get(f"{SENSOR}average_cost_per_day")
    assert float(average.state) == pytest.approx((cost - comp) / 6 + DAILY_FIXED, abs=0.006)
    assert average.attributes["unit_of_measurement"] == "AUD/d"

    # A past cycle through the service: 2026-08-20..09-19 holds 09-16..09-19 only.
    response = await hass.services.async_call(
        DOMAIN, "bill_estimate", {"date": "2026-09-01"}, blocking=True, return_response=True
    )
    assert (response["cycle_start"], response["data_from"], response["data_through"]) == (
        "2026-08-20",
        "2026-09-16",
        "2026-09-19",
    )
    assert response["complete_from_cycle_start"] is False
    assert response["days_into_cycle"] == 31
    today = await hass.services.async_call(
        DOMAIN, "bill_estimate", {}, blocking=True, return_response=True
    )
    assert today["bill_to_date"] == float(state.state)


async def test_cycle_starting_mid_import(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """A cycle that began before the integration's first day: only imported days count,
    and the attributes say where the data starts and runs to."""
    await _entry(hass, aioclient_mock, hass_storage, days=3, options={CONF_BILLING_DAY: 10})
    attrs = hass.states.get(f"{SENSOR}bill_to_date").attributes
    assert (attrs["cycle_start"], attrs["data_from"], attrs["data_through"]) == (
        "2026-09-10",
        "2026-09-23",
        "2026-09-25",
    )
    assert attrs["days_elapsed"] == 3
    assert attrs["complete_from_cycle_start"] is False


async def test_daylight_saving_boundary_uses_nem_days(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Billing day 26 in summer: at 00:30 local (AEDT) on the 26th it is still 23:30 AEST
    on the 25th, so the old cycle continues, as on Amber's bill; at 01:00 local the new
    cycle starts."""
    await _entry(hass, aioclient_mock, hass_storage, options={CONF_BILLING_DAY: 26})
    manager = hass.config_entries.async_get_entry(ENTRY_ID).runtime_data.manager
    clock.now = datetime(2026, 10, 25, 13, 30, tzinfo=UTC)  # 2026-10-26 00:30 AEDT
    assert manager.snapshot()["bill"]["cycle_start"] == "2026-09-26"
    assert manager.snapshot()["bill"]["days_into_cycle"] == 30
    clock.now = datetime(2026, 10, 25, 14, 0, tzinfo=UTC)  # 01:00 AEDT = 00:00 AEST
    assert manager.snapshot()["bill"]["cycle_start"] == "2026-10-26"


@pytest.mark.parametrize("mode", ["pricing", "recovery"])
async def test_bill_in_other_usage_modes(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    clock: Clock,
    hass_storage: dict,
    mode: str,
) -> None:
    """Pricing-only imports no usage: the sensors are unknown and the service refuses.
    Recovery-only uses whatever days were imported (here by the first run)."""
    entry = await _entry(hass, aioclient_mock, hass_storage)
    manager = entry.runtime_data.manager
    manager.usage_mode = mode
    manager._publish()
    await hass.async_block_till_done()
    state = hass.states.get(f"{SENSOR}bill_to_date")
    if mode == "pricing":
        assert state.state == "unknown"
        assert state.attributes.get("lines") is None
        with pytest.raises(ServiceValidationError, match="Pricing-only"):
            await hass.services.async_call(
                DOMAIN, "bill_estimate", {}, blocking=True, return_response=True
            )
    else:
        assert state.attributes["data_through"] == YESTERDAY.isoformat()


async def test_service_without_a_billing_day(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    _preload_store(hass_storage, retention_days=2)
    await _setup_entry(hass, aioclient_mock, FakeAmber(TODAY - timedelta(days=30), YESTERDAY))
    assert hass.states.get(f"{SENSOR}bill_to_date") is None
    with pytest.raises(ServiceValidationError, match="Set a billing day"):
        await hass.services.async_call(
            DOMAIN, "bill_estimate", {}, blocking=True, return_response=True
        )


# --- options ---------------------------------------------------------------------------


async def test_options_turn_on_change_and_turn_off(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Turning the estimate on reloads and adds the sensors; changing the billing day
    mid-cycle recomputes at once (no reload); the import settings keep the bill options;
    turning it off reloads and removes the sensors."""
    _preload_store(hass_storage, retention_days=10)
    entry = await _setup_entry(
        hass, aioclient_mock, FakeAmber(TODAY - timedelta(days=60), YESTERDAY)
    )
    await _run(hass)
    assert hass.states.get(f"{SENSOR}bill_to_date") is None

    async def bill_step(values: dict[str, Any]) -> dict[str, Any]:
        flow = await hass.config_entries.options.async_init(entry.entry_id)
        assert "bill" in flow["menu_options"]
        flow = await hass.config_entries.options.async_configure(
            flow["flow_id"], {"next_step_id": "bill"}
        )
        assert flow["step_id"] == "bill"
        assert flow["description_placeholders"]["bill_help_url"].endswith(
            "INSTALL.md#finding-these-on-your-bill"
        )
        result = await hass.config_entries.options.async_configure(flow["flow_id"], values)
        await hass.async_block_till_done()
        return result

    result = await bill_step({CONF_BILLING_DAY: 20, **CHARGES})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_BILLING_DAY] == 20
    assert entry.options[CONF_GST_PERCENT] == 10.0
    assert entry.options["other_daily"] == 0.0
    assert entry.options[CONF_FIXED_STATISTIC] is False
    assert hass.states.get(f"{SENSOR}bill_to_date").attributes["cycle_start"] == "2026-09-20"

    manager = entry.runtime_data.manager
    await bill_step({CONF_BILLING_DAY: 24, **CHARGES, CONF_GST_PERCENT: 10})
    assert entry.runtime_data.manager is manager  # applied in place
    attrs = hass.states.get(f"{SENSOR}bill_to_date").attributes
    assert (attrs["cycle_start"], attrs["days_elapsed"]) == ("2026-09-24", 2)

    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "settings"}
    )
    await hass.config_entries.options.async_configure(
        flow["flow_id"], {CONF_SCHEDULE_MODE: "automatic", CONF_USAGE_MODE: "full"}
    )
    await hass.async_block_till_done()
    assert entry.options[CONF_BILLING_DAY] == 24  # kept by the import settings step

    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "bill"}
    )
    suggested = {
        str(key): key.description["suggested_value"]
        for key in flow["data_schema"].schema
        if key.description and "suggested_value" in key.description
    }
    assert suggested[CONF_BILLING_DAY] == 24
    assert suggested["daily_supply"] == 1.0871
    await hass.config_entries.options.async_configure(flow["flow_id"], {})
    await hass.async_block_till_done()
    assert CONF_BILLING_DAY not in entry.options
    assert "daily_supply" not in entry.options
    assert hass.states.get(f"{SENSOR}bill_to_date") is None
    assert (
        er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{SITE_ID}_bill_to_date") is None
    )


# --- the statistic including fixed charges ---------------------------------------------


async def test_cost_including_fixed_charges_statistic(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Built from our own cost statistic, no API calls: each hour is the cost plus the
    daily fixed charges (with GST) / 24, first hour included; the next day follows."""
    entry = await _entry(
        hass, aioclient_mock, hass_storage, days=4, options={CONF_FIXED_STATISTIC: True}
    )
    manager = entry.runtime_data.manager
    rows = await _rows(hass, [E1_COST, FIXED])
    cost, fixed = rows[E1_COST], rows[FIXED]
    assert len(fixed) == len(cost) == 4 * 24
    for ours, base in zip(fixed, cost, strict=True):
        assert ours["start"] == base["start"]
        assert ours["state"] == pytest.approx(base["state"] + DAILY_FIXED / 24, abs=2e-6)
    assert fixed[-1]["sum"] == pytest.approx(cost[-1]["sum"] + DAILY_FIXED * 4, abs=1e-5)
    assert manager.store.fixed_schedule == [{"from": None, "daily_incl_gst": round(DAILY_FIXED, 6)}]
    calls = len(aioclient_mock.mock_calls)
    assert await manager.async_sync_derived() == {
        FIXED: {"rows": 96, "written": 0, "from": None, "cleared": False}
    }
    assert len(aioclient_mock.mock_calls) == calls  # no API calls

    # The next day continues; new charges apply from the day after the last imported day.
    clock.now = NOW + timedelta(days=1)
    aioclient_mock.clear_requests()
    FakeAmber(TODAY - timedelta(days=60), TODAY).install(aioclient_mock, [site_json()])
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "bill"}
    )
    await hass.config_entries.options.async_configure(
        flow["flow_id"], {**BILL, "daily_supply": 2.0871, CONF_FIXED_STATISTIC: True}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    await _run(hass)
    rows = await _rows(hass, [E1_COST, FIXED])
    assert len(rows[FIXED]) == 5 * 24
    raised = DAILY_FIXED + 1.1
    assert manager.store.fixed_schedule[-1] == {
        "from": "2026-09-25T14:00:00+00:00",
        "daily_incl_gst": round(raised, 6),
    }
    expected = rows[E1_COST][-1]["sum"] + DAILY_FIXED * 4 + raised
    assert rows[FIXED][-1]["sum"] == pytest.approx(expected, abs=1e-5)
    assert rows[FIXED][-1]["state"] == pytest.approx(
        rows[E1_COST][-1]["state"] + raised / 24, abs=2e-6
    )


async def test_fixed_statistic_follows_history_and_rewrites(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """The statistic covers history copied before the integration's first day (daily lumps
    accrue a whole day of charges each), includes a controlled load, follows a rewritten
    cost day from that day on, and clears a stray row it no longer should have."""
    channels = (
        *((c.identifier, c.type, c.tariff) for c in CHANNELS),
        ("E2", "controlledLoad", None),
    )
    from .test_manager import _setup_entry as setup  # noqa: PLC0415

    _preload_store(hass_storage, retention_days=3)
    fake = FakeAmber(TODAY - timedelta(days=60), YESTERDAY)

    def with_controlled_load(day: date, records: list[dict[str, Any]]) -> None:
        records[:] = make_usage_day(day, channels)

    fake.mutate = with_controlled_load
    entry = await setup(
        hass,
        aioclient_mock,
        fake,
        site=site_json(channels),
        data={"channels": [{"identifier": i, "type": t, "tariff": tf} for i, t, tf in channels]},
        options={CONF_SCHEDULE_MODE: "automatic", **BILL},
    )
    assert (await _run(hass))["status"] == "caught_up"
    manager = entry.runtime_data.manager
    e2_cost = E1_COST.replace("_e1_", "_e2_")
    first = importer.nem_day_start(TODAY - timedelta(days=3))
    # Copied history before the first day: two daily lumps and the carry row.
    lumps = [
        {"start": first - timedelta(days=2), "state": 0.0, "sum": 50.0},
        {"start": first - timedelta(days=1), "state": 4.0, "sum": 54.0},
        {"start": first - HOUR, "state": 0.0, "sum": 54.0},
    ]
    async_add_external_statistics(hass, manager.ctx.specs[1].metadata(), lumps)
    await async_wait_recording_done(hass)
    # The integration's own rows continue from 0 here, which a re-base would fix; the
    # derived statistic just follows whatever the base rows are.
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "bill"}
    )
    await hass.config_entries.options.async_configure(
        flow["flow_id"], {**BILL, CONF_FIXED_STATISTIC: True}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    rows = await _rows(hass, [E1_COST, e2_cost, FIXED])
    fixed = rows[FIXED]
    assert fixed[0]["start"] == lumps[0]["start"].timestamp()
    assert fixed[0]["sum"] == pytest.approx(50.0 + DAILY_FIXED / 24, abs=1e-5)
    assert fixed[1]["state"] == pytest.approx(4.0 + DAILY_FIXED, abs=1e-5)  # a whole day
    assert len(fixed) == 3 + 3 * 24
    own = {r["start"]: r["state"] for r in rows[E1_COST]}
    cl = {r["start"]: r["state"] for r in rows[e2_cost]}
    last = fixed[-1]
    assert last["state"] == pytest.approx(
        own[last["start"]] + cl[last["start"]] + DAILY_FIXED / 24, abs=2e-6
    )

    # A rewritten cost day (as after a revision): the derived rows follow from that day.
    day_start = importer.nem_day_start(YESTERDAY)
    changed = [
        {
            **r,
            "start": datetime.fromtimestamp(r["start"], UTC),
            "state": r["state"] + 1.0,
            "sum": r["sum"] + 1.0 * (i + 1),
        }
        for i, r in enumerate(x for x in rows[E1_COST] if x["start"] >= day_start.timestamp())
    ]
    async_add_external_statistics(hass, manager.ctx.specs[1].metadata(), changed)
    await async_wait_recording_done(hass)
    result = await manager.async_sync_derived()
    assert result[FIXED]["written"] == 24
    assert result[FIXED]["from"] == day_start.isoformat()

    # A row the base statistics don't have: cleared and written again in full.
    stray = {"start": first - timedelta(days=5), "state": 1.0, "sum": 1.0}
    async_add_external_statistics(hass, manager.fixed_spec.metadata(), [stray])
    await async_wait_recording_done(hass)
    result = await manager.async_sync_derived()
    assert result[FIXED]["cleared"] is True
    assert result[FIXED]["written"] == 3 + 3 * 24
    rows = await _rows(hass, [FIXED])
    assert rows[FIXED][0]["start"] == lumps[0]["start"].timestamp()


async def test_fixed_statistic_not_written_in_pricing_mode(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    entry = await _entry(
        hass,
        aioclient_mock,
        hass_storage,
        days=2,
        options={CONF_FIXED_STATISTIC: True, CONF_USAGE_MODE: "pricing"},
    )
    assert entry.runtime_data.manager.fixed_spec is None
    assert FIXED not in await _rows(hass, [FIXED])


def test_no_fixed_statistic_without_a_general_channel() -> None:
    from custom_components.amber_energy_dashboard.statistics import (  # noqa: PLC0415
        fixed_cost_spec,
    )

    assert fixed_cost_spec(SITE_ID, [ChannelConfig("B1", "feedIn")]) is None
    spec = fixed_cost_spec(SITE_ID, CHANNELS)
    assert spec is not None
    assert spec.statistic_id == FIXED
    assert spec.name == "Amber import cost including fixed charges"


async def test_rc1_charges_migrate_to_the_bill_summary_fields(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """2.1.0-rc1's options (network, metering and subscription) are folded into the daily
    supply charge and the Amber subscription; other daily charges are kept; the estimate
    is unchanged."""
    _preload_store(hass_storage, retention_days=10)
    old = {
        CONF_SCHEDULE_MODE: "automatic",
        CONF_BILLING_DAY: 20,
        "network_daily": 0.7019,
        "metering_daily": 0.3852,
        "subscription_daily": 0.7471,
        "other_daily": 0.1,
        CONF_GST_PERCENT: 10.0,
    }
    entry = await _setup_entry(
        hass, aioclient_mock, FakeAmber(TODAY - timedelta(days=60), YESTERDAY), options=old
    )
    assert entry.minor_version == 2
    assert entry.options == {
        CONF_SCHEDULE_MODE: "automatic",
        CONF_BILLING_DAY: 20,
        "daily_supply": 1.0871,
        "amber_subscription": 0.7471,
        "other_daily": 0.1,
        CONF_GST_PERCENT: 10.0,
    }
    await _run(hass)
    await hass.async_block_till_done()
    lines = hass.states.get(f"{SENSOR}bill_to_date").attributes["lines"]
    assert lines["daily_supply"] == pytest.approx((0.7019 + 0.3852) * 6 * 1.1, abs=1e-4)
    assert lines["amber_subscription"] == pytest.approx(0.7471 * 6 * 1.1, abs=1e-4)
    assert lines["other_daily"] == pytest.approx(0.1 * 6 * 1.1, abs=1e-4)


def test_migrate_charges_without_rc1_fields() -> None:
    options = {CONF_BILLING_DAY: 28, "daily_supply": 1.0}
    assert bill_mod.migrate_charges(options) == options
    assert bill_mod.migrate_charges({"metering_daily": 0.4}) == {"daily_supply": 0.4}


async def test_newer_entry_version_is_refused(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock
) -> None:
    from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: PLC0415

    from custom_components.amber_energy_dashboard import async_migrate_entry  # noqa: PLC0415

    entry = MockConfigEntry(domain=DOMAIN, version=2, data={})
    assert await async_migrate_entry(hass, entry) is False


async def test_subscription_is_required_and_may_be_zero(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """The daily supply charge and the Amber subscription have no default: with a billing
    day they must be entered. 0 is valid (a free first-year subscription)."""
    _preload_store(hass_storage, retention_days=10)
    entry = await _setup_entry(
        hass, aioclient_mock, FakeAmber(TODAY - timedelta(days=60), YESTERDAY)
    )
    await _run(hass)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "bill"}
    )
    suggested = {
        str(k): k.description["suggested_value"]
        for k in flow["data_schema"].schema
        if k.description and "suggested_value" in k.description
    }
    assert suggested == {"other_daily": 0.0, CONF_GST_PERCENT: 10.0}  # no charge defaults
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {CONF_BILLING_DAY: 20, "daily_supply": 1.0871}
    )
    assert flow["errors"] == {"amber_subscription": "charge_required"}
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"], {CONF_BILLING_DAY: 20, "daily_supply": 1.0871, "amber_subscription": 0}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["amber_subscription"] == 0.0
    attrs = hass.states.get(f"{SENSOR}bill_to_date").attributes
    assert "amber_subscription" not in attrs["lines"]
    days = [date(2026, 9, d) for d in range(20, 26)]
    cost = math.fsum(_day_money(d)[0] for d in days)
    comp = math.fsum(_day_money(d)[1] for d in days)
    state = hass.states.get(f"{SENSOR}bill_to_date")
    assert float(state.state) == pytest.approx(cost - comp + 1.0871 * 1.1 * 6, abs=0.006)


@pytest.mark.parametrize(
    ("allowance", "credit_line", "total"), [(False, -2.1238, 168.96), (True, -5.3749, 165.71)]
)
def test_september_acceptance_figures(allowance: bool, credit_line: float, total: float) -> None:
    """The September 2026 cycle with the migrated fields (daily supply 1.0871, Amber
    subscription 0.7471, GST 10 %), from day totals that add up to the real cycle's:
    usage $108.542390, compensation $2.123809, export penalty $3.251076 (all refunded
    within the allowance). Without the allowance $168.96, with it $165.71."""
    settings = bill_mod.BillSettings(
        28,
        bill_mod.migrate_charges(
            {"network_daily": 0.7019, "metering_daily": 0.3852, "subscription_daily": 0.7471}
        ),
    )
    assert settings.charges == {"daily_supply": 1.0871, "amber_subscription": 0.7471}
    cycle = [date(2026, 8, 28) + timedelta(days=i) for i in range(31)]
    days = {d: _record(108.542390 / 31, 2.123809 / 31) for d in cycle}
    export = {d: (3.251076 / 31, 3.251076 / 31) for d in cycle} if allowance else None
    result = bill_mod.estimate(
        settings,
        days,
        build_specs(SITE_ID, CHANNELS),
        CHANNELS,
        cycle_day=date(2026, 9, 27),
        today=date(2026, 10, 9),
        export=export,
    )
    assert result["bill_to_date"] == total
    assert result["lines"]["usage"] == 108.5424
    assert result["lines"]["daily_supply"] == 37.0701
    assert result["lines"]["amber_subscription"] == 25.4761
    expected_credit = -(2.123809 + 3.251076) if allowance else credit_line
    assert result["lines"]["export_credit"] == pytest.approx(expected_credit, abs=1e-4)
