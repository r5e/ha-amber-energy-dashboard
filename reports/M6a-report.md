# Milestone 6a report: migration from the YAML kits

Date: 2026-09-27, 15:18 to about 17:00 AEST. Branch `v2`.

**Summary.** Built DESIGN section 14:
- detection of the v1 kit and the advanced YAML version by name, with a manual pick when
  detection is partial or ambiguous;
- a parity check before any change;
- copy of the history from before the integration's first day (export cost negated into
  compensation, net cost rebuilt), then a re-base of the integration's own rows through the
  range-rewrite path;
- the Energy dashboard switch (a full copy saved and read back first);
- legacy automations turned off (prior state recorded);
- a cleanup list;
- undo, resumability and a second-run refusal;
- an options-flow menu entry, and three services.

**353 tests pass, with 100 % coverage** and synthetic data only. hassfest passes.

**VM 101 lab: the full cycle ran against the real Amber API with the test key.** Dry
run, real migration, verification, undo, re-run to the migrated state, and a refused
second run.
- 101's partly present v1 leftovers (with the 99999 spike) were ignored, with the reason
  stated, and the active advanced version was chosen.
- Parity passed over 90 days with **every daily kWh total exact**.
- After the migration the new statistics end at **exactly** the legacy totals: import
  6655.585 kWh and export 3518.881 kWh. The seam gap is 0.
- Compensation is now positive when earned (23.41 against the legacy −23.41).

**The lab found one real bug, now fixed.** 101's legacy cost series starts on the
integration's first day, which the first parity version flagged as missing.

**101's key:** as instructed, I turned off the legacy automation first, and it stayed off
throughout. The secrets were not touched. VMs 9102 and 9104 were not touched.

## 1. What was built

Commits since the M5 addendum (`2720082`):
- `3e26274`: M6a decisions recorded in DESIGN section 14 (before any code);
- `2c74636`: Milestone 6a code, tests and README section;
- `7aee4aa`: parity compares each legacy statistic within its own span (lab finding);
- `4845d20`: undone runs kept in the record's history;
- `e338c1f`: cleanup Repairs issue kept across restarts (lab finding);
- `4b6768c`: those details recorded in DESIGN section 14;
- this report.

Diffstat: 17 files, +3329 / −9 lines. Manifest version `2.0.0-dev6`, with
`after_dependencies` automation and energy.

**`migration.py` (new).**
- **Layouts.** `V1_KIT` and `ADVANCED` each hold:
  - the role → statistic ID mapping;
  - the required roles;
  - the export-cost sign;
  - the parity rules;
  - the known automation IDs;
  - content markers for automations;
  - the cleanup text.
- **`async_detect()`.** A layout is a candidate if its import kWh statistic has data.
  - A layout is chosen only if it is complete and its data is at least 2 days newer than
    any other candidate's; the others are reported as ignored, with the reason.
  - Otherwise `needs_manual_pick` is set, with the reason (none found, incomplete, or
    ambiguous).
- **`_manual_sources()`** validates a pick:
  - import kWh is required;
  - no statistic is picked twice;
  - each exists with data;
  - none is one of this integration's own;
  - the units are kWh or the currency.
- **Plan (`_async_plan`, with `_preconditions`, `_unit_problems`, `_boundary`,
  `_export_sign`, `_plan_copy`).**
  - Preconditions: not in Pricing mode, first import done, no pending write, no foreign
    range rewrite, and one general and at most one feed-in channel.
  - The boundary is the integration's first row. It must be a NEM day start and not
    older than retention.
  - Legacy and new daily totals, then parity (`_parity`).
  - Copy rows (`_copy_rows`): sums kept, states recomputed, and a carry row in the hour
    before the boundary.
  - Net rows (`_net_rows`).
  - A refusal if a copied kWh series ever decreases.
  - The Energy plan (`_energy_plan`: before and after, validated with
    `ENERGY_SOURCE_SCHEMA`, other uses of legacy statistics, written instructions).
  - The automations (`_find_automations`) and the cleanup list.
- **`async_migrate()`.**
  - A dry run returns the report.
  - A real run refuses when:
    - the migration is already completed ("undo it first");
    - picks differ from an in-progress run;
    - there are plan problems;
    - parity fails;
    - the backup is not confirmed.
  - Otherwise `_async_execute` runs under the manager's run lock:
    1. **Begin:** a new record, with the full Energy preferences and the plan saved to the
       Store and read back from disk through a fresh Store handle (the backup check);
       resume if one is in progress.
    2. **Copy:** written and read back row by row.
    3. **Re-base:** `manager.async_rebase`, skipped if the seam is already continuous; a
       pause on API errors or waiting data, with the progress kept.
    4. **Energy:** `async_update` through HA's energy manager, read back; on a mismatch
       the backup is restored and instructions given.
    5. **Automations:** those that are on are turned off; every prior state is recorded.
    6. **Finish:** a persistent `legacy_cleanup` Repairs issue.
  - Each step and change is recorded with a timestamp.
- **`async_undo()`.** Restores the saved Energy preferences (only if the migration changed
  them), re-enables only the automations it turned off, and verifies both.
- **`async_delete_legacy()`.** Needs a confirm flag and a completed migration; the
  statistics are cleared and their absence verified. Undo is refused afterwards.
- **`format_report()` / `format_result()`** produce the options-flow text.

**Changes elsewhere.**
- `manager.py`: `_async_baselines`: a first imported day with no earlier imported day
  continues from a row in the hour before it (the copied carry row), else 0.
  `async_rebase(first)` runs a range rewrite (resuming stored progress) with its own call
  budget.
- `importer.py`: `async_verify_range()` (read back arbitrary rows), and `_compare` takes a
  tolerance.
- `storage.py`: a `migration` record (default None), `async_set_migration`, and
  `async_read_back_migration` (fresh handle, on disk).
- `__init__.py`:
  - services `migrate_v1(dry_run=true, confirm_backup=false, import_energy, export_energy,
    import_cost, export_cost)`, `undo_migration()` and `delete_legacy_statistics(confirm)`;
    refusals are raised as `ServiceValidationError`;
  - the cleanup issue is re-created at setup while the migration is completed.
- `config_flow.py`:
  - the options flow is now a menu: *Import settings* (the former form, now step
    `settings`), *Migrate from the YAML kit* and *Undo migration* (shown once a migration
    has started);
  - steps `migrate_pick`, `migrate_confirm` (the dry-run report plus the "I have a
    current Home Assistant backup" box), `migrate_result` and `undo_migration`.
- `strings.json` / `en.json`, `services.yaml`, `icons.json` and `manifest.json` are
  updated.
- `README.md` has a new "Migrating from the YAML kits" section.
- DESIGN section 14 was rewritten with the decisions (layout table with the exact names),
  plus the section 12 service list and the section 16 split into 6a and 6b.

## 2. Test results

`uv run pytest`: **353 passed, 0 skipped, 0 failed** (193 s). Coverage is **100 %** of
3076 statements. ruff check and format are clean. hassfest: 1 integration, 0 invalid.

`tests/test_migration.py` adds 38 tests, all synthetic. Legacy statistics are imported as
recorder statistics shaped as each kit writes them: advanced hourly inside the window
with daily lumps before it; v1 daily lumps throughout; state equal to the running total.

**Required cases:**

| Requirement | Tests |
|---|---|
| Both layouts | advanced end to end; `test_v1_kit_daily_lumps` |
| Manual pick | two recent layouts; a renamed statistic; a manual daily pick with an automation found by content; pick validation (6 cases) |
| Partial detection | `test_partial_detection_needs_manual_pick` |
| 101 case (active layout chosen, broken stale v1 ignored, no mixing) | `test_both_layouts_picks_active_and_ignores_broken_v1` |
| Sign negation | advanced copy: compensation equals −legacy and net equals cost − compensation; v1 keeps HA's sign; manual sign from the overlap |
| Sum continuity at the seam | carry row, first own row continues from it, all rows continuous, final sum equals the legacy final, and the next import continues |
| Re-base when data exists | every migration test starts from an integration that already imported the window |
| Parity failures | kWh off by 0.001 (advanced) and cost off by 0.02; v1 kWh off by 0.02; too few days; a missing day inside a span. Nothing changes: rows, prefs, automation and record all verified |
| Interrupted and resumed | the call budget stops the re-base; the copy is recorded; the dashboard and automation are untouched; the run again finishes; different picks mid-run are refused |
| Second-run refusal | `test_second_run_refused_undo_and_rerun` |
| Undo | prefs restored exactly; the automation turned back on; an already-off automation left off; failures reported; refused after deleting legacy statistics |

**Also tested:**
- the backup-check failure (record untouched);
- the missing backup confirmation;
- unit and boundary problems;
- the preconditions (pricing mode, pending write, rewrite running, first day older than
  retention, no data yet, no feed-in channel, channel shape);
- the Energy fallbacks (not used, schema failure, read-back mismatch, manager unavailable,
  other uses reported);
- deleting legacy statistics;
- the options-flow paths (menu, confirm without the box, run, result, undo, manual pick
  with an error, blocked by parity, entry not loaded);
- the cleanup issue surviving a reload;
- a cost series that starts at the boundary (101's shape).

**Existing tests:** the 6 options-flow call sites now pass through the menu
(`next_step_id: settings`). There are no other changes.

## 3. Lab verification (VM 101)

All steps ran on VM 101 with read-back checks. The scratchpad log is `lab101.log`, and
the JSON reports are `lab101_dryrun.json`, `lab101_migrate.json` and `lab101_rerun.json`.

**State before any change (15:18 to 15:21, read only):**
- HA 2026.9.3; configuration in `configuration.yaml`, with no packages folder.
- Advanced layout statistics, all recorder-sourced:

  | Statistic | Unit | Rows | Range | Last sum |
  |---|---|---|---|---|
  | `sensor.amber_cumulative_grid_import_v2` | kWh | 2219 (59 daily lumps from 2026-04-30, then 2160 hourly) | to 2026-09-26 13:00Z | 6655.585 |
  | `sensor.amber_cumulative_grid_export_v2` | kWh | 2219 | to 2026-09-26 13:00Z | 3518.881 |
  | `sensor.amber_hourly_cost_import` | AUD | 2160 hourly | from 2026-06-28 14:00Z | 360.3648 |
  | `sensor.amber_hourly_cost_export` | AUD | 2160 hourly | from 2026-06-28 14:00Z | −23.41 |

- v1 leftovers:
  - `sensor.amber_energy_import` / `_export`: 95 and 94 rows, 2026-06-21 to 2026-09-20;
    the import ends at the 99999 spike;
  - the rest sensors `sensor.amber_daily_grid_*` (unavailable);
  - the v1 `input_number` running totals.
- Helpers, scripts, the automation and the YAML blocks are exactly as listed in DESIGN
  section 14's table.
- The Energy dashboard used Solar Analytics historical statistics for grid and solar,
  plus 5 device-consumption entries.
- **Key check:** `secrets.yaml` `amber_api_key` is a Bearer `psk_` key of 36 characters
  and **not equal to `AMBER_TEST_API_KEY`**. It was compared in memory; nothing was
  printed.

**Changes made, in order (AEST):**
1. **15:51:** `automation.amber_daily_statistics_import` turned off (turn_off only). It
   had been `on`, last triggered 12:05 today. After the call it was `off`, and it stayed
   off through all 5 restarts.
2. **16:26:** the original Energy preferences were saved to the scratchpad and to
   `local/lab101_energy_prefs_original.json` (not in git).
3. **16:26:** the grid source was pointed at the `_v2` statistics: import, export, and
   `stat_cost` / `stat_compensation` set to the two hourly cost statistics, matching
   production. Solar and devices were read back unchanged.
4. **16:26:** the integration was installed (checksums matched) and HA restarted.
5. **16:29:** config entry created with `AMBER_TEST_API_KEY` (same account and site;
   channels E9 general and B9 feed-in), on a fixed schedule of 07:25, 10:25 and 13:25.
   - The first setup imported **90 days (2026-06-29 to 2026-09-26) in 21 calls**, with
     retention 90 by bisection.
   - My first flow script missed the schedule step; the stale flow was cleared by a
     restart at 16:28.
6. **16:31, dry run:**
   - Layout `advanced`. **v1 ignored: "last data 2026-09-20, older than the advanced YAML
     version (2026-09-26)"**.
   - Boundary 2026-06-28 14:00Z.
   - **Parity FAILED**, on 2026-06-29 import cost and export cost only: "missing on one
     side". The legacy cost series starts on that day, so it has no earlier total.
   - Fixed in `7aee4aa` (with a test), reinstalled, and the dry run was repeated.
7. **16:36, dry run again:**
   - **Parity passed**, hourly rules, 90 days (06-29 to 09-26), 0 mismatches.
   - Cost totals over the compared days: import 355.8237 against 355.8239; export
     22.8169 against 22.8169.
   - Copy: 60 rows each for e9 and b9 energy (59 lumps from 2026-04-30 plus the carry row;
     baselines 5322.15 and 2541.05).
   - No cost rows to copy (legacy cost starts at the boundary).
   - Energy preview exact, before and after.
   - Automation found in state `off`.
8. **16:37, real migration:**
   - Status `completed`.
   - Re-base: **13 calls, 90 days rewritten**.
   - Energy switched.
   - Automation: `prior off, turned_off false`.
   - Changes recorded: backup, copy, rebase, energy, automations, completed.
9. **Verification:**
   - **e9 energy:** 60 copied rows then 2160 own rows, seam gap 0.0, 0 sum breaks, final
     sum **6655.585, equal to the legacy 6655.585**.
   - **b9 energy:** the same, final sum **3518.881, equal to the legacy 3518.881**.
   - Cost: 360.36497 against 360.3648. Compensation: **+23.409923 against the legacy
     −23.41** (the sign fix).
   - The Energy grid source now uses `…_e9_energy`, `…_e9_cost`, `…_b9_energy` and
     `…_b9_compensation`. This was confirmed in `.storage/energy` on disk.
   - `energy/validate`: **no issues for the grid source**. The only issues are the
     existing `recorder_untracked` warnings for the Solar Analytics sensors, which are
     recorder-excluded on 101 and unrelated to this work.
   - The Repairs issue `legacy_cleanup` lists the automation, the 8 helpers, the 3
     scripts, the `rest_command`, the template sensors and their recorder excludes, the
     secret, the package note and the statistics note.
10. **16:37, undo:**
    - `energy_restored true`; the grid is back on the `_v2` statistics.
    - `automations_enabled []`, and `left_unchanged` lists the automation, which is still
      `off`, as required for an automation that was already disabled.
    - Status `undone`; the copied history stays.
11. **16:37, re-run:**
    - Completed; the re-base was **skipped ("already continuous", 0 calls)**; energy
      switched again.
    - **A further real run was refused:** "The migration was completed on 2026-09-27.
      Undo it first to run it again."
12. **16:42 and 16:47:** redeployed `4845d20` and then `e338c1f`, with restarts.
    - After the first of these restarts the cleanup issue was gone, because issues are not
      persistent by default. This was fixed with a persistent issue that is re-created at
      setup.
    - After the final restart: status `completed`, the issue is present, sums unchanged,
      and the automation is `off`.
    - The options menu on 101 shows *Import settings / Migrate / Undo migration*, and
      *Migrate* answers "already completed".
13. **101's file listing is the same set of files as before.** The only additions are
    `custom_components/amber_energy_dashboard` and this integration's `.storage` files.
    No working folder was needed on 101; the backups are local.

## 4. Open questions and observations

- **The exact names on VM 101** are recorded in DESIGN section 14's table and above. The
  v1 names come from `legacy/v1/`. The v1 automation's entity ID is assumed from its alias
  (`automation.amber_usage_daily_statistics_import`); it is also found by content (its
  `input_number` running totals).
- **Can the Energy dashboard be updated safely from the integration? Yes,** through HA's
  energy manager (`async_update`), validated with its schema and read back. HA writes the
  file with a delay of up to 60 s; it was confirmed on disk on 101. The written-instructions
  fallback is implemented and tested, but was not needed.
- **v1 cost in practice.** The v1 kit's cost comes from the Energy dashboard's own cost
  sensors, which are driven by live state changes of a sensor frozen at 0. In real v1
  installs this cost is therefore probably near zero, not merely approximate. It is
  copied as is and only reported in parity, as decided. This is untested against a real
  v1 install; that is M6b's clone test.
- **101's key** was not the test key, so it was treated as possibly production; details
  are in section 3. I made no change to secrets.

## 5. Deviations from DESIGN.md and proposed changes

All the decisions are implemented. Details I settled are in DESIGN section 14 and listed
here for confirmation:
1. **"Backup check"** means two things. First, the migration's own backup of the Energy
   preferences and the plan is written to the Store and read back from disk before any
   change; if it doesn't read back, the migration refuses. Second, the user must confirm a
   current HA backup (`confirm_backup`, or the options box). The integration does not
   inspect HA's backup list.
2. **Export-cost sign per layout.**
   - Advanced: negated, as decided.
   - v1: its compensation statistic is HA-generated and already positive when earned, so
     it is copied as is. (The decision's "legacy export cost is negated" fits the advanced
     version's Amber-signed statistic.)
   - Manual pick: the sign that matches the new compensation over the overlap; Amber's
     sign is assumed without overlap. The dry run states it.
3. **Parity per-statistic span** (the lab finding). Each legacy statistic is compared from
   the day after its first row to its last row. A gap inside the span still fails.
4. **Thresholds.**
   - At least 3 comparable days.
   - A layout is chosen only when its data is at least 2 days newer than any other
     layout's.
   - "kWh exact" means equal to 3 decimals.
5. **Copied rows** keep the legacy sums. Their states are recomputed as the change from
   the previous row (the kits store the running total in `state`). A carry row is added in
   the hour before the boundary.
6. **Scripts cannot be "disabled",** so they are listed, like the helpers and YAML blocks.
   The Repairs issue is persistent while the migration is completed.

**Proposal for the planning chat: the re-base without re-fetching.** The agreed re-base
uses the range-rewrite path, which **fetches every day again from Amber**. That works
only while the integration's first day is still inside Amber's retention: on 101 it
worked today, but it would not have tomorrow. As built, the migration therefore refuses
cleanly ("re-basing needs to fetch it again") once the first day is older than retention.
That blocks anyone who installed v2 more than about 90 days before migrating.

Proposed change: re-base from the **stored hourly amounts** through the same
write-and-verify path, day by day with stored progress:

| | Stored-amounts re-base | Current (re-fetch) |
|---|---|---|
| API calls | none | ~13 for 90 days |
| Days older than retention | works | refuses |
| Days rewritten | exactly the same (verified by the exact final-sum match on 101) | the same |

I recommend adopting it for M6b, before the release.

**Also for M6b (not a deviation):** test the migration on a clone with a real v1-kit
install (planned), including its HA-generated cost statistics and a DST-period lump.

## 6. Live Amber API calls

**42 calls, all with `AMBER_TEST_API_KEY`**, from 16:27 to 16:48 AEST. There were no
calls in the quiet windows. `RateLimit-Remaining` was 37 after the first setup (the
re-base run does not report it).

| Time | Calls | What |
|---|---|---|
| 16:27 to 16:29 | 4 | config flow `/sites` (two aborted attempts, one debug step, then the real flow) |
| 16:29 | 1 | entry setup `/sites` |
| 16:31 | 21 | first setup: 90 days, including 8 bisection probes |
| 16:36, 16:42, 16:48 | 3 | entry setup `/sites` after the redeploy restarts |
| 16:37 | 13 | migration re-base (90 days) |
| 16:37 | 0 | re-run (re-base skipped); undo and dry runs make no calls |

**Standing calls from now on:** 101's entry runs at 07:25, 10:25 and 13:25 daily with the
test key, about 1 to 3 calls per day when a day is due. None of these times is in a
production quiet window, and they are 10 minutes after 9102's times.

## 7. Lab state left behind

**VM 101, migrated.** Nothing was deleted. All changes:
- `automation.amber_daily_statistics_import` is **off** (turned off by me at 15:51,
  before the migration; the migration recorded it as already off). To restore it: turn it
  on, *only* if its key may make live calls.
- The Energy grid source uses the new statistics. The solar and device entries are
  unchanged.
- **To restore 101's original Energy preferences** (the Solar Analytics setup from before
  this milestone), call websocket `energy/save_prefs` with `energy_sources`,
  `device_consumption` and `device_consumption_water` taken from
  `local/lab101_energy_prefs_original.json` (also in the session scratchpad). Or roll 101
  back to your Proxmox snapshot, which undoes everything in this list.
  - Note: *Undo migration* restores the `_v2` setup, which is what the dashboard used just
    before the migration.
- The integration is installed at `e338c1f` (`2.0.0-dev6`), with one entry: test key,
  fixed times 07:25, 10:25 and 13:25.
- The new external statistics hold 90 days, plus 60 copied kWh rows from 2026-04-30.
- The migration record is `completed`, with the `legacy_cleanup` Repairs issue.
- HA was restarted 5 times (16:26, 16:28, 16:35, 16:42, 16:47).
- The existing clutter, `secrets.yaml` and the legacy statistics are untouched.

**Other VMs:**
- **VM 9102** was not touched. It is still running `2.0.0-dev5` with snapshot `pre-m5`.
- **VM 9104** was not touched. It is still running for the M5 Phase 2 check (M5 report
  section 8).

**Local files:** `local/lab101_energy_prefs_original.json` (excluded from git) and the
scratchpad logs.

## 8. Recommended next steps

1. Robert: decide on the re-base proposal in section 5, and confirm details 1 to 6.
2. Robert: decide 101's automation and key, since the automation stays off until you say
   otherwise.
3. Tomorrow: the M5 Phase 2 check on 9104 (M5 report section 8), then destroy 9104.
4. M6b, after sign-off:
   - the v1-kit clone test (a fresh clone with the published kit, its cost sensors, a
     real parity run, the migration and undo);
   - the re-base change if accepted;
   - README credits;
   - the HACS release.

## Addendum (2026-09-27 evening): M6a review decisions

This was offline work only: no lab contact and no live Amber calls. VMs 101, 9102 and
9104 were not touched.

**Decisions implemented (commits `7e7ed1a` and `1b09c27`, recorded in DESIGN section
14):**

**Re-base from stored hourly amounts (adopted).**
- `migration._async_rebase_stored` replaces the re-fetching re-base, and
  `manager.async_rebase` is removed.
- It reads the integration's own rows from the boundary, then rewrites each day's 24
  stored hourly amounts through `importer.async_write_amounts`, the same write-and-verify
  path as an import.
- Each day continues from the previous day's written sums; the first day continues from
  the copied carry row.
- After each day it stores `rebase_progress` (next day, running sums, day count) in the
  migration record. A paused re-base (for example a verification failure, or a day
  without all 24 hours) resumes from there.
- **0 API calls.** The "first day older than retention" refusal is removed.
- The "range rewrite in progress" precondition now always applies, since the migration
  no longer uses that progress.

**v1 cost (provisional until M6b).** For the v1 layout, if the cost and compensation rows
to copy move less than 0.05 AUD in total over the copy period, the dry run notes "v1 cost
history appears empty; not copied" and they (and net cost) are not copied. Otherwise they
are copied as is.

**Section 5 details 1 to 6** are accepted and recorded as the settled design.

**VM 101** stays in the migrated state with its legacy automation off permanently. No
further changes were made to it.

**Tests: 355 passed, 100 % coverage** (3096 statements); ruff clean. There are 40
migration tests. New or changed:
- `test_first_day_older_than_retention`: the migration completes after Amber has dropped
  every imported day, with no calls; sums are continuous and end at the legacy total.
- `test_interrupted_rebase_resumes`: an injected verification failure on the third day
  pauses the re-base with progress `next` = the third day and 2 days done; the next run
  resumes and finishes exactly.
- `test_rebase_partial_day_and_seam_failure`.
- `test_v1_empty_cost_history_is_not_copied`.
- The end-to-end advanced test now asserts the re-base source "stored hourly amounts",
  `calls {}`, and no Amber calls during the migration.

Manifest version `2.0.0-dev7`. VM 101 still runs `2.0.0-dev6`, as it was to be left
unchanged.
