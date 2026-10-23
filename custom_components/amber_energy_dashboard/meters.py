"""Grid meters, appliances and 2.1 own-sensor mappings (DESIGN section 21).

Each is a config sub-entry. A **grid meter** is a meter on the main supply: an import
sensor priced on the general (or controlled load) channel, and optionally an export
sensor priced on the feed-in channel. An **appliance** is any single energy sensor priced
on one channel. A 2.1 **own-sensor mapping** (one sensor on one channel) keeps working
unchanged until the user converts it (a Repairs issue with a fix flow).

Every side is an ``OwnSensor`` with its own cost chain. The cost statistic's ID comes from
the sensor's entity ID only, so a conversion keeps every statistic; the chain state in
the Store moves to the new chain key.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from .const import (
    CHANNEL_FEED_IN,
    CONF_CHANNEL,
    CONF_ENTITY_IDS,
    CONF_EXPORT_CHANNEL,
    CONF_EXPORT_SENSOR,
    CONF_IMPORT_CHANNEL,
    CONF_IMPORT_SENSOR,
    CONF_SENSOR,
    CONF_SITE_ID,
    DOMAIN,
    ROLE_EXPORT,
    ROLE_IMPORT,
    SUBENTRY_APPLIANCE,
    SUBENTRY_GRID_METER,
    SUBENTRY_OWN_SENSOR,
)
from .devices import MANUFACTURER
from .manager import AmberManager, OwnSensor
from .statistics import CHANNEL_LABELS, ChannelConfig, own_cost_spec
from .storage import async_rename_chains

ISSUE_CONVERT = "convert_own_sensors"
DEFAULT_GRID_NAME = "Grid meter"


def grid_meter_title(name: str) -> str:
    """The sub-entry title, for example "Whole house meter · grid meter"."""
    return f"{name} · grid meter"


def appliance_title(name: str, channel: ChannelConfig) -> str:
    """The sub-entry title, for example "Pool heat pump · appliance (general E1)"."""
    return f"{name} · appliance ({CHANNEL_LABELS[channel.type]} {channel.identifier})"


def _sides(subentry: ConfigSubentry) -> list[tuple[str, str, str | None, str | None]]:
    """(entity ID, channel identifier, role, statistic label) for each priced sensor."""
    data = subentry.data
    if subentry.subentry_type == SUBENTRY_OWN_SENSOR:
        return [(data[CONF_SENSOR], data[CONF_CHANNEL], None, None)]
    if subentry.subentry_type == SUBENTRY_APPLIANCE:
        return [(data[CONF_SENSOR], data[CONF_CHANNEL], None, data[CONF_NAME])]
    if subentry.subentry_type != SUBENTRY_GRID_METER:
        return []  # a type this version doesn't know
    name = data[CONF_NAME]
    sides = [(data[CONF_IMPORT_SENSOR], data[CONF_IMPORT_CHANNEL], ROLE_IMPORT, f"{name} import")]
    if data.get(CONF_EXPORT_SENSOR):
        sides.append(
            (data[CONF_EXPORT_SENSOR], data[CONF_EXPORT_CHANNEL], ROLE_EXPORT, f"{name} export")
        )
    return sides


def own_sensors_from_entry(
    entry: ConfigEntry, channels: tuple[ChannelConfig, ...]
) -> list[OwnSensor]:
    """Every priced sensor of the entry's sub-entries whose channel still exists."""
    by_ident = {c.identifier: c for c in channels}
    site = entry.data[CONF_SITE_ID]
    sensors = []
    for subentry in entry.subentries.values():
        for entity_id, ident, role, label in _sides(subentry):
            channel = by_ident.get(ident)
            if channel is None:
                continue
            sensors.append(
                OwnSensor(
                    subentry.subentry_id,
                    entity_id,
                    channel,
                    own_cost_spec(site, entity_id, channel, label),
                    subentry.data.get(CONF_NAME, entity_id),
                    kind=subentry.subentry_type,
                    role=role,
                )
            )
    return sensors


def mapped_entities(entry: ConfigEntry) -> set[str]:
    """Every energy sensor used by a sub-entry (a sensor is priced by one mapping only:
    its cost statistic's ID comes from its entity ID)."""
    return {entity_id for sub in entry.subentries.values() for entity_id, *_ in _sides(sub)}


def mapping_device_info(site: str, sensor: OwnSensor, site_device_id: str) -> DeviceInfo:
    """A mapping's device, linked to the site's. A device belongs to one config
    sub-entry, so the mapping's entities can't share the site's device (section 18)."""
    if sensor.kind == SUBENTRY_GRID_METER:
        name, model = sensor.name, "Grid meter"
    elif sensor.kind == SUBENTRY_APPLIANCE:
        channel = sensor.channel
        name = sensor.name
        model = f"Appliance ({CHANNEL_LABELS[channel.type]} {channel.identifier})"
    else:
        name, model = f"{sensor.name} (own sensor)", "Own energy sensor"
    return DeviceInfo(
        identifiers={(DOMAIN, f"{site}_own_{sensor.subentry_id}")},
        name=name,
        manufacturer=MANUFACTURER,
        model=model,
        entry_type=DeviceEntryType.SERVICE,
        via_device_id=site_device_id,
    )


# --- converting 2.1 own-sensor mappings ---------------------------------------------------


@dataclass(slots=True)
class ConversionPlan:
    """Which 2.1 mappings become one grid meter, and which become appliances."""

    grid_import: str | None = None
    """The 2.1 mapping (sub-entry ID) that becomes the grid meter's import side."""
    grid_export: str | None = None
    grid_name: str = DEFAULT_GRID_NAME
    appliances: list[str] = field(default_factory=list)


def legacy_mappings(entry: ConfigEntry, channels: Iterable[ChannelConfig]) -> list[ConfigSubentry]:
    """The 2.1 own-sensor mappings whose channel still exists (others are ignored at run
    time, and left alone)."""
    idents = {c.identifier for c in channels}
    return [
        sub
        for sub in entry.subentries.values()
        if sub.subentry_type == SUBENTRY_OWN_SENSOR and sub.data[CONF_CHANNEL] in idents
    ]


def split_legacy(
    legacy: Iterable[ConfigSubentry], channels: Iterable[ChannelConfig]
) -> tuple[list[ConfigSubentry], list[ConfigSubentry]]:
    """The 2.1 mappings that can be a grid meter's import side, and its export side."""
    types = {c.identifier: c.type for c in channels}
    imports, exports = [], []
    for sub in legacy:
        (exports if types[sub.data[CONF_CHANNEL]] == CHANNEL_FEED_IN else imports).append(sub)
    return imports, exports


def default_plan(
    legacy: list[ConfigSubentry], channels: tuple[ChannelConfig, ...], grid_name: str | None = None
) -> ConversionPlan:
    """An import mapping and an export mapping become one grid meter when each is the
    only one of its kind (an import on the general channel is preferred); every other
    mapping becomes an appliance."""
    imports, exports = split_legacy(legacy, channels)
    general = {c.identifier for c in channels if c.type == "general"}
    on_general = [s for s in imports if s.data[CONF_CHANNEL] in general]
    grid_import = (
        on_general[0].subentry_id
        if len(on_general) == 1
        else imports[0].subentry_id
        if len(imports) == 1
        else None
    )
    grid_export = exports[0].subentry_id if grid_import and len(exports) == 1 else None
    return plan_with(legacy, grid_import, grid_export, grid_name or DEFAULT_GRID_NAME)


def plan_with(
    legacy: list[ConfigSubentry], grid_import: str | None, grid_export: str | None, name: str
) -> ConversionPlan:
    """A plan with the given grid meter sides; every other mapping is an appliance."""
    grid = {grid_import, grid_export}
    return ConversionPlan(
        grid_import,
        grid_export,
        name,
        [s.subentry_id for s in legacy if s.subentry_id not in grid],
    )


def describe_plan(entry: ConfigEntry, plan: ConversionPlan) -> str:
    """The plan as Markdown lines for the fix flow."""
    subs = entry.subentries
    lines = []
    if plan.grid_import:
        lines.append(f"- Grid meter **{plan.grid_name}**:")
        lines.append(f"  - import: {subs[plan.grid_import].title}")
        if plan.grid_export:
            lines.append(f"  - export: {subs[plan.grid_export].title}")
    for sid in plan.appliances:
        sub = subs[sid]
        name = sub.data.get(CONF_NAME, sub.data[CONF_SENSOR])
        lines.append(f"- Appliance **{name}**: {sub.title}")
    return "\n".join(lines)


def describe_mappings(subentries: Iterable[ConfigSubentry]) -> str:
    """The 2.1 mappings as Markdown lines (the Repairs issue)."""
    return "\n".join(f"- {sub.title}" for sub in subentries)


def grid_meter_unique_id(import_entity_id: str) -> str:
    """A grid meter's sub-entry unique ID (its import sensor)."""
    return f"{SUBENTRY_GRID_METER}:{import_entity_id}"


def appliance_unique_id(entity_id: str) -> str:
    """An appliance's sub-entry unique ID (its sensor)."""
    return f"{SUBENTRY_APPLIANCE}:{entity_id}"


class ConversionFailed(Exception):
    """A conversion did not read back as written."""


@callback
def async_update_convert_issue(
    hass: HomeAssistant, entry: ConfigEntry, channels: Iterable[ChannelConfig]
) -> None:
    """Offer the conversion (a fixable Repairs issue) while 2.1 mappings remain."""
    issue_id = f"{ISSUE_CONVERT}_{entry.entry_id}"
    legacy = legacy_mappings(entry, channels)
    if not legacy:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=True,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_CONVERT,
        translation_placeholders={"entry": entry.title, "mappings": describe_mappings(legacy)},
        data={"entry_id": entry.entry_id},
    )


async def async_convert(
    hass: HomeAssistant, entry: ConfigEntry, plan: ConversionPlan
) -> dict[str, Any]:
    """Replace 2.1 mappings with a grid meter and appliances, keeping every statistic.

    Under the run lock: each old chain's Store state moves to its new chain key, the old
    sub-entries are removed (with their devices and entities), and the new ones added; the
    entry then reloads. A grid meter's reconciliation sensors ask for the entity IDs of
    the mappings they replace. Everything is read back.
    """
    manager: AmberManager = entry.runtime_data.manager
    channels = {c.identifier: c for c in manager.ctx.channels}
    site = manager.ctx.site_id
    registry = er.async_get(hass)

    def reconciliation(sid: str) -> str | None:
        return registry.async_get_entity_id("sensor", DOMAIN, f"{site}_reconciliation_{sid}")

    async with manager.ctx.lock:
        old_ids = [s for s in (plan.grid_import, plan.grid_export, *plan.appliances) if s]
        old = {sid: entry.subentries[sid] for sid in old_ids}
        if any(s.subentry_type != SUBENTRY_OWN_SENSOR for s in old.values()):
            raise ConversionFailed("only 2.1 own-sensor mappings can be converted")
        new: list[ConfigSubentry] = []
        renames: dict[str, str] = {}
        if plan.grid_import:
            imp = old[plan.grid_import].data
            data: dict[str, Any] = {
                CONF_NAME: plan.grid_name,
                CONF_IMPORT_SENSOR: imp[CONF_SENSOR],
                CONF_IMPORT_CHANNEL: imp[CONF_CHANNEL],
            }
            hints = {"import_reconciliation": reconciliation(plan.grid_import)}
            if plan.grid_export:
                exp = old[plan.grid_export].data
                data[CONF_EXPORT_SENSOR] = exp[CONF_SENSOR]
                data[CONF_EXPORT_CHANNEL] = exp[CONF_CHANNEL]
                hints["export_reconciliation"] = reconciliation(plan.grid_export)
            if hints := {k: v for k, v in hints.items() if v}:
                data[CONF_ENTITY_IDS] = hints
            grid = ConfigSubentry(
                data=MappingProxyType(data),
                subentry_type=SUBENTRY_GRID_METER,
                title=grid_meter_title(plan.grid_name),
                unique_id=grid_meter_unique_id(imp[CONF_SENSOR]),
            )
            new.append(grid)
            renames[f"own:{plan.grid_import}"] = f"own:{grid.subentry_id}:{ROLE_IMPORT}"
            if plan.grid_export:
                renames[f"own:{plan.grid_export}"] = f"own:{grid.subentry_id}:{ROLE_EXPORT}"
        for sid in plan.appliances:
            data = dict(old[sid].data)
            name = data.get(CONF_NAME, data[CONF_SENSOR])
            appliance = ConfigSubentry(
                data=MappingProxyType(
                    {
                        CONF_NAME: name,
                        CONF_SENSOR: data[CONF_SENSOR],
                        CONF_CHANNEL: data[CONF_CHANNEL],
                    }
                ),
                subentry_type=SUBENTRY_APPLIANCE,
                title=appliance_title(name, channels[data[CONF_CHANNEL]]),
                unique_id=appliance_unique_id(data[CONF_SENSOR]),
            )
            new.append(appliance)
            renames[f"own:{sid}"] = f"own:{appliance.subentry_id}"
        had = set(manager.store.as_dict()["chains"])
        sensors, manager.own_sensors = manager.own_sensors, []  # no chain runs on old keys
        await async_rename_chains(manager.store, renames)
        chains = set(manager.store.as_dict()["chains"])
        if moved := [
            f"chain {o} did not move to {n}"
            for o, n in renames.items()
            if o in had and (o in chains or n not in chains)
        ]:
            manager.own_sensors = sensors  # nothing else has changed
            raise ConversionFailed("; ".join(moved))
        # One reload at the end, once the old sub-entries (and so their entities) are
        # gone, so the new entities can take over their entity IDs.
        entry.runtime_data.converting = True
        for sid in old:
            hass.config_entries.async_remove_subentry(entry, sid)
        for sub in new:
            hass.config_entries.async_add_subentry(entry, sub)
        hass.config_entries.async_schedule_reload(entry.entry_id)

    if problems := [
        *(f"{sid} is still configured" for sid in old if sid in entry.subentries),
        *(f"{sub.title} was not added" for sub in new if sub.subentry_id not in entry.subentries),
    ]:
        raise ConversionFailed("; ".join(problems))
    async_update_convert_issue(hass, entry, manager.ctx.channels)
    return {
        "removed": [old[sid].title for sid in old_ids],
        "added": [sub.title for sub in new],
        "chains_moved": len([o for o in renames if o in had]),
    }
