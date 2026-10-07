"""Device buttons (DESIGN section 18): run now, and re-check the retention boundary."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AmberConfigEntry, async_probe_retention
from .manager import AmberManager
from .sensor import device_info

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class AmberButtonDescription(ButtonEntityDescription):
    """A button that calls the manager."""

    press_fn: Callable[[AmberManager], Awaitable[Any]]


BUTTONS = (
    AmberButtonDescription(
        key="run_now",
        translation_key="run_now",
        press_fn=lambda manager: manager.async_run("button"),
    ),
    AmberButtonDescription(
        key="probe_retention",
        translation_key="probe_retention",
        entity_category=EntityCategory.DIAGNOSTIC,
        press_fn=async_probe_retention,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AmberConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the buttons for one site."""
    async_add_entities(AmberButton(entry, description) for description in BUTTONS)


class AmberButton(ButtonEntity):
    """A button on the site's device."""

    entity_description: AmberButtonDescription
    _attr_has_entity_name = True

    def __init__(self, entry: AmberConfigEntry, description: AmberButtonDescription) -> None:
        """Initialise the button."""
        self.entity_description = description
        self._manager = entry.runtime_data.manager
        site = entry.runtime_data.context.site_id
        self._attr_unique_id = f"{site}_{description.key}"
        self._attr_device_info = device_info(site)

    async def async_press(self) -> None:
        """Run the action; errors are shown in the UI."""
        await self.entity_description.press_fn(self._manager)
