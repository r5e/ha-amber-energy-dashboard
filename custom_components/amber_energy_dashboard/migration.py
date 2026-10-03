"""Migration from the YAML kits (DESIGN section 14).

Recognises the published v1 kit and the advanced YAML version by name, or takes the
user's pick of legacy statistics. Before changing anything it checks parity over the
overlap window. It then copies the legacy history from before the integration's first
imported day into the new statistics, re-bases the integration's own rows onto it,
switches the Energy dashboard, and turns off the legacy automations. Every change is
recorded in the Store, so the migration resumes after an interruption and can be undone.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
import copy
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import itertools
import json
import logging
import math
import os
from statistics import median
from typing import TYPE_CHECKING, Any, Final

from homeassistant.components.automation import DATA_COMPONENT as AUTOMATIONS
from homeassistant.components.energy.data import ENERGY_SOURCE_SCHEMA, async_get_manager
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    async_list_statistic_ids,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant, valid_entity_id
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.util import dt as dt_util
import voluptuous as vol

from . import importer
from .const import (
    CHANNEL_FEED_IN,
    CHANNEL_GENERAL,
    DOMAIN,
    MODE_PRICING,
    ROLE_EXPORT_COST,
    ROLE_EXPORT_ENERGY,
    ROLE_IMPORT_COST,
    ROLE_IMPORT_ENERGY,
    ROLES,
)
from .importer import HOUR, ImportDayError, ImportRefusedError, nem_date, nem_day_start
from .statistics import CURRENCY, Metric

if TYPE_CHECKING:
    from .manager import AmberManager

_LOGGER = logging.getLogger(__name__)

STATUS_IN_PROGRESS: Final = "in_progress"
STATUS_COMPLETED: Final = "completed"
STATUS_UNDONE: Final = "undone"

RULES_HOURLY: Final = "hourly"
"""Parity rules for hourly legacy data (the advanced version): kWh exact, cost 0.01."""
RULES_DAILY: Final = "daily"
"""Parity rules for daily lumps (the v1 kit): kWh within 0.01, cost reported only."""

CHOICE_MARGIN: Final = timedelta(days=2)
"""A layout is chosen only if its data is at least this much newer than any other's."""
MIN_PARITY_DAYS: Final = 3
KWH_DIGITS: Final = 3
COST_TOLERANCE: Final = 0.01
DAILY_KWH_TOLERANCE: Final = 0.01
MAX_REPORTED_MISMATCHES: Final = 10
V1_EMPTY_COST: Final = 0.05
ENERGY_ROW_CAP: Final = 100.0
"""Implausible: more kWh than this per elapsed hour between two legacy rows (M6c), so a
large household's daily lump is never flagged."""
COST_ROW_CAP: Final = 500.0
"""Implausible: more cost than this (in the currency) per elapsed hour between two legacy
rows (M6c review), so a real wholesale price spike is never flagged. Decreases never are."""
ENERGY_DROP_TOLERANCE: Final = 0.0005
MAX_REPORTED_FLAGS: Final = 20
"""v1 cost and compensation moving less than this in total (AUD) over the copy period are
treated as empty and not copied (provisional until a real v1 install confirms it)."""
ROLE_NET: Final = "net_cost"
ISSUE_LEGACY_CLEANUP: Final = "legacy_cleanup"
_EPOCH: Final = datetime(2000, 1, 1, tzinfo=UTC)
_ENERGY_ROLES: Final = (ROLE_IMPORT_ENERGY, ROLE_EXPORT_ENERGY)
_ROLE_LABELS: Final = {
    ROLE_IMPORT_ENERGY: "grid import kWh",
    ROLE_EXPORT_ENERGY: "grid export kWh",
    ROLE_IMPORT_COST: "import cost",
    ROLE_EXPORT_COST: "export cost",
    ROLE_NET: "net cost",
}

BACKUP_WARNING: Final = (
    "Take a full Home Assistant backup before migrating. The migration keeps a copy of "
    "your Energy dashboard settings and can be undone, but it also writes statistics."
)
_PACKAGE_HINT: Final = (
    "If you installed the kit as a package, delete that single package file instead of "
    "the individual blocks."
)
_COMMON_CLEANUP: Final = (
    _PACKAGE_HINT,
    "Old statistics are kept. Delete them later with "
    "amber_energy_dashboard.delete_legacy_statistics if you wish (undo is then no longer "
    "possible).",
)


def _and(names: Sequence[str]) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


@dataclass(frozen=True, slots=True)
class Leftover:
    """One part of the YAML kit to remove by hand. ``text`` names the parts present
    (``{items}``); the Repairs issue lists it only while one of them still exists. An item
    with nothing to check (the amber_api_key secret) is advisory: it is listed in the
    migration result only, and never keeps the issue open."""

    text: str
    entities: tuple[str, ...] = ()
    """Entity IDs: present while they have a state or an entity registry entry."""
    services: tuple[str, ...] = ()
    """``domain.service`` names, such as a rest_command."""
    files: tuple[str, ...] = ()
    """File names in the configuration folder."""

    def render(self, present: Sequence[str] | None = None) -> str:
        names = list(self.entities + self.services + self.files if present is None else present)
        return self.text.format(items=_and(names)) if names else self.text


@dataclass(frozen=True, slots=True)
class Layout:
    """A known YAML-kit layout, recognised by its names."""

    key: str
    title: str
    statistics: Mapping[str, str]
    """Role -> legacy statistic ID."""
    required: tuple[str, ...]
    export_cost_sign: float
    """Multiplier turning the legacy export cost into compensation (positive = earned)."""
    rules: str
    automations: tuple[str, ...]
    markers: tuple[str, ...]
    """Strings in an automation's configuration that tie it to this layout."""
    cleanup: tuple[Leftover, ...]


V1_KIT: Final = Layout(
    key="v1",
    title="v1 kit",
    statistics={
        ROLE_IMPORT_ENERGY: "sensor.amber_energy_import",
        ROLE_EXPORT_ENERGY: "sensor.amber_energy_export",
        ROLE_IMPORT_COST: "sensor.amber_energy_import_cost",
        ROLE_EXPORT_COST: "sensor.amber_energy_export_compensation",
    },
    required=(ROLE_IMPORT_ENERGY, ROLE_EXPORT_ENERGY),
    export_cost_sign=1.0,  # HA-generated compensation already uses HA's sign
    rules=RULES_DAILY,
    automations=("automation.amber_usage_daily_statistics_import",),
    markers=(
        "input_number.amber_energy_import_running_total",
        "input_number.amber_energy_export_running_total",
    ),
    cleanup=(
        Leftover(
            "Helpers {items} (configuration.yaml, input_number:).",
            entities=(
                "input_number.amber_energy_import_running_total",
                "input_number.amber_energy_export_running_total",
            ),
        ),
        Leftover(
            "REST sensors {items} (configuration.yaml, the rest: block with the Amber usage URL).",
            entities=("sensor.amber_daily_grid_import", "sensor.amber_daily_grid_export"),
        ),
        Leftover(
            "Template sensors {items} (configuration.yaml, template:), and their lines under "
            "recorder: exclude: entities:.",
            entities=("sensor.amber_energy_import", "sensor.amber_energy_export"),
        ),
        Leftover("The amber_api_key line in secrets.yaml, if nothing else uses it."),
        Leftover(
            "{items}, wherever you ran the backfill.",
            files=("amber_backfill.py", "amber_backfill_cache.json"),
        ),
    ),
)
ADVANCED: Final = Layout(
    key="advanced",
    title="advanced YAML version",
    statistics={
        ROLE_IMPORT_ENERGY: "sensor.amber_cumulative_grid_import_v2",
        ROLE_EXPORT_ENERGY: "sensor.amber_cumulative_grid_export_v2",
        ROLE_IMPORT_COST: "sensor.amber_hourly_cost_import",
        ROLE_EXPORT_COST: "sensor.amber_hourly_cost_export",
    },
    required=ROLES,
    export_cost_sign=-1.0,  # Amber's sign: negative when earned
    rules=RULES_HOURLY,
    automations=("automation.amber_daily_statistics_import",),
    markers=(
        "script.amber_daily_import",
        "script.amber_retention_probe",
        "input_text.amber_last_imported_date",
    ),
    cleanup=(
        Leftover(
            "Helpers {items}.",
            entities=(
                "input_number.amber_lifetime_grid_import_v2",
                "input_number.amber_lifetime_grid_export_v2",
                "input_number.amber_lifetime_cost_import",
                "input_number.amber_lifetime_cost_export",
                "input_number.amber_retention_days",
                "input_number.amber_nodata_patience_days",
                "input_text.amber_last_imported_date",
                "input_text.amber_nodata_tracker",
            ),
        ),
        Leftover(
            "Scripts {items} (scripts.yaml).",
            entities=(
                "script.amber_daily_import",
                "script.amber_retention_probe",
                "script.amber_window_rebuild",
            ),
        ),
        Leftover(
            "{items} (configuration.yaml, rest_command:).",
            services=("rest_command.amber_fetch_usage",),
        ),
        Leftover(
            "Template sensors {items} (configuration.yaml, template:), and their lines under "
            "recorder: exclude: entities:.",
            entities=(
                "sensor.amber_cumulative_grid_import_v2",
                "sensor.amber_cumulative_grid_export_v2",
                "sensor.amber_hourly_cost_import",
                "sensor.amber_hourly_cost_export",
            ),
        ),
        Leftover("The amber_api_key line in secrets.yaml, if nothing else uses it."),
    ),
)
LAYOUTS: Final = (V1_KIT, ADVANCED)
_BY_KEY: Final = {layout.key: layout for layout in LAYOUTS}
_MANUAL_CLEANUP: Final = (
    "Your legacy automations, helpers, scripts and YAML blocks: remove them once you are "
    "satisfied with the migration.",
)


class MigrationRefused(HomeAssistantError):
    """The migration will not run (nothing was changed by this call)."""

    def __init__(self, reason: str, message: str, report: dict[str, Any] | None = None) -> None:
        """Keep a machine-readable reason and the report."""
        super().__init__(message)
        self.reason = reason
        self.report = report


# --- reading statistics ------------------------------------------------------------------


async def _read(
    hass: HomeAssistant, ids: Iterable[str], start: datetime = _EPOCH, end: datetime | None = None
) -> dict[str, list[dict[str, Any]]]:
    ids = [i for i in ids if i]
    raw = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, end, set(ids), "hour", None, {"state", "sum"}
    )
    return {
        sid: [
            {
                "start": datetime.fromtimestamp(r["start"], UTC),
                "state": r.get("state"),
                "sum": float(r["sum"]),
            }
            for r in raw.get(sid, [])
            if r.get("sum") is not None
        ]
        for sid in ids
    }


async def _metadata(hass: HomeAssistant, ids: Iterable[str] | None = None) -> dict[str, dict]:
    if ids is not None:
        items = await async_list_statistic_ids(hass, set(ids))
    else:
        items = await async_list_statistic_ids(hass, statistic_type="sum")
    return {item["statistic_id"]: item for item in items}


def _currencies(hass: HomeAssistant) -> set[str]:
    return {CURRENCY, hass.config.currency}


async def async_pick_options(hass: HomeAssistant) -> tuple[list[str], list[str]]:
    """Statistics offered in the manual step: (kWh statistics, currency statistics)."""
    meta = await _metadata(hass)
    energy, cost = [], []
    for sid, item in meta.items():
        if item.get("source") == DOMAIN:
            continue
        unit = item.get("statistics_unit_of_measurement")
        if unit == UnitOfEnergy.KILO_WATT_HOUR:
            energy.append(sid)
        elif unit in _currencies(hass):
            cost.append(sid)
    return sorted(energy), sorted(cost)


# --- detection ---------------------------------------------------------------------------


async def async_detect(hass: HomeAssistant) -> dict[str, Any]:
    """Find the known layouts and choose the active one, if that is safe."""
    ids = {sid for layout in LAYOUTS for sid in layout.statistics.values()}
    latest = await importer.async_latest_hours(hass, ids)
    candidates: list[dict[str, Any]] = []
    for layout in LAYOUTS:
        found = {role: sid for role, sid in layout.statistics.items() if latest.get(sid)}
        last = latest.get(layout.statistics[ROLE_IMPORT_ENERGY])
        if last is None:
            continue
        candidates.append(
            {
                "layout": layout.key,
                "title": layout.title,
                "last": last,
                "last_data": dt_util.as_local(last).date().isoformat(),
                "found": found,
                "missing": [role for role in layout.required if role not in found],
            }
        )
    result: dict[str, Any] = {"chosen": None, "ignored": [], "reason": None}
    if not candidates:
        result["reason"] = "No statistics of a known YAML kit were found."
    else:
        ranked = sorted(candidates, key=lambda c: c["last"], reverse=True)
        best, others = ranked[0], ranked[1:]
        if best["missing"]:
            result["reason"] = (
                f"The {best['title']} is incomplete: no data for "
                f"{', '.join(_ROLE_LABELS[r] for r in best['missing'])}."
            )
        elif any(best["last"] - other["last"] < CHOICE_MARGIN for other in others):
            result["reason"] = "More than one YAML kit has recent data, so the source is ambiguous."
        else:
            result["chosen"] = best["layout"]
            result["ignored"] = [
                {
                    "layout": other["layout"],
                    "title": other["title"],
                    "reason": f"last data {other['last_data']}, older than the "
                    f"{best['title']} ({best['last_data']})"
                    + (
                        f"; no data for {', '.join(_ROLE_LABELS[r] for r in other['missing'])}"
                        if other["missing"]
                        else ""
                    ),
                }
                for other in others
            ]
    result["candidates"] = [{k: v for k, v in c.items() if k != "last"} for c in candidates]
    return result


def _layout_sources(layout: Layout, found: Mapping[str, str]) -> dict[str, Any]:
    return {
        "layout": layout.key,
        **{role: found.get(role) for role in ROLES},
        "export_cost_sign": layout.export_cost_sign,
        "rules": layout.rules,
    }


async def _manual_sources(hass: HomeAssistant, picks: Mapping[str, str | None]) -> dict[str, Any]:
    chosen = {role: picks.get(role) or None for role in ROLES}
    if not chosen[ROLE_IMPORT_ENERGY]:
        raise MigrationRefused("invalid_pick", "Pick the legacy grid import kWh statistic.")
    picked = [sid for sid in chosen.values() if sid]
    if len(set(picked)) != len(picked):
        raise MigrationRefused("invalid_pick", "Each legacy statistic can be picked only once.")
    meta = await _metadata(hass, picked)
    latest = await importer.async_latest_hours(hass, picked)
    for role, sid in chosen.items():
        if not sid:
            continue
        item = meta.get(sid)
        if item is None or latest.get(sid) is None:
            raise MigrationRefused("invalid_pick", f"{sid} has no statistics.")
        if item.get("source") == DOMAIN:
            raise MigrationRefused("invalid_pick", f"{sid} belongs to this integration.")
        unit = item.get("statistics_unit_of_measurement")
        if role in _ENERGY_ROLES and unit != UnitOfEnergy.KILO_WATT_HOUR:
            raise MigrationRefused("invalid_pick", f"{sid} is not in kWh ({unit}).")
        if role not in _ENERGY_ROLES and unit not in _currencies(hass):
            raise MigrationRefused("invalid_pick", f"{sid} is not in {CURRENCY} ({unit}).")
    return {"layout": "manual", **chosen, "export_cost_sign": None, "rules": None}


# --- planning ----------------------------------------------------------------------------


def _targets(manager: AmberManager) -> tuple[dict[str, str], list[str]]:
    channels = manager.ctx.channels
    general = [c for c in channels if c.type == CHANNEL_GENERAL]
    feed_in = [c for c in channels if c.type == CHANNEL_FEED_IN]
    problems = []
    if len(general) != 1:
        problems.append(
            f"The site has {len(general)} general channels; the migration needs exactly one."
        )
    if len(feed_in) > 1:
        problems.append(f"The site has {len(feed_in)} feed-in channels; at most one is supported.")
    specs = {(s.channel, s.metric): s.statistic_id for s in manager.ctx.specs}
    targets = {ROLE_NET: specs[(None, Metric.NET_COST)]}
    if general:
        targets[ROLE_IMPORT_ENERGY] = specs[(general[0].identifier, Metric.ENERGY)]
        targets[ROLE_IMPORT_COST] = specs[(general[0].identifier, Metric.COST)]
    if feed_in:
        targets[ROLE_EXPORT_ENERGY] = specs[(feed_in[0].identifier, Metric.ENERGY)]
        targets[ROLE_EXPORT_COST] = specs[(feed_in[0].identifier, Metric.COMPENSATION)]
    return targets, problems


def _legacy_daily(rows: Sequence[Mapping[str, Any]], rules: str, tz: Any) -> dict[date, float]:
    """Daily totals from cumulative sums: end-of-day sum minus the previous day's.

    Hourly data is assigned to NEM days; daily lumps (one row at local midnight holding
    the total through the end of that day) to their local date.
    """
    ends: dict[date, float] = {}
    for row in rows:
        ends[_legacy_day(row["start"], rules, tz)] = row["sum"]
    days = sorted(ends)
    return {
        day: ends[day] - ends[prev]
        for prev, day in itertools.pairwise(days)
        if (day - prev).days == 1
    }


def _legacy_day(start: datetime, rules: str, tz: Any) -> date:
    return nem_date(start) if rules == RULES_HOURLY else start.astimezone(tz).date()


def _legacy_span(rows: Sequence[Mapping[str, Any]], rules: str, tz: Any) -> tuple[date, date]:
    """The days a legacy series can be compared on: from the day after its first row (the
    first day has no earlier total to start from) to its last row's day."""
    first = _legacy_day(rows[0]["start"], rules, tz)
    return first + timedelta(days=1), _legacy_day(rows[-1]["start"], rules, tz)


def _new_daily(rows: Sequence[Mapping[str, Any]], first: date, last: date) -> dict[date, float]:
    by_day: dict[date, list[float]] = defaultdict(list)
    for row in rows:
        by_day[nem_date(row["start"])].append(float(row["state"] or 0.0))
    return {
        day: math.fsum(values)
        for day, values in by_day.items()
        if len(values) == importer.DAY_HOURS and first <= day <= last
    }


def _detect_rules(rows: Sequence[Mapping[str, Any]], boundary: datetime, tz: Any) -> str:
    per_day: dict[date, int] = defaultdict(int)
    for row in rows:
        if row["start"] >= boundary:
            per_day[row["start"].astimezone(tz).date()] += 1
    if per_day and median(per_day.values()) >= 12:
        return RULES_HOURLY
    return RULES_DAILY


def _parity(
    legacy: Mapping[str, dict[date, float]],
    new: Mapping[str, dict[date, float]],
    rules: str,
    sign: float,
    spans: Mapping[str, tuple[date, date]],
) -> dict[str, Any]:
    """Compare daily totals. Each statistic is compared inside its own legacy span (a cost
    series that starts later is not a mismatch before it starts); a gap inside is."""
    days = sorted(set(legacy[ROLE_IMPORT_ENERGY]) & set(new[ROLE_IMPORT_ENERGY]))
    mismatches: list[dict[str, Any]] = []
    cost_totals: dict[str, list[float]] = {}
    compared: dict[str, int] = defaultdict(int)
    for day in days:
        for role in ROLES:
            if role not in legacy or not spans[role][0] <= day <= spans[role][1]:
                continue
            compared[_ROLE_LABELS[role]] += 1
            old, ours = legacy[role].get(day), new[role].get(day)
            energy = role in _ENERGY_ROLES
            if old is None or ours is None:
                if energy or rules == RULES_HOURLY:
                    mismatches.append(
                        {
                            "day": day.isoformat(),
                            "statistic": _ROLE_LABELS[role],
                            "legacy": old,
                            "new": ours,
                            "problem": "missing on one side",
                        }
                    )
                continue
            if role == ROLE_EXPORT_COST:
                old *= sign
            if energy:
                ok = (
                    round(old, KWH_DIGITS) == round(ours, KWH_DIGITS)
                    if rules == RULES_HOURLY
                    else abs(old - ours) <= DAILY_KWH_TOLERANCE
                )
            else:
                totals = cost_totals.setdefault(_ROLE_LABELS[role], [0.0, 0.0])
                totals[0] += old
                totals[1] += ours
                ok = rules == RULES_DAILY or abs(abs(old) - abs(ours)) <= COST_TOLERANCE
            if not ok:
                mismatches.append(
                    {
                        "day": day.isoformat(),
                        "statistic": _ROLE_LABELS[role],
                        "legacy": round(old, 6),
                        "new": round(ours, 6),
                        "difference": round(ours - old, 6),
                    }
                )
    passed = len(days) >= MIN_PARITY_DAYS and not mismatches
    return {
        "rules": rules,
        "days": len(days),
        "first": days[0].isoformat() if days else None,
        "last": days[-1].isoformat() if days else None,
        "passed": passed,
        "compared": dict(compared),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches[:MAX_REPORTED_MISMATCHES],
        "cost_totals": {
            label: {"legacy": round(v[0], 4), "new": round(v[1], 4)}
            for label, v in cost_totals.items()
        },
        "reason": None
        if passed
        else (
            f"only {len(days)} comparable days (at least {MIN_PARITY_DAYS} needed)"
            if len(days) < MIN_PARITY_DAYS
            else f"{len(mismatches)} daily totals differ"
        ),
    }


def _copy_rows(
    rows: Sequence[Mapping[str, Any]], sign: float, boundary: datetime
) -> list[dict[str, Any]]:
    """Legacy rows before the boundary: sums kept (times ``sign``), states recomputed, and
    a carry row in the hour before the boundary."""
    out: list[dict[str, Any]] = []
    previous: float | None = None
    for row in rows:
        if row["start"] >= boundary:
            break
        value = sign * row["sum"]
        state = 0.0 if previous is None else value - previous
        out.append({"start": row["start"], "state": round(state, 6), "sum": round(value, 6)})
        previous = value
    if out and out[-1]["start"] != boundary - HOUR:
        out.append({"start": boundary - HOUR, "state": 0.0, "sum": out[-1]["sum"]})
    return out


def _net_rows(
    cost: Sequence[Mapping[str, Any]], compensation: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Net cost = import cost - compensation, on the union of their hours."""
    if not cost and not compensation:
        return []
    starts = sorted({r["start"] for r in cost} | {r["start"] for r in compensation})
    if cost and compensation:
        first = max(cost[0]["start"], compensation[0]["start"])
        starts = [s for s in starts if s >= first]
    cost_at = {r["start"]: r["sum"] for r in cost}
    comp_at = {r["start"]: r["sum"] for r in compensation}
    out: list[dict[str, Any]] = []
    c = p = 0.0
    previous: float | None = None
    for start in starts:
        c = cost_at.get(start, c)
        p = comp_at.get(start, p)
        value = c - p
        state = 0.0 if previous is None else value - previous
        out.append({"start": start, "state": round(state, 6), "sum": round(value, 6)})
        previous = value
    return out


def _find_automations(
    hass: HomeAssistant, layout: Layout | None, sources: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """The legacy automations: known entity IDs, or configuration that mentions the layout's
    helpers and scripts. For a manual pick: automations that import statistics into one of
    the picked statistics."""
    component = hass.data.get(AUTOMATIONS)
    picked = [sources.get(role) for role in ROLES if sources.get(role)]
    found = []
    for entity in list(component.entities) if component else []:
        raw = json.dumps(getattr(entity, "raw_config", None) or {}, default=str)
        if layout is not None:
            match = entity.entity_id in layout.automations or any(
                marker in raw for marker in layout.markers
            )
        else:
            match = "import_statistics" in raw and any(sid in raw for sid in picked)
        if match:
            state = hass.states.get(entity.entity_id)
            found.append(
                {
                    "entity_id": entity.entity_id,
                    "name": state.name if state else entity.entity_id,
                    "state": state.state if state else "unknown",
                }
            )
    return sorted(found, key=lambda a: a["entity_id"])


async def _energy_plan(
    hass: HomeAssistant, sources: Mapping[str, Any], targets: Mapping[str, str]
) -> dict[str, Any]:
    """The Energy dashboard change: before and after of each grid source that uses the
    legacy statistics, validated against HA's schema."""
    instructions = _energy_instructions(sources, targets)
    try:
        manager = await async_get_manager(hass)
    except Exception as err:  # any failure means "not safely updatable"
        return {
            "available": False,
            "changed": False,
            "safe": False,
            "reason": f"Energy preferences unavailable: {err}",
            "instructions": instructions,
        }
    if manager.data is None:
        return {
            "available": False,
            "changed": False,
            "safe": False,
            "reason": "No Energy dashboard preferences are saved.",
            "instructions": instructions,
        }
    before = copy.deepcopy(manager.data.get("energy_sources", []))
    after = copy.deepcopy(before)
    changed: list[int] = []
    for index, source in enumerate(after):
        if source.get("type") != "grid":
            continue
        if source.get("stat_energy_from") == sources[ROLE_IMPORT_ENERGY]:
            source["stat_energy_from"] = targets[ROLE_IMPORT_ENERGY]
            source["stat_cost"] = targets[ROLE_IMPORT_COST]
            source["entity_energy_price"] = None
            source["number_energy_price"] = None
            changed.append(index)
        export = sources.get(ROLE_EXPORT_ENERGY)
        if export and ROLE_EXPORT_ENERGY in targets and source.get("stat_energy_to") == export:
            source["stat_energy_to"] = targets[ROLE_EXPORT_ENERGY]
            source["stat_compensation"] = targets[ROLE_EXPORT_COST]
            source["entity_energy_price_export"] = None
            source["number_energy_price_export"] = None
            if index not in changed:
                changed.append(index)
    legacy_ids = {sources.get(role) for role in ROLES} - {None}
    remaining = json.dumps({**manager.data, "energy_sources": after}, default=str)
    other_uses = sorted(sid for sid in legacy_ids if f'"{sid}"' in remaining)
    plan: dict[str, Any] = {
        "available": True,
        "changed": bool(changed),
        "indexes": changed,
        "before": [before[i] for i in changed],
        "after": [after[i] for i in changed],
        "sources_after": after,
        "other_uses": other_uses,
        "safe": True,
        "reason": None if changed else "The Energy dashboard does not use the legacy statistics.",
        "instructions": instructions,
    }
    try:
        ENERGY_SOURCE_SCHEMA(copy.deepcopy(after))
    except vol.Invalid as err:
        plan["safe"] = False
        plan["reason"] = f"The changed preferences do not validate: {err}"
    return plan


def _energy_instructions(sources: Mapping[str, Any], targets: Mapping[str, str]) -> list[str]:
    lines = [
        "Settings > Dashboards > Energy > Electricity grid: edit the grid connection.",
        f"Grid consumption: replace {sources[ROLE_IMPORT_ENERGY]} with "
        f"{targets.get(ROLE_IMPORT_ENERGY)}, and for cost choose 'Use an entity tracking the "
        f"total costs' with {targets.get(ROLE_IMPORT_COST)}.",
    ]
    if sources.get(ROLE_EXPORT_ENERGY) and ROLE_EXPORT_ENERGY in targets:
        lines.append(
            f"Return to grid: replace {sources[ROLE_EXPORT_ENERGY]} with "
            f"{targets[ROLE_EXPORT_ENERGY]}, and for compensation choose 'Use an entity "
            f"tracking the total received' with {targets[ROLE_EXPORT_COST]}."
        )
    return lines


@dataclass(slots=True)
class _Plan:
    sources: dict[str, Any]
    layout: Layout | None
    targets: dict[str, str]
    boundary: datetime | None
    copy: dict[str, list[dict[str, Any]]]
    parity: dict[str, Any] | None
    energy: dict[str, Any] | None
    automations: list[dict[str, Any]]
    cleanup: list[str]
    problems: list[str]
    notes: list[str]
    flagged: list[dict[str, Any]] | None = None


# --- implausible legacy rows (DESIGN section 14) -----------------------------------------


def _scan_series(
    rows: Sequence[Mapping[str, Any]], energy: bool
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Flag implausible rows of one legacy series; return (flags, cleaned rows).

    Each row's step is its sum minus the last good row's sum. Energy: a step above
    ENERGY_ROW_CAP per elapsed hour between the two rows (at least one hour's cap), or
    any decrease.
    Cost: a step above COST_ROW_CAP per elapsed hour between the two rows (at least one
    hour's cap). A cost decrease is never flagged, since negative prices make the cost
    fall legitimately. One row of lookahead tells the kinds apart:
    - a *spike* (the next row is plausible again from the last good row, as with the v1
      README's 99999 test value): the row is left out and nothing else changes;
    - a *jump* or *reset* (the series continues from the new level): the row is left
      out and every later sum is shifted by the step, so the history is re-derived
      around it.
    The first row has no earlier total and is never flagged.
    """
    flags: list[dict[str, Any]] = []
    cleaned: list[dict[str, Any]] = []
    if not rows:
        return flags, cleaned
    base = rows[0]
    offset = 0.0
    cleaned.append(dict(rows[0]))

    def bad(step: float, spacing: timedelta) -> str | None:
        cap = (ENERGY_ROW_CAP if energy else COST_ROW_CAP) * max(1.0, spacing / timedelta(hours=1))
        if energy and step < -ENERGY_DROP_TOLERANCE:
            return "decrease"
        if step > cap:
            return "above cap"
        return None

    for i, row in enumerate(rows[1:], start=1):
        step = row["sum"] - base["sum"]
        problem = bad(step, row["start"] - base["start"])
        if problem is None:
            base = row
            cleaned.append({**row, "sum": row["sum"] + offset})
            continue
        following = rows[i + 1] if i + 1 < len(rows) else None
        spike = following is None or (
            bad(following["sum"] - base["sum"], following["start"] - base["start"]) is None
        )
        if spike:
            kind = "spike"
        else:
            kind = "reset" if problem == "decrease" else "jump"
            offset -= step
            base = row
        flags.append(
            {
                "start": row["start"].isoformat(),
                "sum": round(row["sum"], 6),
                "step": round(step, 6),
                "kind": kind,
                "problem": (
                    f"sum decreases by {round(-step, 6)}"
                    if problem == "decrease"
                    else f"step of {round(step, 6)} in one row"
                ),
            }
        )
    return flags, cleaned


def _scan_legacy(
    legacy: Mapping[str, list[dict[str, Any]]],
    legacy_ids: Mapping[str, str],
    boundary: datetime,
    last_day: date,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """Scan every legacy series; return the flags (with where each row lies) and the
    cleaned series."""
    flags: list[dict[str, Any]] = []
    cleaned: dict[str, list[dict[str, Any]]] = {}
    end = nem_day_start(last_day + timedelta(days=1))
    for role, sid in legacy_ids.items():
        found, cleaned[sid] = _scan_series(legacy[sid], role in _ENERGY_ROLES)
        for flag in found:
            start = datetime.fromisoformat(flag["start"])
            where = "copy" if start < boundary else "overlap" if start < end else "after"
            flags.append({"statistic": sid, "role": _ROLE_LABELS[role], "where": where, **flag})
    return flags, cleaned


def _preconditions(
    manager: AmberManager, sources: Mapping[str, Any], targets: Mapping[str, str]
) -> list[str]:
    store = manager.store
    problems = []
    if manager.usage_mode == MODE_PRICING:
        problems.append("Usage statistics are not written in Pricing-only mode.")
    if store.last_written is None:
        problems.append("The integration has not imported any data yet; wait for its first run.")
    if store.pending is not None:
        problems.append(f"A write of {store.pending} was interrupted; let a run recover it first.")
    if store.tail_rewrite is not None:
        problems.append("A range rewrite is in progress; let a run finish it first.")
    if any(sources.get(role) and role not in targets for role in ROLES):
        problems.append("The legacy data has grid export, but the site has no feed-in channel.")
    return problems


async def _unit_problems(hass: HomeAssistant, legacy_ids: Mapping[str, str]) -> list[str]:
    meta = await _metadata(hass, legacy_ids.values())
    problems = []
    for role, sid in legacy_ids.items():
        unit = (meta.get(sid) or {}).get("statistics_unit_of_measurement")
        wanted = {UnitOfEnergy.KILO_WATT_HOUR} if role in _ENERGY_ROLES else _currencies(hass)
        if unit not in wanted:
            problems.append(f"{sid} is in {unit}, expected {' or '.join(sorted(wanted))}.")
    return problems


async def _boundary(
    hass: HomeAssistant,
    manager: AmberManager,
    targets: Mapping[str, str],
    record: Mapping[str, Any] | None,
) -> tuple[datetime, list[str]]:
    """Where the integration's own data begins: stored by an earlier run, else its first row."""
    if record is not None and record.get("boundary"):
        return datetime.fromisoformat(record["boundary"]), []
    rows = (await _read(hass, [targets[ROLE_IMPORT_ENERGY]]))[targets[ROLE_IMPORT_ENERGY]]
    boundary = rows[0]["start"]
    problems = []
    if boundary != nem_day_start(nem_date(boundary)):
        problems.append(f"The integration's first row ({boundary}) is not a NEM day start.")
    return boundary, problems


def _export_sign(
    sources: Mapping[str, Any],
    legacy_daily: Mapping[str, dict[date, float]],
    new_daily: Mapping[str, dict[date, float]],
    notes: list[str],
) -> float:
    """The layout's sign, or for a manual pick the sign that matches the new compensation."""
    if sources.get("export_cost_sign") is not None:
        return float(sources["export_cost_sign"])
    if ROLE_EXPORT_COST not in legacy_daily:
        return -1.0
    old, new = legacy_daily[ROLE_EXPORT_COST], new_daily[ROLE_EXPORT_COST]
    dot = math.fsum(old[d] * new[d] for d in set(old) & set(new))
    sign = 1.0 if dot > 0 else -1.0
    notes.append(
        "Export cost sign: "
        + ("negated (Amber's sign)" if sign < 0 else "kept (already positive when earned)")
        + (" (from the overlap)" if dot else " (assumed; no overlap to decide from)")
    )
    return sign


def _plan_copy(
    plan: _Plan, legacy: Mapping[str, list[dict[str, Any]]], legacy_ids: Mapping[str, str]
) -> None:
    assert plan.boundary is not None
    sign = plan.sources["export_cost_sign"]
    for role, sid in legacy_ids.items():
        rows = _copy_rows(legacy[sid], sign if role == ROLE_EXPORT_COST else 1.0, plan.boundary)
        if rows:
            plan.copy[plan.targets[role]] = rows
    if plan.layout is V1_KIT:
        cost_ids = [
            plan.targets[role]
            for role in (ROLE_IMPORT_COST, ROLE_EXPORT_COST)
            if plan.targets.get(role) in plan.copy
        ]
        magnitude = math.fsum(abs(r["state"]) for sid in cost_ids for r in plan.copy[sid])
        if cost_ids and magnitude < V1_EMPTY_COST:
            for sid in cost_ids:
                del plan.copy[sid]
            plan.notes.append("v1 cost history appears empty; not copied.")
    net = _net_rows(
        plan.copy.get(plan.targets.get(ROLE_IMPORT_COST, ""), []),
        plan.copy.get(plan.targets.get(ROLE_EXPORT_COST, ""), []),
    )
    if net:
        plan.copy[plan.targets[ROLE_NET]] = net


async def _async_plan(
    hass: HomeAssistant,
    manager: AmberManager,
    sources: dict[str, Any],
    record: Mapping[str, Any] | None,
) -> _Plan:
    layout = _BY_KEY.get(sources["layout"])
    targets, problems = _targets(manager)
    problems += _preconditions(manager, sources, targets)
    plan = _Plan(sources, layout, targets, None, {}, None, None, [], [], problems, [])
    if problems:
        return plan
    legacy_ids = {role: sources[role] for role in ROLES if sources.get(role)}
    problems += await _unit_problems(hass, legacy_ids)
    plan.boundary, more = await _boundary(hass, manager, targets, record)
    problems += more
    if problems:
        return plan

    tz = dt_util.get_time_zone(hass.config.time_zone)
    legacy = await _read(hass, legacy_ids.values())
    ours = await _read(hass, targets.values(), plan.boundary)
    last_written = manager.store.last_written
    assert last_written is not None
    plan.flagged, cleaned = _scan_legacy(legacy, legacy_ids, plan.boundary, last_written)
    if plan.flagged and sources.get("exclude_flagged"):
        legacy = cleaned
        plan.notes.append(
            f"{len(plan.flagged)} implausible legacy rows are left out; the sums around "
            "them are re-derived."
        )
    elif plan.flagged:
        problems.append(
            f"{len(plan.flagged)} implausible legacy rows (see 'flagged'), possible "
            "corruption such as a test spike. Nothing was changed. Run the migration with "
            "exclude_flagged to leave them out and re-derive the sums around them."
        )
    rules = sources.get("rules") or _detect_rules(
        legacy[legacy_ids[ROLE_IMPORT_ENERGY]], plan.boundary, tz
    )
    first_day, last_day = nem_date(plan.boundary), manager.store.last_written
    assert last_day is not None
    legacy_daily = {role: _legacy_daily(legacy[sid], rules, tz) for role, sid in legacy_ids.items()}
    new_daily = {role: _new_daily(ours[targets[role]], first_day, last_day) for role in legacy_ids}
    sign = _export_sign(sources, legacy_daily, new_daily, plan.notes)
    plan.sources = {**sources, "rules": rules, "export_cost_sign": sign}
    spans = {role: _legacy_span(legacy[sid], rules, tz) for role, sid in legacy_ids.items()}
    plan.parity = _parity(legacy_daily, new_daily, rules, sign, spans)
    _plan_copy(plan, legacy, legacy_ids)
    if rules == RULES_DAILY:
        cost_copied = any(
            plan.targets.get(role) in plan.copy
            for role in (ROLE_IMPORT_COST, ROLE_EXPORT_COST, ROLE_NET)
        )
        plan.notes.append(
            "The copied history is approximate: daily totals"
            + (", approximate cost." if cost_copied else " (no cost history copied).")
        )
    plan.energy = await _energy_plan(hass, sources, targets)
    plan.automations = _find_automations(hass, layout, sources)
    listed = [item.render() for item in layout.cleanup] if layout else list(_MANUAL_CLEANUP)
    plan.cleanup = listed + list(_COMMON_CLEANUP)
    return plan


def _report(
    plan: _Plan | None, detection: Mapping[str, Any], *, dry_run: bool, status: str | None
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "dry_run": dry_run,
        "migration_status": status,
        "warning": BACKUP_WARNING,
        "detection": dict(detection),
    }
    if plan is None:
        report["needs_manual_pick"] = True
        return report
    energy = dict(plan.energy or {})
    energy.pop("sources_after", None)
    energy.pop("indexes", None)
    report.update(
        {
            "needs_manual_pick": False,
            "layout": plan.sources["layout"],
            "sources": plan.sources,
            "targets": plan.targets,
            "boundary": plan.boundary.isoformat() if plan.boundary else None,
            "copy": {
                sid: {
                    "rows": len(rows),
                    "from": rows[0]["start"].isoformat(),
                    "to": rows[-1]["start"].isoformat(),
                    "baseline": rows[-1]["sum"],
                }
                for sid, rows in plan.copy.items()
            },
            "parity": plan.parity,
            "energy": energy,
            "automations": plan.automations,
            "cleanup": _cleanup_lines(plan.automations, plan.cleanup),
            "problems": plan.problems,
            "notes": plan.notes,
            "flagged": {
                "count": len(plan.flagged or []),
                "excluded": bool(plan.sources.get("exclude_flagged")) and bool(plan.flagged),
                "blocking": bool(plan.flagged) and not plan.sources.get("exclude_flagged"),
                "rows": (plan.flagged or [])[:MAX_REPORTED_FLAGS],
            },
        }
    )
    return report


def _cleanup_lines(automations: Sequence[Mapping[str, Any]], cleanup: Sequence[str]) -> list[str]:
    lines = [
        f"Automation {a['entity_id']}: turned off by the migration; delete it when you are "
        "satisfied."
        if a.get("disabled_by_migration") or a.get("state") == "on"
        else f"Automation {a['entity_id']}: already off; delete it when you are satisfied."
        for a in automations
    ]
    return lines + list(cleanup)


# --- running -----------------------------------------------------------------------------


def _now() -> str:
    return dt_util.utcnow().isoformat()


def _change(record: dict[str, Any], action: str, details: Any) -> None:
    record.setdefault("changes", []).append({"at": _now(), "action": action, "details": details})


async def async_migrate(
    hass: HomeAssistant,
    manager: AmberManager,
    *,
    dry_run: bool = True,
    confirm_backup: bool = False,
    picks: Mapping[str, str | None] | None = None,
    exclude_flagged: bool = False,
) -> dict[str, Any]:
    """Dry-run or run the migration. Raises MigrationRefused when it will not run.

    ``exclude_flagged`` leaves implausible legacy rows out (see ``_scan_series``);
    without it, any such row stops the real run. A run in progress keeps its choice.
    """
    store = manager.store
    record = copy.deepcopy(store.migration)
    status = record["status"] if record else None
    if status == STATUS_COMPLETED and not dry_run:
        raise MigrationRefused(
            "already_migrated",
            f"The migration was completed on {record['finished'][:10]}. Undo it first to run "
            "it again.",
        )
    detection = await async_detect(hass)
    picks = {role: v for role, v in (picks or {}).items() if v}
    if status == STATUS_IN_PROGRESS:
        sources = record["sources"]
        if picks and any(picks.get(role) != sources.get(role) for role in picks):
            raise MigrationRefused(
                "in_progress",
                "A migration with other statistics is in progress; run it again without "
                "picks to finish it, or undo it.",
            )
    elif picks:
        sources = await _manual_sources(hass, picks)
    elif detection["chosen"]:
        layout = _BY_KEY[detection["chosen"]]
        found = next(c["found"] for c in detection["candidates"] if c["layout"] == layout.key)
        sources = _layout_sources(layout, found)
    else:
        report = _report(None, detection, dry_run=dry_run, status=status)
        if dry_run:
            return report
        raise MigrationRefused("needs_manual_pick", detection["reason"], report)

    if status != STATUS_IN_PROGRESS:
        sources = {**sources, "exclude_flagged": exclude_flagged}
    plan = await _async_plan(hass, manager, sources, record)
    report = _report(plan, detection, dry_run=dry_run, status=status)
    if dry_run:
        return report
    if plan.problems:
        raise MigrationRefused("plan_refused", " ".join(plan.problems), report)
    assert plan.parity is not None
    if not plan.parity["passed"]:
        raise MigrationRefused(
            "parity_failed",
            f"Parity check failed ({plan.parity['reason']}); nothing was changed.",
            report,
        )
    if not confirm_backup:
        raise MigrationRefused(
            "backup_not_confirmed",
            "Confirm that you have a current Home Assistant backup (confirm_backup).",
            report,
        )
    report["result"] = await _async_execute(hass, manager, plan)
    report["migration_status"] = manager.store.migration["status"]
    report["cleanup"] = _cleanup_lines(manager.store.migration["automations"], plan.cleanup)
    return report


async def _async_execute(hass: HomeAssistant, manager: AmberManager, plan: _Plan) -> dict:
    """Run (or resume) the steps in order; each finished step is recorded in the Store."""
    store = manager.store
    async with manager.ctx.lock:
        record = await _async_begin(hass, store, plan)
        steps = record["steps"]
        if "copy" not in steps:
            await _async_copy(hass, manager, plan)
            steps["copy"] = {
                sid: {"rows": len(rows), "baseline": rows[-1]["sum"]}
                for sid, rows in plan.copy.items()
            }
            _change(record, "copy", steps["copy"])
            await store.async_set_migration(record)
        if "rebase" not in steps:
            paused = await _async_rebase_step(hass, manager, plan, record)
            if paused is not None:
                return {"copy": steps["copy"], "paused": paused}
        if "energy" not in steps:
            steps["energy"] = await _async_apply_energy(hass, plan, record)
            _change(record, "energy", steps["energy"])
            await store.async_set_migration(record)
        if "automations" not in steps:
            await _async_turn_off_automations(hass, plan, record)
            await store.async_set_migration(record)
        await _async_finish(hass, manager, plan, record)
        return {key: steps[key] for key in ("copy", "rebase", "energy", "automations")}


async def _async_begin(hass: HomeAssistant, store: Any, plan: _Plan) -> dict[str, Any]:
    """Start a new record (backup check included), or return the one in progress."""
    previous = copy.deepcopy(store.migration)
    if previous is not None and previous["status"] == STATUS_IN_PROGRESS:
        return previous
    assert plan.boundary is not None
    energy = await async_get_manager(hass)
    record = {
        "status": STATUS_IN_PROGRESS,
        "started": _now(),
        "finished": None,
        "sources": plan.sources,
        "targets": plan.targets,
        "boundary": plan.boundary.isoformat(),
        "parity": plan.parity,
        "energy_backup": copy.deepcopy(energy.data),
        "energy_applied": False,
        "energy_instructions": None,
        "automations": [],
        "steps": {},
        "changes": [],
        "legacy_deleted": False,
        # Earlier runs that were undone, with their recorded changes.
        "history": [
            *(previous or {}).get("history", []),
            *(
                [{k: previous.get(k) for k in ("started", "finished", "undone", "changes")}]
                if previous
                else []
            ),
        ],
    }
    await store.async_set_migration(record)
    stored = await store.async_read_back_migration()
    if (
        stored is None
        or stored.get("energy_backup") != record["energy_backup"]
        or stored.get("sources") != record["sources"]
    ):
        await store.async_set_migration(previous)  # nothing changed: keep the old record
        raise MigrationRefused(
            "backup_check_failed",
            "The saved copy of the Energy preferences did not read back; nothing was changed.",
        )
    _change(record, "backup", "saved the Energy preferences and the plan; read back")
    await store.async_set_migration(record)
    return record


async def _async_rebase_step(
    hass: HomeAssistant, manager: AmberManager, plan: _Plan, record: dict[str, Any]
) -> str | None:
    """Re-base the integration's rows onto the copied history. Returns a pause message."""
    steps = record["steps"]
    if "rebase_progress" not in steps and await _seam_continuous(hass, plan):
        steps["rebase"] = {"skipped": "already continuous"}
    else:
        try:
            steps["rebase"] = await _async_rebase_stored(hass, manager, plan, record)
        except ImportDayError as err:
            _change(record, "rebase_paused", str(err))
            await manager.store.async_set_migration(record)
            return f"re-base paused: {err}. Run the migration again to resume."
        steps.pop("rebase_progress", None)
        if not await _seam_continuous(hass, plan):
            raise MigrationRefused(
                "rebase_failed", "The sums do not continue at the seam after re-basing."
            )
    _change(record, "rebase", steps["rebase"])
    await manager.store.async_set_migration(record)
    return None


async def _async_rebase_stored(
    hass: HomeAssistant, manager: AmberManager, plan: _Plan, record: dict[str, Any]
) -> dict[str, Any]:
    """Rewrite the integration's own rows from the boundary on, from their stored hourly
    amounts, so each day continues from the one before (the first from the copied carry
    row). The same write-and-verify path as an import, day by day, with the progress
    (next day and running sums) stored after each day. No API calls, so days older than
    Amber's retention are re-based too."""
    assert plan.boundary is not None
    boundary = plan.boundary
    specs = list(manager.ctx.specs)
    ids = [spec.statistic_id for spec in specs]
    rows = await _read(hass, ids, boundary - HOUR)
    progress = record["steps"].get("rebase_progress")
    if progress is not None:
        sums = dict(progress["sums"])
        start = date.fromisoformat(progress["next"])
        count = progress["days"]
    else:
        sums = {
            sid: next((r["sum"] for r in rows[sid] if r["start"] == boundary - HOUR), 0.0)
            for sid in ids
        }
        start, count = nem_date(boundary), 0
    by_day: dict[date, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for sid in ids:
        for row in rows[sid]:
            if row["start"] >= boundary:
                by_day[nem_date(row["start"])][sid].append(row)
    for day in sorted(d for d in by_day if d >= start):
        day_specs = [spec for spec in specs if spec.statistic_id in by_day[day]]
        amounts: dict[str, list[float]] = {}
        for spec in day_specs:
            day_rows = by_day[day][spec.statistic_id]
            if len(day_rows) != importer.DAY_HOURS:
                raise ImportRefusedError(
                    "partial_history",
                    f"{spec.statistic_id} does not have all 24 hours of {day}; cannot re-base.",
                )
            amounts[spec.statistic_id] = [float(r["state"] or 0.0) for r in day_rows]
        written = await importer.async_write_amounts(
            hass,
            day,
            day_specs,
            amounts,
            {spec.statistic_id: sums[spec.statistic_id] for spec in day_specs},
            expect_latest=False,
        )
        for sid, day_rows in written.items():
            sums[sid] = day_rows[-1]["sum"]
        count += 1
        record["steps"]["rebase_progress"] = {
            "next": (day + timedelta(days=1)).isoformat(),
            "sums": sums,
            "days": count,
        }
        await manager.store.async_set_migration(record)
    return {"source": "stored hourly amounts", "calls": {}, "rewritten_days": count}


async def _async_turn_off_automations(
    hass: HomeAssistant, plan: _Plan, record: dict[str, Any]
) -> None:
    """Turn off the legacy automations that are on; record every prior state."""
    for automation in plan.automations:
        entry = {
            "entity_id": automation["entity_id"],
            "prior": automation["state"],
            "disabled_by_migration": False,
        }
        if automation["state"] == "on":
            await hass.services.async_call(
                "automation",
                "turn_off",
                {"entity_id": automation["entity_id"], "stop_actions": True},
                blocking=True,
            )
            state = hass.states.get(automation["entity_id"])
            entry["disabled_by_migration"] = state is not None and state.state == "off"
        record["automations"].append(entry)
    record["steps"]["automations"] = [
        {"entity_id": a["entity_id"], "prior": a["prior"], "turned_off": a["disabled_by_migration"]}
        for a in record["automations"]
    ]
    _change(record, "automations", record["steps"]["automations"])


async def _async_finish(
    hass: HomeAssistant, manager: AmberManager, plan: _Plan, record: dict[str, Any]
) -> None:
    record["status"] = STATUS_COMPLETED
    record["finished"] = _now()
    record["cleanup"] = _cleanup_lines(record["automations"], plan.cleanup)
    _change(record, "completed", None)
    await manager.store.async_set_migration(record)
    await async_check_cleanup(hass, manager)
    _LOGGER.info("Migration from the %s completed", plan.sources["layout"])


async def async_check_cleanup(hass: HomeAssistant, manager: AmberManager) -> list[str]:
    """Re-check the YAML kit's leftovers while a migration is completed, and show the
    Repairs issue listing only those that still exist. Once none remain, the issue is
    removed (the old statistics are optional, so they never keep it open). Runs when the
    integration loads (after Home Assistant has started) and after each scheduled run;
    undo removes the issue."""
    record = manager.store.migration
    if record is None or record["status"] != STATUS_COMPLETED:
        return []
    issue_id = f"{ISSUE_LEGACY_CLEANUP}_{manager.entry.entry_id}"
    lines = await _async_leftovers(hass, record)
    if not lines:
        if ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None:
            _LOGGER.info("The YAML kit's leftovers are all removed; the Repairs issue is cleared")
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return []
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        is_persistent=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_LEGACY_CLEANUP,
        translation_placeholders={
            "entry": manager.entry.title,
            "items": "\n".join(f"- {line}" for line in lines),
        },
    )
    return lines


async def _async_leftovers(hass: HomeAssistant, record: Mapping[str, Any]) -> list[str]:
    """The cleanup lines for the parts of the kit that still exist."""
    sources = record.get("sources") or {}
    layout = _BY_KEY.get(sources.get("layout"))
    if layout is not None:
        items = layout.cleanup
    else:
        # A manually picked source: its legacy sensors (the statistics stay).
        picked = (sources.get(role) for role in ROLES)
        sensors = tuple(dict.fromkeys(s for s in picked if s and valid_entity_id(s)))
        items = (
            Leftover(
                "Legacy sensors {items}, and the helpers, scripts and YAML blocks that feed "
                "them: remove them once you are satisfied with the migration.",
                entities=sensors,
            ),
        )
    registry = er.async_get(hass)
    files = [f for item in items for f in item.files]
    found = await hass.async_add_executor_job(_on_disk, hass.config.path(), files)

    def present(item: Leftover) -> list[str]:
        return [
            *(e for e in item.entities if _entity_exists(hass, registry, e)),
            *(s for s in item.services if hass.services.has_service(*s.split(".", 1))),
            *(f for f in item.files if f in found),
        ]

    automations = [
        a for a in record.get("automations", []) if _entity_exists(hass, registry, a["entity_id"])
    ]
    lines = _cleanup_lines(automations, [])
    in_config = bool(automations)
    for item in items:
        names = present(item)
        if names:
            lines.append(item.render(names))
            in_config = in_config or bool(item.entities or item.services)
    if in_config and layout is not None:
        lines.append(_PACKAGE_HINT)
    return lines


def _entity_exists(hass: HomeAssistant, registry: er.EntityRegistry, entity_id: str) -> bool:
    return hass.states.get(entity_id) is not None or registry.async_get(entity_id) is not None


def _on_disk(config_dir: str, files: Sequence[str]) -> set[str]:
    """Which of these files exist in the configuration folder."""
    return {f for f in files if os.path.isfile(os.path.join(config_dir, f))}


async def _async_copy(hass: HomeAssistant, manager: AmberManager, plan: _Plan) -> None:
    specs = {s.statistic_id: s for s in manager.ctx.specs}
    for sid, rows in plan.copy.items():
        async_add_external_statistics(hass, specs[sid].metadata(), rows)
    for sid, rows in plan.copy.items():
        await importer.async_verify_range(
            hass, {sid: rows}, rows[0]["start"], plan.boundary, tolerance=1e-6
        )


async def _seam_continuous(hass: HomeAssistant, plan: _Plan) -> bool:
    """True when every copied statistic's first new row continues from the carry row."""
    assert plan.boundary is not None
    ids = list(plan.copy)
    rows = await _read(hass, ids, plan.boundary - HOUR, plan.boundary + HOUR)
    for sid in ids:
        by_start = {r["start"]: r for r in rows[sid]}
        carry, first = by_start.get(plan.boundary - HOUR), by_start.get(plan.boundary)
        if carry is None or first is None:
            return False
        if abs(first["sum"] - float(first["state"] or 0.0) - carry["sum"]) > 1e-6:
            return False
    return True


async def _async_apply_energy(
    hass: HomeAssistant, plan: _Plan, record: dict[str, Any]
) -> dict[str, Any]:
    energy = plan.energy or {}
    if not energy.get("changed"):
        return {"changed": False, "reason": energy.get("reason")}
    if not energy.get("safe"):
        record["energy_instructions"] = energy["instructions"]
        return {
            "changed": False,
            "reason": energy.get("reason"),
            "instructions": energy["instructions"],
        }
    manager = await async_get_manager(hass)
    await manager.async_update({"energy_sources": copy.deepcopy(energy["sources_after"])})
    current = (manager.data or {}).get("energy_sources", [])
    keys = ("stat_energy_from", "stat_cost", "stat_energy_to", "stat_compensation")
    ok = all(
        index < len(current)
        and all(current[index].get(k) == energy["sources_after"][index].get(k) for k in keys)
        for index in energy["indexes"]
    )
    if not ok:
        backup = record["energy_backup"] or {}
        await manager.async_update(
            {"energy_sources": copy.deepcopy(backup.get("energy_sources", []))}
        )
        record["energy_instructions"] = energy["instructions"]
        return {
            "changed": False,
            "reason": "the change did not read back; restored",
            "instructions": energy["instructions"],
        }
    record["energy_applied"] = True
    return {"changed": True, "before": energy["before"], "after": energy["after"]}


# --- undo and deletion -------------------------------------------------------------------


async def async_undo(hass: HomeAssistant, manager: AmberManager) -> dict[str, Any]:
    """Restore the saved Energy preferences and re-enable the automations it turned off."""
    store = manager.store
    record = copy.deepcopy(store.migration)
    if record is None or record["status"] not in (STATUS_IN_PROGRESS, STATUS_COMPLETED):
        raise MigrationRefused("nothing_to_undo", "There is no migration to undo.")
    if record.get("legacy_deleted"):
        raise MigrationRefused(
            "legacy_deleted", "The legacy statistics were deleted; the migration cannot be undone."
        )
    async with manager.ctx.lock:
        result: dict[str, Any] = {"energy_restored": False, "automations_enabled": []}
        if record.get("energy_applied"):
            energy = await async_get_manager(hass)
            backup = record["energy_backup"] or {}
            await energy.async_update(
                {
                    key: copy.deepcopy(backup[key])
                    for key in ("energy_sources", "device_consumption", "device_consumption_water")
                    if key in backup
                }
            )
            current = (energy.data or {}).get("energy_sources")
            if current != backup.get("energy_sources"):
                raise MigrationRefused(
                    "undo_failed", "The Energy preferences did not read back after restoring."
                )
            result["energy_restored"] = True
            record["energy_applied"] = False
        for automation in record["automations"]:
            if automation["disabled_by_migration"]:
                await hass.services.async_call(
                    "automation", "turn_on", {"entity_id": automation["entity_id"]}, blocking=True
                )
                state = hass.states.get(automation["entity_id"])
                if state is None or state.state != "on":
                    raise MigrationRefused(
                        "undo_failed", f"{automation['entity_id']} did not turn back on."
                    )
                automation["disabled_by_migration"] = False
                result["automations_enabled"].append(automation["entity_id"])
        result["left_unchanged"] = [
            a["entity_id"] for a in record["automations"] if a["prior"] != "on"
        ]
        result["kept"] = (
            "Copied history before the integration's first day, and the re-based sums, stay."
        )
        record["status"] = STATUS_UNDONE
        record["undone"] = _now()
        _change(record, "undo", result)
        await store.async_set_migration(record)
        ir.async_delete_issue(hass, DOMAIN, f"{ISSUE_LEGACY_CLEANUP}_{manager.entry.entry_id}")
        return result


async def async_delete_legacy(
    hass: HomeAssistant, manager: AmberManager, *, confirm: bool
) -> dict[str, Any]:
    """Delete the legacy statistics after a completed migration (explicit, irreversible)."""
    store = manager.store
    record = copy.deepcopy(store.migration)
    if record is None or record["status"] != STATUS_COMPLETED:
        raise MigrationRefused(
            "not_migrated", "Only a completed migration's statistics can be deleted."
        )
    if not confirm:
        raise MigrationRefused("not_confirmed", "Set confirm to delete the legacy statistics.")
    ids = sorted({record["sources"].get(role) for role in ROLES} - {None})
    instance = get_instance(hass)
    instance.async_clear_statistics(ids)
    await instance.async_block_till_done()
    remaining = sorted(await _metadata(hass, ids))
    if remaining:
        raise MigrationRefused("delete_failed", f"Still present: {', '.join(remaining)}")
    record["legacy_deleted"] = True
    _change(record, "delete_legacy_statistics", ids)
    await store.async_set_migration(record)
    return {"deleted": ids}


# --- text for the options flow -----------------------------------------------------------


def _flagged_lines(flagged: Mapping[str, Any]) -> list[str]:
    if not flagged.get("count"):
        return []
    lines = [
        f"Implausible legacy rows: {flagged['count']}"
        + (" (left out; sums re-derived)." if flagged["excluded"] else ".")
    ]
    lines.extend(
        f"- {flag['statistic']} at {flag['start']}: {flag['problem']} "
        f"({flag['kind']}, in the {flag['where']} period), sum {flag['sum']}"
        for flag in flagged["rows"]
    )
    return lines


def format_report(report: Mapping[str, Any]) -> str:
    """A readable summary of a dry-run or run report (markdown)."""
    lines: list[str] = [f"**{report['warning']}**", ""]
    detection = report.get("detection") or {}
    if report.get("needs_manual_pick"):
        lines.append(f"Detection: {detection.get('reason')} Pick the legacy statistics.")
        return "\n".join(lines)
    lines.append(f"Source: {report['layout']}.")
    for ignored in detection.get("ignored", []):
        lines.append(f"Ignored: the {ignored['title']} ({ignored['reason']}).")
    sources, targets = report["sources"], report["targets"]
    for role in ROLES:
        if sources.get(role):
            lines.append(f"- {_ROLE_LABELS[role]}: {sources[role]} -> {targets.get(role)}")
    for problem in report.get("problems", []):
        lines.append(f"Problem: {problem}")
    lines.extend(_flagged_lines(report.get("flagged") or {}))
    parity = report.get("parity")
    if parity:
        lines.append(
            f"Parity ({parity['rules']} rules): {parity['days']} days "
            f"({parity['first']} to {parity['last']}), "
            + ("passed." if parity["passed"] else f"FAILED: {parity['reason']}.")
        )
        for mismatch in parity["mismatches"]:
            lines.append(f"- {mismatch}")
    for sid, info in report.get("copy", {}).items():
        lines.append(
            f"Copy {info['rows']} rows into {sid} ({info['from'][:10]} to {info['to'][:10]}), "
            f"baseline {info['baseline']}."
        )
    energy = report.get("energy") or {}
    if energy.get("changed"):
        lines.append("Energy dashboard, before:")
        lines.append(f"`{json.dumps(energy['before'])}`")
        lines.append("after:")
        lines.append(f"`{json.dumps(energy['after'])}`")
        if not energy.get("safe"):
            lines.append(f"Cannot be changed automatically ({energy['reason']}); instructions:")
            lines.extend(f"- {line}" for line in energy["instructions"])
    elif energy:
        lines.append(f"Energy dashboard: {energy.get('reason')}")
    for automation in report.get("automations", []):
        lines.append(
            f"Automation {automation['entity_id']} ({automation['state']}): "
            + ("will be turned off." if automation["state"] == "on" else "left as it is.")
        )
    lines.extend(f"Note: {note}" for note in report.get("notes", []))
    return "\n".join(lines)


def format_result(report: Mapping[str, Any]) -> str:
    """A readable summary of a completed (or paused) run."""
    result = report.get("result") or {}
    lines = [f"Migration status: {report.get('migration_status')}."]
    if "paused" in result:
        lines.append(f"Paused: {result['paused']}")
    for sid, info in (result.get("copy") or {}).items():
        lines.append(f"Copied {info['rows']} rows into {sid} (baseline {info['baseline']}).")
    rebase = result.get("rebase")
    if rebase:
        lines.append(f"Re-base: {rebase}.")
    energy = result.get("energy") or {}
    if energy.get("changed"):
        lines.append("Energy dashboard switched to the new statistics.")
    elif energy:
        lines.append(f"Energy dashboard not changed: {energy.get('reason')}")
        lines.extend(f"- {line}" for line in energy.get("instructions", []))
    for automation in result.get("automations") or []:
        lines.append(
            f"Automation {automation['entity_id']}: "
            + ("turned off." if automation["turned_off"] else f"left {automation['prior']}.")
        )
    if report.get("cleanup"):
        lines.append("Still to remove by hand:")
        lines.extend(f"- {line}" for line in report["cleanup"])
    return "\n".join(lines)
