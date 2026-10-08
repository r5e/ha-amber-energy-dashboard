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
from homeassistant.helpers import config_validation as cv, device_registry as dr
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
    Site,
)
from .bill import CHARGES, DEFAULT_GST_PERCENT, BillSettings
from .const import (
    ATTR_ACKNOWLEDGE_UNVERIFIED,
    ATTR_CONFIG_ENTRY_ID,
    ATTR_CONFIRM,
    ATTR_CONFIRM_BACKUP,
    ATTR_DATE,
    ATTR_DRY_RUN,
    ATTR_END_DATE,
    ATTR_EXCLUDE_FLAGGED,
    ATTR_START_DATE,
    CONF_ADD_TO_ENERGY,
    CONF_ALLOWANCE,
    CONF_ALLOWANCE_KWH,
    CONF_ALLOWANCE_TOTALLING,
    CONF_BILLING_DAY,
    CONF_CHANNEL,
    CONF_CHANNELS,
    CONF_FIXED_STATISTIC,
    CONF_FIXED_TIMES,
    CONF_GST_PERCENT,
    CONF_OWN_FALLBACK,
    CONF_PATIENCE_DAYS,
    CONF_PENALTY_PERIOD,
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
    SERVICE_BILL_ESTIMATE,
    SERVICE_DELETE_LEGACY,
    SERVICE_IMPORT_DAY,
    SERVICE_MIGRATE,
    SERVICE_PROBE_RETENTION,
    SERVICE_RUN_NOW,
    SERVICE_UNDO_MIGRATION,
    SUBENTRY_OWN_SENSOR,
)
from .devices import device_info
from .energy import async_add_to_energy, async_check_energy_issue, async_listen_for_changes
from .export import (
    DEFAULT_PENALTY_PERIOD,
    DEFAULT_REWARD_PERIOD,
    TOTALLING_PERIOD,
    AllowanceSettings,
    TwoWayTariff,
    detect_tariff,
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
        vol.Optional(ATTR_ACKNOWLEDGE_UNVERIFIED, default=False): cv.boolean,
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
    device_id: str = ""
    """The site's device in the device registry."""
    last_import: dict[str, Any] | None = None
    last_error: dict[str, str] | None = None


type AmberConfigEntry = ConfigEntry[AmberRuntimeData]


def settings_from_options(
    options: dict[str, Any], tariff: TwoWayTariff | None = None
) -> tuple[tuple, dict[str, Any]]:
    """Positional and keyword settings for the manager, from entry options (and the
    two-way tariff detected at setup)."""
    bill = bill_from_options(options)
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
            "bill": bill,
            "allowance": allowance_from_options(options, tariff, bill),
        },
    )


def allowance_from_options(
    options: dict[str, Any], tariff: TwoWayTariff | None, bill: BillSettings | None
) -> AllowanceSettings | None:
    """The export allowance settings, or None when it is off (section 20).

    By default it is on for a known two-way tariff with a billing day set. The options
    override the allowance, the totalling and the penalty period. Billing-period
    totalling needs the billing day; without one only daily totalling works.
    """
    enabled = options.get(CONF_ALLOWANCE)
    if enabled is None:
        enabled = tariff is not None and bill is not None
    per_day = options.get(CONF_ALLOWANCE_KWH) or (tariff.allowance_kwh_per_day if tariff else 0)
    totalling = options.get(CONF_ALLOWANCE_TOTALLING) or (
        tariff.totalling if tariff else TOTALLING_PERIOD
    )
    billing_day = bill.billing_day if bill else None
    if not enabled or not per_day or (totalling == TOTALLING_PERIOD and billing_day is None):
        return None
    return AllowanceSettings(
        allowance_kwh_per_day=float(per_day),
        totalling=totalling,
        penalty_period=options.get(CONF_PENALTY_PERIOD)
        or (tariff.penalty_period if tariff else DEFAULT_PENALTY_PERIOD),
        reward_period=tariff.reward_period if tariff else DEFAULT_REWARD_PERIOD,
        billing_day=billing_day,
    )


def bill_from_options(options: dict[str, Any]) -> BillSettings | None:
    """The bill estimate settings, or None until a billing day is set (section 19)."""
    if not options.get(CONF_BILLING_DAY):
        return None
    return BillSettings(
        billing_day=int(options[CONF_BILLING_DAY]),
        charges={name: float(options.get(name) or 0.0) for name in CHARGES},
        gst_percent=float(options.get(CONF_GST_PERCENT, DEFAULT_GST_PERCENT)),
        fixed_statistic=bool(options.get(CONF_FIXED_STATISTIC, False)),
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
    _register_bill_service(hass)
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


def _register_bill_service(hass: HomeAssistant) -> None:
    """bill_estimate: the estimate for the cycle containing a date (default today)."""

    async def _bill_estimate(call: ServiceCall) -> ServiceResponse:
        entry = _resolve_entry(hass, call.data.get(ATTR_CONFIG_ENTRY_ID))
        manager = entry.runtime_data.manager
        if manager.bill is None:
            raise ServiceValidationError(
                "Set a billing day in the integration's options (Bill estimate) first."
            )
        if not manager.bill_available:
            raise ServiceValidationError(
                "Pricing-only mode imports no usage, so there is no bill estimate."
            )
        return manager.bill_estimate(call.data.get(ATTR_DATE) or nem_today())

    hass.services.async_register(
        DOMAIN,
        SERVICE_BILL_ESTIMATE,
        _bill_estimate,
        schema=vol.Schema(
            {vol.Optional(ATTR_DATE): cv.date, vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string}
        ),
        supports_response=SupportsResponse.ONLY,
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
                acknowledge_unverified=call.data[ATTR_ACKNOWLEDGE_UNVERIFIED],
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


async def _async_detect_tariff(store: AmberStore, site: Site) -> TwoWayTariff | None:
    """The known two-way tariff from this setup's /sites answer; recorded in the Store."""
    tariff = detect_tariff(site.network, [c.tariff for c in site.channels])
    await store.async_set_tariff(
        {
            "network": site.network,
            "tariffs": {c.identifier: c.tariff for c in site.channels},
            "known": f"{tariff.network} {tariff.tariff}" if tariff else None,
        }
    )
    return tariff


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
    tariff = await _async_detect_tariff(store, site)
    (schedule, patience_days, revision_days), extra = settings_from_options(
        dict(entry.options), tariff
    )
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
        tariff=tariff,
        **extra,
    )
    # The site's device first, so own-sensor devices can link to it (section 18).
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, **device_info(site_id)
    )
    entry.runtime_data = AmberRuntimeData(context=ctx, manager=manager, device_id=device.id)
    if entry.data.get(CONF_ADD_TO_ENERGY) and store.energy_setup is None:
        # Chosen in the config flow: carried out once (section 18).
        await async_add_to_energy(hass, manager, "setup")

    async def _check_energy() -> None:
        await async_check_energy_issue(hass, manager)

    async def _check_all(_hass: HomeAssistant | None = None) -> None:
        await async_check_cleanup(hass, manager)
        await _check_energy()

    async def _started(_hass: HomeAssistant) -> None:
        await async_listen_for_changes(hass)
        await _check_all()

    # Re-check the YAML kit's leftovers and the Energy dashboard once Home Assistant has
    # started (so the kit's entities have been set up), and after each scheduled attempt;
    # the Energy dashboard also after every run and whenever its preferences change.
    entry.async_on_unload(async_at_started(hass, _started))
    manager.after_scheduled = _check_all
    manager.after_run = _check_energy

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
    args, kwargs = settings_from_options(dict(entry.options), manager.tariff)
    # Own-sensor mappings, or turning the bill estimate or the allowance on or off,
    # change the entities.
    if (
        current != wanted
        or (manager.bill is None) != (kwargs["bill"] is None)
        or (manager.allowance is None) != (kwargs["allowance"] is None)
    ):
        hass.config_entries.async_schedule_reload(entry.entry_id)
        return
    manager.async_update_settings(*args, **kwargs)


async def async_unload_entry(hass: HomeAssistant, entry: AmberConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: AmberConfigEntry) -> None:
    """Delete the entry's Store when the entry is removed (statistics are kept)."""
    await AmberStore(hass, entry.entry_id).async_remove()
