"""The integration's site device (each mapping's device is in meters.py, section 21),
the site entities' IDs, and the config entry's title."""

from collections.abc import Iterable

from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.translation import async_get_translations
from homeassistant.util import slugify

from .const import DOMAIN

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


def site_model(network: str | None, nmi: str | None) -> str | None:
    """The site device's model line (2.2): "<network> · NMI …<last 4>", from /sites. It
    never shows the full NMI."""
    parts = [network, f"NMI …{nmi[-4:]}" if nmi else None]
    return " · ".join(p for p in parts if p) or None


def device_info(site: str, model: str | None = None) -> DeviceInfo:
    """The site's device, shared by the sensors and buttons. The model is set once, at
    setup; entities leave it out, so the registry keeps it."""
    info = DeviceInfo(
        identifiers={(DOMAIN, site)},
        name=SITE_DEVICE_NAME,
        manufacturer=MANUFACTURER,
        entry_type=DeviceEntryType.SERVICE,
    )
    if model is not None:
        info["model"] = model
    return info


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
