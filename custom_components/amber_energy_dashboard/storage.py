"""Persistent per-entry state (DESIGN section 3): everything that is not a statistic.

The statistics table stays the source of truth for running totals. The Store holds:

- ``marker``: the last NEM day that is resolved (imported, or skipped as unavailable or
  as a gap). The walk continues from marker + 1.
- ``last_written``: the last NEM day that has statistics rows. Guard 1 checks the table
  against this. It equals the marker unless the latest resolved days were skipped.
- ``pending``: a day whose write started but was not recorded as done (crash recovery).
- ``days``: per-day status, reason and totals.
- retention, patience (``empty_seen``), revisions and tail-rewrite progress, the schedule
  seed, per-channel start days for channels added later, and the last run.
- the YAML-kit migration record, and the Energy dashboard setup (section 18): when the
  first import was seen, the last "Add to the Energy dashboard" outcome with the saved
  preferences, and whether the "not used" issue was dismissed.

Writes are atomic and immediate.
"""

from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta
import hashlib
from typing import Any, Final

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, NEM_TZ

STORAGE_VERSION: Final = 1
STORAGE_MINOR_VERSION: Final = 3
MAX_DAY_ENTRIES: Final = 400

STATUS_IMPORTED: Final = "imported"
STATUS_SKIPPED_UNAVAILABLE: Final = "skipped_unavailable"
STATUS_SKIPPED_GAP: Final = "skipped_gap"
STATUS_SKIPPED_NOT_REQUESTED: Final = "skipped_not_requested"
"""Recovery-only mode: a day between two backfilled ranges that was not asked for."""
STATUS_SKIPPED_SENSOR: Final = "skipped_sensor_data"
"""Own-sensor chain: the sensor has no usable statistics for the day."""
STATUS_SKIPPED_PRECISION: Final = "skipped_no_short_term"
"""Own-sensor chain: 5-minute data is gone and the hourly fallback is turned off."""
SKIPPED_STATUSES: Final = (
    STATUS_SKIPPED_UNAVAILABLE,
    STATUS_SKIPPED_GAP,
    STATUS_SKIPPED_NOT_REQUESTED,
    STATUS_SKIPPED_SENSOR,
    STATUS_SKIPPED_PRECISION,
)


def schedule_seed_for(entry_id: str) -> int:
    """Return a stable 32-bit seed derived from the config entry ID."""
    return int(hashlib.sha256(entry_id.encode()).hexdigest()[:8], 16)


def _day(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _nem_today() -> date:
    return dt_util.utcnow().astimezone(NEM_TZ).date()


def retention_boundary_from_days(data: dict[str, Any], today: date) -> str | None:
    """The boundary date for a store that held a day count (layout 1.2 and earlier).

    The date recorded with the measurement wins; otherwise the count is taken as
    measured on its ``measured_on`` day, or today when that is missing too.
    """
    info = data.get("retention") or {}
    if info.get("boundary"):
        return info["boundary"]
    days = data.get("retention_days")
    if days is None:
        return None
    base = _day(info.get("measured_on")) or today
    return (base - timedelta(days=days)).isoformat()


class _VersionedStore(Store[dict[str, Any]]):
    def __init__(self, *args: Any, today: Callable[[], date] = _nem_today, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._today = today

    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: dict[str, Any]
    ) -> dict[str, Any]:
        """Migrate older layouts.

        1.1 -> 1.2 (Milestone 4): add ``last_written``. In 1.1 every resolved day was
        imported, so it equals the marker. Other new keys are filled with defaults.
        1.2 -> 1.3 (Milestone 6b): retention is kept as the boundary date
        (``retention_boundary``); the day count is derived from it.
        """
        if old_major_version > STORAGE_VERSION:
            raise NotImplementedError(f"store version {old_major_version} is newer")
        data = dict(old_data)
        if old_minor_version < 2:
            data.setdefault("last_written", data.get("marker"))
        if old_minor_version < 3 and not data.get("retention_boundary"):
            data["retention_boundary"] = retention_boundary_from_days(data, self._today())
        if old_minor_version < 3:
            data.pop("retention_days", None)
        return data


class AmberStore:
    """Typed access to the per-entry store."""

    def __init__(
        self, hass: HomeAssistant, entry_id: str, today: Callable[[], date] = _nem_today
    ) -> None:
        """Create the store handle (call async_load before use).

        ``today`` returns the current NEM date; the manager passes its own clock.
        """
        self._entry_id = entry_id
        self._today = today
        self._store = _VersionedStore(
            hass,
            STORAGE_VERSION,
            f"{DOMAIN}.{entry_id}",
            minor_version=STORAGE_MINOR_VERSION,
            atomic_writes=True,
            today=today,
        )
        self._data: dict[str, Any] = {}

    def _defaults(self) -> dict[str, Any]:
        return {
            "marker": None,
            "last_written": None,
            "pending": None,
            "days": {},
            "retention_boundary": None,
            "retention": None,
            "schedule_seed": schedule_seed_for(self._entry_id),
            "last_run": None,
            "empty_seen": {},
            "revisions": {},
            "revisions_checked": None,
            "tail_rewrite": None,
            "channel_since": {},
            "chains": {},
            "migration": None,
            "first_import_at": None,
            "energy_setup": None,
            "energy_issue_dismissed": False,
            "fixed_schedule": [],
            "export_days": {},
            "tariff": None,
            "site": None,
        }

    async def async_load(self) -> None:
        """Load from disk (migrating if needed), filling in defaults for new keys."""
        data = await self._store.async_load()
        self._data = {**self._defaults(), **(data or {})}
        await self._async_save()

    async def _async_save(self) -> None:
        await self._store.async_save(self._data)

    async def async_remove(self) -> None:
        """Delete the store file (entry removed)."""
        await self._store.async_remove()

    # --- read accessors -------------------------------------------------------------

    @property
    def marker(self) -> date | None:
        """The last resolved NEM day (imported or skipped)."""
        return _day(self._data["marker"])

    @property
    def last_written(self) -> date | None:
        """The last NEM day with statistics rows."""
        return _day(self._data["last_written"])

    @property
    def pending(self) -> date | None:
        """A day whose write started but whose completion was not recorded."""
        return _day(self._data["pending"])

    @property
    def retention_boundary(self) -> date | None:
        """The earliest NEM day with usage data, or None before discovery."""
        return _day(self._data["retention_boundary"])

    @property
    def retention_days(self) -> int | None:
        """Days from the boundary to today (derived, for display), or None."""
        boundary = self.retention_boundary
        return None if boundary is None else (self._today() - boundary).days

    @property
    def retention(self) -> dict[str, Any] | None:
        """How and when retention was last determined or verified."""
        return self._data["retention"]

    @property
    def schedule_seed(self) -> int:
        """Stable seed for the automatic schedule offset."""
        return self._data["schedule_seed"]

    @property
    def last_run(self) -> dict[str, Any] | None:
        """Summary of the most recent run (status 'running' means it was interrupted)."""
        return self._data["last_run"]

    @property
    def revisions(self) -> dict[str, dict[str, Any]]:
        """Days in the revision set: day -> {since, hash}."""
        return self._data["revisions"]

    @property
    def revisions_checked(self) -> date | None:
        """The NEM day revisions were last checked."""
        return _day(self._data["revisions_checked"])

    @property
    def tail_rewrite(self) -> dict[str, Any] | None:
        """A tail rewrite in progress: {from, next, to}, or None."""
        return self._data["tail_rewrite"]

    @property
    def migration(self) -> dict[str, Any] | None:
        """The YAML-kit migration record (section 14), or None if never started."""
        return self._data["migration"]

    @property
    def first_import_at(self) -> datetime | None:
        """When the first successful import was seen (section 18), or None."""
        value = self._data["first_import_at"]
        return datetime.fromisoformat(value) if value else None

    @property
    def energy_setup(self) -> dict[str, Any] | None:
        """The last "Add to the Energy dashboard" outcome (section 18), or None."""
        return self._data["energy_setup"]

    @property
    def fixed_schedule(self) -> list[dict[str, Any]]:
        """The daily fixed amounts (incl. GST) for the derived statistic, each with the
        hour it applies from (None: from the start); section 19."""
        return list(self._data["fixed_schedule"])

    @property
    def export_days(self) -> dict[str, dict[str, Any]]:
        """Per-day export aggregates for the allowance (section 20), keyed by NEM date."""
        return self._data["export_days"]

    @property
    def tariff(self) -> dict[str, Any] | None:
        """The network and tariff codes detected from /sites at the last setup."""
        return self._data["tariff"]

    @property
    def site(self) -> dict[str, Any] | None:
        """The site's start and close dates from /sites at the last setup."""
        return self._data["site"]

    @property
    def energy_issue_dismissed(self) -> bool:
        """True once the user dismissed the "Energy dashboard not used" issue."""
        return bool(self._data["energy_issue_dismissed"])

    def channel_since(self, identifier: str) -> date | None:
        """First NEM day a channel added after setup is imported for (None = always)."""
        return _day(self._data["channel_since"].get(identifier))

    def day(self, day: date) -> dict[str, Any] | None:
        """Per-day status record."""
        return self._data["days"].get(day.isoformat())

    def empty_seen(self, day: date) -> list[str]:
        """The distinct NEM dates on which ``day`` returned empty."""
        return list(self._data["empty_seen"].get(day.isoformat(), []))

    def last_imported_before(self, day: date) -> date | None:
        """The latest day before ``day`` that has statistics (per the day records)."""
        days = [
            date.fromisoformat(k)
            for k, v in self._data["days"].items()
            if v.get("status") == STATUS_IMPORTED and k < day.isoformat()
        ]
        return max(days) if days else None

    def as_dict(self) -> dict[str, Any]:
        """A copy for diagnostics."""
        return {
            **self._data,
            "days": dict(self._data["days"]),
            "retention_days": self.retention_days,
        }

    # --- writes (each saved immediately) --------------------------------------------

    async def async_set_pending(self, day: date) -> None:
        """Record that a write of ``day`` is about to start."""
        self._data["pending"] = day.isoformat()
        await self._async_save()

    async def async_mark_imported(self, day: date, summary: dict[str, Any]) -> None:
        """Record a verified write of ``day``; advance the marker and last_written."""
        if self.marker is None or day > self.marker:
            self._data["marker"] = day.isoformat()
        if self.last_written is None or day > self.last_written:
            self._data["last_written"] = day.isoformat()
        self._data["pending"] = None
        self._data["empty_seen"].pop(day.isoformat(), None)
        self._set_day(
            day,
            {
                "status": STATUS_IMPORTED,
                "reason": None,
                "mode": summary.get("mode"),
                "records": summary.get("records"),
                "estimated_records": summary.get("estimated_records"),
                "totals": {sid: s["day_total"] for sid, s in summary.get("statistics", {}).items()},
            },
        )
        await self._async_save()

    async def async_mark_skipped(self, first: date, last: date, status: str, reason: str) -> None:
        """Resolve days ``first``..``last`` without statistics and advance the marker."""
        day = first
        while day <= last:
            self._set_day(day, {"status": status, "reason": reason})
            self._data["empty_seen"].pop(day.isoformat(), None)
            self._data["revisions"].pop(day.isoformat(), None)
            day += timedelta(days=1)
        if self.marker is None or last > self.marker:
            self._data["marker"] = last.isoformat()
        await self._async_save()

    async def async_mark_day(
        self, day: date, status: str, reason: str | None, message: str | None = None
    ) -> None:
        """Record a non-imported outcome for ``day`` (the marker does not move)."""
        previous = self.day(day)
        if previous and previous.get("status") == STATUS_IMPORTED:
            previous = {**previous, "last_problem": {"status": status, "reason": reason}}
            self._set_day(day, previous)
        else:
            self._set_day(day, {"status": status, "reason": reason, "message": message})
        await self._async_save()

    async def async_note_empty(self, day: date, seen_on: date) -> int:
        """Record that ``day`` returned empty on NEM date ``seen_on``; return the count."""
        seen = self._data["empty_seen"].setdefault(day.isoformat(), [])
        if seen_on.isoformat() not in seen:
            seen.append(seen_on.isoformat())
        await self.async_mark_day(day, "empty", "no_data")
        return len(seen)

    async def async_clear_pending(self) -> None:
        """Forget a pending day (used when a write failed before touching statistics)."""
        if self._data["pending"] is not None:
            self._data["pending"] = None
            await self._async_save()

    async def async_set_retention(self, boundary: date, info: dict[str, Any]) -> None:
        """Store the learned retention boundary."""
        self._data["retention_boundary"] = boundary.isoformat()
        self._data["retention"] = info
        await self._async_save()

    async def async_set_last_run(self, info: dict[str, Any]) -> None:
        """Store the run summary."""
        self._data["last_run"] = info
        await self._async_save()

    async def async_set_revision(self, day: date, since: date, fingerprint: str) -> None:
        """Put ``day`` in the revision set (or update its fingerprint)."""
        entry = self._data["revisions"].get(day.isoformat(), {})
        self._data["revisions"][day.isoformat()] = {
            "since": entry.get("since", since.isoformat()),
            "hash": fingerprint,
        }
        await self._async_save()

    async def async_drop_revisions(self, days: Iterable[date]) -> None:
        """Remove days from the revision set."""
        for day in days:
            self._data["revisions"].pop(day.isoformat(), None)
        await self._async_save()

    async def async_set_revisions_checked(self, day: date) -> None:
        """Record that revisions were checked on NEM day ``day``."""
        self._data["revisions_checked"] = day.isoformat()
        await self._async_save()

    async def async_set_tail_rewrite(self, progress: dict[str, Any] | None) -> None:
        """Record tail-rewrite progress, or None when it is complete."""
        self._data["tail_rewrite"] = progress
        await self._async_save()

    async def async_set_migration(self, record: dict[str, Any] | None) -> None:
        """Replace the migration record (saved immediately)."""
        self._data["migration"] = record
        await self._async_save()

    async def async_read_back_migration(self) -> dict[str, Any] | None:
        """The migration record as stored on disk, read through a fresh handle."""
        return await self.async_read_back("migration")

    async def async_read_back(self, key: str) -> Any:
        """One key as stored on disk, read through a fresh handle."""
        fresh = _VersionedStore(
            self._store.hass,
            STORAGE_VERSION,
            f"{DOMAIN}.{self._entry_id}",
            minor_version=STORAGE_MINOR_VERSION,
        )
        data = await fresh.async_load()
        return (data or {}).get(key)

    async def async_set_first_import_at(self, when: datetime) -> None:
        """Record when the first successful import was seen."""
        self._data["first_import_at"] = when.isoformat()
        await self._async_save()

    async def async_set_energy_setup(self, record: dict[str, Any]) -> None:
        """Replace the "Add to the Energy dashboard" record (saved immediately)."""
        self._data["energy_setup"] = record
        await self._async_save()

    async def async_set_fixed_schedule(self, schedule: list[dict[str, Any]]) -> None:
        """Replace the fixed-amount schedule."""
        self._data["fixed_schedule"] = schedule
        await self._async_save()

    async def async_set_export_day(self, day: date, record: dict[str, Any]) -> None:
        """Store one day's export aggregates (the oldest beyond the day limit are pruned)."""
        days = self._data["export_days"]
        days[day.isoformat()] = record
        if len(days) > MAX_DAY_ENTRIES:
            for key in sorted(days)[: len(days) - MAX_DAY_ENTRIES]:
                del days[key]
        await self._async_save()

    async def async_set_site(self, site: dict[str, Any]) -> None:
        """Record the site's start and close dates."""
        self._data["site"] = site
        await self._async_save()

    async def async_set_tariff(self, tariff: dict[str, Any]) -> None:
        """Record the detected network and tariff codes."""
        self._data["tariff"] = tariff
        await self._async_save()

    async def async_dismiss_energy_issue(self) -> None:
        """Remember that the user dismissed the "Energy dashboard not used" issue."""
        self._data["energy_issue_dismissed"] = True
        await self._async_save()

    async def async_set_channel_since(self, identifiers: Iterable[str], since: date) -> None:
        """Record the first import day for channels added after setup."""
        for ident in identifiers:
            self._data["channel_since"][ident] = since.isoformat()
        await self._async_save()

    def _set_day(self, day: date, record: dict[str, Any]) -> None:
        days: dict[str, Any] = self._data["days"]
        days[day.isoformat()] = {**record, "at": datetime.now(UTC).isoformat(timespec="seconds")}
        if len(days) > MAX_DAY_ENTRIES:
            for key in sorted(days)[: len(days) - MAX_DAY_ENTRIES]:
                del days[key]


class ChainState:
    """Store-backed state for a secondary chain (price series, or one own sensor).

    Same rules as the usage chain: ``marker`` is the last resolved day, ``last_written``
    the last day with statistics, ``pending`` a day whose write started, ``rewrite`` the
    progress of an interrupted rewrite. Saved through the parent store.
    """

    def __init__(self, store: AmberStore, key: str) -> None:
        """Bind to ``store.chains[key]``, creating it if needed."""
        self._store = store
        self.key = key
        chains = store._data["chains"]
        self._data: dict[str, Any] = chains.setdefault(
            key,
            {"marker": None, "last_written": None, "pending": None, "rewrite": None, "days": {}},
        )

    @property
    def marker(self) -> date | None:
        """The last resolved day."""
        return _day(self._data["marker"])

    @property
    def last_written(self) -> date | None:
        """The last day with statistics."""
        return _day(self._data["last_written"])

    @property
    def pending(self) -> date | None:
        """A day whose write started."""
        return _day(self._data["pending"])

    @property
    def rewrite(self) -> dict[str, Any] | None:
        """Progress of an interrupted rewrite: {next, to}."""
        return self._data["rewrite"]

    def day(self, day: date) -> dict[str, Any] | None:
        """Per-day record."""
        return self._data["days"].get(day.isoformat())

    def last_imported_before(self, day: date) -> date | None:
        """The latest day before ``day`` with statistics."""
        found = [
            date.fromisoformat(k)
            for k, v in self._data["days"].items()
            if v.get("status") == STATUS_IMPORTED and k < day.isoformat()
        ]
        return max(found) if found else None

    async def async_set_pending(self, day: date | None) -> None:
        """Record (or clear) the day being written."""
        self._data["pending"] = day.isoformat() if day else None
        await self._store._async_save()

    async def async_adopt(self, last: date) -> None:
        """Continue after existing statistics that end at ``last`` (a re-added sensor)."""
        self._data["marker"] = self._data["last_written"] = last.isoformat()
        await self._store._async_save()

    async def async_mark_written(self, day: date, record: dict[str, Any]) -> None:
        """Record a verified write of ``day``."""
        if self.marker is None or day > self.marker:
            self._data["marker"] = day.isoformat()
        if self.last_written is None or day > self.last_written:
            self._data["last_written"] = day.isoformat()
        self._data["pending"] = None
        self._set_day(day, {"status": STATUS_IMPORTED, **record})
        await self._store._async_save()

    async def async_mark_skipped(self, first: date, last: date, status: str, reason: str) -> None:
        """Resolve days without statistics and advance the marker."""
        day = first
        while day <= last:
            if (self.day(day) or {}).get("status") != STATUS_IMPORTED:
                self._set_day(day, {"status": status, "reason": reason})
            day += timedelta(days=1)
        if self.marker is None or last > self.marker:
            self._data["marker"] = last.isoformat()
        await self._store._async_save()

    async def async_set_rewrite(self, progress: dict[str, Any] | None) -> None:
        """Record rewrite progress, or None when done."""
        self._data["rewrite"] = progress
        await self._store._async_save()

    def _set_day(self, day: date, record: dict[str, Any]) -> None:
        days: dict[str, Any] = self._data["days"]
        days[day.isoformat()] = {**record, "at": datetime.now(UTC).isoformat(timespec="seconds")}
        if len(days) > MAX_DAY_ENTRIES:
            for key in sorted(days)[: len(days) - MAX_DAY_ENTRIES]:
                del days[key]


def chain_state(store: AmberStore, key: str) -> ChainState:
    """Return the state object for a secondary chain."""
    return ChainState(store, key)


async def async_forget_chain(store: AmberStore, key: str) -> None:
    """Drop a chain's state (the sensor mapping was removed; statistics are kept)."""
    if store._data["chains"].pop(key, None) is not None:
        await store._async_save()
