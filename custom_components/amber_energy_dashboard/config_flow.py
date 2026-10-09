"""Config flow: API key, site selection, channel confirmation, and reauth."""

from collections.abc import Mapping
from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_API_KEY, CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
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
    CONF_SENSOR,
    CONF_SITE_ID,
    CONF_USAGE_MODE,
    DEFAULT_PATIENCE_DAYS,
    DEFAULT_REVISION_DAYS,
    DOMAIN,
    MODE_FULL,
    ROLE_IMPORT_ENERGY,
    ROLES,
    SCHEDULE_AUTOMATIC,
    SCHEDULE_FIXED,
    SUBENTRY_OWN_SENSOR,
    SUPPORTED_CHANNEL_TYPES,
    USAGE_MODES,
)
from .energy import async_preferences, has_grid
from .export import DEFAULT_PENALTY_PERIOD, TOTALLING_PERIOD, TOTALLINGS
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


_CHANNEL_LABELS = {
    CHANNEL_GENERAL: "general",
    CHANNEL_CONTROLLED_LOAD: "controlled load",
    CHANNEL_FEED_IN: "feed-in",
}


class AmberEnergyDashboardConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Amber Energy Dashboard."""

    VERSION = 1
    MINOR_VERSION = 2
    """1.2 (2.1.0-rc2): the bill estimate's charges as on the Amber bill (daily supply,
    Amber subscription, other)."""

    def __init__(self) -> None:
        """Initialise flow state."""
        self._api_key: str | None = None
        self._sites: dict[str, Site] = {}
        self._site: Site | None = None

    async def _async_fetch_sites(self, api_key: str) -> tuple[list[Site] | None, str | None]:
        """Return (sites, None) or (None, error key)."""
        client = AmberClient(async_get_clientsession(self.hass), api_key)
        try:
            return await client.async_get_sites(), None
        except AmberAuthError:
            return None, "invalid_auth"
        except AmberConnectionError, AmberServerError, AmberRateLimitError:
            return None, "cannot_connect"
        except AmberError:
            _LOGGER.exception("Unexpected response from Amber")
            return None, "unknown"

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
            self._site = site
            return await self.async_step_channels()
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
        channel_list = "\n".join(
            f"- {c.identifier}: {_CHANNEL_LABELS[c.type]}"
            + (f" (tariff {c.tariff})" if c.tariff else "")
            for c in site.channels
        )
        return self.async_show_form(
            step_id="channels",
            data_schema=vol.Schema({}),
            description_placeholders={"nmi": site.nmi, "channels": channel_list},
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
                    title=f"Amber {site.nmi}",
                    data={
                        CONF_API_KEY: self._api_key,
                        CONF_SITE_ID: site.id,
                        CONF_NMI: site.nmi,
                        CONF_CHANNELS: [
                            {"identifier": c.identifier, "type": c.type, "tariff": c.tariff}
                            for c in site.channels
                        ],
                        CONF_ADD_TO_ENERGY: offer_energy
                        and bool(user_input.get(CONF_ADD_TO_ENERGY, True)),
                    },
                    options=options,
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
        """Own-sensor cost mappings are config sub-entries, one per sensor."""
        return {SUBENTRY_OWN_SENSOR: OwnSensorSubentryFlow}

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Refresh the channel list from Amber (for example a new controlled load).

        New channels get statistics from the next day to be imported; statistics of
        channels that disappeared are kept but no longer written.
        """
        entry = self._get_reconfigure_entry()
        sites, error = await self._async_fetch_sites(entry.data[CONF_API_KEY])
        if error:
            return self.async_abort(reason=error)
        site = next((s for s in sites or [] if s.id == entry.unique_id), None)
        if site is None:
            return self.async_abort(reason="site_not_found")
        if any(c.type not in SUPPORTED_CHANNEL_TYPES for c in site.channels) or not site.channels:
            return self.async_abort(reason="unsupported_channels")
        current = {c["identifier"] for c in entry.data[CONF_CHANNELS]}
        found = {c.identifier for c in site.channels}
        added = sorted(found - current)
        removed = sorted(current - found)
        if not added and not removed:
            return self.async_abort(reason="channels_unchanged")
        if user_input is not None:
            if added:
                await _async_record_new_channels(self.hass, entry, added)
            ir.async_delete_issue(self.hass, DOMAIN, f"unexpected_channel_{entry.entry_id}")
            return self.async_update_reload_and_abort(
                entry,
                data_updates={
                    CONF_CHANNELS: [
                        {"identifier": c.identifier, "type": c.type, "tariff": c.tariff}
                        for c in site.channels
                    ]
                },
            )
        labels = {c.identifier: _CHANNEL_LABELS[c.type] for c in site.channels}
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema({}),
            description_placeholders={
                "added": ", ".join(f"{i} ({labels[i]})" for i in added) or "none",
                "removed": ", ".join(removed) or "none",
            },
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start reauth after the key was rejected."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new key; it must still see this entry's site."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            sites, error = await self._async_fetch_sites(api_key)
            if error:
                errors["base"] = error
            elif entry.unique_id not in {s.id for s in sites or []}:
                errors["base"] = "site_not_found"
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_API_KEY: api_key}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_KEY_SCHEMA,
            errors=errors,
            description_placeholders={"nmi": entry.data.get(CONF_NMI, "")},
        )


async def _async_record_new_channels(
    hass: HomeAssistant, entry: ConfigEntry, identifiers: list[str]
) -> None:
    """Record that new channels start at the next day to import (marker + 1)."""
    runtime = getattr(entry, "runtime_data", None)
    if runtime is not None:
        await runtime.manager.async_add_channels(identifiers)
        return
    store = AmberStore(hass, entry.entry_id)
    await store.async_load()
    if store.marker is not None:
        await store.async_set_channel_since(identifiers, store.marker + timedelta(days=1))


class AmberOptionsFlow(OptionsFlow):
    """Import settings (applied without a reload), and the YAML-kit migration."""

    def __init__(self) -> None:
        """Start with no migration picks or report."""
        self._picks: dict[str, str | None] | None = None
        self._exclude_flagged = False
        self._report: dict[str, Any] | None = None
        self._result = ""

    def _manager(self) -> Any:
        runtime = getattr(self.config_entry, "runtime_data", None)
        return runtime.manager if runtime is not None else None

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Menu: settings, migration, and undo once a migration has started."""
        options = ["settings", "bill", "allowance", "migrate"]
        manager = self._manager()
        record = manager.store.migration if manager is not None else None
        if record is not None and record["status"] in (STATUS_IN_PROGRESS, STATUS_COMPLETED):
            options.append("undo_migration")
        return self.async_show_menu(step_id="init", menu_options=options)

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
                current = self.config_entry.options
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
                # The bill estimate and the allowance have their own steps; keep theirs.
                options.update(
                    {k: v for k, v in current.items() if k in _BILL_KEYS | _ALLOWANCE_KEYS}
                )
                return self.async_create_entry(data=options)
        current = dict(self.config_entry.options)
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
        current = dict(self.config_entry.options)
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
                return self.async_create_entry(data=options)
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
        current = dict(self.config_entry.options)
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
                return self.async_create_entry(data=options)
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
        """Close the flow, leaving the options unchanged."""
        return self.async_create_entry(data=dict(self.config_entry.options))


class OwnSensorSubentryFlow(ConfigSubentryFlow):
    """Map a cumulative energy sensor to an Amber channel for own-sensor cost."""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Choose the sensor and the channel whose billed price applies."""
        entry = self._get_entry()
        channels = entry.data[CONF_CHANNELS]
        errors: dict[str, str] = {}
        if user_input is not None:
            entity_id = user_input[CONF_SENSOR]
            state = self.hass.states.get(entity_id)
            attrs = state.attributes if state else {}
            mapped = [
                s for s in entry.subentries.values() if s.subentry_type == SUBENTRY_OWN_SENSOR
            ]
            if any(sub.unique_id == entity_id for sub in mapped):
                return self.async_abort(reason="already_configured")
            if state is None:
                errors[CONF_SENSOR] = "sensor_not_found"
            elif attrs.get("device_class") != "energy" or attrs.get("state_class") not in (
                "total",
                "total_increasing",
            ):
                errors[CONF_SENSOR] = "not_cumulative_energy"
            else:
                channel = next(c for c in channels if c["identifier"] == user_input[CONF_CHANNEL])
                name = attrs.get("friendly_name") or entity_id
                return self.async_create_entry(
                    title=f"{name} ({_CHANNEL_LABELS[channel['type']]} {channel['identifier']})",
                    data={
                        CONF_SENSOR: entity_id,
                        CONF_CHANNEL: channel["identifier"],
                        CONF_NAME: name,  # fixed at creation; names the reconciliation entity
                    },
                    unique_id=entity_id,
                )
        schema = vol.Schema(
            {
                vol.Required(CONF_SENSOR): EntitySelector(
                    EntitySelectorConfig(domain="sensor", device_class="energy")
                ),
                vol.Required(CONF_CHANNEL, default=channels[0]["identifier"]): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(
                                value=c["identifier"],
                                label=f"{c['identifier']} ({_CHANNEL_LABELS[c['type']]})",
                            )
                            for c in channels
                        ],
                        mode=SelectSelectorMode.LIST,
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or {}),
            errors=errors,
        )
