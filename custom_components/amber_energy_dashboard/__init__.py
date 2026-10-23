"""Amber Energy Dashboard (unofficial).

Imports Amber Electric usage and cost history into Home Assistant external statistics.
The config entry is the Amber account (its API key); each site is a sub-entry (section 23).
"""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState, ConfigSubentry
from homeassistant.const import CONF_API_KEY, Platform
from homeassistant.core import (
    Event,
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_device_registry_updated_event
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
from .bill import CHARGES, DEFAULT_GST_PERCENT, BillSettings, migrate_charges
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
    CONF_CHANNELS,
    CONF_FIXED_STATISTIC,
    CONF_FIXED_TIMES,
    CONF_GST_PERCENT,
    CONF_NMI,
    CONF_OWN_FALLBACK,
    CONF_PATIENCE_DAYS,
    CONF_PENALTY_PERIOD,
    CONF_PRICE_SERIES,
    CONF_REVISION_DAYS,
    CONF_SCHEDULE_MODE,
    CONF_SITE,
    CONF_SITE_ID,
    CONF_SITE_NAME,
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
)
from .devices import (
    device_info,
    displayed_site_name,
    entry_title,
    is_default_title,
    site_model,
)
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
from .manager import AmberManager, nem_today
from .meters import async_update_convert_issue, own_sensors_from_entry
from .migration import (
    MigrationRefused,
    async_check_cleanup,
    async_delete_legacy,
    async_migrate,
    async_undo,
)
from .schedule import ScheduleConfig, parse_times
from .sites import (
    SiteMigrationError,
    async_ensure_site_ownership,
    async_migrate_to_sites,
    site_key,
    site_options,
    site_subentries,
)
from .statistics import ChannelConfig, build_specs
from .storage import AmberStore, async_forget_chain

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BUTTON, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

IMPORT_DAY_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DATE): cv.date,
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Optional(CONF_SITE): cv.string,
    }
)
RUN_NOW_SCHEMA = vol.Schema(
    {vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string, vol.Optional(CONF_SITE): cv.string}
)
BACKFILL_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_START_DATE): cv.date,
        vol.Required(ATTR_END_DATE): cv.date,
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Optional(CONF_SITE): cv.string,
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
        vol.Optional(CONF_SITE): cv.string,
    }
)
DELETE_LEGACY_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIRM): cv.boolean,
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Optional(CONF_SITE): cv.string,
    }
)


@dataclass(slots=True)
class SiteRuntime:
    """One site's runtime state (a site sub-entry, section 23)."""

    subentry_id: str
    context: ImportContext
    manager: AmberManager
    device_id: str = ""
    """The site's device in the device registry."""
    last_import: dict[str, Any] | None = None
    last_error: dict[str, str] | None = None
    options: dict[str, Any] = field(default_factory=dict)
    """The settings the manager runs with (a sub-entry update may change only a title)."""


@dataclass(slots=True)
class AmberRuntimeData:
    """Per-entry (Amber account) runtime state: the account's client and run lock, shared
    by its sites, so their runs take turns on one rate budget (section 23)."""

    client: AmberClient
    lock: asyncio.Lock
    sites: dict[str, SiteRuntime] = field(default_factory=dict)
    """By site sub-entry ID, the entry's first site first."""
    converting: bool = False
    """Sub-entries are being converted (section 21): one reload follows, at the end."""

    @property
    def site(self) -> SiteRuntime:
        """The entry's first site (its only one, for most accounts)."""
        return next(iter(self.sites.values()))

    @property
    def manager(self) -> AmberManager:
        """The first site's manager."""
        return self.site.manager

    @property
    def context(self) -> ImportContext:
        """The first site's import context."""
        return self.site.context

    @property
    def device_id(self) -> str:
        """The first site's device."""
        return self.site.device_id

    @property
    def last_import(self) -> dict[str, Any] | None:
        """The first site's last import_day result."""
        return self.site.last_import

    @property
    def last_error(self) -> dict[str, str] | None:
        """The first site's last import_day error."""
        return self.site.last_error


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


def schedule_from_options(options: dict[str, Any]) -> ScheduleConfig:
    """Build the schedule from entry options (automatic when unset)."""
    if options.get(CONF_SCHEDULE_MODE) == SCHEDULE_FIXED:
        return ScheduleConfig(SCHEDULE_FIXED, parse_times(options.get(CONF_FIXED_TIMES, [])))
    return ScheduleConfig(SCHEDULE_AUTOMATIC)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the services (once, independent of config entries)."""

    async def _import_day(call: ServiceCall) -> ServiceResponse:
        entry, runtime = _resolve_site(hass, call.data)
        day: date = call.data[ATTR_DATE]
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
        _, site = _resolve_site(hass, call.data)
        summary = await site.manager.async_run("run_now")
        return summary if call.return_response else None

    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_DAY,
        _import_day,
        schema=IMPORT_DAY_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    async def _backfill(call: ServiceCall) -> ServiceResponse:
        _, site = _resolve_site(hass, call.data)
        start: date = call.data[ATTR_START_DATE]
        end: date = call.data[ATTR_END_DATE]
        manager = site.manager
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
        _, site = _resolve_site(hass, call.data)
        result = await async_probe_retention(site.manager)
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
        _, site = _resolve_site(hass, call.data)
        manager = site.manager
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
            {
                vol.Optional(ATTR_DATE): cv.date,
                vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
                vol.Optional(CONF_SITE): cv.string,
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )


def _register_migration_services(hass: HomeAssistant) -> None:
    """The YAML-kit migration services (section 14)."""

    async def _migrate(call: ServiceCall) -> ServiceResponse:
        _, site = _resolve_site(hass, call.data)
        try:
            report = await async_migrate(
                hass,
                site.manager,
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
        _, site = _resolve_site(hass, call.data)
        try:
            result = await async_undo(hass, site.manager)
        except MigrationRefused as err:
            raise ServiceValidationError(str(err)) from err
        return result if call.return_response else None

    async def _delete_legacy(call: ServiceCall) -> ServiceResponse:
        _, site = _resolve_site(hass, call.data)
        try:
            result = await async_delete_legacy(hass, site.manager, confirm=call.data[ATTR_CONFIRM])
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


def _resolve_site(
    hass: HomeAssistant, data: Mapping[str, Any]
) -> tuple[AmberConfigEntry, SiteRuntime]:
    """The site a service call is for (section 23): ``site`` (the site's device, its
    Amber site ID or its sub-entry ID) and/or ``config_entry_id``; either may be left
    out while it leaves exactly one loaded site."""
    entry_id, ref = data.get(ATTR_CONFIG_ENTRY_ID), data.get(CONF_SITE)
    entries = [
        e for e in hass.config_entries.async_entries(DOMAIN) if e.state is ConfigEntryState.LOADED
    ]
    if entry_id is not None:
        entries = [e for e in entries if e.entry_id == entry_id]
        if not entries:
            raise ServiceValidationError(f"No loaded {DOMAIN} config entry {entry_id}")
    sites = [(e, s) for e in entries for s in e.runtime_data.sites.values()]
    if ref is not None:
        sites = [
            (e, s)
            for e, s in sites
            if ref in (s.device_id, s.subentry_id) or ref.lower() == s.context.site_id.lower()
        ]
        if not sites:
            raise ServiceValidationError(f"No loaded {DOMAIN} site {ref}")
    if not sites:
        raise ServiceValidationError(f"No loaded {DOMAIN} site")
    if len(sites) > 1:
        raise ServiceValidationError(
            f"Several {DOMAIN} sites are loaded; pass site (the site's device)"
        )
    return sites[0]


async def _async_detect_tariff(store: AmberStore, site: Site) -> TwoWayTariff | None:
    """The known two-way tariff from this setup's /sites answer; recorded in the Store,
    with the site's start and close dates (the start date bounds the retention)."""
    dates = {
        "active_from": site.active_from.isoformat() if site.active_from else None,
        "closed_on": site.closed_on.isoformat() if site.closed_on else None,
    }
    if dates != store.site:
        _LOGGER.info(
            "Site active from %s%s",
            dates["active_from"] or "(not given)",
            f", closed on {dates['closed_on']}" if dates["closed_on"] else "",
        )
        await store.async_set_site(dates)
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
    """Set up an Amber account and its sites, validating the key with one GET /sites.

    A rejected key, or a key that no longer sees one of the sites, starts reauth.
    Network errors, 5xx, 429 and unexpected responses retry setup later.
    """
    client = AmberClient(async_get_clientsession(hass), entry.data[CONF_API_KEY])
    try:
        sites = {s.id: s for s in await client.async_get_sites()}
    except AmberAuthError as err:
        raise ConfigEntryAuthFailed("Amber rejected the API key") from err
    except (AmberConnectionError, AmberServerError, AmberRateLimitError) as err:
        raise ConfigEntryNotReady(f"Amber API unavailable: {err}") from err
    except AmberError as err:
        raise ConfigEntryNotReady(f"Unexpected response from Amber: {err}") from err
    subentries = site_subentries(entry)
    if any(sub.data[CONF_SITE_ID] not in sites for sub in subentries):
        raise ConfigEntryAuthFailed("The API key no longer has access to one of the sites")

    # Before any platform is set up: each site's device and entities in its sub-entry.
    async_ensure_site_ownership(hass, entry)
    runtime = AmberRuntimeData(client=client, lock=asyncio.Lock())
    entry.runtime_data = runtime
    for sub in subentries:
        runtime.sites[sub.subentry_id] = await _async_setup_site(
            hass, entry, runtime, sub, sites[sub.data[CONF_SITE_ID]], several=len(subentries) > 1
        )
    async_update_titles(hass)
    if runtime.sites:
        _follow_site_renames(hass, entry, runtime.site.device_id)

    async def _started(_hass: HomeAssistant) -> None:
        await async_listen_for_changes(hass)
        for site in list(runtime.sites.values()):
            await site.manager.async_refresh_calibration()
            await async_check_cleanup(hass, site.manager)
            await async_check_energy_issue(hass, site.manager)

    # Re-check the YAML kit's leftovers and the Energy dashboard once Home Assistant has
    # started (so the kit's entities have been set up), and after each scheduled attempt;
    # the Energy dashboard also after every run and whenever its preferences change.
    entry.async_on_unload(async_at_started(hass, _started))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    for site in runtime.sites.values():
        site.manager.async_start()
        entry.async_on_unload(site.manager.async_stop)
        _schedule_startup_work(hass, entry, site.manager)
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    return True


async def _async_setup_site(
    hass: HomeAssistant,
    entry: AmberConfigEntry,
    runtime: AmberRuntimeData,
    sub: ConfigSubentry,
    site: Site,
    *,
    several: bool,
) -> SiteRuntime:
    """One site: its Store, manager and device (created in its sub-entry)."""
    site_id = sub.data[CONF_SITE_ID]
    channels = tuple(ChannelConfig.from_dict(c) for c in sub.data[CONF_CHANNELS])
    key = site_key(entry.entry_id, site_id)
    # The entry's first site keeps the entry's schedule offset (as before sites).
    seed = entry.entry_id if site_id == entry.unique_id else key
    store = AmberStore(hass, key, today=nem_today, seed_source=seed)
    await store.async_load()
    ctx = ImportContext(
        client=runtime.client,
        site_id=site_id,
        channels=channels,
        specs=tuple(build_specs(site_id, channels)),
        lock=runtime.lock,
    )
    tariff = await _async_detect_tariff(store, site)
    options = site_options(sub)
    (schedule, patience_days, revision_days), extra = settings_from_options(options, tariff)
    own = own_sensors_from_entry(entry, channels, site_id)
    for chain in list(store.as_dict()["chains"]):
        if chain.startswith("own:") and chain.split(":")[1] not in entry.subentries:
            await async_forget_chain(store, chain)  # mapping removed; statistics are kept
    manager = AmberManager(
        hass,
        entry,
        ctx,
        store,
        schedule,
        site_key=key,
        site_title=sub.title if several else None,
        active_from=site.active_from,
        patience_days=patience_days,
        revision_days=revision_days,
        own_sensors=own,
        tariff=tariff,
        **extra,
    )
    # The site's device first, in its sub-entry, so mapping devices can link to it.
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        config_subentry_id=sub.subentry_id,
        **device_info(site_id, site_model(site.network, site.nmi), sub.data.get(CONF_SITE_NAME)),
    )
    async_update_convert_issue(hass, entry, manager)
    if sub.data.get(CONF_ADD_TO_ENERGY) and store.energy_setup is None:
        # Chosen in the config flow: carried out once (section 18).
        await async_add_to_energy(hass, manager, "setup")

    async def _check_energy() -> None:
        await async_check_energy_issue(hass, manager)

    async def _check_all() -> None:
        await async_check_cleanup(hass, manager)
        await _check_energy()

    manager.after_scheduled = _check_all
    manager.after_run = _check_energy
    return SiteRuntime(sub.subentry_id, ctx, manager, device.id, options=options)


def _schedule_startup_work(
    hass: HomeAssistant, entry: AmberConfigEntry, manager: AmberManager
) -> None:
    """Once Home Assistant has started: a run, or measuring missing export aggregates."""
    if manager.needs_startup_run:
        # First setup (no marker yet), or the previous run was interrupted: run as soon
        # as Home Assistant has started, rather than waiting for the next schedule slot.
        trigger = "first_setup" if manager.store.marker is None else "resume_after_interruption"

        async def _start_run(_hass: HomeAssistant) -> None:
            entry.async_create_background_task(
                hass, manager.async_run(trigger), f"{DOMAIN} {trigger}"
            )

        entry.async_on_unload(async_at_started(hass, _start_run))
    else:
        # Days lacking export aggregates (the allowance or the bill estimate was just
        # turned on, which reloads the entry): measure them now, not at the next run.

        async def _measure(_hass: HomeAssistant) -> None:
            manager.async_start_export_measurement("setup")

        entry.async_on_unload(async_at_started(hass, _measure))


@callback
def _follow_site_renames(hass: HomeAssistant, entry: AmberConfigEntry, device_id: str) -> None:
    """A name given to the site's device on Home Assistant's device page is the site's
    name (2.2): a default entry title follows it."""

    @callback
    def _renamed(event: Event[dr.EventDeviceRegistryUpdatedData]) -> None:
        changes = event.data.get("changes") or {}
        if "name_by_user" in changes:
            sites = site_subentries(entry)
            old = changes["name_by_user"] or (sites[0].data.get(CONF_SITE_NAME) if sites else None)
            async_update_titles(hass, previous={entry.entry_id: old})

    entry.async_on_unload(async_track_device_registry_updated_event(hass, device_id, _renamed))


@callback
def async_update_titles(
    hass: HomeAssistant, removed: str | None = None, previous: dict[str, str | None] | None = None
) -> None:
    """Give every entry still on a default title the current default (2.1.1: no NMI; with
    several entries the entry's first site's name (2.2), or its NMI's last 4 digits). A
    title the user chose is kept. ``previous`` is a site's name before a rename, by entry."""
    entries = [e for e in hass.config_entries.async_entries(DOMAIN) if e.entry_id != removed]
    for entry in entries:
        sites = site_subentries(entry)
        nmi = sites[0].data.get(CONF_NMI, "") if sites else entry.data.get(CONF_NMI, "")
        name = displayed_site_name(hass, entry)
        title = entry_title(nmi, len(entries) > 1, name)
        old = (previous or {}).get(entry.entry_id, name)
        if entry.title != title and (
            is_default_title(entry.title, nmi, name) or is_default_title(entry.title, nmi, old)
        ):
            hass.config_entries.async_update_entry(entry, title=title)


async def _async_entry_updated(hass: HomeAssistant, entry: AmberConfigEntry) -> None:
    """Apply a site's new settings in place. Reload when sites, mappings (grid meters,
    appliances, 2.1 own sensors) or a site's channels change, or the bill estimate or the
    allowance is turned on or off (they add or remove entities)."""
    runtime = entry.runtime_data
    if runtime.converting:
        return
    subs = {s.subentry_id: s for s in site_subentries(entry)}
    if set(subs) != set(runtime.sites):
        for sub_id in set(runtime.sites) - set(subs):
            await runtime.sites[sub_id].manager.store.async_remove()  # the site was removed
        hass.config_entries.async_schedule_reload(entry.entry_id)
        return
    for sub_id, site in runtime.sites.items():
        sub, manager = subs[sub_id], site.manager
        options = site_options(sub)
        current = {s.subentry_id for s in manager.own_sensors}
        wanted = {
            s.subentry_id
            for s in own_sensors_from_entry(entry, manager.ctx.channels, manager.ctx.site_id)
        }
        channels = tuple(ChannelConfig.from_dict(c) for c in sub.data[CONF_CHANNELS])
        if current != wanted or channels != manager.ctx.channels:
            hass.config_entries.async_schedule_reload(entry.entry_id)
            return
        if options == site.options:
            continue  # a title (or nothing the manager uses) changed
        site.options = options
        args, kwargs = settings_from_options(options, manager.tariff)
        if (manager.bill is None) != (kwargs["bill"] is None) or (manager.allowance is None) != (
            kwargs["allowance"] is None
        ):
            hass.config_entries.async_schedule_reload(entry.entry_id)
            return
        manager.async_update_settings(*args, **kwargs)


async def async_migrate_entry(hass: HomeAssistant, entry: AmberConfigEntry) -> bool:
    """1.1 -> 1.2: fold rc1's network, metering and subscription charges into the daily
    supply charge and the Amber subscription (the bill estimate's figures are unchanged).
    1.x -> 2.1 (2.2.0-dev2): the entry becomes the Amber account, with the site as a
    sub-entry (section 23)."""
    if entry.version > 2:
        return False
    if entry.version == 1:
        if entry.minor_version < 2:
            options = migrate_charges(entry.options)
            if options != dict(entry.options):
                _LOGGER.info("Bill estimate charges migrated to the daily supply charge")
            hass.config_entries.async_update_entry(entry, options=options, minor_version=2)
        try:
            await async_migrate_to_sites(hass, entry)
        except SiteMigrationError, HomeAssistantError:
            _LOGGER.exception("Migration to site sub-entries failed; it is retried at next start")
            return False
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AmberConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: AmberConfigEntry) -> None:
    """Delete the sites' Stores when the entry is removed (statistics are kept)."""
    for sub in site_subentries(entry):
        await AmberStore(hass, site_key(entry.entry_id, sub.data[CONF_SITE_ID])).async_remove()
    await AmberStore(hass, entry.entry_id).async_remove()  # an entry never migrated
    async_update_titles(hass, removed=entry.entry_id)
