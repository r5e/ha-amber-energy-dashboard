"""Import manager: Guard 1, retention discovery, catch-up walk and scheduling.

DESIGN sections 7 to 10. One manager per config entry. Every run and every
``import_day`` call holds the entry's lock, so writes never race.

Crash safety: before a day is written, the Store records it as ``pending``. The marker
advances only after the day is verified. On the next run, Guard 1 accepts exactly one
inconsistency, a pending day that may be fully or partly written, and rewrites it from
the marker's baseline. Any other disagreement between the marker and the statistics
table raises a Repairs issue and stops. The manager never guesses.
"""

from collections import defaultdict
from collections.abc import Callable
from datetime import date, datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import importer
from .api import (
    AmberAuthError,
    AmberBudgetExhaustedError,
    AmberConnectionError,
    AmberError,
    AmberRateLimitError,
    AmberServerError,
    RunBudget,
    UsageRecord,
)
from .const import (
    DOMAIN,
    FETCH_WINDOW_DAYS,
    NEM_TZ,
    RETENTION_BRACKET_DAYS,
    RETENTION_DISCOVERY_MAX_CALLS,
    RETENTION_FALLBACK_DAYS,
)
from .importer import (
    HOUR,
    ImportContext,
    ImportDayError,
    ImportRefusedError,
    IncompleteDataError,
    VerificationFailedError,
    nem_day_start,
)
from .schedule import ScheduleConfig, is_final_attempt, next_attempt
from .storage import AmberStore

_LOGGER = logging.getLogger(__name__)

ISSUE_MARKER_MISMATCH = "marker_mismatch"
ISSUE_UNEXPECTED_CHANNEL = "unexpected_channel"
ISSUE_INCOMPLETE_DATA = "incomplete_data"
ISSUE_IMPORT_FAILED = "import_failed"
_DATA_ISSUES = (ISSUE_UNEXPECTED_CHANNEL, ISSUE_INCOMPLETE_DATA, ISSUE_IMPORT_FAILED)

# Run outcomes (also the status sensor's states)
STATUS_NEVER_RUN = "never_run"
STATUS_RUNNING = "running"
STATUS_CAUGHT_UP = "caught_up"
STATUS_WAITING = "waiting_for_data"
STATUS_BUDGET = "budget_exhausted"
STATUS_RATE_LIMITED = "rate_limited"
STATUS_UNAVAILABLE = "api_unavailable"
STATUS_AUTH = "auth_failed"
STATUS_ATTENTION = "needs_attention"
STATUSES = [
    STATUS_NEVER_RUN,
    STATUS_RUNNING,
    STATUS_CAUGHT_UP,
    STATUS_WAITING,
    STATUS_BUDGET,
    STATUS_RATE_LIMITED,
    STATUS_UNAVAILABLE,
    STATUS_AUTH,
    STATUS_ATTENTION,
]


class MarkerMismatchError(ImportRefusedError):
    """Guard 1: the Store marker and the statistics table disagree."""


def _utcnow() -> datetime:
    """The manager's clock (patched in tests; the event loop's clock is left alone)."""
    return dt_util.utcnow()


def nem_today(now: datetime | None = None) -> date:
    """Return today's NEM date."""
    return (now or _utcnow()).astimezone(NEM_TZ).date()


def _day_last_hour(day: date) -> datetime:
    return nem_day_start(day) + 23 * HOUR


class AmberManager:
    """Owns the Store, the run lock, the schedule and the display snapshot."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        ctx: ImportContext,
        store: AmberStore,
        schedule: ScheduleConfig,
        *,
        active_from: date | None = None,
    ) -> None:
        """Create the manager (call async_start after the store is loaded)."""
        self.hass = hass
        self.entry = entry
        self.ctx = ctx
        self.store = store
        self.schedule = schedule
        self.active_from = active_from
        self.status = STATUS_NEVER_RUN if store.last_run is None else store.last_run["status"]
        self.next_run: datetime | None = None
        self.coordinator: DataUpdateCoordinator[dict[str, Any]] = DataUpdateCoordinator(
            hass, _LOGGER, config_entry=entry, name=f"{DOMAIN} status"
        )
        self._unsub_timer: CALLBACK_TYPE | None = None
        self._ids = [spec.statistic_id for spec in ctx.specs]

    # --- lifecycle -----------------------------------------------------------------

    @callback
    def async_start(self) -> None:
        """Schedule the next attempt and publish the initial snapshot."""
        self._schedule_next()
        self._publish()

    @callback
    def async_stop(self) -> None:
        """Cancel the timer."""
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None

    @property
    def needs_startup_run(self) -> bool:
        """True on first setup, or if the previous run was interrupted (for example a crash)."""
        last = self.store.last_run
        return self.store.marker is None or (last is not None and last["status"] == STATUS_RUNNING)

    @callback
    def async_update_schedule(self, schedule: ScheduleConfig) -> None:
        """Apply a new schedule (options flow) without reloading the entry."""
        self.schedule = schedule
        self.async_stop()
        self._schedule_next()
        self._publish()

    # --- scheduling ----------------------------------------------------------------

    @property
    def _tz(self):
        return dt_util.get_time_zone(self.hass.config.time_zone)

    @callback
    def _schedule_next(self) -> None:
        self.next_run = next_attempt(_utcnow(), self.schedule, self.store.schedule_seed, self._tz)
        self._unsub_timer = async_track_point_in_utc_time(self.hass, self._on_timer, self.next_run)

    @callback
    def _on_timer(self, when: datetime) -> None:
        self._unsub_timer = None
        final = is_final_attempt(when, self.schedule, self.store.schedule_seed, self._tz)
        self._schedule_next()
        self.entry.async_create_background_task(
            self.hass, self._async_scheduled(final), f"{DOMAIN} scheduled run"
        )

    async def _async_scheduled(self, final: bool) -> None:
        if self.caught_up:
            _LOGGER.debug("Caught up; skipping scheduled attempt")
            self._publish()
            return
        await self.async_run("scheduled" + (" (final)" if final else ""))

    @property
    def caught_up(self) -> bool:
        """True when yesterday (NEM) has been imported."""
        marker = self.store.marker
        return marker is not None and marker >= nem_today() - timedelta(days=1)

    # --- runs ---------------------------------------------------------------------

    async def async_run(self, trigger: str) -> dict[str, Any]:
        """Run a catch-up now. Returns a summary; never raises for API or data stops."""
        async with self.ctx.lock:
            started = _utcnow()
            self.status = STATUS_RUNNING
            await self.store.async_set_last_run(
                {"status": STATUS_RUNNING, "trigger": trigger, "started": started.isoformat()}
            )
            self._publish()
            budget = self.ctx.client.start_run(RunBudget())
            imported: list[str] = []
            outcome: dict[str, Any]
            try:
                outcome = await self._async_walk(imported)
            except AmberBudgetExhaustedError as err:
                outcome = {"status": STATUS_BUDGET, "reason": str(err)}
            except AmberRateLimitError as err:
                outcome = {"status": STATUS_RATE_LIMITED, "reason": str(err)}
            except AmberAuthError:
                self.entry.async_start_reauth(self.hass)
                outcome = {"status": STATUS_AUTH, "reason": "Amber rejected the API key"}
            except (AmberConnectionError, AmberServerError) as err:
                outcome = {"status": STATUS_UNAVAILABLE, "reason": str(err)}
            except AmberError as err:
                outcome = {"status": STATUS_UNAVAILABLE, "reason": f"unexpected response: {err}"}
            except ImportDayError as err:
                outcome = {"status": STATUS_ATTENTION, "reason": err.reason, "message": str(err)}
            except Exception as err:
                _LOGGER.exception("Unexpected error during import run")
                outcome = {
                    "status": STATUS_ATTENTION,
                    "reason": "internal_error",
                    "message": f"{type(err).__name__}: {err}",
                }
            finally:
                self.ctx.client.end_run()
            summary = {
                **outcome,
                "trigger": trigger,
                "started": started.isoformat(),
                "finished": _utcnow().isoformat(),
                "imported_days": imported,
                "marker": self.store.marker.isoformat() if self.store.marker else None,
                "calls": dict(budget.calls),
                "rate_limit_remaining": dict(budget.remaining),
            }
            self.status = summary["status"]
            await self.store.async_set_last_run(summary)
            self._publish()
            _LOGGER.info(
                "Run (%s) finished: %s, %d day(s) imported, marker %s",
                trigger,
                summary["status"],
                len(imported),
                summary["marker"],
            )
            return summary

    async def _async_walk(self, imported: list[str]) -> dict[str, Any]:
        recovery = await self._async_guard1()
        marker = self.store.marker
        yesterday = nem_today() - timedelta(days=1)
        if marker is None and recovery is None:
            if self.store.retention_days is None:
                await self._async_discover_retention()
            start = nem_today() - timedelta(days=self.store.retention_days)
        else:
            start = recovery or marker + timedelta(days=1)

        day = start
        while day <= yesterday:
            end = min(day + timedelta(days=FETCH_WINDOW_DAYS - 1), yesterday)
            records = await self.ctx.client.async_get_usage(self.ctx.site_id, day, end)
            by_date = self._split_by_date(records, day, end)
            while day <= end:
                day_records = by_date.get(day, [])
                if not day_records:
                    await self.store.async_mark_day(day, "empty", "no_data")
                    return {
                        "status": STATUS_WAITING,
                        "reason": f"no usage for {day} yet",
                        "waiting_for": day.isoformat(),
                    }
                await self._async_write(
                    day, day_records, mode="recovery" if day == recovery else "catch_up"
                )
                imported.append(day.isoformat())
                day += timedelta(days=1)
        return {"status": STATUS_CAUGHT_UP, "reason": None}

    def _split_by_date(
        self, records: list[UsageRecord], start: date, end: date
    ) -> dict[date, list[UsageRecord]]:
        by_date: dict[date, list[UsageRecord]] = defaultdict(list)
        for record in records:
            if not start <= record.date <= end:
                raise IncompleteDataError(
                    "wrong_date",
                    f"usage for {start} to {end} contains a record dated {record.date}",
                )
            by_date[record.date].append(record)
        return by_date

    async def _async_write(
        self, day: date, records: list[UsageRecord], mode: str
    ) -> dict[str, Any]:
        """Write one day after the marker (or the pending day), then advance the marker."""
        marker = self.store.marker
        if marker is None:
            baselines = dict.fromkeys(self._ids, 0.0)
        else:
            baselines = await importer.async_baselines_before(self.hass, self._ids, day)
        await self.store.async_set_pending(day)
        try:
            summary = await importer.async_write_day(
                self.hass, self.ctx, day, records, baselines, mode=mode
            )
        except IncompleteDataError as err:
            # Guard 3 stops before any write, so nothing is pending any more.
            await self.store.async_clear_pending()
            await self.store.async_mark_day(day, "failed", err.reason, str(err))
            self._raise_data_issue(err, day)
            raise
        except VerificationFailedError as err:
            await self.store.async_mark_day(day, "failed", err.reason, str(err))
            self._create_issue(ISSUE_IMPORT_FAILED, {"day": day.isoformat(), "details": str(err)})
            raise
        await self.store.async_mark_imported(day, summary)
        self._clear_data_issues()
        return summary

    # --- Guard 1 ------------------------------------------------------------------

    async def _async_guard1(self) -> date | None:
        """Check the marker against the statistics table.

        Returns the pending day to rewrite, if a previous write was interrupted.
        Raises MarkerMismatchError (with a Repairs issue) on any other disagreement.
        """
        marker, pending = self.store.marker, self.store.pending
        latest = await importer.async_latest_hours(self.hass, self._ids)
        values = set(latest.values())
        expected = _day_last_hour(marker) if marker else None
        if values == {expected}:
            self._delete_issue(ISSUE_MARKER_MISMATCH)
            if pending is not None:
                await self.store.async_clear_pending()
            return None
        next_day = marker + timedelta(days=1) if marker else pending
        if (
            pending is not None
            and pending == next_day
            and values <= {expected, _day_last_hour(pending)}
        ):
            _LOGGER.warning("Recovering interrupted write of %s", pending)
            self._delete_issue(ISSUE_MARKER_MISMATCH)
            return pending

        found = sorted({v.isoformat() if v else "none" for v in values})
        details = (
            f"marker {marker or 'none'} expects the last stored hour to be "
            f"{expected.isoformat() if expected else 'none (no statistics)'}, "
            f"but the statistics end at {', '.join(found)}"
        )
        self._create_issue(ISSUE_MARKER_MISMATCH, {"details": details})
        raise MarkerMismatchError(
            "marker_mismatch", f"Import marker and statistics disagree: {details}"
        )

    # --- retention discovery ------------------------------------------------------

    async def _async_discover_retention(self) -> None:
        """Find the earliest NEM day with usage by bisection (at most 8 calls)."""
        today = nem_today()
        calls = 0
        probes: dict[str, bool] = {}

        async def has_data(day: date) -> bool:
            nonlocal calls
            if self.active_from is not None and day < self.active_from:
                probes[day.isoformat()] = False
                return False
            if calls >= RETENTION_DISCOVERY_MAX_CALLS:
                raise _DiscoveryIncomplete
            calls += 1
            result = bool(await self.ctx.client.async_get_usage(self.ctx.site_id, day, day))
            probes[day.isoformat()] = result
            return result

        method = "bisection"
        try:
            boundary = await self._search_boundary(today, has_data)
        except (_DiscoveryIncomplete, AmberError) as err:
            boundary = None
            method = f"fallback ({type(err).__name__})"
            if isinstance(err, AmberAuthError | AmberBudgetExhaustedError):
                await self._store_retention(RETENTION_FALLBACK_DAYS, method, calls, probes)
                raise
        if boundary is None and method == "bisection":
            method = "fallback (not bracketed)"
        days = (today - boundary).days if boundary else RETENTION_FALLBACK_DAYS
        await self._store_retention(days, method, calls, probes)

    async def _search_boundary(self, today: date, has_data: Callable) -> date | None:
        """Probe around the expected boundary, then bisect. None if it cannot bracket it."""
        guess = today - timedelta(days=RETENTION_FALLBACK_DAYS)
        if self.active_from is not None and self.active_from > guess:
            # A site younger than the usual window: its data starts at activeFrom.
            return self.active_from if await has_data(self.active_from) else None
        if await has_data(guess):
            return await self._search_older(guess, has_data)
        return await self._search_newer(guess, has_data)

    async def _search_older(self, guess: date, has_data: Callable) -> date | None:
        """``guess`` has data: the boundary is ``guess`` or up to 30 days before it."""
        before = guess - timedelta(days=1)
        if not await has_data(before):
            return guess
        lo = before - timedelta(days=RETENTION_BRACKET_DAYS)
        if await has_data(lo):
            return None
        return await self._bisect(lo, before, has_data)

    async def _search_newer(self, guess: date, has_data: Callable) -> date | None:
        """``guess`` is empty: the boundary is up to 30 days after it."""
        after = guess + timedelta(days=1)
        if await has_data(after):
            return after
        hi = after + timedelta(days=RETENTION_BRACKET_DAYS)
        if not await has_data(hi):
            return None
        return await self._bisect(after, hi, has_data)

    async def _bisect(self, lo: date, hi: date, has_data: Callable) -> date:
        """lo has no data, hi has data: return the earliest day with data."""
        while (hi - lo).days > 1:
            mid = lo + (hi - lo) // 2
            if await has_data(mid):
                hi = mid
            else:
                lo = mid
        return hi

    async def _store_retention(self, days: int, method: str, calls: int, probes: dict) -> None:
        info = {
            "measured_on": nem_today().isoformat(),
            "method": method,
            "calls": calls,
            "probes": probes,
            "boundary": (nem_today() - timedelta(days=days)).isoformat(),
        }
        _LOGGER.info("Usage retention: %d days (%s, %d calls)", days, method, calls)
        await self.store.async_set_retention(days, info)

    # --- import_day service -------------------------------------------------------

    async def async_import_day(self, day: date) -> dict[str, Any]:
        """The import_day service: Guard 1, then the M2 append-only rule for one day."""
        async with self.ctx.lock:
            recovery = await self._async_guard1()
            if recovery is not None and day != recovery:
                raise ImportRefusedError(
                    "recovery_pending",
                    f"{recovery} was being written when a previous run stopped; import "
                    f"{recovery} (or use run_now) first.",
                )
            if recovery is not None:
                baselines = (
                    await importer.async_baselines_before(self.hass, self._ids, day)
                    if self.store.marker
                    else dict.fromkeys(self._ids, 0.0)
                )
                mode = "recovery"
            else:
                mode, baselines = await importer.async_plan_day(self.hass, self.ctx, day)
            self.ctx.client.start_run(RunBudget())
            try:
                records = await self.ctx.client.async_get_usage(self.ctx.site_id, day, day)
            finally:
                self.ctx.client.end_run()
            if mode != "reimport":
                await self.store.async_set_pending(day)
            try:
                summary = await importer.async_write_day(
                    self.hass, self.ctx, day, records, baselines, mode=mode
                )
            except IncompleteDataError as err:
                await self.store.async_clear_pending()
                await self.store.async_mark_day(day, "failed", err.reason, str(err))
                self._raise_data_issue(err, day)
                raise
            except VerificationFailedError as err:
                await self.store.async_mark_day(day, "failed", err.reason, str(err))
                self._create_issue(
                    ISSUE_IMPORT_FAILED, {"day": day.isoformat(), "details": str(err)}
                )
                raise
            await self.store.async_mark_imported(day, summary)
            self._clear_data_issues()
            self._publish()
            return summary

    # --- Repairs ------------------------------------------------------------------

    def _issue_id(self, kind: str) -> str:
        return f"{kind}_{self.entry.entry_id}"

    def _create_issue(self, kind: str, placeholders: dict[str, str]) -> None:
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._issue_id(kind),
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=kind,
            translation_placeholders={"entry": self.entry.title, **placeholders},
        )

    def _delete_issue(self, kind: str) -> None:
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id(kind))

    def _raise_data_issue(self, err: IncompleteDataError, day: date) -> None:
        if err.reason == "unexpected_channel":
            self._create_issue(
                ISSUE_UNEXPECTED_CHANNEL, {"day": day.isoformat(), "details": str(err)}
            )
        else:
            self._create_issue(
                ISSUE_INCOMPLETE_DATA,
                {"day": day.isoformat(), "reason": err.reason, "details": str(err)},
            )

    def _clear_data_issues(self) -> None:
        for kind in _DATA_ISSUES:
            self._delete_issue(kind)

    # --- display snapshot ---------------------------------------------------------

    @callback
    def _publish(self) -> None:
        self.coordinator.async_set_updated_data(self.snapshot())

    def snapshot(self) -> dict[str, Any]:
        """State for the display sensors."""
        marker = self.store.marker
        yesterday = nem_today() - timedelta(days=1)
        record = self.store.day(yesterday) or {}
        totals = record.get("totals") if record.get("status") == "imported" else None
        return {
            "status": self.status,
            "last_run": self.store.last_run,
            "marker": marker,
            "days_behind": (yesterday - marker).days if marker else None,
            "next_run": self.next_run,
            "yesterday": yesterday,
            "yesterday_totals": totals,
        }


class _DiscoveryIncomplete(Exception):
    """Retention discovery could not finish within its call cap."""
