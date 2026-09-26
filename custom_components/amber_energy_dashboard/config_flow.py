"""Config flow: API key, site selection, channel confirmation, and reauth."""

from collections.abc import Mapping
import logging
from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_API_KEY
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
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
from .const import (
    CHANNEL_CONTROLLED_LOAD,
    CHANNEL_FEED_IN,
    CHANNEL_GENERAL,
    CONF_CHANNELS,
    CONF_NMI,
    CONF_SITE_ID,
    DOMAIN,
    SUPPORTED_CHANNEL_TYPES,
)

_LOGGER = logging.getLogger(__name__)

_KEY_SCHEMA = vol.Schema(
    {vol.Required(CONF_API_KEY): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))}
)
_CHANNEL_LABELS = {
    CHANNEL_GENERAL: "general",
    CHANNEL_CONTROLLED_LOAD: "controlled load",
    CHANNEL_FEED_IN: "feed-in",
}


class AmberEnergyDashboardConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Amber Energy Dashboard."""

    VERSION = 1

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
                },
            )
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
