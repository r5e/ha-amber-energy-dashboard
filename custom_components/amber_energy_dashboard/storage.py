"""Persistent per-entry state (DESIGN section 3): everything that is not a statistic.

The statistics table stays the source of truth for running totals. The Store holds the
import marker (last verified NEM day), a pending day for crash recovery, per-day status,
the learned retention and the schedule seed. Writes are atomic and immediate.
"""

from datetime import UTC, date, datetime
import hashlib
from typing import Any, Final

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

STORAGE_VERSION: Final = 1
STORAGE_MINOR_VERSION: Final = 1
MAX_DAY_ENTRIES: Final = 400


def schedule_seed_for(entry_id: str) -> int:
    """Return a stable 32-bit seed derived from the config entry ID."""
    return int(hashlib.sha256(entry_id.encode()).hexdigest()[:8], 16)


class _VersionedStore(Store[dict[str, Any]]):
    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: dict[str, Any]
    ) -> dict[str, Any]:
        """Migrate older layouts. There are none yet; refuse anything unexpected."""
        if old_major_version > STORAGE_VERSION:
            raise NotImplementedError(f"store version {old_major_version} is newer")
        return old_data


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
            "pending": None,
            "days": {},
            "retention_days": None,
            "retention": None,
            "schedule_seed": schedule_seed_for(self._entry_id),
            "last_run": None,
        }

    async def async_load(self) -> None:
        """Load from disk, filling in defaults for a new entry."""
        data = await self._store.async_load()
        self._data = {**self._defaults(), **(data or {})}
        if data is None:
            await self._async_save()

    async def _async_save(self) -> None:
        await self._store.async_save(self._data)

    async def async_remove(self) -> None:
        """Delete the store file (entry removed)."""
        await self._store.async_remove()

    # --- read accessors -------------------------------------------------------------

    @property
    def marker(self) -> date | None:
        """The last NEM day imported and verified."""
        value = self._data["marker"]
        return date.fromisoformat(value) if value else None

    @property
    def pending(self) -> date | None:
        """A day whose write started but whose marker advance was not recorded."""
        value = self._data["pending"]
        return date.fromisoformat(value) if value else None

    @property
    def retention_days(self) -> int | None:
        """Learned usage retention in days, or None before discovery."""
        return self._data["retention_days"]

    @property
    def retention(self) -> dict[str, Any] | None:
        """How and when retention was last determined."""
        return self._data["retention"]

    @property
    def schedule_seed(self) -> int:
        """Stable seed for the automatic schedule offset."""
        return self._data["schedule_seed"]

    @property
    def last_run(self) -> dict[str, Any] | None:
        """Summary of the most recent run (status 'running' means it was interrupted)."""
        return self._data["last_run"]

    def day(self, day: date) -> dict[str, Any] | None:
        """Per-day status record."""
        return self._data["days"].get(day.isoformat())

    def as_dict(self) -> dict[str, Any]:
        """A copy for diagnostics."""
        return {**self._data, "days": dict(self._data["days"])}

    # --- writes (each saved immediately) --------------------------------------------

    async def async_set_pending(self, day: date) -> None:
        """Record that a write of ``day`` is about to start."""
        self._data["pending"] = day.isoformat()
        await self._async_save()

    async def async_mark_imported(self, day: date, summary: dict[str, Any]) -> None:
        """Advance the marker to ``day`` after a verified write, and clear pending."""
        marker = self.marker
        if marker is None or day > marker:
            self._data["marker"] = day.isoformat()
        self._data["pending"] = None
        self._set_day(
            day,
            {
                "status": "imported",
                "reason": None,
                "mode": summary.get("mode"),
                "records": summary.get("records"),
                "estimated_records": summary.get("estimated_records"),
                "totals": {sid: s["day_total"] for sid, s in summary.get("statistics", {}).items()},
            },
        )
        await self._async_save()

    async def async_mark_day(
        self, day: date, status: str, reason: str | None, message: str | None = None
    ) -> None:
        """Record a non-imported outcome for ``day`` (the marker does not move)."""
        previous = self.day(day)
        if previous and previous.get("status") == "imported":
            previous = {**previous, "last_problem": {"status": status, "reason": reason}}
            self._set_day(day, previous)
        else:
            self._set_day(day, {"status": status, "reason": reason, "message": message})
        await self._async_save()

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

    def _set_day(self, day: date, record: dict[str, Any]) -> None:
        days: dict[str, Any] = self._data["days"]
        days[day.isoformat()] = {**record, "at": datetime.now(UTC).isoformat(timespec="seconds")}
        if len(days) > MAX_DAY_ENTRIES:
            for key in sorted(days)[: len(days) - MAX_DAY_ENTRIES]:
                del days[key]
