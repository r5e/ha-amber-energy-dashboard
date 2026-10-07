"""Amber Energy Dashboard (unofficial).

Imports Amber Electric usage and cost history into Home Assistant external statistics.
Milestone 3: Store, Guard 1, retention discovery, scheduled catch-up and display sensors.
"""

import asyncio
from dataclasses import dataclass
from datetime import date
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_API_KEY, CONF_NAME, Platform
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
    ATTR_CONFIRM,
    ATTR_CONFIRM_BACKUP,
    ATTR_DATE,
    ATTR_DRY_RUN,
    ATTR_END_DATE,
    ATTR_EXCLUDE_FLAGGED,
    ATTR_START_DATE,
    CONF_CHANNEL,
    CONF_CHANNELS,
    CONF_FIXED_TIMES,
    CONF_OWN_FALLBACK,
    CONF_PATIENCE_DAYS,
    CONF_PRICE_SERIES,
    CONF_REVISION_DAYS,
    CONF_SCHEDULE_MODE,
    CONF_SENSOR,
    CONF_SITE_ID,
    CONF_USAGE_MODE,
    DEFAULT_PATIENCE_DAYS,
    DEFAULT_REVISION_DAYS,
    DOMAIN,
    MODE_FULL,
    MODE_PRICING,
    ROLES,
    SCHEDULE_AUTOMATIC,
    SCHEDULE_FIXED,
    SERVICE_BACKFILL,
    SERVICE_DELETE_LEGACY,
    SERVICE_IMPORT_DAY,
    SERVICE_MIGRATE,
    SERVICE_PROBE_RETENTION,
    SERVICE_RUN_NOW,
    SERVICE_UNDO_MIGRATION,
    SUBENTRY_OWN_SENSOR,
)
from .importer import ImportContext
from .manager import AmberManager, OwnSensor, nem_today
from .migration import (
    MigrationRefused,
    async_check_cleanup,
    async_delete_legacy,
    async_migrate,
    async_undo,
)
from .schedule import ScheduleConfig, parse_times
from .statistics import ChannelConfig, build_specs, own_cost_spec
from .storage import AmberStore, async_forget_chain

PLATFORMS = [Platform.BUTTON, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

IMPORT_DAY_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DATE): cv.date,
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
    }
)
RUN_NOW_SCHEMA = vol.Schema({vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string})
BACKFILL_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_START_DATE): cv.date,
        vol.Required(ATTR_END_DATE): cv.date,
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
    }
)
MIGRATE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_DRY_RUN, default=True): cv.boolean,
        vol.Optional(ATTR_CONFIRM_BACKUP, default=False): cv.boolean,
        vol.Optional(ATTR_EXCLUDE_FLAGGED, default=False): cv.boolean,
        **{vol.Optional(role): cv.string for role in ROLES},
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
    }
)
DELETE_LEGACY_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIRM): cv.boolean,
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
    }
)


@dataclass(slots=True)
class AmberRuntimeData:
    """Per-entry runtime state."""

    context: ImportContext
    manager: AmberManager
    last_import: dict[str, Any] | None = None
    last_error: dict[str, str] | None = None


type AmberConfigEntry = ConfigEntry[AmberRuntimeData]


def settings_from_options(options: dict[str, Any]) -> tuple[tuple, dict[str, Any]]:
    """Positional and keyword settings for the manager, from entry options."""
    return (
        (
            schedule_from_options(options),
            int(options.get(CONF_PATIENCE_DAYS, DEFAULT_PATIENCE_DAYS)),
            int(options.get(CONF_REVISION_DAYS, DEFAULT_REVISION_DAYS)),
        ),
        {
            "usage_mode": options.get(CONF_USAGE_MODE, MODE_FULL),
            "price_series": bool(options.get(CONF_PRICE_SERIES, False)),
            "own_fallback": bool(options.get(CONF_OWN_FALLBACK, True)),
        },
    )


def own_sensors_from_entry(
    entry: ConfigEntry, channels: tuple[ChannelConfig, ...]
) -> list[OwnSensor]:
    """The own-sensor mappings (config sub-entries) whose channel still exists."""
    by_ident = {c.identifier: c for c in channels}
    sensors = []
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_OWN_SENSOR:
            continue
        channel = by_ident.get(subentry.data[CONF_CHANNEL])
        if channel is None:
            continue
        entity_id = subentry.data[CONF_SENSOR]
        sensors.append(
            OwnSensor(
                subentry.subentry_id,
                entity_id,
                channel,
                own_cost_spec(entry.data[CONF_SITE_ID], entity_id, channel),
                subentry.data.get(CONF_NAME, entity_id),
            )
        )
    return sensors


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

    async def _backfill(call: ServiceCall) -> ServiceResponse:
        entry = _resolve_entry(hass, call.data.get(ATTR_CONFIG_ENTRY_ID))
        start: date = call.data[ATTR_START_DATE]
        end: date = call.data[ATTR_END_DATE]
        manager = entry.runtime_data.manager
        if manager.usage_mode == MODE_PRICING:
            raise ServiceValidationError(
                "Pricing-only mode writes no usage statistics; there is nothing to backfill."
            )
        if end < start:
            raise ServiceValidationError(f"end_date {end} is before start_date {start}")
        summary = await manager.async_run("backfill", backfill=(start, end))
        if summary.get("reason") in ("backfill_forward", "backfill_range"):
            raise ServiceValidationError(summary.get("message") or summary["reason"])
        return summary if call.return_response else None

    hass.services.async_register(
        DOMAIN,
        SERVICE_BACKFILL,
        _backfill,
        schema=BACKFILL_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RUN_NOW,
        _run_now,
        schema=RUN_NOW_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    _register_retention_service(hass)
    _register_migration_services(hass)
    return True


async def async_probe_retention(manager: AmberManager) -> dict[str, Any]:
    """A fresh retention discovery (the service and the button). Failures raise
    HomeAssistantError; the stored boundary is kept."""
    try:
        return await manager.async_probe_retention()
    except AmberAuthError as err:
        raise HomeAssistantError(
            "Amber rejected the API key; re-authenticate the integration"
        ) from err
    except AmberError as err:
        raise HomeAssistantError(
            f"Retention probe failed ({err}); the stored boundary was kept"
        ) from err


def _register_retention_service(hass: HomeAssistant) -> None:
    """probe_retention: force a fresh discovery of the retention boundary."""

    async def _probe_retention(call: ServiceCall) -> ServiceResponse:
        entry = _resolve_entry(hass, call.data.get(ATTR_CONFIG_ENTRY_ID))
        result = await async_probe_retention(entry.runtime_data.manager)
        return result if call.return_response else None

    hass.services.async_register(
        DOMAIN,
        SERVICE_PROBE_RETENTION,
        _probe_retention,
        schema=RUN_NOW_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


def _register_migration_services(hass: HomeAssistant) -> None:
    """The YAML-kit migration services (section 14)."""

    async def _migrate(call: ServiceCall) -> ServiceResponse:
        entry = _resolve_entry(hass, call.data.get(ATTR_CONFIG_ENTRY_ID))
        try:
            report = await async_migrate(
                hass,
                entry.runtime_data.manager,
                dry_run=call.data[ATTR_DRY_RUN],
                confirm_backup=call.data[ATTR_CONFIRM_BACKUP],
                picks={role: call.data.get(role) for role in ROLES},
                exclude_flagged=call.data[ATTR_EXCLUDE_FLAGGED],
            )
        except MigrationRefused as err:
            raise ServiceValidationError(str(err)) from err
        return report if call.return_response else None

    async def _undo(call: ServiceCall) -> ServiceResponse:
        entry = _resolve_entry(hass, call.data.get(ATTR_CONFIG_ENTRY_ID))
        try:
            result = await async_undo(hass, entry.runtime_data.manager)
        except MigrationRefused as err:
            raise ServiceValidationError(str(err)) from err
        return result if call.return_response else None

    async def _delete_legacy(call: ServiceCall) -> ServiceResponse:
        entry = _resolve_entry(hass, call.data.get(ATTR_CONFIG_ENTRY_ID))
        try:
            result = await async_delete_legacy(
                hass, entry.runtime_data.manager, confirm=call.data[ATTR_CONFIRM]
            )
        except MigrationRefused as err:
            raise ServiceValidationError(str(err)) from err
        return result if call.return_response else None

    for name, handler, schema in (
        (SERVICE_MIGRATE, _migrate, MIGRATE_SCHEMA),
        (SERVICE_UNDO_MIGRATION, _undo, RUN_NOW_SCHEMA),
        (SERVICE_DELETE_LEGACY, _delete_legacy, DELETE_LEGACY_SCHEMA),
    ):
        hass.services.async_register(
            DOMAIN, name, handler, schema=schema, supports_response=SupportsResponse.OPTIONAL
        )


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

    store = AmberStore(hass, entry.entry_id, today=nem_today)
    await store.async_load()
    ctx = ImportContext(
        client=client,
        site_id=site_id,
        channels=channels,
        specs=tuple(build_specs(site_id, channels)),
        lock=asyncio.Lock(),
    )
    (schedule, patience_days, revision_days), extra = settings_from_options(dict(entry.options))
    own = own_sensors_from_entry(entry, channels)
    known = {s.key for s in own} | {"price"}
    for key in list(store.as_dict()["chains"]):
        if key not in known:
            await async_forget_chain(store, key)  # mapping removed; statistics are kept
    manager = AmberManager(
        hass,
        entry,
        ctx,
        store,
        schedule,
        active_from=site.active_from,
        patience_days=patience_days,
        revision_days=revision_days,
        own_sensors=own,
        **extra,
    )
    entry.runtime_data = AmberRuntimeData(context=ctx, manager=manager)

    async def _check_cleanup(_hass: HomeAssistant | None = None) -> None:
        await async_check_cleanup(hass, manager)

    # Re-check the YAML kit's leftovers once Home Assistant has started (so the kit's
    # entities have been set up), and after each scheduled run.
    entry.async_on_unload(async_at_started(hass, _check_cleanup))
    manager.after_scheduled = _check_cleanup

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
    """Apply option changes in place; reload only when own-sensor mappings changed."""
    manager = entry.runtime_data.manager
    current = {s.subentry_id for s in manager.own_sensors}
    wanted = {
        sid for sid, sub in entry.subentries.items() if sub.subentry_type == SUBENTRY_OWN_SENSOR
    }
    if current != wanted:
        hass.config_entries.async_schedule_reload(entry.entry_id)
        return
    args, kwargs = settings_from_options(dict(entry.options))
    manager.async_update_settings(*args, **kwargs)


async def async_unload_entry(hass: HomeAssistant, entry: AmberConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: AmberConfigEntry) -> None:
    """Delete the entry's Store when the entry is removed (statistics are kept)."""
    await AmberStore(hass, entry.entry_id).async_remove()
