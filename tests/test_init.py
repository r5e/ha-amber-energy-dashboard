"""Tests for integration setup, including key validation at entry setup."""

from unittest.mock import PropertyMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.amber_energy_dashboard.const import (
    CONF_CHANNELS,
    CONF_NMI,
    CONF_SITE_ID,
    DOMAIN,
)

from .synthetic import SITE_ID, site_json

SITES_URL = "https://api.amber.com.au/v1/sites"


@pytest.fixture(autouse=True)
def _no_startup_run():
    """Setup tests only; the first-setup catch-up is tested in test_manager.py."""
    with patch(
        "custom_components.amber_energy_dashboard.manager.AmberManager.needs_startup_run",
        new_callable=PropertyMock,
        return_value=False,
    ):
        yield


@pytest.fixture(autouse=True)
def _setup(recorder_mock, enable_custom_integrations: None) -> None:
    """Recorder first (it must precede hass), then allow custom integrations."""


async def test_setup_component(hass: HomeAssistant) -> None:
    """The integration loads (with its recorder dependency) and sets up cleanly."""
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    assert DOMAIN in hass.config.components


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=SITE_ID,
        title="Amber FAKENMI000",
        data={
            CONF_API_KEY: "psk_test",
            CONF_SITE_ID: SITE_ID,
            CONF_NMI: "FAKENMI000",
            CONF_CHANNELS: [{"identifier": "E1", "type": "general", "tariff": None}],
        },
    )
    entry.add_to_hass(hass)
    return entry


async def _setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_setup_validates_key(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Setup makes exactly one GET /sites with the entry's key, then loads."""
    aioclient_mock.get(SITES_URL, json=[site_json()])
    entry = _entry(hass)

    await _setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert aioclient_mock.call_count == 1
    assert aioclient_mock.mock_calls[0][3]["Authorization"] == "Bearer psk_test"
    assert entry.runtime_data.context.client.request_count == 1


def _reauth_sources(hass: HomeAssistant) -> list[str]:
    return [f["context"]["source"] for f in hass.config_entries.flow.async_progress()]


@pytest.mark.parametrize("status", [401, 403])
async def test_setup_auth_failure_starts_reauth(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, status: int
) -> None:
    """A rejected key raises ConfigEntryAuthFailed, which starts reauth."""
    aioclient_mock.get(SITES_URL, status=status, text='{"message":"denied"}')
    entry = _entry(hass)

    await _setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert _reauth_sources(hass) == ["reauth"]


async def test_setup_site_no_longer_visible_starts_reauth(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A key that no longer sees this site is treated as an auth failure."""
    aioclient_mock.get(SITES_URL, json=[site_json(id="01OTHERSITE000000000000000")])
    entry = _entry(hass)

    await _setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert _reauth_sources(hass) == ["reauth"]


@pytest.mark.parametrize(
    "mock",
    [
        {"exc": TimeoutError()},
        {"status": 500},
        {"status": 503},
        {"status": 429, "headers": {"Retry-After": "60"}},
        {"text": "<html>maintenance</html>"},
    ],
    ids=["timeout", "500", "503", "429", "malformed"],
)
async def test_setup_transient_failure_retries(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, mock: dict
) -> None:
    """Network errors, 5xx, 429 and odd responses raise ConfigEntryNotReady (retry)."""
    aioclient_mock.get(SITES_URL, **mock)
    entry = _entry(hass)

    await _setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert _reauth_sources(hass) == []
