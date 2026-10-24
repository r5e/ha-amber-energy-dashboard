"""Config flow: API key, site selection, channel confirmation, and reauth."""

from collections.abc import Mapping
from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentry,
    ConfigSubentryData,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_API_KEY, CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

from . import async_update_titles
from .api import (
    AmberAuthError,
    AmberClient,
    AmberConnectionError,
    AmberError,
    AmberRateLimitError,
    AmberServerError,
    Site,
)
from .bill import CHARGES, DEFAULT_GST_PERCENT, MAX_BILLING_DAY
from .const import (
    ATTR_ACKNOWLEDGE_UNVERIFIED,
    ATTR_CONFIRM,
    ATTR_CONFIRM_BACKUP,
    ATTR_EXCLUDE_FLAGGED,
    CHANNEL_CONTROLLED_LOAD,
    CHANNEL_FEED_IN,
    CHANNEL_GENERAL,
    CONF_ADD_TO_ENERGY,
    CONF_ALLOWANCE,
    CONF_ALLOWANCE_KWH,
    CONF_ALLOWANCE_TOTALLING,
    CONF_BILLING_DAY,
    CONF_CHANNEL,
    CONF_CHANNELS,
    CONF_EXPORT_CHANNEL,
    CONF_EXPORT_SENSOR,
    CONF_FIXED_STATISTIC,
    CONF_FIXED_TIMES,
    CONF_GST_PERCENT,
    CONF_IMPORT_CHANNEL,
    CONF_IMPORT_SENSOR,
    CONF_NMI,
    CONF_OPTIONS,
    CONF_OWN_FALLBACK,
    CONF_PATIENCE_DAYS,
    CONF_PENALTY_PERIOD,
    CONF_PRICE_SERIES,
    CONF_REVISION_DAYS,
    CONF_SCHEDULE_MODE,
    CONF_SENSOR,
    CONF_SITE,
    CONF_SITE_ID,
    CONF_SITE_NAME,
    CONF_SOLAR_AWARE,
    CONF_USAGE_MODE,
    DEFAULT_PATIENCE_DAYS,
    DEFAULT_REVISION_DAYS,
    DEFAULT_SITE_NAME,
    DOMAIN,
    MODE_FULL,
    ROLE_IMPORT_ENERGY,
    ROLES,
    SCHEDULE_AUTOMATIC,
    SCHEDULE_FIXED,
    SUBENTRY_APPLIANCE,
    SUBENTRY_GRID_METER,
    SUBENTRY_SITE,
    SUPPORTED_CHANNEL_TYPES,
    USAGE_MODES,
)
from .devices import displayed_site_name, entry_title
from .energy import async_preferences, has_grid
from .export import DEFAULT_PENALTY_PERIOD, TOTALLING_PERIOD, TOTALLINGS
from .meters import (
    appliance_title,
    appliance_unique_id,
    grid_meter_title,
    grid_meter_unique_id,
    mapped_entities,
)
from .migration import (
    STATUS_COMPLETED,
    STATUS_IN_PROGRESS,
    MigrationRefused,
    async_migrate,
    async_pick_options,
    async_undo,
    format_report,
    format_result,
)
from .schedule import format_times, parse_times
from .sites import mapping_site_id, site_key, site_options, site_subentries, site_title
from .solar import solar_aware_problem
from .sources import (
    CONF_BATTERY_CHARGE,
    CONF_BATTERY_DISCHARGE,
    CONF_PRICE_FEED_IN,
    CONF_PRICE_GENERAL,
    CONF_SOLAR_ENERGY,
    CONF_SOLAR_POWER,
    CONF_TODAY_METER,
    PRICE_KEYS,
    SOURCE_KEYS,
    SOURCE_NAMES,
    detect_prices,
    price_factor,
    today_meters,
)
from .statistics import CHANNEL_LABELS, ChannelConfig
from .storage import AmberStore

_LOGGER = logging.getLogger(__name__)

_KEY_SCHEMA = vol.Schema(
    {vol.Required(CONF_API_KEY): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))}
)
_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_SCHEDULE_MODE): SelectSelector(
            SelectSelectorConfig(
                options=[SCHEDULE_AUTOMATIC, SCHEDULE_FIXED],
                mode=SelectSelectorMode.LIST,
                translation_key=CONF_SCHEDULE_MODE,
            )
        ),
        vol.Optional(CONF_FIXED_TIMES): TextSelector(),
    }
)


_SITE_NAME_SCHEMA = vol.Schema({vol.Optional(CONF_SITE_NAME): TextSelector()})


def site_details(site: Site) -> dict[str, str]:
    """The site's details for the site-name step: never the full NMI."""
    return {
        "network": site.network or "not given",
        "nmi": f"…{site.nmi[-4:]}",
        "active_from": site.active_from.isoformat() if site.active_from else "not given",
    }


_OPTIONS_SCHEMA = _SCHEDULE_SCHEMA.extend(
    {
        vol.Optional(CONF_USAGE_MODE): SelectSelector(
            SelectSelectorConfig(
                options=list(USAGE_MODES),
                mode=SelectSelectorMode.LIST,
                translation_key=CONF_USAGE_MODE,
            )
        ),
        vol.Optional(CONF_PRICE_SERIES): BooleanSelector(),
        vol.Optional(CONF_OWN_FALLBACK): BooleanSelector(),
        vol.Optional(CONF_PATIENCE_DAYS): NumberSelector(
            NumberSelectorConfig(min=1, max=30, step=1, mode=NumberSelectorMode.BOX)
        ),
        vol.Optional(CONF_REVISION_DAYS): NumberSelector(
            NumberSelectorConfig(min=0, max=60, step=1, mode=NumberSelectorMode.BOX)
        ),
    }
)


def _money() -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=0, max=50, step="any", mode=NumberSelectorMode.BOX, unit_of_measurement="$/day"
        )
    )


BILL_HELP_URL = (
    "https://github.com/r5e/ha-amber-energy-dashboard/blob/main/docs/INSTALL.md"
    "#finding-these-on-your-bill"
)
_BILL_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_BILLING_DAY): NumberSelector(
            NumberSelectorConfig(min=1, max=MAX_BILLING_DAY, step=1, mode=NumberSelectorMode.BOX)
        ),
        **{vol.Optional(name): _money() for name in CHARGES},
        vol.Optional(CONF_GST_PERCENT): NumberSelector(
            NumberSelectorConfig(
                min=0, max=30, step=0.1, mode=NumberSelectorMode.BOX, unit_of_measurement="%"
            )
        ),
        vol.Optional(CONF_FIXED_STATISTIC): BooleanSelector(),
    }
)
_REQUIRED_CHARGES = ("daily_supply", "amber_subscription")
_BILL_KEYS = frozenset({CONF_BILLING_DAY, *CHARGES, CONF_GST_PERCENT, CONF_FIXED_STATISTIC})


_ALLOWANCE_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_ALLOWANCE): BooleanSelector(),
        vol.Optional(CONF_ALLOWANCE_KWH): NumberSelector(
            NumberSelectorConfig(
                min=0, max=100, step="any", mode=NumberSelectorMode.BOX, unit_of_measurement="kWh"
            )
        ),
        vol.Optional(CONF_ALLOWANCE_TOTALLING): SelectSelector(
            SelectSelectorConfig(
                options=list(TOTALLINGS),
                mode=SelectSelectorMode.LIST,
                translation_key=CONF_ALLOWANCE_TOTALLING,
            )
        ),
        vol.Optional(CONF_PENALTY_PERIOD): TextSelector(),
    }
)
_ALLOWANCE_KEYS = frozenset(
    {CONF_ALLOWANCE, CONF_ALLOWANCE_KWH, CONF_ALLOWANCE_TOTALLING, CONF_PENALTY_PERIOD}
)


def _schedule_options(user_input: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Validate the schedule form. Returns (options, error key)."""
    if user_input[CONF_SCHEDULE_MODE] != SCHEDULE_FIXED:
        return {CONF_SCHEDULE_MODE: SCHEDULE_AUTOMATIC}, None
    try:
        times = parse_times(user_input.get(CONF_FIXED_TIMES, ""))
    except ValueError:
        return {}, "invalid_times"
    return {
        CONF_SCHEDULE_MODE: SCHEDULE_FIXED,
        CONF_FIXED_TIMES: [t.strftime("%H:%M") for t in times],
    }, None


class AmberEnergyDashboardConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Amber Energy Dashboard."""

    VERSION = 2
    MINOR_VERSION = 1
    """1.2 (2.1.0-rc2): the bill estimate's charges as on the Amber bill (daily supply,
    Amber subscription, other). 2.1 (2.2.0-dev2): the entry is the Amber account, each
    site a sub-entry (section 23)."""

    def __init__(self) -> None:
        """Initialise flow state."""
        self._api_key: str | None = None
        self._sites: dict[str, Site] = {}
        self._site: Site | None = None
        self._site_name: str | None = None

    async def _async_fetch_sites(self, api_key: str) -> tuple[list[Site] | None, str | None]:
        return await _async_fetch_sites(self.hass, api_key)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the API key and validate it live."""
        errors: dict[str, str] = {}
        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            sites, error = await self._async_fetch_sites(api_key)
            if error:
                errors["base"] = error
            else:
                active = [s for s in sites or [] if s.status == "active"]
                if not active:
                    return self.async_abort(reason="no_active_sites")
                self._api_key = api_key
                self._sites = {s.id: s for s in active}
                return await self.async_step_site()
        return self.async_show_form(step_id="user", data_schema=_KEY_SCHEMA, errors=errors)

    async def async_step_site(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Choose one active site (one config entry per site)."""
        if user_input is not None:
            site = self._sites[user_input[CONF_SITE_ID]]
            await self.async_set_unique_id(site.id)
            self._abort_if_unique_id_configured()
            if site.id in configured_site_ids(self.hass):
                return self.async_abort(reason="already_configured")
            self._site = site
            return await self.async_step_site_name()
        options = [
            SelectOptionDict(
                value=site.id,
                label=f"NMI {site.nmi}" + (f" ({site.network})" if site.network else ""),
            )
            for site in self._sites.values()
        ]
        return self.async_show_form(
            step_id="site",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SITE_ID, default=options[0]["value"]): SelectSelector(
                        SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
                    )
                }
            ),
        )

    async def async_step_site_name(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the site's details and ask for a friendly name for its device (2.2)."""
        site = self._site
        assert site is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            name = str(user_input.get(CONF_SITE_NAME) or "").strip()
            if name:
                self._site_name = name
                return await self.async_step_channels()
            errors[CONF_SITE_NAME] = "name_required"
        return self.async_show_form(
            step_id="site_name",
            data_schema=self.add_suggested_values_to_schema(
                _SITE_NAME_SCHEMA, user_input or {CONF_SITE_NAME: DEFAULT_SITE_NAME}
            ),
            errors=errors,
            description_placeholders=site_details(site),
        )

    async def async_step_channels(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the discovered channels for confirmation."""
        site = self._site
        assert site is not None
        unsupported = [c for c in site.channels if c.type not in SUPPORTED_CHANNEL_TYPES]
        if unsupported or not site.channels:
            return self.async_abort(reason="unsupported_channels")
        if user_input is not None:
            return await self.async_step_schedule()
        return self.async_show_form(
            step_id="channels",
            data_schema=vol.Schema({}),
            description_placeholders=channel_details(site),
        )

    async def async_step_schedule(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose automatic scheduling or fixed times."""
        site = self._site
        assert site is not None
        errors: dict[str, str] = {}
        # Offered only when the Energy dashboard has no grid source (section 18); an
        # existing one is never changed.
        energy = await async_preferences(self.hass)
        offer_energy = energy is not None and not has_grid(energy.data)
        schema = _SCHEDULE_SCHEMA
        if offer_energy:
            schema = schema.extend({vol.Optional(CONF_ADD_TO_ENERGY): BooleanSelector()})
        if user_input is not None:
            options, error = _schedule_options(user_input)
            if error:
                errors[CONF_FIXED_TIMES] = error
            else:
                return self.async_create_entry(
                    title=entry_title(
                        site.nmi, bool(self._async_current_entries()), self._site_name
                    ),
                    data={CONF_API_KEY: self._api_key},
                    subentries=[
                        new_site(
                            site,
                            self._site_name,
                            options,
                            add_to_energy=offer_energy
                            and bool(user_input.get(CONF_ADD_TO_ENERGY, True)),
                        )
                    ],
                )
        return self.async_show_form(
            step_id="schedule",
            data_schema=self.add_suggested_values_to_schema(
                schema,
                user_input or {CONF_SCHEDULE_MODE: SCHEDULE_AUTOMATIC, CONF_ADD_TO_ENERGY: True},
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow (schedule)."""
        return AmberOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Sites (2.2.0-dev2), grid meters and appliances (2.2) are config sub-entries. The
        2.1 own-sensor mappings are still loaded, but no longer offered; a Repairs issue
        converts them."""
        return {
            SUBENTRY_SITE: SiteSubentryFlow,
            SUBENTRY_GRID_METER: GridMeterSubentryFlow,
            SUBENTRY_APPLIANCE: ApplianceSubentryFlow,
        }

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start reauth after the key was rejected."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new key; it must see at least one of this entry's sites (a site it
        doesn't see is skipped, with a Repairs issue, 2.2.0-dev3)."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            sites, error = await self._async_fetch_sites(api_key)
            if error:
                errors["base"] = error
            elif (configured := {s.data[CONF_SITE_ID] for s in site_subentries(entry)}) and not (
                configured & {s.id for s in sites or []}
            ):
                errors["base"] = "site_not_found"
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_API_KEY: api_key}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_KEY_SCHEMA,
            errors=errors,
            description_placeholders={"entry": entry.title},
        )


def configured_site_ids(hass: HomeAssistant) -> set[str]:
    """Every site configured in any entry (a site can be in one entry only)."""
    return {
        s.data[CONF_SITE_ID]
        for entry in hass.config_entries.async_entries(DOMAIN)
        for s in site_subentries(entry)
    }


def channel_details(site: Site) -> dict[str, str]:
    """The site's channels for the confirmation step (never the full NMI)."""
    return {
        "nmi": f"…{site.nmi[-4:]}",
        "channels": "\n".join(
            f"- {c.identifier}: {CHANNEL_LABELS[c.type]}"
            + (f" (tariff {c.tariff})" if c.tariff else "")
            for c in site.channels
        ),
    }


def _channels_data(site: Site) -> list[dict[str, Any]]:
    return [{"identifier": c.identifier, "type": c.type, "tariff": c.tariff} for c in site.channels]


def new_site(
    site: Site, name: str | None, options: dict[str, Any], *, add_to_energy: bool
) -> ConfigSubentryData:
    """A new site sub-entry (section 23)."""
    return ConfigSubentryData(
        data={
            CONF_SITE_ID: site.id,
            CONF_NMI: site.nmi,
            CONF_SITE_NAME: name,
            CONF_CHANNELS: _channels_data(site),
            CONF_ADD_TO_ENERGY: add_to_energy,
            CONF_OPTIONS: options,
        },
        subentry_type=SUBENTRY_SITE,
        title=site_title(name, site.nmi),
        unique_id=site.id,
    )


async def _async_fetch_sites(
    hass: HomeAssistant, api_key: str
) -> tuple[list[Site] | None, str | None]:
    """Return (sites, None) or (None, error key)."""
    client = AmberClient(async_get_clientsession(hass), api_key)
    try:
        return await client.async_get_sites(), None
    except AmberAuthError:
        return None, "invalid_auth"
    except AmberConnectionError, AmberServerError, AmberRateLimitError:
        return None, "cannot_connect"
    except AmberError:
        _LOGGER.exception("Unexpected response from Amber")
        return None, "unknown"


async def _async_record_new_channels(
    hass: HomeAssistant, entry: ConfigEntry, site_id: str, identifiers: list[str]
) -> None:
    """Record that new channels start at the next day to import (marker + 1)."""
    runtime = getattr(entry, "runtime_data", None)
    site = next(
        (s for s in (runtime.sites.values() if runtime else []) if s.context.site_id == site_id),
        None,
    )
    if site is not None:
        await site.manager.async_add_channels(identifiers)
        return
    store = AmberStore(hass, site_key(entry.entry_id, site_id))
    await store.async_load()
    if store.marker is not None:
        await store.async_set_channel_since(identifiers, store.marker + timedelta(days=1))


class _SiteSettings:
    """A site's settings steps (section 23): import settings, bill estimate, export
    allowance, refreshing the channels, and the YAML-kit migration. Shared by the entry's
    options flow (which first asks which site, when there are several) and the site
    sub-entry's reconfigure flow. A step that saves updates the site sub-entry."""

    hass: HomeAssistant
    _sub_id: str | None
    _picks: dict[str, str | None] | None
    _exclude_flagged: bool
    _report: dict[str, Any] | None
    _result: str

    def _init_settings(self) -> None:
        self._sub_id = None
        self._picks = None
        self._exclude_flagged = False
        self._report = None
        self._result = ""

    def _settings_entry(self) -> ConfigEntry:
        raise NotImplementedError  # pragma: no cover - each flow defines it

    def _site(self) -> ConfigSubentry:
        assert self._sub_id is not None
        return self._settings_entry().subentries[self._sub_id]

    def _current(self) -> dict[str, Any]:
        return site_options(self._site())

    def _manager(self) -> Any:
        runtime = getattr(self._settings_entry(), "runtime_data", None)
        site = runtime.sites.get(self._sub_id) if runtime is not None else None
        return site.manager if site is not None else None

    def _save_site(self, data_updates: dict[str, Any], title: str | None = None) -> Any:
        """Update the site sub-entry (and its title, if given) and close the flow."""
        raise NotImplementedError  # pragma: no cover - each flow defines it

    def _close(self) -> Any:
        """Close the flow without a change."""
        raise NotImplementedError  # pragma: no cover - each flow defines it

    def _save_options(self, options: dict[str, Any]) -> Any:
        return self._save_site({CONF_OPTIONS: options})

    def _settings_menu(self, step_id: str) -> Any:
        """Settings, bill estimate, allowance, renaming, channels, migration, and undo
        once a migration has started."""
        options = ["settings", "sources", "bill", "allowance", "rename", "channels", "migrate"]
        manager = self._manager()
        record = manager.store.migration if manager is not None else None
        if record is not None and record["status"] in (STATUS_IN_PROGRESS, STATUS_COMPLETED):
            options.append("undo_migration")
        return self.async_show_menu(step_id=step_id, menu_options=options)  # type: ignore[attr-defined]

    async def async_step_sources(self, user_input: dict[str, Any] | None = None) -> Any:
        """The site-wide sources (section 22): live prices (auto-detected until set; none
        is valid), the grid meter Today so far reads (with several), solar production
        and the battery's charge and discharge."""
        entry, sub = self._settings_entry(), self._site()
        current = self._current()
        types = {c["type"] for c in sub.data[CONF_CHANNELS]}
        prices = [key for channel, key in PRICE_KEYS.items() if channel in types]
        meters = today_meters(entry, sub.data)
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _check_sources(self.hass, user_input)
            if not errors:
                options = {k: v for k, v in current.items() if k not in SOURCE_KEYS}
                options.update({key: user_input.get(key) or None for key in prices})
                for key in _OPTIONAL_SOURCES:
                    if user_input.get(key):
                        options[key] = user_input[key]
                if len(meters) > 1 and user_input.get(CONF_TODAY_METER):
                    options[CONF_TODAY_METER] = user_input[CONF_TODAY_METER]
                return self._save_options(options)
        detected = detect_prices(self.hass, sub.data[CONF_SITE_ID])
        fields: dict[Any, Any] = {vol.Optional(key): _price_selector() for key in prices}
        if len(meters) > 1:
            fields[vol.Optional(CONF_TODAY_METER)] = SelectSelector(
                SelectSelectorConfig(
                    options=[SelectOptionDict(value=m.subentry_id, label=m.title) for m in meters],
                    mode=SelectSelectorMode.LIST,
                )
            )
        fields[vol.Optional(CONF_SOLAR_POWER)] = EntitySelector(
            EntitySelectorConfig(domain="sensor", device_class="power")
        )
        for key in (CONF_SOLAR_ENERGY, CONF_BATTERY_CHARGE, CONF_BATTERY_DISCHARGE):
            fields[vol.Optional(key)] = _energy_selector()
        suggested = {
            **{
                key: current[key]
                if key in current
                else getattr(detected.get(channel), "entity_id", None)
                for channel, key in PRICE_KEYS.items()
                if key in prices
            },
            **{k: v for k, v in current.items() if k in SOURCE_KEYS - set(PRICE_KEYS.values())},
            **(user_input or {}),
        }
        if len(meters) > 1:
            suggested.setdefault(CONF_TODAY_METER, meters[0].subentry_id)
        found = [
            f"- {label}: {detected[channel].entity_id} ({SOURCE_NAMES[detected[channel].origin]})"
            for channel, label in ((CHANNEL_GENERAL, "General"), (CHANNEL_FEED_IN, "Feed-in"))
            if channel in detected and channel in types
        ]
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="sources",
            data_schema=self.add_suggested_values_to_schema(  # type: ignore[attr-defined]
                vol.Schema(fields), suggested
            ),
            errors=errors,
            description_placeholders={
                "detected": "\n".join(found)
                or "- none (neither Amber Express nor Amber Electric is set up for this site)",
                "meter": meters[0].title
                if len(meters) == 1
                else ("choose one below" if meters else "none: add a grid meter"),
            },
        )

    async def async_step_rename(self, user_input: dict[str, Any] | None = None) -> Any:
        """Rename the site (2.2.0-dev3): its sub-entry's title and its device's name. A
        name given to the device on Home Assistant's device page is replaced, so the new
        name shows everywhere. Entity IDs don't change."""
        entry, sub = self._settings_entry(), self._site()
        errors: dict[str, str] = {}
        if user_input is not None:
            name = str(user_input.get(CONF_SITE_NAME) or "").strip()
            if name:
                previous = displayed_site_name(self.hass, entry)
                devices = dr.async_get(self.hass)
                device = devices.async_get_device_by_identifier(
                    (DOMAIN, sub.data[CONF_SITE_ID]), entry.entry_id
                )
                if device is not None:
                    devices.async_update_device(device.id, name=name, name_by_user=None)
                result = self._save_site({CONF_SITE_NAME: name}, title=name)
                async_update_titles(self.hass, previous={entry.entry_id: previous})
                return result
            errors[CONF_SITE_NAME] = "name_required"
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="rename",
            data_schema=self.add_suggested_values_to_schema(  # type: ignore[attr-defined]
                _SITE_NAME_SCHEMA, user_input or {CONF_SITE_NAME: sub.title}
            ),
            errors=errors,
        )

    async def async_step_channels(self, user_input: dict[str, Any] | None = None) -> Any:
        """Refresh the site's channels from Amber (for example a new controlled load).

        New channels get statistics from the next day to be imported; statistics of
        channels that disappeared are kept but no longer written.
        """
        entry, sub = self._settings_entry(), self._site()
        site_id = sub.data[CONF_SITE_ID]
        sites, error = await _async_fetch_sites(self.hass, entry.data[CONF_API_KEY])
        if error:
            return self.async_abort(reason=error)  # type: ignore[attr-defined]
        site = next((s for s in sites or [] if s.id == site_id), None)
        if site is None:
            return self.async_abort(reason="site_not_found")  # type: ignore[attr-defined]
        if any(c.type not in SUPPORTED_CHANNEL_TYPES for c in site.channels) or not site.channels:
            return self.async_abort(reason="unsupported_channels")  # type: ignore[attr-defined]
        current = {c["identifier"] for c in sub.data[CONF_CHANNELS]}
        found = {c.identifier for c in site.channels}
        added = sorted(found - current)
        removed = sorted(current - found)
        if not added and not removed:
            return self.async_abort(reason="channels_unchanged")  # type: ignore[attr-defined]
        if user_input is not None:
            if added:
                await _async_record_new_channels(self.hass, entry, site_id, added)
            ir.async_delete_issue(
                self.hass, DOMAIN, f"unexpected_channel_{site_key(entry.entry_id, site_id)}"
            )
            return self._save_site({CONF_CHANNELS: _channels_data(site)})
        labels = {c.identifier: CHANNEL_LABELS[c.type] for c in site.channels}
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="channels",
            data_schema=vol.Schema({}),
            description_placeholders={
                "added": ", ".join(f"{i} ({labels[i]})" for i in added) or "none",
                "removed": ", ".join(removed) or "none",
            },
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and validate the settings form."""
        errors: dict[str, str] = {}
        if user_input is not None:
            options, error = _schedule_options(user_input)
            if error:
                errors[CONF_FIXED_TIMES] = error
            else:
                current = self._current()
                options[CONF_PATIENCE_DAYS] = int(
                    user_input.get(
                        CONF_PATIENCE_DAYS, current.get(CONF_PATIENCE_DAYS, DEFAULT_PATIENCE_DAYS)
                    )
                )
                options[CONF_REVISION_DAYS] = int(
                    user_input.get(
                        CONF_REVISION_DAYS, current.get(CONF_REVISION_DAYS, DEFAULT_REVISION_DAYS)
                    )
                )
                options[CONF_USAGE_MODE] = user_input.get(
                    CONF_USAGE_MODE, current.get(CONF_USAGE_MODE, MODE_FULL)
                )
                options[CONF_PRICE_SERIES] = bool(
                    user_input.get(CONF_PRICE_SERIES, current.get(CONF_PRICE_SERIES, False))
                )
                options[CONF_OWN_FALLBACK] = bool(
                    user_input.get(CONF_OWN_FALLBACK, current.get(CONF_OWN_FALLBACK, True))
                )
                # The bill estimate, the allowance and the sources have their own steps.
                options.update(
                    {
                        k: v
                        for k, v in current.items()
                        if k in _BILL_KEYS | _ALLOWANCE_KEYS | SOURCE_KEYS
                    }
                )
                return self._save_options(options)
        current = self._current()
        suggested = user_input or {
            CONF_SCHEDULE_MODE: current.get(CONF_SCHEDULE_MODE, SCHEDULE_AUTOMATIC),
            CONF_FIXED_TIMES: format_times(parse_times(current[CONF_FIXED_TIMES]))
            if current.get(CONF_FIXED_TIMES)
            else "",
            CONF_PATIENCE_DAYS: current.get(CONF_PATIENCE_DAYS, DEFAULT_PATIENCE_DAYS),
            CONF_REVISION_DAYS: current.get(CONF_REVISION_DAYS, DEFAULT_REVISION_DAYS),
            CONF_USAGE_MODE: current.get(CONF_USAGE_MODE, MODE_FULL),
            CONF_PRICE_SERIES: current.get(CONF_PRICE_SERIES, False),
            CONF_OWN_FALLBACK: current.get(CONF_OWN_FALLBACK, True),
        }
        return self.async_show_form(
            step_id="settings",
            data_schema=self.add_suggested_values_to_schema(_OPTIONS_SCHEMA, suggested),
            errors=errors,
        )

    async def async_step_bill(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The bill estimate: billing day, daily fixed charges (ex GST), GST, statistic."""
        current = self._current()
        errors: dict[str, str] = {}
        if user_input is not None:
            # The daily supply charge and the subscription have no default: with a
            # billing day they must be entered (0 is valid, for a free subscription).
            if user_input.get(CONF_BILLING_DAY):
                errors = {
                    name: "charge_required"
                    for name in _REQUIRED_CHARGES
                    if user_input.get(name) is None
                }
            if not errors:
                options = {k: v for k, v in current.items() if k not in _BILL_KEYS}
                if user_input.get(CONF_BILLING_DAY):
                    options[CONF_BILLING_DAY] = int(user_input[CONF_BILLING_DAY])
                    options.update({name: float(user_input.get(name) or 0.0) for name in CHARGES})
                    options[CONF_GST_PERCENT] = float(
                        user_input.get(CONF_GST_PERCENT, DEFAULT_GST_PERCENT)
                    )
                    options[CONF_FIXED_STATISTIC] = bool(user_input.get(CONF_FIXED_STATISTIC))
                return self._save_options(options)
        suggested = {
            "other_daily": 0.0,
            CONF_GST_PERCENT: DEFAULT_GST_PERCENT,
            **{k: v for k, v in current.items() if k in _BILL_KEYS},
            **(user_input or {}),
        }
        return self.async_show_form(
            step_id="bill",
            data_schema=self.add_suggested_values_to_schema(_BILL_SCHEMA, suggested),
            errors=errors,
            description_placeholders={"bill_help_url": BILL_HELP_URL},
        )

    async def async_step_allowance(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The free export allowance of a two-way network tariff (section 20)."""
        current = self._current()
        manager = self._manager()
        tariff = manager.tariff if manager is not None else None
        errors: dict[str, str] = {}
        if user_input is not None:
            enabled = bool(user_input.get(CONF_ALLOWANCE))
            totalling = user_input.get(CONF_ALLOWANCE_TOTALLING) or TOTALLING_PERIOD
            per_day = user_input.get(CONF_ALLOWANCE_KWH)
            if enabled and totalling == TOTALLING_PERIOD and not current.get(CONF_BILLING_DAY):
                errors[CONF_ALLOWANCE_TOTALLING] = "needs_billing_day"
            elif enabled and not per_day:
                errors[CONF_ALLOWANCE_KWH] = "allowance_required"
            else:
                options = {k: v for k, v in current.items() if k not in _ALLOWANCE_KEYS}
                options[CONF_ALLOWANCE] = enabled
                if enabled:
                    options[CONF_ALLOWANCE_KWH] = float(per_day)
                    options[CONF_ALLOWANCE_TOTALLING] = totalling
                    options[CONF_PENALTY_PERIOD] = (
                        user_input.get(CONF_PENALTY_PERIOD) or DEFAULT_PENALTY_PERIOD
                    )
                return self._save_options(options)
        defaults: dict[str, Any] = {
            CONF_ALLOWANCE: tariff is not None and bool(current.get(CONF_BILLING_DAY)),
            CONF_ALLOWANCE_TOTALLING: tariff.totalling if tariff else TOTALLING_PERIOD,
            CONF_PENALTY_PERIOD: tariff.penalty_period if tariff else DEFAULT_PENALTY_PERIOD,
        }
        if tariff is not None:
            defaults[CONF_ALLOWANCE_KWH] = tariff.allowance_kwh_per_day
        suggested = {
            **defaults,
            **{k: v for k, v in current.items() if k in _ALLOWANCE_KEYS},
            **(user_input or {}),
        }
        detected = (
            f"Detected {tariff.network} {tariff.tariff}: {tariff.allowance_kwh_per_day:g} kWh "
            f"a day free, exports in {tariff.penalty_period} charged beyond it."
            if tariff
            else "No known two-way tariff was detected for this site; set the allowance "
            "yourself if your network has one."
        )
        return self.async_show_form(
            step_id="allowance",
            data_schema=self.add_suggested_values_to_schema(_ALLOWANCE_SCHEMA, suggested),
            errors=errors,
            description_placeholders={
                "detected": detected,
                "billing_day": str(current.get(CONF_BILLING_DAY) or "not set"),
            },
        )

    async def async_step_migrate(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Detect the YAML kit and show the dry run (or ask for the statistics)."""
        manager = self._manager()
        if manager is None:
            return self.async_abort(reason="not_loaded")
        report = await async_migrate(self.hass, manager, dry_run=True)
        self._report = report
        if report["needs_manual_pick"]:
            return await self.async_step_migrate_pick()
        return await self.async_step_migrate_confirm()

    async def async_step_migrate_pick(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manual step: the user picks the legacy statistics."""
        manager = self._manager()
        errors: dict[str, str] = {}
        placeholders = {"error": ""}
        if user_input is not None:
            self._picks = {role: user_input.get(role) for role in ROLES}
            try:
                self._report = await async_migrate(
                    self.hass, manager, dry_run=True, picks=self._picks
                )
            except MigrationRefused as err:
                errors["base"] = "invalid_pick"
                placeholders["error"] = str(err)
            else:
                return await self.async_step_migrate_confirm()
        energy, cost = await async_pick_options(self.hass)
        detection = (self._report or {}).get("detection") or {}
        reason = detection.get("reason") or "The YAML kit was not recognised automatically."

        def select(values: list[str]) -> SelectSelector:
            return SelectSelector(
                SelectSelectorConfig(options=values, mode=SelectSelectorMode.DROPDOWN)
            )

        schema = vol.Schema(
            {
                vol.Required(ROLE_IMPORT_ENERGY): select(energy),
                **{
                    vol.Optional(role): select(cost if "cost" in role else energy)
                    for role in ROLES
                    if role != ROLE_IMPORT_ENERGY
                },
            }
        )
        return self.async_show_form(
            step_id="migrate_pick",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or {}),
            errors=errors,
            description_placeholders={"reason": reason, **placeholders},
        )

    async def async_step_migrate_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the dry run; on confirmation (with the backup box ticked), run it."""
        report = self._report or {}
        parity = report.get("parity") or {}
        flagged = report.get("flagged") or {}
        unverified = bool(parity.get("unverified"))
        blocked = report.get("problems") or not (parity.get("passed") or unverified)
        if report.get("migration_status") == STATUS_COMPLETED:
            return self._show_result(
                "The migration was already completed. Use Undo migration to run it again."
            )
        if flagged.get("blocking") and len(report.get("problems") or []) == 1:
            return await self.async_step_migrate_flagged()
        if blocked:
            return self._show_result(
                "The migration cannot run; nothing was changed.\n\n" + format_report(report)
            )
        errors: dict[str, str] = {}
        if user_input is not None:
            if unverified and not user_input.get(ATTR_ACKNOWLEDGE_UNVERIFIED):
                errors["base"] = "not_acknowledged"
            elif not user_input.get(ATTR_CONFIRM_BACKUP):
                errors["base"] = "backup_not_confirmed"
            else:
                try:
                    result = await async_migrate(
                        self.hass,
                        self._manager(),
                        dry_run=False,
                        confirm_backup=True,
                        picks=self._picks,
                        exclude_flagged=self._exclude_flagged,
                        acknowledge_unverified=unverified,
                    )
                except MigrationRefused as err:
                    return self._show_result(f"The migration did not run: {err}")
                return self._show_result(format_result(result))
        fields: dict[Any, Any] = {}
        if unverified:
            fields[vol.Required(ATTR_ACKNOWLEDGE_UNVERIFIED, default=False)] = BooleanSelector()
        fields[vol.Required(ATTR_CONFIRM_BACKUP, default=False)] = BooleanSelector()
        return self.async_show_form(
            step_id="migrate_confirm",
            data_schema=vol.Schema(fields),
            errors=errors,
            description_placeholders={"report": format_report(report)},
        )

    async def async_step_migrate_flagged(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Implausible legacy rows: stop, or dry-run again with them left out."""
        report = self._report or {}
        if user_input is not None:
            if not user_input.get(ATTR_EXCLUDE_FLAGGED):
                return self._show_result(
                    "The migration did not run; nothing was changed.\n\n" + format_report(report)
                )
            self._exclude_flagged = True
            self._report = await async_migrate(
                self.hass, self._manager(), dry_run=True, picks=self._picks, exclude_flagged=True
            )
            return await self.async_step_migrate_confirm()
        return self.async_show_form(
            step_id="migrate_flagged",
            data_schema=vol.Schema(
                {vol.Required(ATTR_EXCLUDE_FLAGGED, default=False): BooleanSelector()}
            ),
            description_placeholders={"report": format_report(report)},
        )

    async def async_step_undo_migration(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm, then undo the migration."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get(ATTR_CONFIRM):
                errors["base"] = "not_confirmed"
            else:
                try:
                    result = await async_undo(self.hass, self._manager())
                except MigrationRefused as err:
                    return self._show_result(f"Undo did not run: {err}")
                return self._show_result(
                    f"Energy dashboard restored: {result['energy_restored']}. "
                    f"Automations turned back on: {result['automations_enabled'] or 'none'}. "
                    f"Left as they were: {result['left_unchanged'] or 'none'}. {result['kept']}"
                )
        return self.async_show_form(
            step_id="undo_migration",
            data_schema=vol.Schema({vol.Required(ATTR_CONFIRM, default=False): BooleanSelector()}),
            errors=errors,
        )

    def _show_result(self, text: str) -> ConfigFlowResult:
        self._result = text
        return self.async_show_form(
            step_id="migrate_result",
            data_schema=vol.Schema({}),
            description_placeholders={"result": text},
        )

    async def async_step_migrate_result(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Close the flow, leaving the settings unchanged."""
        return self._close()


class AmberOptionsFlow(_SiteSettings, OptionsFlow):
    """The entry's options (the cog): a site's settings, after asking which site when the
    account has several (section 23). The entry's own options are account-wide; there
    are none yet."""

    def __init__(self) -> None:
        """Start with no site chosen, and no migration picks or report."""
        self._init_settings()

    def _settings_entry(self) -> ConfigEntry:
        return self.config_entry

    def _save_site(
        self, data_updates: dict[str, Any], title: str | None = None
    ) -> ConfigFlowResult:
        sub = self._site()
        self.hass.config_entries.async_update_subentry(
            self.config_entry, sub, data={**sub.data, **data_updates}, title=title or sub.title
        )
        return self.async_create_entry(data=dict(self.config_entry.options))

    def _close(self) -> ConfigFlowResult:
        return self.async_create_entry(data=dict(self.config_entry.options))

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Which site, when there are several; then its settings menu."""
        sites = site_subentries(self.config_entry)
        if not sites:
            return self.async_abort(reason="no_sites")
        if len(sites) == 1:
            self._sub_id = sites[0].subentry_id
            return self._settings_menu("init")
        return await self.async_step_site()

    async def async_step_site(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Choose the site whose settings to change."""
        if user_input is not None:
            self._sub_id = user_input[CONF_SITE]
            return self._settings_menu("init")
        sites = site_subentries(self.config_entry)
        return self.async_show_form(
            step_id="site",
            data_schema=vol.Schema(
                {vol.Required(CONF_SITE, default=sites[0].subentry_id): _site_selector(sites)}
            ),
        )


class SiteSubentryFlow(_SiteSettings, ConfigSubentryFlow):
    """ "+ Site": add a site the account's key can see (its name, its channels); and a
    site's settings (reconfigure, the pencil on the site), as in the entry's options."""

    def __init__(self) -> None:
        """Start with no site chosen."""
        self._init_settings()
        self._sites: dict[str, Site] = {}
        self._new: Site | None = None
        self._name: str | None = None

    def _settings_entry(self) -> ConfigEntry:
        return self._get_entry()

    def _save_site(
        self, data_updates: dict[str, Any], title: str | None = None
    ) -> SubentryFlowResult:
        sub = self._site()
        return self.async_update_and_abort(
            self._get_entry(), sub, data_updates=data_updates, title=title or sub.title
        )

    def _close(self) -> SubentryFlowResult:
        return self.async_abort(reason="settings_closed")

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """The site's settings menu."""
        self._sub_id = self._get_reconfigure_subentry().subentry_id
        return self._settings_menu("reconfigure")

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Choose a site the key can see that isn't set up yet."""
        if user_input is not None:
            self._new = self._sites[user_input[CONF_SITE_ID]]
            return await self.async_step_site_name()
        sites, error = await _async_fetch_sites(self.hass, self._get_entry().data[CONF_API_KEY])
        if error:
            return self.async_abort(reason=error)
        configured = configured_site_ids(self.hass)
        self._sites = {
            s.id: s for s in sites or [] if s.status == "active" and s.id not in configured
        }
        if not self._sites:
            return self.async_abort(reason="no_new_sites")
        options = [
            SelectOptionDict(
                value=site.id,
                label=f"NMI …{site.nmi[-4:]}" + (f" ({site.network})" if site.network else ""),
            )
            for site in self._sites.values()
        ]
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SITE_ID, default=options[0]["value"]): SelectSelector(
                        SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
                    )
                }
            ),
        )

    async def async_step_site_name(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """The site's details, and a name for its device."""
        site = self._new
        assert site is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            name = str(user_input.get(CONF_SITE_NAME) or "").strip()
            if name:
                self._name = name
                return await self.async_step_new_channels()
            errors[CONF_SITE_NAME] = "name_required"
        return self.async_show_form(
            step_id="site_name",
            data_schema=self.add_suggested_values_to_schema(_SITE_NAME_SCHEMA, user_input or {}),
            errors=errors,
            description_placeholders=site_details(site),
        )

    async def async_step_new_channels(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Confirm the site's channels; the site is added with automatic scheduling."""
        site = self._new
        assert site is not None
        if any(c.type not in SUPPORTED_CHANNEL_TYPES for c in site.channels) or not site.channels:
            return self.async_abort(reason="unsupported_channels")
        if user_input is not None:
            sub = new_site(
                site, self._name, {CONF_SCHEDULE_MODE: SCHEDULE_AUTOMATIC}, add_to_energy=False
            )
            return self.async_create_entry(
                title=sub["title"], data=sub["data"], unique_id=sub["unique_id"]
            )
        return self.async_show_form(
            step_id="new_channels",
            data_schema=vol.Schema({}),
            description_placeholders=channel_details(site),
        )


def _site_selector(sites: list[ConfigSubentry]) -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=[SelectOptionDict(value=s.subentry_id, label=s.title) for s in sites],
            mode=SelectSelectorMode.LIST,
        )
    )


def _energy_sensor_error(hass: HomeAssistant, entity_id: str) -> str | None:
    """Why an entity can't be priced: missing, or not a cumulative energy sensor."""
    state = hass.states.get(entity_id)
    if state is None:
        return "sensor_not_found"
    attrs = state.attributes
    if attrs.get("device_class") != "energy" or attrs.get("state_class") not in (
        "total",
        "total_increasing",
    ):
        return "not_cumulative_energy"
    return None


def _check_sensors(
    hass: HomeAssistant, entry: ConfigEntry, sensors: dict[str, str | None]
) -> dict[str, str]:
    """Errors by field: each sensor must be a cumulative energy sensor that no other
    mapping uses (its cost statistic's ID comes from its entity ID)."""
    used = mapped_entities(entry)
    errors: dict[str, str] = {}
    seen: set[str] = set()
    for key, entity_id in sensors.items():
        if not entity_id:
            continue
        if entity_id in used:
            errors[key] = "sensor_in_use"
        elif entity_id in seen:
            errors[key] = "same_sensor"
        elif error := _energy_sensor_error(hass, entity_id):
            errors[key] = error
        seen.add(entity_id)
    return errors


_OPTIONAL_SOURCES = (
    CONF_SOLAR_POWER,
    CONF_SOLAR_ENERGY,
    CONF_BATTERY_CHARGE,
    CONF_BATTERY_DISCHARGE,
)


def _price_selector() -> EntitySelector:
    return EntitySelector(EntitySelectorConfig(domain="sensor"))


def _check_sources(hass: HomeAssistant, user_input: dict[str, Any]) -> dict[str, str]:
    """Errors by field: a price must be in $/kWh, c/kWh or $/MWh; solar power a power
    sensor; the energy sensors cumulative; and no sensor used twice."""
    errors: dict[str, str] = {}
    seen: set[str] = set()
    for key in (CONF_PRICE_GENERAL, CONF_PRICE_FEED_IN, *_OPTIONAL_SOURCES):
        entity_id = user_input.get(key)
        if not entity_id:
            continue
        state = hass.states.get(entity_id)
        if entity_id in seen:
            errors[key] = "same_sensor"
        elif state is None:
            errors[key] = "sensor_not_found"
        elif key in (CONF_PRICE_GENERAL, CONF_PRICE_FEED_IN):
            if price_factor(state.attributes.get("unit_of_measurement")) is None:
                errors[key] = "price_unit"
        elif key == CONF_SOLAR_POWER:
            if state.attributes.get("device_class") != "power":
                errors[key] = "not_power"
        elif error := _energy_sensor_error(hass, entity_id):
            errors[key] = error
        seen.add(entity_id)
    return errors


def _energy_selector() -> EntitySelector:
    return EntitySelector(EntitySelectorConfig(domain="sensor", device_class="energy"))


def _channel_selector(channels: list[ChannelConfig]) -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=[
                SelectOptionDict(
                    value=c.identifier, label=f"{c.identifier} ({CHANNEL_LABELS[c.type]})"
                )
                for c in channels
            ],
            mode=SelectSelectorMode.LIST,
        )
    )


def _import_channels(site: Mapping[str, Any]) -> list[ChannelConfig]:
    """The channels an import is priced on: general first, then controlled load."""
    channels = [ChannelConfig.from_dict(c) for c in site[CONF_CHANNELS]]
    return [c for c in channels if c.type == CHANNEL_GENERAL] + [
        c for c in channels if c.type == CHANNEL_CONTROLLED_LOAD
    ]


class _MappingFlow(ConfigSubentryFlow):
    """A grid meter or appliance belongs to one site: ask which, when there are several
    (section 23)."""

    _site_data: Mapping[str, Any] | None = None

    def _pick_site(self) -> bool:
        """True when the user must choose the site first."""
        if self._site_data is None:
            sites = site_subentries(self._get_entry())
            if len(sites) > 1:
                return True
            if sites:
                self._site_data = sites[0].data
        return False

    async def async_step_site(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Choose the site."""
        sites = site_subentries(self._get_entry())
        if user_input is not None:
            self._site_data = self._get_entry().subentries[user_input[CONF_SITE]].data
            return await self.async_step_user()
        return self.async_show_form(
            step_id="site",
            data_schema=vol.Schema(
                {vol.Required(CONF_SITE, default=sites[0].subentry_id): _site_selector(sites)}
            ),
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """The form, once the site is known."""
        if self._pick_site():
            return await self.async_step_site()
        if self._site_data is None:
            return self.async_abort(reason="no_sites")
        return await self._async_form(self._site_data, user_input)

    async def _async_form(
        self, site: Mapping[str, Any], user_input: dict[str, Any] | None
    ) -> SubentryFlowResult:
        raise NotImplementedError  # pragma: no cover - each flow defines it


class GridMeterSubentryFlow(_MappingFlow):
    """A meter on the main supply: import priced on the general (or controlled load)
    channel, and optionally export priced on the feed-in channel (section 21)."""

    async def _async_form(
        self, site: Mapping[str, Any], user_input: dict[str, Any] | None
    ) -> SubentryFlowResult:
        """Name the meter and choose its sensors."""
        entry = self._get_entry()
        imports = _import_channels(site)
        if not imports:
            return self.async_abort(reason="no_import_channel")
        feed_in = next(
            (
                ChannelConfig.from_dict(c)
                for c in site[CONF_CHANNELS]
                if c["type"] == CHANNEL_FEED_IN
            ),
            None,
        )
        errors: dict[str, str] = {}
        if user_input is not None:
            name = user_input[CONF_NAME].strip()
            import_id = user_input[CONF_IMPORT_SENSOR]
            export_id = user_input.get(CONF_EXPORT_SENSOR) or None
            errors = _check_sensors(
                self.hass, entry, {CONF_IMPORT_SENSOR: import_id, CONF_EXPORT_SENSOR: export_id}
            )
            if not name:
                errors[CONF_NAME] = "name_required"
            if not errors:
                data = {
                    CONF_SITE_ID: site[CONF_SITE_ID],
                    CONF_NAME: name,
                    CONF_IMPORT_SENSOR: import_id,
                    CONF_IMPORT_CHANNEL: user_input.get(CONF_IMPORT_CHANNEL)
                    or imports[0].identifier,
                }
                if export_id and feed_in is not None:
                    data[CONF_EXPORT_SENSOR] = export_id
                    data[CONF_EXPORT_CHANNEL] = feed_in.identifier
                return self.async_create_entry(
                    title=grid_meter_title(name),
                    data=data,
                    unique_id=grid_meter_unique_id(import_id),
                )
        fields: dict[Any, Any] = {
            vol.Required(CONF_NAME): TextSelector(),
            vol.Required(CONF_IMPORT_SENSOR): _energy_selector(),
        }
        if len(imports) > 1:
            fields[vol.Required(CONF_IMPORT_CHANNEL, default=imports[0].identifier)] = (
                _channel_selector(imports)
            )
        if feed_in is not None:
            fields[vol.Optional(CONF_EXPORT_SENSOR)] = _energy_selector()
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(fields), user_input or {CONF_NAME: "Whole house meter"}
            ),
            errors=errors,
        )


class ApplianceSubentryFlow(_MappingFlow):
    """Any single energy sensor, priced on the general (or controlled load) channel,
    without reconciliation (section 21)."""

    async def _async_form(
        self, site: Mapping[str, Any], user_input: dict[str, Any] | None
    ) -> SubentryFlowResult:
        """Name the appliance, choose its sensor and the channel it is priced on."""
        entry = self._get_entry()
        imports = _import_channels(site)
        if not imports:
            return self.async_abort(reason="no_import_channel")
        errors: dict[str, str] = {}
        if user_input is not None:
            name = user_input[CONF_NAME].strip()
            entity_id = user_input[CONF_SENSOR]
            errors = _check_sensors(self.hass, entry, {CONF_SENSOR: entity_id})
            if not name:
                errors[CONF_NAME] = "name_required"
            ident = user_input.get(CONF_CHANNEL) or imports[0].identifier
            channel = next(c for c in imports if c.identifier == ident)
            solar_aware = bool(user_input.get(CONF_SOLAR_AWARE))
            if solar_aware and (problem := solar_aware_problem(entry, site, channel.type)):
                errors[CONF_SOLAR_AWARE] = problem
            if not errors:
                return self.async_create_entry(
                    title=appliance_title(name, channel),
                    data={
                        CONF_SITE_ID: site[CONF_SITE_ID],
                        CONF_NAME: name,
                        CONF_SENSOR: entity_id,
                        CONF_CHANNEL: ident,
                        CONF_SOLAR_AWARE: solar_aware,
                    },
                    unique_id=appliance_unique_id(entity_id),
                )
        fields: dict[Any, Any] = {
            vol.Required(CONF_NAME): TextSelector(),
            vol.Required(CONF_SENSOR): _energy_selector(),
        }
        if len(imports) > 1:
            fields[vol.Required(CONF_CHANNEL, default=imports[0].identifier)] = _channel_selector(
                imports
            )
        fields[vol.Optional(CONF_SOLAR_AWARE, default=False)] = BooleanSelector()
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(vol.Schema(fields), user_input or {}),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Turn the appliance's solar-aware cost on or off (2.2.0-dev4)."""
        entry, subentry = self._get_entry(), self._get_reconfigure_subentry()
        site_id = mapping_site_id(entry, subentry.data)
        site = next((s for s in site_subentries(entry) if s.data[CONF_SITE_ID] == site_id), None)
        channel = next(
            (
                c
                for c in (site.data[CONF_CHANNELS] if site is not None else [])
                if c["identifier"] == subentry.data[CONF_CHANNEL]
            ),
            None,
        )
        if site is None or channel is None:
            return self.async_abort(reason="appliance_site_missing")
        errors: dict[str, str] = {}
        if user_input is not None:
            solar_aware = bool(user_input.get(CONF_SOLAR_AWARE))
            if solar_aware and (problem := solar_aware_problem(entry, site.data, channel["type"])):
                errors[CONF_SOLAR_AWARE] = problem
            else:
                return self.async_update_and_abort(
                    entry, subentry, data_updates={CONF_SOLAR_AWARE: solar_aware}
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema({vol.Optional(CONF_SOLAR_AWARE): BooleanSelector()}),
                {CONF_SOLAR_AWARE: bool(subentry.data.get(CONF_SOLAR_AWARE))},
            ),
            errors=errors,
            description_placeholders={"name": subentry.data.get(CONF_NAME, "")},
        )
