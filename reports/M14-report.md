# Milestone 14 report: 2.1.0 released

Date: 2026-10-10 (Sydney). **2.1.0 is released:** `main` and `v2` were fast-forwarded to
`8843d82`, and the annotated tag **`v2.1.0`** (tag object `59bfe54`) is on it. All of
these are pushed. CI on `main` is green. `feature/v2.1` is deleted locally and on GitHub.

**Summary.**
- **Measurement trigger (the production report).** The days lacking export aggregates
  are now measured straight away, in the background, without an import run, in three
  cases:
  - the allowance or the bill estimate is turned on (at the setup that the reload makes);
  - either is changed in the options;
  - a scheduled retry skips because the day is already imported.
- **9102 check passed.** I reproduced production's state: allowance off, no aggregates
  stored, the day already imported. Turning the allowance on measured all 89 days with 13
  usage calls and no run. The re-measured aggregates are byte-identical to the backup, and
  the sensors went back to 43.552 used / 196.448 remaining. A plain off and on, with every
  day measured, made no usage calls.
- **Screenshots:** six images in `docs/images/`, replacing 14 and 16. The bill picture is
  used for both bill placeholders, and every image is shown at about half its width.
- **491 tests pass, with 100 % coverage**, and ruff is clean.
- **9102 runs the tagged 2.1.0:** the sha256 of all 27 files matched, and there were no
  warnings. Snapshot `pre-rc2` is deleted.

## 1. What was built

| Commit | What |
|---|---|
| `492fa33` | Measure missing export aggregates without an import run (`manager.py`, `__init__.py`, `diagnostics.py`, tests, DESIGN section 20) |
| `a07f9f9` | INSTALL.md: the screenshots and the annotated bill, plus one sentence saying that turning the allowance on measures at once |
| `8843d82` | 2.1.0: manifest, CHANGELOG, README banner, `reports/release-notes-v2.1.0.md` (**tagged `v2.1.0`**) |
| (this commit, on `main`) | This report |

### 1.1 Measurement trigger (`manager.py`, `__init__.py`)

- **`export_unmeasured`** (property) lists the imported days in [retention boundary, last
  imported] that have no stored aggregates, while the allowance applies. It leaves out two
  kinds of day:
  - days before the feed-in channel existed (`channel_since`);
  - days already found to have no feed-in records (see 5.1).

  The run's `_async_export_days` now uses the same list.
- **`async_measure_export(trigger)`** does the measurement outside an import run:
  - it returns None, and makes no calls, when nothing is missing;
  - otherwise it takes **the run lock** and checks again (a run may have measured the
    days meanwhile);
  - it starts a **`RunBudget`**, so the per-run cap and the `RateLimit-Remaining`
    reserve apply as in a run;
  - it measures, then syncs the derived statistics (the adjusted compensation and the
    cost including fixed charges);
  - it publishes the sensors.

  An `AmberError` (budget, rate limit, connection or auth) stops it with a warning, and
  the next attempt continues. It never writes `last_run`, so the import status is
  untouched. The result (trigger, days measured, calls, derived) is kept in
  `last_export_measurement` and shown in **diagnostics** (`export_measurement`).
- **`async_start_export_measurement(trigger)`** starts it as a background task, but only
  when there is something to measure.
- **The triggers:**
  - **Turned on** (allowance or bill estimate): this reloads the entry. Setup now starts
    the measurement once Home Assistant has started, unless a startup run is due anyway
    (`_schedule_startup_work`, split out of `async_setup_entry`).
  - **Changed** in the options without a reload (`async_update_settings`): if the bill or
    allowance settings differ and days are missing, the measurement runs, including the
    derived sync. Otherwise the derived sync runs alone, as before.
  - **Scheduled retry that skips** (`_async_scheduled`, caught up): it calls
    `async_measure_export("scheduled")`. With nothing missing, that makes no calls, so the
    10:22 and 13:22 retries stay call-free once everything is measured.

## 2. Test results

`uv run pytest --cov=custom_components.amber_energy_dashboard`: **491 passed**, 0 skipped
(419 s). Every file is at 100 % (4295 statements). That is 484 at rc2 plus 7 new tests,
all in `test_allowance.py`:

| Test | What |
|---|---|
| `test_turning_the_allowance_on_measures_without_a_run` | **The production case:** allowance off, run, no aggregates. Turning it on in the options measures all 10 days at the reload's setup (trigger `setup`), with `/sites` + 2 usage calls. `last_run` is unchanged (no run). "Allowance used" equals the independent window sum. The adjusted statistic has 240 rows. Diagnostics show the measurement. A further reload makes `/sites` only |
| `test_changing_the_allowance_measures_missing_days` | A change applied in place with nothing missing: no calls. With days missing: trigger `options`, all days measured, 1 call |
| `test_bill_change_measures_missing_days` | Changing the bill estimate (billing day) also measures |
| `test_skipped_scheduled_attempt_measures_missing_days` | **A caught-up scheduled retry** measures the missing days (1 call, `last_run` unchanged, sensor > 0). A second retry makes 0 calls |
| `test_measurement_stops_on_api_errors_and_respects_the_budget` | `RateLimit-Remaining` below the reserve: stops after 1 call (7 days stored), with the warning logged, nothing raised, and the budget released. The next attempt measures the rest |
| `test_days_without_feed_in_are_not_fetched_again` | A day whose measurement finds no feed-in records is not fetched again by a skipped retry or by a run |
| `test_measurement_after_a_run_measured_meanwhile` | The measurement waits for the run lock and does nothing if the days were measured meanwhile. Without the allowance there is never anything to measure |

**CI:**
- `8843d82` on `feature/v2.1`: lint-and-test, hassfest and hacs all **success**;
- the push to `main` (same commit): **Tests**, **Validate with hassfest** and **HACS
  validation** all **success**.

## 3. Lab verification on VM 9102

All times are AEDT on 2026-10-10. The first Amber call was at 07:28, after the quiet window.

**The measurement check (with the code at `a07f9f9`):**

| Step | Evidence |
|---|---|
| Snapshot | `pre-m14` taken first (deleted at the end, see below) |
| Before | rc2. Imported to 2026-10-09 by 9102's own 07:15 run. `export_days` 89 (2026-07-13 to 2026-10-09). Allowance used **43.552**, remaining **196.448** |
| Allowance off (options) | The entry reloaded; the allowance sensors were gone and the options showed `export_allowance: false`. `export_days` was still 89, because turning the allowance off keeps them (see 5.3) |
| Production's state | HA core stopped. The Store was backed up to `/config/.claude_work/20261010-0729/`, and `export_days` set to `{}`. sha256 of the Store without `export_days`: identical before and after. New code installed (sha256 of all 27 files matched). Core started: `export_days` **0**, no allowance sensors, `last_run` unchanged |
| **Allowance on (options), no run** | Within 20 s. `export_measurement`: trigger **`setup`**, **89 days** measured (2026-07-13 to 2026-10-09), calls `{"sites_usage": 13}`. `last_run` **unchanged** (the 07:15 scheduled run). Sensors: used **43.552**, remaining **196.448**, export charge 0.0, bill to date 62.10, projected 155.25 |
| **Read-back** | sha256 of the re-measured `export_days` (sorted JSON) = **the backup's** (`be70ac8c…`). The rest of the Store is unchanged (`4aab9500…` both) |
| Off and on again, all measured | Each reload made `/sites` only. `export_measurement` was null (nothing to measure); the sensors were the same |

**The release install (`v2.1.0`, 07:50):**
- the sha256 of all 27 files matched, and the manifest says `2.1.0`;
- after the restart, the entry was `loaded`, with 1 request since start;
- every sensor was as before (caught up, last imported 2026-10-09, used 43.552 /
  remaining 196.448, bill 62.10 / 155.25);
- the log has no integration warnings or errors. The only entries are Home Assistant's
  standard "custom integration … not tested" notice, and the cancelled
  `homeassistant.restart` service task at shutdown, which is normal for a restart
  requested through the API;
- there are no persistent notifications.

**Snapshots:** both `pre-rc2` and `pre-m14` were deleted (task `OK`). 9102 now has no
snapshots.

## 4. Open questions assigned to the milestone

None.

## 5. Deviations and points for the planning chat

1. **Days without feed-in records are remembered (in memory).** `day_aggregates` returns
   None for such a day, so nothing was stored, and every run fetched the day again. With
   the new triggers, that would also have made calls on every skipped retry. Such days are
   now remembered for the session, and days before the feed-in channel's `channel_since`
   are excluded. After a restart such a day is fetched once more.
2. **The setup trigger fires at every setup with missing days**, not only when the
   allowance was just turned on. The reload after turning it on is indistinguishable from
   any other setup. In practice, missing days occur only after turning the feature on (or
   an interrupted measurement), and with nothing missing there are no calls.
3. **Turning the allowance off keeps `export_days`** (as since M11). A plain off and on of
   an install that has already measured therefore measures nothing, correctly. To
   reproduce production's state for your "off and on" check, I cleared `export_days` in
   9102's Store, with HA stopped, after a backup and a snapshot.
4. **CHANGELOG:** the rc1 and rc2 sections were replaced by the one `[2.1.0] -
   2026-10-10` entry, and their link references removed (2.0.0 had kept its rc entries
   "for reference"). Say if you want them back.
5. **Screenshots:**
   - `device-page.png` (from "NewFeatures") replaces 14, and `options-menu.png` (from
     "Bill Estimate") replaces 16;
   - `new-sensors.png` is in section 4, under "their sensors appear here too";
   - `bill-estimate-step.png` and `export-allowance-step.png` are at the top of their
     subsections;
   - `bill-fields-explained.png` replaces both placeholders, and the two "(Picture: …)"
     mentions now say "see the picture above".

   The bill picture shows charge figures and the billing period, but no name, address or
   NMI.
6. **Lab note:** the `ha` CLI on the SSH add-on needs a login shell (`bash -lc "ha core
   stop"`); without one it says "unauthorized". My first stop attempt therefore failed while
   the Store was being edited. Nothing was lost: the Store saves immediately, HA had no
   pending write, and I stopped core properly and verified the file before starting. Worth
   adding to CLAUDE.md's lab notes.

## 6. Live Amber API calls

**19**, all on 9102 on 2026-10-10, between 07:28 and 07:51 (after the quiet window):
- 07:28: `/sites`, reload (allowance off);
- 07:29: `/sites`, core start (new code);
- 07:30: `/sites` + **13 usage**, reload (allowance on) and the measurement;
- 07:30 and 07:31: `/sites` ×2, the plain off and on;
- 07:50: `/sites`, the restart for `v2.1.0`.

The most in one 300 s window was 15, well within 50. The tests make no live calls.

## 7. Lab state left behind

- **VM 9102** (`amber-test-m3`) is running **2.1.0** (the tagged tree), with the bill
  estimate, the fixed-charges statistic and the export allowance on. It has **no
  snapshots**. `/config/.claude_work` was removed.
- No other clones. VM 101 was not accessed. The template was not touched.
- `~/screenshots.zip` is deleted. The extracted copies were only in the session
  scratchpad.
- **Repository:**
  - `main` = `v2` = `8843d82`, plus this report's commit on `main`;
  - tag `v2.1.0` (`59bfe54`, commit `8843d82`);
  - `feature/v2.1` is deleted locally and on GitHub;
  - everything is pushed.

## 8. Recommended next steps

1. **Create the GitHub release** for `v2.1.0` from `reports/release-notes-v2.1.0.md`
   (there is no `gh` here). HACS then offers 2.1.0 as an update.
2. Update production from HACS. Its export aggregates are already measured, so nothing
   more happens. The fix matters for new users turning the allowance on.
3. Optionally, tell the forum user that 2.1.0 contains the self-heal.
4. Add the `bash -lc` note (5.6) to CLAUDE.md.
