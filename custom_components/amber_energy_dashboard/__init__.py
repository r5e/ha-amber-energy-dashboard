"""Amber Energy Dashboard (unofficial).

Imports Amber Electric usage and cost history into Home Assistant external statistics.
Milestone 3: Store, Guard 1, retention discovery, scheduled catch-up and display sensors.
"""

import asyncio
from dataclasses import dataclass
from datetime import date
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_API_KEY, Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.start import async_at_started
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
    CONF_FIXED_TIMES,
    CONF_PATIENCE_DAYS,
    CONF_REVISION_DAYS,
    CONF_SCHEDULE_MODE,
    CONF_SITE_ID,
    DEFAULT_PATIENCE_DAYS,
    DEFAULT_REVISION_DAYS,
    DOMAIN,
    SCHEDULE_AUTOMATIC,
    SCHEDULE_FIXED,
    SERVICE_IMPORT_DAY,
    SERVICE_RUN_NOW,
)
from .importer import ImportContext
from .manager import AmberManager
from .schedule import ScheduleConfig, parse_times
from .statistics import ChannelConfig, build_specs
from .storage import AmberStore

PLATFORMS = [Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

IMPORT_DAY_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DATE): cv.date,
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
    }
)
RUN_NOW_SCHEMA = vol.Schema({vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string})


@dataclass(slots=True)
class AmberRuntimeData:
    """Per-entry runtime state."""

    context: ImportContext
    manager: AmberManager
    last_import: dict[str, Any] | None = None
    last_error: dict[str, str] | None = None


type AmberConfigEntry = ConfigEntry[AmberRuntimeData]


def settings_from_options(options: dict[str, Any]) -> tuple[ScheduleConfig, int, int]:
    """Schedule, patience days and revision days from entry options."""
    return (
        schedule_from_options(options),
        int(options.get(CONF_PATIENCE_DAYS, DEFAULT_PATIENCE_DAYS)),
        int(options.get(CONF_REVISION_DAYS, DEFAULT_REVISION_DAYS)),
    )


def schedule_from_options(options: dict[str, Any]) -> ScheduleConfig:
    """Build the schedule from entry options (automatic when unset)."""
    if options.get(CONF_SCHEDULE_MODE) == SCHEDULE_FIXED:
        return ScheduleConfig(SCHEDULE_FIXED, parse_times(options.get(CONF_FIXED_TIMES, [])))
    return ScheduleConfig(SCHEDULE_AUTOMATIC)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the services (once, independent of config entries)."""

    async def _import_day(call: ServiceCall) -> ServiceResponse:
        entry = _resolve_entry(hass, call.data.get(ATTR_CONFIG_ENTRY_ID))
        day: date = call.data[ATTR_DATE]
        runtime = entry.runtime_data
        try:
            summary = await runtime.manager.async_import_day(day)
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

    async def _run_now(call: ServiceCall) -> ServiceResponse:
        entry = _resolve_entry(hass, call.data.get(ATTR_CONFIG_ENTRY_ID))
        summary = await entry.runtime_data.manager.async_run("run_now")
        return summary if call.return_response else None

    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_DAY,
        _import_day,
        schema=IMPORT_DAY_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RUN_NOW,
        _run_now,
        schema=RUN_NOW_SCHEMA,
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
    site = next((s for s in sites if s.id == site_id), None)
    if site is None:
        raise ConfigEntryAuthFailed("The API key no longer has access to this site")

    store = AmberStore(hass, entry.entry_id)
    await store.async_load()
    ctx = ImportContext(
        client=client,
        site_id=site_id,
        channels=channels,
        specs=tuple(build_specs(site_id, channels)),
        lock=asyncio.Lock(),
    )
    schedule, patience_days, revision_days = settings_from_options(dict(entry.options))
    manager = AmberManager(
        hass,
        entry,
        ctx,
        store,
        schedule,
        active_from=site.active_from,
        patience_days=patience_days,
        revision_days=revision_days,
    )
    entry.runtime_data = AmberRuntimeData(context=ctx, manager=manager)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    manager.async_start()
    entry.async_on_unload(manager.async_stop)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    if manager.needs_startup_run:
        # First setup (no marker yet), or the previous run was interrupted: run as soon
        # as Home Assistant has started, rather than waiting for the next schedule slot.
        trigger = "first_setup" if store.marker is None else "resume_after_interruption"

        async def _start_run(_hass: HomeAssistant) -> None:
            entry.async_create_background_task(
                hass, manager.async_run(trigger), f"{DOMAIN} {trigger}"
            )

        entry.async_on_unload(async_at_started(hass, _start_run))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: AmberConfigEntry) -> None:
    """Apply option changes without reloading (no API call needed)."""
    entry.runtime_data.manager.async_update_settings(*settings_from_options(dict(entry.options)))


async def async_unload_entry(hass: HomeAssistant, entry: AmberConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: AmberConfigEntry) -> None:
    """Delete the entry's Store when the entry is removed (statistics are kept)."""
    await AmberStore(hass, entry.entry_id).async_remove()
