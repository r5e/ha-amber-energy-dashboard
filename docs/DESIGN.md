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

**Reconciliation** (Full mode, for each sensor mapped to a general channel): a display
sensor (no `state_class`) for the last NEM day that both Amber and the sensor have. Its
state is own kWh − Amber general kWh; the percentage and both totals are attributes. It
catches CT calibration drift and dropped sensors. In other modes it is unknown. It is named
"Reconciliation: <source friendly name>", using the friendly name when the mapping was
created. That name is stored in the sub-entry and not re-derived later, so renaming the
source sensor does not rename it (mappings without a stored name use the entity ID).

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
5. Schedule (automatic or fixed times).

A **reauth flow** handles key rotation (triggered on 401/403).

**Options flow:** usage mode, schedule, revision window, patience days, optional price
series, own-sensor mappings, lower-precision fallback behaviour, and (from Milestone 6a)
"Migrate from the YAML kit" and "Undo migration" (section 14).

**Services:**
- `backfill(start_date, end_date)`
- `run_now()` and `import_day(date)`
- `probe_retention()`: a fresh retention discovery (section 8)
- `rebuild_from_anchor` was dropped at the M6b review: `backfill` with the guarded tail
  rewrite (section 8) covers the YAML `amber_window_rebuild` use.
- `migrate_v1(dry_run, confirm_backup, exclude_flagged, …)`, `undo_migration()` and
  `delete_legacy_statistics(confirm)` (Milestone 6a, section 14)

## 13. Error handling

- Timeouts on every request. 401/403 starts reauth (an invalid key returns 403). 429
  triggers backoff. A run stopped by the rate-limit reserve (section 9) resumes at the
  next attempt. 5xx and network errors retry at the next scheduled attempt.
- Empty or ambiguous inner results are always treated as **errors**, never as success
  (lesson from the YAML rebuild bug).
- Every stop is recorded in the Store with a reason, and exposed via the status sensor
  and diagnostics.

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
- At least 3 comparable days are required. On failure the migration stops with a clear
  report and changes nothing.

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
still exist (entities by state or registry entry, the rest_command service, the
`amber_api_key` key in secrets.yaml, the v1 backfill files in the configuration folder) and
clears itself once none remain. Undo removes it. Old statistics are kept by default and do
not keep the issue open; a separate, explicit service deletes them later (after which undo
is no longer possible).

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

Each milestone ends with a written report and Robert's sign-off before the next starts.

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
