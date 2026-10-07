"""The integration's devices: one per site, and one per own-sensor mapping (section 18)."""

from typing import TYPE_CHECKING

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from .const import DOMAIN

if TYPE_CHECKING:
    from .manager import OwnSensor

MANUFACTURER = "Amber Electric (unofficial integration)"


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
