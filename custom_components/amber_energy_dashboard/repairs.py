"""Repairs fix flows: "The Energy dashboard isn't using Amber Energy Dashboard" (DESIGN
section 18), and converting 2.1 own-sensor mappings to a grid meter and appliances
(section 21). The integration's other issues are not fixable."""

from typing import Any

from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)
import voluptuous as vol

from .const import DOMAIN
from .energy import (
    async_add_to_energy,
    async_preferences,
    describe_sources,
    grid_sources,
    has_grid,
)
from .manager import AmberManager
from .meters import (
    DEFAULT_GRID_NAME,
    ISSUE_CONVERT,
    ConversionFailed,
    ConversionPlan,
    async_convert,
    default_plan,
    describe_mappings,
    describe_plan,
    legacy_mappings,
    plan_with,
    split_legacy,
)
from .migration import async_detect

NONE = "none"
GRID_IMPORT = "grid_import"
GRID_EXPORT = "grid_export"
GRID_NAME = "grid_name"


def _site_manager(hass: HomeAssistant, data: dict[str, Any]) -> tuple[Any, AmberManager] | None:
    """The loaded entry and the manager of the site an issue is for (``site_id`` in its
    data; the entry's first site without one), or None."""
    entry = hass.config_entries.async_get_entry(data.get("entry_id", ""))
    if entry is None or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED:
        return None
    sites = list(entry.runtime_data.sites.values())
    site_id = data.get("site_id")
    site = next((s for s in sites if s.context.site_id == site_id), None)
    if site is None and site_id is None and sites:
        site = sites[0]
    return None if site is None else (entry, site.manager)


class EnergyNotUsedFlow(RepairsFlow):
    """Add the grid sources, point to the migration, or explain and dismiss."""

    def __init__(self, data: dict[str, Any]) -> None:
        """Remember which site the issue is for."""
        self._data = data
        self._manager: AmberManager | None = None

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Choose the path: no grid source, a YAML kit, or another grid source."""
        found = _site_manager(self.hass, self._data)
        if found is None:
            return self.async_abort(reason="not_loaded")
        self._manager = found[1]
        energy = await async_preferences(self.hass)
        if energy is None:
            return self.async_abort(reason="energy_unavailable")
        if not has_grid(energy.data):
            return await self.async_step_add()
        if (await async_detect(self.hass))["candidates"]:
            return await self.async_step_migrate()
        return await self.async_step_other()

    async def async_step_add(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """No grid source: add this entry's grid sources."""
        assert self._manager is not None
        if user_input is not None:
            result = await async_add_to_energy(self.hass, self._manager, "repair")
            if result["added"]:
                return self.async_create_entry(data={})
            return self.async_abort(
                reason="add_failed", description_placeholders={"reason": str(result["reason"])}
            )
        return self.async_show_form(
            step_id="add",
            data_schema=vol.Schema({}),
            description_placeholders={"sources": describe_sources(grid_sources(self._manager))},
        )

    async def async_step_migrate(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """A YAML kit is detected: the migration switches the Energy dashboard."""
        if user_input is not None:
            return self.async_abort(reason="use_migration")
        return self.async_show_form(step_id="migrate", data_schema=vol.Schema({}))

    async def async_step_other(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Another grid source: explain how to select the statistics, and dismiss."""
        assert self._manager is not None
        if user_input is not None:
            await self._manager.store.async_dismiss_energy_issue()
            return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="other",
            data_schema=vol.Schema({}),
            description_placeholders={"sources": describe_sources(grid_sources(self._manager))},
        )


class ConvertMappingsFlow(RepairsFlow):
    """Convert 2.1 own-sensor mappings: an import and an export mapping become one grid
    meter, every other mapping an appliance. Statistics are kept (section 21)."""

    def __init__(self, data: dict[str, Any]) -> None:
        """Remember which site the issue is for."""
        self._data = data
        self._entry: ConfigEntry | None = None
        self._manager: AmberManager | None = None
        self._plan: ConversionPlan | None = None

    def _legacy(self) -> tuple[list[Any], tuple[Any, ...]]:
        """The site's 2.1 mappings to convert, and its channels."""
        assert self._entry is not None and self._manager is not None
        channels = self._manager.ctx.channels
        return legacy_mappings(self._entry, channels, self._manager.ctx.site_id), channels

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Choose the grid meter's sides when there is a choice; else confirm."""
        found = _site_manager(self.hass, self._data)
        if found is None:
            return self.async_abort(reason="not_loaded")
        self._entry, self._manager = found
        legacy, channels = self._legacy()
        if not legacy:
            return self.async_abort(reason="nothing_to_convert")
        imports, _ = split_legacy(legacy, channels)
        if not imports:
            self._plan = plan_with(legacy, None, None, DEFAULT_GRID_NAME)
            return await self.async_step_confirm()
        return await self.async_step_plan()

    async def async_step_plan(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Which mappings make the grid meter (import, and optionally export), and its name."""
        legacy, channels = self._legacy()
        imports, exports = split_legacy(legacy, channels)
        errors: dict[str, str] = {}
        if user_input is not None:
            grid_import = user_input[GRID_IMPORT]
            grid_export = user_input.get(GRID_EXPORT, NONE)
            name = str(user_input.get(GRID_NAME) or "").strip()
            if grid_import == NONE and grid_export != NONE:
                errors[GRID_EXPORT] = "export_needs_import"
            elif grid_import != NONE and not name:
                errors[GRID_NAME] = "name_required"
            else:
                self._plan = plan_with(
                    legacy,
                    None if grid_import == NONE else grid_import,
                    None if grid_export == NONE else grid_export,
                    name or DEFAULT_GRID_NAME,
                )
                return await self.async_step_confirm()
        default = default_plan(legacy, channels)

        def select(subs: list[Any]) -> SelectSelector:
            return SelectSelector(
                SelectSelectorConfig(
                    options=[
                        *(SelectOptionDict(value=s.subentry_id, label=s.title) for s in subs),
                        SelectOptionDict(value=NONE, label="None"),
                    ],
                    mode=SelectSelectorMode.LIST,
                )
            )

        fields: dict[Any, Any] = {
            vol.Required(GRID_IMPORT, default=default.grid_import or NONE): select(imports)
        }
        if exports:
            fields[vol.Required(GRID_EXPORT, default=default.grid_export or NONE)] = select(exports)
        fields[vol.Optional(GRID_NAME, default=DEFAULT_GRID_NAME)] = TextSelector()
        return self.async_show_form(
            step_id="plan",
            data_schema=vol.Schema(fields),
            errors=errors,
            description_placeholders={"mappings": describe_mappings(legacy)},
        )

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Show the outcome; convert on submit."""
        assert self._entry is not None
        assert self._plan is not None
        if user_input is not None:
            try:
                assert self._manager is not None
                await async_convert(self.hass, self._entry, self._manager, self._plan)
            except ConversionFailed as err:
                return self.async_abort(
                    reason="convert_failed", description_placeholders={"reason": str(err)}
                )
            return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={"plan": describe_plan(self._entry, self._plan)},
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    """The fix flow for a fixable issue: energy_not_used, or convert_own_sensors."""
    if issue_id.startswith(ISSUE_CONVERT):
        return ConvertMappingsFlow(dict(data or {}))
    return EnergyNotUsedFlow(dict(data or {}))
