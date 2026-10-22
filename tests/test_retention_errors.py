"""2.1.1: a failed retention check never moves the stored boundary (DESIGN section 8).

Seen on a lab VM with intermittent DNS failures: a daily check that failed with a
connection error asked for rediscovery, every rediscovery failed too, and each one stored
the fallback (today - 89) in place of the verified boundary, moving it forward a day at a
time. Days older than the stored boundary are skipped without being fetched (the walk, a
new own-sensor mapping or price series, a backfill), so a boundary that is too recent can
lose data that Amber still has.

Recovery-only mode keeps these runs to the retention check (no walk).
"""

from datetime import timedelta
from unittest.mock import patch

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.amber_energy_dashboard import manager as manager_mod
from custom_components.amber_energy_dashboard.const import (
    CONF_SCHEDULE_MODE,
    CONF_USAGE_MODE,
    DOMAIN,
    MODE_RECOVERY,
)

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

REAL = TODAY - timedelta(days=100)  # older than the fallback, today - 89
RECOVERY = {CONF_SCHEDULE_MODE: "automatic", CONF_USAGE_MODE: MODE_RECOVERY}
VERIFIED = {"last_verified": (TODAY - timedelta(days=1)).isoformat(), "method": "verified"}
OUTAGE = aiohttp.ClientConnectionError("Cannot connect to host (DNS)")


async def test_outage_never_moves_a_verified_boundary(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Day 1: the daily check fails (rediscovery requested). Day 2: rediscovery fails.
    The verified boundary stays; day 3, with the network back, rediscovery confirms it."""
    _preload_store(hass_storage, retention_days=100, retention=VERIFIED)
    fake = FakeAmber(REAL, YESTERDAY)
    entry = await _setup_entry(
        hass, aioclient_mock, fake, options=RECOVERY, site=site_json(activeFrom=None)
    )
    store = entry.runtime_data.manager.store
    assert store.retention_boundary == REAL
    fake.error = OUTAGE

    await _run(hass)
    assert store.retention_boundary == REAL
    assert store.retention["method"] == "unresolved (AmberConnectionError)"
    assert store.retention["needs_discovery"] is True

    for days in (1, 2, 3):  # the outage goes on: every rediscovery fails
        clock.now = NOW + timedelta(days=days)
        result = await _run(hass)
        assert result["retention"] == "discovered"
        assert store.retention_boundary == REAL
        info = store.retention
        assert info["method"] == "unresolved (AmberConnectionError)"
        assert info["needs_discovery"] is True
        assert info["provisional"] is False  # still the verified boundary
        assert info["last_verified"] == VERIFIED["last_verified"]
        assert info["previous_boundary"] == REAL.isoformat()

    fake.error = None
    clock.now = NOW + timedelta(days=4)
    result = await _run(hass)
    assert result["retention"] == "discovered"
    assert store.retention_boundary == REAL
    assert store.retention["method"] == "bisection"
    assert store.retention["needs_discovery"] is False


async def test_outage_does_not_creep_a_provisional_boundary(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock
) -> None:
    """First setup during an outage: the fallback (today - 89) is the only option and is
    provisional. Later failed rediscoveries keep that date rather than today - 89 of each
    new day; once the network is back, the real (older) boundary replaces it."""
    fake = FakeAmber(REAL, YESTERDAY)
    entry = await _setup_entry(
        hass, aioclient_mock, fake, options=RECOVERY, site=site_json(activeFrom=None)
    )
    store = entry.runtime_data.manager.store
    fake.error = OUTAGE
    await _run(hass)
    first = TODAY - timedelta(days=89)
    assert store.retention_boundary == first
    assert store.retention["method"] == "fallback (AmberConnectionError)"
    assert store.retention["provisional"] is True

    for days in (1, 2):
        clock.now = NOW + timedelta(days=days)
        assert (await _run(hass))["retention"] == "discovered"
        assert store.retention_boundary == first
        assert store.retention["method"] == "unresolved (AmberConnectionError)"
        assert store.retention["provisional"] is True
        clock.now += timedelta(hours=3)
        assert "retention" not in await _run(hass)  # a provisional one: once a day

    fake.error = None
    clock.now = NOW + timedelta(days=3)
    await _run(hass)
    assert store.retention_boundary == REAL
    assert store.retention["provisional"] is False


@pytest.mark.parametrize(
    "error",
    [
        manager_mod.AmberBudgetExhaustedError("x", "sites_usage"),
        manager_mod.AmberAuthError("rejected"),
        manager_mod._DiscoveryIncomplete(),
    ],
    ids=["budget", "auth", "call_cap"],
)
async def test_failed_rediscovery_keeps_the_boundary(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    clock: Clock,
    hass_storage: dict,
    error: Exception,
) -> None:
    """Every failure kind keeps a stored boundary. Budget and auth failures still stop
    the run (the run outcome and reauthentication handle them)."""
    _preload_store(hass_storage, retention_days=100, retention=VERIFIED)
    fake = FakeAmber(REAL, YESTERDAY)
    entry = await _setup_entry(
        hass, aioclient_mock, fake, options=RECOVERY, site=site_json(activeFrom=None)
    )
    manager = entry.runtime_data.manager
    with patch.object(manager, "_search_boundary", side_effect=error):
        if isinstance(error, manager_mod._DiscoveryIncomplete):
            await manager._async_discover_retention()
        else:
            with pytest.raises(type(error)):
                await manager._async_discover_retention()
    assert manager.store.retention_boundary == REAL
    assert manager.store.retention["method"] == f"unresolved ({type(error).__name__})"
    assert manager.store.retention["needs_discovery"] is True


async def test_probe_retention_during_an_outage_reports_and_keeps(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """The probe_retention action fails with an error and leaves the record as it was."""
    _preload_store(hass_storage, retention_days=100, retention=VERIFIED)
    fake = FakeAmber(REAL, YESTERDAY)
    entry = await _setup_entry(
        hass, aioclient_mock, fake, options=RECOVERY, site=site_json(activeFrom=None)
    )
    store = entry.runtime_data.manager.store
    before = dict(store.retention)
    fake.error = OUTAGE
    with pytest.raises(HomeAssistantError, match="AmberConnectionError"):
        await hass.services.async_call(
            DOMAIN, "probe_retention", {}, blocking=True, return_response=True
        )
    assert store.retention_boundary == REAL
    assert store.retention == before
