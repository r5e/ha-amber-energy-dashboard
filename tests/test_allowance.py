"""Milestone 11: the export allowance of a two-way network tariff (DESIGN section 20).

The synthetic days are shaped like the N61 data in reports/billing-reconciliation-2026-09.md:
the general channel carries the tariff period (solarSponge 10:00-14:00, peak on weekdays
16:00-20:00, otherwise offPeak), and the feed-in price is 0.97404 x spot plus the network
component (-1.86 c in solarSponge, +3.47 c in peak).
"""

from datetime import date, datetime, timedelta
import math
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.amber_energy_dashboard import (
    allowance_from_options,
    bill_from_options,
    export as export_mod,
)
from custom_components.amber_energy_dashboard.api import _parse_usage
from custom_components.amber_energy_dashboard.const import (
    CONF_ALLOWANCE,
    CONF_ALLOWANCE_KWH,
    CONF_ALLOWANCE_TOTALLING,
    CONF_BILLING_DAY,
    CONF_PENALTY_PERIOD,
    CONF_SCHEDULE_MODE,
    CONF_USAGE_MODE,
    DOMAIN,
)
from custom_components.amber_energy_dashboard.importer import nem_day_start

from .fake_amber import FakeAmber
from .synthetic import SITE_ID, make_usage_day, site_json
from .test_manager import (  # noqa: F401 - fixtures are used by name
    ALL_IDS,
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
ADJUSTED = f"{DOMAIN}:{SITE_ID.lower()}_b1_compensation_adjusted"
LOSS_FACTOR = 0.97404
PENALTY_C = 1.86
REWARD_C = 3.47
N61_CHANNELS = (("E1", "general", "N71"), ("B1", "feedIn", "N61"))
CHARGES = {"network_daily": 0.7019, "metering_daily": 0.3852, "subscription_daily": 0.7471}
BILL = {CONF_BILLING_DAY: 20, **CHARGES, "gst_percent": 10.0}
SENSOR = "sensor.amber_energy_dashboard_"
N61 = export_mod.KNOWN_TARIFFS[0]


@pytest.fixture(autouse=True)
async def _sydney(sydney: None) -> None:
    """Every test runs in Australia/Sydney."""


def _period(day: date, minute: int) -> str:
    if 10 * 60 <= minute < 14 * 60:
        return "solarSponge"
    if day.weekday() < 5 and 16 * 60 <= minute < 20 * 60:
        return "peak"
    return "offPeak"


def n61(day: date, records: list[dict[str, Any]], *, scale: float = 1.0) -> None:
    """Reshape a synthetic day into N61 form (in place)."""
    for r in records:
        start = datetime.fromisoformat(r["startTime"].replace("Z", "+00:00"))
        minute = int((start - nem_day_start(day)).total_seconds() // 60)
        period = _period(day, minute)
        if r["channelType"] == "general":
            r["tariffInformation"] = {"period": period, "season": "nonSummer"}
            continue
        spot = 6.0 + (minute % 97) / 10 - (4.0 if period == "solarSponge" else 0.0)
        component = {"solarSponge": -PENALTY_C, "peak": REWARD_C}.get(period, 0.0)
        earned = LOSS_FACTOR * spot + component
        r["kwh"] = round(r["kwh"] * scale, 3)
        r["spotPerKwh"] = round(spot, 5)
        r["perKwh"] = -round(earned, 5)
        r["cost"] = round(r["kwh"] * r["perKwh"], 4)


def _day(day: date, scale: float = 1.0) -> list[dict[str, Any]]:
    records = make_usage_day(day, N61_CHANNELS)
    n61(day, records, scale=scale)
    return records


def _window_kwh(day: date) -> float:
    """Independent window (solarSponge) export for a synthetic day."""
    records = _day(day)
    periods = {
        r["startTime"]: r["tariffInformation"]["period"]
        for r in records
        if r["channelType"] == "general"
    }
    return math.fsum(
        r["kwh"]
        for r in records
        if r["channelType"] == "feedIn" and periods[r["startTime"]] == "solarSponge"
    )


def _fake(scale: dict[date, float] | None = None) -> FakeAmber:
    fake = FakeAmber(TODAY - timedelta(days=60), YESTERDAY)

    def mutate(day: date, records: list[dict[str, Any]]) -> None:
        records[:] = _day(day, (scale or {}).get(day, 1.0))

    fake.mutate = mutate
    return fake


async def _entry(
    hass, aioclient_mock, hass_storage, *, days: int = 10, options: dict | None = None, **kw
) -> Any:
    _preload_store(hass_storage, retention_days=days)
    entry = await _setup_entry(
        hass,
        aioclient_mock,
        kw.pop("fake", None) or _fake(),
        site=site_json(N61_CHANNELS, network="Endeavour Energy"),
        data={
            "channels": [{"identifier": i, "type": t, "tariff": tf} for i, t, tf in N61_CHANNELS]
        },
        options={CONF_SCHEDULE_MODE: "automatic", **BILL, **(options or {})},
    )
    result = await _run(hass)
    assert result["status"] == "caught_up", result
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry


# --- detection and settings --------------------------------------------------------------


def test_detect_tariff() -> None:
    assert export_mod.detect_tariff("Endeavour Energy", ["N71", "N61"]) is N61
    assert export_mod.detect_tariff("endeavour energy", ["N61"]) is N61
    assert export_mod.detect_tariff("Ausgrid", ["N61"]) is None
    assert export_mod.detect_tariff("Endeavour Energy", ["N71", None]) is None
    assert export_mod.detect_tariff(None, ["N61"]) is None


def test_allowance_from_options() -> None:
    """On by default for a known tariff with a billing day; overrides apply; billing-
    period totalling needs the billing day; an unknown tariff needs the allowance."""
    bill = bill_from_options(BILL)
    on = allowance_from_options({}, N61, bill)
    assert on == export_mod.AllowanceSettings(8.0, "billing_period", "solarSponge", "peak", 20)
    assert allowance_from_options({}, N61, None) is None  # no billing day: off by default
    assert allowance_from_options({CONF_ALLOWANCE: False}, N61, bill) is None
    daily = {CONF_ALLOWANCE: True, CONF_ALLOWANCE_TOTALLING: "daily"}
    assert allowance_from_options(daily, N61, None).totalling == "daily"
    assert allowance_from_options({CONF_ALLOWANCE: True}, N61, None) is None
    assert allowance_from_options({CONF_ALLOWANCE: True}, None, bill) is None  # no kWh
    custom = allowance_from_options(
        {CONF_ALLOWANCE: True, CONF_ALLOWANCE_KWH: 5, CONF_PENALTY_PERIOD: "shoulder"}, None, bill
    )
    assert (custom.allowance_kwh_per_day, custom.penalty_period, custom.reward_period) == (
        5.0,
        "shoulder",
        "peak",
    )


# --- per-day aggregates (pure) -----------------------------------------------------------


def _records(day: date) -> list[Any]:
    return [_parse_usage(r) for r in _day(day)]


SETTINGS = export_mod.AllowanceSettings(8.0, "billing_period", "solarSponge", "peak", 20)


def test_day_aggregates_measure_the_rates() -> None:
    """Loss factor, penalty and reward rates measured from the data; window export per
    hour; the reward only on weekdays."""
    weekday = date(2026, 9, 24)  # a Thursday
    agg = export_mod.day_aggregates(
        _records(weekday), weekday, SETTINGS, channels=("E1", "B1"), fallback_loss_factor=None
    )
    assert agg["loss_factor"] == pytest.approx(LOSS_FACTOR, abs=1e-5)
    assert agg["penalty_rate"] == PENALTY_C
    assert agg["reward_rate"] == REWARD_C
    assert agg["window_kwh"] == pytest.approx(_window_kwh(weekday), abs=1e-4)
    assert sum(1 for h in agg["window_kwh_hours"] if h) == 4  # 10:00 to 14:00
    assert agg["penalty"] == pytest.approx(_window_kwh(weekday) * PENALTY_C / 100, abs=1e-6)
    assert agg["measured"] is True
    saturday = date(2026, 9, 26)
    agg = export_mod.day_aggregates(
        _records(saturday), saturday, SETTINGS, channels=("E1", "B1"), fallback_loss_factor=None
    )
    assert (agg["reward_kwh"], agg["reward_rate"], agg["reward"]) == (0.0, None, 0.0)


def test_day_aggregates_fallbacks() -> None:
    """No spot price big enough to measure the loss factor: the last known one is used;
    with none at all, the day is unmeasured. No feed-in records: no aggregates."""
    day = date(2026, 9, 24)
    low = _day(day)
    for r in low:
        if r["channelType"] == "feedIn":
            r["spotPerKwh"] = min(r["spotPerKwh"], 4.0)
    records = [_parse_usage(r) for r in low]
    agg = export_mod.day_aggregates(
        records, day, SETTINGS, channels=("E1", "B1"), fallback_loss_factor=0.95
    )
    assert agg["loss_factor"] == 0.95
    agg = export_mod.day_aggregates(
        records, day, SETTINGS, channels=("E1", "B1"), fallback_loss_factor=None
    )
    assert (agg["measured"], agg["penalty_rate"], agg["penalty"]) == (False, None, 0.0)
    assert agg["window_kwh"] > 0
    general = [r for r in records if r.channel_identifier == "E1"]
    assert (
        export_mod.day_aggregates(
            general, day, SETTINGS, channels=("E1", "B1"), fallback_loss_factor=None
        )
        is None
    )


def test_refunds_stop_at_the_allowance() -> None:
    """Window exports are set against the period's allowance in time order: refunds
    stop once it is used up; a new period starts again; daily totalling resets daily."""

    def agg(kwh_per_hour: float) -> dict[str, Any]:
        hours = [0.0] * 24
        for h in (0, 1, 2, 3):
            hours[h] = kwh_per_hour
        return {"window_kwh_hours": hours, "penalty_rate": 2.0, "penalty": kwh_per_hour * 4 * 0.02}

    small = export_mod.AllowanceSettings(1.0, "billing_period", "solarSponge", "peak", 20)
    days = {"2026-09-18": agg(3.0), "2026-09-19": agg(3.0), "2026-09-20": agg(3.0)}
    by_hour, periods = export_mod.refunds(small, days)
    august = periods["2026-08-20"]  # 31 days x 1 kWh = 31 kWh
    assert august["window_kwh"] == 24.0
    assert august["refund"] == pytest.approx(24.0 * 0.02)
    september = periods["2026-09-20"]
    assert september["allowance_kwh"] == 30.0
    assert september["refund"] == pytest.approx(12.0 * 0.02)

    tight = export_mod.AllowanceSettings(0.5, "billing_period", "solarSponge", "peak", 20)
    _, periods = export_mod.refunds(tight, {"2026-09-20": agg(5.0)})
    assert periods["2026-09-20"]["refund"] == pytest.approx(15.0 * 0.02)  # 15 of 20 kWh
    daily = export_mod.AllowanceSettings(10.0, "daily", "solarSponge", "peak", None)
    by_hour, periods = export_mod.refunds(daily, days)
    assert set(periods) == set(days)
    assert all(p["refund"] == pytest.approx(10.0 * 0.02) for p in periods.values())
    assert len(by_hour) == 3 * 4

    summary = export_mod.period_summary(
        small, days, date(2026, 9, 25), [date(2026, 9, 20), date(2026, 9, 21)]
    )
    assert summary["unadjusted_days"] == ["2026-09-21"]
    assert (summary["allowance_used_kwh"], summary["allowance_remaining_kwh"]) == (12.0, 18.0)
    empty = export_mod.period_summary(small, {}, date(2026, 10, 25), [])
    assert (empty["window_export_kwh"], empty["data_through"], empty["penalty_rate_c"]) == (
        0.0,
        None,
        None,
    )


# --- the integration -------------------------------------------------------------------


async def test_allowance_end_to_end(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Detected N61 and a billing day: on by default. The aggregates are measured for
    every imported day; the adjusted compensation adds the refunded penalty; the sensors
    and the bill estimate use it."""
    entry = await _entry(hass, aioclient_mock, hass_storage)
    manager = entry.runtime_data.manager
    assert manager.tariff is N61
    assert manager.store.tariff == {
        "network": "Endeavour Energy",
        "tariffs": {"E1": "N71", "B1": "N61"},
        "known": "Endeavour Energy N61",
    }
    days = [TODAY - timedelta(days=d) for d in range(10, 0, -1)]
    stored = manager.store.export_days
    assert sorted(stored) == [d.isoformat() for d in days]
    assert all(stored[d.isoformat()]["penalty_rate"] == PENALTY_C for d in days)

    # The adjusted compensation: billed compensation + the refund, hour by hour.
    rows = await _rows(hass, [B1_COMP, ADJUSTED])
    comp, adjusted = rows[B1_COMP], rows[ADJUSTED]
    assert len(adjusted) == len(comp) == 10 * 24
    total_window = math.fsum(_window_kwh(d) for d in days)
    refund = adjusted[-1]["sum"] - comp[-1]["sum"]
    assert refund == pytest.approx(total_window * PENALTY_C / 100, abs=1e-4)  # all within

    # Sensors for the current cycle (2026-09-20..10-19, 240 kWh): six days so far.
    cycle = [d for d in days if d >= date(2026, 9, 20)]
    window = math.fsum(_window_kwh(d) for d in cycle)
    used = hass.states.get(f"{SENSOR}export_allowance_used")
    assert float(used.state) == pytest.approx(window, abs=1e-3)
    assert used.attributes["allowance_kwh"] == 240.0
    assert used.attributes["penalty_rate_c"] == PENALTY_C
    assert used.attributes["reward_rate_c"] == REWARD_C
    assert used.attributes["unadjusted_days"] == []
    remaining = hass.states.get(f"{SENSOR}export_allowance_remaining")
    assert float(remaining.state) == pytest.approx(240.0 - window, abs=1e-3)
    assert float(hass.states.get(f"{SENSOR}export_charge_after_allowance").state) == 0.0

    # The bill estimate: the credit before the export charge, and a 0 charge line.
    lines = hass.states.get(f"{SENSOR}bill_to_date").attributes["lines"]
    penalty = window * PENALTY_C / 100
    billed = math.fsum(
        -sum(r["cost"] for r in _day(d) if r["channelType"] == "feedIn") / 100 for d in cycle
    )
    assert lines["network_export_charge"] == pytest.approx(0.0, abs=1e-4)
    assert lines["export_credit"] == pytest.approx(-(billed + penalty), abs=1e-3)

    # The action adds the allowance figures for the cycle.
    response = await hass.services.async_call(
        DOMAIN, "bill_estimate", {}, blocking=True, return_response=True
    )
    assert response["export_allowance"]["allowance_used_kwh"] == pytest.approx(window, abs=1e-3)

    # Nothing to measure on a run with no new day: no extra API calls.
    calls = len(aioclient_mock.mock_calls)
    await _run(hass)
    assert len(aioclient_mock.mock_calls) == calls


async def test_allowance_used_up(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """A small allowance (0.5 kWh a day, 15 kWh this cycle): refunds stop when the
    cycle's window export passes it; the rest is the export charge."""
    entry = await _entry(
        hass,
        aioclient_mock,
        hass_storage,
        options={CONF_ALLOWANCE: True, CONF_ALLOWANCE_KWH: 0.5},
    )
    cycle = [TODAY - timedelta(days=d) for d in range(6, 0, -1)]
    window = math.fsum(_window_kwh(d) for d in cycle)
    assert window > 15
    summary = entry.runtime_data.manager.snapshot()["allowance"]
    assert summary["allowance_used_kwh"] == 15.0
    assert summary["allowance_remaining_kwh"] == 0.0
    assert summary["refund"] == pytest.approx(15.0 * PENALTY_C / 100, abs=1e-4)
    assert summary["export_charge"] == pytest.approx((window - 15) * PENALTY_C / 100, abs=1e-3)
    lines = hass.states.get(f"{SENSOR}bill_to_date").attributes["lines"]
    assert lines["network_export_charge"] == pytest.approx(summary["export_charge"], abs=1e-4)


async def test_daily_totalling_without_a_billing_day(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Without a billing day the allowance can total daily; the sensors show yesterday."""
    entry = await _entry(
        hass,
        aioclient_mock,
        hass_storage,
        days=3,
        options={
            CONF_BILLING_DAY: None,
            CONF_ALLOWANCE: True,
            CONF_ALLOWANCE_TOTALLING: "daily",
            CONF_ALLOWANCE_KWH: 2.0,
        },
    )
    summary = entry.runtime_data.manager.snapshot()["allowance"]
    assert (summary["period_start"], summary["period_end"]) == (YESTERDAY.isoformat(),) * 2
    assert summary["allowance_kwh"] == 2.0
    assert summary["window_export_kwh"] == pytest.approx(_window_kwh(YESTERDAY), abs=1e-3)
    assert hass.states.get(f"{SENSOR}bill_to_date") is None


async def test_revised_day_is_measured_again(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """A revision (estimated data that changes) rewrites the usage; its aggregates are
    measured again, and the adjusted compensation follows."""
    scale: dict[date, float] = {}
    fake = _fake(scale)
    previous = fake.mutate

    def estimated(day: date, records: list[dict[str, Any]]) -> None:
        previous(day, records)
        if day == YESTERDAY:
            for r in records:
                r["quality"] = "estimated"

    fake.mutate = estimated
    entry = await _entry(hass, aioclient_mock, hass_storage, days=3, fake=fake)
    manager = entry.runtime_data.manager
    before = manager.store.export_days[YESTERDAY.isoformat()]["window_kwh"]
    scale[YESTERDAY] = 2.0
    clock.now = NOW + timedelta(days=1)  # revisions are checked once a day
    result = await _run(hass)
    assert result["revisions"]["changed"] == [YESTERDAY.isoformat()]
    assert result["export"]["measured"] == [YESTERDAY.isoformat()]
    after = manager.store.export_days[YESTERDAY.isoformat()]["window_kwh"]
    assert after == pytest.approx(before * 2, abs=0.01)
    rows = await _rows(hass, [B1_COMP, ADJUSTED])
    refund = rows[ADJUSTED][-1]["sum"] - rows[B1_COMP][-1]["sum"]
    total = math.fsum(v["window_kwh"] for v in manager.store.export_days.values())
    assert refund == pytest.approx(total * PENALTY_C / 100, abs=1e-4)


async def test_not_in_pricing_mode_or_without_feed_in(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    entry = await _entry(hass, aioclient_mock, hass_storage, days=2)
    manager = entry.runtime_data.manager
    assert manager.allowance_active
    manager.usage_mode = "pricing"
    assert not manager.allowance_active
    manager._publish()
    await hass.async_block_till_done()
    state = hass.states.get(f"{SENSOR}export_allowance_used")
    assert state.state == "unknown"
    assert "allowance_kwh" not in state.attributes
    manager.usage_mode = "full"
    manager.ctx = manager.ctx.__class__(
        manager.ctx.client,
        manager.ctx.site_id,
        manager.ctx.channels[:1],
        tuple(s for s in manager.ctx.specs if s.channel != "B1"),
        manager.ctx.lock,
    )
    assert not manager.allowance_active


async def test_allowance_options_step(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """The step shows the detection and suggests its rule; billing-period totalling needs
    the billing day; turning it off reloads and removes the sensors; the import settings
    keep the allowance's options."""
    entry = await _entry(hass, aioclient_mock, hass_storage, days=2)
    assert hass.states.get(f"{SENSOR}export_allowance_used") is not None

    async def step(values: dict[str, Any] | None) -> dict[str, Any]:
        flow = await hass.config_entries.options.async_init(entry.entry_id)
        assert "allowance" in flow["menu_options"]
        flow = await hass.config_entries.options.async_configure(
            flow["flow_id"], {"next_step_id": "allowance"}
        )
        if values is None:
            return flow
        result = await hass.config_entries.options.async_configure(flow["flow_id"], values)
        await hass.async_block_till_done(wait_background_tasks=True)
        return result

    form = await step(None)
    assert form["description_placeholders"]["detected"].startswith(
        "Detected Endeavour Energy N61: 8 kWh a day free"
    )
    assert form["description_placeholders"]["billing_day"] == "20"
    suggested = {
        str(k): k.description["suggested_value"]
        for k in form["data_schema"].schema
        if k.description and "suggested_value" in k.description
    }
    assert suggested == {
        CONF_ALLOWANCE: True,
        CONF_ALLOWANCE_KWH: 8.0,
        CONF_ALLOWANCE_TOTALLING: "billing_period",
        CONF_PENALTY_PERIOD: "solarSponge",
    }
    result = await step({CONF_ALLOWANCE: True, CONF_ALLOWANCE_KWH: 0})
    assert result["errors"] == {CONF_ALLOWANCE_KWH: "allowance_required"}

    result = await step({CONF_ALLOWANCE: False})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_ALLOWANCE] is False
    assert hass.states.get(f"{SENSOR}export_allowance_used") is None
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{SITE_ID}_allowance_used") is None

    await step({CONF_ALLOWANCE: True, CONF_ALLOWANCE_KWH: 6, CONF_PENALTY_PERIOD: ""})
    assert entry.options[CONF_ALLOWANCE_KWH] == 6.0
    assert entry.options[CONF_PENALTY_PERIOD] == "solarSponge"
    assert entry.runtime_data.manager.allowance.allowance_kwh_per_day == 6.0

    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "settings"}
    )
    await hass.config_entries.options.async_configure(
        flow["flow_id"], {CONF_SCHEDULE_MODE: "automatic", CONF_USAGE_MODE: "full"}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.options[CONF_ALLOWANCE_KWH] == 6.0

    # Without a billing day, only daily totalling.
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "bill"}
    )
    await hass.config_entries.options.async_configure(flow["flow_id"], {})
    await hass.async_block_till_done(wait_background_tasks=True)
    result = await step({CONF_ALLOWANCE: True, CONF_ALLOWANCE_KWH: 8})
    assert result["errors"] == {CONF_ALLOWANCE_TOTALLING: "needs_billing_day"}


async def test_no_known_tariff(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """An unknown tariff: off by default, and the step says nothing was detected."""
    _preload_store(hass_storage, retention_days=2)
    entry = await _setup_entry(
        hass,
        aioclient_mock,
        FakeAmber(TODAY - timedelta(days=30), YESTERDAY),
        options={CONF_SCHEDULE_MODE: "automatic", **BILL},
    )
    manager = entry.runtime_data.manager
    assert manager.tariff is None
    assert manager.allowance is None
    assert manager.store.tariff["known"] is None
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "allowance"}
    )
    assert flow["description_placeholders"]["detected"].startswith("No known two-way tariff")


def test_no_adjusted_statistic_without_feed_in() -> None:
    from custom_components.amber_energy_dashboard.statistics import (  # noqa: PLC0415
        ChannelConfig,
        adjusted_compensation_spec,
    )

    assert adjusted_compensation_spec(SITE_ID, [ChannelConfig("E1", "general")]) is None
    spec = adjusted_compensation_spec(SITE_ID, [ChannelConfig("B1", "feedIn")])
    assert spec.statistic_id == ADJUSTED


async def test_days_without_feed_in_and_pruning(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """A day without feed-in records (a channel added later) has no aggregates; the
    stored aggregates are pruned like the day records."""
    from unittest.mock import patch  # noqa: PLC0415

    from custom_components.amber_energy_dashboard import manager as manager_mod  # noqa: PLC0415
    from custom_components.amber_energy_dashboard.storage import (  # noqa: PLC0415
        MAX_DAY_ENTRIES,
    )

    with patch.object(manager_mod, "export_day_aggregates", return_value=None):
        entry = await _entry(hass, aioclient_mock, hass_storage, days=2)
    store = entry.runtime_data.manager.store
    assert store.export_days == {}
    first = date(2025, 1, 1)
    for i in range(MAX_DAY_ENTRIES + 2):
        await store.async_set_export_day(first + timedelta(days=i), {"i": i})
    assert len(store.export_days) == MAX_DAY_ENTRIES
    assert min(store.export_days) == (first + timedelta(days=2)).isoformat()
