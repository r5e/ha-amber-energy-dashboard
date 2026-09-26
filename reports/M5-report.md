# Milestone 5 report: usage modes, own-sensor cost, reconciliation, price series

Date: 2026-09-27, 07:54 to about 09:50 AEST. Branch `v2`.

**Summary.** Built DESIGN section 11 as agreed:
- three usage modes (Full, Recovery-only and Pricing-only) and the
  `amber_energy_dashboard.backfill` service;
- own-sensor cost as **config sub-entries**, with a precise path from 5-minute data and
  an hourly fallback flagged `lower_precision`;
- a reconciliation display sensor;
- an optional hourly price series.

Each own sensor, and the price series, is a *secondary chain* with its own marker, guard
and resumable rewrite.

**314 tests pass, with 100 % coverage** and synthetic data only.

**Lab (VM 9104).** Phase 1 ran against the real Amber API:
- the fallback matched a hand-computed day exactly;
- reconciliation showed **+2.0 %** for the ×1.02 synthetic sensor;
- the **Energy dashboard accepts the own-cost statistic as `stat_cost`**, with no
  validation issues;
- the price series matched the raw data;
- every mode switch and backfill path behaved as designed.

Phase 2 is set up and running, with a real-time sensor mapped. The checks for tomorrow
are in section 8.

**Retention re-probe (standing decision B): the boundary did not roll.**

**VM 9102 was not touched.**

## 1. What was built

Commits since M4 (`404edcf`):
- `8baded8`: Milestone 5 code and tests;
- `dd82b4e`: M5 decisions recorded in DESIGN section 11, and the README sections on
  modes, own-sensor cost and the price series;
- `ee5b214`: own sensors start on the day the sensor's history starts (partial first
  day);
- this report.

Diffstat: 20 files, +2770 / −138 lines. Manifest version `2.0.0-dev4`.

**`chains.py` (new).** Per-day computations:
- `price_rows()`: hourly mean, min and max of `perKwh`/100.
- `async_sensor_energy()`: returns 288 five-minute and 24 hourly energy deltas in kWh.
  Each delta is the change of the statistic's `sum`, so meter resets are handled. Each
  list is `None` when its data is incomplete. On the sensor's first day, buckets before
  its first statistic count as zero.
- `own_cost_day()`: prices one day.
  - Precise path: for each Amber interval, Σ 5-minute energy × `perKwh`.
  - Fallback: hourly energy × the hour's mean `perKwh`.
  - It raises `SensorDataUnavailable` or `PreciseDataUnavailable`.
  - The sign is flipped for feed-in, so money earned is positive.
- Amber's `hh:mm:01` start times are floored to the minute.

**`storage.py`.**
- `ChainState`: marker, `last_written`, pending, rewrite progress, per-day records, and
  an `adopt` for a mapping that is re-added.
- `chain_state()` and `async_forget_chain()`, which saves.
- New skipped statuses: `skipped_not_requested`, `skipped_sensor_data` and
  `skipped_no_short_term`.

**`statistics.py`.**
- `MeanSpec` / `price_specs()`: AUD/kWh, `mean_type` arithmetic, `unit_class` None.
- `own_cost_spec()` and `sensor_slug()`: `{site}_own_{slug}_cost`, AUD, sum.

**`importer.py`.**
- `async_write_amounts()` and `async_write_means()`.
- Verification and row reads can now be limited to particular statistic keys and types.

**`manager.py`.**
- Mode-aware run steps:
  - usage Guard 1 (skipped in Pricing mode);
  - tail resume, retention and revisions;
  - then a backfill, the walk (Full mode only) or nothing;
  - then the secondary chains.
- `_async_range_rewrite()` generalises the M4 tail rewrite with stored `requested_end`
  and `on_empty`. It serves backfill, revisions and resume.
- The backfill implementation: fill before existing data, append after it, and record
  `skipped_not_requested` for the days in between.
- Per chain: a guard (`marker_mismatch` issue with a per-chain suffix), start, walk,
  first day, empty day, write and rewrite.
- The chains share the run's record cache, retention and patience.
- Recovery mode has no timer (`next_run` None).
- The reconciliation snapshot.

**`__init__.py`.**
- Settings are read from the options, and own sensors from the sub-entries.
- The `backfill` service: refused in Pricing mode and for an inverted range. Guard
  refusals map to `ServiceValidationError`.
- A change to the sub-entry set reloads the entry; any other options change is applied
  live.
- State of removed sub-entries is forgotten.

**`config_flow.py`.**
- New options: `usage_mode` (select), `price_series` and `own_fallback`.
- Sub-entry flow `own_sensor`: pick an entity and a channel.
  - The entity must have `device_class` energy and `state_class` total or
    total_increasing.
  - Errors are `sensor_not_found` and `not_cumulative_energy`; a duplicate aborts with
    `already_configured` (the unique ID is the entity ID).

**Other files.**
- `sensor.py`: one reconciliation sensor per general-mapped own sensor, attached to its
  sub-entry. Its unit is kWh, with no `device_class` or `state_class`. Attributes: `date`,
  `difference_percent`, `own_kwh` and `amber_kwh`.
- `services.yaml`, `strings.json` / `translations/en.json` and `icons.json` are updated
  for the new options, the sub-entry flow, the reconciliation entity and the service.
- `docs/DESIGN.md` section 11 records the decisions, including sub-entries, chains and
  the partial first day.
- `README.md` gains three sections:
  - "Usage modes";
  - "Own-sensor cost (for example CT clamps)", including the Energy dashboard setup;
  - "Price series (optional)", including why an hourly mean price isn't a cost rate.

## 2. Test results

`uv run pytest`: **314 passed, 0 skipped, 0 failed** (144 s). Coverage is **100 %** of
2254 statements. ruff check and format are clean.

New tests are in `tests/test_own.py` (32 tests), all with synthetic fixtures. They cover:
- **Modes:**
  - Pricing writes no usage statistics but still prices chains;
  - Recovery has no timer and `run_now` does no walk;
  - switching modes never deletes statistics and resumes from the markers;
  - backfill is refused in Pricing mode.
- **Backfill:**
  - older than retention gives `skipped_unavailable`;
  - a range overlapping existing data triggers a tail rewrite;
  - a range after existing data is appended, with the gap recorded as
    `skipped_not_requested`;
  - filling that gap later rewrites the tail;
  - an empty day follows patience;
  - an interrupted backfill resumes;
  - an inverted range and a forward range in Full mode are refused.
- **Own-sensor cost:**
  - precise path against an independent computation, including a meter reset;
  - fallback with `lower_precision`;
  - fallback off gives `skipped_no_short_term`;
  - no hourly data gives `skipped_sensor_data`;
  - feed-in sign;
  - a partial first day;
  - backfill from the later of history start and boundary;
  - the chain's Guard 1 mismatch raises a suffixed issue;
  - adopt after re-adding a mapping;
  - removing a sub-entry forgets its state and keeps the statistic;
  - a revision rewrites the chains;
  - a failed day keeps the walk's partial report.
- **Sub-entry flow:** create, duplicate, missing entity, and wrong device or state class.
- **Reconciliation:** value and attributes, and unknown outside Full mode.
- **Price series:** mean, min and max, metadata, and starting at the boundary.

Mutation check: shifting interval alignment by one 5-minute bucket fails 3 tests.

Existing tests: `test_manager._setup_entry` gained `subentries=`. One window expectation
in `test_recovery.py` changed to `(days[0], TODAY)`, because the fetch window now always
extends to today so the chains share one fetch. `test_config_flow.py` gained the new
option defaults.

## 3. Lab verification

Clone **VM 9104** `amber-test-m5` was linked from template 9000 (HAOS 18.3, HA 2026.9.3).
The integration was installed from the working tree over SSH. Schedule: fixed times
07:45, 10:45 and 13:45. All values below are read back from the recorder over the
websocket API, or from the diagnostics.

**Synthetic sensors** (template sensors on the clone):
- **A**, `sensor.lab_house_energy`: its state is constant. It received imported hourly
  statistics for 09-22 to 09-26 equal to Amber E9 hourly kWh × 1.02 (121 rows, final sum
  108.853).
- **B**, `sensor.lab_meter_energy`: follows `input_number.lab_counter`. The automation
  `lab_meter_tick` adds 0.01 kWh every minute, starting at 08:47 AEST on 09-27.

**P1: first setup.**
- 90 days imported in 21 calls.
- Retention 90 days by bisection (8 calls): 06-29 has data, 06-28 is empty.
- 2160 hourly rows per statistic, no duplicates or discontinuities, E9 final sum
  1333.435.

**P3: own sensor A → E9 (sub-entry created over REST), hourly fallback.**
- `run_now` wrote chain `own:<A>` for 09-22 to 09-26, all `lower_precision` (A has no
  5-minute data).
- **Hand check of 09-24**, where each hour = A hourly kWh × mean E9 `perKwh` that hour:
  24 hours compared, **max |HA − hand| = 0.0**. Day total 5.583342 vs 5.583343 (the
  hand total is rounded once; HA rounds each hour).
- **Reconciliation** (09-26): state **0.621 kWh**, `difference_percent` **2.0**,
  `own_kwh` 31.667, `amber_kwh` 31.046. This is exactly the ×1.02 built into A.

**P4: Energy dashboard.**
- `energy/save_prefs` with a grid source: `stat_energy_from` = sensor A and
  **`stat_cost` = the own-cost statistic**. It succeeded.
- `energy/get_prefs` reads back that `stat_cost`.
- `energy/validate` returned `{"energy_sources": [[]], ...}`, meaning **no issues**.
- The CT-clamp use case works and is documented in the README.

**P5: price series enabled.**
- 90 days written (06-29 to 09-26) in 13 calls.
- 09-24 18:00 NEM, HA mean/min/max **0.336348 / 0.315391 / 0.353114**, identical to the
  raw records.
- Metadata: unit AUD/kWh, `mean_type` 1 (arithmetic), `has_sum` False.

**P6: modes and backfill.**
- **Pricing-only:** `run_now` made 0 calls and wrote no usage. The usage statistics
  were unchanged and present (2160 rows).
- **Recovery-only:**
  - `next_run` is None, and `run_now` does no usage walk.
  - Backfill 06-01 to 06-05 (older than retention): `skipped_unavailable`, **0 calls**.
  - Backfill 09-20 to 09-21 over existing data: tail rewrite to 09-26 in 1 call. The
    statistics were **identical** afterwards, still 2160 rows and final sum 1333.435.
- **Fresh ranges:** I cleared the usage statistics and reset the usage Store (staged
  edit, then an HA restart). Then, still in Recovery mode:
  - Backfill 09-20 to 09-21: imported in 1 call.
  - Backfill 09-24 to 09-25: imported in 1 call. 09-22 and 09-23 were recorded as
    `skipped_not_requested`.
  - Backfill 09-22 to 09-23: imported, with the tail rewritten to 09-25, in 1 call.
  - Result: **144 hourly rows**, no problems. E9 final sum **106.12**, equal to the raw
    09-20 to 09-25 total of 106.12.
- **Back to Full:** `run_now` resumed from the marker and imported 09-26 in 1 call. That
  gave 168 rows, final sum 137.166, no problems.

**Phase 2 setup** (09:42 AEST):
- Sub-entry B → E9 created: "Lab meter energy (general E9)".
- B had 11 five-minute rows so far on 09-27, the first at 22:45Z and the latest sum
  0.52.
- Chains present: `own:<A>`, `price` and `own:<B>`.
- The next scheduled run was 10:45 AEST. 09-27 cannot be priced until Amber publishes it
  on 09-28.

## 4. Open questions assigned to this milestone

- **Config sub-entries supported?** Yes, in HA 2026.9, and they are used. Each mapping is
  a sub-entry with its own add and delete in the UI, a unique ID (the entity ID), and the
  reconciliation entity attached to it.
- **Energy dashboard accepts the own-cost statistic as `stat_cost`?** **Yes.** The
  evidence is in P4 above; `validate` reports no issues.
- **Standing decision B: retention re-probe.**
  - Done at 07:54 AEST on 2026-09-27 (2 calls): 2026-06-28 returned `[]` (HTTP 200), and
    2026-06-29 returned 576 records.
  - **The boundary did not roll** overnight: the earliest day is still 06-29.
  - Retention is now 90 days, which lab discovery confirmed independently at 09:31
    (bisection).
  - 06-30 was not needed.

## 5. Deviations from DESIGN.md and proposed changes

None against the agreed decisions. Details settled during implementation are recorded in
DESIGN section 11, and are listed here for confirmation:
1. **Own-cost statistic ID** is `{site}_own_{slug}_cost` for every channel, including
   feed-in. The slug comes from the entity ID. For feed-in, the name says "compensation"
   and the value is positive when earned.
2. **History start** is the hour after the sensor's first hourly statistic, because the
   first row is only a starting reading. The first, possibly partial, day counts earlier
   hours as zero energy.
3. **Secondary chains** each have their own marker and Guard 1. A revision rewrites
   every chain from the earliest changed day.
4. **Backfill.**
   - In Full mode it only fills or rewrites up to the last imported day; later days
     belong to the schedule.
   - It is refused in Pricing mode, where there is no usage to write.
   - A range after existing data leaves `skipped_not_requested` days, which a later
     backfill can fill.
5. **`run_now` in Recovery mode** extends only the secondary chains.
6. **Fetch window.** A run now always fetches up to today, so the chains share the fetch.
   This cost no extra calls in the lab.

**Proposals for the planning chat:**
- **Reconciliation entity name.** It is "Reconciliation `<entity_id>`" (for example
  "Reconciliation sensor.lab_house_energy"). That is unambiguous but not pretty.
  Alternative: use the source sensor's friendly name, which can change.
- **Price series call cost.** Enabling it on an existing install fetches the whole
  retention window once: 13 calls for 90 days in the lab. That fits the run budget, but
  it's worth mentioning in the README option help. It's currently in the README text only
  implicitly.

## 6. Live Amber API calls

**49 calls today (2026-09-27 AEST)**, all outside the quiet windows. The Amber API
reported a lowest `RateLimit-Remaining` of 18. Times are AEST.

| Time | Calls | What |
|---|---|---|
| 07:54 | 2 | retention re-probe (06-28, 06-29) |
| 09:31 | 2 | config flow and entry setup (`/sites`) |
| 09:32 | 21 | first-setup import, 90 days, including 8 bisection probes |
| 09:32 | 1 | independent raw fetch 09-22 to 09-26 (for sensor A and the hand check) |
| 09:32 | 1 | entry reload after sub-entry A (`/sites`) |
| 09:37 | 1 | P3 own-chain backfill run |
| 09:39 | 13 | P5 price series, 90 days |
| 09:39 | 1 | P6 overlapping backfill |
| 09:40 | 1 | setup after the staged restart (`/sites`) |
| 09:41 | 3 | three fresh backfills |
| 09:41 | 1 | independent raw fetch 09-20 to 09-21 |
| 09:41 | 1 | P6d `run_now` back in Full mode |
| 09:42 | 1 | entry reload after sub-entry B (`/sites`) |

**Between 09:30 and 09:31 the lab made no calls** (the quiet window ended at 09:30).

**Clone 9104 keeps running with the test key.** Its scheduled runs make a few calls
each, at 10:45 and 13:45 today and 07:45 tomorrow. None of these times is in a quiet
window. Some lab log labels still say "M4 lab" (a reused helper), but they are counted
here.

## 7. Lab state left behind

- **VM 9104 `amber-test-m5` is running** at `http://192.168.94.111/`, left on purpose
  for Phase 2.
  - It holds entry options `usage_mode` full and `price_series` on.
  - It has own sensors A and B mapped to E9, and the `lab_meter_tick` automation.
  - Destroy it after the Phase 2 check.
- **VM 9102 was not touched** (no API calls, no SSH).
- Two clones exist in total (9102 and 9104), within the limit.
- VM 101 was not accessed.
- No working files are on any HA instance, apart from what is inside 9104.
- Repository: the M5 commits and this report are pushed to `origin/v2`; see the commit
  log.

## 8. Recommended next steps

**Phase 2 check, tomorrow (2026-09-28).** Do this after the **07:45 AEST scheduled run**
on 09-28, or after a `run_now` any time after 07:00 outside the quiet windows. If Amber
hasn't published 09-27 by 07:45, the 10:45 run picks it up. Run
`lab_m5.py phase2check` (scratchpad) and confirm:
1. `last_run` shows 09-27 imported, and chain `own:<B>` has day **2026-09-27**
   `imported` with **`lower_precision` False** (the precise 5-minute path).
2. Chain `energy_kwh` ≈ the counter's rise over the day: about **9.1 kWh** (0.01 kWh per
   minute from 08:47 to 24:00 AEST, less any minutes HA was down). It must equal the
   independent Σ of 5-minute deltas.
3. For each of the 24 hours, `sensor.lab_meter_energy`'s own-cost `state` equals the
   independent value from 5-minute delta × that interval's E9 `perKwh`, within 1e-6.
   Hours before 08:45 are 0.
4. The day totals match, and the statistic's sums continue without a break.
5. Also check that sensor A's chain was extended to 09-27 as `skipped_sensor_data` (A has
   no statistics that day), that the price series extended to 09-27, and that there are
   no Repairs issues.

Then destroy VM 9104.

After that:
- Robert to take the section 5 proposals to the planning chat.
- VM 9102's in-place upgrade (separate prompt).
- M6 only after sign-off.
