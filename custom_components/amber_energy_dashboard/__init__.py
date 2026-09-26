"""Amber Energy Dashboard (unofficial).

Imports Amber Electric usage and cost history into Home Assistant external statistics.
Milestone 2: config flow, statistics model and the ``import_day`` service.
"""

import asyncio
from dataclasses import dataclass
from datetime import date
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_API_KEY
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType
import voluptuous as vol

from .api import (
    AmberAuthError,
    AmberBudgetExhaustedError,
    AmberClient,
    AmberConnectionError,
    AmberError,
    AmberRateLimitError,
    AmberServerError,
)
from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_DATE,
    CONF_CHANNELS,
    CONF_SITE_ID,
    DOMAIN,
    SERVICE_IMPORT_DAY,
)
from .importer import ImportContext, async_import_day
from .statistics import ChannelConfig, build_specs

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

IMPORT_DAY_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DATE): cv.date,
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
    }
)


@dataclass(slots=True)
class AmberRuntimeData:
    """Per-entry runtime state."""

    context: ImportContext
    last_import: dict[str, Any] | None = None
    last_error: dict[str, str] | None = None


type AmberConfigEntry = ConfigEntry[AmberRuntimeData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the services (once, independent of config entries)."""

    async def _import_day(call: ServiceCall) -> ServiceResponse:
        entry = _resolve_entry(hass, call.data.get(ATTR_CONFIG_ENTRY_ID))
        day: date = call.data[ATTR_DATE]
        runtime = entry.runtime_data
        try:
            summary = await async_import_day(hass, runtime.context, day)
        except AmberAuthError as err:
            entry.async_start_reauth(hass)
            runtime.last_error = {"date": str(day), "reason": "auth"}
            raise HomeAssistantError(
                "Amber rejected the API key. Re-authenticate the integration."
            ) from err
        except AmberBudgetExhaustedError as err:
            runtime.last_error = {"date": str(day), "reason": "rate_limit_reserve"}
            raise HomeAssistantError(
                f"Stopped to protect the shared Amber rate limit: {err}. Try again later."
            ) from err
        except AmberRateLimitError as err:
            runtime.last_error = {"date": str(day), "reason": "rate_limited"}
            raise HomeAssistantError(
                f"Amber rate limit reached; retry after {err.retry_after or 'a few'} seconds."
            ) from err
        except (AmberConnectionError, AmberServerError) as err:
            runtime.last_error = {"date": str(day), "reason": "unavailable"}
            raise HomeAssistantError(f"Amber API unavailable: {err}") from err
        except AmberError as err:
            runtime.last_error = {"date": str(day), "reason": "api_error"}
            raise HomeAssistantError(f"Amber API error: {err}") from err
        except HomeAssistantError as err:
            runtime.last_error = {
                "date": str(day),
                "reason": getattr(err, "reason", "error"),
                "message": str(err),
            }
            raise
        runtime.last_import = summary
        runtime.last_error = None
        return summary if call.return_response else None

    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_DAY,
        _import_day,
        schema=IMPORT_DAY_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


def _resolve_entry(hass: HomeAssistant, entry_id: str | None) -> AmberConfigEntry:
    entries = [
        e for e in hass.config_entries.async_entries(DOMAIN) if e.state is ConfigEntryState.LOADED
    ]
    if entry_id is not None:
        for entry in entries:
            if entry.entry_id == entry_id:
                return entry
        raise ServiceValidationError(f"No loaded {DOMAIN} config entry {entry_id}")
    if not entries:
        raise ServiceValidationError(f"No loaded {DOMAIN} config entry")
    if len(entries) > 1:
        raise ServiceValidationError(
            f"Several {DOMAIN} config entries are loaded; pass config_entry_id"
        )
    return entries[0]


async def async_setup_entry(hass: HomeAssistant, entry: AmberConfigEntry) -> bool:
    """Set up one Amber site, validating the key with one GET /sites.

    A rejected key, or a key that no longer sees this site, starts reauth. Network
    errors, 5xx, 429 and unexpected responses retry setup later.
    """
    channels = tuple(ChannelConfig.from_dict(c) for c in entry.data[CONF_CHANNELS])
    site_id = entry.data[CONF_SITE_ID]
    client = AmberClient(async_get_clientsession(hass), entry.data[CONF_API_KEY])
    try:
        sites = await client.async_get_sites()
    except AmberAuthError as err:
        raise ConfigEntryAuthFailed("Amber rejected the API key") from err
    except (AmberConnectionError, AmberServerError, AmberRateLimitError) as err:
        raise ConfigEntryNotReady(f"Amber API unavailable: {err}") from err
    except AmberError as err:
        raise ConfigEntryNotReady(f"Unexpected response from Amber: {err}") from err
    if site_id not in {site.id for site in sites}:
        raise ConfigEntryAuthFailed("The API key no longer has access to this site")
    entry.runtime_data = AmberRuntimeData(
        ImportContext(
            client=client,
            site_id=site_id,
            channels=channels,
            specs=tuple(build_specs(site_id, channels)),
            lock=asyncio.Lock(),
        )
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AmberConfigEntry) -> bool:
    """Unload a config entry."""
    return True
