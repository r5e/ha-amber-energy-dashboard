"""The integration's site device (each mapping's device is in meters.py, section 21),
the site entities' IDs, and the config entry's title."""

from collections.abc import Iterable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.translation import async_get_translations
from homeassistant.util import slugify

from .const import CONF_SITE_ID, CONF_SITE_NAME, DOMAIN

MANUFACTURER = "Amber Electric (unofficial integration)"
DEFAULT_TITLE = "Amber Energy Dashboard"
SITE_DEVICE_NAME = "Amber Energy Dashboard (site)"
ENTITY_ID_PREFIX = "Amber Energy Dashboard"
"""The site device's name until 2.1.0, which new site entities' IDs still start with."""


def entry_title(nmi: str, several_sites: bool, site_name: str | None = None) -> str:
    """The default entry title (2.1.1). With several sites it names the site: its name
    (2.2), else the NMI's last 4 digits. It never shows the full NMI."""
    if not several_sites:
        return DEFAULT_TITLE
    return f"{DEFAULT_TITLE} ({site_name})" if site_name else f"{DEFAULT_TITLE} (…{nmi[-4:]})"


def is_default_title(title: str, nmi: str, site_name: str | None = None) -> bool:
    """True for a title the integration chose (until 2.1.0 "Amber <NMI>"), not the user."""
    return title in (
        f"Amber {nmi}",
        entry_title(nmi, False),
        entry_title(nmi, True),
        entry_title(nmi, True, site_name),
    )


def displayed_site_name(hass: HomeAssistant, entry: ConfigEntry) -> str | None:
    """The site's name wherever the integration shows it: the name the user gave the
    site's device on Home Assistant's device page, else the one from setup (2.2)."""
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, entry.data.get(CONF_SITE_ID, "")), entry.entry_id
    )
    return (device.name_by_user if device else None) or entry.data.get(CONF_SITE_NAME)


def site_device_name(site_name: str | None) -> str:
    """The site device's name: the site name (2.2), else "Amber Energy Dashboard (site)"."""
    return site_name or SITE_DEVICE_NAME


def site_model(network: str | None, nmi: str | None) -> str | None:
    """The site device's model line (2.2): "<network> · NMI …<last 4>", from /sites. It
    never shows the full NMI."""
    parts = [network, f"NMI …{nmi[-4:]}" if nmi else None]
    return " · ".join(p for p in parts if p) or None


def device_info(site: str, model: str | None, site_name: str | None) -> DeviceInfo:
    """The site's device, as created at setup (named, with its model line)."""
    info = DeviceInfo(
        identifiers={(DOMAIN, site)},
        name=site_device_name(site_name),
        manufacturer=MANUFACTURER,
        entry_type=DeviceEntryType.SERVICE,
    )
    if model is not None:
        info["model"] = model
    return info


def site_link(site: str) -> DeviceInfo:
    """The site's entities only link to its device (created at setup), so they never
    reset its name or model."""
    return DeviceInfo(identifiers={(DOMAIN, site)})


async def async_site_entity_ids(
    hass: HomeAssistant, platform: str, descriptions: Iterable[EntityDescription]
) -> dict[str, str]:
    """The entity IDs the site's new entities ask for, by description key (2.1.1).

    HA derives a new entity's ID from its device's name, and the site device was renamed
    to say it is the site's (2.1.1), and can carry the site's name (2.2); this keeps new
    installs on the IDs 2.1.0 gave, whatever the name (existing entities keep theirs in
    the entity registry). Names come from the English strings,
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
