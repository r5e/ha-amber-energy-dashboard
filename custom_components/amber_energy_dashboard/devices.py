"""The integration's devices (one per site, and one per own-sensor mapping, section 18),
and the config entry's title."""

from typing import TYPE_CHECKING

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from .const import DOMAIN

if TYPE_CHECKING:
    from .manager import OwnSensor

MANUFACTURER = "Amber Electric (unofficial integration)"
DEFAULT_TITLE = "Amber Electric"


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
        name="Amber Energy Dashboard",
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
