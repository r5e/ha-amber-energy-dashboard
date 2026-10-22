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


# --- entry titles (2.1.1) -------------------------------------------------------------------

SITE_2 = "01FAKESITE0000000000000002"


def _site_entry(hass: HomeAssistant, site_id: str, nmi: str, title: str) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=site_id,
        title=title,
        data={
            CONF_API_KEY: "psk_test",
            CONF_SITE_ID: site_id,
            CONF_NMI: nmi,
            CONF_CHANNELS: [{"identifier": "E1", "type": "general", "tariff": None}],
        },
    )
    entry.add_to_hass(hass)
    return entry


async def test_title_migrated_to_one_without_the_nmi(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Until 2.1.0 the default title was "Amber <NMI>"; it becomes "Amber Energy Dashboard"."""
    aioclient_mock.get(SITES_URL, json=[site_json()])
    entry = _entry(hass)
    await _setup_entry(hass, entry)
    assert entry.title == "Amber Energy Dashboard"


async def test_title_chosen_by_the_user_is_kept(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A renamed entry keeps its name."""
    aioclient_mock.get(SITES_URL, json=[site_json()])
    entry = _site_entry(hass, SITE_ID, "FAKENMI000", "Our house")
    await _setup_entry(hass, entry)
    assert entry.title == "Our house"


async def test_titles_with_several_sites(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """With a second site, both default titles show the NMI's last 4 digits; after one
    is removed, the other goes back to "Amber Energy Dashboard". A renamed one is never changed."""
    aioclient_mock.get(
        SITES_URL,
        json=[site_json(nmi="6000000123"), site_json(id=SITE_2, nmi="6000000456")],
    )
    first = _site_entry(hass, SITE_ID, "6000000123", "Amber 6000000123")
    await _setup_entry(hass, first)
    assert first.title == "Amber Energy Dashboard"
    second = _site_entry(hass, SITE_2, "6000000456", "Amber 6000000456")
    await _setup_entry(hass, second)
    assert (first.title, second.title) == (
        "Amber Energy Dashboard (…0123)",
        "Amber Energy Dashboard (…0456)",
    )

    await hass.config_entries.async_remove(second.entry_id)
    await hass.async_block_till_done()
    assert first.title == "Amber Energy Dashboard"

    third = _site_entry(hass, SITE_2, "6000000456", "Holiday house")
    await _setup_entry(hass, third)
    assert (first.title, third.title) == ("Amber Energy Dashboard (…0123)", "Holiday house")


async def test_title_update_leaves_the_manager_alone(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A title change (another site's setup, or a rename) does not reschedule the manager;
    an options change still applies."""
    aioclient_mock.get(SITES_URL, json=[site_json()])
    entry = _entry(hass)
    await _setup_entry(hass, entry)
    manager = entry.runtime_data.manager
    with patch.object(manager, "async_update_settings") as update:
        hass.config_entries.async_update_entry(entry, title="Renamed")
        await hass.async_block_till_done()
        update.assert_not_called()
        hass.config_entries.async_update_entry(entry, options={"patience_days": 9})
        await hass.async_block_till_done()
        update.assert_called_once()
