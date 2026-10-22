"""The integration's devices (one per site, and one per own-sensor mapping, section 18),
the site entities' IDs, and the config entry's title."""

from collections.abc import Iterable
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.translation import async_get_translations
from homeassistant.util import slugify

from .const import DOMAIN

if TYPE_CHECKING:
    from .manager import OwnSensor

MANUFACTURER = "Amber Electric (unofficial integration)"
DEFAULT_TITLE = "Amber Energy Dashboard"
SITE_DEVICE_NAME = "Amber Energy Dashboard (site)"
ENTITY_ID_PREFIX = "Amber Energy Dashboard"
"""The site device's name until 2.1.0, which new site entities' IDs still start with."""


def entry_title(nmi: str, several_sites: bool) -> str:
    """The default entry title (2.1.1). It names no NMI, except its last 4 digits when
    several sites are configured, to tell them apart."""
    return f"{DEFAULT_TITLE} (…{nmi[-4:]})" if several_sites else DEFAULT_TITLE


def is_default_title(title: str, nmi: str) -> bool:
    """True for a title the integration chose (until 2.1.0 "Amber <NMI>"), not the user."""
    return title in (f"Amber {nmi}", entry_title(nmi, False), entry_title(nmi, True))


def device_info(site: str) -> DeviceInfo:
    """The site's device, shared by the sensors and buttons."""
    return DeviceInfo(
        identifiers={(DOMAIN, site)},
        name=SITE_DEVICE_NAME,
        manufacturer=MANUFACTURER,
        entry_type=DeviceEntryType.SERVICE,
    )


def own_sensor_device_info(site: str, sensor: OwnSensor, site_device_id: str) -> DeviceInfo:
    """An own-sensor mapping's device, linked to the site's. A device belongs to one
    config sub-entry, so the mapping's entities can't share the site's device."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"{site}_own_{sensor.subentry_id}")},
        name=f"{sensor.name} (own sensor)",
        manufacturer=MANUFACTURER,
        model="Own energy sensor",
        entry_type=DeviceEntryType.SERVICE,
        via_device_id=site_device_id,
    )


async def async_site_entity_ids(
    hass: HomeAssistant, platform: str, descriptions: Iterable[EntityDescription]
) -> dict[str, str]:
    """The entity IDs the site's new entities ask for, by description key (2.1.1).

    HA derives a new entity's ID from its device's name, and the site device was renamed
    to say it is the site's; this keeps new installs on the IDs 2.1.0 gave (existing
    entities keep theirs in the entity registry). Names come from the English strings,
    as before (the integration has no other language).
    """
    strings = await async_get_translations(hass, "en", "entity", [DOMAIN])
    ids = {}
    for description in descriptions:
        name = strings.get(
            f"component.{DOMAIN}.entity.{platform}.{description.translation_key}.name"
        )
        if name:
            name = name.format(**(description.translation_placeholders or {}))
            ids[description.key] = f"{platform}.{slugify(f'{ENTITY_ID_PREFIX} {name}')}"
    return ids
