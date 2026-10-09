"""2.1.0-rc2: retention self-heal and the site's start date (DESIGN section 8).

A forum report: a customer who joined Amber recently was stuck on "Waiting for Amber data"
for a pre-join date at the 89-day boundary. The first-setup discovery had fallen back to
today - 89, and the walk then waited with patience on each empty pre-join day.
"""

from datetime import timedelta
import logging
from unittest.mock import patch

from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.amber_energy_dashboard import manager as manager_mod

from .fake_amber import FakeAmber
from .synthetic import site_json
from .test_manager import (  # noqa: F401 - fixtures are used by name
    NOW,
    TODAY,
    YESTERDAY,
    Clock,
    _fast_verify,
    _no_real_timers,
    _preload_store,
    _run,
    _setup,
    _setup_entry,
    clock,
)


def _days(result: dict) -> int:
    return len(result["imported_days"])


async def test_new_customer_imports_from_the_start_date(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock
) -> None:
    """A customer who started 30 days ago: discovery uses the start date, and the first
    run imports all 30 days at once, with few calls."""
    start = TODAY - timedelta(days=30)
    fake = FakeAmber(start, YESTERDAY)
    entry = await _setup_entry(
        hass, aioclient_mock, fake, site=site_json(activeFrom=start.isoformat())
    )
    result = await _run(hass)
    assert result["status"] == "caught_up"
    assert _days(result) == 30
    store = entry.runtime_data.manager.store
    assert store.retention_boundary == start
    assert store.retention["provisional"] is False
    assert store.site == {"active_from": start.isoformat(), "closed_on": None}
    assert result["calls"]["sites_usage"] <= 7  # 1 probe + 5 windows (+1 spare)


async def test_start_day_without_usage_skips_ahead_at_once(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    clock: Clock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The start date has no usage (data begins 3 days later): discovery takes the start
    date, and the walk skips the 3 leading empty days at once (no patience), moves the
    boundary to the first day with data, and records the self-heal."""
    start = TODAY - timedelta(days=30)
    fake = FakeAmber(start + timedelta(days=3), YESTERDAY)
    entry = await _setup_entry(
        hass, aioclient_mock, fake, site=site_json(activeFrom=start.isoformat())
    )
    with caplog.at_level(logging.WARNING):
        result = await _run(hass)
    assert result["status"] == "caught_up"
    assert _days(result) == 27
    assert result["skipped_leading"]["days"] == 3
    store = entry.runtime_data.manager.store
    assert store.retention_boundary == start + timedelta(days=3)
    heal = store.retention["self_heal"]
    assert (heal["skipped_from"], heal["skipped_to"]) == (
        start.isoformat(),
        (start + timedelta(days=2)).isoformat(),
    )
    assert heal["previous_method"] == "site start date (no usage on it)"
    assert store.day(start)["status"] == "skipped_unavailable"
    assert "Retention self-heal" in caplog.text


async def test_stuck_install_recovers_without_user_action(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """The forum case, as stored by 2.1.0-rc1: a fallback boundary 89 days back, nothing
    imported, and the customer's data starting 30 days ago (no start date from the API).
    The next run skips the empty run and imports everything."""
    _preload_store(
        hass_storage,
        retention_boundary=(TODAY - timedelta(days=89)).isoformat(),
        retention={
            "last_verified": TODAY.isoformat(),
            "measured_on": (TODAY - timedelta(days=3)).isoformat(),
            "method": "fallback (not bracketed)",
            "boundary": (TODAY - timedelta(days=89)).isoformat(),
        },
        empty_seen={(TODAY - timedelta(days=89)).isoformat(): ["x", "y", "z"]},
    )
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    entry = await _setup_entry(hass, aioclient_mock, fake, site=site_json(activeFrom=None))
    result = await _run(hass)
    assert result["status"] == "caught_up"
    assert _days(result) == 30
    store = entry.runtime_data.manager.store
    assert store.retention_boundary == TODAY - timedelta(days=30)
    assert store.retention["self_heal"]["days"] == 59
    assert store.site == {"active_from": None, "closed_on": None}


async def test_old_boundary_before_the_start_date_is_bounded(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """A stored boundary older than the site's start date: the walk starts at the start
    date (nothing before it is fetched)."""
    start = TODAY - timedelta(days=20)
    _preload_store(
        hass_storage,
        retention_boundary=(TODAY - timedelta(days=89)).isoformat(),
        retention={"last_verified": TODAY.isoformat(), "method": "fallback (not bracketed)"},
    )
    fake = FakeAmber(start, YESTERDAY)
    await _setup_entry(hass, aioclient_mock, fake, site=site_json(activeFrom=start.isoformat()))
    result = await _run(hass)
    assert _days(result) == 20
    assert min(s for s, _e in fake.calls) == start


async def test_fallback_is_provisional_and_rediscovered_once_a_day(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock
) -> None:
    """Discovery hits its call cap: the fallback (today - 89, here later than the real
    boundary) is provisional. Another run the same day does not rediscover; the next
    day's run does, and the real boundary replaces it."""
    fake = FakeAmber(TODAY - timedelta(days=100), YESTERDAY)
    entry = await _setup_entry(hass, aioclient_mock, fake, site=site_json(activeFrom=None))
    with patch.object(manager_mod, "RETENTION_DISCOVERY_MAX_CALLS", 1):
        first = await _run(hass)
    store = entry.runtime_data.manager.store
    assert first["retention"] == "discovered"
    assert store.retention["method"] == "fallback (_DiscoveryIncomplete)"
    assert store.retention["provisional"] is True
    assert store.retention["needs_discovery"] is True
    assert store.retention_boundary == TODAY - timedelta(days=89)

    clock.now = NOW + timedelta(hours=3)
    assert "retention" not in await _run(hass)  # once a day

    clock.now = NOW + timedelta(days=1)
    result = await _run(hass)
    assert result["retention"] == "discovered"
    assert store.retention["provisional"] is False
    assert store.retention["method"] == "bisection"
    assert store.retention_boundary == TODAY - timedelta(days=100)


async def test_failed_discovery_on_a_young_site(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock
) -> None:
    """Discovery fails on a site younger than 89 days: the start date is used, but it is
    still provisional. A failure that stops the run (the rate-limit budget) stores the
    fallback raised to the start date, and says so."""
    start = TODAY - timedelta(days=40)
    fake = FakeAmber(start, YESTERDAY)
    entry = await _setup_entry(
        hass, aioclient_mock, fake, site=site_json(activeFrom=start.isoformat())
    )
    manager = entry.runtime_data.manager
    with patch.object(manager, "_search_boundary", side_effect=manager_mod._DiscoveryIncomplete):
        await manager._async_discover_retention()
    info = manager.store.retention
    assert manager.store.retention_boundary == start
    assert info["method"] == "fallback (_DiscoveryIncomplete), from the site start date"
    assert info["provisional"] is True
    with (
        patch.object(
            manager,
            "_search_boundary",
            side_effect=manager_mod.AmberBudgetExhaustedError("x", "sites_usage"),
        ),
        pytest.raises(manager_mod.AmberBudgetExhaustedError),
    ):
        await manager._async_discover_retention()
    assert manager.store.retention_boundary == start
    assert manager.store.retention["site_start_applied"] == start.isoformat()
    assert manager.store.retention["provisional"] is True


async def test_interior_gap_keeps_patience(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Once something is imported, an empty day with data after it is an interior gap:
    it still waits with patience (not skipped at once)."""
    _preload_store(hass_storage, retention_days=10)
    gap = TODAY - timedelta(days=5)
    fake = FakeAmber(TODAY - timedelta(days=60), YESTERDAY)
    fake.empty = {gap}
    entry = await _setup_entry(hass, aioclient_mock, fake)
    result = await _run(hass)
    assert result["status"] == "waiting_for_data"
    assert result["waiting_for"] == gap.isoformat()
    assert result["empty_days_seen"] == 1
    assert "skipped_leading" not in result
    assert entry.runtime_data.manager.store.marker == gap - timedelta(days=1)


async def test_no_usage_at_all_yet(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """A brand-new site with nothing published yet: it waits from the first day, without
    skipping anything."""
    start = TODAY - timedelta(days=3)
    fake = FakeAmber(TODAY + timedelta(days=5), TODAY + timedelta(days=5))
    entry = await _setup_entry(
        hass, aioclient_mock, fake, site=site_json(activeFrom=start.isoformat())
    )
    result = await _run(hass)
    assert result["status"] == "waiting_for_data"
    assert result["waiting_for"] == start.isoformat()
    assert entry.runtime_data.manager.store.marker is None
