"""Sites as config sub-entries (2.2.0-dev2, DESIGN section 23).

The config entry is the Amber account (its API key). Each site is a ``site`` sub-entry
that owns the site's device and entities, with the site's data and settings:
``site_id``, ``nmi``, ``site_name``, ``channels``, ``add_to_energy`` and ``options``
(until 2.2.0-dev1 the entry's data and options). Grid meters, appliances and 2.1
own-sensor mappings are separate sub-entries linked to their site by ``site_id``.

Each site has a key, ``<entry ID>_<site ID>``, used for its Store and its Repairs issues.

**Migration** (config entry 1.x -> 2.1, in ``async_migrate_entry``, before any platform
is set up): the entry keeps its ID; a site sub-entry is created with a sub-entry ID
derived from the site ID (so a crash halfway finds it again); mappings get ``site_id``;
the site's device and entities move into the site sub-entry; the Store moves to the
site's key; old per-entry Repairs issues are removed (the checks raise them again);
then the entry keeps only the API key. Every step is idempotent and read back.
Home Assistant saves the device and entity registries about 10 s after a change, later
than the config entries, so the device and entity move also runs at every setup
(``async_ensure_site_ownership``): a kill in between is finished on the next start.
"""

from collections.abc import Mapping
import logging
from types import MappingProxyType
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)

from .const import (
    CONF_ADD_TO_ENERGY,
    CONF_CHANNELS,
    CONF_NMI,
    CONF_OPTIONS,
    CONF_SITE_ID,
    CONF_SITE_NAME,
    DOMAIN,
    SUBENTRY_SITE,
)
from .manager import nem_today
from .storage import async_move_store

_LOGGER = logging.getLogger(__name__)

SITE_DATA_KEYS = (CONF_SITE_ID, CONF_NMI, CONF_SITE_NAME, CONF_CHANNELS, CONF_ADD_TO_ENERGY)
"""What moved from the entry's data to the site sub-entry's."""


class SiteMigrationError(Exception):
    """The migration to site sub-entries did not read back as written."""


def site_key(entry_id: str, site_id: str) -> str:
    """The site's key: its Store and its Repairs issues."""
    return f"{entry_id}_{site_id.lower()}"


def migrated_site_subentry_id(site_id: str) -> str:
    """The sub-entry ID the migration gives a site (fixed, so a crash halfway finds it)."""
    return f"site_{site_id.lower()}"


def site_title(site_name: str | None, nmi: str | None) -> str:
    """A site sub-entry's title: its name, else the NMI's last 4 digits."""
    return site_name or f"Site …{(nmi or '')[-4:]}"


def site_subentries(entry: ConfigEntry) -> list[ConfigSubentry]:
    """The entry's sites, the entry's first site (its unique ID) first."""
    sites = [s for s in entry.subentries.values() if s.subentry_type == SUBENTRY_SITE]
    return sorted(sites, key=lambda s: s.data[CONF_SITE_ID] != entry.unique_id)


def site_options(subentry: ConfigSubentry) -> dict[str, Any]:
    """A site's settings."""
    return dict(subentry.data.get(CONF_OPTIONS) or {})


def mapping_site_id(entry: ConfigEntry, data: Mapping[str, Any]) -> str | None:
    """The site a grid meter, appliance or 2.1 mapping belongs to: its ``site_id``, else
    the entry's first site (a 2.1 mapping from before sites)."""
    if data.get(CONF_SITE_ID):
        return str(data[CONF_SITE_ID])
    sites = site_subentries(entry)
    return sites[0].data[CONF_SITE_ID] if sites else None


@callback
def async_ensure_site_ownership(hass: HomeAssistant, entry: ConfigEntry) -> int:
    """Move each site's device, and its entities, into the site's sub-entry, where they
    aren't already. Run before the platforms are set up, so no platform ever adds an
    entity whose sub-entry differs from its device's (the cause of the 2.0.x sensor
    drops). Returns the number of devices and entities moved."""
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    moved = 0
    for sub in site_subentries(entry):
        site_id = sub.data[CONF_SITE_ID]
        device = devices.async_get_device_by_identifier((DOMAIN, site_id), entry.entry_id)
        if device is not None and device.config_subentry_id != sub.subentry_id:
            devices.async_update_device(device.id, new_config_subentry_id=sub.subentry_id)
            moved += 1
        for entity in er.async_entries_for_config_entry(entities, entry.entry_id):
            on_device = device is not None and entity.device_id == device.id
            site_entity = entity.config_subentry_id is None and entity.unique_id.startswith(
                f"{site_id}_"
            )
            if (on_device or site_entity) and entity.config_subentry_id != sub.subentry_id:
                entities.async_update_entity(entity.entity_id, config_subentry_id=sub.subentry_id)
                moved += 1
    if moved:
        _LOGGER.info("Moved %d site devices and entities into their site sub-entries", moved)
    return moved


async def async_migrate_to_sites(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Entry 1.x (one site, settings in the entry's options) -> 2.1 (an account entry
    with a site sub-entry). Idempotent; raises SiteMigrationError if a step doesn't read
    back."""
    data = dict(entry.data)
    site_id: str = data[CONF_SITE_ID]
    sub_id = migrated_site_subentry_id(site_id)
    if sub_id not in entry.subentries:
        hass.config_entries.async_add_subentry(
            entry,
            ConfigSubentry(
                data=MappingProxyType(
                    {k: data[k] for k in SITE_DATA_KEYS if k in data}
                    | {CONF_OPTIONS: dict(entry.options)}
                ),
                subentry_id=sub_id,
                subentry_type=SUBENTRY_SITE,
                title=site_title(data.get(CONF_SITE_NAME), data.get(CONF_NMI)),
                unique_id=site_id,
            ),
        )
    for sub in list(entry.subentries.values()):
        if sub.subentry_type != SUBENTRY_SITE and CONF_SITE_ID not in sub.data:
            hass.config_entries.async_update_subentry(
                entry, sub, data={**sub.data, CONF_SITE_ID: site_id}
            )
    async_ensure_site_ownership(hass, entry)
    key = site_key(entry.entry_id, site_id)
    result = await async_move_store(hass, entry.entry_id, key, nem_today)
    registry = ir.async_get(hass)
    for domain, issue_id in list(registry.issues):
        if domain == DOMAIN and issue_id.endswith(f"_{entry.entry_id}"):
            ir.async_delete_issue(hass, DOMAIN, issue_id)
    hass.config_entries.async_update_entry(
        entry,
        data={CONF_API_KEY: data[CONF_API_KEY]},
        options={},
        version=2,
        minor_version=1,
    )
    _check_migrated(hass, entry, site_id, sub_id)
    _LOGGER.info("Entry migrated to an account with a site sub-entry (store %s)", result)


def _check_migrated(hass: HomeAssistant, entry: ConfigEntry, site_id: str, sub_id: str) -> None:
    """Read the migration back: the site sub-entry, the mappings' site, the device's and
    the site entities' sub-entry, and the entry's version and data."""
    problems = []
    sub = entry.subentries.get(sub_id)
    if sub is None or sub.unique_id != site_id or sub.data.get(CONF_SITE_ID) != site_id:
        problems.append("site sub-entry")
    if any(
        s.subentry_type != SUBENTRY_SITE and CONF_SITE_ID not in s.data
        for s in entry.subentries.values()
    ):
        problems.append("mappings without a site")
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, site_id), entry.entry_id)
    if device is not None and device.config_subentry_id != sub_id:
        problems.append("site device")
    entities = er.async_get(hass)
    if any(
        e.config_subentry_id is None and e.unique_id.startswith(f"{site_id}_")
        for e in er.async_entries_for_config_entry(entities, entry.entry_id)
    ):
        problems.append("site entities")
    if (entry.version, entry.minor_version) != (2, 1) or set(entry.data) != {CONF_API_KEY}:
        problems.append("entry version or data")
    if problems:
        raise SiteMigrationError(f"migration to site sub-entries: {', '.join(problems)}")
