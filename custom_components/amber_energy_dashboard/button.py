"""Device buttons (DESIGN section 18): run now, and re-check the retention boundary."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AmberConfigEntry, SiteRuntime, async_probe_retention
from .devices import async_site_entity_ids, site_link
from .manager import AmberManager

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
    """Create each site's buttons (in its sub-entry, section 23). Only the entry's first
    site asks for the 2.1.0 entity IDs."""
    entity_ids = await async_site_entity_ids(hass, "button", BUTTONS)
    for index, site in enumerate(entry.runtime_data.sites.values()):
        ids = entity_ids if index == 0 else {}
        async_add_entities(
            (AmberButton(site, description, ids.get(description.key)) for description in BUTTONS),
            config_subentry_id=site.subentry_id,
        )


class AmberButton(ButtonEntity):
    """A button on the site's device."""

    entity_description: AmberButtonDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        site: SiteRuntime,
        description: AmberButtonDescription,
        entity_id: str | None = None,
    ) -> None:
        """Initialise the button (``entity_id`` is only a suggestion for a new one)."""
        self.entity_description = description
        if entity_id is not None:
            self.entity_id = entity_id
        self._manager = site.manager
        site_id = site.context.site_id
        self._attr_unique_id = f"{site_id}_{description.key}"
        self._attr_device_info = site_link(site_id)

    async def async_press(self) -> None:
        """Run the action; errors are shown in the UI."""
        await self.entity_description.press_fn(self._manager)
