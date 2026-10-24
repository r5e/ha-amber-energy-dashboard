"""2.2.0b4: solar sensors that go offline at night (DESIGN section 22).

Most inverters stop reporting after sunset, so Home Assistant records no statistics for
those hours. For the site's solar source only, a gap counts as zero production when the
sum is unchanged across it or it lies outside daylight (power: also when the sensor
reports nothing on both sides). Solar-aware days skipped before this fix are worked out
again once. All data is synthetic."""

from datetime import UTC, date, datetime, timedelta
import math
from typing import Any
from unittest.mock import patch

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.db_schema import Statistics, StatisticsShortTerm
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.const import SUN_EVENT_SUNRISE, SUN_EVENT_SUNSET
from homeassistant.core import HomeAssistant
from homeassistant.helpers.sun import get_astral_event_date
import pytest
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.amber_energy_dashboard import chains, importer, manager as manager_mod, solar

from .fake_amber import FakeAmber
from .test_manager import (  # noqa: F401 - fixtures are used by name
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
from .test_solar import (
    APPLIANCE,
    EXPORT,
    IMPORT,
    SOLAR,
    SOLAR_AWARE,
    SOLAR_POWER,
    _appliance,
    _expected,
    _flows,
    _grid_meter,
)

FIVE = timedelta(minutes=5)
HOUR = timedelta(hours=1)
T0 = datetime(2026, 9, 25, 14, tzinfo=UTC)  # 00:00 AEST on 2026-09-26
CHAIN = "own:sub_pump:solar_aware"


@pytest.fixture
def sydney_home(hass: HomeAssistant) -> None:
    """Home Assistant's home location in Sydney, for sunrise and sunset."""
    hass.config.latitude, hass.config.longitude = -33.87, 151.21


def _sums(points: dict[int, float], step: timedelta = FIVE) -> list[dict[str, Any]]:
    """Rows at T0 + i * step with the given sums."""
    return [{"start": (T0 + i * step).timestamp(), "sum": s} for i, s in sorted(points.items())]


# --- the gap rules, with a stand-in daylight test ------------------------------------------


def test_fill_solar_sums_gap_rules() -> None:
    """A gap counts as zero production when the sum is unchanged across it, or outside
    daylight; a changed sum in daylight, or no row before the gap, is refused."""
    night = lambda start, end: False  # noqa: E731
    day = lambda start, end: True  # noqa: E731
    # Rows 0-2, a gap at 3-5, rows 6-8: the change shows in the bucket after the gap.
    rows = _sums({0: 1.0, 1: 1.0, 2: 1.5, 6: 1.75, 7: 2.0, 8: 2.0})
    assert solar.fill_solar_sums(rows, T0, FIVE, 8, night) == [0.0, 0.5, 0, 0, 0, 0.25, 0.25, 0]
    assert solar.fill_solar_sums(rows, T0, FIVE, 8, day) is None
    # Unchanged across the gap: accepted even in daylight.
    rows = _sums({0: 1.0, 1: 1.5, 5: 1.5, 6: 2.0})
    assert solar.fill_solar_sums(rows, T0, FIVE, 6, day) == [0.5, 0, 0, 0, 0, 0.5]
    # Still offline at the day's end (no row after): only outside daylight.
    rows = _sums({0: 1.0, 1: 1.5})
    assert solar.fill_solar_sums(rows, T0, FIVE, 4, night) == [0.5, 0, 0, 0]
    assert solar.fill_solar_sums(rows, T0, FIVE, 4, day) is None
    # A leading gap uses the last row before the day (from the rows around it).
    rows = _sums({-30: 4.0, 2: 4.0, 3: 4.25})
    assert solar.fill_solar_sums(rows, T0, FIVE, 3, night) == [0, 0, 0.25]
    # No row at all before the gap: refused.
    assert solar.fill_solar_sums(_sums({2: 1.0, 3: 1.0}), T0, FIVE, 3, night) is None
    # The span given to the daylight test runs from the last row's end to the next row.
    spans: list[tuple[datetime, datetime]] = []
    solar.fill_solar_sums(
        _sums({0: 1.0, 4: 2.0}), T0, FIVE, 4, lambda s, e: spans.append((s, e)) or False
    )
    assert spans == [(T0 + FIVE, T0 + 4 * FIVE)]


def test_fill_solar_means_gap_rules() -> None:
    """A power sensor's gap is zero outside daylight, or in daylight when the sensor
    reports nothing (at most 1 W) on both sides of it."""
    night = lambda start, end: False  # noqa: E731
    day = lambda start, end: True  # noqa: E731

    def means(points: dict[int, float]) -> list[dict[str, Any]]:
        return [{"start": (T0 + i * FIVE).timestamp(), "mean": m} for i, m in points.items()]

    rows = means({0: 500.0, 3: 0.4})
    assert solar.fill_solar_means(rows, T0, FIVE, 4, night) == [500.0, 0.0, 0.0, 0.4]
    assert solar.fill_solar_means(rows, T0, FIVE, 4, day) is None
    assert solar.fill_solar_means(means({0: 0.0, 3: 0.5}), T0, FIVE, 4, day) == [0, 0, 0, 0.5]
    # Missing at both ends of the day, nothing around it: only outside daylight.
    assert solar.fill_solar_means(means({1: 0.0}), T0, FIVE, 3, night) == [0, 0, 0]
    assert solar.fill_solar_means(means({1: 0.0}), T0, FIVE, 3, day) is None


async def test_daylight_test_in_sydney(hass: HomeAssistant, sydney_home: None) -> None:
    """Daylight is sunrise + 1 h to sunset - 1 h at the home location; a night gap may end
    up to 3 h after sunrise, but a gap may not start in daylight or span a whole day."""
    await hass.config.async_set_time_zone("Australia/Sydney")
    daylight = solar.daylight_test(hass)
    day = date(2026, 9, 25)
    sunrise = get_astral_event_date(hass, SUN_EVENT_SUNRISE, day)
    sunset = get_astral_event_date(hass, SUN_EVENT_SUNSET, day)
    assert sunrise is not None and sunset is not None
    next_sunrise = get_astral_event_date(hass, SUN_EVENT_SUNRISE, day + timedelta(days=1))
    assert next_sunrise is not None
    assert not daylight(sunset - timedelta(minutes=30), next_sunrise + timedelta(hours=2))
    assert daylight(sunset - timedelta(minutes=30), next_sunrise + timedelta(hours=4))
    assert not daylight(sunrise - HOUR, sunrise + timedelta(minutes=30))
    noon = sunrise + (sunset - sunrise) / 2
    assert daylight(noon, noon + FIVE)
    # It starts in daylight (a lost connection while producing): refused, however short.
    assert daylight(sunrise + timedelta(minutes=90), sunrise + timedelta(minutes=95))
    # A whole day offline.
    assert daylight(sunset, next_sunrise + timedelta(hours=24))
    with patch.object(solar, "get_astral_event_date", return_value=None):
        assert solar.daylight_test(hass)(noon, noon + FIVE)


# --- sites with a solar sensor that goes offline at night ----------------------------------


def _online(values: list[float]) -> tuple[int, int]:
    """The inverter's online buckets in a day: from the first to the last with production."""
    producing = [i for i, v in enumerate(values) if v > 0]
    return producing[0], producing[-1]


async def _import_offline_solar(
    hass: HomeAssistant, energies: dict[date, list[float]], *, short: bool = True
) -> None:
    """A solar energy sensor that reports only while the inverter is online: no 5-minute
    or hourly statistics at night, as Home Assistant records an unavailable sensor."""
    hass.states.async_set(
        SOLAR,
        "unavailable",
        {"device_class": "energy", "state_class": "total_increasing", "unit_of_measurement": "kWh"},
    )
    meta = {
        "has_sum": True,
        "mean_type": StatisticMeanType.NONE,
        "name": None,
        "source": "recorder",
        "statistic_id": SOLAR,
        "unit_class": "energy",
        "unit_of_measurement": "kWh",
    }
    short_rows: list[dict[str, Any]] = []
    hour_rows: list[dict[str, Any]] = []
    total = 0.0
    for day in sorted(energies):
        start = importer.nem_day_start(day)
        first, last = _online(energies[day])
        for i in range(first, last + 1):
            total += energies[day][i]
            short_rows.append({"start": start + i * FIVE, "state": total, "sum": total})
            if i % 12 == 11 or i == last:  # an hour with any reading gets a row
                hour_rows.append({"start": start + (i // 12) * HOUR, "state": total, "sum": total})
    recorder = get_instance(hass)
    if short:
        recorder.async_import_statistics(meta, short_rows, StatisticsShortTerm)
    recorder.async_import_statistics(meta, hour_rows, Statistics)
    await async_wait_recording_done(hass)


async def _offline_site(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict,
    days: list[date],
    *,
    short: bool = True,
    solar_days: dict[date, list[float]] | None = None,
):
    """The test_solar site, but its solar sensor is offline every night. The sensor's
    history starts a day before ``days``."""
    await hass.config.async_set_time_zone("Australia/Sydney")
    _preload_store(hass_storage, retention_days=len(days))
    flows = {d: _flows(d) for d in days}
    await _import_sensor(
        hass, APPLIANCE, {d: f["appliance"] for d, f in flows.items()}, short=short
    )
    await _import_sensor(hass, IMPORT, {d: f["import"] for d, f in flows.items()}, short=short)
    await _import_sensor(hass, EXPORT, {d: f["export"] for d, f in flows.items()}, short=short)
    before = days[0] - timedelta(days=1)
    await _import_offline_solar(
        hass,
        solar_days
        or {before: _flows(before)["solar"], **{d: f["solar"] for d, f in flows.items()}},
        short=short,
    )
    entry = await _setup_entry(
        hass,
        aioclient_mock,
        FakeAmber(TODAY - timedelta(days=30), YESTERDAY),
        options={"schedule_mode": "automatic", "solar_energy": SOLAR},
        subentries=[_grid_meter(), _appliance()],
    )
    return entry, flows


async def test_solar_sensor_offline_at_night(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    clock: Clock,
    hass_storage: dict,
    sydney_home: None,
) -> None:
    """With the solar sensor offline every night (no statistics from shortly before
    sunset to after sunrise), each day is worked out as if it reported zero, matching the
    independent sum exactly."""
    days = [TODAY - timedelta(days=d) for d in (2, 1)]
    entry, flows = await _offline_site(hass, aioclient_mock, hass_storage, days)
    result = await _run(hass)
    chain = result["chains"][CHAIN]
    assert chain["written"] == [d.isoformat() for d in days]
    assert chain["skipped"] == [] and chain["lower_precision"] == []
    assert chain["recalculated"] == []  # nothing was skipped before
    rows = await _rows(hass, [SOLAR_AWARE])
    expected = [h for d in days for h in _expected(d, flows[d])]
    for row, want in zip(rows[SOLAR_AWARE], expected, strict=True):
        assert row["state"] == pytest.approx(want, abs=1e-6)
    record = entry.runtime_data.manager.store._data["chains"][CHAIN]["days"][YESTERDAY.isoformat()]
    assert record["solar_kwh"] > 0
    # Grid meters and appliances keep the strict rule (they report all day here).
    assert result["chains"]["own:sub_pump"]["written"] == [d.isoformat() for d in days]


async def test_solar_sensor_offline_at_night_hourly(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    clock: Clock,
    hass_storage: dict,
    sydney_home: None,
) -> None:
    """The same on the hourly (lower precision) path, for days older than Home
    Assistant's 5-minute statistics: the night hours have no rows."""
    days = [TODAY - timedelta(days=d) for d in (2, 1)]
    await _offline_site(hass, aioclient_mock, hass_storage, days, short=False)
    result = await _run(hass)
    chain = result["chains"][CHAIN]
    assert chain["written"] == [d.isoformat() for d in days]
    assert chain["lower_precision"] == [d.isoformat() for d in days]


async def test_solar_gap_in_daylight_is_refused(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    clock: Clock,
    hass_storage: dict,
    sydney_home: None,
) -> None:
    """A gap that starts in daylight while the sum changes across it (production during a
    lost connection) can't be placed: that day is still skipped, naming the sensor."""
    days = [TODAY - timedelta(days=d) for d in (2, 1)]
    before = days[0] - timedelta(days=1)
    solar_days = {d: _flows(d)["solar"] for d in (before, *days)}
    # Yesterday 11:00 to 12:00 AEST: offline while producing. The energy shows up after.
    dropped = solar_days[YESTERDAY][:]
    lump = math.fsum(dropped[132:144])
    dropped[132:144] = [0.0] * 12
    dropped[144] += lump

    real_import = _import_offline_solar

    async def offline(hass: HomeAssistant, energies, *, short: bool = True) -> None:
        await real_import(hass, energies, short=short)
        # Remove the dropped hour's rows, as an unavailable sensor leaves them.
        start = importer.nem_day_start(YESTERDAY) + 132 * FIVE
        await get_instance(hass).async_add_executor_job(_delete_rows, hass, start, 12)

    with patch(f"{__name__}._import_offline_solar", offline):
        await _offline_site(
            hass, aioclient_mock, hass_storage, days, solar_days={**solar_days, YESTERDAY: dropped}
        )
    result = await _run(hass)
    chain = result["chains"][CHAIN]
    assert chain["written"] == [days[0].isoformat()]
    assert chain["skipped"] == [YESTERDAY.isoformat()]
    manager = hass.config_entries.async_entries("amber_energy_dashboard")[0].runtime_data.manager
    reason = manager.store._data["chains"][CHAIN]["days"][YESTERDAY.isoformat()]["reason"]
    assert reason == f"no complete statistics for {YESTERDAY} from {SOLAR}"


def _delete_rows(hass: HomeAssistant, start: datetime, count: int) -> None:
    """Delete a solar sensor's 5-minute and hourly rows from ``start`` (``count`` 5-minute
    buckets, and the hours they fall in)."""
    from homeassistant.components.recorder.util import session_scope  # noqa: PLC0415

    end = start + count * FIVE
    with session_scope(hass=hass, session=get_instance(hass).get_session()) as session:
        for table in (StatisticsShortTerm, Statistics):
            session.query(table).filter(
                table.start_ts >= start.timestamp(), table.start_ts < end.timestamp()
            ).filter(table.metadata_id.in_(_solar_ids(session))).delete(synchronize_session=False)


def _solar_ids(session) -> list[int]:
    from homeassistant.components.recorder.db_schema import StatisticsMeta  # noqa: PLC0415

    return [
        m.id for m in session.query(StatisticsMeta).filter(StatisticsMeta.statistic_id == SOLAR)
    ]


async def test_solar_power_sensor_offline_at_night(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    clock: Clock,
    hass_storage: dict,
    sydney_home: None,
) -> None:
    """A solar power sensor with no mean statistics at night: those buckets count as no
    production."""
    await hass.config.async_set_time_zone("Australia/Sydney")
    days = [TODAY - timedelta(days=d) for d in (2, 1)]
    _preload_store(hass_storage, retention_days=2)
    flows = {d: _flows(d) for d in days}
    await _import_sensor(hass, APPLIANCE, {d: f["appliance"] for d, f in flows.items()})
    await _import_sensor(hass, IMPORT, {d: f["import"] for d, f in flows.items()})
    await _import_sensor(hass, EXPORT, {d: f["export"] for d, f in flows.items()})
    hass.states.async_set(
        SOLAR_POWER,
        "unavailable",
        {"device_class": "power", "state_class": "measurement", "unit_of_measurement": "W"},
    )
    meta = {
        "has_sum": False,
        "mean_type": StatisticMeanType.ARITHMETIC,
        "name": None,
        "source": "recorder",
        "statistic_id": SOLAR_POWER,
        "unit_class": "power",
        "unit_of_measurement": "W",
    }
    short, hourly = [], []
    for day, f in flows.items():
        start = importer.nem_day_start(day)
        first, last = _online(f["solar"])
        watts = [v * 12 * 1000 for v in f["solar"]]
        for i in range(first, last + 1):
            short.append({"start": start + i * FIVE, "mean": watts[i], "min": 0, "max": 0})
        for h in range(first // 12, last // 12 + 1):
            mean = math.fsum(watts[h * 12 : h * 12 + 12]) / 12
            hourly.append({"start": start + h * HOUR, "mean": mean, "min": 0, "max": 0})
    get_instance(hass).async_import_statistics(meta, short, StatisticsShortTerm)
    get_instance(hass).async_import_statistics(meta, hourly, Statistics)
    await async_wait_recording_done(hass)
    await _setup_entry(
        hass,
        aioclient_mock,
        FakeAmber(TODAY - timedelta(days=30), YESTERDAY),
        options={"schedule_mode": "automatic", "solar_power": SOLAR_POWER},
        subentries=[_grid_meter(), _appliance()],
    )
    result = await _run(hass)
    chain = result["chains"][CHAIN]
    assert chain["written"] == [d.isoformat() for d in days]
    rows = await _rows(hass, [SOLAR_AWARE])
    expected = [h for d in days for h in _expected(d, flows[d])]
    for row, want in zip(rows[SOLAR_AWARE], expected, strict=True):
        assert row["state"] == pytest.approx(want, abs=1e-6)


# --- days skipped before the fix are worked out again, once ---------------------------------


@pytest.mark.parametrize("skipped", ["all", "first"])
async def test_skipped_days_worked_out_again_once(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    clock: Clock,
    hass_storage: dict,
    sydney_home: None,
    skipped: str,
) -> None:
    """A chain that b3 left with days skipped for sensor data ("all" of them, or the
    "first" with a later day written) is worked out again at the next run: the skipped
    days are written and every later sum re-derived. It happens once."""
    days = [TODAY - timedelta(days=d) for d in (2, 1)]
    entry, flows = await _offline_site(hass, aioclient_mock, hass_storage, days)
    manager = entry.runtime_data.manager
    strict_days = set(days) if skipped == "all" else {days[0]}
    real = solar.async_solar_energy

    async def b3(hass: HomeAssistant, entity_id: str, day: date):
        if day in strict_days:  # b3's rule: any missing row refuses the day
            return await chains.async_sensor_energy(hass, entity_id, day)
        return await real(hass, entity_id, day)

    with patch.object(solar, "async_solar_energy", b3):
        result = await _run(hass)
    chain = result["chains"][CHAIN]
    assert chain["skipped"] == sorted(d.isoformat() for d in strict_days)
    # As a b3 Store: no record of the retry.
    del manager.store._data["chains"][CHAIN]["retried"]

    result = await _run(hass)
    chain = result["chains"][CHAIN]
    assert chain["recalculated"] == sorted(d.isoformat() for d in strict_days)
    state = manager.store._data["chains"][CHAIN]
    assert state["retried"] == manager_mod.RETRY_SOLAR_GAPS
    assert state["rewrite"] is None
    assert state["last_written"] == YESTERDAY.isoformat()
    rows = await _rows(hass, [SOLAR_AWARE])
    expected = [h for d in days for h in _expected(d, flows[d])]
    assert len(rows[SOLAR_AWARE]) == 48
    total = 0.0
    for row, want in zip(rows[SOLAR_AWARE], expected, strict=True):
        total += want
        assert row["state"] == pytest.approx(want, abs=1e-6)
        assert row["sum"] == pytest.approx(total, abs=1e-5)

    # Once only: the next run doesn't retry.
    with patch.object(manager, "_async_retry_solar_gaps") as retry:
        result = await _run(hass)
    retry.assert_not_called()
    assert "recalculated" not in result["chains"][CHAIN]


async def test_retry_waits_for_the_flows(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    clock: Clock,
    hass_storage: dict,
    sydney_home: None,
) -> None:
    """A retry interrupted while the site had its flows doesn't resume while they are
    missing (the chain waits); it resumes once they are back."""
    days = [TODAY - timedelta(days=d) for d in (2, 1)]
    entry, flows = await _offline_site(hass, aioclient_mock, hass_storage, days)
    manager = entry.runtime_data.manager
    await _run(hass)
    state = manager.store._data["chains"][CHAIN]
    state["rewrite"] = {"next": days[0].isoformat(), "to": YESTERDAY.isoformat(), "retry": True}
    flows_before = manager.flows
    manager.flows = None
    result = await _run(hass)
    assert result["chains"][CHAIN]["status"] == "waiting_for_data"
    assert state["rewrite"] is not None
    manager.flows = flows_before
    result = await _run(hass)
    assert result["chains"][CHAIN]["status"] == "caught_up"
    assert state["rewrite"] is None
    rows = await _rows(hass, [SOLAR_AWARE])
    expected = [h for d in days for h in _expected(d, flows[d])]
    for row, want in zip(rows[SOLAR_AWARE], expected, strict=True):
        assert row["state"] == pytest.approx(want, abs=1e-6)


async def test_retry_with_nothing_skipped(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    clock: Clock,
    hass_storage: dict,
    sydney_home: None,
) -> None:
    """A retry with nothing skipped only records that it ran."""
    days = [TODAY - timedelta(days=d) for d in (2, 1)]
    entry, _ = await _offline_site(hass, aioclient_mock, hass_storage, days)
    manager = entry.runtime_data.manager
    chain = next(c for c in manager._secondary_chains() if c.key == CHAIN)
    assert await manager._async_retry_solar_gaps(chain) == []
    assert manager.store._data["chains"][CHAIN]["retried"] == manager_mod.RETRY_SOLAR_GAPS
