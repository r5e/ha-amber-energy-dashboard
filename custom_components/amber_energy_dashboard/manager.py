"""Import manager: Guards 1 and 2, retention, revisions, catch-up walk and scheduling.

DESIGN sections 7 to 10. One manager per config entry. Every run and every
``import_day`` call holds the entry's lock, so writes never race.

A run does, in order:

1. **Guard 1.** The last stored hour of every statistic must match ``last_written``
   (the last day with statistics). The only accepted difference is an interrupted write
   of the ``pending`` day (marker + 1), which is then rewritten. Anything else raises a
   Repairs issue and stops. The manager never guesses.
2. **Tail rewrite resume**, if a previous revision rewrite did not finish.
3. **Retention.** Discovery on first setup, then a daily 2-call re-verify that
   self-corrects in either direction (one-day step, or bisection within 8 calls).
4. **Revisions**, once a day: days with estimated data are re-fetched for
   ``revision_days``. If any changed, the tail from the earliest changed day to
   ``last_written`` is rewritten through the shared per-day path, re-deriving every sum.
5. **The walk** from marker + 1 to yesterday, in 7-day windows. Days older than the
   boundary are marked ``skipped_unavailable``. An empty day waits (Guard 2 patience)
   until it has been empty on ``patience_days`` distinct days and a later day has data;
   then it is marked ``skipped_gap`` and the walk continues.
"""

from collections import defaultdict
from collections.abc import Callable, Iterable
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
    DEFAULT_PATIENCE_DAYS,
    DEFAULT_REVISION_DAYS,
    DOMAIN,
    FETCH_WINDOW_DAYS,
    NEM_TZ,
    RETENTION_BRACKET_DAYS,
    RETENTION_DISCOVERY_MAX_CALLS,
    RETENTION_FALLBACK_DAYS,
    RETENTION_REVERIFY_BRACKET_DAYS,
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
from .statistics import StatisticSpec
from .storage import STATUS_SKIPPED_GAP, STATUS_SKIPPED_UNAVAILABLE, AmberStore

_LOGGER = logging.getLogger(__name__)

ISSUE_MARKER_MISMATCH = "marker_mismatch"
ISSUE_UNEXPECTED_CHANNEL = "unexpected_channel"
ISSUE_INCOMPLETE_DATA = "incomplete_data"
ISSUE_IMPORT_FAILED = "import_failed"
ISSUE_BEHIND = "behind"
_DATA_ISSUES = (ISSUE_UNEXPECTED_CHANNEL, ISSUE_INCOMPLETE_DATA, ISSUE_IMPORT_FAILED)
_SKIPPED = (STATUS_SKIPPED_GAP, STATUS_SKIPPED_UNAVAILABLE)

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


class _DiscoveryIncomplete(Exception):
    """Retention discovery could not finish within its call cap."""


def _utcnow() -> datetime:
    """The manager's clock (patched in tests; the event loop's clock is left alone)."""
    return dt_util.utcnow()


def nem_today(now: datetime | None = None) -> date:
    """Return today's NEM date."""
    return (now or _utcnow()).astimezone(NEM_TZ).date()


def _day_last_hour(day: date) -> datetime:
    return nem_day_start(day) + 23 * HOUR


def _days(first: date, last: date) -> Iterable[date]:
    day = first
    while day <= last:
        yield day
        day += timedelta(days=1)


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
        patience_days: int = DEFAULT_PATIENCE_DAYS,
        revision_days: int = DEFAULT_REVISION_DAYS,
    ) -> None:
        """Create the manager (call async_start after the store is loaded)."""
        self.hass = hass
        self.entry = entry
        self.ctx = ctx
        self.store = store
        self.schedule = schedule
        self.active_from = active_from
        self.patience_days = patience_days
        self.revision_days = revision_days
        self.status = STATUS_NEVER_RUN if store.last_run is None else store.last_run["status"]
        self.next_run: datetime | None = None
        self.coordinator: DataUpdateCoordinator[dict[str, Any]] = DataUpdateCoordinator(
            hass, _LOGGER, config_entry=entry, name=f"{DOMAIN} status"
        )
        self._unsub_timer: CALLBACK_TYPE | None = None
        self._final_attempt = False

    @property
    def _ids(self) -> list[str]:
        return [spec.statistic_id for spec in self.ctx.specs]

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
    def async_update_settings(
        self, schedule: ScheduleConfig, patience_days: int, revision_days: int
    ) -> None:
        """Apply new options (options flow) without reloading the entry."""
        self.schedule = schedule
        self.patience_days = patience_days
        self.revision_days = revision_days
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
            self._delete_issue(ISSUE_BEHIND)
            self._publish()
            return
        await self.async_run("scheduled (final)" if final else "scheduled", final=final)

    @property
    def caught_up(self) -> bool:
        """True when yesterday (NEM) is resolved."""
        marker = self.store.marker
        return marker is not None and marker >= nem_today() - timedelta(days=1)

    # --- runs ---------------------------------------------------------------------

    async def async_run(self, trigger: str, *, final: bool = False) -> dict[str, Any]:
        """Run a catch-up now. Returns a summary; never raises for API or data stops.

        ``final`` marks the last scheduled attempt of the day: only then are the
        "still behind" and ``incomplete_data`` Repairs issues raised.
        """
        async with self.ctx.lock:
            started = _utcnow()
            self.status = STATUS_RUNNING
            self._final_attempt = final
            await self.store.async_set_last_run(
                {"status": STATUS_RUNNING, "trigger": trigger, "started": started.isoformat()}
            )
            self._publish()
            budget = self.ctx.client.start_run(RunBudget())
            imported: list[str] = []
            info: dict[str, Any] = {}
            outcome: dict[str, Any]
            try:
                outcome = await self._async_run_steps(imported, info)
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
                self._final_attempt = False
            summary = {
                **outcome,
                **info,
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
            self._update_behind_issue(summary, final)
            self._publish()
            _LOGGER.info(
                "Run (%s) finished: %s, %d day(s) imported, marker %s",
                trigger,
                summary["status"],
                len(imported),
                summary["marker"],
            )
            return summary

    async def _async_run_steps(self, imported: list[str], info: dict[str, Any]) -> dict[str, Any]:
        recovery = await self._async_guard1()
        if self.store.tail_rewrite is not None:
            progress = self.store.tail_rewrite
            info["tail_resumed"] = progress
            await self._async_tail_rewrite(date.fromisoformat(progress["next"]), {})
        retention = self.store.retention or {}
        if self.store.retention_days is None or retention.get("needs_discovery"):
            await self._async_discover_retention()
            info["retention"] = "discovered"
        elif retention.get("last_verified") != nem_today().isoformat():
            info["retention"] = await self._async_verify_retention()
        info["revisions"] = await self._async_check_revisions()
        return await self._async_walk(recovery, imported, info)

    # --- per-day context -----------------------------------------------------------

    def _channel_active(self, identifier: str | None, day: date) -> bool:
        if identifier is None:
            return True
        since = self.store.channel_since(identifier)
        return since is None or day >= since

    def _ctx_for(self, day: date) -> ImportContext:
        """The channels and statistics that exist on ``day`` (channels added later start
        at their ``channel_since`` day)."""
        channels = tuple(c for c in self.ctx.channels if self._channel_active(c.identifier, day))
        if len(channels) == len(self.ctx.channels):
            return self.ctx
        specs = tuple(s for s in self.ctx.specs if self._channel_active(s.channel, day))
        return ImportContext(self.ctx.client, self.ctx.site_id, channels, specs, self.ctx.lock)

    def _spec(self, statistic_id: str) -> StatisticSpec:
        return next(s for s in self.ctx.specs if s.statistic_id == statistic_id)

    async def _async_baselines(self, day: date, ctx: ImportContext) -> dict[str, float]:
        """Each statistic's cumulative sum at the end of the last imported day before ``day``."""
        last_written = self.store.last_written
        if last_written is not None and last_written < day:
            prev: date | None = last_written
        else:
            prev = self.store.last_imported_before(day)
        ids = [spec.statistic_id for spec in ctx.specs]
        if prev is None:
            return dict.fromkeys(ids, 0.0)
        sums = await importer.async_sums_at(self.hass, ids, _day_last_hour(prev))
        baselines: dict[str, float] = {}
        for sid in ids:
            value = sums[sid]
            if value is None:
                # Only a channel added after setup may have no history yet.
                if not self._channel_active(self._spec(sid).channel, prev):
                    value = 0.0
                else:
                    raise ImportRefusedError(
                        "partial_history",
                        f"{sid} has no row at the end of {prev}; cannot continue the sums.",
                    )
            baselines[sid] = value
        return baselines

    # --- Guard 1 ------------------------------------------------------------------

    def _expected_end(self, sid: str, last_written: date | None) -> datetime | None:
        if last_written is None or not self._channel_active(self._spec(sid).channel, last_written):
            return None
        return _day_last_hour(last_written)

    def _marker_problem(self, marker: date | None, last_written: date | None) -> str | None:
        """The marker may only be ahead of last_written by days recorded as skipped."""
        if marker is None:
            return None if last_written is None else f"no marker, but {last_written} is imported"
        if last_written is None:
            imported = sorted(
                d
                for d, rec in self.store.as_dict()["days"].items()
                if d <= marker.isoformat() and rec.get("status") == "imported"
            )
            if imported:
                return f"marker {marker}, but days {imported} are recorded as imported"
            return None
        if marker < last_written:
            return f"marker {marker} is before the last imported day {last_written}"
        for day in _days(last_written + timedelta(days=1), marker):
            if (self.store.day(day) or {}).get("status") not in _SKIPPED:
                return (
                    f"marker {marker} is ahead of the last imported day {last_written}, "
                    f"but {day} is not recorded as skipped"
                )
        return None

    async def _async_guard1(self) -> date | None:
        """Check ``last_written`` against the statistics table.

        Returns the pending day to rewrite, if a previous write was interrupted.
        Raises MarkerMismatchError (with a Repairs issue) on any other disagreement.
        """
        marker, last_written, pending = (
            self.store.marker,
            self.store.last_written,
            self.store.pending,
        )
        if problem := self._marker_problem(marker, last_written):
            self._create_issue(ISSUE_MARKER_MISMATCH, {"details": problem})
            raise MarkerMismatchError(
                "marker_mismatch", f"Import marker and statistics disagree: {problem}"
            )
        latest = await importer.async_latest_hours(self.hass, self._ids)
        expected = {sid: self._expected_end(sid, last_written) for sid in self._ids}
        if all(latest[sid] == expected[sid] for sid in self._ids):
            self._delete_issue(ISSUE_MARKER_MISMATCH)
            if pending is not None:
                await self.store.async_clear_pending()
            return None
        next_day = marker + timedelta(days=1) if marker else pending
        if pending is not None and pending == next_day:
            pending_end = _day_last_hour(pending)
            if all(
                latest[sid] == expected[sid]
                or (
                    latest[sid] == pending_end
                    and self._channel_active(self._spec(sid).channel, pending)
                )
                for sid in self._ids
            ):
                _LOGGER.warning("Recovering interrupted write of %s", pending)
                self._delete_issue(ISSUE_MARKER_MISMATCH)
                return pending

        found = sorted({v.isoformat() if v else "none" for v in latest.values()})
        want = sorted({v.isoformat() if v else "none (no statistics)" for v in expected.values()})
        details = (
            f"last imported day {last_written or 'none'} (marker {marker or 'none'}) expects "
            f"the last stored hour to be {', '.join(want)}, but the statistics end at "
            f"{', '.join(found)}"
        )
        self._create_issue(ISSUE_MARKER_MISMATCH, {"details": details})
        raise MarkerMismatchError(
            "marker_mismatch", f"Import marker and statistics disagree: {details}"
        )

    # --- the walk -----------------------------------------------------------------

    async def _async_walk(
        self, recovery: date | None, imported: list[str], info: dict[str, Any]
    ) -> dict[str, Any]:
        today = nem_today()
        yesterday = today - timedelta(days=1)
        boundary = today - timedelta(days=self.store.retention_days)
        marker = self.store.marker
        if recovery is not None:
            start = recovery
        elif marker is not None:
            start = marker + timedelta(days=1)
        else:
            start = boundary

        if start < boundary:
            if recovery is not None:
                raise ImportDayError(
                    "recovery_unavailable",
                    f"{recovery} was being written when a run stopped, but it is now older "
                    "than Amber's retention and cannot be fetched again.",
                )
            last = min(boundary - timedelta(days=1), yesterday)
            await self.store.async_mark_skipped(
                start, last, STATUS_SKIPPED_UNAVAILABLE, "older than Amber's usage retention"
            )
            info["skipped_unavailable"] = {
                "from": start.isoformat(),
                "to": last.isoformat(),
                "days": (last - start).days + 1,
            }
            _LOGGER.warning("Skipped %s to %s: older than Amber's retention", start, last)
            start = last + timedelta(days=1)

        day = start
        while day <= yesterday:
            end = min(day + timedelta(days=FETCH_WINDOW_DAYS - 1), yesterday)
            records = await self.ctx.client.async_get_usage(self.ctx.site_id, day, end)
            by_date = self._split_by_date(records, day, end)
            while day <= end:
                day_records = by_date.get(day, [])
                if not day_records:
                    waiting = await self._async_patience(day, by_date, end, yesterday, info)
                    if waiting is not None:
                        return waiting
                else:
                    mode = "recovery" if day == recovery else "catch_up"
                    await self._async_write(day, day_records, mode)
                    imported.append(day.isoformat())
                day += timedelta(days=1)
        return {"status": STATUS_CAUGHT_UP, "reason": None}

    async def _async_patience(
        self,
        day: date,
        by_date: dict[date, list[UsageRecord]],
        window_end: date,
        yesterday: date,
        info: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Guard 2: decide whether an empty day is a gap to skip. None means skipped."""
        count = await self.store.async_note_empty(day, nem_today())
        waiting = {
            "status": STATUS_WAITING,
            "reason": f"no usage for {day} yet",
            "waiting_for": day.isoformat(),
            "empty_days_seen": count,
        }
        if count < self.patience_days:
            return waiting
        later = any(d > day for d in by_date)
        if not later and window_end < yesterday:
            probe_end = min(window_end + timedelta(days=FETCH_WINDOW_DAYS), yesterday)
            later = bool(
                await self.ctx.client.async_get_usage(
                    self.ctx.site_id, window_end + timedelta(days=1), probe_end
                )
            )
        if not later:
            waiting["reason"] = f"no usage for {day} on {count} days, and none for later days yet"
            return waiting
        await self.store.async_mark_skipped(
            day,
            day,
            STATUS_SKIPPED_GAP,
            f"empty on {count} separate days while later days have data",
        )
        info.setdefault("skipped_gap", []).append(day.isoformat())
        _LOGGER.warning("Skipped %s as a gap: empty on %d days, later days have data", day, count)
        return None

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
        """Write one day through the shared path, then record it in the Store.

        Modes ``catch_up`` and ``recovery`` write marker + 1 (``pending`` is set first);
        ``revision`` rewrites a day inside history during a tail rewrite.
        """
        ctx = self._ctx_for(day)
        baselines = await self._async_baselines(day, ctx)
        tail = mode == "revision"
        if not tail:
            await self.store.async_set_pending(day)
        try:
            summary = await importer.async_write_day(
                self.hass, ctx, day, records, baselines, mode=mode, expect_latest=not tail
            )
        except IncompleteDataError as err:
            if not tail:
                await self.store.async_clear_pending()
            await self.store.async_mark_day(day, "failed", err.reason, str(err))
            self._raise_data_issue(err, day)
            raise
        except VerificationFailedError as err:
            await self.store.async_mark_day(day, "failed", err.reason, str(err))
            self._create_issue(ISSUE_IMPORT_FAILED, {"day": day.isoformat(), "details": str(err)})
            raise
        await self.store.async_mark_imported(day, summary)
        await self._async_track_revision(day, records, summary)
        self._clear_data_issues()
        return summary

    # --- revisions ----------------------------------------------------------------

    async def _async_track_revision(
        self, day: date, records: list[UsageRecord], summary: dict[str, Any]
    ) -> None:
        if summary.get("estimated_records"):
            await self.store.async_set_revision(
                day, nem_today(), importer.revision_fingerprint(records)
            )
        elif day.isoformat() in self.store.revisions:
            await self.store.async_drop_revisions([day])

    async def _async_fetch_days(
        self, days: Iterable[date], fetched: dict[date, list[UsageRecord]], last: date
    ) -> None:
        """Fetch the given days (windows of up to 7 days, capped at ``last``)."""
        for day in sorted(days):
            if day in fetched:
                continue
            end = min(day + timedelta(days=FETCH_WINDOW_DAYS - 1), last)
            records = await self.ctx.client.async_get_usage(self.ctx.site_id, day, end)
            by_date = self._split_by_date(records, day, end)
            for d in _days(day, end):
                fetched[d] = by_date.get(d, [])

    async def _async_check_revisions(self) -> dict[str, Any] | None:
        """Once a day: re-fetch estimated days; rewrite the tail if any changed."""
        today = nem_today()
        if self.store.revisions_checked == today:
            return None
        last_written = self.store.last_written
        entries = self.store.revisions
        expired = [
            date.fromisoformat(d)
            for d, e in entries.items()
            if last_written is None
            or date.fromisoformat(d) > last_written
            or date.fromisoformat(e["since"]) + timedelta(days=self.revision_days) < today
        ]
        if expired:
            await self.store.async_drop_revisions(expired)
        days = sorted(date.fromisoformat(d) for d in self.store.revisions)
        result: dict[str, Any] = {
            "checked": [d.isoformat() for d in days],
            "expired": sorted(d.isoformat() for d in expired),
            "changed": [],
            "rewritten_days": 0,
        }
        if days:
            assert last_written is not None
            fetched: dict[date, list[UsageRecord]] = {}
            await self._async_fetch_days(days, fetched, last_written)
            changed, final = [], []
            for day in days:
                records = fetched.get(day, [])
                if not records:
                    continue  # not available right now; try again tomorrow
                if (
                    importer.revision_fingerprint(records)
                    != self.store.revisions[day.isoformat()]["hash"]
                ):
                    changed.append(day)
                elif not any(r.quality == "estimated" for r in records):
                    final.append(day)
            if final:
                await self.store.async_drop_revisions(final)
            if changed:
                result["changed"] = [d.isoformat() for d in changed]
                _LOGGER.info(
                    "Revised data for %s; rewriting from %s", result["changed"], changed[0]
                )
                result["rewritten_days"] = await self._async_tail_rewrite(changed[0], fetched)
        await self.store.async_set_revisions_checked(today)
        return result

    async def _async_tail_rewrite(self, first: date, fetched: dict[date, list[UsageRecord]]) -> int:
        """Rewrite every imported day from ``first`` to ``last_written``, in order.

        Progress is stored after each day, so an interrupted rewrite resumes where it
        stopped (from the first day not yet rewritten).
        """
        progress = self.store.tail_rewrite
        last = (
            date.fromisoformat(progress["to"]) if progress is not None else self.store.last_written
        )
        assert last is not None
        origin = progress["from"] if progress is not None else first.isoformat()
        await self.store.async_set_tail_rewrite(
            {"from": origin, "next": first.isoformat(), "to": last.isoformat()}
        )
        rewritten = 0
        for day in _days(first, last):
            record = self.store.day(day) or {}
            if record.get("status") not in _SKIPPED:
                if day not in fetched:
                    await self._async_fetch_days([day], fetched, last)
                records = fetched.get(day, [])
                if not records:
                    raise ImportDayError(
                        "tail_unavailable",
                        f"{day} returned no usage during a revision rewrite; it will be retried.",
                    )
                await self._async_write(day, records, "revision")
                rewritten += 1
            await self.store.async_set_tail_rewrite(
                {
                    "from": origin,
                    "next": (day + timedelta(days=1)).isoformat(),
                    "to": last.isoformat(),
                }
            )
        await self.store.async_set_tail_rewrite(None)
        return rewritten

    # --- retention ----------------------------------------------------------------

    def _prober(self, probes: dict[str, bool], counter: list[int]) -> Callable:
        async def has_data(day: date) -> bool:
            if day.isoformat() in probes:
                return probes[day.isoformat()]
            if self.active_from is not None and day < self.active_from:
                probes[day.isoformat()] = False
                return False
            if counter[0] >= RETENTION_DISCOVERY_MAX_CALLS:
                raise _DiscoveryIncomplete
            counter[0] += 1
            result = bool(await self.ctx.client.async_get_usage(self.ctx.site_id, day, day))
            probes[day.isoformat()] = result
            return result

        return has_data

    async def _async_discover_retention(self) -> None:
        """Find the earliest NEM day with usage by bisection (at most 8 calls)."""
        today = nem_today()
        probes: dict[str, bool] = {}
        counter = [0]
        has_data = self._prober(probes, counter)
        method = "bisection"
        try:
            boundary = await self._search_boundary(today, has_data)
        except (_DiscoveryIncomplete, AmberError) as err:
            boundary = None
            method = f"fallback ({type(err).__name__})"
            if isinstance(err, AmberAuthError | AmberBudgetExhaustedError):
                await self._store_retention(RETENTION_FALLBACK_DAYS, method, counter[0], probes)
                raise
        if boundary is None and method == "bisection":
            method = "fallback (not bracketed)"
        days = (today - boundary).days if boundary else RETENTION_FALLBACK_DAYS
        await self._store_retention(days, method, counter[0], probes)

    async def _async_verify_retention(self) -> dict[str, Any]:
        """Daily check: the boundary day has data and the day before does not.

        Self-corrects in either direction: a one-day move is confirmed with one more
        probe; a larger move is bisected within a 15-day bracket, keeping the whole
        check within 8 calls. If it cannot be resolved, the old value is kept and full
        discovery runs on the next day.
        """
        today = nem_today()
        previous = self.store.retention_days
        assert previous is not None
        boundary = today - timedelta(days=previous)
        probes: dict[str, bool] = {}
        counter = [0]
        has_data = self._prober(probes, counter)
        one = timedelta(days=1)
        bracket = timedelta(days=RETENTION_REVERIFY_BRACKET_DAYS)
        new: date | None = None
        try:
            at, before = await has_data(boundary), await has_data(boundary - one)
            if at and not before:
                new, method = boundary, "verified"
            elif not at and before:
                new, method = boundary, "verified (boundary day empty, day before has data: gap)"
            elif not at:
                if await has_data(boundary + one):
                    new, method = boundary + one, "moved forward 1 day"
                elif await has_data(boundary + one + bracket):
                    new = await self._bisect(boundary + one, boundary + one + bracket, has_data)
                    method = f"moved forward {(new - boundary).days} days (bisected)"
                else:
                    method = "unresolved (moved forward beyond the bracket)"
            elif not await has_data(boundary - 2 * one):
                new, method = boundary - one, "moved back 1 day"
            elif not await has_data(boundary - 2 * one - bracket):
                new = await self._bisect(boundary - 2 * one - bracket, boundary - 2 * one, has_data)
                method = f"moved back {(boundary - new).days} days (bisected)"
            else:
                method = "unresolved (moved back beyond the bracket)"
        except (_DiscoveryIncomplete, AmberError) as err:
            method = f"unresolved ({type(err).__name__})"
            if isinstance(err, AmberAuthError | AmberBudgetExhaustedError):
                raise
        days = (today - new).days if new else previous
        await self._store_retention(
            days, method, counter[0], probes, previous=previous, needs_discovery=new is None
        )
        return {"method": method, "calls": counter[0], "retention_days": days, "previous": previous}

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

    async def _store_retention(
        self,
        days: int,
        method: str,
        calls: int,
        probes: dict,
        *,
        previous: int | None = None,
        needs_discovery: bool = False,
    ) -> None:
        today = nem_today()
        info = {
            "measured_on": today.isoformat(),
            "last_verified": today.isoformat(),
            "method": method,
            "calls": calls,
            "probes": probes,
            "boundary": (today - timedelta(days=days)).isoformat(),
            "previous_days": previous,
            "needs_discovery": needs_discovery,
        }
        if previous is not None and previous != days:
            _LOGGER.warning(
                "Usage retention changed from %d to %d days (%s)", previous, days, method
            )
        _LOGGER.info("Usage retention: %d days (%s, %d calls)", days, method, calls)
        await self.store.async_set_retention(days, info)

    # --- import_day service -------------------------------------------------------

    async def async_import_day(self, day: date) -> dict[str, Any]:
        """The import_day service: Guard 1, then the day after the marker (append) or the
        latest imported day (re-import)."""
        async with self.ctx.lock:
            recovery = await self._async_guard1()
            marker, last_written = self.store.marker, self.store.last_written
            ctx = self._ctx_for(day)
            if recovery is not None and day != recovery:
                raise ImportRefusedError(
                    "recovery_pending",
                    f"{recovery} was being written when a previous run stopped; import "
                    f"{recovery} (or use run_now) first.",
                )
            if recovery is not None or marker is None or day == marker + timedelta(days=1):
                mode = "recovery" if recovery else ("first" if marker is None else "append")
                baselines = await self._async_baselines(day, ctx)
            elif day == last_written and marker == last_written:
                mode, baselines = await importer.async_plan_day(self.hass, ctx, day)
            else:
                allowed = f"{marker + timedelta(days=1)} (the next day)"
                if marker == last_written:
                    allowed += f" or {last_written} (re-import of the latest day)"
                raise ImportRefusedError(
                    "not_next_day",
                    f"{day} cannot be imported: the import has reached {marker}. "
                    f"Only {allowed} can be imported now.",
                )
            self.ctx.client.start_run(RunBudget())
            try:
                records = await self.ctx.client.async_get_usage(self.ctx.site_id, day, day)
            finally:
                self.ctx.client.end_run()
            if mode != "reimport":
                await self.store.async_set_pending(day)
            try:
                summary = await importer.async_write_day(
                    self.hass, ctx, day, records, baselines, mode=mode
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
            await self._async_track_revision(day, list(records), summary)
            self._clear_data_issues()
            self._publish()
            return summary

    # --- channels added after setup ----------------------------------------------

    async def async_add_channels(self, identifiers: Iterable[str]) -> None:
        """Record new channels (reconfigure flow): they start at marker + 1."""
        identifiers = list(identifiers)
        if identifiers and self.store.marker is not None:
            await self.store.async_set_channel_since(
                identifiers, self.store.marker + timedelta(days=1)
            )
        self._delete_issue(ISSUE_UNEXPECTED_CHANNEL)

    # --- Repairs ------------------------------------------------------------------

    def _issue_id(self, kind: str) -> str:
        return f"{kind}_{self.entry.entry_id}"

    def _create_issue(self, kind: str, placeholders: dict[str, str]) -> None:
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._issue_id(kind),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING if kind == ISSUE_BEHIND else ir.IssueSeverity.ERROR,
            translation_key=kind,
            translation_placeholders={"entry": self.entry.title, **placeholders},
            data={"entry_id": self.entry.entry_id},
        )

    def _delete_issue(self, kind: str) -> None:
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id(kind))

    def _raise_data_issue(self, err: IncompleteDataError, day: date) -> None:
        if err.reason == "unexpected_channel":
            self._create_issue(
                ISSUE_UNEXPECTED_CHANNEL, {"day": day.isoformat(), "details": str(err)}
            )
        elif self._final_attempt:
            self._create_issue(
                ISSUE_INCOMPLETE_DATA,
                {"day": day.isoformat(), "reason": err.reason, "details": str(err)},
            )
        else:
            _LOGGER.info("%s is incomplete (%s); a later attempt will retry", day, err.reason)

    def _clear_data_issues(self) -> None:
        for kind in _DATA_ISSUES:
            self._delete_issue(kind)

    def _update_behind_issue(self, summary: dict[str, Any], final: bool) -> None:
        if self.caught_up:
            self._delete_issue(ISSUE_BEHIND)
        elif final:
            marker = self.store.marker
            behind = (nem_today() - timedelta(days=1) - marker).days if marker else None
            self._create_issue(
                ISSUE_BEHIND,
                {
                    "last_date": marker.isoformat() if marker else "none",
                    "days_behind": str(behind) if behind is not None else "unknown",
                    "status": summary["status"],
                    "reason": str(summary.get("reason") or summary.get("message") or ""),
                },
            )

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
            "last_imported": self.store.last_written,
            "days_behind": (yesterday - marker).days if marker else None,
            "next_run": self.next_run,
            "yesterday": yesterday,
            "yesterday_totals": totals,
        }
