"""Config flow, options flow and reauth tests."""

from unittest.mock import PropertyMock, patch

from homeassistant import config_entries
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.amber_energy_dashboard.const import (
    CONF_CHANNELS,
    CONF_FIXED_TIMES,
    CONF_NMI,
    CONF_SCHEDULE_MODE,
    CONF_SITE_ID,
    DOMAIN,
)

from .synthetic import SITE_ID, site_json

SITES_URL = "https://api.amber.com.au/v1/sites"
KEY = "psk_test_key"


@pytest.fixture(autouse=True)
def _no_startup_run():
    """Entries created here would start a first-setup catch-up; that is tested elsewhere."""
    with patch(
        "custom_components.amber_energy_dashboard.manager.AmberManager.needs_startup_run",
        new_callable=PropertyMock,
        return_value=False,
    ):
        yield


@pytest.fixture(autouse=True)
def _setup(recorder_mock, enable_custom_integrations: None) -> None:
    """Recorder first (it must precede hass), then allow custom integrations.

    Entries created by the flow are set up for real, and the integration depends on
    the recorder.
    """


async def _start(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def test_full_flow(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Key, then site (shown with NMI), then channel confirmation creates the entry."""
    aioclient_mock.get(SITES_URL, json=[site_json()])

    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: f"  {KEY} "}
    )
    assert result["step_id"] == "site"
    options = result["data_schema"].schema[CONF_SITE_ID].config["options"]
    assert options == [{"value": SITE_ID, "label": "NMI FAKENMI000 (Example Network)"}]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SITE_ID: SITE_ID}
    )
    assert result["step_id"] == "channels"
    assert result["description_placeholders"] == {
        "nmi": "FAKENMI000",
        "channels": "- E1: general (tariff EA116)\n- B1: feed-in",
    }

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "schedule"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SCHEDULE_MODE: "automatic"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"] == {CONF_SCHEDULE_MODE: "automatic"}
    assert result["title"] == "Amber FAKENMI000"
    assert result["result"].unique_id == SITE_ID
    assert result["data"] == {
        CONF_API_KEY: KEY,
        CONF_SITE_ID: SITE_ID,
        CONF_NMI: "FAKENMI000",
        CONF_CHANNELS: [
            {"identifier": "E1", "type": "general", "tariff": "EA116"},
            {"identifier": "B1", "type": "feedIn", "tariff": None},
        ],
    }
    assert aioclient_mock.mock_calls[0][3]["Authorization"] == f"Bearer {KEY}"


@pytest.mark.parametrize("status", [401, 403])
async def test_bad_key(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, status: int
) -> None:
    """401 and 403 both show invalid_auth, and the user can retry."""
    aioclient_mock.get(SITES_URL, status=status, text='{"message":"denied"}')

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: "bad"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}

    aioclient_mock.clear_requests()
    aioclient_mock.get(SITES_URL, json=[site_json()])
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_API_KEY: KEY})
    assert result["step_id"] == "site"


@pytest.mark.parametrize(
    ("mock", "error"),
    [
        ({"status": 500}, "cannot_connect"),
        ({"status": 429}, "cannot_connect"),
        ({"exc": TimeoutError()}, "cannot_connect"),
        ({"text": "<html>"}, "unknown"),
        ({"status": 422, "text": "nope"}, "unknown"),
    ],
)
async def test_other_errors(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, mock: dict, error: str
) -> None:
    """Transport and unexpected-response failures map to form errors."""
    aioclient_mock.get(SITES_URL, **mock)

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_API_KEY: KEY})
    assert result["errors"] == {"base": error}


@pytest.mark.parametrize("sites", [[], [site_json(status="closed")], [site_json(status="pending")]])
async def test_no_active_sites(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, sites: list
) -> None:
    """A valid key with no active site aborts."""
    aioclient_mock.get(SITES_URL, json=sites)

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_API_KEY: KEY})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_active_sites"


async def test_only_active_sites_offered(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Closed sites are not offered; several active sites are all listed."""
    other = site_json(id="01FAKESITE0000000000000001", nmi="FAKENMI001", network=None)
    closed = site_json(id="01FAKESITE0000000000000002", nmi="FAKENMI002", status="closed")
    aioclient_mock.get(SITES_URL, json=[site_json(), other, closed])

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_API_KEY: KEY})
    options = result["data_schema"].schema[CONF_SITE_ID].config["options"]
    assert [o["label"] for o in options] == ["NMI FAKENMI000 (Example Network)", "NMI FAKENMI001"]


async def test_already_configured(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """The site ID is the unique ID: a second entry for it aborts."""
    MockConfigEntry(domain=DOMAIN, unique_id=SITE_ID, data={}).add_to_hass(hass)
    aioclient_mock.get(SITES_URL, json=[site_json()])

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_API_KEY: KEY})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SITE_ID: SITE_ID}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_unsupported_channel(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A channel type we cannot model aborts rather than importing partially."""
    aioclient_mock.get(
        SITES_URL, json=[site_json([("E1", "general", None), ("X1", "battery", None)])]
    )

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_API_KEY: KEY})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SITE_ID: SITE_ID}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unsupported_channels"


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=SITE_ID,
        title="Amber FAKENMI000",
        data={
            CONF_API_KEY: "old_key",
            CONF_SITE_ID: SITE_ID,
            CONF_NMI: "FAKENMI000",
            CONF_CHANNELS: [{"identifier": "E1", "type": "general", "tariff": None}],
        },
    )
    entry.add_to_hass(hass)
    return entry


async def test_reauth_success(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """A new key that still sees the site replaces the old key and reloads."""
    entry = _entry(hass)
    aioclient_mock.get(SITES_URL, json=[site_json()])

    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: "new_key"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_API_KEY] == "new_key"
    assert entry.data[CONF_SITE_ID] == SITE_ID


async def test_reauth_wrong_site_then_bad_key(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A key for another account, or a rejected key, keeps the form open."""
    entry = _entry(hass)
    aioclient_mock.get(SITES_URL, json=[site_json(id="01OTHERSITE000000000000000")])

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: "other_account"}
    )
    assert result["errors"] == {"base": "site_not_found"}

    aioclient_mock.clear_requests()
    aioclient_mock.get(SITES_URL, status=403)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: "bad"}
    )
    assert result["errors"] == {"base": "invalid_auth"}
    assert entry.data[CONF_API_KEY] == "old_key"


async def _to_schedule_step(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    aioclient_mock.get(SITES_URL, json=[site_json()])
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_API_KEY: KEY})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SITE_ID: SITE_ID}
    )
    return await hass.config_entries.flow.async_configure(result["flow_id"], {})


@pytest.mark.parametrize(
    "bad", ["", "7:15, 25:00", "0715", "07:15, 08:15, 09:15, 10:15, 11:15, 12:15, 13:15"]
)
async def test_fixed_times_validated(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, bad: str
) -> None:
    """Fixed times must be 1 to 6 valid HH:MM values; errors keep the form open."""
    result = await _to_schedule_step(hass, aioclient_mock)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SCHEDULE_MODE: "fixed", CONF_FIXED_TIMES: bad}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_FIXED_TIMES: "invalid_times"}


async def test_fixed_times_saved_sorted(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Valid fixed times are normalised, de-duplicated and sorted."""
    result = await _to_schedule_step(hass, aioclient_mock)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_SCHEDULE_MODE: "fixed", CONF_FIXED_TIMES: "13:15, 7:15; 10:15, 07:15"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"] == {
        CONF_SCHEDULE_MODE: "fixed",
        CONF_FIXED_TIMES: ["07:15", "10:15", "13:15"],
    }
    await hass.async_block_till_done()
    manager = result["result"].runtime_data.manager
    assert [t.strftime("%H:%M") for t in manager.schedule.fixed_times] == [
        "07:15",
        "10:15",
        "13:15",
    ]


async def test_options_flow_changes_schedule_without_reload(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The options flow reschedules in place: no reload, so no extra API call."""
    result = await _to_schedule_step(hass, aioclient_mock)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SCHEDULE_MODE: "automatic"}
    )
    entry = result["result"]
    await hass.async_block_till_done()
    calls = aioclient_mock.call_count
    manager = entry.runtime_data.manager

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCHEDULE_MODE: "fixed", CONF_FIXED_TIMES: "x"}
    )
    assert result["errors"] == {CONF_FIXED_TIMES: "invalid_times"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCHEDULE_MODE: "fixed", CONF_FIXED_TIMES: "07:15, 10:15"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_SCHEDULE_MODE: "fixed", CONF_FIXED_TIMES: ["07:15", "10:15"]}
    assert entry.runtime_data.manager is manager  # same instance: not reloaded
    assert manager.schedule.mode == "fixed"
    assert manager.next_run.astimezone(dt_util.get_time_zone(hass.config.time_zone)).strftime(
        "%H:%M"
    ) in {"07:15", "10:15"}
    assert aioclient_mock.call_count == calls

    # And back to automatic, with the current fixed times offered as the suggestion.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    schema_fields = {str(k): k for k in result["data_schema"].schema}
    assert schema_fields[CONF_FIXED_TIMES].description == {"suggested_value": "07:15, 10:15"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCHEDULE_MODE: "automatic"}
    )
    assert entry.options == {CONF_SCHEDULE_MODE: "automatic"}
    assert manager.schedule.mode == "automatic"
