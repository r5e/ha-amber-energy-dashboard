# Milestone 6b report: v1-kit migration test, final polish, release candidate

Date: 2026-09-28, 08:15 to about 11:30 AEST. Branch `v2`.

**Summary.**
1. **Retention is now kept as a boundary date.** The daily check is 2 probes, whatever the
   day count. The store upgrades itself from 1.2 to 1.3.
2. **The migration scans the legacy statistics for implausible rows.** The dry run lists
   them, a real run refuses by default, and `exclude_flagged` leaves them out and
   re-derives the sums around them.
3. **The published v1 kit was installed on a fresh clone (VM 9105)** exactly as its README
   says, with the test key. It built 180 days of real-shaped v1 history using its own
   backfill script and daily automation. The full migration cycle then passed on it:
   - dry run, real run and verification;
   - seam gap 0;
   - final sums **exactly** equal to the v1 totals;
   - Energy dashboard swap, undo, re-run, and the second-run refusal.

   The v1 README's 99999 spike (plus one inside the copy period) was detected, refused, and
   excluded correctly. **The provisional v1 cost rule is confirmed:** a real v1 install has
   no cost history. VM 9105 is destroyed.
4. **Release polish:**
   - the README rewritten for users;
   - a new CHANGELOG;
   - an original brand icon shipped in the integration, so the HACS brands check runs
     again;
   - manifest version `2.0.0-rc1`.

   CI (hassfest, HACS validation, tests) is green on the pushed commits.
5. **Tag `v2.0.0-rc1`** is on the commit that adds this report. The pre-release notes are
   in `reports/release-notes-v2.0.0-rc1.md`.

**371 tests pass, with 100 % coverage** (3213 statements). ruff is clean, and hassfest
reports 0 invalid.

## 1. What was built

Commits (since `bb7966b`):

| Commit | What |
|---|---|
| `a464df2` | Retention by boundary date; the daily re-verify is 2 probes |
| `fbeeced` | Migration: scan the legacy statistics for implausible rows |
| `c2c0422` | M6b decisions recorded in DESIGN sections 4, 8 and 14 |
| `0139fdc` | Release polish: README, CHANGELOG, brand icon, `2.0.0-rc1` |
| `b81da19` | v1 cost rule confirmed on a real v1 install; clearer note for a daily copy |
| (this commit) | This report and the release notes, tagged `v2.0.0-rc1` |

### Item 1: retention by boundary date (`storage.py`, `manager.py`)

- **Store (layout 1.3).**
  - `retention_boundary` (an ISO date) replaces `retention_days`.
  - `AmberStore.retention_days` is now a derived property: today − boundary, using the
    manager's clock. Diagnostics show it.
  - `async_set_retention(boundary, info)`.
- **Store migration 1.2 → 1.3** (`_VersionedStore._async_migrate_func`, helper
  `retention_boundary_from_days`):
  - the boundary date recorded with the last measurement is kept (M4 and later store
    `retention.boundary`);
  - otherwise the count is taken back from `measured_on`, or from today when that is
    missing too;
  - `retention_days` is dropped;
  - an existing `retention_boundary` is never overwritten.
- **`_async_verify_retention`** probes the **stored date** and the day before.
  - If both are as expected, the result is `verified`, however far the day count has
    grown.
  - The step, the 15-day bracket and bisection apply only when the probes disagree.
- **New `_rolling_candidate`.** A forward move beyond the bracket also tries today − (the
  day count at the last verification), with 2 probes (at most 6 calls in all). This keeps
  a *rolling* boundary resolvable after a long outage. Otherwise the result is
  `unresolved`, and full discovery runs the next day, as before.
- **The walk, backfill and chains** read `store.retention_boundary` directly. The setup
  fallback is today − 89, stored as a date.
- **Run summaries** now report `boundary`, `previous_boundary`, `retention_days` and
  `previous_days`.

### Item 2: implausible legacy rows (`migration.py`, `config_flow.py`, `__init__.py`)

- **`_scan_series(rows, energy)`.** A row's step is its sum minus the last good row's sum.
  - **Energy** is flagged for a step above **100 kWh** per row (times the days between the
    rows, when they are more than a day apart), or any decrease (more than 0.0005).
  - **Cost** is flagged for a step whose size is above **100**. Cost can fall legitimately
    with negative prices.
  - **One row of lookahead** names the kind:
    - **spike:** the next row is plausible again from the last good row. The row is
      dropped and nothing else moves.
    - **jump / reset:** the series continues from the new level. The row is dropped and
      every later sum is shifted by the step.
  - The first row is never flagged.
- **`_scan_legacy`** runs it on every chosen source. Each flag records its statistic, role,
  hour, sum, step, kind, the problem, and **where** it lies: `copy`, `overlap` or `after`.
- **Report.** The dry run has `flagged: {count, excluded, blocking, rows[:20]}`, and
  `format_report` lists them.
  - Without `exclude_flagged`, flagged rows are a plan problem, so the real run refuses
    and nothing changes.
  - With it, parity and the copy use the cleaned series, and a note says so.
  - The choice is stored with the run's `sources`, so a resumed run keeps it.
- **Service and flow.**
  - `migrate_v1` has a new field, `exclude_flagged` (in `services.yaml`, `strings.json`
    and `en.json`).
  - New options-flow step `migrate_flagged`, reached when the flagged rows are the only
    problem. It stops, or re-runs the dry run without those rows and goes to the
    confirmation.
- **The M6a check "copied kWh never decreases" is removed.** The scan subsumes it, and the
  check had become unreachable.
- **`b81da19`:** the note for a daily copy says "(no cost history copied)" when no cost
  rows are copied. Before, it said "approximate cost" even when nothing was copied, as on
  9105.

### Item 4: release polish

- **README.md:** a full rewrite for users (details in section 5 below).
- **CHANGELOG.md:** Keep a Changelog format; `2.0.0-rc1` with the changes since `dev7`.
- **Brand icon.** `custom_components/amber_energy_dashboard/brand/icon.png` (256 px) and
  `icon@2x.png` (512 px): an original, generic design of hourly bars and a lightning
  bolt. It uses no Amber Electric marks, and was generated by a script in the scratchpad.
  - HACS accepts local brand assets at that path (checked in HACS's `validate/brands.py`).
  - `ignore: brands` is removed from `.github/workflows/hacs.yml`.
- **Manifest version** `2.0.0-rc1`.

## 2. Test results

`uv run pytest --cov`: **371 passed, 0 skipped, 0 failed** (238 s), with **100 %
coverage** of 3213 statements. ruff check and format are clean.

hassfest (local, pinned HA): 1 integration, 0 invalid.

**CI on GitHub (v2):** hassfest, HACS validation (with the brands check re-enabled) and
Tests are all `success` on `0139fdc`. The results for the final commits are in section 7.

**New and changed tests** (16 more than M6a's 355):

- **Retention** (`test_recovery.py`, `test_manager.py`):

  | Test | What it checks |
  |---|---|
  | `test_fixed_boundary_many_days_costs_two_probes` | **40 consecutive days** with a fixed boundary date: each day `verified` with exactly 2 probes (boundary, day before), plus only the new day's fetch; the day count grows 21 → 60, and the date is unchanged |
  | `test_fixed_boundary_real_move` [forward-1, back-1, forward-9, back-9] | after 10 verified days, a real move in each direction is followed (3 or ≤ 8 calls), `previous_boundary` is recorded, and the next day is back to 2 probes |
  | `test_marker_behind_boundary_after_eight_month_outage` (updated) | a rolling boundary after 240 days: `moved forward 240 days (same day count as at the last verification)`, 6 calls |
  | `test_long_outage_with_changed_day_count_is_unresolved_then_discovered` | 3 → 80 days after 200 days away: `unresolved` (6 calls), then discovery the next day finds it |
  | `test_store_migration_to_boundary_date` [4 cases] | 1.2 → 1.3: boundary kept; counted from `measured_on`; counted from today; never discovered |
  | `test_store_migration_on_disk_and_derived_days` | a real 1.2 file (VM 9102's shape) loads as 1.3; the day count is derived and follows the calendar |

  The existing re-verify tests (`stable`, `forward-1`, `forward-many`, `back-1`,
  `back-many`, the 8-call cap and the move beyond the bracket) pass unchanged.
- **Scan** (`test_migration.py`):

  | Test | What it checks |
  |---|---|
  | `test_scan_series_kinds_and_rederived_sums` | spike, jump, reset and dip in one series with exact re-derived sums; cost falls allowed, but a cost spike flagged; row spacing scales the cap; no lookahead on the last row; empty input |
  | `test_v1_readme_spike_is_listed_and_refused` | **the v1 README's 99999 at the current hour**: listed (`after`, `spike`), the real run refused with the record untouched, and with `exclude_flagged` completed, with the choice stored and the sums continuous |
  | `test_spike_in_copied_history_is_left_out` | a spike in the copy period is not copied, and the other copied sums are unchanged |
  | `test_jump_in_copied_history_rederives_later_sums` | a +5000 jump before the boundary: later sums re-derived; parity passes |
  | `test_options_flow_flagged_rows` | the `migrate_flagged` step: stop, or re-run the dry run without the rows, confirm and complete |
  | `test_decreasing_legacy_energy_is_refused` (updated) | now refused through the scan |

- **Updated for the fixed-date meaning:**
  - `test_pricing_mode_…` (the price series starts at the fixed boundary);
  - `test_chain_mirrors_usage_gap_and_outage` (the boundary is moved explicitly);
  - `test_store_edge_cases`;
  - `test_store_minor_version_1_is_migrated` (an M3 1.1 file with a day count now ends at
    1.3).
- **Test helper:** `_preload_store(retention_days=N)` now writes the old 1.2 layout, so
  every such test also goes through the store migration.

## 3. Lab verification

### 3.1 VM 9105: the published v1 kit, then the migration

**Clone.** `amber-test-v1kit`, linked from template 9000 at 08:45 AEST (HA 2026.9.3,
Australia/Sydney, AUD). `local-lvm` was 91.8 % free; with 9102 that made 2 clones.

**The v1 kit, following `legacy/v1/README.md` step by step:**

1. **Prerequisites.**
   - The core **Amber Electric** integration, added with the test key (site "Home").
     It provides `sensor.home_general_price` and `sensor.home_feed_in_price`.
   - **Import Statistics** (klausj1) **v5.3.0**, the current release (zip sha256
     `9a4d8db4…b45cf`). It was copied into `custom_components` and added as an
     integration. **Deviation:** not through HACS, because adding HACS needs an
     interactive GitHub device login. The files are the same release zip that HACS
     would install.
   - Its services include `import_from_json` and `export_statistics`.
2. **Setup step 1.**
   - `configuration_snippet.yaml` was appended to `configuration.yaml`, with the site ID
     put in the `rest:` URL. The template has no clashing top-level keys.
   - `amber_api_key: "Bearer …"` was added to `secrets.yaml`, as the README says. The key
     was sent over SSH stdin and never printed.
   - `check_config`: `valid`. Then a full restart.
3. **Step 2 (entities exist):**
   - `sensor.amber_energy_import` / `_export` read `0.0 kWh`;
   - both `input_number` running totals read `0.0`;
   - the REST sensors read `19.498` / `1.229 kWh` for 09-27, the same as the integration
     imported on VM 9102.
4. **Steps 3–4: `amber_backfill.py`.**
   - Only the "EDIT THESE" block was changed:
     - the key, site and HA token come from environment variables, so no secret was
       written to a file;
     - `HA_BASE_URL=http://<ip>`;
     - `END_DATE=2026-09-26`, `DAYS=179`;
     - `REQUEST_DELAY=12.0`, to stay near 25 calls per 5 minutes.
   - **Realistic history with the kit's own mechanisms.** Amber no longer serves the 89
     days before its retention boundary (04-01 to 06-28). Those were **seeded as
     synthetic, plausible daily totals into the kit's own resume cache**
     (`amber_backfill_cache.json`, the mechanism it uses to skip days already fetched):
     1388.678 kWh import and 536.237 kWh export, with more import towards winter.
   - The kit then **fetched the 90 real days (06-29 to 09-26) live**. It took 1124 s, made
     90 calls with 0 rate-limit retries, and wrote everything in one `import_from_json`
     call (HTTP 200) before seeding the helpers.
   - Final totals: import **2722.113** = 1388.678 synthetic + **1333.435**, and export
     **1514.068** = 536.237 + **977.831**. The real parts equal, to the digit, VM 9102's
     integration totals for 06-29 to 09-26.
   - So the history is real kit output throughout, with synthetic values only where Amber
     cannot supply any.
5. **Step 5 (verification).** The statistics were checked over the recorder API instead of
   `export_statistics`, to avoid writing a CSV into `/config`: one row per day, rising
   totals.
6. **Step 6: Energy dashboard.**
   - Grid from `sensor.amber_energy_import`, to `sensor.amber_energy_export`.
   - "Use an entity with current price": `sensor.home_general_price` and
     `sensor.home_feed_in_price`.
   - Read back exactly. HA created `sensor.amber_energy_import_cost` and
     `sensor.amber_energy_export_compensation` (AUD, `total`), both `0.0`.
7. **Step 7: daily automation.**
   - Saved from `daily_automation.yaml` as `automation.amber_usage_daily_statistics_import`
     (on), then **run once manually**, as the README says.
   - It refreshed the REST sensors (1 call), added 09-27 to the running totals, and wrote
     the 09-27 lump. There were now 180 rows, with the last sums **2741.611** and
     **1515.297**, and the helpers matched.

**What real v1 history looks like (read back):**
- **kWh:** 180 daily lumps per statistic, from 2026-04-01 to 09-27. Each row is at local
  midnight, source `recorder`, state = sum = the running total.
  - Steps are 8.2 to 31.0 kWh a day for import, 0.08 to 25.5 kWh for export.
  - The lumps cross the **2026-04-05 DST end** correctly: 04-04T13:00Z, then
    04-05T14:00Z, then 04-06T14:00Z. That is local midnight in AEDT, then in AEST.
- **Cost:** see section 4.

**The integration** (the rc1 tree, 20 files with checksums matching) was installed at
09:53. The entry was created at 09:58, with channels E9 general (N71) and B9 feed-in
(N61), fixed times 07:40, 10:40 and 13:40.
- The first run imported **91 days (06-29 to 09-27) in 21 calls**.
- The boundary is **2026-06-29** by bisection (8 calls).
- `RateLimit-Remaining` fell to 19: another client was using the shared budget at the
  same time.

**Migration, clean cycle** (10:00 to 10:03, 0 Amber calls):

| Step | Evidence |
|---|---|
| Dry run | Layout **v1**, detected automatically: all 4 v1 statistics found, and nothing ignored. Boundary 2026-06-28T14:00Z. **Parity passed**: daily rules, **91 days** (06-29 to 09-27), import and export kWh 91 each, **0 mismatches**. Flagged: 0. Copy: 90 rows each for e9 and b9 energy (89 lumps plus the carry row), baselines 1388.678 and 536.237. The Energy preview is exact (prices cleared; `stat_cost` and `stat_compensation` set). The automation was found, state `on`. |
| Snapshot | `premig`, with RAM |
| Real run | `completed`. The copy was followed by a **re-base from stored hourly amounts: 91 days, `calls {}`**. Energy switched. The automation was `on` and was turned off. Changes recorded: backup, copy, rebase, energy, automations, completed. |
| Verify: seam | e9: 90 copied rows (04-01 to 06-28), then 2184 own rows. Carry 1388.678 = the v1 lump before the boundary. **Seam gap 0, 0 sum breaks, final 2741.611 = v1 final 2741.611 (difference 0.0).** b9: the same; **final 1515.297 = v1 1515.297 (0.0)**. |
| Verify: cost | Cost, compensation and net cost start at the boundary (no copied rows): finals 364.859155, 23.492822 and 341.366333 |
| Verify: dashboard | Energy preferences show the new statistics, also on disk in `.storage/energy`. `energy/validate` reports **no issues**. |
| Verify: automation and cleanup | The automation is `off`. The Repairs item `legacy_cleanup` lists the automation, both helpers, the REST sensors, the template sensors and their recorder excludes, the secret, `amber_backfill.py` and its cache, the package note, and the statistics note. |
| Undo | `energy_restored true`: the dashboard is back on the v1 sensors with both price entities. `automations_enabled` is the v1 automation, now `on`. Status `undone`; the copied history stays. |
| Re-run | Completed. The re-base was skipped ("already continuous"); energy switched again; the automation turned off again. Seam and finals are identical to the first run. |
| Second run | **Refused:** "The migration was completed on 2026-09-28. Undo it first to run it again." |

**Spike test** (after a rollback to `premig`, restored from RAM, so no restart and no
Amber call):
- **Two spikes imported with `import_statistics.import_from_json`:**
  - the v1 README's recorder test exactly: `99999` at the current hour (2026-09-28
    10:00);
  - one at the 2026-05-15 lump (inside the copy period; the import replaced that
    lump).

  Both read back as 99999.
- **Dry run:** `flagged` count 2, `blocking`:
  - `sensor.amber_energy_import` 2026-05-14T14:00Z, sum 99999, step 99376.473, **spike,
    copy period**;
  - 2026-09-28T00:00Z, step 97257.389, **spike, after**.

  The problem line names `exclude_flagged`. Parity still passed, because neither row is on
  an overlap day.
- **Real run without the option: refused** with that message. The migration record stayed
  `None`.
- **Real run with `exclude_flagged`: `completed`.**
  - e9 now has **89 copied rows** (the 05-15 lump left out). The 05-16 row's state is
    29.634, which is two days' usage, and the sums continue: 622.527, then 652.161.
  - The carry row is still 1388.678. **Seam gap 0, 0 sum breaks, final 2741.611.** The
    README spike after the data was not used, and the maximum sum is 2741.611.
  - `energy/validate`: no issues. The automation is `off`.

**Other checks.**
- The 10:40 scheduled run on 9105 made 0 calls (already caught up;
  `requests_since_start` 22).
- The v1 cost statistics were read again at 11:02: still exactly 1 row each.

**VM 9105 was destroyed** at 11:02 AEST: stopped and deleted, no longer present. Clones
now: 9102 only.

### 3.2 VM 9102

It was not touched in M6b after the M5 checks this morning (M5 report, Addendum 2). It
still runs `2.0.0-dev5` in store layout 1.2. The rc1 store migration (1.2 → 1.3) was tested
against a file of its shape (`test_store_migration_on_disk_and_derived_days`), but **not
upgraded in place on 9102**. That is suggested as a next step.

## 4. Open questions assigned to this milestone

**v1 cost rule: confirmed, kept as is.** Raw observations on 9105:
- At 09:51, when the Energy preferences were saved, HA created
  `sensor.amber_energy_import_cost` and `sensor.amber_energy_export_compensation` (AUD,
  `total`), state **0.0**.
- After the next HA restart (09:53) both were **`unknown`**, and they stayed so. Their
  source `sensor.amber_energy_import` is frozen at 0 and never changes state, so the cost
  sensors never get a first reading again.
- Statistics: short-term, 1 row (23:50Z, sum 0.0); **hourly, exactly 1 row each**
  (2026-09-27 23:00Z, sum 0.0, movement 0.0). The same was true at 11:02, two hours later.
- So a real v1 install's cost history is empty, apart from a few zero rows between a save
  of the Energy preferences and the next restart. The v1 README itself says historical
  cost shows as $0.
- In the migration the cost statistics were detected as v1 sources, but had no rows
  before the boundary, so nothing was copied. They had no overlap days either (their span
  starts on 09-28), so there was no cost comparison.
- The 0.05 AUD "appears empty" rule stays, for installs with zero rows before the
  boundary. Recorded in DESIGN section 14.

**Item 1 design point** (for confirmation): the rolling-boundary fallback in section 5
item 2.

## 5. Deviations from DESIGN.md and proposed changes

1. **HACS not used on the clone** for Import Statistics (it needs an interactive GitHub
   login). The same release zip was installed by hand. The kit itself was followed
   exactly otherwise.
2. **Retention fallback beyond the bracket (new, within the 8-call cap).** If the boundary
   moved forward beyond the 15-day bracket, the check also tries today − (the day count at
   the last verification). Without this, a *rolling* account coming back from a long
   outage would stay `unresolved` until discovery the next day, which is what the old day
   count gave for free. Please confirm.
3. **Known limitation, found while writing this report: discovery with a fixed, far-back
   boundary.** Discovery is unchanged from M4: it guesses today − 89 and has a 30-day
   bracket.
   - **Who is affected:** accounts whose boundary really is a fixed date (as observed
     here), set up fresh once that date is more than about 120 days back. For this
     account that is from about 2026-10-27. Existing installs are not affected: their
     stored date is simply verified with 2 probes.
   - **What happens:**
     1. Setup cannot bracket the boundary and stores today − 89 ("fallback (not
        bracketed)").
     2. The next day's check finds data on both probes, and the backward move is beyond
        its bracket, so the result is `unresolved`.
     3. The day after, discovery falls back to today − 89 again, and so on.
   - **The cost:** about 3 to 4 extra calls a day, forever, and a boundary that never
     converges.
   - **What it does not break:** imports (the walk goes forward from the marker, and
     skipping days older than the boundary does not apply to them). History older than
     today − 89 is not imported at setup, which is the same as today.
   - **Proposed fix for rc2:**
     - a backward move beyond the bracket accepts the bracket end, which is known to have
       data, as the new boundary ("moved back at least N days"), so it converges by up
       to 17 days a day;
     - discovery that cannot bracket older stores the oldest day it saw with data,
       instead of today − 89.

     This needs your decision, because it changes the M4 rule "unresolved → full
     discovery next day". **It is not fixed in rc1.**
4. **Scan thresholds I chose** (please confirm):
   - 100 kWh per row, scaled by the days between rows;
   - an energy decrease above 0.0005 kWh;
   - a cost step above 100 in size;
   - one row of lookahead to tell a spike from a jump or reset;
   - all legacy rows are scanned, including rows after the integration's data (the
     README's spike is one), and any flag blocks by default.
5. **The M6a "never decreases" refusal is replaced by the scan,** which covers it.
6. **Brand icon:** an original design shipped in the integration folder, not submitted to
   `home-assistant/brands`. Please look at it, and replace it if you prefer.
7. **Not implemented, for your decision.** DESIGN section 12 lists
   `rebuild_from_anchor(anchor_date, anchors)` and `probe_retention()`. Neither was built
   in any milestone, and earlier reports did not flag this; I found it while writing the
   README.
   - `rebuild_from_anchor` is superseded by `backfill` plus the guarded tail rewrite.
     **Proposal:** drop it from DESIGN.
   - `probe_retention` is small (force a discovery). **Proposal:** add it in rc2 if
     wanted, or drop it.
   - The README documents only the services that exist.

## 6. Live Amber API calls

All calls used `AMBER_TEST_API_KEY`. None were in a quiet window: the lab's live steps ran
from 09:31 to 11:02, and the guard also kept ±3 minutes clear of 9102's and 101's
attempts.

**`/sites` + `/usage` counter (counted exactly): 118**

| Time (AEST) | Calls | What |
|---|---|---|
| 09:31 | 1 | Core Amber config flow (`/sites`) |
| 09:32 | 1 | v1 REST sensor refresh at HA start (usage, yesterday) |
| 09:32 to 09:51 | 90 | v1 `amber_backfill.py`, one per day, 0 retries |
| 09:52 | 1 | v1 automation, run manually |
| 09:53 | 1 | v1 REST sensor refresh at the restart for the install |
| 09:58 | 2 | Integration config flow and entry setup (`/sites`) |
| 09:58 to 09:59 | 21 | Integration first run: 13 usage + 8 bisection probes |
| 10:00 to 11:02 | 0 | Migration, undo, re-run, spike test and scheduled 10:40 run |

**`/prices` counter (estimated):** about 95. The core Amber integration polls
`/prices/current` once a minute. It ran from 09:31 until the VM was destroyed at 11:02
(about 91 minutes), with extra refreshes at its setup and at 2 restarts.

**Earlier today** (M5 Addendum 2): 9 calls (9102 at 07:15: 4; 9104 at 07:45: 4; 1 raw
fetch at 08:08). **The day's total on the usage counter is 127.**

## 7. Lab state left behind

- **VM 9105:** destroyed. Its `secrets.yaml` (which held the test key, as the kit
  requires), its config entries and its `.claude_work` folder went with it.
- **VM 9102 `amber-test-m3`:** running `2.0.0-dev5`, snapshot `current` only. Its next
  attempt is 13:15 AEST (about 0 to 4 calls a day). **1 clone exists.**
- **VM 101:** not accessed.
- **Local files:**
  - Scratchpad: the lab logs, the JSON reports (site and NMI redacted) and the script.
  - The kit's cache and the backfill output, which held real daily usage, were deleted.
- **Repository:** `v2`, pushed; tag `v2.0.0-rc1` pushed. `main` was not touched, and no
  `v1-final` or `v2.0.0` was created.
- **CI** for the tagged commit: see the final lines appended at the end of this section
  when the run completes.

## 8. Recommended next steps

1. Robert: decide section 5 items 2, 3, 4, 6 and 7.
2. Robert: create the GitHub **pre-release** from tag `v2.0.0-rc1`, pasting
   `reports/release-notes-v2.0.0-rc1.md`.
3. Upgrade VM 9102 in place to rc1, following the M4 section 9 procedure. This checks the
   1.2 → 1.3 store migration on a real file. Watch one scheduled run: expect `verified`
   with 2 probes.
4. An install through HACS from the custom repository, on a clone where you can do the
   GitHub login for HACS. It proves the pre-release install path end to end.
5. After the RC period: merge `v2` to `main`, tag the old `main` as `v1-final`, and tag
   `v2.0.0`.
