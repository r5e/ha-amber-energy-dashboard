"""Energy dashboard preferences (DESIGN sections 14 and 18).

Shared by the migration (switching grid sources from the YAML kit's statistics to ours)
and the Energy dashboard setup (adding this entry's grid sources when there are none).
Every change validates the new sources against Home Assistant's energy schema, goes
through the energy manager, is read back, and restores the saved sources on a mismatch.
"""

import copy
from datetime import datetime, timedelta
import json
import logging
from typing import TYPE_CHECKING, Any, Final

from homeassistant.components.energy.data import (
    ENERGY_SOURCE_SCHEMA,
    EnergyManager,
    async_get_manager,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .const import CHANNEL_CONTROLLED_LOAD, CHANNEL_FEED_IN, CHANNEL_GENERAL, DOMAIN, MODE_PRICING
from .statistics import Metric

if TYPE_CHECKING:
    from .manager import AmberManager

_LOGGER = logging.getLogger(__name__)

SOURCE_KEYS: Final = ("stat_energy_from", "stat_cost", "stat_energy_to", "stat_compensation")
ISSUE_ENERGY_NOT_USED: Final = "energy_not_used"
ISSUE_DELAY: Final = timedelta(hours=24)
"""The issue is raised only this long after the first successful import."""
_LISTENING: Final = f"{DOMAIN}_energy_listener"


def _utcnow() -> datetime:
    return dt_util.utcnow()


async def async_preferences(hass: HomeAssistant) -> EnergyManager | None:
    """Home Assistant's energy manager, or None when it cannot be loaded."""
    try:
        return await async_get_manager(hass)
    except Exception:  # any failure means "not safely readable"
        _LOGGER.debug("Energy preferences unavailable", exc_info=True)
        return None


def validate_sources(sources: list[dict[str, Any]]) -> str | None:
    """None when the sources validate against Home Assistant's schema, else the error."""
    try:
        ENERGY_SOURCE_SCHEMA(copy.deepcopy(sources))
    except vol.Invalid as err:
        return f"The changed preferences do not validate: {err}"
    return None


async def async_save_sources(
    energy: EnergyManager,
    after: list[dict[str, Any]],
    indexes: list[int],
    restore: list[dict[str, Any]],
) -> bool:
    """Save the energy sources and read the changed ones back. On a mismatch, the
    ``restore`` sources are saved again and False is returned."""
    await energy.async_update({"energy_sources": copy.deepcopy(after)})
    current = (energy.data or {}).get("energy_sources", [])
    ok = all(
        index < len(current)
        and all(current[index].get(k) == after[index].get(k) for k in SOURCE_KEYS)
        for index in indexes
    )
    if not ok:
        await energy.async_update({"energy_sources": copy.deepcopy(restore)})
    return ok


def has_grid(data: dict[str, Any] | None) -> bool:
    """True when the preferences hold at least one grid source."""
    return any(s.get("type") == "grid" for s in (data or {}).get("energy_sources", []))


def uses_statistics(data: dict[str, Any] | None, ids: set[str]) -> bool:
    """True when any energy preference (a source or a device) references one of ``ids``."""
    text = json.dumps(data or {}, default=str)
    return any(f'"{sid}"' in text for sid in ids)


def entry_statistic_ids(manager: AmberManager) -> set[str]:
    """Every statistic this entry writes: usage, cost and net cost, prices, own sensors."""
    ids = {spec.statistic_id for spec in manager.ctx.specs}
    ids |= {sensor.spec.statistic_id for sensor in manager.own_sensors}
    return ids


def _grid(**stats: str | None) -> dict[str, Any]:
    return {
        "type": "grid",
        "stat_energy_from": stats.get("stat_energy_from"),
        "stat_energy_to": stats.get("stat_energy_to"),
        "stat_cost": stats.get("stat_cost"),
        "entity_energy_price": None,
        "number_energy_price": None,
        "stat_compensation": stats.get("stat_compensation"),
        "entity_energy_price_export": None,
        "number_energy_price_export": None,
        "cost_adjustment_day": 0.0,
    }


def grid_sources(manager: AmberManager) -> list[dict[str, Any]]:
    """This entry's grid sources: the general channel with the feed-in on one source, and
    each further import channel (controlled load) on its own source."""
    specs = {(s.channel, s.metric): s.statistic_id for s in manager.ctx.specs}
    channels = manager.ctx.channels
    imports = [c for c in channels if c.type == CHANNEL_GENERAL] + [
        c for c in channels if c.type == CHANNEL_CONTROLLED_LOAD
    ]
    feed_in = next((c for c in channels if c.type == CHANNEL_FEED_IN), None)
    sources = [
        _grid(
            stat_energy_from=specs[(c.identifier, Metric.ENERGY)],
            stat_cost=specs[(c.identifier, Metric.COST)],
        )
        for c in imports
    ]
    if feed_in is not None:
        if not sources or imports[0].type != CHANNEL_GENERAL:
            sources.insert(0, _grid())
        sources[0]["stat_energy_to"] = specs[(feed_in.identifier, Metric.ENERGY)]
        sources[0]["stat_compensation"] = specs[(feed_in.identifier, Metric.COMPENSATION)]
    return sources


def describe_sources(sources: list[dict[str, Any]]) -> str:
    """One line per grid source, for the setup step and the fix flow."""
    lines = []
    for source in sources:
        parts = [
            f"{label} {source[key]}"
            for key, label in (
                ("stat_energy_from", "consumption"),
                ("stat_cost", "cost"),
                ("stat_energy_to", "return to grid"),
                ("stat_compensation", "compensation"),
            )
            if source.get(key)
        ]
        lines.append("- " + ", ".join(parts))
    return "\n".join(lines)


async def async_add_to_energy(
    hass: HomeAssistant, manager: AmberManager, requested_by: str
) -> dict[str, Any]:
    """Add this entry's grid sources when the Energy dashboard has no grid source.

    An existing grid source is never modified. The preferences are saved in the Store and
    read back from disk before anything changes. The outcome is recorded in the Store.
    """
    store = manager.store
    record: dict[str, Any] = {
        "at": _utcnow().isoformat(),
        "requested_by": requested_by,
        "added": None,
        "reason": None,
        "backup": None,
    }
    energy = await async_preferences(hass)
    if energy is None:
        record["reason"] = "Energy preferences unavailable"
    elif has_grid(energy.data):
        record["reason"] = "grid_exists"
    else:
        record["backup"] = copy.deepcopy(energy.data)
        await store.async_set_energy_setup(record)
        stored = await store.async_read_back("energy_setup")
        before = list((energy.data or {}).get("energy_sources", []))
        new = grid_sources(manager)
        after = before + new
        if (stored or {}).get("backup") != record["backup"]:
            record["reason"] = "The saved copy of the preferences did not read back"
        elif error := validate_sources(after):
            record["reason"] = error
        elif await async_save_sources(energy, after, list(range(len(before), len(after))), before):
            record["added"] = new
        else:
            record["reason"] = "The change did not read back; the preferences were restored"
    await store.async_set_energy_setup(record)
    _LOGGER.info(
        "Energy dashboard setup (%s): %s",
        requested_by,
        "grid sources added" if record["added"] else record["reason"],
    )
    return record


def _issue_id(manager: AmberManager) -> str:
    return f"{ISSUE_ENERGY_NOT_USED}_{manager.entry.entry_id}"


async def async_check_energy_issue(hass: HomeAssistant, manager: AmberManager) -> None:
    """Raise or clear the "Energy dashboard isn't using Amber Energy Dashboard" issue.

    Raised 24 h after the first successful import when no energy preference references
    any of the entry's statistics; cleared once one does. Not in Pricing-only mode, and
    never again once dismissed.
    """
    store = manager.store
    now = _utcnow()
    if store.first_import_at is None and store.last_written is not None:
        await store.async_set_first_import_at(now)
    if manager.usage_mode == MODE_PRICING or store.energy_issue_dismissed:
        ir.async_delete_issue(hass, DOMAIN, _issue_id(manager))
        return
    energy = await async_preferences(hass)
    if energy is None:
        return
    if uses_statistics(energy.data, entry_statistic_ids(manager)):
        ir.async_delete_issue(hass, DOMAIN, _issue_id(manager))
        return
    first: datetime | None = store.first_import_at
    if first is None or now - first < ISSUE_DELAY:
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        _issue_id(manager),
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_ENERGY_NOT_USED,
        translation_placeholders={"entry": manager.entry.title},
        data={"entry_id": manager.entry.entry_id},
    )


async def async_listen_for_changes(hass: HomeAssistant) -> None:
    """Re-check every loaded entry whenever the energy preferences change (once per Home
    Assistant run; the energy manager has no way to stop listening)."""
    if hass.data.get(_LISTENING):
        return
    energy = await async_preferences(hass)
    if energy is None:
        return
    hass.data[_LISTENING] = True

    async def _changed() -> None:
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.state is ConfigEntryState.LOADED:
                await async_check_energy_issue(hass, entry.runtime_data.manager)

    energy.async_listen_updates(_changed)
