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
