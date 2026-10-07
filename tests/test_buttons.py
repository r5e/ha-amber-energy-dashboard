"""Milestone 9: the device buttons (DESIGN section 18)."""

from datetime import timedelta

from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.amber_energy_dashboard.const import DOMAIN

from .fake_amber import FakeAmber
from .synthetic import SITE_ID
from .test_manager import (  # noqa: F401 - fixtures are used by name
    TODAY,
    YESTERDAY,
    Clock,
    _fast_verify,
    _no_real_timers,
    _preload_store,
    _setup,
    _setup_entry,
    clock,
)

RUN_NOW = "button.amber_energy_dashboard_run_now"
RE_CHECK = "button.amber_energy_dashboard_re_check_retention"
VERIFIED_YESTERDAY = {"last_verified": (TODAY - timedelta(days=1)).isoformat(), "method": "preset"}


async def _press(hass: HomeAssistant, entity_id: str) -> None:
    await hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)


async def test_buttons_on_the_device(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock
) -> None:
    """Both buttons sit on the site's device, with names, icons and categories."""
    entry = await _setup_entry(
        hass, aioclient_mock, FakeAmber(TODAY - timedelta(days=89), YESTERDAY)
    )
    registry = er.async_get(hass)
    run_now = registry.async_get(RUN_NOW)
    re_check = registry.async_get(RE_CHECK)
    assert run_now is not None and re_check is not None
    assert run_now.unique_id == f"{SITE_ID}_run_now"
    assert run_now.entity_category is None  # under Controls
    assert re_check.entity_category is EntityCategory.DIAGNOSTIC
    assert run_now.translation_key == "run_now"
    assert re_check.translation_key == "probe_retention"
    [device] = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert device.identifiers == {(DOMAIN, SITE_ID)}
    assert run_now.device_id == re_check.device_id == device.id
    assert run_now.config_entry_id == entry.entry_id
    assert hass.states.get(RUN_NOW).attributes["friendly_name"] == "Amber Energy Dashboard Run now"
    assert (
        hass.states.get(RE_CHECK).attributes["friendly_name"]
        == "Amber Energy Dashboard Re-check retention"
    )


async def test_run_now_button_runs_a_catch_up(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Pressing Run now is the run_now service: it catches up, with trigger "button"."""
    _preload_store(hass_storage, retention_days=4)
    entry = await _setup_entry(
        hass, aioclient_mock, FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    )
    manager = entry.runtime_data.manager
    assert manager.store.marker is None

    await _press(hass, RUN_NOW)

    last = manager.store.last_run
    assert last["trigger"] == "button"
    assert last["status"] == "caught_up"
    assert manager.store.marker == YESTERDAY
    assert len(last["imported_days"]) == 4


async def test_re_check_retention_button(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Pressing Re-check retention is probe_retention: a fresh discovery."""
    _preload_store(hass_storage, retention_days=20, retention=VERIFIED_YESTERDAY)
    entry = await _setup_entry(
        hass, aioclient_mock, FakeAmber(TODAY - timedelta(days=89), YESTERDAY)
    )
    store = entry.runtime_data.manager.store

    await _press(hass, RE_CHECK)

    assert store.retention_boundary == TODAY - timedelta(days=89)
    assert store.retention["previous_boundary"] == (TODAY - timedelta(days=20)).isoformat()


@pytest.mark.parametrize(("status", "match"), [(503, "stored boundary was kept"), (403, "re-auth")])
async def test_re_check_retention_button_failure(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    clock: Clock,
    hass_storage: dict,
    status: int,
    match: str,
) -> None:
    """A failed probe shows an error and keeps the stored boundary, as the service does."""
    _preload_store(hass_storage, retention_days=20, retention=VERIFIED_YESTERDAY)
    entry = await _setup_entry(
        hass, aioclient_mock, FakeAmber(TODAY - timedelta(days=89), YESTERDAY)
    )
    store = entry.runtime_data.manager.store
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"https://api.amber.com.au/v1/sites/{SITE_ID}/usage", status=status)

    with pytest.raises(HomeAssistantError, match=match):
        await _press(hass, RE_CHECK)

    assert store.retention_boundary == TODAY - timedelta(days=20)
