# Amber Energy Dashboard v2 (HACS integration): Design

Status: agreed design (September 2026). Milestone 1 API and schema findings applied.
Owner: r5e. Implementation via Claude Code, planning in chat.

Names (agreed): repository `r5e/ha-amber-energy-dashboard` (the existing v1 repo, reused),
integration domain `amber_energy_dashboard` (**permanent**: it prefixes every statistic ID),
display name "Amber Energy Dashboard (unofficial)".

Repository strategy: development happens on a `v2` branch of the existing repo. `main`
stays on the v1 YAML kit until release. On `v2`, the v1 kit lives in `legacy/v1/` (kept
browsable, and used as the reference list of v1 names for the migration tool). At
release, `v2` merges to `main` and the previous `main` is tagged `v1-final`.

---

## 1. Purpose and scope

Home Assistant's core `amberelectric` integration provides live price sensors only.
Amber's actual metered usage (published a day late) and the real per-interval cost of
that usage are not available anywhere in HA core. This integration imports that
history, correctly and reliably, so it can drive the Energy dashboard and analysis.

In scope:
- Hourly grid import/export energy and cost history from Amber's usage API, backfilled
  and kept current, including automatic recovery after outages.
- Accurate cost of the user's own locally-metered energy (CT clips, smart plugs, other
  integrations) using Amber's confirmed per-interval prices.
- Optional historical price series.
- A guided migration/cleanup path off the old YAML kit (`ha-amber-energy-dashboard`).

Out of scope:
- Live and forecast prices: use core `amberelectric` or Amber Express. We run alongside them.
- Battery or device control.

## 2. Prior art and credits (README must acknowledge)

- **Tmbao/amberelectric-usages**: the same foundation (external statistics plus config flow).
  Unmaintained since mid-2024. Its gaps shaped this design: partial days locked in forever,
  no revision handling, a fixed 28-day window re-fetched hourly, statistic IDs derived from
  user-typed titles (the "Invalid statistic_id" failures), sync SDK pinned to a specific
  version, no tests. We adopt its feed-in compensation sign and per-channel-identifier keying.
- **danVnest/amberelectric-usage**: yesterday's figures as live sensors. Shows demand for
  simple "yesterday" display sensors.
- **melvanderwal/HA-Amber-Electric-Usage-Charts**: live estimate of cost from your own power
  sensor times live price. The approximate, live ancestor of our own-sensor cost feature.
- **hass-energy/amber-express**, **nickw444/ha-amberelectric**: confirmation-aware polling
  and jittered scheduling ideas.
- **madpilot/hass-amber-electric**: the original component; its usage sensor was dropped
  during the merge into core, which is the gap this project fills.
- **r5e/ha-amber-energy-dashboard v1**: the YAML predecessor (this repo, `legacy/v1/`). Its full design history is in
  `amber-technical-reference.md`, a private document that is **not in this repository**.

## 3. Architecture overview

- **Python custom component**, config-flow only (no YAML configuration).
- **External statistics** via `async_add_external_statistics`, owned by the `amber_energy_dashboard`
  source. No entities back them, so HA's statistics compiler can never corrupt them. This
  structurally removes Bug #1, Bug #2 and the production scar class of failures, plus the
  need for `recorder: exclude` and frozen template sensors.
- **The statistics table is the source of truth for running totals.** Baselines are read
  back from it (`get_last_statistics` / `statistics_during_period`), never from a separate
  counter.
- **`homeassistant.helpers.storage.Store`** holds everything that is not a statistic:
  import marker, per-day status, patience tracker, learned retention days, schedule seed.
  Versioned with migrations. No `initial:`-style reset hazard.
- **Thin async Amber client** on HA's shared aiohttp session (`async_get_clientsession`).
  No dependency on the `amberelectric` SDK, so no version conflict with core.
- **Scheduler** built on `async_track_point_in_time` / time-change helpers, not a fixed
  `DataUpdateCoordinator` poll interval. A coordinator may still be used to publish
  status to display sensors.
- **Repairs issues** instead of persistent notifications for anything needing user action.
- **Diagnostics platform**, with the API key and NMI redacted.

## 4. Amber API facts relied on

- Usage records carry: `type` (`Usage`), `date` (NEM calendar date), `nemTime` (interval
  END, UTC+10), `startTime` / `endTime` (UTC), `duration` (minutes), `kwh`, `cost` (cents),
  `perKwh` (c/kWh), `spotPerKwh`, `renewables`, `descriptor`, `spikeStatus`,
  `channelType`, `channelIdentifier` and `quality` (`estimated` or `billable`).
  `tariffInformation` is present on general channels and **absent** (not null) on feed-in.
- Channel identifiers vary by account (`E1`/`B1`, but also `E9`/`B9` and others). Always
  take them from site discovery; never hardcode them.
- `startTime` values carry a one-second offset (`14:00:01Z`) on every observed usage and
  price record; `endTime` is exact. Always floor `startTime` to the hour; never assume
  exact boundaries.
- Native usage resolution is 5 minutes. HA long-term statistics accept full-hour
  timestamps only. Short-term (5-minute) statistics cannot be imported at all.
- An empty response (HTTP 200, `[]`) means both "not published yet" and "outside
  retention". There is no distinguishing signal.
- Usage retention: on 2026-09-26 the earliest date with usage data was **2026-06-29,
  89 days back** (2026-06-28 and earlier return `[]`; the boundary day was complete).
  The earlier figure of 86 days was the YAML kit's configured value, not Amber's
  boundary. On 09-27 and 09-28 the earliest date was **still 2026-06-29** (90 and 91
  days back, on two lab VMs): on this account the boundary is a fixed date, not a
  rolling window. Other accounts may differ, so the boundary date is discovered and
  re-verified at runtime (section 8).
- Rate limit: 50 calls per 300 seconds, in **fixed** windows, reported in IETF
  `RateLimit-Limit`, `RateLimit-Remaining`, `RateLimit-Reset` (seconds to window end) and
  `RateLimit-Policy: 50;w=300` headers. There are **two independent counters**: one for
  `/sites` plus `/usage`, one for `/prices`. The budget is **shared** with other clients
  (other keys on the account, or the same source IP; not distinguishable), so treat it as
  shared: another client was observed using about 2 calls a minute.
- Maximum range per call, usage and prices alike: `endDate − startDate ≤ 7` (8 inclusive
  days are accepted, despite the message). Longer ranges return **HTTP 422** with the
  plain-text body `Range requested is too large. Maximum 7 days.` Plan 7-day windows.
- An invalid key returns **HTTP 403** with a JSON `message` (not 401) and no rate-limit
  headers. Treat 401 and 403 alike as auth failures.
- Cost sign convention from Amber: feed-in cost is negative (money earned). Feed-in
  `perKwh` is negative too.
- `cost` = `kwh × perKwh`, rounded by Amber to 4 decimal places (cents). `perKwh` equals
  the `/prices` `ActualInterval` `perKwh` for the same interval and is the all-in retail
  rate: wholesale energy (spot times loss factors) plus network time-of-use charge plus
  market and environmental per-kWh charges, GST-inclusive. There is no daily supply charge
  or membership fee in usage data.
- `/prices` returns complete 5-minute `ActualInterval` history back to at least
  **2025-03-01** (earlier dates return `[]`), well before a site's `activeFrom`. Whether
  this start is fixed or rolling is not yet known.

**Verified in Milestone 1** (details and raw observations in `reports/M1-report.md`):
1. Maximum date range: `endDate − startDate ≤ 7` for usage and prices (above).
2. Price history: back to 2025-03-01 (above). It will not limit own-sensor cost backfill
   in practice; HA's retention of the user's own statistics will.
3. Rate limit: shared budget, two counters (above). No second-key test; treat as shared.
4. Usage `date` is the NEM date; 00:00 to 24:00 AEST is exactly 24 UTC hours
   (`D-1T14:00Z` to `DT14:00Z`), 288 five-minute records per channel.
5. `cost` = `kwh × perKwh`, all-in per-kWh rate, no supply charge (above).

## 5. Time handling (DST-safe by construction)

- A **day** is a NEM day (00:00 to 24:00 AEST, UTC+10, no DST). Assign records to days using
  Amber's `date` field.
- **Hourly buckets** are the UTC hour of the interval start: `floor_hour(startTime)`.
- Local wall-clock time is never used in grouping or bucketing. It is used only for
  scheduling and display.
- Consequence: every NEM day is exactly 24 buckets, all year. DST transitions cannot
  create duplicate or missing hours. South Australian users see a 30-minute display
  offset, which is cosmetic and handled by HA.
- Tests must include synthetic fixtures spanning the April 2027 and October 2026
  transitions, run with HA's time zone set to Australia/Sydney and to Australia/Adelaide.

## 6. Statistics model

IDs are built from lowercased Amber site IDs and channel identifiers only. They never come
from user input.

Per channel:

| Statistic ID | Unit | Type | Notes |
|---|---|---|---|
| `amber_energy_dashboard:{site}_{chan}_energy` | kWh | sum | general, controlled load, feed-in |
| `amber_energy_dashboard:{site}_{chan}_cost` | AUD | sum | general, controlled load. Positive means paid |
| `amber_energy_dashboard:{site}_{chan}_compensation` | AUD | sum | feed-in only. **Positive means earned**, as the Energy dashboard expects |

Per site:

| Statistic ID | Unit | Type | Notes |
|---|---|---|---|
| `amber_energy_dashboard:{site}_net_cost` | AUD | sum | Amber's signed convention: sum of all channels' `cost`. Net amount owed |

Optional:

| Statistic ID | Unit | Type | Notes |
|---|---|---|---|
| `amber_energy_dashboard:{site}_{chan}_price` | AUD/kWh | mean | Off by default. Hourly mean of `perKwh`. README explains why this is not a cost rate |
| `amber_energy_dashboard:{site}_own_{sensor_slug}_cost` | AUD | sum | Own-sensor cost (section 11) |
| `amber_energy_dashboard:{site}_{chan}_cost_incl_fixed` | AUD | sum | Off by default. General cost plus daily fixed charges (section 19) |

Metadata (verified against HA 2026.9.3 in Milestone 1): `source="amber_energy_dashboard"`,
friendly `name`, and **always both `mean_type` and `unit_class`** (omitting either is
deprecated and breaks in HA 2026.11):

| Kind | `has_sum` | `mean_type` | `unit_of_measurement` | `unit_class` |
|---|---|---|---|---|
| energy | `True` | `StatisticMeanType.NONE` | `kWh` | `"energy"` |
| cost, compensation, net cost, own-sensor cost | `True` | `StatisticMeanType.NONE` | `AUD` | `None` |
| price (optional) | `False` | `StatisticMeanType.ARITHMETIC` | `AUD/kWh` | `None` |

Statistic IDs must be lowercase and contain no double or leading underscore in the object
ID (`valid_statistic_id`). Row timestamps must be timezone-aware and on the hour. The
minimum HA version is 2026.9; nothing newer is required.

Rules carried forward:
- Sum at full precision internally. Round only the values written.
- Cost arrives in cents; divide by 100 once, at write time.
- Hourly `state` = that hour's amount; `sum` = cumulative total.

Display-only sensors (no `state_class`, so never compiled into statistics): yesterday's
energy and cost per channel, last imported date, import status, days behind, next
scheduled run. These also partly serve users who export to InfluxDB and similar.

## 7. The daily import algorithm

For one target NEM day D and one site:

1. **Guard 1 (already imported):** if D is at or before the Store marker and D is not in the
   revision set (section 8), stop. Cross-check the marker against the last hour present
   in the statistics table; on disagreement, raise a Repairs issue and stop. Never guess.
2. **Fetch** usage for D (or a multi-day window, section 9).
3. **Guard 2 (empty response):** apply retention and patience logic (section 8).
4. **Guard 3 (completeness):** per channel, all records have `date == D`; one uniform
   `duration`; count equals `1440 / duration`; no duplicate `startTime`. Otherwise stop,
   without writing anything, and record the reason.
   - A channel in the usage data that is not in the configuration fails Guard 3
     (`unexpected_channel`), for example a newly installed controlled load. It is never
     silently ignored. It raises the `unexpected_channel` Repairs issue immediately.
     Imports stay stopped until the channel is added with the integration's
     **Reconfigure** flow. The issue is not a "fixable" Repairs issue; its text directs
     the user to Reconfigure (accepted in M4). A new channel's statistics start at the
     next day to be imported (marker + 1), from zero.
5. **Group** into 24 UTC-hour buckets per channel and metric.
6. **Baseline** for each statistic = the `sum` of the last row strictly before D's first
   hour (zero if none).
7. **Write** all statistics for D. One `async_add_external_statistics` call per statistic
   ID, containing all 24 rows.
8. **Verify** by reading back D's last hour for each statistic and checking the sums.
9. **Only then** advance the Store marker and record D's status (including whether any
   record was `estimated`).

**Crash recovery (accepted in M3).** Before writing day D (always marker + 1), the Store
records D as `pending`. Guard 1 accepts exactly one disagreement between the marker and
the statistics table: `pending` is marker + 1, and every statistic ends either at the
marker's last hour or at D's last hour (so D was fully, partly or not written). D is then
rewritten from the marker's baseline, and the walk continues. Every other disagreement
raises the `marker_mismatch` Repairs issue and stops without writing.
If the pending day has meanwhile become older than Amber's retention, it cannot be
fetched again: the run stops with `recovery_unavailable` (needs attention) rather than
guessing (accepted in M4).

Failure at any step leaves the marker untouched. Existing rows at the same timestamps
are overwritten by the write (confirmed in the YAML era, and re-confirmed for external
statistics in Milestone 1: a re-import of the same hours replaces `state` and `sum`
rather than adding rows).

## 8. Guards 2 and revisions

**Retention boundary (by date, M6b).** The Store keeps the boundary **date**
(`retention_boundary`): the earliest NEM day with usage data. `retention_days` is
derived from it (today − boundary) and is a display value only. The M5 lab showed a
boundary that stayed on the same date while the day count grew, which a stored day
count reported as a daily "move".
- **At setup**, discover it by bisection with single-day usage requests: at most 8
  calls, around today − 89 with a 30-day bracket. If the data goes back further than
  the bracket, the **oldest day seen with data** (the bracket end) is stored, "not
  bracketed older", and the daily check moves it back from there (M6c). If discovery
  cannot finish (budget, errors, the call cap) or nothing newer has data, fall back to
  today − 89 days.
- **`probe_retention` service** (M6c): forces a fresh discovery under the run lock.
  If it fails (an API error, the budget, the call cap), the stored boundary is kept
  and the error is reported; an auth failure also starts reauthentication.
- **Daily**, re-verify with exactly 2 calls: the boundary date (expected to have data)
  and the day before (expected empty). If both hold, the result is **"verified"**,
  whatever the day count, so a boundary that stays fixed costs no more than 2 calls a
  day. Only when the probes disagree does the check step or bisect: if the boundary day
  is empty, the boundary moved forward; if the day before has data, it moved back.
- **When the daily check detects a move (M4):** if the move is one day, step one day
  (confirmed with one more probe). If it moved further, bisect within a **15-day
  bracket** beyond the step probe, so the whole check stays within 8 calls (2 checks +
  1 step + 1 bracket + 4 bisection).
  - **A backward move beyond the bracket** accepts the bracket end (boundary − 17
    days, which is known to have data) as the new boundary, reported as "moved back at
    least N days" (4 calls). The next day continues from there, so a far-back boundary
    converges by 17 days a day, within the call cap, and is then verified with 2 probes
    (M6c; replaces the M4 "unresolved → discovery" for this direction, which never
    converged on a fixed, far-back boundary).
  - **A forward move beyond the bracket** (for example a rolling boundary after a long
    outage) also tries today − the day count seen at the last verification, with 2
    probes (at most 6 calls in all) (M6b). If that does not fit either, the move is
    *unresolved*: the old date is kept, and full discovery (30-day bracket, 8 calls)
    runs on the next day (accepted in M4).
- **Store migration (1.2 → 1.3):** the boundary date recorded with the last
  measurement is kept; without one, the day count is taken back from its measurement
  day (or today), and the count is dropped.
- **Boundary day empty, day before has data:** treated as a gap on the boundary day,
  not a boundary move. The retention value is kept (accepted in M4).
- Days older than the boundary that return empty are marked `skipped_unavailable`, and
  the marker advances past them.
- **Self-heal (2.1.0-rc2, from a forum report:** a new customer stuck on "Waiting for Amber
  data" for a pre-join day at the 89-day boundary, after discovery had fallen back to
  today − 89):
  - **The site's start date** (`/sites` `activeFrom`) is a lower bound: the stored boundary
    is raised to it (recorded as `site_start_applied`), and the walk never starts before
    it. If the start day itself has no usage, discovery uses the start date rather than
    falling back. `activeFrom` and `closedOn` are recorded in the Store (`site`, so in
    diagnostics) and logged at setup.
  - **A fallback boundary is provisional** (`provisional`, `needs_discovery`): discovery
    runs again on the next run, at most once a day, until a real discovery succeeds.
- **A failed check never moves a stored boundary (2.1.1,** seen on a lab clone with
  intermittent DNS failures, where each failed rediscovery stored today − 89 and moved a
  verified boundary forward a day at a time). A rediscovery that fails (a connection error,
  an API error, the budget) keeps the stored record, recorded as method
  `unresolved (<error>)` with `needs_discovery` still set, and tries again at the next
  discovery. An unresolved daily check keeps `last_verified`, and the daily check does not
  run while a rediscovery is pending. The today − 89 fallback applies only when there is no
  stored boundary yet.
  - **Leading empty run:** while nothing has been imported, a run of empty days at the start
    of the history followed by a day with data is skipped at once (`skipped_unavailable`,
    no patience), and the boundary moves to that day. The retention record keeps
    `self_heal` (when, which days, the previous method) and a warning is logged. This needs
    no extra calls: the walk has fetched those days. Interior gaps (after something was
    imported) keep the patience rule.

**Patience.** Tracks the number of distinct calendar days on which a given date has
returned empty. After `patience_days` (default 7), if any later day already has data
(proving a real gap rather than a feed-wide outage), the blocking day is marked
`skipped_gap` and the chain continues.

**Revisions (new).** Days containing any `estimated` record join a revision set. They are
re-fetched once a day for `revision_days` (default 14, configurable), counted from the
day's **first import**, not from its own date, so an old estimated day found during a
catch-up still gets its checks (accepted in M4). If the data has changed, the
integration **rewrites the tail**: from the earliest changed day forward to the latest
imported hour, re-deriving every sum. A mid-history change must shift all later
cumulative sums, so re-writing a single day on its own is not allowed. The tail
rewrite uses the same guarded per-day path, so it is covered by the same tests.
- The tail is **re-fetched** through that path rather than rebuilt from stored hourly
  states. Cost: at most `ceil(tail days / 7)` calls, and windows already fetched for the
  revision check are reused (accepted in M4).
- Progress is stored after each rewritten day, so an interrupted rewrite resumes from
  the first day not yet rewritten before anything else is written.

## 9. Catch-up and rate limiting

- Walk forward from marker + 1 toward yesterday, applying all guards to each day.
- Fetch in multi-day windows of **7 inclusive days** (one day inside the verified limit,
  so an Amber-side off-by-one fix cannot break us), then validate and write per day. This
  cuts API calls roughly sevenfold compared with the YAML design.
- Budget each run to stay well inside 50 calls per 300 seconds (target no more than 25
  calls per run), **per counter**: `/sites` plus `/usage` share one counter, `/prices` has
  its own.
- The budget is shared with other clients. **Read `RateLimit-Remaining` from the first
  response of each run** (and every later one) and stop the run early, as a normal
  "resume next attempt" outcome, if it falls below a reserve (default 15). The client
  implements this as a per-run budget (`RunBudget`).
- On HTTP 429, back off using the response headers and resume on the next scheduled
  attempt.
- A full 89-day recovery needs 13 usage calls and should complete within one run.

## 10. Scheduling

- **Automatic (default, accepted in M3):** a daily first attempt at a stable offset inside
  **06:30 to 08:30** local time, seeded from the config entry ID. Retries follow at
  **+3 h and +6 h**. Once caught up for the day, remaining attempts are skipped.
- **Fixed times (option):** a user-supplied list of times.
- **Startup run (accepted in M3):** a catch-up also starts once HA has started, on first
  setup (no marker yet) and whenever the last run was left `running` (it was
  interrupted, for example by a crash or kill). Without this, an interrupted run would
  wait for the next scheduled attempt.
- If still behind after the day's final attempt, raise a Repairs issue (replaces the
  YAML-era 12:05 notification). It clears automatically once caught up.
- **Evening retries (2.2.0-dev2):** when the day is still `waiting_for_data` after the
  last daytime attempt, retry every 3 hours after it while the attempt is by 22:00 local
  time (16:22 and 19:22 after 07:22, 10:22 and 13:22; fixed times likewise). The manager
  asks for them only while its last run is waiting, and reschedules after every run, so a
  day that imports (scheduled or by Run now) gets no further attempt, and a day that
  imports normally gets none at all. The last daytime attempt is not final when it ends
  waiting with an evening retry to come; the last evening retry is the final attempt (the
  "behind" and `incomplete_data` issues). Recovery-only mode schedules nothing. Background:
  on 2026-10-23 Amber published the previous day's usage only mid-afternoon, after every
  scheduled attempt.
- **`incomplete_data` (Guard 3) Repairs issue:** raised only if the day still fails on the
  **final attempt of the day**. A day that is incomplete in the morning and complete by a
  later attempt raises nothing. `unexpected_channel` is raised immediately.
- A future enhancement: learn when each user's data typically appears.

## 11. Usage modes and own-sensor cost

Decisions agreed for Milestone 5.

**Usage modes** (options flow, default **Full**):
- **Full:** unchanged. Scheduled import of all usage statistics (sections 6 to 10), plus the
  secondary chains below.
- **Recovery-only:** no scheduled imports (no timer). The service
  `amber_energy_dashboard.backfill(start_date, end_date)` imports a range on demand
  through the existing guarded path:
  - days older than the retention boundary are marked `skipped_unavailable`;
  - a range that starts at or before existing data is written with the tail rewrite, so
    every later sum is re-derived;
  - a range after the existing data is appended, and the days in between are recorded as
    `skipped_not_requested` (they are written if a later backfill requests them);
  - an empty requested day follows the patience rule (wait, or `skipped_gap` after
    `patience_days` with later data), and an interrupted backfill resumes from its stored
    progress.
  `run_now` in this mode extends only the secondary chains. Writing into other
  integrations' statistics is out of scope. In Full mode, `backfill` only fills or
  rewrites history up to the last imported day; later days belong to the schedule.
- **Pricing-only:** no usage statistics are written. Usage records are still fetched for
  their `perKwh`, to drive own-sensor cost and the price series.
- **Switching modes never deletes existing statistics.** Statistics that a mode does not
  update simply stop; switching back resumes each chain from its own marker (days older
  than retention by then are skipped as unavailable).

**Chains.** Besides the usage chain, each **own sensor** and the optional **price series**
is a *secondary chain* with its own marker, `last_written`, pending day, per-day records
and resumable rewrite in the Store, and its own Guard 1 (a separate `marker_mismatch`
Repairs issue per chain). They share the run's fetched usage records, the retention
boundary and patience. A revision (section 8) rewrites every active chain from the
earliest changed day.

**Own-sensor cost** (any mode):
- The user maps any cumulative energy sensor (`device_class: energy`, `state_class: total`
  or `total_increasing`) to one of the site's channels (general, controlled load or
  feed-in). Each mapping is a **config sub-entry** (supported by HA 2026.9): one entry per
  sensor with its own add and delete in the UI, a unique ID per sensor, and entities
  attached to it. Removing a mapping forgets its chain state; its statistic is kept.
- The price is the usage records' `perKwh` per interval (the billed rate), matched by
  channel identifier. Because Amber's own `cost` is exactly `kwh × perKwh`, own-sensor cost
  is directly comparable with Amber's figures.
- **Precise path:** each Amber interval's energy from the sensor's **5-minute short-term
  statistics** (the change of the statistic's `sum`, so meter resets are handled, converted
  to kWh) × that interval's `perKwh`, summed hourly into
  `amber_energy_dashboard:{site}_own_{slug}_cost` (AUD, sum). For a feed-in mapping the value
  is **positive when earned**, like the compensation statistics. Amber's `startTime`
  (`hh:mm:01`) is floored to the minute, so it lines up with HA's 5-minute buckets.
- **Fallback:** when the 5-minute data is gone (HA purges it after about 10 days), hourly
  energy × the hour's mean `perKwh`, with the day flagged `lower_precision`. The
  `own_fallback` option (default on) allows it; when off, such days are recorded as
  `skipped_no_short_term`. A day without even hourly sensor statistics is recorded as
  `skipped_sensor_data`.
- **Start and continuation:** a new mapping backfills from the later of the sensor's
  history start and the retention boundary. The history starts in the hour after the
  sensor's first statistic (the first row is only a starting reading). On that first,
  possibly partial, day the hours before the sensor existed count as zero energy. Each run
  then extends the mapping as new days are imported. A mapping re-added later continues its
  existing statistic after the last written day (a gap in between).
- Price history depth (section 4, item 2) does not limit this in practice; the sensor's own
  statistics and Amber's usage retention do.

From 2.2 the own-sensor mapping is replaced by **grid meters** and **appliances** (section
21); 2.1 mappings keep working as described here until converted.

**Reconciliation** (Full mode, for each own-sensor mapping: general, controlled load or,
from 2.1.1, feed-in, which 2.1.0 left without a sensor or a device): a display
sensor (no `state_class`) for the last NEM day that both Amber and the sensor have. Its
state is own kWh − Amber's kWh on the mapped channel (billed export for feed-in); the percentage and both totals are attributes. It
catches CT calibration drift and dropped sensors. In other modes it is unknown. It sits on
the mapping's own device, "<source friendly name> (own sensor)" (linked to the site's
device), and is named "Reconciliation" (M9: a device belongs to one config sub-entry, so
the mapping's entity cannot share the site's device; until 2.0.1 it was on the site's
device, named "Reconciliation: <source friendly name>"). The friendly name is the one
when the mapping was created. It is stored in the sub-entry and not re-derived later, so
renaming the source sensor does not rename it (mappings without a stored name use the
entity ID).

**Price series** (optional, default off): `amber_energy_dashboard:{site}_{chan}_price`,
AUD/kWh, hourly mean with min and max, `mean_type` arithmetic, `unit_class` None. It follows
Amber's sign (feed-in prices are negative). An hourly mean price is **not** a cost rate:
cost follows usage, which is not spread evenly over the hour; use the cost statistics.
Enabling it fetches the full retention window once (about 13 calls for 90 days); the option
help and the README say so. Afterwards it shares each run's fetch at no extra cost.

## 12. Configuration

**Config flow:**
1. API key, validated live. Each entry setup validates it again with one `GET /sites`.
   A rejected key, or a key that no longer sees the site, raises
   `ConfigEntryAuthFailed` (starting reauth). Network errors, 5xx and 429 raise
   `ConfigEntryNotReady` (HA retries setup).
2. Site selection (active sites, shown with NMI). The unique ID is the site ID, so one
   config entry per site.
3. Channel discovery. Controlled load is supported automatically.
4. Usage mode.
5. Schedule (automatic or fixed times), and "Add to the Energy dashboard" when it has no
   grid source (section 18).

A **reauth flow** handles key rotation (triggered on 401/403).

From 2.2.0-dev2 the entry is the Amber account and each site a sub-entry; the per-site
steps and settings below belong to the site (section 23).

**Options flow:** usage mode, schedule, revision window, patience days, optional price
series, own-sensor mappings, lower-precision fallback behaviour, and (from Milestone 6a)
"Migrate from the YAML kit" and "Undo migration" (section 14).

**Services:**
- `backfill(start_date, end_date)`
- `run_now()` and `import_day(date)`
- `probe_retention()`: a fresh retention discovery (section 8)
- `bill_estimate(date)`: the bill estimate for a cycle (section 19)
- `rebuild_from_anchor` was dropped at the M6b review: `backfill` with the guarded tail
  rewrite (section 8) covers the YAML `amber_window_rebuild` use.
- `migrate_v1(dry_run, confirm_backup, exclude_flagged, acknowledge_unverified, …)`,
  `undo_migration()` and `delete_legacy_statistics(confirm)` (Milestone 6a, section 14;
  `acknowledge_unverified` from section 18)

**Buttons** (section 18): Run now, and Re-check retention (diagnostic).

## 13. Error handling

- Timeouts on every request. 401/403 starts reauth (an invalid key returns 403). 429
  triggers backoff. A run stopped by the rate-limit reserve (section 9) resumes at the
  next attempt. 5xx and network errors retry at the next scheduled attempt.
- Empty or ambiguous inner results are always treated as **errors**, never as success
  (lesson from the YAML rebuild bug).
- Every stop is recorded in the Store with a reason, and exposed via the status sensor
  and diagnostics.
- The status sensor shows the last run's outcome, except that "Up to date" is shown only
  while nothing is outstanding. When the last run caught up but days have become due
  since (for example after a restart following downtime), it shows "Waiting for next
  run" (`waiting_for_next_run`, display only; the stored outcome is unchanged). In
  Recovery-only mode the usage marker is not scheduled work, so it does not count as
  outstanding.

## 14. Migration from the YAML kits (Milestone 6a)

Decisions agreed for Milestone 6a, with the M6a review decisions (re-base from stored
amounts, empty v1 cost, and M6a report section 5 details 1 to 6 accepted). VM 101 stays
in the migrated state with its legacy automation off permanently.
The published v1 kit (`legacy/v1/`) is in use by
other households, so the v1 path is a first-class, safety-critical path.

**Layouts recognised** (by name; recorded from `legacy/v1/` and, read-only, from VM 101):

| Role | v1 kit | Advanced YAML version |
|---|---|---|
| Grid import kWh | `sensor.amber_energy_import` | `sensor.amber_cumulative_grid_import_v2` |
| Grid export kWh | `sensor.amber_energy_export` | `sensor.amber_cumulative_grid_export_v2` |
| Import cost AUD | `sensor.amber_energy_import_cost` (HA-generated, optional) | `sensor.amber_hourly_cost_import` |
| Export cost AUD | `sensor.amber_energy_export_compensation` (HA-generated, optional) | `sensor.amber_hourly_cost_export` (Amber sign) |
| Automations | `automation.amber_usage_daily_statistics_import` | `automation.amber_daily_statistics_import` |
| Helpers | `input_number.amber_energy_{import,export}_running_total` | `input_number.amber_lifetime_grid_{import,export}_v2`, `input_number.amber_lifetime_cost_{import,export}`, `input_number.amber_retention_days`, `input_number.amber_nodata_patience_days`, `input_text.amber_last_imported_date`, `input_text.amber_nodata_tracker` |
| Scripts | none | `script.amber_daily_import`, `script.amber_retention_probe`, `script.amber_window_rebuild` |
| YAML blocks | `rest:` sensors `sensor.amber_daily_grid_{import,export}`, template sensors, `recorder: exclude` | `rest_command.amber_fetch_usage`, template sensors, `recorder: exclude` |

- Both layouts write recorder statistics under the frozen template sensors' entity IDs.
  History older than the retention window is **daily lumps**: one row at local midnight
  of day D holding the cumulative total through the end of D. The advanced version is
  hourly inside its rebuilt window. The v1 kit is daily lumps throughout, and its cost
  (the Energy dashboard's own cost sensors driven by a live price) is approximate.
- Automations are also found by content: any automation whose configuration mentions a
  layout's helpers, scripts or statistics belongs to that layout.
- **Choosing the layout.** A layout is a candidate when its import kWh statistic has
  data. It is complete when all its required statistics have data (v1: both kWh; advanced:
  all four). If one candidate is complete and its data is at least 2 days newer than any
  other candidate's, it is chosen, and the others are reported as ignored with the reason
  (older last data, missing statistics). Sources are never mixed between layouts.
  Otherwise, including partial detection (for example renamed entities) and two equally
  recent layouts, the user picks the legacy statistics for grid import kWh (required),
  grid export kWh, import cost and export cost from lists (manual step). Anything else
  (debug scripts, orphaned entities) is out of scope.
- The new integration must already hold data (its first run done), and the site must
  have one general channel (import) and at most one feed-in channel (export). Legacy data
  has **no controlled-load channel**; a controlled-load channel's statistics simply start
  with the integration's own data.

**History.**
- Legacy rows are copied into the new statistic IDs **only before the new integration's
  own data begins** (its first imported day, the *boundary*). Inside the window the new
  data wins; nothing there is copied.
- Sums are copied unchanged; each copied row's state is recomputed as the change from the
  previous row (the new statistics' convention). The copy ends with a row in the hour
  before the boundary (a carry row with state 0 if the legacy data has none there).
- The integration's existing rows are then **re-based from their stored hourly amounts**
  (adopted at the M6a review, replacing a re-fetching range rewrite): from the boundary to
  the last written day, each day's stored hourly amounts are written again through the same
  write-and-verify path as an import, continuing from the day before, the first day from
  the copied carry row. Progress (next day and running sums) is stored after each day, so
  an interrupted re-base resumes. It makes **no API calls**, so it also works when the
  integration's first day is older than Amber's retention. If the seam is already
  continuous (a re-run), the re-base is skipped. Future imports continue from the re-based
  sums; a later range rewrite (revision, backfill) of a first imported day with no earlier
  imported day continues from a row in the hour before it (the copied history), else 0.
- **Sign.** The advanced version's export cost carries Amber's sign (negative when
  earned). It is **negated** into compensation (state and sum), which fixes the Energy
  dashboard sign bug. The v1 kit's HA-generated compensation already uses HA's sign and is
  copied as is. For a manual pick, the sign is taken from the overlap window (the sign
  that matches the new compensation), and shown in the dry run.
- `net_cost` for the copied period is rebuilt as import cost minus compensation.
- v1 history is copied as is and recorded as **approximate** (daily lumps).
- **v1 cost (confirmed in M6b on a real v1 install, VM 9105).**
  - The kit's cost and compensation are the Energy dashboard's own "current price"
    sensors (`sensor.amber_energy_import_cost`, `sensor.amber_energy_export_compensation`,
    state class `total`), driven by a sensor frozen at 0.
  - When the preferences are saved they read 0.0. After the next HA restart they are
    `unknown` and stay so, because their source never changes state. Home Assistant
    therefore compiles statistics only between the save and the next restart: on 9105, one
    hourly row each, with sum 0.0.
  - A real v1 install thus has **no cost history**, at most a few zero rows.
  - The rule stays as it was: if the v1 cost and compensation rows to copy move less than
    0.05 AUD in total (the sum of the changes over the copy period), the dry run reports
    "v1 cost history appears empty; not copied", and they are not copied (nor net cost);
    otherwise they are copied as is. When no cost rows lie before the boundary (the usual
    case), nothing is copied and the note says "no cost history copied".
- **Implausible legacy rows (M6b).** Before anything is copied, every legacy series of
  the chosen sources is scanned. A row's step is its sum minus the last good row's sum.
  - Energy: a step above **100 kWh per elapsed hour** between the row and the last good
    row (at least one hour's cap; M6c, so a large household's daily lump of, say,
    150 kWh is never flagged), or any decrease (more than 0.0005 kWh), is flagged.
  - Cost: a step above **500 (currency) per elapsed hour** between the row and the last
    good row (at least one hour's cap) is flagged (M6c review). An Amber wholesale price
    spike (about 17.50/kWh at the market cap) can legitimately make one hour cost over
    100, and flagging it would drop a real, expensive hour. A cost **decrease is never
    flagged**, since negative prices make the cost fall legitimately.
  - One row of lookahead names the kind. A *spike* is a single bad row after which the
    series is plausible again from the last good row, as with the v1 README's 99999
    recorder test. A *jump* or *reset* is a series that continues from the new level.
  - The dry run lists every flagged row (statistic, hour, sum, step, kind, and whether it
    lies in the copy period, the overlap, or after the integration's data).
  - A real run **refuses by default**. With the explicit option `exclude_flagged`
    (service field, or a checkbox in the options flow that re-runs the dry run first),
    flagged rows are left out and the sums around them are re-derived. A spike's row is
    simply dropped; after a jump or reset every later sum is shifted by the step. Parity
    and the copy then use the cleaned series. The choice is stored with the run's
    sources, so a resumed run keeps it.
  - This replaces the M6a rule "copied kWh sums must never decrease", which the scan
    covers.

**Parity check** (before any change; local statistics only, no API calls). Over the
overlap window (days both the legacy and the new statistics have), compare daily totals:
- advanced version: kWh exact (equal to 3 decimals); import cost and export cost by
  magnitude within 0.01 AUD per day;
- v1 kit: kWh within 0.01 kWh per day; cost differences are reported but never fail.
  v1 lumps are assigned to their local date.
- A manual pick uses the advanced rules if its overlap data is hourly, else the v1 rules.
- Each legacy statistic is compared inside its own span: from the day after its first row
  (that day has no earlier total) to its last row. A cost series that starts later than the
  kWh series (as on VM 101, where cost tracking started fresh at the rebuild) is therefore
  not a mismatch before it starts; a missing day inside the span is.
- **Legacy gaps (2.0.1, from a real v1 migration).** The overlap window runs from the
  legacy import series' first comparable day to the integration's last day, so it includes
  days after the kit stopped. A day is a *legacy gap* when the legacy statistics have no
  data for it: no legacy import row that day, or exactly 0 import and 0 export (it rounds
  to 0.000 kWh; a stalled v1 kit keeps writing rows, adding 0 when its REST sensor fails)
  while the integration's import is above 0. Gap days are reported, not compared; the
  integration's own data covers them. Consecutive gap days form a run:
  - *trailing* (no compared day after it): the kit stopped. Reported as "The YAML kit
    stopped importing after D; N days (from to to) will come from the integration's own
    data."
  - *interior*: the next day's legacy total (from the last legacy data before the run when
    the run has no rows) is compared with the integration's total for that day alone (the
    kit resumed without catching up). If that disagrees but it agrees with the
    integration's total over the run plus that day, the day is a *catch-up lump* (the kit
    resumed and caught up): every statistic is compared as one total over the run and the
    lump, with the per-day tolerance times the number of days (hourly kWh still exact to 3
    decimals). Otherwise the day is compared on its own and fails as usual.
  Days where both sides have data keep the tolerances above; only genuine disagreement
  fails.
- At least 3 compared days (both sides with data) are required. On failure the migration
  stops with a clear report and changes nothing. (From 2.1, fewer than 3 compared days
  with no difference and a legacy gap is *unverified*, and runs with an explicit
  acknowledgement; section 18.) The report counts the failures by
  category (statistic and problem) before listing the first 10, and the reason carries the
  same summary.
- A gap does not change the copy, the re-base or the Energy dashboard switch: the copy
  takes only legacy rows before the boundary, the re-base continues from the copied carry
  row, and the integration's own rows cover the gap.

**Energy dashboard.** A full copy of the current energy preferences is saved in the Store
and read back from disk (the backup check). With explicit confirmation, after a dry-run
preview of the exact before and after, grid sources that use the legacy statistics are
switched to the new ones: import kWh and cost (stat_cost; price entity and number
cleared), export kWh and compensation (stat_compensation; export price cleared). The
update goes through HA's energy manager, validated against its schema and read back. If
that cannot be done safely (no preferences, schema failure, read-back mismatch), the
result gives exact written instructions instead. Legacy statistics used elsewhere (for
example as device consumption) are reported, not changed.

**Cleanup, cautious.** The layout's automations are **disabled** (turned off), never
deleted; each one's prior state is recorded, and one already off stays off. Everything
else is **listed** for the user to remove (helpers, scripts, YAML blocks, recorder
excludes, or, for package users, the single package file), in the result and in a
persistent Repairs issue. The issue is re-checked when the integration loads (once Home
Assistant has started) and after each scheduled attempt: it lists only the leftovers that
still exist (entities by state or registry entry, the rest_command service, the v1
backfill files in the configuration folder) and clears itself once none remain. Undo
removes it. Two parts are advisory and never keep the issue open: the `amber_api_key` line
in secrets.yaml (listed in the result only, since something else may use it; secrets.yaml
is not read) and the old statistics. Old statistics are kept by default; a separate,
explicit service deletes them later (after which undo is no longer possible).

**Undo.** Every change is recorded in the Store. "Undo migration" restores the saved
energy preferences and re-enables only the automations the migration turned off. A later
run keeps the undone runs' records in its history. Copied
pre-window history and the re-based sums stay; this is documented. After an undo the
migration may be run again.

**UI and safety.**
- Options flow menu entry "Migrate from the YAML kit": dry-run report, then confirm
  (including "I have a current Home Assistant backup"), then run, then result. "Undo
  migration" appears once a migration has started.
- Services: `amber_energy_dashboard.migrate_v1(dry_run, confirm_backup, …manual picks)`
  (dry run by default), `undo_migration`, and `delete_legacy_statistics(confirm)`.
- Idempotent and resumable: progress and the plan are in the Store; a real run while a
  migration is in progress resumes it (re-checking parity first); a completed migration
  refuses to run again until undone. It refuses if the parity check or the backup check
  fails, or without the backup confirmation.
- Order: detect, plan, parity, backup check, copy, re-base, energy preferences, disable
  automations, record cleanup.

## 15. Testing strategy

- `pytest-homeassistant-custom-component`, pinned to the target HA version.
- Recorded Amber fixtures, anonymised (no NMI, no site ID from the real account).
- Required coverage: every guard's stop path; partial days; duplicates; mixed durations;
  an empty-then-late day; retention boundary movement in both directions; patience
  write-off; a revision tail rewrite; 429 backoff; 401 reauth; DST fixtures (section 5);
  catch-up after a simulated 8-month outage; injected failures mid-walk (the marker must
  not advance).
- Lab end-to-end on disposable clones of the HAOS template: real recorder, real Energy
  dashboard.
- Parity: over a shared window, totals must match production's known-good figures exactly.

## 16. Milestones

Each milestone ends with a written report and the maintainer's sign-off before the next starts.

1. **Scaffolding and verification:** repository, manifest, `hacs.json`, dev environment (uv,
   pinned HA test harness, ruff), CI workflow files (hassfest, HACS validation, pytest; not
   pushed yet). Async Amber client with tests. The five API verifications in section 4.
   Lab smoke test: clone the template, reach HA over HTTP and SSH, destroy the clone.
2. **One day end to end:** config flow, the statistics model (section 6) and a service that
   imports one given NEM day. Verified on a lab clone and compared against known-good
   figures.
3. **Catch-up and first guards:** scheduler, catch-up loop, Store, Guards 1 and 3.
4. **Recovery:** retention, patience, revisions and tail rewrite, Repairs issues.
5. **Usage modes and own-sensor cost:** plus reconciliation and the optional price series.
6. **Migration and release.** 6a: migration from the YAML kits (section 14), verified on
   VM 101. 6b: the v1-kit clone test, README with credits, HACS release.
7. **2.0.0 release preparation:** guides, cleanup repair, release notes (2.0.0).
8. **2.0.1:** legacy gaps in the migration parity check.
9. **v2.1 usability** (section 18): device buttons, Energy dashboard setup and its Repairs
   issue, the migration acknowledgement for unverifiable history, documentation.
10. **v2.1 bill estimate** (section 19).
11. **v2.1 export allowance for two-way network tariffs** (section 20). Depends on M10's
    billing day.
12.–16. 2.1.0 release candidates, and 2.1.1 (own-sensor fixes, naming, retention guard).
17. **2.2.0-dev1:** site identity, grid meters, appliances, the calibration offset, and
    converting 2.1 own-sensor mappings (section 21).
18. **2.2.0-dev2:** the account entry with site sub-entries (section 23), and evening
    retries (section 10).
19. **2.2.0-dev3:** site-wide sources and Today so far (section 22), the calibration
    offset as a mean (section 21), skipping a site the key no longer sees, and Rename site
    (section 23).
20. **2.2.0-dev4:** solar-aware appliance cost with the battery's data path (section 22),
    the previous bill estimate (section 19), Today so far's offset-corrected value and
    live prices found after setup (section 22).
21. **2.2.0b3:** the Repairs issue for a waiting solar-aware chain (section 22); beta 3.

The battery cost basis for solar-aware appliance cost moves to 2.3, after the 2.2 release
(decided at the M20 review).

## 17. Carried-forward lessons (do not rediscover)

- Never let a dashboard-facing statistic be derived from a live entity state.
- An import overwrites existing rows at the same timestamps. A double run is a risk to
  correctness, not a harmless no-op.
- A chunked, racing import lost about 87 kWh in the YAML era. Compute absolute sums
  ourselves and write them in single calls.
- `nemTime` marks the interval END. Bucket by start.
- Round once, at output.
- Derive the expected interval count from `duration`, never hardcode 288.
- Advance the marker last, only after verified success.
- Treat empty inner results as errors.
- Test failure paths, not just happy paths. Lab before production. Verify every write.

---

# Version 2.1

Agreed in the v2.1 planning chat (October 2026). Milestone 9 is implemented first;
Milestones 10 and 11 are design only until their milestones start.

## 18. Usability (Milestone 9)

**Site device and entry title (2.1.1).** The site's device is named "Amber Energy
Dashboard (site)": Home Assistant lists it under its own heading "Devices that don't
belong to a sub-entry", which the old name "Amber Energy Dashboard" did not explain.
Entity IDs do not change: existing ones are kept by the entity registry, and new site
entities suggest the ID 2.1.0 gave them (`<platform>.amber_energy_dashboard_<name>`, from
the English name). The entry's default title is "Amber Energy Dashboard" (the NMI is not
shown); with several sites, "Amber Energy Dashboard (…1234)", the NMI's last 4 digits.
Titles the integration chose (including 2.1.0's "Amber <NMI>") follow the default as sites
are added or removed; a title the user set is kept.

**Buttons** (button platform, on the site's device):
- **Run now** (`button.<device>_run_now`, under Controls): the same as the `run_now`
  service, a catch-up run under the run lock.
- **Re-check retention** (`button.<device>_re_check_retention`, a diagnostic entity): the
  same as `probe_retention`. A failure raises an error in the UI, and the stored boundary is
  kept, as with the service.
- Names and icons through translations (`entity.button`) and `icons.json`.
- **Own-sensor devices.** Each own-sensor mapping (a config sub-entry) has its own device,
  "<name> (own sensor)", linked to the site's device, holding its reconciliation sensor
  (section 11). Home Assistant 2026.9 gives a device exactly one sub-entry: with the
  reconciliation sensor on the site's device, the buttons (no sub-entry) moved the device
  out of the sub-entry, and Home Assistant could drop the sensor, depending on which
  platform finished setting up last. Home Assistant also warned that sharing stops
  working in 2027.8.

**Energy dashboard setup.**
- The grid sources for an entry: **one grid source** with the general channel's energy
  (`stat_energy_from`) and cost (`stat_cost`) and the feed-in channel's energy
  (`stat_energy_to`) and compensation (`stat_compensation`); price fields empty;
  `cost_adjustment_day` 0. A **controlled-load channel gets a second grid source** with its
  energy and cost (Home Assistant's grid source holds one import meter; several grid
  sources are allowed). A site without a general channel puts the feed-in on a grid source
  with no import meter. The sources are validated against Home Assistant's energy schema
  before saving.
- **Config flow:** the last step (schedule) shows "Add to the Energy dashboard" (default
  on) only when the Energy dashboard has **no grid source**. The choice is stored in the
  entry's data. It is carried out once, when the entry is first set up; the Store records
  the outcome (`energy_setup`), so it never repeats. If a grid source exists by then, it
  is skipped silently. An existing grid source is never modified; legacy kits are the
  migration's job.
- **The change uses the migration's energy-preference code:** a full copy of the
  preferences is saved in the Store and read back from disk before any change, the new
  preferences are validated and saved through Home Assistant's energy manager, the
  changed sources are read back, and on a mismatch the saved sources are restored.
- **Repairs issue `energy_not_used`** ("The Energy dashboard isn't using Amber Energy
  Dashboard", a warning, fixable). Raised when, **24 h after the first successful
  import**, no energy preference (any source or device) references any of the entry's
  statistics. The Store records the first successful import (`first_import_at`); an
  existing install with data gets the time of the upgrade, so it has the same 24 h.
  Checked when Home Assistant has started, after each run, and whenever the energy
  preferences change; cleared automatically once the statistics are in use. Not raised
  in Pricing-only mode (no usage statistics).
- **Fix flow:**
  - no grid source: offer to add the grid sources (as above); the issue is resolved;
  - a YAML kit is detected (migration detection finds a candidate): explain that the
    migration switches the Energy dashboard, and point to the integration's options
    ("Migrate from the YAML kit"); the issue stays until the statistics are in use;
  - otherwise (another grid source): explain how to select the statistics by hand, and
    let the user dismiss the issue. A dismissal is stored and the issue is not raised
    again for that entry.

**Migration acknowledgement for unverifiable history** (changes section 14's "at least 3
compared days").
- When parity has **fewer than 3 compared days, no differences, and at least one legacy
  gap** (the legacy data does not overlap the integration's: all gaps, or a kit that
  stopped before the integration's first day), parity is *unverified*, not failed. The dry
  run says why, for example "The YAML kit's last data is D, before the integration's first
  day (B); the old history can't be checked against Amber's data", and names the days
  that will have no data in either source.
- A real run then needs an explicit acknowledgement: the options-flow checkbox "I
  understand the old history can't be checked against Amber's data", or the service field
  `acknowledge_unverified: true`. The choice is stored with the run's sources, so a
  resumed run keeps it.
- Fewer than 3 compared days with no gap (for example an integration with only 2 days of
  data) still refuses: waiting fixes it. Any genuine difference still refuses.

**Documentation:** README FAQ on NEM days and daylight saving; README troubleshooting on
HACS's "icon not available"; INSTALL.md's version step shortened to a pre-release
footnote, the new setup option (section 3) and the buttons (section 4).

## 19. Bill estimate (Milestone 10)

**Options** (options flow, a "Bill estimate" step; off until a billing day is set):
- billing day of month, 1 to 28 (the cycle runs from that day to the day before it next
  month);
- daily fixed charges **excluding GST**, as on the Amber bill's charges summary (rc2): the
  **daily supply charge** (the summary's "Network Daily Supply Charges" rate, which already
  includes metering), the **Amber subscription** (from the Amber Fees; 0 is valid for a
  free first-year offer), and **other daily charges** (optional, default 0), in AUD per
  day. The daily supply charge and the subscription have no default and must be entered
  with a billing day; GST defaults to 10 %. Every field has help text, and the step links
  to INSTALL.md "Finding these on your bill". rc1's separate network, metering and subscription fields are migrated (config
  entry 1.1 → 1.2: network + metering → daily supply; subscription → Amber subscription);
- GST rate, default 10 %.

**Sensors** (AUD, display only, no state class):
- **Bill to date** = billed import cost (Amber's values, GST-inclusive, all import
  channels) − billed feed-in compensation + fixed charges × elapsed days × (1 + GST), over
  the **complete imported days** of the current cycle. Attributes: the breakdown by line
  (usage, each fixed charge, export credit), cycle start and end, data-through date, days
  elapsed and days in cycle.
- **Projected bill** = average daily usage cost (import cost − compensation) over the
  completed days × days in the cycle, plus the exact fixed charges for the whole cycle.
- **Days into cycle** and **average cost per day**.
- One-off charges (for example card payment fees) are out of scope; the docs say so.

**Optional statistic** (opt-in): "import cost including fixed charges",
`amber_energy_dashboard:{site}_{chan}_cost_incl_fixed` (general channel), with the fixed
daily amount including GST spread evenly over the day's 24 hours, for users who want the
Energy dashboard's totals to match the bill. The user selects it in the Energy dashboard
instead of the plain cost. It is written by the import path alongside the other
statistics (a secondary chain, so it follows revisions and rewrites).

**Acceptance** (VM 9102 holds this account's history): the bill for 28 Aug to 27 Sep
2026 (31 days):
- usage $98.68 ex GST ($108.548 inc; the integration holds $108.542390);
- fixed: network 0.7019 + metering 0.3852 + subscription 0.7471 = 1.8342 $/day ex GST,
  × 31 × 1.1 = $62.5462;
- export credits $5.38 (wholesale 5.20 + reward 0.18); a one-off card fee $1.61 ex GST
  ($1.77 inc); total payable $167.51, so **$165.74 without the card fee**.
- With M11's allowance adjustment (compensation $5.3749): 108.5424 − 5.3749 + 62.5462 =
  **$165.71**, within a few cents of $165.74.
- Without it (billed compensation $2.1238): 108.5424 − 2.1238 + 62.5462 = **$168.96**,
  about **$3.25 higher**. (Confirmed at the M9 review: the brief's "lower" was a sign slip.)

**As built (M10):**
- **Cycle days are NEM days**, as for Amber's meter data and bill. During daylight saving
  a cycle starts at 01:00 local time; the local hour 00:00 to 01:00 on the billing day
  belongs to the previous cycle. "Today" (days into cycle) is the NEM date.
- **Coverage:** usage counts only imported (complete, verified) days, from the Store's
  per-day totals (exact, and updated by revisions). Fixed charges apply to every day from
  the first to the last imported day of the cycle (`data_from` to `data_through`); a
  skipped day inside that range is listed in `missing_days` and still bears its fixed
  charges. A cycle that began before the integration's first day says so
  (`complete_from_cycle_start` false). With no imported day yet, bill to date and the
  projection are unknown.
- **Average cost per day** = bill to date ÷ days covered (fixed charges included).
  **Projected** = (usage − credit) per imported day × days in cycle + fixed charges ×
  days in cycle × (1 + GST).
- **Billing day changes** apply at once: the estimate is recomputed, nothing is stored per
  cycle. Turning the estimate on or off reloads the entry (its sensors are added or
  removed).
- **Modes:** Pricing-only has no usage, so the sensors are unknown and the action refuses;
  the statistic is not written. Recovery-only uses whatever days are imported.
- **Statistic including fixed charges** (rebuilt at the M10 review): a **derived
  statistic** (`derived.py`) built from the integration's own import cost statistics
  (general and controlled load), not from usage records, so it covers the whole history,
  including history copied from a YAML kit, with no API calls. Its sum at each row = the
  cost sums (carried forward) + the accumulated fixed charges; the charges accrue with the
  time between rows (an hourly row adds the daily amount ÷ 24, a copied daily lump a whole
  day, the first row its own hour). **Sync by difference** after every run, every options
  change and every migration: the expected rows are recomputed from the stored cost rows,
  and the statistic is rewritten from the first row that differs (cleared and rewritten in
  full if it holds a row the cost no longer has). So every rewrite of the cost (revision,
  tail rewrite, crash recovery, backfill, migration) is followed without hooks in each
  write path. Every write is read back.
- **Changed charges** (accepted at the M10 review) apply from the day after the last
  imported day: the Store keeps a schedule of daily amounts with the hour each applies
  from (`fixed_schedule`), so earlier rows keep the amount they had.
- **Action** `bill_estimate(date)`: the estimate for the cycle containing `date`
  (default today), for any cycle still in the Store's day records (about 400 days).
- **Previous bill estimate** (2.2.0-dev4): a sensor on the site with the estimate for the
  cycle before the current one (its `bill_to_date` as the state; attributes: the lines,
  cycle dates, `data_from`, `data_through`, `days_elapsed`, `days_imported`,
  `missing_days`, `gst_percent` and `complete`: every day of the cycle imported). It
  replaces users' own template packages for last cycle's total. An example bill card
  (button-card) is in `docs/cards/`.
- **Acceptance** (VM 9102, 2026-10-08): billing day 28, the three charges, GST 10 % →
  **$168.96** for 28 Aug to 27 Sep (usage 108.5424, export credit −2.1238, network
  23.9348, metering 13.1353, subscription 25.4761). The brief's sign is corrected: the
  targets are $168.96 now and $165.71 after M11's allowance adjustment.

## 20. Export allowance for two-way network tariffs (Milestone 11)

**Facts** (reports/billing-reconciliation-2026-09.md):
- Amber's feed-in `perKwh` = loss factor (about 0.974) × spot + a network component:
  −1.86 c/kWh in the solarSponge period (10:00 to 14:00) and +3.47 c/kWh in peak
  (weekdays 16:00 to 20:00, April to October; higher November to March). The period of
  each interval comes from the general channel's `tariffInformation.period` (feed-in
  records have none).
- Endeavour N61's free export threshold is 2,920 kWh a year, "calculated on a daily basis
  and applied to the billing period" (AER pricing proposal): allowance = 8 kWh × days in
  the billing period, against total exports in the penalty window over the period; only
  the excess is charged. The September bill shows no export charge (174.79 kWh exported in
  the window, against a 248 kWh allowance), while Amber's usage data deducts it per
  interval.

**Detection and options:**
- Detect the network (`/sites` `network`) and the channel tariff codes at setup; store
  them in the entry, refreshed on reload (the setup `/sites` call, no extra calls).
- A built-in table of two-way tariffs, starting with **Endeavour N61 only** (verified);
  others are added when verified.
- Options: enable; override the allowance (kWh per day); billing-period or daily
  totalling; the penalty period by name (default from the table, `solarSponge` for N61).

**Rates measured, not hard-coded:** the penalty and reward rates are the median network
component per period over recent days (`−perKwh − loss factor × spot`, with the loss
factor measured from offPeak intervals), because Endeavour's published rates differ from
what Amber applies (1.79 against 1.86 c/kWh).

**Stored per day:** window export kWh and the penalty amount (Σ kWh × rate in the penalty
period).

**Outputs:**
- An **"adjusted compensation"** statistic, `amber_energy_dashboard:{site}_{chan}_compensation_adjusted`:
  billed compensation plus the refunded penalty on exports within the allowance,
  accumulated per billing period (so the refund is not known in full until the period's
  window exports exceed the allowance, or the period ends), for users to select in the
  Energy dashboard.
- Sensors: allowance used and allowance remaining (kWh) for the current billing period.
- The bill estimate's export line uses the adjusted figure.

**Acceptance (September):** billed compensation $2.1238 + refund (174.789 kWh × 1.86 c =
$3.2511) = **$5.3749**, against the bill's $5.38.

**As built (M11):**
- **Detection** at every setup from the `/sites` answer the setup already fetches: the
  network and the channel tariff codes, recorded in the Store (`tariff`). On the test
  account: network "Endeavour Energy", general N71, feed-in **N61** (the feed-in channel's
  code identifies the two-way tariff). The table (`export.KNOWN_TARIFFS`) has Endeavour
  Energy N61 only: 8 kWh a day, penalty period `solarSponge`, reward period `peak`,
  billing-period totalling.
- **Settings:** on by default for a known tariff with a billing day; otherwise off. The
  options ("Export allowance") override: on/off, kWh per day, billing-period or daily
  totalling, and the penalty period name. Billing-period totalling needs the billing day
  (the step refuses without one; at runtime the feature is then off). An unknown tariff
  needs the kWh per day. The reward period is the table's, else `peak`.
- **Per-day aggregates** (Store `export_days`, keyed by NEM date, pruned with the day
  records): window export per NEM hour and in total, the penalty and reward rates (c/kWh)
  and amounts (AUD), the loss factor, and a fingerprint of the day's usage. Measured in
  each run for imported days that lack them (inside retention: about 13 calls once for 90
  days, using the run's 7-day windows) and for days whose records changed (a revision's
  rewrite fetches them anyway). The loss factor is the median of earned ÷ spot over
  off-period intervals with |spot| ≥ 5 c/kWh, falling back to the last measured one; a day
  with neither has no rates (`measured` false) and no refund.
- **Measured without a run (2.1.0):** the days lacking aggregates are also measured, under
  the run lock and with a run budget but without an import run, when the allowance or the
  bill estimate is turned on (at the setup the reload makes) or changed in the options, and
  at a scheduled retry that skips because the day is already imported. Nothing is fetched
  when no day is missing. A day found without feed-in records (or before the feed-in
  channel existed) is not fetched again. API errors stop it with a warning; the next
  attempt continues.
- **Refunds:** for each allowance period (billing cycle, or day), allowance = kWh per day ×
  the period's days; window exports are set against it hour by hour in time order, and the
  penalty on the part within it is refunded at that day's measured rate. Days without
  aggregates count nothing and are listed as unadjusted.
- **Adjusted compensation** `…_{feed-in}_compensation_adjusted`: a derived statistic (as in
  section 19) from the compensation statistic plus each hour's refund, over the whole
  history; days before retention are carried unchanged.
- **Sensors** (current period, up to yesterday): Export allowance used and remaining (kWh),
  Export charge after allowance (AUD) = penalty − refund. Attributes: period, totalling,
  allowance, window export, measured rates (median of the last 30 days), data through,
  unadjusted days.
- **Bill estimate:** with the allowance, the export credit line is the billed compensation
  plus the full penalty (what the bill credits before the network's charge), and a
  `network_export_charge` line is the penalty less the refund. Their sum is the adjusted
  compensation.

---

# Version 2.2

Agreed in the v2.2 planning chat (October 2026).

## 21. Site identity, grid meters and appliances (Milestone 17)

**Site identity.**
- The site device's **model** line is "<network> · NMI …<last 4>" (for example "Endeavour
  Energy · NMI …1234"), from the `/sites` answer at each setup. The full NMI is never shown.
- **Site name.** The config flow asks for it once, after the site is chosen
  (`site_name` step): it shows the network, "NMI ending …1234" and the active-from date,
  and suggests "Home". It is stored in the entry's data (`site_name`) and is the site
  device's **name**. Installs from before 2.2 have none and keep "Amber Energy Dashboard
  (site)".
- Later renames: the site settings' **Rename site** step (2.2.0-dev3, section 23). Areas
  and labels use Home Assistant's device page. A name the user gives the device there
  (`name_by_user`) is the site's name wherever the integration shows one
  (`devices.displayed_site_name`), so far the multi-site entry title; a device-registry
  listener updates a default title on a rename.
- **Entity IDs stay pinned** (section 18's `async_site_entity_ids`). Site entities link to
  the device by identifier only (`devices.site_link`), so they never reset its name or
  model; the device is created, named and given its model at setup.
- **Entry title:** "Amber Energy Dashboard", or with several sites "Amber Energy Dashboard
  (<site name>)" (the NMI's last 4 digits for a site without a name). Default titles follow
  the name; a title the user set is kept.

**Sub-entry types** (`async_get_supported_subentry_types`): `grid_meter` and `appliance`.
The 2.1 `own_sensor` type is no longer offered, but existing ones load and work unchanged
(statistics, entity IDs, reconciliation). Grid meters and appliances store their site's
ID (`site_id`), for a later split into per-site sub-entries (see the M17 report).

- **Grid meter** (`grid_meter`): a meter on the main supply. Data: `name`,
  `import_entity_id`, `import_channel` (general; controlled load selectable when the site
  has one), and optionally `export_entity_id` with `export_channel` (the feed-in channel;
  the field is offered only when the site has one). Title "<name> · grid meter"; unique ID
  `grid_meter:<import entity>`. One device per meter, named `<name>`, model "Grid meter",
  holding **Import reconciliation**, **Export reconciliation** (with an export) and the
  **Calibration offset** (with both). Each side is a cost chain, keyed `own:<sub>:import`
  and `own:<sub>:export`.
- **Appliance** (`appliance`): any single energy sensor. Data: `name`, `entity_id`,
  `channel` (general by default, controlled load when the site has one; a converted 2.1
  feed-in mapping keeps feed-in). Title "<name> · appliance (<channel label> <id>)";
  unique ID `appliance:<entity>`. One device, model "Appliance (<channel>)", with
  **Yesterday energy** and **Yesterday cost** (or **Yesterday compensation** on feed-in),
  from the chain's record for yesterday's NEM day. **No reconciliation:** comparing one
  circuit with the whole house's billed total means nothing. Chain key `own:<sub>`.
- A sensor can be priced by one mapping only (`sensor_in_use`), because the cost
  statistic's ID comes from its entity ID: `amber_energy_dashboard:{site}_own_{slug}_cost`
  for every kind. The statistic's display name follows the mapping ("<name> import cost
  (Amber prices)"); a 2.1 mapping keeps "Amber own <slug> cost".
- Help text in both flows explains the kind with generic examples, and for grid meters
  why they are reconciled and that the mapped sensors must be the raw ones.

**Calibration offset** (`calibration.py`; grid meters with import and export):
- For each hour of the **complete billed days** in the 14 NEM days ending at the last
  imported day (status imported, no estimated interval): Amber net (import − export, the
  hour's `state` of the integration's energy statistics on the meter's channels) minus the
  meter net (the change of each mapped sensor's hourly `sum`), × 1000 → watts.
- Hours where either meter statistic lacks the row at the hour or the one before are left
  out. Below 24 usable hours there is no value.
- The sensor (W, device class power, no state class) shows the **mean**, rounded to
  0.1 W (2.2.0-dev3; the median until then): the correction is meant to make the meter's
  energy totals match the bill, and only the mean does that when some hours disagree
  strongly (on the maintainer's CT meter typical hours are 14–15 W, but hours of rapid
  import/export switching are far off; the mean over 5–21 October was about 10 W). Only
  hours with a reading gap are left out. Attributes: `hours_used`, `days_used`,
  `first_day`, `last_day`, `median_w`, `p25_w`, `p75_w`, `iqr_w`, `last_updated`.
- Recomputed after every run and once Home Assistant has started (so after a reload);
  a failure is logged and never stops a run.
- Always from the mapped (raw) sensors; the docs say to map raw, uncorrected sensors, since
  a sensor corrected by the offset would feed it back. Expected: about 14 W on the
  maintainer's CT meter, near 0 W for an accurate inverter meter.

**Converting 2.1 mappings** (Repairs issue `convert_own_sensors_<entry>`, fixable, raised at
setup while 2.1 mappings with an existing channel remain):
- The fix flow proposes a grid meter: the only general-channel mapping (else the only
  import mapping) as its import, and the only feed-in mapping as its export; with a
  choice, the user picks (or None). An export needs an import; the grid meter needs a
  name (default "Grid meter"). Every other mapping becomes an appliance with its stored
  name. With no import mapping the flow goes straight to the confirmation.
- `meters.async_convert`, under the run lock: each old chain's Store state moves to its
  new key (`storage.async_rename_chains`) and is read back **before** any sub-entry
  changes (on a mismatch nothing else changes); then the old sub-entries are removed
  (their devices and entities with them), the new ones added, and the entry reloaded once.
  Reloads triggered by the individual sub-entry changes are suppressed
  (`AmberRuntimeData.converting`): Home Assistant runs the update listener eagerly, and an
  intermediate setup would otherwise see neither the old nor the new mapping.
- A grid meter's reconciliation sensors ask for the entity IDs of the mappings they
  replace (`entity_ids` in the sub-entry data), so dashboards keep working.
- Statistics are untouched (same IDs). The next run continues each chain after its last
  written day.
- **Setup forgets a chain only when its sub-entry is gone** (not merely when its key is
  not currently known), so no intermediate state can drop a moved chain.
- Unconverted 2.1 mappings keep their reconciliation sensors; the user may ignore the
  issue.

## 22. Site-wide sources, Today so far, and solar-aware cost (Milestone 19; cost: M20)

Sources and Today so far are built (2.2.0-dev3, `sources.py`, `today.py`); solar-aware
appliance cost is built without the battery's cost basis (2.2.0-dev4, `solar.py`; the
cost basis is planned for 2.3). The settings belong to the site sub-entry's options
(section 23), set in the site settings' **Sources** step; a change reloads the entry.

**Site-wide sources** (`sources.resolve`, at setup):
- **Live price** for the site's general and feed-in channels (`live_price_general`,
  `live_price_feed_in`; a channel the site lacks has no field). For display and live
  estimates only; billed figures always come from Amber's usage data. A key absent from
  the options means auto-detect: Amber Express (`amber_express`, unique ID
  `<site>_general_price` / `<site>_feed_in_price`) first, then the core integration
  (`amberelectric`, `<site>-current-general` / `<site>-current-feed_in`), matched by this
  site's Amber site ID (case-insensitively); disabled entities are ignored. A key set to
  None means none. **Found after setup** (2.2.0-dev4): while an auto-detected price is
  missing and the site has a suitable grid meter, detection runs again once Home Assistant
  has started, on every entity registry create or update of an Amber Express or Amber
  Electric entity, and every minute for 15 minutes after the start; a price found rebuilds
  Today so far in place (its Store carries the day over) and adds its sensors, with no
  reload. Until then Today so far's sensors are left in the entity registry. The step suggests what was found and shows it. Both integrations give
  $/kWh with the feed-in price positive when paid; accepted units are $/kWh, AUD/kWh,
  c/kWh, ¢/kWh, $/MWh and AUD/MWh (converted to $/kWh); the step rejects other units
  (`price_unit`).
- **Solar production:** an optional power sensor (`solar_power`, device class power)
  and/or a cumulative energy sensor (`solar_energy`).
- **Battery:** optional cumulative charge and discharge energy sensors
  (`battery_charge`, `battery_discharge`). Stored only; used from M20.
- **Today so far's grid meter** (`today_grid_meter`): asked only when the site has
  several grid meters with the import on a general channel; else the first is used.
- No sensor may be used twice in the step (`same_sensor`).

**Today so far** (`today.TodayTracker`, one per site, when the site has a grid meter with
a general-channel import and a general live price):
- Sides: import (the meter's import sensor, general price) and export (the meter's export
  sensor and the feed-in price, when both exist).
- Every 5 minutes on the UTC 5-minute boundaries, each side's energy since the previous
  boundary (the mapped sensor's state, converted to kWh) is priced at the price in effect
  at the interval's end: the last price state before the boundary (price states are
  sampled by a state listener; a price that arrives just after the boundary belongs to the
  next interval).
- Days are NEM days (the interval's start decides). At the first boundary of a new day
  the previous day's totals go into a 14-day history; readings carry over only when the
  previous interval was the last of that day.
- No reading: nothing that interval (the energy counts at the next reading). No usable
  price: the energy is counted as `unpriced_kwh`. A reading below the previous one (a
  meter reset) drops the interval. After a restart the same day, the energy used while
  down is priced at the first interval's price; across midnight the readings start
  afresh.
- State in its own Store (`amber_energy_dashboard.<site key>_today`, saved with a 30 s
  delay and at shutdown), removed with the site.
- Sensors on the site device (display only, no state class, never statistics):
  **Today's cost so far (estimated)** (`today_cost`) and **Today's feed-in so far
  (estimated)** (`today_feed_in`), entity IDs `sensor.<prefix>_today_cost_estimated` and
  `…_today_feed_in_estimated` (prefix `amber_energy_dashboard` on the first site, the
  device name's slug on others). Attributes: `date`, `energy_kwh`, `unpriced_kwh`,
  `intervals`, `expected_intervals`, `last_interval_end`, `meter`, `energy_entity`,
  `price_entity`, `channel`, and `yesterday_estimate_vs_billed`: the latest history day
  before today whose billed record is imported, with estimated and billed amount and kWh,
  the error (estimate minus billed), its percentage, and `complete` (all 288 intervals
  priced).
- The mapped sensors are used as they are (raw, as section 21 requires); the calibration
  offset is not applied, so the estimate is expected to differ from the bill by about the
  offset × hours.
- **Offset-corrected value** (2.2.0-dev4, for comparison while the M19 decision is
  open): each side also accumulates its energy and amount with the grid meter's current
  calibration offset applied (`today.offset_corrected`: offset × the time since the
  previous boundary, added to the import when the interval's import is at least its
  export, else taken from the export; a side driven below zero passes the rest to the
  other, so the net changes by exactly the correction). Attributes `offset_w`,
  `offset_corrected`, `offset_corrected_kwh`, `offset_corrected_intervals`; the
  comparison adds `estimated_offset_corrected` and `error_offset_corrected`.
- **Bill to date** gets `including_today_estimated`: bill to date + today's cost − today's
  feed-in + one day of fixed charges (incl. GST), when the bill's data runs through
  yesterday (or today is the cycle's first day); otherwise None.
  `including_today_leaves_out` lists what the estimate can't see: "controlled load" (the
  site has one) and "feed-in" (a feed-in channel but no export side).

**Solar-aware appliance cost** (opt-in per appliance; the M17 cost statistic stays the
billed-rate figure):
- For each 5-minute interval, the house's load is supplied by solar first, then battery
  discharge, then the grid. An appliance's share of each source is its share of the load.
- **Solar** is valued at the interval's **feed-in price** (the export income forgone).
- **Grid** is valued at the interval's billed import price, as now.
- **Battery discharge** is its own source, valued at a **cost basis**: the average cost of
  the energy charged into the battery, tracked as a running pool. Energy charged from solar
  enters at that interval's feed-in price; energy charged from the grid at its import price.
  Discharge takes the pool's average cost. The pool's state (kWh and AUD) is kept in the
  Store per site, so it survives restarts and is rebuilt by a rewrite.
- Delivery order: the solar-only version first (battery discharge treated as grid), then
  the battery cost basis as a follow-up, if that keeps M20 small.

**As built (M20, 2.2.0-dev4):**
- **Option:** `solar_aware` in the appliance sub-entry's data, set in the appliance flow
  or the appliance's new reconfigure step. Checks (`solar.solar_aware_problem`): a
  general channel (`solar_aware_channel`), a solar sensor in Sources
  (`solar_aware_no_solar`), a grid meter with import and export
  (`solar_aware_no_grid_meter`). Toggling it reloads the entry.
- **Chain and statistic:** a second chain per appliance, `own:<sub>:solar_aware` (an
  `OwnSensor` with role `solar_aware`), writing `amber_energy_dashboard:{site}_own_{slug}_cost_solar_aware`
  ("<name> cost (solar-aware)", AUD). Its own marker, guard, adoption, revisions and
  rewrites, as for any secondary chain; it starts like the appliance's chain (the later
  of retention and the sensor's first statistics). Turned off, the chain's state and the
  statistic are kept and the sensor is removed. Without the site's flows at run time the
  chain waits (nothing is skipped).
- **Site flows** (`solar.site_flows`, at setup): the grid meter (Today so far's when it
  has an export, else the first general-channel meter with one), the solar energy sensor
  (else the solar power sensor's mean statistics × the bucket's length), and the battery
  sensors when both are set.
- **Split per interval** from the 5-minute statistics (sum changes): load L = I + S + D −
  E − C (at least 0); Ls = min(S, L), Lb = min(D, L − Ls), Lg = L − Ls − Lb; the
  appliance takes the same shares; with L = 0 its energy counts as grid. Netting within
  an interval and the house-average share are the documented limits.
- **Valuation:** solar share × the feed-in record's −perKwh, the rest (grid and, until
  2.3, battery) × the general record's perKwh, per interval (30-minute records price each
  of their buckets). With the allowance active, the feed-in value in the penalty period
  gets `penalty_rate × fraction(hour)` (`export.refund_fractions`: 1 while the period's
  window export so far, the hour included, is within the allowance, the part within it
  in the hour that passes it, 0 after); a day without export aggregates is valued
  unadjusted (`allowance_adjusted` false). Export aggregates are now measured before the
  chains in a run, so the day just imported is adjusted.
- **Lower precision:** when any series (appliance, grid, solar, battery) lacks 5-minute
  data, hourly shares and the hour's mean prices, flagged; with the fallback off, skipped
  (`skipped_no_short_term`). A series without hourly data skips the day
  (`skipped_sensor_data`, naming the sensor).
- **Per day** in the chain record: `amount`, `energy_kwh`, `solar_kwh`, `battery_kwh`,
  `grid_kwh`, `lower_precision`, `allowance_adjusted`. Sensor **Yesterday solar-aware
  cost** on the appliance's device, with these and `solar_share` as attributes.
- **Battery:** the data path is built (the charge and discharge series, the battery
  share); its cost basis (the running pool, section 22 above) moves to 2.3. Until then the
  import-price valuation of battery discharge is a documented limitation.
- **Waiting chain issue** (2.2.0b3): a solar-aware chain that waits for the site's flows
  with more than 2 days unwritten (from its first unwritten day to yesterday) raises the
  Repairs issue `solar_aware_waiting_<site key>_own_<sub>_solar_aware` (warning), naming
  the appliance, what is missing (`solar.flows_missing`: a solar sensor in Sources, a grid
  meter with import and export) and the first waiting day. It clears at the next run that
  has the flows. Unconverted 2.1 own-sensor mappings get no solar-aware cost (M20 review).

**Compatibility kept by M17:** appliances and grid meters are separate sub-entries with
their own chains and devices; an appliance's data can gain a cost-basis option without
changing its statistic; grid meters and appliances record `site_id`; the site-wide sources
belong to the site (entry options today, a site sub-entry later).

## 23. The account entry and site sub-entries (Milestone 18)

Decided at the M17 review (M17 report, section 5, point 1): the config entry is the Amber
account; each site is a sub-entry. Existing per-site entries are never merged.

**Data model.**
- Entry (version 2.1): `data = {api_key}`; `options = {}` (account-wide settings; none
  yet). Its unique ID stays its first site's ID.
- Site sub-entry (`site`): `data = {site_id, nmi, site_name, channels, add_to_energy,
  options}`, where `options` are the site's settings (schedule, usage mode, patience,
  revisions, price series, own fallback, bill estimate, export allowance). Title: the site
  name, else "Site …1234". Unique ID: the Amber site ID (a site can be in one entry only).
- Grid meters, appliances and 2.1 own-sensor mappings stay sub-entries, linked to their
  site by `site_id` (a 2.1 mapping without one belongs to the entry's first site).
- **Site key** `<entry ID>_<site ID>`: the site's Store (`amber_energy_dashboard.<site key>`)
  and its Repairs issue IDs (`<kind>_<site key>`). The entry's first site keeps the
  entry's schedule offset (seeded from the entry ID); a further site's is seeded from its
  key.

**Runtime.** One manager per site (`SiteRuntime`), in `AmberRuntimeData.sites` by
sub-entry ID. An account's sites share one `AmberClient` and one run lock, so their runs
take turns; each run starts from the rate-limit state (`RateLimit-Remaining`, window end)
the account's previous run saw, so the reserve applies before its first call (one budget
for the account, as Amber's limit is). Setup fetches `/sites` once. A configured site the
key no longer sees (for example a closed site) is skipped (`AmberRuntimeData.skipped`),
with a Repairs issue `site_not_found_<site key>` naming it, and the account's other sites
keep running (2.2.0-dev3); the issue clears at the next setup that sees the site, or when
the site is deleted (its Stores go with it). Only a key that sees none of the configured
sites fails setup with reauth.

**Devices and entities.** Each site's device is created in its sub-entry
(`config_subentry_id`), and its sensors and buttons are added in it. The first site keeps
the 2.1.0 entity IDs (`<platform>.amber_energy_dashboard_<name>`); a further site's follow
its device's name. Mapping devices link via their site's device. Every device belongs to a
sub-entry, so Home Assistant no longer shows "Devices that don't belong to a sub-entry".

**Flows.**
- Config flow: key, first site, site name, channels, schedule (and "Add to the Energy
  dashboard"); creates the entry with its site sub-entry. A site already set up in any
  entry is refused.
- "+ Site" (`site` sub-entry flow): the active sites the key sees that aren't set up
  anywhere, then a name and the channels; added with automatic scheduling.
- Site settings: the entry's options (the cog; asks which site when there are several)
  and the site's reconfigure flow (the pencil) share one set of steps
  (`_SiteSettings`): import settings, **Sources** (section 22), bill estimate, export
  allowance, **Rename site** (2.2.0-dev3: the sub-entry's title, `site_name` and the
  device's name, clearing a `name_by_user`; entity IDs unchanged), **Refresh channels**
  (replaces the entry's reconfigure), the YAML-kit migration and its undo. Saving updates
  the site sub-entry; the update listener applies it in place, or reloads when sites,
  mappings or channels change, or the bill estimate or allowance is turned on or off.
- Grid meter and appliance flows ask which site first when there are several.
- Reauth: the new key must see at least one configured site (2.2.0-dev3; any it doesn't
  see is skipped, as above).

**Services, Repairs, diagnostics.** Every service takes `site` (the site's device, its
Amber site ID or its sub-entry ID); `config_entry_id` alone works while the entry has one
site, and either may be left out while only one site is loaded. Repairs issues carry
`{entry_id, site_id}` and name the site ("<entry title> (<site>)" with several sites); the
fix flows find their site from it (the entry's first site without `site_id`). Diagnostics
list each site under `sites`, by sub-entry; site names, NMIs and titles are redacted.

**Migration** (`sites.async_migrate_to_sites`, from `async_migrate_entry`, 1.x to 2.1,
before any platform is set up):
1. Create the site sub-entry, with a sub-entry ID derived from the site ID
   (`site_<site id>`), so a re-run finds it; data and the entry's options move into it.
2. Give every mapping its `site_id`.
3. Move the site's device (`async_update_device(new_config_subentry_id=...)`) and every
   site entity (on the device, or a site entity without a device) into the site sub-entry.
4. Move the Store from the entry's key to the site's (`storage.async_move_store`: copied,
   read back, then the old one removed; a Store already at the new key wins).
5. Remove the entry's old per-entry Repairs issues (the checks raise any still due).
6. Leave the entry with only the API key, version 2.1; read every step back
   (`SiteMigrationError` otherwise: the migration fails, is logged, and runs again at the
   next start).

Entity IDs, unique IDs and statistic IDs do not change. Home Assistant saves the device and
entity registries about 10 s after a change, later than the config entries: a kill in
between leaves a migrated entry with the device and entities not yet moved. So step 3 also
runs at **every** setup (`async_ensure_site_ownership`), before the platforms: no platform
ever adds an entity whose sub-entry differs from its device's (the 2.0.x sensor-drop
cause).
