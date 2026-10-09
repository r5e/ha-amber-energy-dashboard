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
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
import logging
import math
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import chains, importer
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
from .bill import BillSettings, cycle_for, estimate
from .const import (
    CHANNEL_FEED_IN,
    CHANNEL_GENERAL,
    DEFAULT_PATIENCE_DAYS,
    DEFAULT_REVISION_DAYS,
    DOMAIN,
    FETCH_WINDOW_DAYS,
    MODE_FULL,
    MODE_PRICING,
    MODE_RECOVERY,
    NEM_TZ,
    RETENTION_BRACKET_DAYS,
    RETENTION_DISCOVERY_MAX_CALLS,
    RETENTION_FALLBACK_DAYS,
    RETENTION_REVERIFY_BRACKET_DAYS,
)
from .derived import Derived, async_sync as async_sync_derived, fixed_extra, refund_extra
from .export import (
    AllowanceSettings,
    TwoWayTariff,
    day_aggregates as export_day_aggregates,
    day_refunds as export_day_refunds,
    period_summary as export_period_summary,
    refunds as export_refunds,
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
from .statistics import (
    ChannelConfig,
    MeanSpec,
    Metric,
    StatisticSpec,
    adjusted_compensation_spec,
    fixed_cost_spec,
    price_specs,
)
from .storage import (
    SKIPPED_STATUSES,
    STATUS_IMPORTED,
    STATUS_SKIPPED_GAP,
    STATUS_SKIPPED_NOT_REQUESTED,
    STATUS_SKIPPED_PRECISION,
    STATUS_SKIPPED_SENSOR,
    STATUS_SKIPPED_UNAVAILABLE,
    AmberStore,
    ChainState,
    chain_state,
)

_LOGGER = logging.getLogger(__name__)

ISSUE_MARKER_MISMATCH = "marker_mismatch"
ISSUE_UNEXPECTED_CHANNEL = "unexpected_channel"
ISSUE_INCOMPLETE_DATA = "incomplete_data"
ISSUE_IMPORT_FAILED = "import_failed"
ISSUE_BEHIND = "behind"
_DATA_ISSUES = (ISSUE_UNEXPECTED_CHANNEL, ISSUE_INCOMPLETE_DATA, ISSUE_IMPORT_FAILED)
_SKIPPED = SKIPPED_STATUSES

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
STATUS_WAITING_NEXT = "waiting_for_next_run"
"""Display only: the last run caught up, but days are outstanding now (for example after
a restart following downtime). The next attempt imports them."""
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
    STATUS_WAITING_NEXT,
]


class MarkerMismatchError(ImportRefusedError):
    """Guard 1: the Store marker and the statistics table disagree."""


@dataclass(frozen=True, slots=True)
class OwnSensor:
    """A user's energy sensor mapped to an Amber channel (a config sub-entry)."""

    subentry_id: str
    entity_id: str
    channel: ChannelConfig
    spec: StatisticSpec
    name: str
    """The source sensor's friendly name when the mapping was created."""

    @property
    def key(self) -> str:
        """The chain key in the Store."""
        return f"own:{self.subentry_id}"


@dataclass(slots=True)
class _Chain:
    """A secondary chain: the price series, or one own sensor."""

    key: str
    state: ChainState
    mean_specs: list[MeanSpec] = field(default_factory=list)
    sensor: OwnSensor | None = None

    @property
    def ids(self) -> list[str]:
        if self.sensor is not None:
            return [self.sensor.spec.statistic_id]
        return [s.statistic_id for s in self.mean_specs]


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
        usage_mode: str = MODE_FULL,
        price_series: bool = False,
        own_fallback: bool = True,
        own_sensors: Iterable[OwnSensor] = (),
        bill: BillSettings | None = None,
        allowance: AllowanceSettings | None = None,
        tariff: TwoWayTariff | None = None,
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
        self.usage_mode = usage_mode
        self.price_series = price_series
        self.own_fallback = own_fallback
        self.own_sensors = list(own_sensors)
        self.bill = bill
        self.allowance = allowance
        self.tariff = tariff
        """The known two-way tariff detected from /sites at setup (section 20)."""
        self._cache: dict[date, list[UsageRecord]] = {}
        self._last_rewrite_count = 0
        self.status = STATUS_NEVER_RUN if store.last_run is None else store.last_run["status"]
        self.next_run: datetime | None = None
        self.coordinator: DataUpdateCoordinator[dict[str, Any]] = DataUpdateCoordinator(
            hass, _LOGGER, config_entry=entry, name=f"{DOMAIN} status"
        )
        self._unsub_timer: CALLBACK_TYPE | None = None
        self.after_scheduled: Callable[[], Awaitable[None]] | None = None
        """Called after each scheduled attempt (the YAML-kit cleanup re-check)."""
        self.after_run: Callable[[], Awaitable[None]] | None = None
        """Called after each run (the Energy dashboard check, section 18)."""
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
        self,
        schedule: ScheduleConfig,
        patience_days: int,
        revision_days: int,
        *,
        usage_mode: str = MODE_FULL,
        price_series: bool = False,
        own_fallback: bool = True,
        bill: BillSettings | None = None,
        allowance: AllowanceSettings | None = None,
    ) -> None:
        """Apply new options (options flow) without reloading the entry.

        Switching usage mode never deletes statistics; inactive ones stop updating.
        """
        self.schedule = schedule
        self.patience_days = patience_days
        self.revision_days = revision_days
        self.usage_mode = usage_mode
        self.price_series = price_series
        self.own_fallback = own_fallback
        self.bill = bill
        self.allowance = allowance
        self.async_stop()
        self._schedule_next()
        self._publish()
        if self._derived():
            # New charges or allowance settings: bring the derived statistics in line.
            self.entry.async_create_background_task(
                self.hass, self.async_sync_derived(), f"{DOMAIN} derived statistics"
            )

    # --- scheduling ----------------------------------------------------------------

    @property
    def _tz(self):
        return dt_util.get_time_zone(self.hass.config.time_zone)

    @callback
    def _schedule_next(self) -> None:
        if self.usage_mode == MODE_RECOVERY:
            self.next_run = None  # recovery-only: nothing is scheduled
            return
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
        else:
            await self.async_run("scheduled (final)" if final else "scheduled", final=final)
        if self.after_scheduled is not None:
            await self.after_scheduled()

    @property
    def display_status(self) -> str:
        """The status sensor's state: the last outcome, except that "caught up" is shown
        only while nothing is outstanding."""
        if self.status == STATUS_CAUGHT_UP and not self.caught_up:
            return STATUS_WAITING_NEXT
        return self.status

    @property
    def caught_up(self) -> bool:
        """True when yesterday (NEM) is resolved by every chain the mode updates."""
        yesterday = nem_today() - timedelta(days=1)
        markers = [c.state.marker for c in self._secondary_chains()]
        if self.usage_mode == MODE_FULL:
            markers.append(self.store.marker)
        return all(m is not None and m >= yesterday for m in markers)

    # --- runs ---------------------------------------------------------------------

    async def async_run(
        self,
        trigger: str,
        *,
        final: bool = False,
        backfill: tuple[date, date] | None = None,
    ) -> dict[str, Any]:
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
            self._cache = {}
            imported: list[str] = []
            info: dict[str, Any] = {}
            outcome: dict[str, Any]
            try:
                outcome = await self._async_run_steps(imported, info, backfill)
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
        if self.after_run is not None:
            await self.after_run()
        return summary

    @property
    def _usage_active(self) -> bool:
        """True when usage statistics are written (Full and Recovery-only modes)."""
        return self.usage_mode != MODE_PRICING

    async def _async_run_steps(
        self,
        imported: list[str],
        info: dict[str, Any],
        backfill: tuple[date, date] | None = None,
    ) -> dict[str, Any]:
        recovery = await self._async_guard1() if self._usage_active else None
        if self._usage_active and self.store.tail_rewrite is not None:
            progress = self.store.tail_rewrite
            info["tail_resumed"] = progress
            outcome = await self._async_range_rewrite(date.fromisoformat(progress["next"]))
            if outcome is not None:
                return outcome
        retention = self.store.retention or {}
        if self.store.retention_boundary is None or (
            retention.get("needs_discovery")
            # A provisional (fallback) boundary is rediscovered at most once a day.
            and not (
                retention.get("provisional")
                and retention.get("measured_on") == nem_today().isoformat()
            )
        ):
            await self._async_discover_retention()
            info["retention"] = "discovered"
        elif retention.get("last_verified") != nem_today().isoformat():
            info["retention"] = await self._async_verify_retention()
        info["revisions"] = await self._async_check_revisions()
        if backfill is not None:
            outcome = await self._async_backfill(*backfill, imported, info)
        elif self.usage_mode == MODE_FULL:
            outcome = await self._async_walk(recovery, imported, info)
        else:
            outcome = {"status": STATUS_CAUGHT_UP, "reason": None}
        chain_results = await self._async_secondary(info)
        if self.allowance_active:
            info["export"] = await self._async_export_days()
        if derived := await self._async_sync_derived_locked():
            info["derived"] = derived
        if chain_results:
            info["chains"] = chain_results
            if not (self.usage_mode == MODE_FULL or backfill is not None):
                # The mode writes no usage now: the chains decide the status.
                worst = [r for r in chain_results.values() if r["status"] != STATUS_CAUGHT_UP]
                if worst:
                    outcome = {k: worst[0].get(k) for k in ("status", "reason")}
        return outcome

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
            # No earlier imported day: continue from a row in the hour before, if there is
            # one (history copied from a YAML kit, section 14), else start at 0.
            seam = await importer.async_sums_at(self.hass, ids, nem_day_start(day) - HOUR)
            return {sid: seam[sid] or 0.0 for sid in ids}
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
        yesterday = nem_today() - timedelta(days=1)
        start = await self._async_walk_start(recovery, yesterday, info)
        day = start
        leading: date | None = None
        """The first of a run of empty days at the very start of the history."""
        while day <= yesterday:
            end = min(day + timedelta(days=FETCH_WINDOW_DAYS - 1), yesterday)
            by_date = await self._async_fetch_window(day, end)
            while day <= end:
                day_records = by_date.get(day, [])
                if not day_records and self.store.last_written is None:
                    leading = leading or day  # nothing imported before it: no patience
                elif not day_records:
                    waiting = await self._async_patience(day, by_date, end, yesterday, info)
                    if waiting is not None:
                        return waiting
                else:
                    if leading is not None:
                        await self._async_skip_leading(leading, day, info)
                        leading = None
                    mode = "recovery" if day == recovery else "catch_up"
                    await self._async_write(day, day_records, mode)
                    imported.append(day.isoformat())
                day += timedelta(days=1)
        if leading is not None:
            return {
                "status": STATUS_WAITING,
                "reason": f"no usage from {leading} to {yesterday} yet",
                "waiting_for": leading.isoformat(),
            }
        return {"status": STATUS_CAUGHT_UP, "reason": None}

    async def _async_walk_start(
        self, recovery: date | None, yesterday: date, info: dict[str, Any]
    ) -> date:
        """The day the walk starts from; days older than retention (or than the site's
        start date) are skipped first."""
        boundary = self.store.retention_boundary
        assert boundary is not None
        if self.active_from is not None and boundary < self.active_from:
            boundary = self.active_from  # a boundary stored before the start date applied
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
        return start

    async def _async_skip_leading(self, first: date, data_day: date, info: dict[str, Any]) -> None:
        """Self-heal: empty days at the very start of the history, followed by data, are
        before Amber's real boundary (for example a fallback boundary, or a start date with
        no usage yet). Skip them at once and move the boundary to the first day with data."""
        last = data_day - timedelta(days=1)
        days = (data_day - first).days
        await self.store.async_mark_skipped(
            first, last, STATUS_SKIPPED_UNAVAILABLE, "before the first day with usage data"
        )
        old = (self.store.retention or {}).get("method")
        heal = {
            "at": _utcnow().isoformat(),
            "skipped_from": first.isoformat(),
            "skipped_to": last.isoformat(),
            "days": days,
            "previous_method": old,
        }
        await self._store_retention(
            data_day,
            f"first day with data ({days} empty days before it skipped)",
            0,
            {},
            previous=self.store.retention_boundary,
            self_heal=heal,
        )
        info["skipped_leading"] = heal
        _LOGGER.warning(
            "Retention self-heal: %s to %s had no usage and nothing was imported before them; "
            "skipped them and moved the boundary to %s (was: %s)",
            first,
            last,
            data_day,
            old,
        )

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

        A day after ``last_written`` (catch-up, recovery, or a backfill that extends the
        history) is written as the latest day, with ``pending`` set first. A day inside the
        history (a revision or backfill rewrite) is written without it; an interrupted
        range rewrite is resumed from its stored progress instead.
        """
        ctx = self._ctx_for(day)
        baselines = await self._async_baselines(day, ctx)
        last_written = self.store.last_written
        tail = last_written is not None and day <= last_written
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
        await self._async_track_revision(day, records, summary.get("estimated_records", 0))
        self._clear_data_issues()
        return summary

    # --- revisions ----------------------------------------------------------------

    async def _async_track_revision(
        self, day: date, records: list[UsageRecord], estimated: int
    ) -> None:
        if estimated:
            await self.store.async_set_revision(
                day, nem_today(), importer.revision_fingerprint(records)
            )
        elif day.isoformat() in self.store.revisions:
            await self.store.async_drop_revisions([day])

    async def _async_fetch_window(self, start: date, end: date) -> dict[date, list[UsageRecord]]:
        """Fetch usage for ``start``..``end`` (one call) into the run's cache."""
        records = await self.ctx.client.async_get_usage(self.ctx.site_id, start, end)
        by_date = self._split_by_date(records, start, end)
        for d in _days(start, end):
            self._cache[d] = by_date.get(d, [])
        return by_date

    async def _async_records_for(self, day: date, last: date) -> list[UsageRecord]:
        """Usage records for ``day``, fetching a window up to ``last`` if not cached."""
        if day not in self._cache:
            await self._async_fetch_window(
                day, min(day + timedelta(days=FETCH_WINDOW_DAYS - 1), last)
            )
        return self._cache[day]

    async def _async_fetch_days(self, days: Iterable[date], last: date) -> None:
        """Fetch the given days (windows of up to 7 days, capped at ``last``)."""
        for day in sorted(days):
            await self._async_records_for(day, max(day, last))

    def _later_has_data(self, day: date) -> bool:
        return any(d > day and recs for d, recs in self._cache.items())

    async def _async_rewrite_after_revision(self, first: date, result: dict[str, Any]) -> None:
        """Rewrite every active chain from the earliest revised day."""
        last_written = self.store.last_written
        if self._usage_active and last_written and first <= last_written:
            outcome = await self._async_range_rewrite(first)
            if outcome is not None:  # pragma: no cover - revision rewrites never wait
                raise ImportDayError(outcome["reason_code"], outcome["reason"])
            result["rewritten_days"] = self._last_rewrite_count
        for chain in self._secondary_chains():
            if chain.state.last_written and first <= chain.state.last_written:
                result.setdefault("chains_rewritten", {})[
                    chain.key
                ] = await self._async_chain_rewrite(chain, first)

    async def _async_check_revisions(self) -> dict[str, Any] | None:
        """Once a day: re-fetch estimated days; rewrite the tail if any changed."""
        today = nem_today()
        if self.store.revisions_checked == today:
            return None
        entries = self.store.revisions
        expired = [
            date.fromisoformat(d)
            for d, e in entries.items()
            if date.fromisoformat(e["since"]) + timedelta(days=self.revision_days) < today
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
            await self._async_fetch_days(days, today - timedelta(days=1))
            changed, final = [], []
            for day in days:
                records = self._cache.get(day, [])
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
                await self._async_rewrite_after_revision(changed[0], result)
        await self.store.async_set_revisions_checked(today)
        return result

    async def _async_range_rewrite(
        self,
        first: date,
        last: date | None = None,
        *,
        requested_end: date | None = None,
        on_empty: str = "error",
        imported: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Write every day from ``first`` to ``last`` in order through the shared path.

        Used by the revision tail rewrite (``on_empty="error"``: every day must be
        available) and by ``backfill`` (``on_empty="patience"``: an empty requested day
        follows the patience rule). Imported days are rewritten; days up to
        ``requested_end`` without statistics are written; skipped days stay skipped
        (except days skipped as not requested, when they are now requested).
        Progress is stored after each day, so an interrupted rewrite resumes where it
        stopped. Returns None when done, or a waiting outcome.
        """
        progress = self.store.tail_rewrite
        if progress is not None:
            last = date.fromisoformat(progress["to"])
            requested_end = date.fromisoformat(progress.get("requested_end") or progress["to"])
            on_empty = progress.get("on_empty", "error")
            origin = progress["from"]
        else:
            last = last or self.store.last_written
            assert last is not None
            requested_end = requested_end or last
            origin = first.isoformat()

        async def save(next_day: date) -> None:
            await self.store.async_set_tail_rewrite(
                {
                    "from": origin,
                    "next": next_day.isoformat(),
                    "to": last.isoformat(),
                    "requested_end": requested_end.isoformat(),
                    "on_empty": on_empty,
                }
            )

        await save(first)
        count = 0
        for day in _days(first, last):
            status = (self.store.day(day) or {}).get("status")
            requested = day <= requested_end
            skip = status in _SKIPPED and not (status == STATUS_SKIPPED_NOT_REQUESTED and requested)
            if skip or (status != STATUS_IMPORTED and not requested):
                await save(day + timedelta(days=1))
                continue
            records = await self._async_records_for(day, last)
            if not records:
                if status == STATUS_IMPORTED or on_empty == "error":
                    raise ImportDayError(
                        "tail_unavailable",
                        f"{day} returned no usage during a rewrite; it will be retried.",
                    )
                seen = await self.store.async_note_empty(day, nem_today())
                if seen >= self.patience_days and self._later_has_data(day):
                    await self.store.async_mark_skipped(
                        day, day, STATUS_SKIPPED_GAP, f"empty on {seen} separate days"
                    )
                    await save(day + timedelta(days=1))
                    continue
                await save(day)
                self._last_rewrite_count = count
                return {
                    "status": STATUS_WAITING,
                    "reason": f"no usage for {day} yet",
                    "reason_code": "waiting_for_data",
                    "waiting_for": day.isoformat(),
                    "empty_days_seen": seen,
                }
            await self._async_write(
                day, records, "revision" if status == STATUS_IMPORTED else "backfill"
            )
            if status != STATUS_IMPORTED and imported is not None:
                imported.append(day.isoformat())
            count += 1
            await save(day + timedelta(days=1))
        await self.store.async_set_tail_rewrite(None)
        self._last_rewrite_count = count
        return None

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
        """Find the earliest NEM day with usage by bisection (at most 8 calls).

        The record keeps the boundary stored before discovery as previous_boundary
        (None on a first discovery), so a probe_retention move shows in diagnostics.
        """
        today = nem_today()
        previous = self.store.retention_boundary
        probes: dict[str, bool] = {}
        counter = [0]
        has_data = self._prober(probes, counter)
        method = "bisection"
        try:
            boundary, bracketed = await self._search_boundary(today, has_data)
            if boundary is not None and not bracketed:
                method = "oldest day seen with data (not bracketed older)"
        except (_DiscoveryIncomplete, AmberError) as err:
            boundary = None
            method = f"fallback ({type(err).__name__})"
            if isinstance(err, AmberAuthError | AmberBudgetExhaustedError):
                await self._store_retention(
                    today - timedelta(days=RETENTION_FALLBACK_DAYS),
                    method,
                    counter[0],
                    probes,
                    previous=previous,
                    needs_discovery=True,
                    provisional=True,
                )
                raise
        guess = today - timedelta(days=RETENTION_FALLBACK_DAYS)
        failed = boundary is None and method != "bisection"
        provisional = failed
        if boundary is None and self.active_from is not None and self.active_from > guess:
            # A young site: start at its start date. If that day has no usage, the walk
            # skips the empty days up to the first day with data at once.
            boundary = self.active_from
            method = (
                f"{method}, from the site start date"
                if failed
                else "site start date (no usage on it)"
            )
        if boundary is None:
            method = method if failed else "fallback (not bracketed)"
            boundary, provisional = guess, True
        await self._store_retention(
            boundary,
            method,
            counter[0],
            probes,
            previous=previous,
            needs_discovery=provisional,
            provisional=provisional,
        )

    async def _async_verify_retention(self) -> dict[str, Any]:
        """Daily check of the stored boundary date (DESIGN section 8).

        Two probes: the boundary day has data and the day before does not. Then the
        boundary is "verified", whatever the day count, so a boundary that stays fixed
        costs nothing more. Only when the probes disagree does it step (a one-day move,
        confirmed with one more probe) or bisect within a 15-day bracket. A backward move
        beyond the bracket accepts the bracket end (it has data) and continues from there
        the next day. A forward move beyond the bracket (a long outage with a rolling
        boundary) also tries the day count seen at the last verification, today - that
        count, with 2 probes. The whole check stays within 8 calls. If a forward move
        cannot be resolved, the old date is kept and full discovery runs the next day.
        """
        today = nem_today()
        boundary = self.store.retention_boundary
        assert boundary is not None
        previous_days = (today - boundary).days
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
                    new = await self._rolling_candidate(boundary + one + bracket, has_data)
                    method = (
                        f"moved forward {(new - boundary).days} days (same day count as at "
                        "the last verification)"
                        if new
                        else "unresolved (moved forward beyond the bracket)"
                    )
            elif not await has_data(boundary - 2 * one):
                new, method = boundary - one, "moved back 1 day"
            elif not await has_data(boundary - 2 * one - bracket):
                new = await self._bisect(boundary - 2 * one - bracket, boundary - 2 * one, has_data)
                method = f"moved back {(boundary - new).days} days (bisected)"
            else:
                # The bracket end has data: accept it; the next days continue from there
                # until the boundary is bracketed (M6c).
                new = boundary - 2 * one - bracket
                method = f"moved back at least {(boundary - new).days} days"
        except (_DiscoveryIncomplete, AmberError) as err:
            method = f"unresolved ({type(err).__name__})"
            if isinstance(err, AmberAuthError | AmberBudgetExhaustedError):
                raise
        await self._store_retention(
            new or boundary,
            method,
            counter[0],
            probes,
            previous=boundary,
            needs_discovery=new is None,
        )
        return {
            "method": method,
            "calls": counter[0],
            "boundary": (new or boundary).isoformat(),
            "previous_boundary": boundary.isoformat(),
            "retention_days": (today - (new or boundary)).days,
            "previous_days": previous_days,
        }

    async def _rolling_candidate(self, known_empty: date, has_data: Callable) -> date | None:
        """today - (the day count at the last verification), if it is exactly the boundary."""
        boundary = self.store.retention_boundary
        assert boundary is not None
        verified = (self.store.retention or {}).get("last_verified")
        count = (date.fromisoformat(verified) - boundary).days if verified else 0
        candidate = nem_today() - timedelta(days=count)
        if candidate <= known_empty:
            return None
        if await has_data(candidate) and not await has_data(candidate - timedelta(days=1)):
            return candidate
        return None

    async def _search_boundary(self, today: date, has_data: Callable) -> tuple[date | None, bool]:
        """Probe around the expected boundary, then bisect.

        Returns (boundary, bracketed). If the data goes back further than the 30-day
        bracket, the oldest day seen with data is returned unbracketed; the daily check
        then moves it back until it is found (M6c). None if nothing newer has data.
        """
        guess = today - timedelta(days=RETENTION_FALLBACK_DAYS)
        if self.active_from is not None and self.active_from > guess:
            # A site younger than the usual window: its data starts at activeFrom.
            return (self.active_from if await has_data(self.active_from) else None), True
        if await has_data(guess):
            return await self._search_older(guess, has_data)
        return await self._search_newer(guess, has_data), True

    async def _search_older(self, guess: date, has_data: Callable) -> tuple[date, bool]:
        """``guess`` has data: the boundary is ``guess`` or up to 30 days before it, or
        older still (then the bracket end, the oldest day seen with data, unbracketed)."""
        before = guess - timedelta(days=1)
        if not await has_data(before):
            return guess, True
        lo = before - timedelta(days=RETENTION_BRACKET_DAYS)
        if await has_data(lo):
            return lo, False
        return await self._bisect(lo, before, has_data), True

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
        boundary: date,
        method: str,
        calls: int,
        probes: dict,
        *,
        previous: date | None = None,
        needs_discovery: bool = False,
        provisional: bool = False,
        self_heal: dict[str, Any] | None = None,
    ) -> None:
        today = nem_today()
        start_date = None
        if self.active_from is not None and boundary < self.active_from:
            # No usage before the site's start date (/sites activeFrom).
            boundary, start_date = self.active_from, self.active_from.isoformat()
        info = {
            "measured_on": today.isoformat(),
            "last_verified": today.isoformat(),
            "method": method,
            "calls": calls,
            "probes": probes,
            "boundary": boundary.isoformat(),
            "previous_boundary": None if previous is None else previous.isoformat(),
            "needs_discovery": needs_discovery,
            "provisional": provisional,
            "site_start_applied": start_date,
            "self_heal": self_heal,
        }
        if previous is not None and previous != boundary:
            _LOGGER.warning(
                "Usage retention boundary moved from %s to %s (%s)", previous, boundary, method
            )
        _LOGGER.info(
            "Usage retention: from %s, %d days (%s, %d calls)",
            boundary,
            (today - boundary).days,
            method,
            calls,
        )
        await self.store.async_set_retention(boundary, info)

    # --- backfill (Recovery-only mode, or rewriting history in Full mode) ------------

    async def _async_backfill(
        self, start: date, end: date, imported: list[str], info: dict[str, Any]
    ) -> dict[str, Any]:
        """Import ``start``..``end`` through the guarded path.

        Days older than the retention boundary are skipped as unavailable. A range that
        starts at or before existing data is written with a tail rewrite, so every later
        sum is re-derived. In Recovery-only mode a range after the existing data is
        appended, and the days in between are recorded as not requested. In Full mode,
        days after the last import are left to the schedule.
        """
        today = nem_today()
        yesterday = today - timedelta(days=1)
        boundary = self.store.retention_boundary
        assert boundary is not None
        end = min(end, yesterday)
        if start > end:
            raise ImportRefusedError(
                "backfill_range", f"nothing to import between {start} and {end}"
            )
        old_last = min(end, boundary - timedelta(days=1))
        if start <= old_last:
            await self.store.async_mark_skipped(
                start, old_last, STATUS_SKIPPED_UNAVAILABLE, "older than Amber's usage retention"
            )
            info["skipped_unavailable"] = {"from": start.isoformat(), "to": old_last.isoformat()}
        first = max(start, boundary)
        if first > end:
            return {"status": STATUS_CAUGHT_UP, "reason": "the whole range is older than retention"}
        last_written = self.store.last_written
        if last_written is not None and first > last_written + timedelta(days=1):
            if self.usage_mode == MODE_FULL:
                raise ImportRefusedError(
                    "backfill_forward",
                    f"In Full mode, days after the last import ({last_written}) are imported by "
                    "the schedule (or run_now). backfill fills or rewrites history up to it.",
                )
            marker = self.store.marker
            gap_first = marker + timedelta(days=1) if marker else first
            if gap_first < first:
                await self.store.async_mark_skipped(
                    gap_first,
                    first - timedelta(days=1),
                    STATUS_SKIPPED_NOT_REQUESTED,
                    "not part of a backfill range",
                )
        last = max(end, last_written) if last_written else end
        outcome = await self._async_range_rewrite(
            first, last, requested_end=end, on_empty="patience", imported=imported
        )
        info["backfill"] = {
            "from": first.isoformat(),
            "to": end.isoformat(),
            "rewritten_to": last.isoformat(),
        }
        return outcome or {"status": STATUS_CAUGHT_UP, "reason": None}

    # --- secondary chains: price series and own sensors ---------------------------

    def _secondary_chains(self) -> list[_Chain]:
        chains_: list[_Chain] = []
        if self.price_series:
            chains_.append(
                _Chain(
                    "price",
                    chain_state(self.store, "price"),
                    mean_specs=price_specs(self.ctx.site_id, self.ctx.channels),
                )
            )
        chains_.extend(
            _Chain(sensor.key, chain_state(self.store, sensor.key), sensor=sensor)
            for sensor in self.own_sensors
        )
        return chains_

    @property
    def fixed_spec(self) -> StatisticSpec | None:
        """The "import cost including fixed charges" statistic, when it is written: opted
        in, with usage statistics written (not Pricing-only) and a general channel."""
        if self.bill is None or not self.bill.fixed_statistic or not self._usage_active:
            return None
        return fixed_cost_spec(self.ctx.site_id, self.ctx.channels)

    # --- derived statistics and the export allowance (sections 19 and 20) -----------

    @property
    def allowance_active(self) -> bool:
        """The export allowance applies: configured, with usage written and a feed-in."""
        return (
            self.allowance is not None
            and self._usage_active
            and any(c.type == CHANNEL_FEED_IN for c in self.ctx.channels)
        )

    def _derived(self) -> list[Derived]:
        derived: list[Derived] = []
        fixed = self.fixed_spec
        if fixed is not None:
            schedule = self.store.fixed_schedule
            derived.append(
                Derived(
                    fixed,
                    tuple(
                        s.statistic_id
                        for s in self.ctx.specs
                        if s.metric is Metric.COST and s.channel is not None
                    ),
                    fixed_extra(schedule),
                )
            )
        if self.allowance_active:
            assert self.allowance is not None
            spec = adjusted_compensation_spec(self.ctx.site_id, self.ctx.channels)
            assert spec is not None
            by_hour, _ = export_refunds(self.allowance, self.store.export_days)
            base = next(s.statistic_id for s in self.ctx.specs if s.metric is Metric.COMPENSATION)
            derived.append(Derived(spec, (base,), refund_extra(by_hour)))
        return derived

    async def async_sync_derived(self) -> dict[str, Any]:
        """Sync the derived statistics under the run lock (options changes, migration)."""
        async with self.ctx.lock:
            return await self._async_sync_derived_locked()

    async def _async_sync_derived_locked(self) -> dict[str, Any]:
        if self.fixed_spec is not None and self.bill is not None:
            await self._async_fixed_schedule(self.bill.daily_fixed_incl_gst)
        return {
            derived.spec.statistic_id: await async_sync_derived(self.hass, derived)
            for derived in self._derived()
        }

    async def _async_fixed_schedule(self, daily: float) -> None:
        """Record a new daily fixed amount: from the start when the statistic is new,
        else from the day after the last imported day (section 19)."""
        schedule = self.store.fixed_schedule
        if schedule and math.isclose(schedule[-1]["daily_incl_gst"], daily, abs_tol=1e-9):
            return
        last = self.store.last_written
        start = (
            None
            if not schedule or last is None
            else nem_day_start(last + timedelta(days=1)).isoformat()
        )
        await self.store.async_set_fixed_schedule(
            [*schedule, {"from": start, "daily_incl_gst": round(daily, 6)}]
        )

    async def _async_export_days(self) -> dict[str, Any]:
        """Measure the export aggregates for imported days that lack them, or whose
        usage changed (a revision): from the run's records, fetching the rest."""
        assert self.allowance is not None
        general = next(c.identifier for c in self.ctx.channels if c.type == CHANNEL_GENERAL)
        feed_in = next(c.identifier for c in self.ctx.channels if c.type == CHANNEL_FEED_IN)
        stored = self.store.export_days
        boundary, last = self.store.retention_boundary, self.store.last_written
        missing = [
            day
            for day in (_days(boundary, last) if boundary and last else ())
            if (self.store.day(day) or {}).get("status") == STATUS_IMPORTED
            and day.isoformat() not in stored
        ]
        changed = [
            day
            for day, records in self._cache.items()
            if records
            and day.isoformat() in stored
            and stored[day.isoformat()]["fingerprint"] != importer.revision_fingerprint(records)
        ]
        measured: list[str] = []
        loss_factors = [v["loss_factor"] for _, v in sorted(stored.items()) if v["loss_factor"]]
        fallback = loss_factors[-1] if loss_factors else None
        for day in sorted({*missing, *changed}):
            records = await self._async_records_for(day, last or day)
            record = export_day_aggregates(
                records,
                day,
                self.allowance,
                channels=(general, feed_in),
                fallback_loss_factor=fallback,
            )
            if record is None:
                continue
            fallback = record["loss_factor"] or fallback
            await self.store.async_set_export_day(day, record)
            measured.append(day.isoformat())
        return {"measured": measured}

    def allowance_summary(self, day: date) -> dict[str, Any]:
        """The allowance figures for the period containing ``day``."""
        assert self.allowance is not None
        first, last = self.allowance.period_for(day)
        imported = [
            d
            for d in _days(first, last)
            if (self.store.day(d) or {}).get("status") == STATUS_IMPORTED
        ]
        return export_period_summary(self.allowance, self.store.export_days, day, imported)

    async def _async_secondary(self, info: dict[str, Any]) -> dict[str, Any]:
        results: dict[str, Any] = {}
        for chain in self._secondary_chains():
            try:
                results[chain.key] = await self._async_chain_walk(chain)
            except ImportDayError as err:
                results[chain.key] = {
                    "status": STATUS_ATTENTION,
                    "reason": err.reason,
                    "message": str(err),
                }
        return results

    async def _async_chain_guard(self, chain: _Chain) -> date | None:
        """Guard 1 for a secondary chain; also adopts statistics left by an earlier mapping."""
        state = chain.state
        latest = await importer.async_latest_hours(self.hass, chain.ids)
        values = set(latest.values())
        if (
            state.marker is None
            and state.last_written is None
            and None not in values
            and len(values) == 1
        ):
            end = values.pop()
            last = importer.nem_date(end)
            if end == _day_last_hour(last):
                _LOGGER.info("%s: continuing existing statistics after %s", chain.key, last)
                await state.async_adopt(last)
                return None
            values = {end}
        marker, last_written, pending = state.marker, state.last_written, state.pending
        expected = _day_last_hour(last_written) if last_written else None
        problem = None
        if marker is not None and (last_written is None or marker >= last_written):
            gap_start = last_written + timedelta(days=1) if last_written else None
            if gap_start is not None:
                for day in _days(gap_start, marker):
                    if (state.day(day) or {}).get("status") not in _SKIPPED:
                        problem = f"{chain.key}: {day} is neither imported nor skipped"
                        break
        elif marker is not None or last_written is not None:
            problem = f"{chain.key}: marker {marker} is before the last written day {last_written}"
        if problem is None and values == {expected}:
            self._delete_issue(ISSUE_MARKER_MISMATCH, chain.key.replace(":", "_"))
            if pending is not None:
                await state.async_set_pending(None)
            return None
        if (
            problem is None
            and pending is not None
            and pending == (marker + timedelta(days=1) if marker else pending)
            and values <= {expected, _day_last_hour(pending)}
        ):
            return pending
        details = problem or (
            f"{chain.key}: expected the last stored hour to be "
            f"{expected.isoformat() if expected else 'none'}, but the statistics end at "
            f"{', '.join(sorted(v.isoformat() if v else 'none' for v in values))}"
        )
        self._create_issue(ISSUE_MARKER_MISMATCH, {"details": details}, chain.key.replace(":", "_"))
        raise MarkerMismatchError("marker_mismatch", details)

    async def _async_chain_start(self, chain: _Chain, boundary: date) -> date | None:
        """First day for a new chain: the boundary, or the day the sensor's history starts."""
        if chain.sensor is None:
            return boundary
        first = await self._async_sensor_first_day(chain.sensor.entity_id, boundary)
        return None if first is None else max(first, boundary)

    async def _async_sensor_first_day(self, entity_id: str, boundary: date) -> date | None:
        start = nem_day_start(boundary) - HOUR
        rows = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period, self.hass, start, None, {entity_id}, "hour", None, {"sum"}
        )
        found = rows.get(entity_id, [])
        if not found:
            return None
        # The first row is only a starting reading: energy is measurable from the hour
        # after it. That hour's day is where the history starts (possibly part-way).
        return importer.nem_date(datetime.fromtimestamp(found[0]["start"], UTC) + HOUR)

    async def _async_chain_walk(self, chain: _Chain) -> dict[str, Any]:
        """Extend one secondary chain from its marker to yesterday."""
        state = chain.state
        recovery = await self._async_chain_guard(chain)
        if state.rewrite is not None:
            await self._async_chain_rewrite(chain, date.fromisoformat(state.rewrite["next"]))
        yesterday = nem_today() - timedelta(days=1)
        start = await self._async_chain_first_day(chain, recovery, yesterday)
        report: dict[str, Any] = {
            "status": STATUS_CAUGHT_UP,
            "reason": None,
            "written": [],
            "skipped": [],
            "lower_precision": [],
        }
        if start is None:
            return {
                **report,
                "status": STATUS_WAITING,
                "reason": "the sensor has no statistics yet",
            }
        for day in _days(start, yesterday):
            records = await self._async_records_for(day, yesterday)
            if not records:
                if await self._async_chain_empty_day(state, day, yesterday):
                    report["skipped"].append(day.isoformat())
                    continue
                return {**report, "status": STATUS_WAITING, "reason": f"no usage for {day} yet"}
            try:
                result = await self._async_chain_write(chain, day, records, rewriting=False)
            except ImportDayError as err:
                return {
                    **report,
                    "status": STATUS_ATTENTION,
                    "reason": err.reason,
                    "message": str(err),
                }
            if result in ("written", "lower_precision"):
                report["written"].append(day.isoformat())
                if result == "lower_precision":
                    report["lower_precision"].append(day.isoformat())
            else:
                report["skipped"].append(day.isoformat())
        return report

    async def _async_chain_first_day(
        self, chain: _Chain, recovery: date | None, yesterday: date
    ) -> date | None:
        """The day a chain continues from; days older than retention are skipped first."""
        state = chain.state
        boundary = self.store.retention_boundary
        assert boundary is not None
        if recovery is not None:
            start: date | None = recovery
        elif state.marker is not None:
            start = state.marker + timedelta(days=1)
        else:
            start = await self._async_chain_start(chain, boundary)
        if start is not None and start < boundary:
            last = min(boundary - timedelta(days=1), yesterday)
            await state.async_mark_skipped(
                start, last, STATUS_SKIPPED_UNAVAILABLE, "older than retention"
            )
            start = last + timedelta(days=1)
        return start

    async def _async_chain_empty_day(self, state: ChainState, day: date, yesterday: date) -> bool:
        """Retention and patience for a chain's empty day. True if the day was skipped."""
        shared = (self.store.day(day) or {}).get("status")
        if shared in (STATUS_SKIPPED_GAP, STATUS_SKIPPED_UNAVAILABLE):
            await state.async_mark_skipped(day, day, shared, "as for the usage data")
            return True
        seen = await self.store.async_note_empty(day, nem_today())
        if seen < self.patience_days:
            return False
        if not self._later_has_data(day) and day < yesterday:
            await self._async_records_for(day + timedelta(days=1), yesterday)
        if not self._later_has_data(day):
            return False
        await state.async_mark_skipped(
            day, day, STATUS_SKIPPED_GAP, f"empty on {seen} separate days"
        )
        return True

    async def _async_chain_write(
        self, chain: _Chain, day: date, records: list[UsageRecord], *, rewriting: bool
    ) -> str:
        """Write one day of a secondary chain.

        Returns ``written``, ``lower_precision`` or the skip status it recorded.
        """
        state = chain.state
        ctx = self._ctx_for(day)
        try:
            by_channel = importer.check_completeness(records, day, ctx.channels)
        except IncompleteDataError as err:
            self._raise_data_issue(err, day)
            raise
        tail = rewriting or (state.last_written is not None and day <= state.last_written)
        estimated = sum(1 for r in records if r.quality == "estimated")
        if chain.sensor is None:
            specs = [s for s in chain.mean_specs if s.channel in by_channel]
            rows = chains.price_rows(records, specs, day)
            if not tail:
                await state.async_set_pending(day)
            await importer.async_write_means(self.hass, day, specs, rows, expect_latest=not tail)
            await state.async_mark_written(day, {"mode": "revision" if tail else "catch_up"})
            await self._async_track_revision(day, records, estimated)
            return "written"
        sensor = chain.sensor
        channel_records = by_channel.get(sensor.channel.identifier)
        if not channel_records:
            await state.async_mark_skipped(
                day, day, STATUS_SKIPPED_SENSOR, f"channel {sensor.channel.identifier} not active"
            )
            return STATUS_SKIPPED_SENSOR
        first_day = state.marker is None and state.last_written is None
        five, hourly = await chains.async_sensor_energy(
            self.hass, sensor.entity_id, day, first_day=first_day
        )
        try:
            priced = chains.own_cost_day(
                channel_records,
                day,
                five,
                hourly,
                feed_in=sensor.channel.type == CHANNEL_FEED_IN,
                allow_fallback=self.own_fallback,
            )
        except chains.SensorDataUnavailable as err:
            await state.async_mark_skipped(day, day, STATUS_SKIPPED_SENSOR, str(err))
            return STATUS_SKIPPED_SENSOR
        except chains.PreciseDataUnavailable as err:
            await state.async_mark_skipped(day, day, STATUS_SKIPPED_PRECISION, str(err))
            return STATUS_SKIPPED_PRECISION
        await self._async_write_sum_chain(
            chain,
            day,
            sensor.spec,
            priced.amounts,
            tail=tail,
            details={"energy_kwh": priced.energy_kwh, "lower_precision": priced.lower_precision},
        )
        await self._async_track_revision(day, records, estimated)
        return "lower_precision" if priced.lower_precision else "written"

    async def _async_write_sum_chain(
        self,
        chain: _Chain,
        day: date,
        spec: StatisticSpec,
        amounts: list[float],
        *,
        tail: bool,
        details: dict[str, Any],
    ) -> None:
        """Write one day of a sum-type secondary chain from its baseline, and record it."""
        state = chain.state
        sid = spec.statistic_id
        prev = (
            state.last_written
            if (state.last_written and state.last_written < day)
            else state.last_imported_before(day)
        )
        if prev is None:
            baseline = 0.0
        else:
            sums = await importer.async_sums_at(self.hass, [sid], _day_last_hour(prev))
            if sums[sid] is None:
                raise ImportRefusedError(
                    "partial_history", f"{sid} has no row at the end of {prev}"
                )
            baseline = sums[sid]
        if not tail:
            await state.async_set_pending(day)
        await importer.async_write_amounts(
            self.hass, day, [spec], {sid: amounts}, {sid: baseline}, expect_latest=not tail
        )
        await state.async_mark_written(
            day,
            {
                "mode": "revision" if tail else "catch_up",
                "amount": round(math.fsum(amounts), 6),
                **details,
            },
        )

    async def _async_chain_rewrite(self, chain: _Chain, first: date) -> int:
        """Rewrite a secondary chain from ``first`` to its last written day, resumably."""
        state = chain.state
        last = date.fromisoformat(state.rewrite["to"]) if state.rewrite else state.last_written
        if last is None:
            return 0
        count = 0
        for day in _days(first, last):
            await state.async_set_rewrite({"next": day.isoformat(), "to": last.isoformat()})
            if (state.day(day) or {}).get("status") != STATUS_IMPORTED:
                continue
            records = await self._async_records_for(day, last)
            if not records:
                raise ImportDayError(
                    "tail_unavailable", f"{chain.key}: {day} returned no usage during a rewrite"
                )
            await self._async_chain_write(chain, day, records, rewriting=True)
            count += 1
        await state.async_set_rewrite(None)
        return count

    # --- probe_retention service ---------------------------------------------------

    async def async_probe_retention(self) -> dict[str, Any]:
        """Force a fresh discovery of the retention boundary (at most 8 calls).

        If discovery fails (an API error, the rate-limit budget, or the call cap), the
        stored boundary is kept and an error is raised; an auth failure also starts
        reauthentication.
        """
        async with self.ctx.lock:
            previous_boundary = self.store.retention_boundary
            previous_info = self.store.retention
            budget = self.ctx.client.start_run(RunBudget())
            try:
                await self._async_discover_retention()
            except AmberAuthError:
                self.entry.async_start_reauth(self.hass)
                await self._restore_retention(previous_boundary, previous_info)
                raise
            except AmberError:
                await self._restore_retention(previous_boundary, previous_info)
                raise
            finally:
                self.ctx.client.end_run()
            info = self.store.retention or {}
            method = info.get("method", "")
            if method.startswith("fallback (") and method != "fallback (not bracketed)":
                await self._restore_retention(previous_boundary, previous_info)
                raise AmberError(f"retention probe failed: {info['method']}")
            boundary = self.store.retention_boundary
            assert boundary is not None
            return {
                "boundary": boundary.isoformat(),
                "retention_days": self.store.retention_days,
                "method": info.get("method"),
                "calls": dict(budget.calls),
                "previous_boundary": previous_boundary.isoformat() if previous_boundary else None,
            }

    async def _restore_retention(self, boundary: date | None, info: dict | None) -> None:
        if boundary is not None:
            await self.store.async_set_retention(boundary, info or {})

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
            await self._async_track_revision(
                day, list(records), summary.get("estimated_records", 0)
            )
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

    def _issue_id(self, kind: str, suffix: str | None = None) -> str:
        base = f"{kind}_{self.entry.entry_id}"
        return f"{base}_{suffix}" if suffix else base

    def _create_issue(
        self, kind: str, placeholders: dict[str, str], suffix: str | None = None
    ) -> None:
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._issue_id(kind, suffix),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING if kind == ISSUE_BEHIND else ir.IssueSeverity.ERROR,
            translation_key=kind,
            translation_placeholders={"entry": self.entry.title, **placeholders},
            data={"entry_id": self.entry.entry_id},
        )

    def _delete_issue(self, kind: str, suffix: str | None = None) -> None:
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id(kind, suffix))

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

    def _reconciliation(self) -> dict[str, dict[str, Any] | None]:
        """Own kWh minus Amber general kWh for the last day both have (Full mode only)."""
        result: dict[str, dict[str, Any] | None] = {}
        for sensor in self.own_sensors:
            if sensor.channel.type != CHANNEL_GENERAL:
                continue
            result[sensor.subentry_id] = None
            if self.usage_mode != MODE_FULL:
                continue
            energy_sid = next(
                s.statistic_id
                for s in self.ctx.specs
                if s.channel == sensor.channel.identifier and s.unit == "kWh"
            )
            state = chain_state(self.store, sensor.key)
            day = nem_today() - timedelta(days=1)
            for _ in range(15):
                own = state.day(day) or {}
                amber = self.store.day(day) or {}
                if own.get("status") == STATUS_IMPORTED and amber.get("status") == STATUS_IMPORTED:
                    amber_kwh = (amber.get("totals") or {}).get(energy_sid)
                    own_kwh = own.get("energy_kwh")
                    if amber_kwh is not None and own_kwh is not None:
                        diff = own_kwh - amber_kwh
                        result[sensor.subentry_id] = {
                            "date": day.isoformat(),
                            "difference_kwh": round(diff, 3),
                            "difference_percent": round(100 * diff / amber_kwh, 2)
                            if amber_kwh
                            else None,
                            "own_kwh": round(own_kwh, 3),
                            "amber_kwh": round(amber_kwh, 3),
                        }
                        break
                day -= timedelta(days=1)
        return result

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
            "reconciliation": self._reconciliation(),
            "chains": {
                c.key: {"marker": c.state.marker, "last_written": c.state.last_written}
                for c in self._secondary_chains()
            },
            "status": self.display_status,
            "last_run": self.store.last_run,
            "marker": marker,
            "last_imported": self.store.last_written,
            "days_behind": (yesterday - marker).days if marker else None,
            "next_run": self.next_run,
            "yesterday": yesterday,
            "yesterday_totals": totals,
            "bill": self.bill_estimate(nem_today()) if self.bill_available else None,
            "allowance": self.allowance_summary(nem_today() - timedelta(days=1))
            if self.allowance_active
            else None,
        }

    @property
    def bill_available(self) -> bool:
        """The bill estimate needs its options and imported usage (not Pricing-only)."""
        return self.bill is not None and self._usage_active

    def bill_estimate(self, cycle_day: date) -> dict[str, Any]:
        """The bill estimate for the cycle containing ``cycle_day`` (section 19)."""
        assert self.bill is not None
        start, end = cycle_for(cycle_day, self.bill.billing_day)
        days = {day: self.store.day(day) for day in _days(start, end)}
        export = (
            export_day_refunds(self.allowance, self.store.export_days, start, end)
            if self.allowance_active and self.allowance is not None
            else None
        )
        result = estimate(
            self.bill,
            days,
            list(self.ctx.specs),
            self.ctx.channels,
            cycle_day=cycle_day,
            today=nem_today(),
            export=export,
        )
        if export is not None:
            result["export_allowance"] = self.allowance_summary(cycle_day)
        return result
