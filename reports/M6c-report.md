# Milestone 6c report: M6b review decisions, 2.0.0-rc2

Date: 2026-09-28. Branch `v2`. Code and tests only: no lab contact and no live Amber
calls. No tag was created.

**Summary.** The four accepted decisions from the M6b review (report section 5) are
implemented:
- **Item 3, retention:** a fixed boundary far in the past now converges.
- **Item 4, scan:** the energy cap is per elapsed hour.
- **Item 7:** `probe_retention` added, and `rebuild_from_anchor` dropped from the design.
- **Item 2** (the rolling-boundary fallback) was already in rc1 and is unchanged.
- **Item 6:** the brand icon stays.

The manifest is `2.0.0-rc2`, and the CHANGELOG is updated. **379 tests pass, with 100 %
coverage** (3257 statements). ruff is clean, and local hassfest reports 0 invalid.
**CI on `748a2e9`: hassfest, HACS validation and Tests all succeeded.**

## 1. What was built

| Commit | What |
|---|---|
| `8199c18` | Retention: converge on a fixed boundary far in the past |
| `ec283a2` | The `probe_retention` service |
| `bace38e` | Migration scan: energy cap per elapsed hour between rows |
| `748a2e9` | 2.0.0-rc2: DESIGN sections 8, 12 and 14, README, CHANGELOG, manifest |
| (this commit) | This report |

### Item 3: retention with a far-back boundary (`manager.py`)

- **Discovery.**
  - `_search_boundary` and `_search_older` now return `(boundary, bracketed)`.
  - When the 30-day bracket below today − 89 still has data, the bracket end (today −
    120, the oldest day seen with data) is returned unbracketed. It is stored with
    method **"oldest day seen with data (not bracketed older)"** (3 calls).
  - The fallback to today − 89 is kept for the other cases: errors, the budget, the call
    cap, and "nothing newer has data".
- **Daily check, backward move beyond the 15-day bracket.**
  - It now accepts the bracket end (boundary − 17 days, known to have data) as the new
    boundary: **"moved back at least N days"** (4 calls, `needs_discovery` false).
  - The next day continues from there. Once the boundary falls inside the bracket, it is
    bisected (at most 8 calls), and from then on verified with 2 probes.
- **Unchanged:** a forward move beyond the bracket still tries the rolling candidate,
  then becomes `unresolved` and triggers discovery the next day.

### Item 4: scan cap (`migration.py`)

- **Energy:** a step above 100 kWh per elapsed **hour** between the row and the last good
  row, with at least one hour's cap. A daily lump may now hold up to 2400 kWh (2300 or
  2500 across a DST change).
- **Unchanged:** the decrease tolerance (0.0005 kWh), the cost cap (100 per row, scaled
  by days), and the spike/jump/reset lookahead.

### Item 7: services (`__init__.py`, `manager.py`, `services.yaml`, strings, icons)

- **`amber_energy_dashboard.probe_retention`** (optional `config_entry_id`, optional
  response): `AmberManager.async_probe_retention()` runs a fresh discovery under the run
  lock, with its own budget of at most 8 calls.
  - It returns `boundary`, `retention_days`, `method`, `calls` and `previous_boundary`.
  - **On failure, the stored boundary and its record are restored**, and a
    `HomeAssistantError` is raised. This covers an API error, the rate-limit budget, the
    call cap, and auth (which also starts reauthentication).
  - The service is registered in its own function, `_register_retention_service`.
- **DESIGN section 12:** `rebuild_from_anchor` is removed, with a note (backfill with the
  guarded tail rewrite covers it). The list now also shows `run_now` and `import_day`.

### Documentation

- **DESIGN section 8:**
  - the discovery rule for "not bracketed older";
  - the backward move beyond the bracket, which replaces the M4 "unresolved → discovery"
    for that direction and says why;
  - the `probe_retention` behaviour.
- **DESIGN section 14:** the per-hour cap, with the 150 kWh example.
- **README:**
  - the `probe_retention` line under Services;
  - the retention bullet ("If the date moves, it follows it");
  - the scan bullet ("more energy between two rows than 100 kWh per hour between them").
- **CHANGELOG:** `2.0.0-rc2` (Added, Changed, Removed). The rc1 entry now states rc1's
  actual cap rule.

## 2. Test results

`uv run pytest --cov`: **379 passed, 0 skipped, 0 failed** (265 s), with **100 %** of 3257
statements. ruff check and format are clean. hassfest (local): 0 invalid.

**New:**

| Test | What it checks |
|---|---|
| `test_fresh_setup_fixed_boundary_far_back_converges` | **fixed boundary 250 days back, fresh setup.** Discovery stores today − 120 ("oldest day seen with data", 3 calls). It converges in **8 days**: 7 steps of "moved back at least 17 days", then "moved back 11 days (bisected)". Every day is within 8 calls and the status is `caught_up`. The next 5 days are each `verified` with **exactly 2 probes plus the new day's fetch, and nothing more** |
| `test_retention_move_back_beyond_bracket_converges` | replaces the M4 test "move beyond bracket triggers discovery". A stored boundary 69 days too new converges as 17, 17, 17, then "moved back 1 day", then "verified", with no discovery |
| `test_probe_retention_forces_fresh_discovery` | the exact response; 2 probes; the store is updated and verified today; the next run makes no further check |
| `test_probe_retention_failure_keeps_stored_boundary` [server, auth, budget, cap] | the boundary and the retention record are unchanged; a clear error; a reauth flow only for auth |
| `test_scan_energy_cap_is_per_elapsed_hour` | a **150 kWh daily row is not flagged**; 150 kWh in one hour is; the **99999 spike is flagged**; a 3-day gap accepts 7000 kWh |
| `test_v1_large_household_day_is_not_flagged` | a real-shaped v1 history with a **150 kWh day** before the boundary: 0 flagged, parity passes, migration completes, and the day is copied with state 150 |

**Updated:**
- `test_retention_bisection`: the 130-day case now expects today − 120, "oldest day
  seen".
- `test_scan_series_kinds_and_rederived_sums`: hourly rows, since a 600 kWh daily step is
  now plausible.
- `test_pricing_mode_writes_no_usage_but_prices_and_own_cost`: its preset boundary had
  data before it, so the check now moves back to the bracket end, and the price series
  starts there (20 days).
- The M6b tests of the 99999 spike (v1 README case, copy period) and the +5000 jump pass
  unchanged.

## 3. Lab verification

None, as instructed: no lab contact, no VMs touched, and no live calls. The behaviour
above is verified by tests only.

## 4. Open questions assigned to this milestone

None beyond the decisions implemented here.

## 5. Deviations and proposed design changes

1. **`probe_retention` also restores the stored boundary when discovery hits the call cap**
   (`fallback (_DiscoveryIncomplete)`), not only on API errors. A forced probe should
   never replace a known boundary with a guess.
2. **The cost cap is left as it was** (100 per row, scaled by days), since "the other
   thresholds stand". A per-hour cost cap would be the parallel rule, if you want it.
3. **A side effect to know about:** a stored boundary that is too new, with data before
   it, is now always followed back by up to 17 days a day. That is the intended fix. It
   means an install whose stored date came from the old fallback (today − 89, for example
   after an API error at setup) also corrects itself over the following days. It never
   costs more than 8 calls a day.

## 6. Live Amber API calls

**0.**

## 7. Lab state left behind

Unchanged from M6b:
- **VM 9102** is running `2.0.0-dev5`; it was not touched.
- **VM 101** was not accessed.
- No other clones exist.
- Repository: `v2` is pushed; no new tag (`v2.0.0-rc1` is unchanged); `main` was not
  touched.

## 8. Recommended next steps

1. Robert: sign off rc2, then tag `v2.0.0-rc2` (not done, as instructed).
2. Upgrade VM 9102 in place to rc2, which checks the 1.2 → 1.3 store migration on a real
   file. Then watch one scheduled run: expect `verified`, with 2 probes.
3. Run `probe_retention` once on 9102, outside the quiet windows. Expect boundary
   2026-06-29 if Amber still holds it.
4. The HACS install test from the custom repository (M6b report section 8).

---

## Addendum: M6c review decisions, 9102 upgrade to rc2, and the v2.0.0-rc2 tag

Date: 2026-09-28, 13:20 to 13:50 AEST. The review decisions on section 5: items 1 and 3
accepted, item 2 changed (below).

### A.1 Cost cap: 500 per elapsed hour (commit `e416022`)

- **`migration.py`:** `COST_ROW_CAP` is now **500 per elapsed hour** between the row and
  the last good row, with at least one hour's cap, the same rule shape as energy. A cost
  step is flagged only when it is an **increase** above the cap. **A cost decrease is
  never flagged**; before, a fall of more than 100 in one row was.
- **Tests:**
  - New `test_scan_cost_cap_is_per_elapsed_hour`:
    - a real-shaped hourly series (about 0.30 an hour, then one **150.50 spike hour**,
      8.6 kWh at 17.50/kWh): **not flagged**, and the rows are unchanged;
    - 500.01 in one hour is flagged; 999 over two hours is not;
    - **the 99999 spike is flagged**, in hourly rows and in daily rows (a daily cap is
      12000);
    - **decreases are never flagged**: negative-price hours, a long fall, a drop of 520
      in one hour, and 30 hours of Amber-sign compensation falling.
  - New `test_advanced_price_spike_hour_is_copied`: advanced-layout hourly history
    before the boundary with **+150 import cost in one hour** and a **negative-price hour
    (−40)**. 0 flagged, parity passes, the migration completes, and both hours are copied
    with their exact states.
  - Updated `test_scan_series_kinds_and_rederived_sums`: its cost spike is now 99999 (a
    160 hour is now plausible).
- **Docs:**
  - DESIGN section 14: the cost rule, with the reason (wholesale spikes) and the
    decrease rule;
  - README scan bullet: the cost line;
  - CHANGELOG rc2: the cost cap entry; the rc1 entry now states rc1's actual cost rule.
- **Results:** `uv run pytest --cov=custom_components`: **381 passed**, 0 skipped, **100 %**
  of 3255 statements. ruff check and format are clean.
- **CI:** hassfest, HACS validation and Tests all **succeeded** on `e416022`, and again on
  `a588474` (the release notes commit, which is the one tagged).

### A.2 VM 9102: in-place upgrade 2.0.0-dev5 → 2.0.0-rc2 (M4 section 9 procedure)

Run at 13:36 to 13:38 AEST: outside the quiet windows, and more than 10 minutes from
9102's 13:15 attempt. The script enforced both.

| Step | Evidence |
|---|---|
| Preconditions | `local-lvm` 91.8 % free; 1 clone (9102). Status sensor `caught_up` (not running). Repairs: none |
| Snapshot | **`pre-rc2`** (no RAM), task `OK` in 2 s. Description: "M6c: 2.0.0-dev5 (store 1.2), before in-place upgrade to 2.0.0-rc2". **Kept**, as instructed |
| Before | manifest `2.0.0-dev5`; **store file 1.2** with `retention_days` 91 and no `retention_boundary`; the retention record holds `boundary` 2026-06-29 ("moved back 1 day", 3 calls, by the 07:15 run); marker and last_written 2026-09-27; fixed schedule 07:15, 10:15 and 13:15. Statistics: 2184 hourly rows for each of the 5, from 2026-06-28T14:00Z to 09-27T13:00Z, e9 final sum 1352.933, no duplicates and no sum breaks |
| Install | v2 HEAD `e416022`: 20 files over SSH, **checksums match**. Restart; `RUNNING` after 12 s (HA 2026.9.3); integration loaded; setup made 1 `/sites` call |
| **Store migration 1.2 → 1.3** | The file on disk (`/config/.storage/amber_energy_dashboard.<entry>`) now reads **version 1.3**, with **`retention_boundary` "2026-06-29"** (taken from the record's boundary, as DESIGN section 8 says) and **`retention_days` removed**. The in-memory `retention_days` (derived) is 91, the same as before. The marker, last_written and retention record are unchanged. Repairs: none |
| `run_now` | `caught_up`, **0 calls** (today's check was already done at 07:15; nothing new to import). The retention record is still `last_verified` 2026-09-28 |
| After | **All 2184 pre-existing rows are identical** in all 5 statistics; 0 new rows; no problems. The schedule is unchanged (fixed 07:15, 10:15 and 13:15; next run 2026-09-29 07:15) |

No rollback was needed.

### A.3 `probe_retention` on 9102 (13:38 AEST)

The response, verbatim:

```
{"boundary": "2026-06-29", "retention_days": 91, "method": "bisection",
 "calls": {"sites_usage": 8}, "previous_boundary": "2026-06-29"}
```

- **As expected: boundary 2026-06-29**, the same as the stored one.
- The probes, in order: 07-01 data, 06-30 data (today − 89 and the day before, both with
  data, so it searched older), 05-31 empty (the 30-day bracket end), then bisection:
  06-15, 06-22, 06-26 and 06-28 empty, and 06-29 with data. That makes 8 calls, the cap,
  and it was bracketed.
- The store file afterwards has `retention_boundary` 2026-06-29, method `bisection`,
  `last_verified` 2026-09-28. All 2184 rows are still identical.

### A.4 Tag and release notes

- **`v2.0.0-rc2`** is an annotated tag on **`a588474`** ("Add v2.0.0-rc2 release notes"),
  pushed. rc1 followed the same pattern, with the tag on the commit that holds its
  release notes. `v2.0.0-rc1` is unchanged, and **`main` was not touched** (`cc179b7`).
- **`reports/release-notes-v2.0.0-rc2.md`**, for the GitHub pre-release, covers:
  - what's new since rc1 (`probe_retention`, far-back retention, the scan caps);
  - upgrading from rc1 and from the dev builds;
  - the known limitations;
  - a link to the CHANGELOG at the tag.

  The GitHub release itself was not created, since there is no `gh` or token here.

### A.5 Notes for the planning chat

1. **The discovery record keeps `previous_boundary: null`.** Discovery always writes
   `previous_boundary: None` into the stored retention record, so after a
   `probe_retention` the diagnostics do not show the earlier date (the service response
   does). No warning is logged if a probe moves the boundary. This is cosmetic. A
   possible fix is to pass the previous boundary into the record. **It is not changed
   before the tag.**
2. **9102's 07:15 run** moved the boundary back 1 day, to 2026-06-29 (91 days). The
   later probe confirmed it.

### A.6 Live Amber API calls

**9 in this addendum**, all at 13:37 to 13:38 AEST (logged, count only):
- 1 `/sites` at setup after the restart;
- 0 in `run_now`;
- 8 in `probe_retention`.

The M6c total is 9.

### A.7 Lab state left behind

- **VM 9102** is running **`2.0.0-rc2`** (tree `e416022`, the same as the tag's), in store
  layout 1.3. **Snapshot `pre-rc2` is kept** until you sign off; the M5 snapshot `pre-m5`
  was already deleted. No scratch files were left on it.
- VM 101 was not accessed. No other clones exist.
- Repository: `v2` and the tag `v2.0.0-rc2` are pushed.

### A.8 Recommended next steps

1. Watch 9102's 2026-09-29 07:15 run: expect retention `verified` with 2 probes, 1 new
   day, and continuous sums. Then delete `pre-rc2`.
2. Create the GitHub pre-release from the tag, with the release notes.
3. The HACS install test from the custom repository (M6b report section 8).
4. Decide on note A.5.1.

---

## Addendum 2: VM 9102's first scheduled run on rc2 (2026-09-29 07:15)

Checked at 07:49 AEST, read-only: diagnostics, the store file, the recorder statistics and
Repairs. No Amber calls were made for the check.

| Check | Expected | Observed |
|---|---|---|
| Run | the scheduled 07:15 run | `trigger` scheduled, started 07:15:00, finished 07:15:02, status **`caught_up`** |
| Retention | `verified`, exactly 2 probes | **`verified`, 2 calls**: 2026-06-29 has data, 06-28 is empty. The boundary is 2026-06-29 (92 days, derived); `previous_boundary` 2026-06-29 |
| New day | 2026-09-28, 1 day | **`imported_days` ["2026-09-28"]**; marker and last_written 2026-09-28 |
| Calls | 2 probes + 1 window | **`sites_usage` 3**; `RateLimit-Remaining` 42. `requests_since_start` is 12: 1 setup, 8 probe, 3 this run |
| Revisions | nothing to check | checked `[]`, changed `[]`, 0 rewritten |
| Statistics | continuous, no gaps or duplicates | **2208** hourly rows for each of the 5 (2184 + **24**), 2026-06-28T14:00Z to 09-28T13:00Z. **0 hour gaps, 0 duplicates, 0 sum breaks.** All 2184 earlier rows are identical to the pre-upgrade read |
| Seam and day totals | sums continue from 09-27 | e9 energy 1352.933 + 0.371 = 1353.304, day **20.826 kWh**, final 1373.759. e9 cost day 4.749026, final 369.608181. b9 energy day 3.471, final 982.531. b9 compensation day 0.192706, final 23.685528. Net cost day 4.556320 (= 4.749026 − 0.192706), final 345.922653 |
| Repairs | none | **none**; status sensor `caught_up` |
| Store file | 1.3 | 1.3, `retention_boundary` 2026-06-29, no `retention_days` |
| Schedule | unchanged | fixed 07:15, 10:15 and 13:15; next run 10:15 |

**All checks passed, so snapshot `pre-rc2` was deleted** (task `OK` at 07:49). VM 9102's
only snapshot entry is now `current`.

**Live Amber calls:** 0 by me. The integration's own scheduled run made 3.

**Lab state:**
- VM 9102 is running `2.0.0-rc2`, with no snapshots.
- VM 101 was not accessed. No other clones exist.
