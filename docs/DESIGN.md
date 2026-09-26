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
  boundary. Whether and how the boundary rolls forward is still being observed, so it
  is discovered and re-verified at runtime (section 8).
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

**Retention boundary.** A learned `retention_days`, stored in Store.
- **At setup**, discover it by bisection with single-day usage requests: at most 8
  calls. If discovery cannot finish (budget, errors), fall back to 89 days.
- **Daily**, re-verify with 2 calls: the boundary day (expected to have data) and the
  day before (expected empty). Self-correct in either direction: if the boundary day
  is empty, the boundary moved forward; if the day before has data, it moved back.
- **When the daily check detects a move (M4):** if the move is one day, step one day
  (confirmed with one more probe). If it moved further, bisect within a **15-day
  bracket** beyond the step probe, so the whole check stays within 8 calls (2 checks +
  1 step + 1 bracket + 4 bisection). A move too large for the bracket is *unresolved*:
  the old value is kept, and full discovery (30-day bracket, 8 calls) runs on the next
  day (accepted in M4).
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

Usage modes (options flow):
- **Full:** scheduled import of all usage statistics (sections 6 to 10).
- **Recovery-only:** no schedule. The `amber_energy_dashboard.backfill` service imports a given date
  range on demand, for users whose own metering had an outage.
- **Pricing-only:** no usage statistics. Provides prices and/or own-sensor cost only.

Own-sensor cost (available in any mode):
- The user selects one or more energy sensors (`device_class: energy`, cumulative) and maps
  each to a price channel (general, controlled load or feed-in).
- For each Amber interval, cost = the sensor's energy delta over that interval (from HA
  **short-term 5-minute statistics**) times Amber's confirmed `perKwh` for that interval.
  Because Amber's own `cost` is exactly `kwh × perKwh`, own-sensor cost is directly
  comparable with Amber's figures.
  Summed hourly into `amber_energy_dashboard:{site}_own_{slug}_cost`.
- Short-term statistics are purged after roughly 10 days by default. Amber's day-late data
  arrives well within that window. For older periods (a backfill), the default is to use
  hourly statistics times the hourly mean price and flag the day as lower precision. An
  option skips such days instead.
- **Reconciliation:** when a whole-house sensor is mapped to general, a display sensor shows
  the daily difference between the user's own metered energy and Amber's metered energy.
  This catches CT calibration drift and dropped sensors.
- Price history depth (section 4, item 2) bounds how far back own-sensor cost can go.

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
series, own-sensor mappings, lower-precision fallback behaviour, and (from Milestone 6)
"Migrate from the v1 kit".

**Services:**
- `backfill(start_date, end_date)`
- `rebuild_from_anchor(anchor_date, anchors)`: port of the YAML `amber_window_rebuild`,
  keeping its strict-stop semantics.
- `probe_retention()`
- `migrate_v1(dry_run)` (Milestone 6)

## 13. Error handling

- Timeouts on every request. 401/403 starts reauth (an invalid key returns 403). 429
  triggers backoff. A run stopped by the rate-limit reserve (section 9) resumes at the
  next attempt. 5xx and network errors retry at the next scheduled attempt.
- Empty or ambiguous inner results are always treated as **errors**, never as success
  (lesson from the YAML rebuild bug).
- Every stop is recorded in the Store with a reason, and exposed via the status sensor
  and diagnostics.

## 14. Migration and cleanup (Milestone 6)

- **Detects** the published v1 kit's known entities, helpers, scripts, automations and
  statistic IDs.
- **Removes automatically**, with confirmation: UI-created helpers, and automations and
  scripts that have an `id` (via HA's config API).
- **Never edits** `configuration.yaml`. YAML-defined blocks (`rest:`, templates,
  `rest_command`, `recorder: exclude`) are listed in a Repairs issue with exact
  file-and-block instructions.
- **Old statistics**, one of: keep frozen; delete; or (recommended) copy the history into
  the new external IDs so the dashboard shows one continuous series, then retire the old IDs.
- **Compensation sign.** The v1 kit's compensation statistic carries Amber's sign: feed-in
  earnings are **negative**. As a result, the Energy dashboard adds feed-in earnings to
  cost instead of subtracting them, so v1's net cost is wrong. Confirmed against
  production for 2026-09-23 to 25. The new statistic is positive-when-earned (section 6).
  - When copying history, the migration must **negate** the legacy compensation values:
    both each hourly `state` and the cumulative `sum`.
  - The parity check must compare compensation **by magnitude** (`|v1| == |v2|`).
    Energy and import cost are still compared exactly.
- **Order:** backup check, dry-run report, parity verification over the overlap window
  (exact-match test, as in the YAML rebuild, with compensation compared by magnitude),
  switch Energy dashboard sources, disable old automations, remove.
- Robert's own dev leftovers (`_v2` names, debug scripts, backup files) are a one-off
  cleanup task, not part of the public tool.

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
6. **Migration and release:** v1 migration and cleanup, README with credits, HACS release.

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
