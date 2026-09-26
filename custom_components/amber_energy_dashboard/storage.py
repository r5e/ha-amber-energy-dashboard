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

Writes are atomic and immediate.
"""

from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
import hashlib
from typing import Any, Final

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

STORAGE_VERSION: Final = 1
STORAGE_MINOR_VERSION: Final = 2
MAX_DAY_ENTRIES: Final = 400

STATUS_IMPORTED: Final = "imported"
STATUS_SKIPPED_UNAVAILABLE: Final = "skipped_unavailable"
STATUS_SKIPPED_GAP: Final = "skipped_gap"


def schedule_seed_for(entry_id: str) -> int:
    """Return a stable 32-bit seed derived from the config entry ID."""
    return int(hashlib.sha256(entry_id.encode()).hexdigest()[:8], 16)


def _day(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


class _VersionedStore(Store[dict[str, Any]]):
    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: dict[str, Any]
    ) -> dict[str, Any]:
        """Migrate older layouts.

        1.1 -> 1.2 (Milestone 4): add ``last_written``. In 1.1 every resolved day was
        imported, so it equals the marker. Other new keys are filled with defaults.
        """
        if old_major_version > STORAGE_VERSION:
            raise NotImplementedError(f"store version {old_major_version} is newer")
        data = dict(old_data)
        if old_minor_version < 2:
            data.setdefault("last_written", data.get("marker"))
        return data


class AmberStore:
    """Typed access to the per-entry store."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Create the store handle (call async_load before use)."""
        self._entry_id = entry_id
        self._store = _VersionedStore(
            hass,
            STORAGE_VERSION,
            f"{DOMAIN}.{entry_id}",
            minor_version=STORAGE_MINOR_VERSION,
            atomic_writes=True,
        )
        self._data: dict[str, Any] = {}

    def _defaults(self) -> dict[str, Any]:
        return {
            "marker": None,
            "last_written": None,
            "pending": None,
            "days": {},
            "retention_days": None,
            "retention": None,
            "schedule_seed": schedule_seed_for(self._entry_id),
            "last_run": None,
            "empty_seen": {},
            "revisions": {},
            "revisions_checked": None,
            "tail_rewrite": None,
            "channel_since": {},
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
    def retention_days(self) -> int | None:
        """Learned usage retention in days, or None before discovery."""
        return self._data["retention_days"]

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
        return {**self._data, "days": dict(self._data["days"])}

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

    async def async_set_retention(self, days: int, info: dict[str, Any]) -> None:
        """Store the learned retention."""
        self._data["retention_days"] = days
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
