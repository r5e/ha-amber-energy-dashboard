# Milestone 23 report: the calibration offset in Today so far, and the 2.2.0 release

**Release:** `v2.2.0`, an annotated tag ("Amber Energy Dashboard 2.2.0"). Tag object
`f187a17d9ab38001da8108e342bbdb5f227c02da`, commit
`6ae71e1a01dc860f607b6e1a93fbb549c454a382`. `main` and `v2` both point at that commit.
`feature/v2.2` is deleted locally and on GitHub.

**The decision behind this milestone** came from production data for the first full day
(2026-10-25, 285 of 288 intervals):

| | Cost estimate | Error vs billed $5.2665 |
|---|---|---|
| Raw estimate | $5.1706 | −1.8 % |
| Offset-corrected | $5.2158 | −1.0 % |

For energy, the raw estimate was 20.18 kWh against 20.534 kWh billed. The 9.7 W offset
adds about 0.23 kWh. Decision: **apply the offset.**

## 1. What was built

### 1.1 Today so far applies the calibration offset (item 1)

Commit `be2e8a4`.

- **`today.py`:**
  - Each side keeps:
    - the raw totals (`kwh`, `amount`);
    - the estimate (`estimate_kwh`, `estimate_amount`);
    - `corrected_intervals`.
  - In each interval the estimate adds the offset-corrected energy when the meter's
    offset is known, and the raw energy when it isn't (the fallback). Unpriced energy
    stays raw (`unpriced_kwh`).
  - **`upgraded(record)`** converts a side saved by 2.2.0rc1 or earlier on load (in
    `TodayTracker.async_load`) and for each history day (in `compare`). Those saves kept
    offset-corrected totals only for the intervals that had an offset:
    - the estimate is those totals, plus the raw totals scaled by the share of intervals
      without an offset;
    - with no corrected intervals (2.2.0b2), the estimate is the raw figure;
    - the rc1 keys `corrected_kwh` and `corrected_amount` are dropped.
  - **`summary()`** per side:
    - `amount` and `kwh` are now the estimate (the sensor's state and `energy_kwh`);
    - `raw_estimate` and `raw_energy_kwh` are new;
    - `offset_corrected_intervals` is kept;
    - `offset_corrected` and `offset_corrected_kwh` are removed (the state now carries
      that value).
  - **`compare()`** (the `yesterday_estimate_vs_billed` attribute) gives:
    - `date`, `billed`, `billed_kwh`;
    - `estimated_corrected`, `error_corrected`, `error_corrected_percent`,
      `estimated_corrected_kwh`;
    - `estimated_raw`, `error_raw`, `error_raw_percent`, `estimated_raw_kwh`;
    - `unpriced_kwh`, `intervals`, `offset_corrected_intervals`, `complete`.
    - A small `_versus()` helper computes each pair.
    - The rc1 names (`estimated`, `error`, `error_percent`, `estimated_kwh`,
      `estimated_offset_corrected`, `error_offset_corrected`) are gone. `estimated`
      meant the raw figure in rc1, so keeping it with a new meaning would have been
      misleading.
- **`manager.py`, `_bill_including_today`:** `including_today_estimated` uses the
  estimate. The new `including_today_estimated_raw` is the same total with the raw
  estimates. Both are None under the same conditions as before.
- **`sensor.py`:** Bill to date publishes `including_today_estimated_raw`.
- **Docs:**
  - INSTALL.md "Today so far": the offset bullet (how it is applied, the fallback, the
    first-day comparison figures) and the attribute list.
  - The README paragraph.
  - DESIGN section 22: the decision and the stored layout. The rc1 "offset-corrected
    value, for comparison" bullet is removed.

### 1.2 Release 2.2.0 (item 2)

Commit `3c7e136`.

- **`manifest.json`:** `2.2.0`.
- **CHANGELOG:** `[2.2.0] - 2026-10-25`.
  - The Today so far entry under Added describes the offset.
  - A new Changed entry, "Today so far (since 2.2.0rc1)", gives the production
    comparison and maps every renamed rc1 attribute to its new name.
- **`reports/release-notes-v2.2.0.md`**, drafted in M21/M22 and updated here. Its order:
  1. The backup warning, first.
  2. What's new since 2.1.1 (the Today so far bullet now covers the offset).
  3. Updating from 2.1 (the conversion of own energy sensors).
  4. A new short section, "Updating from 2.2.0rc1", on the attribute renames.
  5. Known limitations: battery valuation at the import price, with the cost basis
     planned for 2.3. The Today so far limitation now says the offset is applied once
     the meter has one, and that it is a 14-day mean.

### 1.3 A privacy fix found by the pre-push scan

Commit `6ae71e1`. `reports/M22-report.md` (line 75) listed the words my privacy scan looks
for, and one of them was the lab Proxmox host's name. I wrote that line in M22, and it has
been on `feature/v2.2` on GitHub since then. The scan before pushing `main` flagged it.

- I undid the local fast-forwards and the tag.
- I replaced the word with "hostnames and private addresses" in a commit on
  `feature/v2.2`, and re-ran CI before tagging.
- The released tree doesn't contain it. The name is still in the history (commit
  `3b8f98c`), which only a history rewrite would remove. Your call (section 5).

### 1.4 Tests

All in `tests/test_sources.py`.

- **`test_compare`:** the new comparison layout. A history day saved without an offset
  compares as raw for both estimates. Both percentages are None when nothing was billed.
- **`test_tracker_offset_corrected`**, rewritten:
  - with no offset, the estimate is the raw figure;
  - with the offset (120 W, 0.01 kWh per interval), `amount`/`kwh` are corrected and
    `raw_estimate`/`raw_energy_kwh` are raw, with the import corrected in 2 intervals
    and the export (0.2 + 0.29 kWh) in 2;
  - after a restart the correction covers the 15 minutes the tracker was stopped.
- **`test_tracker_loads_older_states`** (new):
  - an rc1 save (4 intervals, 3 corrected) loads as corrected + ¼ of raw, and
    `corrected_kwh` is dropped;
  - a b2 save loads as raw.
- **`test_compare_offset_corrected`:**
  - both estimates with their errors and percentages (−1.0 %, −5.0 %);
  - an rc1-format day compares identically to the new format.
- **`test_today_sensor_offset_attributes`:**
  - the sensor's state is the corrected value, with `energy_kwh` corrected and
    `raw_estimate`/`raw_energy_kwh` raw;
  - Bill to date's `including_today_estimated` and `_raw` (the bill estimate is now on in
    this test).
- **`test_today_so_far_sensors`** and **`test_bill_including_today`:** the new attribute
  names, and `including_today_estimated_raw` is None when the other one is.

## 2. Test results

- **Full suite, run locally on the final code (9102 stopped): 617 passed, 0 skipped, in
  8 min 17 s. Coverage 100 %** (6217 statements, 0 missed).
  - 3 warnings: SQLite `ResourceWarning`s ("unclosed database") from the HA test
    plugin's recorder teardown, not from the integration.
- `ruff check .` and `ruff format --check .`: clean. A first run caught a closure over a
  loop variable (B023) and two over-long tests (PLR0915). I fixed them by moving
  `_versus` to module level and splitting out `test_tracker_loads_older_states`.
- **CI:**

| Commit / ref | Tests | hassfest | HACS |
|---|---|---|---|
| `3c7e136` (`feature/v2.2`) | 40236357196 ✓ | 40236357185 ✓ | 40236357203 ✓ |
| `6ae71e1` (`feature/v2.2`) | 40237390065 ✓ | 40237390045 ✓ | 40237390086 ✓ |
| `6ae71e1` (`main`) | 40238202655 ✓ | 40238202591 ✓ | 40238202613 ✓ |
| `6ae71e1` (`v2`) | 40238202880 ✓ | 40238202787 ✓ | 40238202810 ✓ |
| `v2.2.0` (tag) | 40238202764 ✓ | 40238202851 ✓ | 40238202823 ✓ |

## 3. Lab verification (9102, HA 2026.10.0)

| Step | Evidence |
|---|---|
| Start | Local-lvm 91.1 % free. 9102 rolled back to **`m22-lab`** (rc1, export allowance on, v1 kit migrated) and started. `manifest/get` reported **2.2.0rc1**. The rc1 Today so far sensor showed `offset_w` −14.8, `offset_corrected` None, `offset_corrected_intervals` 0 |
| Install | `git archive` of `3c7e136`'s `custom_components/amber_energy_dashboard` over SSH (`sudo tar`). **Checksum:** sha256 of the 33 files' sha256 list, local `6aa25fde295b6a7f…` = clone `6aa25fde295b6a7f…`, with 33 files on each side. `6ae71e1` changes only a report, so the installed code is the released code |
| Restart | Restarted through the REST API; `RUNNING` 42 s later. `manifest/get` reported **2.2.0** |
| Warnings | `ha core logs` since the boot: 4 WARNING lines, none from the integration. Two are HA's standard "custom integration … not been tested"; one is SQLite not shut down cleanly (after the snapshot rollback); one is the cancelled `homeassistant.restart` API call (the restart cut off its own request). No ERROR lines |
| Today so far (read-back) | `sensor.amber_energy_dashboard_today_cost_estimated`: state 0.0, `energy_kwh` 0.0, **`raw_estimate` 0.0, `raw_energy_kwh` 0.0**, `intervals` 2, **`offset_corrected_intervals` 2**, `offset_w` −14.8, `yesterday_estimate_vs_billed` null. The rc1 attributes are gone |
| Bill to date (read-back) | `including_today_estimated` None, **`including_today_estimated_raw` None**, `including_today_leaves_out` ["feed-in"]. They are None because the lab's billed data runs through an earlier day |
| Store upgrade (read-back via SSH) | The saved Today state after the restart: the side has the new keys (`estimate_amount`, `estimate_kwh`, `corrected_intervals`) and no `corrected_kwh` / `corrected_amount`. The rc1 day was carried into the history as 2026-10-25 in the new layout |
| End | Shut down, then snapshot **`m23-220`**. **`m22-lab` is kept** |

**Limits of the lab check:**
- The lab meter's stand-in sensors are constant, and the meter has an import side only.
  So the energies are 0 and the negative offset clips at zero: the lab shows the
  attributes, the fallback count and the store upgrade, but not a corrected amount.
- The arithmetic is covered by the tests in 1.4.
- The rc1 history day had no billed record, so the comparison stayed null.

## 4. Open questions assigned to the milestone

- **Apply the offset?** Decided in the planning chat from the production figures above.
  Implemented as item 1.
- **Fast-forward:** `main` (`0b63969`) and `v2` (`fd8a103`) were both ancestors of
  `feature/v2.2`. Both were fast-forwarded with `git merge --ff-only`, with no merge
  commit.

## 5. Deviations, decisions and points for the planning chat

1. **An extra commit before the tag** (`6ae71e1`, section 1.3). The tag is on that commit,
   not on `3c7e136`. CI was green on `6ae71e1` before the fast-forward.
2. **For you: the history still contains the host name** (commit `3b8f98c`,
   `reports/M22-report.md`). Removing it would need another rewrite like the one on
   2026-10-23, now across `main`, `v2` and the tags. I didn't do that.
3. **Also in the history, older and not new here:** `reports/M1-report.md`, `M2-report.md`
   and `M3-report.md` contain lab clone addresses (private 192.168.x addresses of
   disposable clones). They have been on `main` since 2.0, and the 2026-10-23 rewrite
   kept them. They are not hostnames, but say if you want them replaced.
4. **The fallback for an unknown offset is per interval**, so a day's estimate can mix
   corrected and raw intervals; `offset_corrected_intervals` shows how many were
   corrected. An rc1 save with a partly corrected day is upgraded in proportion. This is
   exact for full days (production) and approximate only for a day that was partly
   without an offset under rc1.
5. **Attribute renames since rc1**, as asked ("rename so it's clear which is which"):
   `estimated_corrected`/`error_corrected` and `estimated_raw`/`error_raw`, each with
   `_percent` and `_kwh`. On the sensor: `raw_estimate`, `raw_energy_kwh` and
   `offset_corrected_intervals`. The release notes and CHANGELOG list them.
6. `docs/cards/bill-card.yaml` uses `including_today_estimated`, which is now the
   corrected figure; the card needed no change.

## 6. Live Amber API calls

With the test key, from 9102 only, between 08:38 and 08:51 Sydney time (outside
07:17–07:27). The test suite makes none.

- **`/sites`: 2**, one at boot (08:38) and one at the restart onto 2.2.0 (08:39).
- **`/usage`: not counted.** The snapshot's last import was on 2026-10-25, so the boot
  and the restart may each have caught up a day of data. The integration keeps its call
  count in memory only, and I didn't read diagnostics before shutting down. By earlier
  milestones, this is a few calls at most.

## 7. Lab state left behind

- **9102 `amber-test-m3`: stopped**, at snapshot **`m23-220`**: 2.2.0 installed over
  `m22-lab`, where it last ran.
- **`m22-lab` is kept**, for retaking screenshots 18 and 19. Earlier snapshots are kept.
- The pool has 9000 (template, untouched) and 9102. No other clones.
- VM 101 was not touched. No `.claude_work` folder was created. SSH add-on protection
  was not touched.
- **Git:**
  - `feature/v2.2` is deleted locally and on GitHub;
  - local `main` and `v2` match origin;
  - this report is committed on `main`.

## 8. Recommended next steps

1. Publish the GitHub release for `v2.2.0` from `reports/release-notes-v2.2.0.md`, and
   check that HACS offers 2.2.0. I didn't create a GitHub release, because that wasn't
   part of this milestone.
2. In production, after updating: the next morning's `yesterday_estimate_vs_billed`
   should show `error_corrected` near −1 %. The first day after updating is a 2.2.0rc1
   day upgraded on load (fully corrected in production, so exact).
3. Decide on the history question (section 5, points 2 and 3).
4. Retake screenshots 18 and 19 from `m22-lab` if wanted.
5. 2.3: the battery cost basis and the built-in card.
