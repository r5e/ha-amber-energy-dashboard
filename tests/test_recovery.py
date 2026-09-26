"""Milestone 4: retention re-verify, patience, revisions and tail rewrite, Repairs,
reconfigure and Store migration. All data is synthetic."""

from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.amber_energy_dashboard import importer, manager as manager_mod
from custom_components.amber_energy_dashboard.const import (
    CONF_FIXED_TIMES,
    CONF_PATIENCE_DAYS,
    CONF_REVISION_DAYS,
    CONF_SCHEDULE_MODE,
    DOMAIN,
)
from custom_components.amber_energy_dashboard.storage import schedule_seed_for

from .fake_amber import FakeAmber
from .synthetic import DEFAULT_CHANNELS, SITE_ID, expected_totals, make_usage_day, site_json
from .test_manager import (  # noqa: F401 - fixtures are used by name
    ALL_IDS,
    E1,
    ENTRY_ID,
    NOW,
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

VERIFIED_YESTERDAY = {"last_verified": (TODAY - timedelta(days=1)).isoformat(), "method": "preset"}
E2_CHANNELS = (*DEFAULT_CHANNELS, ("E2", "controlledLoad", None))


def _issue(hass: HomeAssistant, kind: str):
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{kind}_{ENTRY_ID}")


def _hours(rows: dict, sid: str) -> list[datetime]:
    return [datetime.fromtimestamp(r["start"], UTC) for r in rows[sid]]


def _assert_sums_continuous(rows: dict) -> None:
    for sid, sid_rows in rows.items():
        assert len({r["start"] for r in sid_rows}) == len(sid_rows), f"duplicate hour in {sid}"
        for prev, cur in pairwise(sid_rows):
            assert cur["sum"] == pytest.approx(prev["sum"] + cur["state"], abs=1e-6), sid


# --- retention re-verify -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("earliest_days_ago", "expected", "method_start", "max_calls"),
    [
        (20, 20, "verified", 2),
        (19, 19, "moved forward 1 day", 3),
        (10, 10, "moved forward", 8),
        (21, 21, "moved back 1 day", 3),
        (30, 30, "moved back", 8),
    ],
    ids=["stable", "forward-1", "forward-many", "back-1", "back-many"],
)
async def test_retention_reverify_self_corrects(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    clock: Clock,
    hass_storage: dict,
    earliest_days_ago: int,
    expected: int,
    method_start: str,
    max_calls: int,
) -> None:
    """The daily check confirms the boundary or moves it, in either direction."""
    _preload_store(hass_storage, retention_days=20, retention=VERIFIED_YESTERDAY)
    fake = FakeAmber(TODAY - timedelta(days=earliest_days_ago), YESTERDAY)
    entry = await _setup_entry(hass, aioclient_mock, fake)

    result = await _run(hass)

    store = entry.runtime_data.manager.store
    assert store.retention_days == expected
    assert result["retention"]["method"].startswith(method_start)
    assert result["retention"]["calls"] <= max_calls <= 8
    assert store.retention["last_verified"] == TODAY.isoformat()
    probes = [c for c in fake.calls if c[0] == c[1]]
    assert len(probes) == result["retention"]["calls"]
    # The walk then starts at the (new) boundary.
    assert result["status"] == "caught_up"
    _assert_contiguous(await _rows(hass), TODAY - timedelta(days=expected), YESTERDAY)


@pytest.mark.parametrize("earliest", list(range(3, 40, 3)))
async def test_retention_reverify_call_cap(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    clock: Clock,
    hass_storage: dict,
    earliest: int,
) -> None:
    """Whatever the move, the daily check never makes more than 8 calls."""
    _preload_store(hass_storage, retention_days=20, retention=VERIFIED_YESTERDAY)
    fake = FakeAmber(TODAY - timedelta(days=earliest), YESTERDAY)
    entry = await _setup_entry(hass, aioclient_mock, fake)
    mgr = entry.runtime_data.manager
    mgr.ctx.client.start_run(manager_mod.RunBudget())
    result = await mgr._async_verify_retention()
    assert result["calls"] <= 8
    assert len(fake.calls) == result["calls"]


async def test_retention_move_beyond_bracket_triggers_discovery(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """A move too large for the 8-call check keeps the old value; full discovery follows."""
    _preload_store(hass_storage, retention_days=20, retention=VERIFIED_YESTERDAY)
    fake = FakeAmber(TODAY - timedelta(days=89), YESTERDAY)
    entry = await _setup_entry(hass, aioclient_mock, fake)
    mgr = entry.runtime_data.manager

    mgr.ctx.client.start_run(manager_mod.RunBudget())
    result = await mgr._async_verify_retention()
    assert result["method"].startswith("unresolved")
    assert mgr.store.retention_days == 20
    assert mgr.store.retention["needs_discovery"] is True

    clock.now += timedelta(days=1)
    fake.latest = TODAY
    summary = await _run(hass)
    assert summary["retention"] == "discovered"
    # The data still starts on the same day, which is now 90 days back.
    assert mgr.store.retention_days == 90


async def test_retention_checked_once_per_day(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """The 2-call check runs once per NEM day, not on every attempt."""
    _preload_store(hass_storage, retention_days=5, retention=VERIFIED_YESTERDAY)
    fake = FakeAmber(TODAY - timedelta(days=5), YESTERDAY)
    await _setup_entry(hass, aioclient_mock, fake)
    first = await _run(hass)
    calls = len(fake.calls)
    second = await _run(hass)
    assert first["retention"]["calls"] == 2
    assert "retention" not in second
    assert len(fake.calls) == calls


# --- long outage --------------------------------------------------------------------------


async def test_marker_behind_boundary_after_eight_month_outage(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """After ~8 months offline, days older than the boundary are skipped as unavailable,
    and the import continues from the boundary with the sums carried over."""
    _preload_store(hass_storage, retention_days=3)
    clock.now = NOW - timedelta(days=240)
    then = TODAY - timedelta(days=240)
    fake = FakeAmber(then - timedelta(days=3), YESTERDAY)
    entry = await _setup_entry(hass, aioclient_mock, fake)
    await _run(hass)
    mgr = entry.runtime_data.manager
    assert mgr.store.marker == then - timedelta(days=1)
    old_sum = (await _rows(hass))[E1][-1]["sum"]

    clock.now = NOW  # 8 months later; Amber now only keeps the last 3 days
    fake.earliest = TODAY - timedelta(days=3)
    result = await _run(hass)

    assert result["status"] == "caught_up"
    assert result["retention"]["method"] == "verified"
    skipped = result["skipped_unavailable"]
    assert skipped == {
        "from": then.isoformat(),
        "to": (TODAY - timedelta(days=4)).isoformat(),
        "days": 237,
    }
    assert mgr.store.day(then)["status"] == "skipped_unavailable"
    assert mgr.store.marker == YESTERDAY and mgr.store.last_written == YESTERDAY
    rows = await _rows(hass)
    assert len(rows[E1]) == 6 * 24  # 3 old days + 3 new days, nothing in between
    first_new = next(
        r
        for r in rows[E1]
        if datetime.fromtimestamp(r["start"], UTC)
        >= importer.nem_day_start(TODAY - timedelta(days=3))
    )
    assert first_new["sum"] == pytest.approx(old_sum + first_new["state"], abs=1e-6)
    _assert_sums_continuous(rows)
    # And the next run's Guard 1 accepts the resolved state.
    assert (await _run(hass))["status"] == "caught_up"
    assert _issue(hass, "marker_mismatch") is None


# --- patience ------------------------------------------------------------------------------


async def test_patience_write_off_after_distinct_days(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """An empty day with later data is skipped only after patience_days distinct days."""
    _preload_store(hass_storage, retention_days=6)
    start = TODAY - timedelta(days=6)
    gap = start + timedelta(days=2)
    fake = FakeAmber(start, YESTERDAY)
    fake.empty = {gap}
    entry = await _setup_entry(
        hass, aioclient_mock, fake, options={CONF_SCHEDULE_MODE: "automatic", CONF_PATIENCE_DAYS: 3}
    )
    mgr = entry.runtime_data.manager

    first = await _run(hass)
    again_same_day = await _run(hass)
    assert first["status"] == again_same_day["status"] == "waiting_for_data"
    assert again_same_day["empty_days_seen"] == 1  # the same calendar day counts once
    assert mgr.store.empty_seen(gap) == [TODAY.isoformat()]
    clock.now += timedelta(days=1)
    fake.latest += timedelta(days=1)
    assert (await _run(hass))["empty_days_seen"] == 2
    assert mgr.store.marker == gap - timedelta(days=1)

    clock.now += timedelta(days=1)
    fake.latest += timedelta(days=1)
    result = await _run(hass)

    assert result["status"] == "caught_up"
    assert result["skipped_gap"] == [gap.isoformat()]
    assert mgr.store.day(gap)["status"] == "skipped_gap"
    rows = await _rows(hass)
    hours = _hours(rows, E1)
    assert importer.nem_day_start(gap) not in hours  # the gap has no rows
    assert len(hours) == len(set(hours)) == 24 * ((fake.latest - start).days + 1 - 1)
    _assert_sums_continuous(rows)
    assert (await _run(hass))["status"] == "caught_up"  # Guard 1 accepts the skipped day


async def test_patience_keeps_waiting_without_later_data(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """A feed-wide outage (no later day has data) is never written off."""
    _preload_store(hass_storage, retention_days=6)
    start = TODAY - timedelta(days=6)
    fake = FakeAmber(start, start + timedelta(days=1))  # nothing after day 2, ever
    entry = await _setup_entry(
        hass, aioclient_mock, fake, options={CONF_SCHEDULE_MODE: "automatic", CONF_PATIENCE_DAYS: 2}
    )
    for _ in range(4):
        result = await _run(hass)
        assert result["status"] == "waiting_for_data"
        clock.now += timedelta(days=1)
    assert result["empty_days_seen"] >= 2
    assert entry.runtime_data.manager.store.marker == start + timedelta(days=1)
    assert "skipped_gap" not in result


async def test_patience_probes_later_days_beyond_the_window(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """If the empty day ends its fetch window, one probe checks the following days."""
    _preload_store(hass_storage, retention_days=10)
    start = TODAY - timedelta(days=10)
    gap = start + timedelta(days=6)  # last day of the first 7-day window
    fake = FakeAmber(start, YESTERDAY)
    fake.empty = {gap}
    entry = await _setup_entry(
        hass, aioclient_mock, fake, options={CONF_SCHEDULE_MODE: "automatic", CONF_PATIENCE_DAYS: 1}
    )

    result = await _run(hass)

    assert result["skipped_gap"] == [gap.isoformat()]
    assert (gap + timedelta(days=1), YESTERDAY) in fake.calls  # the probe of later days
    assert entry.runtime_data.manager.store.marker == YESTERDAY


# --- revisions -----------------------------------------------------------------------------


async def _import_with_estimated(hass, aioclient_mock, fake, estimated_day: date, **setup_kw):
    fake.overrides[estimated_day] = make_usage_day(estimated_day, quality="estimated")
    entry = await _setup_entry(hass, aioclient_mock, fake, **setup_kw)
    await _run(hass)
    store = entry.runtime_data.manager.store
    assert estimated_day.isoformat() in store.revisions
    return entry


async def test_revision_mid_history_rewrites_tail(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """A revised day in the middle of history rewrites the tail with continuous sums."""
    _preload_store(hass_storage, retention_days=10)
    start = TODAY - timedelta(days=10)
    revised = start + timedelta(days=3)
    fake = FakeAmber(start, YESTERDAY)
    entry = await _import_with_estimated(hass, aioclient_mock, fake, revised)
    store = entry.runtime_data.manager.store
    before = await _rows(hass)

    # Next day: Amber replaces the estimate with different, billable data.
    clock.now += timedelta(days=1)
    fake.latest += timedelta(days=1)
    fake.overrides[revised] = make_usage_day(revised, seed=99)
    result = await _run(hass)

    assert result["revisions"]["changed"] == [revised.isoformat()]
    assert result["revisions"]["rewritten_days"] == (YESTERDAY - revised).days + 1
    assert result["imported_days"] == [TODAY.isoformat()]
    rows = await _rows(hass)
    _assert_contiguous(rows, start, TODAY)
    # Days before the revised day are untouched; from it on, sums follow the new data.
    cut = next(
        i
        for i, r in enumerate(rows[E1])
        if datetime.fromtimestamp(r["start"], UTC) >= importer.nem_day_start(revised)
    )
    assert rows[E1][:cut] == before[E1][:cut]
    assert rows[E1][-1]["sum"] == pytest.approx(
        expected_totals(fake.records(start, TODAY))["E1"]["kwh"], abs=1e-6
    )
    assert store.day(revised)["mode"] == "revision"
    assert revised.isoformat() not in store.revisions  # now billable: final
    assert store.tail_rewrite is None


async def test_revision_without_change_writes_nothing(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Unchanged estimated data is re-fetched but nothing is written."""
    _preload_store(hass_storage, retention_days=6)
    start = TODAY - timedelta(days=6)
    revised = start + timedelta(days=2)
    fake = FakeAmber(start, YESTERDAY)
    entry = await _import_with_estimated(hass, aioclient_mock, fake, revised)
    before = await _rows(hass)

    clock.now += timedelta(days=1)
    with patch.object(
        importer, "async_add_external_statistics", wraps=importer.async_add_external_statistics
    ) as writes:
        result = await _run(hass)  # today's data is not published yet: nothing new either

    assert result["revisions"]["checked"] == [revised.isoformat()]
    assert result["revisions"]["changed"] == []
    assert writes.call_count == 0
    assert await _rows(hass) == before
    assert revised.isoformat() in entry.runtime_data.manager.store.revisions  # still estimated


async def test_revision_estimated_becomes_billable_unchanged(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Same numbers, now billable: dropped from the revision set without a write."""
    _preload_store(hass_storage, retention_days=6)
    start = TODAY - timedelta(days=6)
    revised = start + timedelta(days=2)
    fake = FakeAmber(start, YESTERDAY)
    entry = await _import_with_estimated(hass, aioclient_mock, fake, revised)
    clock.now += timedelta(days=1)
    fake.overrides[revised] = make_usage_day(revised)  # identical values, billable

    result = await _run(hass)

    assert result["revisions"]["changed"] == []
    assert revised.isoformat() not in entry.runtime_data.manager.store.revisions


async def test_revision_window_expires(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """After revision_days, a still-estimated day is no longer re-checked."""
    _preload_store(hass_storage, retention_days=6)
    start = TODAY - timedelta(days=6)
    revised = start + timedelta(days=2)
    fake = FakeAmber(start, YESTERDAY)
    entry = await _import_with_estimated(
        hass,
        aioclient_mock,
        fake,
        revised,
        options={CONF_SCHEDULE_MODE: "automatic", CONF_REVISION_DAYS: 2},
    )
    for _ in range(3):
        clock.now += timedelta(days=1)
        result = await _run(hass)
    assert result["revisions"]["expired"] == [revised.isoformat()]
    assert entry.runtime_data.manager.store.revisions == {}


async def test_interrupted_tail_rewrite_resumes(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """A tail rewrite that stops part-way resumes where it stopped on the next run."""
    _preload_store(hass_storage, retention_days=8)
    start = TODAY - timedelta(days=8)
    revised = start + timedelta(days=1)
    fake = FakeAmber(start, YESTERDAY)
    entry = await _import_with_estimated(hass, aioclient_mock, fake, revised)
    mgr = entry.runtime_data.manager
    clock.now += timedelta(days=1)
    fake.overrides[revised] = make_usage_day(revised, seed=5)
    real = mgr._async_write
    count = 0

    async def flaky(day, records, mode):
        nonlocal count
        if mode == "revision":
            count += 1
            if count == 3:
                raise OSError("simulated crash")
        return await real(day, records, mode)

    with patch.object(mgr, "_async_write", flaky):
        first = await _run(hass)
    assert first["reason"] == "internal_error"
    progress = mgr.store.tail_rewrite
    assert progress["next"] == (revised + timedelta(days=2)).isoformat()

    second = await _run(hass)
    assert second["tail_resumed"]["next"] == progress["next"]
    assert mgr.store.tail_rewrite is None
    rows = await _rows(hass)
    _assert_contiguous(rows, start, YESTERDAY)
    assert rows[E1][-1]["sum"] == pytest.approx(
        expected_totals(fake.records(start, YESTERDAY))["E1"]["kwh"], abs=1e-6
    )


# --- final-attempt Repairs -------------------------------------------------------------------


async def test_behind_issue_only_after_final_attempt(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """'Still behind' is raised by the day's final attempt and clears once caught up."""
    await hass.config.async_set_time_zone("Australia/Sydney")
    _preload_store(hass_storage, retention_days=4)
    fake = FakeAmber(TODAY - timedelta(days=4), YESTERDAY - timedelta(days=1))  # yesterday late
    entry = await _setup_entry(
        hass,
        aioclient_mock,
        fake,
        options={CONF_SCHEDULE_MODE: "fixed", CONF_FIXED_TIMES: ["07:15", "10:15", "13:15"]},
    )
    mgr = entry.runtime_data.manager
    first, second, final = (datetime(2026, 9, 25, h, 15, tzinfo=UTC) for h in (21, 0, 3))
    second, final = second + timedelta(days=1), final + timedelta(days=1)

    for when in (first, second):
        clock.now = when
        mgr._on_timer(when)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert _issue(hass, "behind") is None
    clock.now = final
    mgr._on_timer(final)
    await hass.async_block_till_done(wait_background_tasks=True)
    issue = _issue(hass, "behind")
    assert issue is not None
    assert issue.translation_placeholders["days_behind"] == "1"
    assert mgr.store.last_run["trigger"] == "scheduled (final)"

    fake.latest = YESTERDAY
    assert (await _run(hass))["status"] == "caught_up"
    assert _issue(hass, "behind") is None


async def test_incomplete_data_issue_only_on_final_attempt(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """An incomplete day raises nothing on early attempts, and an issue on the final one."""
    _preload_store(hass_storage, retention_days=3)
    fake = FakeAmber(TODAY - timedelta(days=3), YESTERDAY)
    fake.mutate = lambda day, recs: recs.pop(0) if day == YESTERDAY else None
    entry = await _setup_entry(hass, aioclient_mock, fake)
    mgr = entry.runtime_data.manager

    await mgr.async_run("scheduled")
    assert _issue(hass, "incomplete_data") is None
    await mgr.async_run("scheduled (final)", final=True)
    assert _issue(hass, "incomplete_data") is not None
    assert _issue(hass, "behind") is not None

    fake.mutate = None
    await _run(hass)
    assert _issue(hass, "incomplete_data") is None
    assert _issue(hass, "behind") is None


# --- reconfigure (new channel) -------------------------------------------------------------------


async def test_reconfigure_adds_new_channel(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """A new controlled load appears: import stops with an issue; reconfigure adds the
    channel from that day, clears the issue, and the walk continues with continuous sums."""
    _preload_store(hass_storage, retention_days=6)
    start = TODAY - timedelta(days=6)
    new_from = start + timedelta(days=3)
    fake = FakeAmber(start, YESTERDAY)
    for d in range(3, 6):
        day = start + timedelta(days=d)
        fake.overrides[day] = make_usage_day(day, E2_CHANNELS)
    entry = await _setup_entry(hass, aioclient_mock, fake)

    result = await _run(hass)
    assert result["reason"] == "unexpected_channel"
    assert _issue(hass, "unexpected_channel") is not None  # immediate, not final-only
    assert entry.runtime_data.manager.store.marker == new_from - timedelta(days=1)

    # The site now reports E2 as well.
    aioclient_mock.clear_requests()
    fake.install(aioclient_mock, [site_json(E2_CHANNELS)])
    flow = await entry.start_reconfigure_flow(hass)
    assert flow["step_id"] == "reconfigure"
    assert flow["description_placeholders"] == {"added": "E2 (controlled load)", "removed": "none"}
    flow = await hass.config_entries.flow.async_configure(flow["flow_id"], {})
    await hass.async_block_till_done()
    assert flow["type"] is FlowResultType.ABORT and flow["reason"] == "reconfigure_successful"
    assert _issue(hass, "unexpected_channel") is None
    assert [c["identifier"] for c in entry.data["channels"]] == ["E1", "B1", "E2"]
    store = entry.runtime_data.manager.store
    assert store.channel_since("E2") == new_from

    result = await _run(hass)
    assert result["status"] == "caught_up"
    e2 = f"{PREFIX}_e2_energy"
    rows = await _rows(hass, [*ALL_IDS, e2, f"{PREFIX}_e2_cost"])
    assert _hours(rows, e2)[0] == importer.nem_day_start(new_from)
    assert rows[e2][0]["sum"] == pytest.approx(rows[e2][0]["state"])  # starts from zero
    _assert_contiguous({k: v for k, v in rows.items() if k in ALL_IDS}, start, YESTERDAY)
    _assert_sums_continuous(rows)
    net = f"{PREFIX}_net_cost"
    all_recs = fake.records(start, YESTERDAY)
    cents = expected_totals(all_recs)
    assert rows[net][-1]["sum"] == pytest.approx(
        sum(c["cents"] for c in cents.values()) / 100, abs=1e-6
    )
    assert (await _run(hass))["status"] == "caught_up"  # Guard 1 accepts the new statistic


async def test_reconfigure_without_changes_aborts(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Nothing to change: the flow says so."""
    _preload_store(hass_storage, retention_days=2)
    fake = FakeAmber(TODAY - timedelta(days=5), YESTERDAY)
    entry = await _setup_entry(hass, aioclient_mock, fake)
    flow = await entry.start_reconfigure_flow(hass)
    assert flow["type"] is FlowResultType.ABORT and flow["reason"] == "channels_unchanged"


@pytest.mark.parametrize(
    ("sites", "reason"),
    [
        ([site_json(id="01OTHER0000000000000000000")], "site_not_found"),
        ([site_json([("E1", "general", None), ("X1", "battery", None)])], "unsupported_channels"),
    ],
)
async def test_reconfigure_refusals(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    clock: Clock,
    hass_storage: dict,
    sites: list,
    reason: str,
) -> None:
    """The site must still exist and all channel types must be supported."""
    _preload_store(hass_storage, retention_days=2)
    fake = FakeAmber(TODAY - timedelta(days=5), YESTERDAY)
    entry = await _setup_entry(hass, aioclient_mock, fake)
    aioclient_mock.clear_requests()
    fake.install(aioclient_mock, sites)
    flow = await entry.start_reconfigure_flow(hass)
    assert flow["type"] is FlowResultType.ABORT and flow["reason"] == reason


# --- Store migration (the in-place upgrade path) --------------------------------------------------


async def test_store_minor_version_1_is_migrated(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """An M3 Store (1.1, no last_written) upgrades cleanly and the walk carries on."""
    fake = FakeAmber(TODAY - timedelta(days=5), YESTERDAY)
    # Build M3-era state: statistics for 3 days and a 1.1 Store without last_written.
    _preload_store(hass_storage, retention_days=5)
    entry = await _setup_entry(hass, aioclient_mock, fake)
    await _run(hass)
    data = dict(hass_storage[_store_key()]["data"])
    marker = data["marker"]
    for key in (
        "last_written",
        "empty_seen",
        "revisions",
        "revisions_checked",
        "tail_rewrite",
        "channel_since",
    ):
        data.pop(key)
    hass_storage[_store_key()] = {
        "version": 1,
        "minor_version": 1,
        "key": _store_key(),
        "data": data,
    }
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    store = entry.runtime_data.manager.store
    assert store.last_written.isoformat() == marker
    assert hass_storage[_store_key()]["minor_version"] == 2
    result = await _run(hass)
    assert result["status"] == "caught_up"
    _assert_contiguous(await _rows(hass), TODAY - timedelta(days=5), YESTERDAY)


async def test_options_patience_and_revision_applied_without_reload(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Changing patience or revision days takes effect in place."""
    _preload_store(hass_storage, retention_days=2)
    fake = FakeAmber(TODAY - timedelta(days=5), YESTERDAY)
    entry = await _setup_entry(hass, aioclient_mock, fake)
    mgr = entry.runtime_data.manager
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {CONF_SCHEDULE_MODE: "automatic", CONF_PATIENCE_DAYS: 3, CONF_REVISION_DAYS: 0},
    )
    await hass.async_block_till_done()
    assert entry.runtime_data.manager is mgr
    assert (mgr.patience_days, mgr.revision_days) == (3, 0)
    assert mgr.store.schedule_seed == schedule_seed_for(ENTRY_ID)


# --- edge paths --------------------------------------------------------------------------


async def test_revision_before_new_channel_rewrites_without_it(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """A revised day from before a channel was added is rewritten without that channel."""
    _preload_store(hass_storage, retention_days=6)
    start = TODAY - timedelta(days=6)
    new_from = start + timedelta(days=3)
    revised = start + timedelta(days=1)
    fake = FakeAmber(start, YESTERDAY)
    fake.overrides[revised] = make_usage_day(revised, quality="estimated")
    for d in range(3, 6):
        day = start + timedelta(days=d)
        fake.overrides[day] = make_usage_day(day, E2_CHANNELS)
    entry = await _setup_entry(hass, aioclient_mock, fake)
    await _run(hass)  # stops at the new channel
    aioclient_mock.clear_requests()
    fake.install(aioclient_mock, [site_json(E2_CHANNELS)])
    flow = await entry.start_reconfigure_flow(hass)
    await hass.config_entries.flow.async_configure(flow["flow_id"], {})
    await hass.async_block_till_done()
    await _run(hass)

    clock.now += timedelta(days=1)
    fake.overrides[revised] = make_usage_day(revised, seed=3)
    result = await _run(hass)

    assert result["revisions"]["changed"] == [revised.isoformat()]
    e2 = f"{PREFIX}_e2_energy"
    rows = await _rows(hass, [*ALL_IDS, e2])
    assert _hours(rows, e2)[0] == importer.nem_day_start(new_from)
    _assert_sums_continuous(rows)
    assert rows[e2][0]["sum"] == pytest.approx(rows[e2][0]["state"])


async def test_two_estimated_days_in_one_window_and_unavailable_recheck(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Revision re-fetches share windows; a day that is unavailable today is kept for later."""
    _preload_store(hass_storage, retention_days=6)
    start = TODAY - timedelta(days=6)
    days = [start + timedelta(days=1), start + timedelta(days=3)]
    fake = FakeAmber(start, YESTERDAY)
    for d in days:
        fake.overrides[d] = make_usage_day(d, quality="estimated")
    entry = await _setup_entry(hass, aioclient_mock, fake)
    await _run(hass)
    calls = len(fake.calls)
    clock.now += timedelta(days=1)
    fake.empty = {days[1]}

    result = await _run(hass)

    assert result["revisions"]["checked"] == [d.isoformat() for d in days]
    assert result["revisions"]["changed"] == []
    # (single-day calls are the daily retention probes, which may land on the same day)
    revision_fetches = [c for c in fake.calls[calls:] if c[0] == days[0] and c[1] != c[0]]
    assert revision_fetches == [(days[0], YESTERDAY)]  # one window covers both days
    assert set(entry.runtime_data.manager.store.revisions) == {d.isoformat() for d in days}


async def test_tail_day_unavailable_stops_and_keeps_progress(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """If a later day in the tail cannot be fetched, the rewrite stops and resumes later."""
    _preload_store(hass_storage, retention_days=6)
    start = TODAY - timedelta(days=6)
    revised = start + timedelta(days=1)
    fake = FakeAmber(start, YESTERDAY)
    entry = await _import_with_estimated(hass, aioclient_mock, fake, revised)
    clock.now += timedelta(days=1)
    fake.overrides[revised] = make_usage_day(revised, seed=4)
    fake.empty = {revised + timedelta(days=2)}

    result = await _run(hass)

    assert result["reason"] == "tail_unavailable"
    progress = entry.runtime_data.manager.store.tail_rewrite
    assert progress["next"] == (revised + timedelta(days=2)).isoformat()
    fake.empty = set()
    assert (await _run(hass))["tail_resumed"] == progress
    _assert_sums_continuous(await _rows(hass))


async def test_reverify_gap_on_boundary_day(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Boundary day empty but the day before has data: a gap, not a boundary move."""
    _preload_store(hass_storage, retention_days=20, retention=VERIFIED_YESTERDAY)
    fake = FakeAmber(TODAY - timedelta(days=25), YESTERDAY)
    fake.empty = {TODAY - timedelta(days=20)}
    entry = await _setup_entry(hass, aioclient_mock, fake)
    mgr = entry.runtime_data.manager
    mgr.ctx.client.start_run(manager_mod.RunBudget())
    result = await mgr._async_verify_retention()
    assert result["method"].startswith("verified (boundary day empty")
    assert mgr.store.retention_days == 20


@pytest.mark.parametrize(("status", "outcome"), [(503, "caught_up"), (403, "auth_failed")])
async def test_reverify_api_errors(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    clock: Clock,
    hass_storage: dict,
    status: int,
    outcome: str,
) -> None:
    """A transient error leaves retention unchanged (rediscovered next day); 403 stops."""
    _preload_store(hass_storage, retention_days=20, retention=VERIFIED_YESTERDAY)
    fake = FakeAmber(TODAY - timedelta(days=20), YESTERDAY)
    entry = await _setup_entry(hass, aioclient_mock, fake)
    mgr = entry.runtime_data.manager
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"https://api.amber.com.au/v1/sites/{SITE_ID}/usage", status=status)
    mgr.ctx.client.start_run(manager_mod.RunBudget())
    if status == 403:
        with pytest.raises(manager_mod.AmberAuthError):
            await mgr._async_verify_retention()
        return
    result = await mgr._async_verify_retention()
    assert result["method"] == "unresolved (AmberServerError)"
    assert mgr.store.retention["needs_discovery"] is True
    assert outcome == "caught_up"


async def test_internal_guards(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Defensive paths: marker/last_written consistency, missing baselines, an
    unrecoverable pending day, and the probe cache."""
    _preload_store(hass_storage, retention_days=3)
    fake = FakeAmber(TODAY - timedelta(days=5), YESTERDAY)
    entry = await _setup_entry(hass, aioclient_mock, fake)
    mgr = entry.runtime_data.manager
    store = mgr.store

    assert mgr._marker_problem(None, None) is None
    assert "no marker" in mgr._marker_problem(None, YESTERDAY)
    assert mgr._marker_problem(YESTERDAY, None) is None
    await store.async_mark_imported(YESTERDAY, {"statistics": {}})
    store._data["last_written"] = None
    assert "recorded as imported" in mgr._marker_problem(YESTERDAY, None)

    with pytest.raises(importer.ImportRefusedError, match="no row at the end of"):
        store._data["last_written"] = (TODAY - timedelta(days=3)).isoformat()
        await mgr._async_baselines(TODAY - timedelta(days=2), mgr.ctx)

    with pytest.raises(importer.ImportDayError) as exc_info:
        await mgr._async_walk(TODAY - timedelta(days=9), [], {})
    assert exc_info.value.reason == "recovery_unavailable"

    probes: dict = {"2026-09-01": True}
    counter = [0]
    assert await mgr._prober(probes, counter)(date(2026, 9, 1)) is True
    assert counter == [0]


async def test_reconfigure_cannot_connect_and_unloaded_entry(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Reconfigure reports API errors, and works on an entry that is not loaded."""
    _preload_store(hass_storage, retention_days=2, marker=(TODAY - timedelta(days=2)).isoformat())
    fake = FakeAmber(TODAY - timedelta(days=5), YESTERDAY)
    aioclient_mock.get("https://api.amber.com.au/v1/sites", status=503)
    entry = await _setup_entry_raw(hass)
    assert entry.state.name == "SETUP_RETRY"

    flow = await entry.start_reconfigure_flow(hass)
    assert flow["type"] is FlowResultType.ABORT and flow["reason"] == "cannot_connect"

    aioclient_mock.clear_requests()
    fake.install(aioclient_mock, [site_json(E2_CHANNELS)])
    flow = await entry.start_reconfigure_flow(hass)
    flow = await hass.config_entries.flow.async_configure(flow["flow_id"], {})
    await hass.async_block_till_done()
    assert flow["reason"] == "reconfigure_successful"
    assert hass_storage[_store_key()]["data"]["channel_since"] == {
        "E2": (TODAY - timedelta(days=1)).isoformat()
    }


async def _setup_entry_raw(hass: HomeAssistant):
    from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: PLC0415

    from custom_components.amber_energy_dashboard.const import (  # noqa: PLC0415
        CONF_CHANNELS,
        CONF_NMI,
        CONF_SITE_ID,
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id=ENTRY_ID,
        unique_id=SITE_ID,
        title="Amber FAKENMI000",
        data={
            "api_key": "psk_test",
            CONF_SITE_ID: SITE_ID,
            CONF_NMI: "FAKENMI000",
            CONF_CHANNELS: [
                {"identifier": "E1", "type": "general", "tariff": "EA116"},
                {"identifier": "B1", "type": "feedIn", "tariff": None},
            ],
        },
        options={CONF_SCHEDULE_MODE: "automatic"},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_reimport_planner_refusals(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """The re-import planner refuses a day that is not the latest stored day."""
    _preload_store(hass_storage, retention_days=2)
    fake = FakeAmber(TODAY - timedelta(days=5), YESTERDAY)
    entry = await _setup_entry(hass, aioclient_mock, fake)
    ctx = entry.runtime_data.context
    with pytest.raises(importer.ImportRefusedError, match="nothing to re-import"):
        await importer.async_plan_day(hass, ctx, YESTERDAY)
    await _run(hass)
    with pytest.raises(importer.ImportRefusedError, match="cannot be imported"):
        await importer.async_plan_day(hass, ctx, YESTERDAY - timedelta(days=1))
    mode, baselines = await importer.async_plan_day(hass, ctx, YESTERDAY)
    assert mode == "reimport" and set(baselines) == set(ALL_IDS)
