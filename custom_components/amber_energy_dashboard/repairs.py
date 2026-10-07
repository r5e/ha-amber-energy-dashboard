"""Repairs fix flow for "The Energy dashboard isn't using Amber Energy Dashboard"
(DESIGN section 18). The integration's other issues are not fixable."""

from typing import Any

from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
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
from .migration import async_detect


class EnergyNotUsedFlow(RepairsFlow):
    """Add the grid sources, point to the migration, or explain and dismiss."""

    def __init__(self, entry_id: str) -> None:
        """Remember which entry the issue is for."""
        self._entry_id = entry_id
        self._manager: AmberManager | None = None

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Choose the path: no grid source, a YAML kit, or another grid source."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="not_loaded")
        self._manager = entry.runtime_data.manager
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


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    """The fix flow for a fixable issue (only energy_not_used is fixable)."""
    return EnergyNotUsedFlow((data or {})["entry_id"])
