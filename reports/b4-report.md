# 2.2.0b4 report: solar sensors offline at night; beta 4

Date: 2026-10-25 (Sydney). Branch `feature/v2.2`, after M21 (accepted). Tagged
`v2.2.0b4`; nothing is merged. **Stopped for review.**

## 1. What was built

**The bug.** In production (b3) the solar-aware chain skipped every day with "no complete
statistics … from <solar energy sensor>".
- The Fronius inverter is unavailable at night, so Home Assistant records no statistics
  for those hours.
- b3 read every series with the grid meters' and appliances' strict rule: a missing row
  refuses the day.

**The fix** (`solar.py`), for the site's **solar** source only:
- **Energy sensor** (`fill_solar_sums`): a run of missing buckets counts as zero
  production when either:
  - (a) the sum is unchanged across it (the first row after it has the last row's sum,
    within 0.1 Wh); or
  - (b) it is outside daylight.

  The change an energy sensor reports when it comes back stays in the bucket Home
  Assistant recorded it in.
- **Power sensor** (`fill_solar_means`): missing buckets count as 0 W when either:
  - the gap is outside daylight; or
  - the sensor reads at most 1 W on both sides of it.
- **Outside daylight** (`daylight_test`, HA's home location and sun):
  - the gap doesn't reach sunrise + 1 h to sunset − 1 h;
  - it may end up to **3 h after sunrise**;
  - it may not **start** inside that core.
- Rows are read 24 h either side of the day, to find the rows around a gap (a NEM day
  starts and ends at night).
- **Any other gap is refused, as today: no spreading.** A day skipped this way still
  says "no complete statistics … from <sensor>".
- Grid meters and appliances keep the strict rule (`chains.async_sensor_energy` is
  unchanged).

**Recalculation** (`manager.py`, `storage.py`):
- Once per solar-aware chain (`RETRY_SOLAR_GAPS`, recorded as the chain's `retried`), the
  days skipped for sensor data (`skipped_sensor_data`, from the retention boundary on)
  are worked out again.
- **How:** a chain rewrite from the first such day to the marker, flagged `retry`.
  - The rewrite now keeps extra keys in its progress. With `retry` it also writes
    `skipped_sensor_data` days.
  - Every later day is rewritten, so the sums are re-derived.
  - It is resumable like any rewrite. The days now written are listed in the run's
    chain result as `recalculated`.
- A solar-aware chain's interrupted rewrite is no longer resumed while the site lacks its
  flows. Before, that would have reached an assertion.
- `_async_solar_aware_ready` holds the solar-aware waiting check, which moved out of the
  chain walk.

**Also in the beta** (M21, accepted):
- the diagnostics redaction fix;
- the boundary-move log level;
- the strings;
- `hacs.json` country;
- the docs.

**Commits:**

| Commit | What |
|---|---|
| `ba49dfd` | Solar-aware cost: solar sensors that go offline at night (code, tests, INSTALL.md, DESIGN section 22) |
| (this) | 2.2.0b4: manifest, CHANGELOG, release notes, this report |

## 2. Test results

- **Full suite, 9102 stopped:** `uv run pytest --cov=custom_components.amber_energy_dashboard`
  gives:
  - **606 passed**, 0 skipped, 0 failed (8 min 21 s);
  - **100 % coverage** (6,129 statements);
  - 4 warnings, the harness's known `ResourceWarning: unclosed database`.
- `ruff check`, `ruff format --check` and `uv lock --check` are clean.
- **New: `tests/test_solar_gaps.py`** (11 tests):
  - **The rules, with a stand-in daylight test:**
    - a night gap with the change landing after it;
    - an unchanged sum accepted in daylight;
    - a changed sum in daylight refused;
    - a gap still open at the day's end;
    - a leading gap using the row before the day;
    - no row before the gap (refused);
    - the span passed to the daylight test;
    - for power: zero at night, "nothing either side", and missing at both ends.
  - **The daylight test at Sydney** (real sun times):
    - a night gap ending 2 h after sunrise is accepted;
    - one ending 4 h after sunrise is refused;
    - a gap starting in daylight is refused, however short;
    - a whole day offline is refused;
    - a day without sun events counts as daylight.
  - **A site with a realistic overnight-offline solar energy sensor** (rows only from the
    first to the last producing interval):
    - both days written, matching the independent sum exactly;
    - the same on the hourly (lower precision) path.
  - **A daytime dropout** (11:00–12:00 offline while producing, the lump after it): that
    day is skipped, naming the solar sensor.
  - **A solar power sensor** with no means at night: written, matching exactly.
  - **The recalculation, parametrised:**
    - the setup: under b3's rule (patched in) "all" days are skipped, or the "first" is
      skipped with a later day written;
    - the next run with a b3-style chain record (no `retried`) writes them;
    - every hourly state and the running sum match;
    - a further run doesn't retry.
  - An interrupted retry doesn't resume without the flows, and resumes once they're back.
  - A retry with nothing skipped only records itself.

## 3. Lab verification (VM 9102, HA 2026.10.0, home location Sydney)

From `m21-lab` (snapshot `m22-before` first):
1. **Installed `v2.2.0b3`.**
   - Two template stand-ins: "Lab solar energy" and "Lab pool pump energy" (energy,
     total_increasing).
   - **Hourly statistics, imported:**
     - **solar:** rows **only 05:00–18:00 AEST** (the inverter online) for 10-15 to
       10-23, with production 06:00–17:00 and nothing at night;
     - **pump:** every hour, 30 % of the grid meter's import, 10-16 to 10-23.
   - Export allowance turned off (to keep the hand check simple).
   - Sources solar energy = the stand-in.
   - Appliance "Lab pool pump" with solar-aware cost.
2. **Run now on b3:**
   - **every day 10-16 to 10-23 skipped:** "no complete statistics for … from
     sensor.lab_solar_energy". That is production's symptom.
   - 10-24 was skipped for the pool pump, which has no statistics that day (correct:
     the strict rule).
   - Snapshot `m22-b3-skipped`.
3. **Installed the fix** (the working tree at `ba49dfd`; the sha256 of `solar.py` matched)
   and restarted. **Run now:**
   - `recalculated` 10-16 to 10-23 (8 days);
   - 10-24 still skipped (pool pump);
   - 2 `/usage` calls.
4. **Independent check:**
   - The same days' records were fetched directly (2 calls).
   - Each hour was recomputed by hand, from the stand-ins' hourly deltas:
     - night solar = 0;
     - load = I + S − E;
     - solar share;
     - the hour's mean general price, and the mean −perKwh of feed-in.

   | NEM day | Written | Expected |
   |---|---|---|
   | 10-16 | 0.6779 | 0.6779 |
   | 10-17 | 0.5329 | 0.5329 |
   | 10-18 | 0.7406 | 0.7406 |
   | 10-19 | 0.6243 | 0.6243 |
   | 10-20 | 0.8821 | 0.8821 |
   | 10-21 | 0.7358 | 0.7358 |
   | 10-22 | 0.8865 | 0.8865 |
   | 10-23 | 0.8680 | 0.8680 |

   - The largest hourly difference was **4.95e-7** AUD (the hourly state's rounding).
   - **Totals:** 5.948101 against 5.948110.
   - **10-23:** `solar_kwh` 1.118 of 3.6 kWh, `lower_precision` true (hourly
     statistics).
5. **A second Run now:** 0 calls, no retry. The chain records `retried`
   `solar_gaps_2.2.0b4`, with no rewrite pending. Nothing from the integration in the
   log.
6. Snapshot `m22-b4-lab`; 9102 shut down.

## 4. Decisions for the planning chat

1. **The 3-hour wake allowance** goes beyond "outside daylight".
   - **Why:** an inverter that wakes after sunrise + 1 h (a dull or east-shaded morning)
     reports its first production in the same row it comes back in. A strict window
     would refuse those days, especially on the hourly path, where the sunrise hour
     always carries production.
   - **The cost:** a gap that starts at night and ends within 3 h of sunrise is accepted
     even if it hid some early production. That production is still counted, in the
     return interval, not lost.
2. **Gaps that start in daylight are refused** unless the sum is unchanged, however
   short. That is a lost connection while producing.
3. **"Spread or refuse":** refused, as b3 did. The day is skipped with the same reason.
4. **Daylight uses HA's home location** (it is the user's site in practice). The site's
   location isn't known to the integration.
5. **The recalculation retries every `skipped_sensor_data` day** of the chain within
   retention, once. Days that still fail stay skipped and aren't retried again.
   - **Cost:** about `ceil(days / 7)` usage calls the first time (production: about 13
     for 89 days), within the run's budget; the rewrite resumes if the budget stops it.
6. **CHANGELOG:** a short `[2.2.0b4]` entry sits above the consolidated `[2.2.0] -
   Unreleased` entry. The consolidated entry also mentions night gaps. At the release
   the b4 entry merges into it.

## 5. Live Amber API calls

**12**, all with the test key, 07:42 to 07:48 Sydney time (after production's window):
- **`/usage`, 9 counted:**
  - b3 Run now: 5 (10-24 imported, retention, the chain's windows);
  - b4 Run now: 2;
  - the hand check: 2.
- **`/sites`, about 3:** the boot, and the restarts onto b3 and b4. The options and
  sub-entry changes reloaded too; those were not counted.

The second Run now made none. The suite makes none.

## 6. Lab state left behind

- **9102: stopped**, at snapshot `m22-b4-lab`:
  - the fix installed;
  - "Lab solar energy" and "Lab pool pump" (solar-aware);
  - the export allowance **off**;
  - Sources with the stand-in price and solar.
- **New snapshots:** `m22-before` (= `m21-lab`), `m22-b3-skipped` and `m22-b4-lab`.
- No other clones. VM 101 was not touched. No `.claude_work` folder.

## 7. Tag and CI

- **Tag `v2.2.0b4`:** annotated, tag object `2d20aac`, on commit `27bba71` (the release
  commit; this report's CI addendum comes after it on the branch).
- **CI on the tag:** Tests 40132197277, hassfest 40132197279 and HACS validation
  40132197273, all **success**.
- **CI on the branch push of the same commit:** Tests 40132193993, hassfest 40132193916
  and HACS validation 40132193903, all success.

## 8. Next steps

1. **Production, after updating to b4:**
   - press Run now;
   - the solar-aware statistic should cover the skipped days;
   - `Yesterday solar-aware cost` should show a sensible `solar_share`.
2. Keep confirming b3/b4 for the 2.2.0 release candidate.
