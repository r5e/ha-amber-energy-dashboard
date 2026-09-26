"""Manager tests: Store, Guard 1, retention discovery, catch-up walk, schedule, sensors.

All data is synthetic (see fake_amber.py). "Now" is controlled by patching the manager's
clock, not the event loop, so the importer's read-back polling still runs normally.
"""

from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import PropertyMock, patch

from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.amber_energy_dashboard import importer, manager as manager_mod
from custom_components.amber_energy_dashboard.api import RunBudget
from custom_components.amber_energy_dashboard.const import (
    CONF_CHANNELS,
    CONF_FIXED_TIMES,
    CONF_NMI,
    CONF_SCHEDULE_MODE,
    CONF_SITE_ID,
    DOMAIN,
)
from custom_components.amber_energy_dashboard.storage import (
    STORAGE_MINOR_VERSION,
    STORAGE_VERSION,
    schedule_seed_for,
)

from .fake_amber import FakeAmber
from .synthetic import SITE_ID, expected_totals, make_usage_day, site_json

NOW = datetime(2026, 9, 26, 0, 0, tzinfo=UTC)  # 10:00 AEST; NEM today 2026-09-26
TODAY = date(2026, 9, 26)
YESTERDAY = TODAY - timedelta(days=1)
ENTRY_ID = "01TESTENTRY000000000000000"
PREFIX = f"{DOMAIN}:{SITE_ID.lower()}"
ALL_IDS = [
    f"{PREFIX}_e1_energy",
    f"{PREFIX}_e1_cost",
    f"{PREFIX}_b1_energy",
    f"{PREFIX}_b1_compensation",
    f"{PREFIX}_net_cost",
]
E1 = ALL_IDS[0]


class Clock:
    """A settable clock for the manager."""

    def __init__(self) -> None:
        """Start at NOW."""
        self.now = NOW

    def __call__(self) -> datetime:
        """Return the current fake time."""
        return self.now


@pytest.fixture(autouse=True)
def _setup(recorder_mock, enable_custom_integrations: None) -> None:
    """Recorder first (it must precede hass), then allow custom integrations."""


@pytest.fixture
def clock():
    """Patch the manager's clock."""
    fake = Clock()
    with patch.object(manager_mod, "_utcnow", fake):
        yield fake


@pytest.fixture(autouse=True)
def _no_real_timers():
    """The manager's clock is fake but the event loop's is real, so real timers would
    fire at the wrong moment. Tests call ``_on_timer`` directly instead."""
    with patch.object(manager_mod, "async_track_point_in_utc_time", return_value=lambda: None):
        yield


@pytest.fixture(autouse=True)
def _fast_verify():
    """Poll the read-back quickly."""
    with patch.object(importer, "VERIFY_INTERVAL", 0.02):
        yield


@pytest.fixture
async def sydney(hass: HomeAssistant) -> None:
    """Run with HA's time zone set to Australia/Sydney."""
    await hass.config.async_set_time_zone("Australia/Sydney")


def _store_key() -> str:
    return f"{DOMAIN}.{ENTRY_ID}"


def _preload_store(hass_storage: dict, **data: Any) -> None:
    hass_storage[_store_key()] = {
        "version": STORAGE_VERSION,
        "minor_version": STORAGE_MINOR_VERSION,
        "key": _store_key(),
        "data": {
            "marker": None,
            "pending": None,
            "days": {},
            "retention_days": None,
            "retention": None,
            "schedule_seed": schedule_seed_for(ENTRY_ID),
            "last_run": None,
            **data,
        },
    }


async def _setup_entry(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    fake: FakeAmber,
    *,
    startup: bool = False,
    options: dict | None = None,
    site: dict | None = None,
) -> MockConfigEntry:
    fake.install(aioclient_mock, [site or site_json()])
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id=ENTRY_ID,
        unique_id=SITE_ID,
        title="Amber FAKENMI000",
        data={
            CONF_API_KEY: "psk_test",
            CONF_SITE_ID: SITE_ID,
            CONF_NMI: "FAKENMI000",
            CONF_CHANNELS: [
                {"identifier": "E1", "type": "general", "tariff": "EA116"},
                {"identifier": "B1", "type": "feedIn", "tariff": None},
            ],
        },
        options=options or {CONF_SCHEDULE_MODE: "automatic"},
    )
    entry.add_to_hass(hass)
    if startup:
        assert await hass.config_entries.async_setup(entry.entry_id)
    else:
        with patch.object(
            manager_mod.AmberManager,
            "needs_startup_run",
            new_callable=PropertyMock,
            return_value=False,
        ):
            assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _run(hass: HomeAssistant) -> dict[str, Any]:
    return await hass.services.async_call(
        DOMAIN, "run_now", {}, blocking=True, return_response=True
    )


async def _rows(hass: HomeAssistant, ids=ALL_IDS) -> dict[str, list[dict]]:
    await async_wait_recording_done(hass)
    return await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2020, 1, 1, tzinfo=UTC),
        None,
        set(ids),
        "hour",
        None,
        {"state", "sum"},
    )


def _assert_contiguous(rows: dict[str, list[dict]], first_day: date, last_day: date) -> None:
    """Every statistic has exactly one row per hour, and sums run on without a break."""
    hours = (
        int(
            (importer.nem_day_start(last_day) - importer.nem_day_start(first_day)).total_seconds()
            // 3600
        )
        + 24
    )
    expected = [importer.nem_day_start(first_day) + timedelta(hours=h) for h in range(hours)]
    for sid in ALL_IDS:
        starts = [datetime.fromtimestamp(r["start"], UTC) for r in rows[sid]]
        assert starts == expected, sid
        assert rows[sid][0]["sum"] == pytest.approx(rows[sid][0]["state"], abs=1e-6)
        for prev, cur in zip(rows[sid], rows[sid][1:], strict=False):
            assert cur["sum"] == pytest.approx(prev["sum"] + cur["state"], abs=1e-6)


def _fake_total(fake: FakeAmber, first: date, last: date, channel: str = "E1") -> float:
    return expected_totals(fake.records(first, last))[channel]["kwh"]


# --- retention discovery ----------------------------------------------------------------


async def _discover(hass, aioclient_mock, fake, site=None) -> tuple[int, dict]:
    entry = await _setup_entry(hass, aioclient_mock, fake, site=site)
    mgr = entry.runtime_data.manager
    mgr.ctx.client.start_run(RunBudget())
    await mgr._async_discover_retention()
    return mgr.store.retention_days, mgr.store.retention


@pytest.mark.parametrize(
    ("boundary_days_ago", "expected_days", "max_calls"),
    [
        (89, 89, 2),  # the common case: 2 calls
        (88, 88, 2),
        (95, 95, 8),  # older than expected: bracket then bisect
        (110, 110, 8),
        (75, 75, 8),  # newer than expected
        (60, 60, 8),
        (130, 89, 3),  # cannot bracket within 30 days: fallback 89
        (40, 89, 3),  # too new to bracket (no activeFrom): fallback 89
    ],
)
async def test_retention_bisection(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    clock: Clock,
    boundary_days_ago: int,
    expected_days: int,
    max_calls: int,
) -> None:
    """Discovery finds the earliest day with data in at most 8 single-day calls."""
    fake = FakeAmber(TODAY - timedelta(days=boundary_days_ago), YESTERDAY)
    days, info = await _discover(hass, aioclient_mock, fake)

    assert days == expected_days
    assert len(fake.calls) <= max_calls <= 8
    assert all(start == end for start, end in fake.calls)  # single-day requests only
    assert info["calls"] == len(fake.calls)
    assert info["method"].startswith(
        "fallback" if expected_days == 89 and boundary_days_ago != 89 else "bisection"
    )
    assert info["boundary"] == (TODAY - timedelta(days=expected_days)).isoformat()


@pytest.mark.parametrize("boundary", range(55, 126, 7))
async def test_retention_call_cap(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, boundary: int
) -> None:
    """Whatever the boundary, discovery never makes more than 8 calls."""
    fake = FakeAmber(TODAY - timedelta(days=boundary), YESTERDAY)
    await _discover(hass, aioclient_mock, fake)
    assert len(fake.calls) <= 8


async def test_retention_young_site_uses_active_from(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock
) -> None:
    """A site younger than the window: one probe at activeFrom settles it."""
    start = TODAY - timedelta(days=20)
    fake = FakeAmber(start, YESTERDAY)
    days, _info = await _discover(
        hass, aioclient_mock, fake, site=site_json(activeFrom=start.isoformat())
    )
    assert days == 20
    assert fake.calls == [(start, start)]


async def test_retention_api_error_falls_back(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock
) -> None:
    """An API failure during discovery falls back to 89 days."""
    fake = FakeAmber(TODAY - timedelta(days=89), YESTERDAY)
    entry = await _setup_entry(hass, aioclient_mock, fake)
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"https://api.amber.com.au/v1/sites/{SITE_ID}/usage", status=503)
    mgr = entry.runtime_data.manager
    mgr.ctx.client.start_run(RunBudget())
    await mgr._async_discover_retention()
    assert mgr.store.retention_days == 89
    assert mgr.store.retention["method"] == "fallback (AmberServerError)"


# --- first setup and catch-up --------------------------------------------------------------


async def test_first_setup_discovers_then_catches_up(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock
) -> None:
    """On first setup the run starts by itself, finds the boundary and imports to yesterday."""
    start = TODAY - timedelta(days=12)
    fake = FakeAmber(start, YESTERDAY)
    entry = await _setup_entry(
        hass, aioclient_mock, fake, startup=True, site=site_json(activeFrom=start.isoformat())
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    mgr = entry.runtime_data.manager
    assert mgr.store.retention_days == 12
    assert mgr.store.marker == YESTERDAY
    assert mgr.store.last_run["status"] == "caught_up"
    assert mgr.store.last_run["trigger"] == "first_setup"
    # 1 discovery probe, then 7-day windows: [start..+6], [+7..yesterday]
    assert fake.calls == [
        (start, start),
        (start, start + timedelta(days=6)),
        (start + timedelta(days=7), YESTERDAY),
    ]
    rows = await _rows(hass)
    _assert_contiguous(rows, start, YESTERDAY)
    assert rows[E1][-1]["sum"] == pytest.approx(_fake_total(fake, start, YESTERDAY), abs=1e-6)


async def test_catch_up_across_several_windows(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """20 days: three fetch windows (7, 7, 6), validated and written per day."""
    _preload_store(hass_storage, retention_days=20)
    start = TODAY - timedelta(days=20)
    fake = FakeAmber(start, YESTERDAY)
    entry = await _setup_entry(hass, aioclient_mock, fake)

    result = await _run(hass)

    assert result["status"] == "caught_up"
    assert len(result["imported_days"]) == 20
    assert fake.calls == [
        (start, start + timedelta(days=6)),
        (start + timedelta(days=7), start + timedelta(days=13)),
        (start + timedelta(days=14), YESTERDAY),
    ]
    assert result["calls"] == {"sites_usage": 3}
    _assert_contiguous(await _rows(hass), start, YESTERDAY)
    store = entry.runtime_data.manager.store
    assert store.marker == YESTERDAY and store.pending is None
    assert store.day(start)["status"] == "imported"
    assert hass_storage[_store_key()]["data"]["marker"] == YESTERDAY.isoformat()  # persisted


# --- Guard 1 -----------------------------------------------------------------------------


async def test_guard1_skips_imported_days(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Days at or before the marker are never fetched or written again."""
    _preload_store(hass_storage, retention_days=5)
    fake = FakeAmber(TODAY - timedelta(days=30), TODAY + timedelta(days=5))
    await _setup_entry(hass, aioclient_mock, fake)
    await _run(hass)
    calls = len(fake.calls)
    before = await _rows(hass)

    again = await _run(hass)
    assert again["status"] == "caught_up"
    assert len(fake.calls) == calls  # nothing fetched: marker is yesterday
    assert await _rows(hass) == before

    clock.now += timedelta(days=1)  # a new day: only it is fetched
    nxt = await _run(hass)
    assert nxt["imported_days"] == [TODAY.isoformat()]
    assert fake.calls[-1] == (TODAY, TODAY)
    _assert_contiguous(await _rows(hass), TODAY - timedelta(days=5), TODAY)


@pytest.mark.parametrize(
    "marker_shift",
    [-2, 1],
    ids=["marker-behind-statistics", "marker-ahead-of-statistics"],
)
async def test_guard1_mismatch_raises_repairs_and_writes_nothing(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    clock: Clock,
    hass_storage: dict,
    marker_shift: int,
) -> None:
    """A marker that disagrees with the statistics stops the run with a Repairs issue."""
    _preload_store(hass_storage, retention_days=5)
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    entry = await _setup_entry(hass, aioclient_mock, fake)
    await _run(hass)
    before = await _rows(hass)
    calls = len(fake.calls)

    # Corrupt the stored marker, as an edit on disk would, and reload the entry.
    hass_storage[_store_key()]["data"]["marker"] = (
        YESTERDAY + timedelta(days=marker_shift)
    ).isoformat()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    result = await _run(hass)

    assert result["status"] == "needs_attention"
    assert result["reason"] == "marker_mismatch"
    assert len(fake.calls) == calls
    assert await _rows(hass) == before
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"marker_mismatch_{ENTRY_ID}")
    assert issue is not None and issue.translation_key == "marker_mismatch"
    assert "statistics end at" in issue.translation_placeholders["details"]
    # import_day refuses too
    with pytest.raises(importer.ImportRefusedError, match="disagree"):
        await hass.services.async_call(
            DOMAIN, "import_day", {"date": TODAY.isoformat()}, blocking=True
        )

    # Restoring the marker clears the issue on the next run.
    hass_storage[_store_key()]["data"]["marker"] = YESTERDAY.isoformat()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert (await _run(hass))["status"] == "caught_up"
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"marker_mismatch_{ENTRY_ID}") is None


async def test_guard1_statistics_without_marker(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock
) -> None:
    """Statistics that exist without any marker (lost Store) are not guessed at."""
    fake = FakeAmber(TODAY - timedelta(days=89), YESTERDAY)
    await _setup_entry(hass, aioclient_mock, fake)
    async_add_external_statistics(
        hass,
        {
            "has_sum": True,
            "mean_type": StatisticMeanType.NONE,
            "name": "x",
            "source": DOMAIN,
            "statistic_id": E1,
            "unit_class": "energy",
            "unit_of_measurement": "kWh",
        },
        [{"start": importer.nem_day_start(YESTERDAY), "state": 1.0, "sum": 1.0}],
    )
    await async_wait_recording_done(hass)

    result = await _run(hass)

    assert result["reason"] == "marker_mismatch"
    assert fake.calls == []  # stopped before discovery or any fetch


# --- budget --------------------------------------------------------------------------------


async def test_budget_exhaustion_stops_and_resumes(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """A spent budget is a planned stop; the next run resumes from the marker."""
    _preload_store(hass_storage, retention_days=20)
    start = TODAY - timedelta(days=20)
    fake = FakeAmber(start, YESTERDAY)
    entry = await _setup_entry(hass, aioclient_mock, fake)

    with patch.object(manager_mod, "RunBudget", lambda: RunBudget(max_calls_per_counter=2)):
        first = await _run(hass)
    assert first["status"] == "budget_exhausted"
    assert len(first["imported_days"]) == 14
    assert entry.runtime_data.manager.store.marker == start + timedelta(days=13)
    assert ir.async_get(hass).issues == {}  # not a failure

    second = await _run(hass)
    assert second["status"] == "caught_up"
    assert second["imported_days"][0] == (start + timedelta(days=14)).isoformat()
    _assert_contiguous(await _rows(hass), start, YESTERDAY)


async def test_rate_limit_reserve_stops_run(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """When another client drains the shared budget, the run stops at the reserve."""
    _preload_store(hass_storage, retention_days=20)
    start = TODAY - timedelta(days=20)
    fake = FakeAmber(start, YESTERDAY)
    fake.remaining = 16  # responses report 15 (still >= reserve 15), then 14 (< 15): stop
    await _setup_entry(hass, aioclient_mock, fake)

    result = await _run(hass)

    assert result["status"] == "budget_exhausted"
    assert "reserve" in result["reason"]
    assert len(fake.calls) == 2
    assert len(result["imported_days"]) == 14
    assert result["rate_limit_remaining"] == {"sites_usage": 14}


# --- interruption and recovery -------------------------------------------------------------


@pytest.mark.parametrize(
    "written", [5, 2, 0], ids=["fully-written", "partly-written", "not-written"]
)
async def test_restart_mid_walk_resumes_cleanly(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    clock: Clock,
    hass_storage: dict,
    written: int,
) -> None:
    """A crash between writing a day and advancing the marker is recovered on restart.

    The Store says marker = D-1, pending = D, last run 'running'. D's statistics are all,
    some or none written. On restart the run resumes by itself, rewrites D from the
    marker's baseline and continues, with no duplicate or missing hour.
    """
    _preload_store(hass_storage, retention_days=10)
    start = TODAY - timedelta(days=10)
    fake = FakeAmber(start, YESTERDAY)
    entry = await _setup_entry(hass, aioclient_mock, fake)
    mgr = entry.runtime_data.manager
    crash_day = start + timedelta(days=4)

    # Import up to the day before the crash day (stop the walk there with an empty day).
    fake.empty = {crash_day}
    await _run(hass)
    assert mgr.store.marker == crash_day - timedelta(days=1)
    fake.empty = set()

    # Simulate the crash: pending set, some statistics of the crash day written.
    await mgr.store.async_set_pending(crash_day)
    await mgr.store.async_set_last_run({"status": "running", "trigger": "scheduled"})
    baselines = await importer.async_baselines_before(hass, ALL_IDS, crash_day)
    by_channel = importer.check_completeness(
        [importer_parse(r) for r in fake.records(crash_day, crash_day)], crash_day, mgr.ctx.channels
    )
    amounts = importer.hourly_amounts(by_channel, mgr.ctx.specs, crash_day)
    for spec in mgr.ctx.specs[:written]:
        async_add_external_statistics(
            hass,
            spec.metadata(),
            importer.build_rows(
                crash_day, amounts[spec.statistic_id], baselines[spec.statistic_id]
            ),
        )
    await async_wait_recording_done(hass)

    # Restart: the entry reloads and resumes by itself.
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    mgr = entry.runtime_data.manager
    assert mgr.store.last_run["trigger"] == "resume_after_interruption"
    assert mgr.store.last_run["status"] == "caught_up"
    assert mgr.store.last_run["imported_days"][0] == crash_day.isoformat()
    assert mgr.store.day(crash_day)["mode"] == ("recovery" if written else "catch_up")
    assert mgr.store.pending is None
    rows = await _rows(hass)
    _assert_contiguous(rows, start, YESTERDAY)
    assert rows[E1][-1]["sum"] == pytest.approx(_fake_total(fake, start, YESTERDAY), abs=1e-6)


def importer_parse(record: dict):
    """Parse a raw record with the client's parser."""
    from custom_components.amber_energy_dashboard.api import _parse_usage  # noqa: PLC0415

    return _parse_usage(record)


async def test_failure_after_write_is_recovered_next_run(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """If advancing the marker fails after a verified write, the next run recovers the day."""
    _preload_store(hass_storage, retention_days=6)
    start = TODAY - timedelta(days=6)
    fake = FakeAmber(start, YESTERDAY)
    entry = await _setup_entry(hass, aioclient_mock, fake)
    store = entry.runtime_data.manager.store
    real = store.async_mark_imported
    count = 0

    async def flaky(day, summary):
        nonlocal count
        count += 1
        if count == 3:
            raise OSError("disk full")
        await real(day, summary)

    with patch.object(store, "async_mark_imported", flaky):
        first = await _run(hass)
    assert first["status"] == "needs_attention"
    assert first["reason"] == "internal_error"
    assert store.marker == start + timedelta(days=1)
    assert store.pending == start + timedelta(days=2)

    second = await _run(hass)
    assert second["status"] == "caught_up"
    assert store.day(start + timedelta(days=2))["mode"] == "recovery"
    _assert_contiguous(await _rows(hass), start, YESTERDAY)


# --- empty days and Guard 3 ------------------------------------------------------------------


async def test_empty_day_stops_the_walk(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """An empty day (no data yet) stops the walk there; later days wait for it."""
    _preload_store(hass_storage, retention_days=10)
    start = TODAY - timedelta(days=10)
    fake = FakeAmber(start, YESTERDAY)
    gap = TODAY - timedelta(days=4)
    fake.empty = {gap}
    entry = await _setup_entry(hass, aioclient_mock, fake)
    store = entry.runtime_data.manager.store

    result = await _run(hass)

    assert result["status"] == "waiting_for_data"
    assert result["waiting_for"] == gap.isoformat()
    assert store.marker == gap - timedelta(days=1)
    assert store.day(gap)["status"] == "empty"
    assert store.day(gap + timedelta(days=1)) is None
    _assert_contiguous(await _rows(hass), start, gap - timedelta(days=1))
    assert ir.async_get(hass).issues == {}

    fake.empty = set()
    assert (await _run(hass))["status"] == "caught_up"
    _assert_contiguous(await _rows(hass), start, YESTERDAY)


async def test_yesterday_not_published_yet(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """The everyday case: all but yesterday imported, yesterday not yet published."""
    _preload_store(hass_storage, retention_days=3)
    fake = FakeAmber(TODAY - timedelta(days=3), YESTERDAY - timedelta(days=1))
    await _setup_entry(hass, aioclient_mock, fake)

    result = await _run(hass)

    assert result["status"] == "waiting_for_data"
    assert result["waiting_for"] == YESTERDAY.isoformat()


@pytest.mark.parametrize(
    ("mutate", "issue"),
    [
        (
            lambda recs: recs.extend({**r, "channelIdentifier": "E2"} for r in recs[:288]),
            "unexpected_channel",
        ),
        (lambda recs: recs.pop(10), "incomplete_data"),
    ],
    ids=["unexpected_channel", "incomplete_day"],
)
async def test_guard3_raises_repairs_issue_and_clears(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    clock: Clock,
    hass_storage: dict,
    mutate,
    issue: str,
) -> None:
    """Guard 3 stops the walk at the bad day with an explanatory Repairs issue."""
    _preload_store(hass_storage, retention_days=6)
    start = TODAY - timedelta(days=6)
    bad = start + timedelta(days=3)
    fake = FakeAmber(start, YESTERDAY)
    fake.mutate = lambda day, recs: mutate(recs) if day == bad else None
    entry = await _setup_entry(hass, aioclient_mock, fake)
    store = entry.runtime_data.manager.store

    result = await _run(hass)

    assert result["status"] == "needs_attention"
    assert store.marker == bad - timedelta(days=1)
    assert store.pending is None
    assert store.day(bad)["status"] == "failed"
    found = ir.async_get(hass).async_get_issue(DOMAIN, f"{issue}_{ENTRY_ID}")
    assert found is not None and found.translation_placeholders["day"] == bad.isoformat()
    _assert_contiguous(await _rows(hass), start, bad - timedelta(days=1))

    fake.mutate = None
    assert (await _run(hass))["status"] == "caught_up"
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"{issue}_{ENTRY_ID}") is None


# --- import_day with the Store -----------------------------------------------------------------


async def test_import_day_advances_marker_and_walk_continues(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """import_day keeps the marker in step, so the next run carries on from it."""
    _preload_store(hass_storage, retention_days=5)
    start = TODAY - timedelta(days=5)
    fake = FakeAmber(start, YESTERDAY)
    entry = await _setup_entry(hass, aioclient_mock, fake)
    for d in (start, start + timedelta(days=1)):
        await hass.services.async_call(DOMAIN, "import_day", {"date": d.isoformat()}, blocking=True)
    assert entry.runtime_data.manager.store.marker == start + timedelta(days=1)

    result = await _run(hass)
    assert result["imported_days"][0] == (start + timedelta(days=2)).isoformat()
    _assert_contiguous(await _rows(hass), start, YESTERDAY)


# --- scheduling ---------------------------------------------------------------------------


async def test_fixed_schedule_runs_then_skips_when_caught_up(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    clock: Clock,
    hass_storage: dict,
    sydney,
) -> None:
    """07:15 runs the catch-up; 10:15 and 13:15 are skipped once caught up."""
    _preload_store(hass_storage, retention_days=3)
    clock.now = datetime(2026, 9, 25, 20, 0, tzinfo=UTC)  # 06:00 AEST on 26 Sep
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    entry = await _setup_entry(
        hass,
        aioclient_mock,
        fake,
        options={CONF_SCHEDULE_MODE: "fixed", CONF_FIXED_TIMES: ["07:15", "10:15", "13:15"]},
    )
    mgr = entry.runtime_data.manager
    first = datetime(2026, 9, 25, 21, 15, tzinfo=UTC)  # 07:15 AEST
    assert mgr.next_run == first

    with patch.object(manager_mod, "async_track_point_in_utc_time") as track:
        clock.now = first
        mgr._on_timer(first)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert mgr.store.last_run["trigger"] == "scheduled"
        assert mgr.store.marker == YESTERDAY
        calls = len(fake.calls)
        assert mgr.next_run == datetime(2026, 9, 26, 0, 15, tzinfo=UTC)  # 10:15 AEST
        assert track.call_args[0][2] == mgr.next_run

        clock.now = mgr.next_run
        mgr._on_timer(mgr.next_run)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert len(fake.calls) == calls  # skipped: caught up
        assert mgr.next_run == datetime(2026, 9, 26, 3, 15, tzinfo=UTC)  # 13:15 AEST

        clock.now = mgr.next_run
        mgr._on_timer(mgr.next_run)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert mgr.next_run == datetime(2026, 9, 26, 21, 15, tzinfo=UTC)  # tomorrow 07:15


async def test_automatic_schedule_is_seeded_from_entry(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, sydney
) -> None:
    """The automatic first attempt is stable for the entry and inside 06:30 to 08:30."""
    clock.now = datetime(2026, 9, 25, 19, 0, tzinfo=UTC)  # 05:00 AEST
    fake = FakeAmber(TODAY, TODAY)
    entry = await _setup_entry(hass, aioclient_mock, fake)
    mgr = entry.runtime_data.manager
    local = mgr.next_run.astimezone(mgr._tz)
    seed = schedule_seed_for(ENTRY_ID)
    minutes = 6 * 60 + 30 + seed % 120
    assert (local.hour, local.minute) == (minutes // 60, minutes % 60)
    assert mgr.store.schedule_seed == seed


# --- sensors -----------------------------------------------------------------------------


async def test_display_sensors(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    clock: Clock,
    hass_storage: dict,
    sydney,
) -> None:
    """Status, last date, days behind, next run and yesterday's values; no state_class."""
    _preload_store(hass_storage, retention_days=3)
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    await _setup_entry(hass, aioclient_mock, fake)
    status = hass.states.get("sensor.amber_energy_dashboard_import_status")
    assert status.state == "never_run"

    await _run(hass)
    await hass.async_block_till_done()

    states = {s.entity_id: s for s in hass.states.async_all("sensor")}
    assert states["sensor.amber_energy_dashboard_import_status"].state == "caught_up"
    assert states["sensor.amber_energy_dashboard_last_imported_date"].state == YESTERDAY.isoformat()
    assert states["sensor.amber_energy_dashboard_days_behind"].state == "0"
    assert states["sensor.amber_energy_dashboard_next_scheduled_run"].state not in (
        "unknown",
        "unavailable",
    )
    totals = expected_totals(make_usage_day(YESTERDAY))
    e1 = states["sensor.amber_energy_dashboard_yesterday_e1_energy"]
    assert float(e1.state) == pytest.approx(totals["E1"]["kwh"], abs=1e-6)
    assert e1.attributes["unit_of_measurement"] == "kWh"
    assert e1.attributes["date"] == YESTERDAY.isoformat()
    cost = states["sensor.amber_energy_dashboard_yesterday_e1_cost"]
    assert float(cost.state) == pytest.approx(totals["E1"]["cents"] / 100, abs=1e-6)
    comp = states["sensor.amber_energy_dashboard_yesterday_b1_compensation"]
    assert float(comp.state) == pytest.approx(-totals["B1"]["cents"] / 100, abs=1e-6)
    assert "sensor.amber_energy_dashboard_yesterday_b1_energy" in states
    for state in states.values():
        if state.entity_id.startswith("sensor.amber_energy_dashboard"):
            assert "state_class" not in state.attributes, state.entity_id


async def test_run_now_returns_summary_and_diagnostics_show_store(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """run_now returns the run summary; diagnostics include the Store and schedule."""
    from custom_components.amber_energy_dashboard.diagnostics import (  # noqa: PLC0415
        async_get_config_entry_diagnostics,
    )

    _preload_store(hass_storage, retention_days=2)
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    entry = await _setup_entry(hass, aioclient_mock, fake)

    result = await _run(hass)
    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert result["trigger"] == "run_now"
    assert result["marker"] == YESTERDAY.isoformat()
    assert diag["store"]["marker"] == YESTERDAY.isoformat()
    assert diag["schedule"]["mode"] == "automatic"
    assert diag["status"] == "caught_up"
    assert "FAKENMI000" not in repr(diag)
    assert len(diag["store"]["days"]) == 2


# --- failure outcomes of a run ----------------------------------------------------------


@pytest.mark.parametrize(
    ("mock", "status"),
    [
        ({"status": 429, "headers": {"Retry-After": "60"}}, "rate_limited"),
        ({"status": 403, "text": '{"message":"denied"}'}, "auth_failed"),
        ({"status": 503}, "api_unavailable"),
        ({"text": "<html>"}, "api_unavailable"),
    ],
    ids=["429", "403", "503", "malformed"],
)
async def test_run_api_failures_are_outcomes(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    clock: Clock,
    hass_storage: dict,
    mock: dict,
    status: str,
) -> None:
    """API failures end the run with a status (never an exception); 403 starts reauth."""
    _preload_store(hass_storage, retention_days=3)
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    entry = await _setup_entry(hass, aioclient_mock, fake)
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"https://api.amber.com.au/v1/sites/{SITE_ID}/usage", **mock)

    result = await _run(hass)

    assert result["status"] == status
    assert entry.runtime_data.manager.store.marker is None
    flows = [f["context"]["source"] for f in hass.config_entries.flow.async_progress()]
    assert flows == (["reauth"] if status == "auth_failed" else [])


async def test_record_outside_window_is_rejected(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """A window response containing a day that was not asked for is a data error."""
    _preload_store(hass_storage, retention_days=3)
    start = TODAY - timedelta(days=3)
    fake = FakeAmber(start, YESTERDAY)
    fake.overrides[start] = make_usage_day(start) + make_usage_day(start - timedelta(days=1))
    await _setup_entry(hass, aioclient_mock, fake)

    result = await _run(hass)

    assert result["status"] == "needs_attention"
    assert result["reason"] == "wrong_date"


async def test_verification_failure_in_walk(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """A write that does not read back stops the walk with an import_failed issue."""
    _preload_store(hass_storage, retention_days=3)
    fake = FakeAmber(TODAY - timedelta(days=3), YESTERDAY)
    entry = await _setup_entry(hass, aioclient_mock, fake)

    with (
        patch.object(importer, "async_add_external_statistics"),
        patch.object(importer, "VERIFY_TIMEOUT", 0.1),
    ):
        result = await _run(hass)

    assert result["reason"] == "verification_failed"
    assert entry.runtime_data.manager.store.marker is None
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"import_failed_{ENTRY_ID}") is not None


@pytest.mark.parametrize("mock", [{"status": 403}, "budget"], ids=["auth", "budget"])
async def test_discovery_stops_run_on_auth_or_budget(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, mock
) -> None:
    """Auth failure or a spent budget during discovery stores the fallback and stops."""
    fake = FakeAmber(TODAY - timedelta(days=89), YESTERDAY)
    entry = await _setup_entry(hass, aioclient_mock, fake)
    if mock == "budget":
        fake.remaining = 15  # first response reports 14 (< reserve): next call refused
        expected = "budget_exhausted"
    else:
        aioclient_mock.clear_requests()
        aioclient_mock.get(f"https://api.amber.com.au/v1/sites/{SITE_ID}/usage", **mock)
        expected = "auth_failed"

    result = await _run(hass)

    assert result["status"] == expected
    store = entry.runtime_data.manager.store
    assert store.retention_days == 89
    assert store.retention["method"].startswith("fallback")


async def test_young_site_without_data_falls_back(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock
) -> None:
    """activeFrom is recent but has no data yet: fall back rather than guess."""
    start = TODAY - timedelta(days=5)
    fake = FakeAmber(TODAY + timedelta(days=1), TODAY + timedelta(days=1))
    days, info = await _discover(
        hass, aioclient_mock, fake, site=site_json(activeFrom=start.isoformat())
    )
    assert days == 89
    assert info["method"] == "fallback (not bracketed)"
    assert fake.calls == [(start, start)]


async def test_import_day_during_pending_recovery(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """With a pending day, import_day only accepts that day, and recovers it."""
    _preload_store(hass_storage, retention_days=4)
    start = TODAY - timedelta(days=4)
    fake = FakeAmber(start, YESTERDAY)
    entry = await _setup_entry(hass, aioclient_mock, fake)
    mgr = entry.runtime_data.manager
    await hass.services.async_call(DOMAIN, "import_day", {"date": start.isoformat()}, blocking=True)
    pending = start + timedelta(days=1)
    await mgr.store.async_set_pending(pending)
    # A write of the pending day reached the statistics before the "crash".
    baselines = await importer.async_baselines_before(hass, ALL_IDS, pending)
    by_channel = importer.check_completeness(
        [importer_parse(r) for r in fake.records(pending, pending)], pending, mgr.ctx.channels
    )
    amounts = importer.hourly_amounts(by_channel, mgr.ctx.specs, pending)
    for spec in mgr.ctx.specs:
        async_add_external_statistics(
            hass,
            spec.metadata(),
            importer.build_rows(pending, amounts[spec.statistic_id], baselines[spec.statistic_id]),
        )
    await async_wait_recording_done(hass)

    with pytest.raises(importer.ImportRefusedError, match="was being written"):
        await hass.services.async_call(
            DOMAIN, "import_day", {"date": (pending + timedelta(days=1)).isoformat()}, blocking=True
        )
    result = await hass.services.async_call(
        DOMAIN, "import_day", {"date": pending.isoformat()}, blocking=True, return_response=True
    )
    assert result["mode"] == "recovery"
    assert mgr.store.marker == pending and mgr.store.pending is None
    _assert_contiguous(await _rows(hass), start, pending)


async def test_import_day_guard3_and_verification_in_service(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Service-path Guard 3 and verification failures record the day and raise issues."""
    _preload_store(hass_storage, retention_days=4)
    start = TODAY - timedelta(days=4)
    fake = FakeAmber(start, YESTERDAY)
    fake.mutate = lambda day, recs: recs.pop(0)
    entry = await _setup_entry(hass, aioclient_mock, fake)
    store = entry.runtime_data.manager.store

    with pytest.raises(importer.IncompleteDataError):
        await hass.services.async_call(
            DOMAIN, "import_day", {"date": start.isoformat()}, blocking=True
        )
    assert store.day(start)["status"] == "failed" and store.pending is None
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"incomplete_data_{ENTRY_ID}") is not None

    fake.mutate = None
    with (
        patch.object(importer, "async_add_external_statistics"),
        patch.object(importer, "VERIFY_TIMEOUT", 0.1),
        pytest.raises(importer.VerificationFailedError),
    ):
        await hass.services.async_call(
            DOMAIN, "import_day", {"date": start.isoformat()}, blocking=True
        )
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"import_failed_{ENTRY_ID}") is not None


async def test_plan_day_defensive_checks(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock
) -> None:
    """The append-only planner still refuses inconsistent history on its own."""
    fake = FakeAmber(TODAY, TODAY)
    entry = await _setup_entry(hass, aioclient_mock, fake)
    ctx = entry.runtime_data.context
    meta = {
        "has_sum": True,
        "mean_type": StatisticMeanType.NONE,
        "name": "x",
        "source": DOMAIN,
        "unit_class": "energy",
        "unit_of_measurement": "kWh",
    }
    # Only one statistic has history.
    async_add_external_statistics(
        hass,
        {**meta, "statistic_id": E1},
        [
            {
                "start": importer.nem_day_start(YESTERDAY) + timedelta(hours=h),
                "state": 1.0,
                "sum": h + 1.0,
            }
            for h in range(24)
        ],
    )
    await async_wait_recording_done(hass)
    with pytest.raises(importer.ImportRefusedError) as exc_info:
        await importer.async_plan_day(hass, ctx, TODAY)
    assert exc_info.value.reason == "inconsistent_history"
    # Same end hour for all but not at a day boundary.
    for sid in ALL_IDS:
        unit = ("kWh", "energy") if sid.endswith("energy") else ("AUD", None)
        async_add_external_statistics(
            hass,
            {**meta, "statistic_id": sid, "unit_of_measurement": unit[0], "unit_class": unit[1]},
            [
                {
                    "start": importer.nem_day_start(TODAY) + timedelta(hours=3),
                    "state": 1.0,
                    "sum": 1.0,
                }
            ],
        )
    await async_wait_recording_done(hass)
    with pytest.raises(importer.ImportRefusedError) as exc_info:
        await importer.async_plan_day(hass, ctx, TODAY)
    assert exc_info.value.reason == "partial_history"
    # And a baseline read with no row before the day.
    with pytest.raises(importer.ImportRefusedError, match="no row for the hour before"):
        await importer.async_baselines_before(hass, ALL_IDS, TODAY - timedelta(days=10))


async def test_plan_day_different_end_hours(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock
) -> None:
    """Statistics ending at different hours are refused by the planner itself."""
    fake = FakeAmber(TODAY, TODAY)
    entry = await _setup_entry(hass, aioclient_mock, fake)
    for i, sid in enumerate(ALL_IDS):
        unit = ("kWh", "energy") if sid.endswith("energy") else ("AUD", None)
        async_add_external_statistics(
            hass,
            {
                "has_sum": True,
                "mean_type": StatisticMeanType.NONE,
                "name": sid,
                "source": DOMAIN,
                "statistic_id": sid,
                "unit_class": unit[1],
                "unit_of_measurement": unit[0],
            },
            [
                {
                    "start": importer.nem_day_start(YESTERDAY) + timedelta(hours=23 - (i == 0)),
                    "state": 1.0,
                    "sum": 1.0,
                }
            ],
        )
    await async_wait_recording_done(hass)
    with pytest.raises(importer.ImportRefusedError) as exc_info:
        await importer.async_plan_day(hass, entry.runtime_data.context, TODAY)
    assert exc_info.value.reason == "inconsistent_history"


# --- store ---------------------------------------------------------------------------------


async def test_store_edge_cases(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Later problems on an imported day are noted without losing the import record."""
    from custom_components.amber_energy_dashboard.storage import (  # noqa: PLC0415
        MAX_DAY_ENTRIES,
        _VersionedStore,
    )

    _preload_store(hass_storage, retention_days=2)
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    entry = await _setup_entry(hass, aioclient_mock, fake)
    store = entry.runtime_data.manager.store
    await _run(hass)

    await store.async_mark_day(YESTERDAY, "failed", "incomplete_day")
    record = store.day(YESTERDAY)
    assert record["status"] == "imported"
    assert record["last_problem"] == {"status": "failed", "reason": "incomplete_day"}
    await store.async_clear_pending()  # no-op when nothing is pending

    for i in range(MAX_DAY_ENTRIES + 5):
        await store.async_mark_day(date(2020, 1, 1) + timedelta(days=i), "empty", "no_data")
    assert len(store.as_dict()["days"]) == MAX_DAY_ENTRIES

    migrator = _VersionedStore(hass, 1, "x")
    assert await migrator._async_migrate_func(1, 0, {"a": 1}) == {"a": 1}
    with pytest.raises(NotImplementedError):
        await migrator._async_migrate_func(2, 0, {})


async def test_removing_entry_deletes_store(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Removing the entry deletes its Store; statistics are left alone."""
    _preload_store(hass_storage, retention_days=2)
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    entry = await _setup_entry(hass, aioclient_mock, fake)
    await _run(hass)
    assert _store_key() in hass_storage

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert _store_key() not in hass_storage
    assert len((await _rows(hass))[E1]) == 48


async def test_active_from_prunes_probes_for_older_site(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock
) -> None:
    """Days before activeFrom are known to be empty and are never requested."""
    start = TODAY - timedelta(days=100)
    fake = FakeAmber(start, YESTERDAY)
    days, _info = await _discover(
        hass, aioclient_mock, fake, site=site_json(activeFrom=start.isoformat())
    )
    assert days == 100
    assert all(s >= start for s, _e in fake.calls)


async def test_discovery_call_cap_is_enforced(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock
) -> None:
    """If the cap is reached before the boundary is found, fall back to 89 days."""
    fake = FakeAmber(TODAY - timedelta(days=100), YESTERDAY)
    with patch.object(manager_mod, "RETENTION_DISCOVERY_MAX_CALLS", 3):
        days, info = await _discover(hass, aioclient_mock, fake)
    assert days == 89
    assert info["method"] == "fallback (_DiscoveryIncomplete)"
    assert len(fake.calls) == 3
