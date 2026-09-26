"""Milestone 5: usage modes, backfill, own-sensor cost, reconciliation, price series.

All data is synthetic. Sensor statistics (hourly and 5-minute) are imported straight
into the recorder, as HA would compile them for a real cumulative energy sensor.
"""

from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
import math
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.db_schema import Statistics, StatisticsShortTerm
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.amber_energy_dashboard import chains, importer
from custom_components.amber_energy_dashboard.api import RunBudget, _parse_usage
from custom_components.amber_energy_dashboard.const import (
    CONF_OWN_FALLBACK,
    CONF_PRICE_SERIES,
    CONF_SCHEDULE_MODE,
    CONF_USAGE_MODE,
    DOMAIN,
)

from .fake_amber import FakeAmber
from .synthetic import make_usage_day
from .test_manager import (  # noqa: F401 - fixtures are used by name
    ALL_IDS,
    E1,
    ENTRY_ID,
    PREFIX,
    TODAY,
    YESTERDAY,
    Clock,
    _assert_contiguous,
    _fast_verify,
    _no_real_timers,
    _preload_store,
    _rows,
    _run,
    _setup,
    _setup_entry,
    _store_key,
    clock,
)

SENSOR = "sensor.house_energy"
EXPORT = "sensor.solar_export"
OWN = f"{PREFIX}_own_house_energy_cost"
OWN_EXPORT = f"{PREFIX}_own_solar_export_cost"  # named "compensation"; ID per DESIGN
E1_PRICE, B1_PRICE = f"{PREFIX}_e1_price", f"{PREFIX}_b1_price"
FIVE = timedelta(minutes=5)
HOUR = timedelta(hours=1)


def _sub(entity_id: str, channel: str, sub_id: str = "sub_house") -> dict[str, Any]:
    return {
        "data": {"entity_id": entity_id, "channel": channel},
        "subentry_id": sub_id,
        "subentry_type": "own_sensor",
        "title": entity_id,
        "unique_id": entity_id,
    }


def _energy_like(day: date, channel: str, factor: float = 1.0) -> list[float]:
    """288 five-minute energies shaped like the synthetic Amber channel, times a factor."""
    recs = [r for r in make_usage_day(day) if r["channelIdentifier"] == channel]
    return [r["kwh"] * factor for r in recs]


async def _import_sensor(
    hass: HomeAssistant,
    entity_id: str,
    energies: dict[date, list[float]],
    *,
    short: bool = True,
    hourly: bool = True,
) -> None:
    """Import cumulative statistics for a sensor: 5-minute and/or hourly rows."""
    hass.states.async_set(
        entity_id,
        "0",
        {"device_class": "energy", "state_class": "total_increasing", "unit_of_measurement": "kWh"},
    )
    meta = {
        "has_sum": True,
        "mean_type": StatisticMeanType.NONE,
        "name": None,
        "source": "recorder",
        "statistic_id": entity_id,
        "unit_class": "energy",
        "unit_of_measurement": "kWh",
    }
    days = sorted(energies)
    first = importer.nem_day_start(days[0])
    short_rows, hour_rows = (
        [{"start": first - FIVE, "state": 0.0, "sum": 0.0}],
        [{"start": first - HOUR, "state": 0.0, "sum": 0.0}],
    )
    total = 0.0
    for day in days:
        start = importer.nem_day_start(day)
        for i, e in enumerate(energies[day]):
            total += e
            short_rows.append({"start": start + i * FIVE, "state": total, "sum": total})
            if i % 12 == 11:
                hour_rows.append({"start": start + (i // 12) * HOUR, "state": total, "sum": total})
    recorder = get_instance(hass)
    if short:
        recorder.async_import_statistics(meta, short_rows, StatisticsShortTerm)
    if hourly:
        recorder.async_import_statistics(meta, hour_rows, Statistics)
    await async_wait_recording_done(hass)


def _expected_own(day: date, channel: str, energies: list[float], sign: float = 1.0) -> list[float]:
    """Independent expectation: each interval's energy x its perKwh, summed hourly (AUD)."""
    recs = [_parse_usage(r) for r in make_usage_day(day) if r["channelIdentifier"] == channel]
    start = importer.nem_day_start(day)
    hours = [0.0] * 24
    for r in recs:
        i = int((r.start_time.replace(second=0) - start) / FIVE)
        hours[i // 12] += sign * energies[i] * r.per_kwh / 100
    return hours


async def _own_rows(hass: HomeAssistant, sid: str = OWN) -> list[dict]:
    return (await _rows(hass, [sid])).get(sid, [])


# --- own-sensor cost -----------------------------------------------------------------------


async def test_own_cost_precise_matches_interval_prices(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """5-minute energy x each interval's billed perKwh, summed hourly, from the boundary on."""
    _preload_store(hass_storage, retention_days=3)
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    days = [TODAY - timedelta(days=d) for d in (3, 2, 1)]
    energies = {d: _energy_like(d, "E1", 1.02) for d in days}
    await _import_sensor(hass, SENSOR, energies)
    entry = await _setup_entry(hass, aioclient_mock, fake, subentries=[_sub(SENSOR, "E1")])

    result = await _run(hass)

    chain = result["chains"]["own:sub_house"]
    assert chain["status"] == "caught_up"
    assert chain["written"] == [d.isoformat() for d in days]
    assert chain["lower_precision"] == []
    rows = await _own_rows(hass)
    assert len(rows) == 72
    expected = [h for d in days for h in _expected_own(d, "E1", energies[d])]
    for row, want in zip(rows, expected, strict=True):
        assert row["state"] == pytest.approx(want, abs=1e-6)
    assert rows[-1]["sum"] == pytest.approx(math.fsum(expected), abs=1e-5)
    store = entry.runtime_data.manager.store
    record = store.as_dict()["chains"]["own:sub_house"]["days"][days[0].isoformat()]
    assert record["lower_precision"] is False
    assert record["energy_kwh"] == pytest.approx(math.fsum(energies[days[0]]), abs=1e-6)
    meta = await hass.async_add_executor_job(get_metadata, hass)
    assert meta[OWN][1]["unit_of_measurement"] == "AUD" and meta[OWN][1]["has_sum"] is True


def test_interval_alignment_one_second_offset() -> None:
    """startTime 14:05:01Z lines up with the 14:05:00Z five-minute bucket."""
    day = TODAY - timedelta(days=1)
    raw = [r for r in make_usage_day(day) if r["channelIdentifier"] == "E1"]
    recs = [_parse_usage(r) for r in raw]
    energies = [0.0] * 288
    energies[37] = 1.0  # only bucket 37 (hour 3, minute 5) has energy
    result = chains.own_cost_day(recs, day, energies, None, feed_in=False, allow_fallback=True)
    assert raw[37]["startTime"].endswith(":01Z")
    assert result.amounts[3] == pytest.approx(recs[37].per_kwh / 100)
    assert sum(1 for a in result.amounts if a) == 1


async def test_own_cost_hourly_fallback_is_flagged(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Without 5-minute data: hourly energy x the hour's mean price, flagged lower_precision."""
    _preload_store(hass_storage, retention_days=2)
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    days = [TODAY - timedelta(days=2), YESTERDAY]
    energies = {d: _energy_like(d, "E1") for d in days}
    await _import_sensor(hass, SENSOR, energies, short=False)
    entry = await _setup_entry(hass, aioclient_mock, fake, subentries=[_sub(SENSOR, "E1")])

    result = await _run(hass)

    assert result["chains"]["own:sub_house"]["lower_precision"] == [d.isoformat() for d in days]
    rows = await _own_rows(hass)
    day = days[0]
    recs = [_parse_usage(r) for r in make_usage_day(day) if r["channelIdentifier"] == "E1"]
    for h in range(24):
        hour_energy = math.fsum(energies[day][h * 12 : h * 12 + 12])
        mean_price = math.fsum(r.per_kwh for r in recs[h * 12 : h * 12 + 12]) / 12
        assert rows[h]["state"] == pytest.approx(hour_energy * mean_price / 100, abs=1e-6)
    record = entry.runtime_data.manager.store.as_dict()["chains"]["own:sub_house"]["days"][
        day.isoformat()
    ]
    assert record["lower_precision"] is True


async def test_own_cost_fallback_off_skips_days(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """With the fallback off, days without 5-minute data are skipped and recorded."""
    _preload_store(hass_storage, retention_days=2)
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    days = [TODAY - timedelta(days=2), YESTERDAY]
    await _import_sensor(hass, SENSOR, {d: _energy_like(d, "E1") for d in days}, short=False)
    entry = await _setup_entry(
        hass,
        aioclient_mock,
        fake,
        subentries=[_sub(SENSOR, "E1")],
        options={CONF_SCHEDULE_MODE: "automatic", CONF_OWN_FALLBACK: False},
    )

    result = await _run(hass)

    assert result["chains"]["own:sub_house"]["skipped"] == [d.isoformat() for d in days]
    chain = entry.runtime_data.manager.store.as_dict()["chains"]["own:sub_house"]
    assert {v["status"] for v in chain["days"].values()} == {"skipped_no_short_term"}
    assert await _own_rows(hass) == []


async def test_feed_in_mapping_is_positive_when_earned(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """A sensor mapped to feed-in records compensation, positive when earning."""
    _preload_store(hass_storage, retention_days=1)
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    energies = {YESTERDAY: _energy_like(YESTERDAY, "B1")}
    await _import_sensor(hass, EXPORT, energies)
    await _setup_entry(hass, aioclient_mock, fake, subentries=[_sub(EXPORT, "B1", "sub_export")])

    await _run(hass)

    rows = await _own_rows(hass, OWN_EXPORT)
    expected = _expected_own(YESTERDAY, "B1", energies[YESTERDAY], sign=-1.0)
    assert rows[-1]["sum"] == pytest.approx(math.fsum(expected), abs=1e-6)
    assert rows[-1]["sum"] > 0


async def test_own_sensor_backfills_from_history_start_and_continues(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """A new mapping starts at the later of the sensor's history and the boundary,
    then the daily run extends it with sums carried on."""
    _preload_store(hass_storage, retention_days=10)
    fake = FakeAmber(TODAY - timedelta(days=30), TODAY + timedelta(days=5))
    history = [TODAY - timedelta(days=d) for d in (3, 2, 1)]
    await _import_sensor(hass, SENSOR, {d: _energy_like(d, "E1") for d in history})
    await _setup_entry(hass, aioclient_mock, fake, subentries=[_sub(SENSOR, "E1")])

    first = await _run(hass)
    assert first["chains"]["own:sub_house"]["written"] == [d.isoformat() for d in history]
    before = await _own_rows(hass)

    clock.now += timedelta(days=1)
    await _import_sensor(hass, SENSOR, {d: _energy_like(d, "E1") for d in [*history, TODAY]})
    second = await _run(hass)
    assert second["chains"]["own:sub_house"]["written"] == [TODAY.isoformat()]
    rows = await _own_rows(hass)
    assert rows[:72] == before
    assert rows[72]["sum"] == pytest.approx(before[-1]["sum"] + rows[72]["state"], abs=1e-6)
    assert len(rows) == 96


async def test_sensor_without_statistics_waits(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """A mapped sensor with no statistics yet waits instead of guessing a start."""
    _preload_store(hass_storage, retention_days=2)
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    await _setup_entry(hass, aioclient_mock, fake, subentries=[_sub(SENSOR, "E1")])
    result = await _run(hass)
    assert result["chains"]["own:sub_house"]["status"] == "waiting_for_data"
    assert result["status"] == "caught_up"  # usage itself is fine


# --- reconciliation ----------------------------------------------------------------------------


async def test_reconciliation_shows_own_minus_amber(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """A general-mapped sensor reading 2 % high shows +2 % against Amber's general kWh."""
    _preload_store(hass_storage, retention_days=2)
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    days = [TODAY - timedelta(days=2), YESTERDAY]
    await _import_sensor(hass, SENSOR, {d: _energy_like(d, "E1", 1.02) for d in days})
    entry = await _setup_entry(hass, aioclient_mock, fake, subentries=[_sub(SENSOR, "E1")])
    await _run(hass)
    await hass.async_block_till_done()

    states = [s for s in hass.states.async_all("sensor") if "reconciliation" in s.entity_id]
    assert len(states) == 1
    state = states[0]
    amber = math.fsum(r["kwh"] for r in make_usage_day(YESTERDAY) if r["channelIdentifier"] == "E1")
    assert float(state.state) == pytest.approx(amber * 0.02, abs=1e-3)
    assert state.attributes["difference_percent"] == pytest.approx(2.0, abs=0.01)
    assert state.attributes["amber_kwh"] == pytest.approx(amber, abs=1e-3)
    assert state.attributes["own_kwh"] == pytest.approx(amber * 1.02, abs=1e-3)
    assert state.attributes["date"] == YESTERDAY.isoformat()
    assert "state_class" not in state.attributes
    # Only in Full mode.
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        flow["flow_id"], {CONF_SCHEDULE_MODE: "automatic", CONF_USAGE_MODE: "pricing"}
    )
    await hass.async_block_till_done()
    assert hass.states.get(state.entity_id).state == "unknown"


# --- price series ----------------------------------------------------------------------------


async def test_price_series_optional_hourly_mean(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Off by default; when on, hourly mean/min/max of perKwh in AUD/kWh per channel."""
    _preload_store(hass_storage, retention_days=2)
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    entry = await _setup_entry(hass, aioclient_mock, fake)
    await _run(hass)
    meta = await hass.async_add_executor_job(get_metadata, hass)
    assert E1_PRICE not in meta

    flow = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        flow["flow_id"], {CONF_SCHEDULE_MODE: "automatic", CONF_PRICE_SERIES: True}
    )
    await hass.async_block_till_done()
    result = await _run(hass)

    assert result["chains"]["price"]["written"] == [
        (TODAY - timedelta(days=2)).isoformat(),
        YESTERDAY.isoformat(),
    ]
    await async_wait_recording_done(hass)
    rows = await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2020, 1, 1, tzinfo=UTC),
        None,
        {E1_PRICE, B1_PRICE},
        "hour",
        None,
        {"mean", "min", "max"},
    )
    recs = [_parse_usage(r) for r in make_usage_day(YESTERDAY) if r["channelIdentifier"] == "E1"]
    last_hour = rows[E1_PRICE][-1]
    prices = [r.per_kwh / 100 for r in recs[-12:]]
    assert last_hour["mean"] == pytest.approx(math.fsum(prices) / 12, abs=1e-6)
    assert last_hour["min"] == pytest.approx(min(prices), abs=1e-6)
    assert last_hour["max"] == pytest.approx(max(prices), abs=1e-6)
    assert len(rows[B1_PRICE]) == 48
    meta = await hass.async_add_executor_job(get_metadata, hass)
    assert meta[E1_PRICE][1]["mean_type"] == StatisticMeanType.ARITHMETIC
    assert meta[E1_PRICE][1]["has_sum"] is False
    assert meta[E1_PRICE][1]["unit_of_measurement"] == "AUD/kWh"


# --- modes -------------------------------------------------------------------------------------


async def _set_mode(hass: HomeAssistant, entry, mode: str, **extra: Any) -> None:
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        flow["flow_id"], {CONF_SCHEDULE_MODE: "automatic", CONF_USAGE_MODE: mode, **extra}
    )
    await hass.async_block_till_done()


async def test_recovery_mode_has_no_schedule(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Recovery-only: no timer, run_now imports no usage, own sensors still extend."""
    _preload_store(hass_storage, retention_days=2)
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    days = [TODAY - timedelta(days=2), YESTERDAY]
    await _import_sensor(hass, SENSOR, {d: _energy_like(d, "E1") for d in days})
    entry = await _setup_entry(
        hass,
        aioclient_mock,
        fake,
        subentries=[_sub(SENSOR, "E1")],
        options={CONF_SCHEDULE_MODE: "automatic", CONF_USAGE_MODE: "recovery"},
    )
    mgr = entry.runtime_data.manager
    assert mgr.next_run is None

    result = await _run(hass)

    assert result["imported_days"] == []
    assert (await _rows(hass)).get(E1, []) == []
    assert result["chains"]["own:sub_house"]["written"] == [d.isoformat() for d in days]
    assert mgr.caught_up is True


async def test_backfill_in_recovery_mode_ranges_and_gaps(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Backfilled ranges are written in order; days between them are 'not requested';
    filling them later rewrites the tail with continuous sums."""
    _preload_store(hass_storage, retention_days=12)
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    entry = await _setup_entry(
        hass,
        aioclient_mock,
        fake,
        options={CONF_SCHEDULE_MODE: "automatic", CONF_USAGE_MODE: "recovery"},
    )
    store = entry.runtime_data.manager.store

    async def backfill(a: int, b: int) -> dict:
        return await hass.services.async_call(
            DOMAIN,
            "backfill",
            {
                "start_date": (TODAY - timedelta(days=a)).isoformat(),
                "end_date": (TODAY - timedelta(days=b)).isoformat(),
            },
            blocking=True,
            return_response=True,
        )

    first = await backfill(10, 8)
    assert first["imported_days"] == [(TODAY - timedelta(days=d)).isoformat() for d in (10, 9, 8)]
    second = await backfill(4, 3)
    assert store.day(TODAY - timedelta(days=6))["status"] == "skipped_not_requested"
    rows = await _rows(hass)
    assert len(rows[E1]) == 5 * 24
    third = await backfill(7, 6)
    assert third["imported_days"] == [
        (TODAY - timedelta(days=7)).isoformat(),
        (TODAY - timedelta(days=6)).isoformat(),
    ]
    rows = await _rows(hass)
    assert len(rows[E1]) == 7 * 24
    for sid in ALL_IDS:
        for prev, cur in pairwise(rows[sid]):
            assert cur["sum"] == pytest.approx(prev["sum"] + cur["state"], abs=2e-6)
    total = sum(
        math.fsum(
            r["kwh"]
            for r in make_usage_day(TODAY - timedelta(days=d))
            if r["channelIdentifier"] == "E1"
        )
        for d in (10, 9, 8, 7, 6, 4, 3)
    )
    assert rows[E1][-1]["sum"] == pytest.approx(total, abs=1e-6)
    assert second["status"] == "caught_up"
    assert (await _run(hass))["status"] == "caught_up"  # Guard 1 accepts the result


async def test_backfill_before_existing_data_rewrites_tail(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """In Full mode, backfilling before the first imported day re-derives later sums."""
    _preload_store(hass_storage, retention_days=4)
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    entry = await _setup_entry(hass, aioclient_mock, fake)
    await _run(hass)
    store = entry.runtime_data.manager.store
    store._data["retention_days"] = 10

    result = await hass.services.async_call(
        DOMAIN,
        "backfill",
        {
            "start_date": (TODAY - timedelta(days=8)).isoformat(),
            "end_date": (TODAY - timedelta(days=6)).isoformat(),
        },
        blocking=True,
        return_response=True,
    )

    assert result["imported_days"] == [(TODAY - timedelta(days=d)).isoformat() for d in (8, 7, 6)]
    rows = await _rows(hass)
    hours = [datetime.fromtimestamp(r["start"], UTC) for r in rows[E1]]
    assert (
        importer.nem_day_start(TODAY - timedelta(days=5)) not in hours
    )  # not requested: left empty
    for sid in ALL_IDS:
        for prev, cur in pairwise(rows[sid]):
            assert cur["sum"] == pytest.approx(prev["sum"] + cur["state"], abs=2e-6)
    assert store.day(YESTERDAY)["mode"] == "revision"


async def test_backfill_refusals(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Forward ranges in Full mode, inverted ranges and Pricing-only mode are refused;
    ranges older than retention are skipped as unavailable."""
    _preload_store(hass_storage, retention_days=3)
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    entry = await _setup_entry(hass, aioclient_mock, fake)
    fake.latest = TODAY - timedelta(days=3)
    await _run(hass)
    fake.latest = YESTERDAY

    def call(a: int, b: int):
        return hass.services.async_call(
            DOMAIN,
            "backfill",
            {
                "start_date": (TODAY - timedelta(days=a)).isoformat(),
                "end_date": (TODAY - timedelta(days=b)).isoformat(),
            },
            blocking=True,
            return_response=True,
        )

    with pytest.raises(ServiceValidationError, match="schedule"):
        await call(1, 1)
    with pytest.raises(ServiceValidationError, match="before start_date"):
        await call(1, 2)
    old = await call(20, 15)
    assert old["skipped_unavailable"] == {
        "from": (TODAY - timedelta(days=20)).isoformat(),
        "to": (TODAY - timedelta(days=15)).isoformat(),
    }
    await _set_mode(hass, entry, "pricing")
    with pytest.raises(ServiceValidationError, match="Pricing-only"):
        await call(2, 2)


async def test_pricing_mode_writes_no_usage_but_prices_and_own_cost(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Pricing-only: usage statistics stop updating (never deleted); prices and own cost go on."""
    _preload_store(hass_storage, retention_days=2)
    fake = FakeAmber(TODAY - timedelta(days=30), TODAY + timedelta(days=5))
    energies = {d: _energy_like(d, "E1") for d in (TODAY - timedelta(days=2), YESTERDAY, TODAY)}
    await _import_sensor(hass, SENSOR, energies)
    entry = await _setup_entry(hass, aioclient_mock, fake, subentries=[_sub(SENSOR, "E1")])
    await _run(hass)
    usage_before = await _rows(hass)

    await _set_mode(hass, entry, "pricing", **{CONF_PRICE_SERIES: True})
    clock.now += timedelta(days=1)
    result = await _run(hass)

    assert result["imported_days"] == []
    assert await _rows(hass) == usage_before  # untouched, still there
    assert result["chains"]["own:sub_house"]["written"] == [TODAY.isoformat()]
    # The series starts at the retention boundary on the day it is enabled.
    assert len(result["chains"]["price"]["written"]) == 2
    meta = await hass.async_add_executor_job(get_metadata, hass)
    assert set(ALL_IDS) <= set(meta)
    # Back to Full: the usage chain resumes from its marker.
    await _set_mode(hass, entry, "full")
    back = await _run(hass)
    assert back["imported_days"] == [TODAY.isoformat()]
    _assert_contiguous(await _rows(hass), TODAY - timedelta(days=2), TODAY)


# --- sub-entries, guards, revisions ------------------------------------------------------------


async def test_subentry_flow_validation_and_removal(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Only cumulative energy sensors; no duplicates; removal keeps the statistics."""
    _preload_store(hass_storage, retention_days=1)
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    entry = await _setup_entry(hass, aioclient_mock, fake)
    hass.states.async_set(
        "sensor.power", "5", {"device_class": "power", "state_class": "measurement"}
    )
    await _import_sensor(hass, SENSOR, {YESTERDAY: _energy_like(YESTERDAY, "E1")})

    async def start():
        return await hass.config_entries.subentries.async_init(
            (entry.entry_id, "own_sensor"), context={"source": "user"}
        )

    flow = await start()
    assert flow["step_id"] == "user"
    flow = await hass.config_entries.subentries.async_configure(
        flow["flow_id"], {"entity_id": "sensor.power", "channel": "E1"}
    )
    assert flow["errors"] == {"entity_id": "not_cumulative_energy"}
    flow = await hass.config_entries.subentries.async_configure(
        flow["flow_id"], {"entity_id": "sensor.missing", "channel": "E1"}
    )
    assert flow["errors"] == {"entity_id": "sensor_not_found"}
    flow = await hass.config_entries.subentries.async_configure(
        flow["flow_id"], {"entity_id": SENSOR, "channel": "E1"}
    )
    assert flow["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    mgr = entry.runtime_data.manager
    assert [s.entity_id for s in mgr.own_sensors] == [SENSOR]  # reloaded with the mapping
    dup = await start()
    dup = await hass.config_entries.subentries.async_configure(
        dup["flow_id"], {"entity_id": SENSOR, "channel": "E1"}
    )
    assert dup["type"] is FlowResultType.ABORT and dup["reason"] == "already_configured"

    await _run(hass)
    assert len(await _own_rows(hass)) == 24
    sub_id = next(iter(entry.subentries))
    hass.config_entries.async_remove_subentry(entry, sub_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.manager.own_sensors == []
    assert f"own:{sub_id}" not in hass_storage[_store_key()]["data"]["chains"]
    assert len(await _own_rows(hass)) == 24  # statistics kept


async def test_readded_sensor_continues_existing_statistics(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """A re-added mapping adopts its earlier statistics and continues after them."""
    _preload_store(hass_storage, retention_days=2)
    fake = FakeAmber(TODAY - timedelta(days=30), TODAY + timedelta(days=3))
    days = [TODAY - timedelta(days=2), YESTERDAY, TODAY]
    await _import_sensor(hass, SENSOR, {d: _energy_like(d, "E1") for d in days})
    entry = await _setup_entry(hass, aioclient_mock, fake, subentries=[_sub(SENSOR, "E1")])
    await _run(hass)
    before = await _own_rows(hass)
    # Forget the chain (as a removal would), then run again the next day.
    hass_storage[_store_key()]["data"]["chains"].pop("own:sub_house")
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    clock.now += timedelta(days=1)

    result = await _run(hass)

    assert result["chains"]["own:sub_house"]["written"] == [TODAY.isoformat()]
    rows = await _own_rows(hass)
    assert rows[:48] == before
    assert rows[48]["sum"] == pytest.approx(before[-1]["sum"] + rows[48]["state"], abs=1e-6)


async def test_chain_marker_mismatch_raises_repairs(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Guard 1 applies per chain: a corrupted sensor marker stops that chain only."""
    _preload_store(hass_storage, retention_days=2)
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    days = [TODAY - timedelta(days=2), YESTERDAY]
    await _import_sensor(hass, SENSOR, {d: _energy_like(d, "E1") for d in days})
    entry = await _setup_entry(hass, aioclient_mock, fake, subentries=[_sub(SENSOR, "E1")])
    await _run(hass)
    chain = hass_storage[_store_key()]["data"]["chains"]["own:sub_house"]
    chain["marker"] = chain["last_written"] = days[0].isoformat()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    result = await _run(hass)

    assert result["status"] == "caught_up"
    assert result["chains"]["own:sub_house"]["reason"] == "marker_mismatch"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"marker_mismatch_{ENTRY_ID}_own_sub_house")
    assert issue is not None


async def test_revision_rewrites_own_cost(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """A revised price changes own cost from that day on; the own chain is rewritten."""
    _preload_store(hass_storage, retention_days=3)
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    days = [TODAY - timedelta(days=d) for d in (3, 2, 1)]
    energies = {d: _energy_like(d, "E1") for d in days}
    await _import_sensor(hass, SENSOR, energies)
    fake.overrides[days[0]] = make_usage_day(days[0], quality="estimated")
    await _setup_entry(hass, aioclient_mock, fake, subentries=[_sub(SENSOR, "E1")])
    await _run(hass)

    clock.now += timedelta(days=1)
    fake.overrides[days[0]] = make_usage_day(days[0], seed=11)  # revised prices and usage
    result = await _run(hass)

    assert result["revisions"]["changed"] == [days[0].isoformat()]
    assert result["revisions"]["chains_rewritten"] == {"own:sub_house": 3}
    rows = await _own_rows(hass)
    recs = [_parse_usage(r) for r in fake.overrides[days[0]] if r["channelIdentifier"] == "E1"]
    want = math.fsum(energies[days[0]][i] * r.per_kwh / 100 for i, r in enumerate(recs))
    assert math.fsum(r["state"] for r in rows[:24]) == pytest.approx(want, abs=5e-5)
    for prev, cur in pairwise(rows):  # two independent 6 dp roundings: allow 2e-6
        assert cur["sum"] == pytest.approx(prev["sum"] + cur["state"], abs=2e-6)


async def test_energy_dashboard_accepts_own_cost_statistic(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """The own-cost statistic works as a grid source's 'entity tracking the total costs'."""
    from homeassistant.components.energy.data import async_get_manager  # noqa: PLC0415
    from homeassistant.components.energy.validate import async_validate  # noqa: PLC0415
    from homeassistant.setup import async_setup_component  # noqa: PLC0415

    _preload_store(hass_storage, retention_days=1)
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    await _import_sensor(hass, SENSOR, {YESTERDAY: _energy_like(YESTERDAY, "E1")})
    await _setup_entry(hass, aioclient_mock, fake, subentries=[_sub(SENSOR, "E1")])
    await _run(hass)
    assert await async_setup_component(hass, "energy", {})
    energy = await async_get_manager(hass)
    await energy.async_update(
        {
            "energy_sources": [
                {
                    "type": "grid",
                    "stat_energy_from": SENSOR,
                    "stat_energy_to": None,
                    "stat_cost": OWN,
                    "stat_compensation": None,
                    "entity_energy_price": None,
                    "number_energy_price": None,
                    "entity_energy_price_export": None,
                    "number_energy_price_export": None,
                    "cost_adjustment_day": 0,
                }
            ]
        }
    )
    validation = (await async_validate(hass)).as_dict()
    cost_issues = [
        i
        for i in validation["energy_sources"][0]
        if i.get("affected_entities") and any(OWN in str(e) for e in i["affected_entities"])
    ]
    assert cost_issues == []


# --- chain edge paths ----------------------------------------------------------------------


async def test_pricing_chain_waits_then_skips_gap(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """In Pricing-only mode the chains apply patience themselves; the status follows them."""
    _preload_store(hass_storage, retention_days=4)
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    gap = TODAY - timedelta(days=2)
    fake.empty = {gap}
    await _setup_entry(
        hass,
        aioclient_mock,
        fake,
        options={
            CONF_SCHEDULE_MODE: "automatic",
            CONF_USAGE_MODE: "pricing",
            CONF_PRICE_SERIES: True,
            "patience_days": 1,
        },
    )
    first_day_patience = await _run(hass)
    assert first_day_patience["chains"]["price"]["skipped"] == [gap.isoformat()]
    assert first_day_patience["status"] == "caught_up"


async def test_pricing_chain_waiting_sets_status(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Yesterday not published: the price chain waits and the run reports it."""
    _preload_store(hass_storage, retention_days=3)
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY - timedelta(days=1))
    await _setup_entry(
        hass,
        aioclient_mock,
        fake,
        options={
            CONF_SCHEDULE_MODE: "automatic",
            CONF_USAGE_MODE: "pricing",
            CONF_PRICE_SERIES: True,
        },
    )
    result = await _run(hass)
    assert result["status"] == "waiting_for_data"
    assert result["chains"]["price"]["written"] == [
        (TODAY - timedelta(days=3)).isoformat(),
        (TODAY - timedelta(days=2)).isoformat(),
    ]


async def test_chain_mirrors_usage_gap_and_outage(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """A day the usage chain skipped is skipped by the chains too; after an outage,
    chain days older than retention are skipped as unavailable."""
    _preload_store(hass_storage, retention_days=4)
    fake = FakeAmber(TODAY - timedelta(days=30), TODAY + timedelta(days=20))
    gap = TODAY - timedelta(days=2)
    fake.empty = {gap}
    entry = await _setup_entry(
        hass,
        aioclient_mock,
        fake,
        options={CONF_SCHEDULE_MODE: "automatic", CONF_PRICE_SERIES: True, "patience_days": 1},
    )
    result = await _run(hass)
    assert result["skipped_gap"] == [gap.isoformat()]
    assert gap.isoformat() in result["chains"]["price"]["skipped"]
    store = entry.runtime_data.manager.store
    store._data["retention"]["last_verified"] = (TODAY + timedelta(days=10)).isoformat()
    clock.now += timedelta(days=10)
    later = await _run(hass)
    price = store.as_dict()["chains"]["price"]["days"]
    assert price[(TODAY + timedelta(days=1)).isoformat()]["status"] == "skipped_unavailable"
    assert later["chains"]["price"]["status"] == "caught_up"


async def test_chain_pending_recovery_and_rewrite_resume(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Own chains recover an interrupted write and resume an interrupted rewrite."""
    _preload_store(hass_storage, retention_days=3)
    fake = FakeAmber(TODAY - timedelta(days=30), TODAY + timedelta(days=3))
    days = [TODAY - timedelta(days=d) for d in (3, 2, 1, 0)]
    energies = {d: _energy_like(d, "E1") for d in days}
    await _import_sensor(hass, SENSOR, energies)
    fake.overrides[days[0]] = make_usage_day(days[0], quality="estimated")
    entry = await _setup_entry(hass, aioclient_mock, fake, subentries=[_sub(SENSOR, "E1")])
    await _run(hass)
    mgr = entry.runtime_data.manager
    chain = next(c for c in mgr._secondary_chains() if c.sensor)

    # 1. Interrupted write of TODAY (next day): rows written, marker not advanced.
    clock.now += timedelta(days=1)
    records = [_parse_usage(r) for r in make_usage_day(TODAY)]
    priced = chains.own_cost_day(
        [r for r in records if r.channel_identifier == "E1"],
        TODAY,
        energies[TODAY],
        None,
        feed_in=False,
        allow_fallback=True,
    )
    base = (await _own_rows(hass))[-1]["sum"]
    await chain.state.async_set_pending(TODAY)
    await importer.async_write_amounts(
        hass, TODAY, [chain.sensor.spec], {OWN: priced.amounts}, {OWN: base}
    )
    fake.overrides[days[0]] = make_usage_day(
        days[0], quality="estimated"
    )  # unchanged: no revision yet
    result = await _run(hass)
    assert result["chains"]["own:sub_house"]["written"] == [TODAY.isoformat()]
    rows = await _own_rows(hass)
    assert len(rows) == 96

    # 2. A revision rewrite of the own chain that stops part-way resumes next run.
    clock.now += timedelta(days=1)
    fake.overrides[days[0]] = make_usage_day(days[0], seed=21)
    real = mgr._async_chain_write
    calls = 0

    async def flaky(ch, day, recs, *, rewriting):
        nonlocal calls
        if rewriting:
            calls += 1
            if calls == 2:
                raise importer.ImportDayError("simulated", "stop")
        return await real(ch, day, recs, rewriting=rewriting)

    mgr._async_chain_write = flaky
    first = await _run(hass)
    assert first["reason"] == "simulated"
    assert chain.state.rewrite is not None
    mgr._async_chain_write = real
    await _run(hass)
    assert chain.state.rewrite is None
    rows = await _own_rows(hass)
    for prev, cur in pairwise(rows):
        assert cur["sum"] == pytest.approx(prev["sum"] + cur["state"], abs=2e-6)


async def test_chain_guard_variants(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Chain Guard 1: marker behind last_written, unexplained gap, and partial history."""
    _preload_store(hass_storage, retention_days=2)
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    days = [TODAY - timedelta(days=2), YESTERDAY]
    await _import_sensor(hass, SENSOR, {d: _energy_like(d, "E1") for d in days})
    entry = await _setup_entry(hass, aioclient_mock, fake, subentries=[_sub(SENSOR, "E1")])
    await _run(hass)
    mgr = entry.runtime_data.manager
    chain = next(c for c in mgr._secondary_chains() if c.sensor)
    data = chain.state._data

    data["marker"] = days[0].isoformat()  # behind last_written
    with pytest.raises(importer.ImportRefusedError, match="before the last written day"):
        await mgr._async_chain_guard(chain)
    data["marker"], data["last_written"] = YESTERDAY.isoformat(), days[0].isoformat()
    data["days"][YESTERDAY.isoformat()]["status"] = "empty"
    with pytest.raises(importer.ImportRefusedError, match="neither imported nor skipped"):
        await mgr._async_chain_guard(chain)
    # Statistics that end mid-day cannot be adopted.
    data.update(marker=None, last_written=None, days={})
    await importer.async_write_amounts(
        hass, TODAY, [chain.sensor.spec], {OWN: [0.0] * 24}, {OWN: 0.0}
    )
    async_add_external_statistics(
        hass,
        chain.sensor.spec.metadata(),
        [{"start": importer.nem_day_start(TODAY + timedelta(days=1)), "state": 0.0, "sum": 0.0}],
    )
    await async_wait_recording_done(hass)
    with pytest.raises(importer.ImportRefusedError, match="statistics end at"):
        await mgr._async_chain_guard(chain)


async def test_chain_data_problems(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Incomplete usage stops a chain; sensor gaps and inactive channels skip days."""
    _preload_store(hass_storage, retention_days=3)
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    days = [TODAY - timedelta(days=3), TODAY - timedelta(days=2), YESTERDAY]
    # Sensor statistics only for the first and last days: the middle day is a sensor gap.
    await _import_sensor(hass, SENSOR, {days[0]: _energy_like(days[0], "E1")})
    fake.mutate = lambda day, recs: recs.pop(0) if day == YESTERDAY else None
    entry = await _setup_entry(
        hass,
        aioclient_mock,
        fake,
        subentries=[_sub(SENSOR, "E1")],
        options={CONF_SCHEDULE_MODE: "automatic", CONF_USAGE_MODE: "pricing"},
    )
    result = await _run(hass)
    chain = result["chains"]["own:sub_house"]
    assert chain["written"] == [days[0].isoformat()]
    assert chain["status"] == "needs_attention" and chain["reason"] == "incomplete_day"
    store = entry.runtime_data.manager.store
    assert (
        store.as_dict()["chains"]["own:sub_house"]["days"][days[1].isoformat()]["status"]
        == "skipped_sensor_data"
    )


async def test_sensor_on_channel_added_later_skips_earlier_days(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Before a channel's start day (reconfigure), a sensor mapped to it has no prices."""
    _preload_store(hass_storage, retention_days=1, channel_since={"B1": TODAY.isoformat()})
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    fake.overrides[YESTERDAY] = make_usage_day(YESTERDAY, [("E1", "general", None)])
    await _import_sensor(hass, EXPORT, {YESTERDAY: [0.01] * 288})
    entry = await _setup_entry(
        hass,
        aioclient_mock,
        fake,
        subentries=[_sub(EXPORT, "B1", "sub_export")],
        options={CONF_SCHEDULE_MODE: "automatic", CONF_USAGE_MODE: "pricing"},
    )
    result = await _run(hass)
    assert result["chains"]["own:sub_export"]["skipped"] == [YESTERDAY.isoformat()]
    days = entry.runtime_data.manager.store.as_dict()["chains"]["own:sub_export"]["days"]
    assert "not active" in days[YESTERDAY.isoformat()]["reason"]


async def test_backfill_patience_and_resume(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """An empty day in a backfill range waits; the next run resumes; patience then skips it."""
    _preload_store(hass_storage, retention_days=10)
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    gap = TODAY - timedelta(days=7)
    fake.empty = {gap}
    entry = await _setup_entry(
        hass,
        aioclient_mock,
        fake,
        options={CONF_SCHEDULE_MODE: "automatic", CONF_USAGE_MODE: "recovery", "patience_days": 2},
    )

    async def backfill(a: int, b: int) -> dict:
        return await hass.services.async_call(
            DOMAIN,
            "backfill",
            {
                "start_date": (TODAY - timedelta(days=a)).isoformat(),
                "end_date": (TODAY - timedelta(days=b)).isoformat(),
            },
            blocking=True,
            return_response=True,
        )

    waiting = await backfill(9, 5)
    assert waiting["status"] == "waiting_for_data" and waiting["waiting_for"] == gap.isoformat()
    store = entry.runtime_data.manager.store
    assert store.tail_rewrite["next"] == gap.isoformat()
    resumed = await _run(hass)  # same day: still waiting, returned from the resume step
    assert resumed["status"] == "waiting_for_data" and "tail_resumed" in resumed
    clock.now += timedelta(days=1)
    done = await _run(hass)
    assert done["status"] == "caught_up"
    assert store.day(gap)["status"] == "skipped_gap"
    assert store.tail_rewrite is None
    with pytest.raises(ServiceValidationError, match="nothing to import"):
        await hass.services.async_call(
            DOMAIN,
            "backfill",
            {
                "start_date": (TODAY + timedelta(days=1)).isoformat(),
                "end_date": (TODAY + timedelta(days=2)).isoformat(),
            },
            blocking=True,
            return_response=True,
        )


async def test_subentry_for_missing_channel_is_ignored(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """A mapping to a channel that no longer exists is ignored (statistics are kept)."""
    _preload_store(hass_storage, retention_days=1)
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    entry = await _setup_entry(
        hass, aioclient_mock, fake, subentries=[_sub("sensor.old_meter", "X9", "sub_old")]
    )
    assert entry.runtime_data.manager.own_sensors == []


def test_sensor_slug_and_missing_hourly() -> None:
    """Slugs are statistic-ID safe; without any sensor data the day is unavailable."""
    from custom_components.amber_energy_dashboard.statistics import sensor_slug  # noqa: PLC0415

    assert sensor_slug("sensor.House  Energy!") == "house_energy"
    assert sensor_slug("sensor.___") == "sensor"
    with pytest.raises(chains.SensorDataUnavailable):
        chains.own_cost_day([], YESTERDAY, None, None, feed_in=False, allow_fallback=True)


async def test_chain_defensive_paths(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Stale pending, outage without later data, missing baseline, rewrite edge cases,
    the day-record cap, and sub-entries of another type."""
    from custom_components.amber_energy_dashboard.storage import MAX_DAY_ENTRIES  # noqa: PLC0415

    _preload_store(hass_storage, retention_days=3)
    fake = FakeAmber(TODAY - timedelta(days=30), TODAY - timedelta(days=3))  # later days missing
    other = {
        **_sub("sensor.x", "E1", "sub_other"),
        "subentry_type": "something_else",
        "unique_id": "x",
    }
    entry = await _setup_entry(
        hass,
        aioclient_mock,
        fake,
        subentries=[other],
        options={
            CONF_SCHEDULE_MODE: "automatic",
            CONF_USAGE_MODE: "pricing",
            CONF_PRICE_SERIES: True,
            "patience_days": 1,
        },
    )
    mgr = entry.runtime_data.manager
    assert mgr.own_sensors == []
    result = await _run(hass)
    # Day T-2 empty, patience reached, but no later day has data: keep waiting.
    assert result["chains"]["price"]["status"] == "waiting_for_data"
    chain = next(c for c in mgr._secondary_chains())

    await chain.state.async_set_pending(TODAY)  # stale: nothing was written for it
    assert await mgr._async_chain_guard(chain) is None
    assert chain.state.pending is None

    assert (
        await mgr._async_chain_rewrite(
            type(chain)("price", type(chain.state)(mgr.store, "empty_chain"), chain.mean_specs),
            TODAY,
        )
        == 0
    )
    gap = TODAY - timedelta(days=4)  # a skipped day inside the rewrite range stays skipped
    await chain.state.async_mark_skipped(gap, gap, "skipped_gap", "x")
    rewritten = await mgr._async_chain_rewrite(chain, gap)
    assert rewritten == 1
    chain.state._data["last_written"] = (TODAY - timedelta(days=1)).isoformat()
    chain.state._data["days"][(TODAY - timedelta(days=1)).isoformat()] = {"status": "imported"}
    with pytest.raises(importer.ImportDayError, match="returned no usage during a rewrite"):
        await mgr._async_chain_rewrite(chain, TODAY - timedelta(days=1))

    first = date(2020, 1, 1)
    await chain.state.async_mark_skipped(
        first, first + timedelta(days=MAX_DAY_ENTRIES + 5), "skipped_gap", "x"
    )
    assert len(chain.state._data["days"]) == MAX_DAY_ENTRIES


async def test_own_baseline_missing_is_refused(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """If the previous day's own-cost row is missing, the chain refuses to guess a baseline."""
    _preload_store(hass_storage, retention_days=2)
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    days = [TODAY - timedelta(days=2), YESTERDAY]
    await _import_sensor(hass, SENSOR, {d: _energy_like(d, "E1") for d in days})
    entry = await _setup_entry(hass, aioclient_mock, fake, subentries=[_sub(SENSOR, "E1")])
    mgr = entry.runtime_data.manager
    chain = next(c for c in mgr._secondary_chains() if c.sensor)
    chain.state._data.update(marker=days[0].isoformat(), last_written=days[0].isoformat())
    mgr.ctx.client.start_run(RunBudget())
    records = await mgr._async_records_for(YESTERDAY, YESTERDAY)
    with pytest.raises(importer.ImportRefusedError, match="no row at the end of"):
        await mgr._async_chain_write(chain, YESTERDAY, records, rewriting=False)
