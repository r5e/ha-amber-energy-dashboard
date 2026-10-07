# Milestone 8 report: 2.0.1, legacy gaps in the migration parity check

Date: 2026-10-07. Branch `fix/parity-legacy-gaps` (from `v2` at `c1cb4ae`), pushed. No
merge to `main` and no tag.

**Summary.** A real v1 migration refused with "Parity (daily rules): 86 days
(2026-07-13 to 2026-10-06), FAILED: 32 daily totals differ". Every listed difference had
legacy 0.0 kWh for both import and export from 2026-09-21: the kit had stopped importing
after 2026-09-20 but kept writing rows. Parity now classifies such days as **legacy
gaps**, reports them explicitly, and fails only on genuine disagreement.

- **401 tests pass, 100 % coverage** (3415 statements); ruff check and format are clean.
- The field case is reproduced by a test: on 2.0.0 it fails with exactly
  "32 daily totals differ"; on this branch it passes.
- **CI on `c3f7073`: Tests, Validate with hassfest and HACS validation all succeeded.**
- No live Amber calls were made. No lab VM was used.

## 1. What was built

| Commit | What |
|---|---|
| `9e3d46f` | Parity: legacy gaps, catch-up lumps, categorised failures; dry-run and result text; tests |
| `c3f7073` | 2.0.1: manifest, CHANGELOG, README version line, DESIGN section 14, MIGRATION.md note |
| (this commit) | This report |

### Where the 2.0.0 check went wrong

`_legacy_daily` turned each legacy series into daily totals, and `_parity` compared every
day in both sets. A stalled v1 kit (its REST sensor fails, so the automation adds
`float(0)` each morning and writes an unchanged sum) produces a 0.0 total every day, so
each day after the stall counted as an import difference and an export difference:
16 days × 2 = 32. A kit that stopped writing rows altogether already passed, silently,
because days without rows were not in the intersection. Neither case was reported.

### The new check (`custom_components/amber_energy_dashboard/migration.py`)

- `_legacy_ends`: each day's last cumulative sum per legacy statistic. `_total(ends,
  first, last)` gives the legacy total over a span of days (end of `last` minus end of the
  day before `first`). `_legacy_daily` is now built from it (used for the manual-pick
  export sign).
- **Overlap window**: the integration's complete days from the legacy import series' first
  comparable day. So it now includes days after the kit stopped (`days` in the report).
- `_gap_days`: a day is a legacy gap when there is **no legacy import row** that day, or
  the legacy import **and** export totals are exactly 0 (below 0.0005 kWh, so they round
  to 0.000) **while our import is above 0**. A missing export series counts as 0.
- `_runs` and `_classify_gaps`: consecutive gap days form a run.
  - **trailing**: no compared day after it (the kit stopped);
  - **interior**: the next day's legacy total is first compared with ours for that day
    alone (the kit resumed without catching up). When the gap has no rows, that legacy
    total runs from the last row before the gap, which is the only number there is;
  - **catch_up**: if the day alone disagrees but the legacy total agrees with ours over
    the run plus that day, the day is a catch-up lump. Every statistic is then compared as
    one total over the whole span.
- `_agrees`: the 2.0.0 tolerances, scaled for spans. Daily rules: kWh within 0.01 per day
  of the span; cost reported only. Hourly rules: kWh exact to 3 decimals; cost within 0.01
  per day of the span.
- `_parity`: gap days are skipped. Every other day is compared as before (or as a span,
  for a catch-up). Per-statistic legacy spans and the "missing on one side" rule are
  unchanged. At least 3 **compared** days are still required.
- New report fields: `compared_days`, `gap_days`, `gaps` (each with `kind`, `first`,
  `last`, `days`, `kwh` (our import over the gap), `lump_day` for a catch-up, and `text`),
  and `mismatch_summary`. Mismatch entries gain `problem` ("differs", "missing on one
  side" or "catch-up total differs") and, for a span, `from`.
- Reasons: `"13 daily totals differ (grid import kWh differs: 1 day; grid export kWh
  differs: 12 days)"`, or `"only 2 comparable days (at least 3 needed); 2 days are legacy
  gaps"`. The service error and the options flow carry the summary.

### Dry run and result text

`format_report` (`_parity_lines`, `_mismatch_line`): the verdict line, then one line per
gap, then failures with a count header. Field case, as asserted by the test (dates
shifted to the test clock):

```
Parity (daily rules): 86 days (2026-07-02 to 2026-09-25), 70 compared and 16 legacy gap days, passed.
- The YAML kit stopped importing after 2026-09-09; 16 days (2026-09-10 to 2026-09-25) will come from the integration's own data.
```

Other gap texts:
- "The YAML kit has no data for A to B (3 days); the integration's own data covers them."
- "The YAML kit has no data for A to B (3 days) and caught up on C; compared as one total
  from A to C."

Failures: `Differences (10 of 13 shown):`, then one line each, for example
`- D, grid import kWh: differs (legacy …, integration …)` or
`- A to C, grid export kWh: catch-up total differs (legacy …, integration …)`. The raw
dicts printed by 2.0.0 are gone.
`format_result` (shown after a real run) repeats the gap lines.

### Copy, re-base and Energy dashboard with an early-ending legacy series (item 3)

No code change was needed. The copy takes only legacy rows before the boundary, and the
re-base continues from the copied carry row through the integration's own stored hourly
amounts. The Energy dashboard switch only swaps statistic IDs. None of them reads legacy
data after the boundary. The tests confirm it on real recorder data (below).

The implausible-row scan: 0-change rows are never flagged, and a v1 catch-up lump is
checked against 100 kWh per elapsed hour (2400 kWh for a day's spacing). See section 5
for one hourly-layout edge.

## 2. Test results

`uv run pytest --cov=custom_components.amber_energy_dashboard`: **401 passed** in 332 s
(389 in 2.0.0, plus 12 new). Coverage 100 % (3415 statements, 0 missed). Nothing skipped.
With `--cov-branch` (not used by CI) there are 11 partial branches in migration.py, the
same count as on 2.0.0.

New tests in `tests/test_migration.py` (section "legacy gaps (2.0.1)"), all through the
`migrate_v1` service on recorder statistics, with helpers `_entry_days` (an entry that
imported N days) and `_stall` (frozen rows or no rows, with or without a catch-up):

| Test | Shape | Asserts |
|---|---|---|
| `test_v1_kit_stopped_before_today` | **Field case:** v1 daily lumps, 86-day overlap, frozen rows from day 70 | Passes. 86 days, 70 compared, 16 gap days; one trailing gap with the exact text. Then a real run: completed; the integration's hourly states are unchanged over the whole window, the gap included; E1 and B1 continuous; seam baseline equals the last copied lump; end-of-gap sum = baseline + all our days; Energy dashboard switched. **Fails on 2.0.0 with "32 daily totals differ".** |
| `test_v1_interior_gap` ×4 | 3-day pause: {frozen rows, no rows} × {catch-up lump, resumed without catch-up} | `catch_up` or `interior` with the right dates and text; passes; the real run completes and stays continuous |
| `test_genuine_mismatch_beside_a_gap_still_fails` | Trailing gap, plus 12 export days +0.02 and 1 import day +0.05 | Fails with 13; summary by category; "10 of 13 shown"; real run refused; nothing written; no record |
| `test_catch_up_lump_that_disagrees_fails` | Lump +1 kWh on import and export | Classified interior; both lump-day values fail as "differs" |
| `test_catch_up_with_export_difference` | Import catch-up agrees, export lump +1 kWh | Catch-up; one "catch-up total differs" across the span, with `from` |
| `test_advanced_kit_stopped_with_an_earlier_pause` | **Advanced layout** (hourly, costs compared): a 1-day pause with frozen hourly rows and the catch-up in the next hour, then no rows for the last 3 days | Two gaps (`catch_up`, then `trailing`); costs compared on the 6 non-gap days; real run completes; E1, B1 and net continuous; compensation states kept; dashboard cost and compensation switched |

Changed tests: `test_too_few_overlap_days` now expects the gap suffix in the reason and a
trailing gap. `test_preconditions` also checks that the report text has no parity section
when it is refused before parity.

## 3. Lab verification

None. The change is confined to planning-time arithmetic and report text over recorder
statistics, which the tests exercise in a real Home Assistant test instance with the
recorder (pytest-homeassistant-custom-component, HA 2026.9.x). The copy, re-base and
energy-preference paths are unchanged and were verified in the lab in M6a to M6c.

If you want a lab check before release: a clone with the v1 kit, imported v1-shaped
statistics frozen for the last N days, then the dry run and a real migration. It would
need the integration's first run (live Amber calls) unless fixtures are injected.

## 4. Open questions assigned

None were assigned. Observation from the field report: a stalled v1 kit keeps writing
rows. The v1 automation does `states('sensor.amber_daily_grid_import') | float(0)`, so an
unavailable REST sensor (key revoked, API error) adds 0 and writes an unchanged sum every
morning. That explains the 0.0 totals rather than missing rows. This is an inference from
`legacy/v1/daily_automation.yaml`; your father's logs were not examined.

## 5. Deviations from DESIGN.md and proposed design changes

DESIGN section 14 (parity) is updated with the gap rules, the compared-days minimum, the
categorised report, and a note that a gap does not affect copy, re-base or the dashboard
switch. Choices for you to confirm:

1. **"Exactly 0" means below 0.0005 kWh** (it rounds to 0.000), so float noise in
   differenced sums cannot hide a gap.
2. **No legacy row = gap even if our import is 0** (nothing to disagree with). The
   0/0-rows case requires our import above 0, as you specified.
3. **Catch-up tolerance**: the per-day tolerance times the days in the span (daily kWh
   0.01 per day; hourly cost 0.01 per day). Hourly kWh stays exact to 3 decimals over the
   span.
4. **After an interior gap the day alone is tried first**, then the span. If both
   disagree, the day is reported on its own ("differs") and the gap is labelled interior.
5. **The minimum of 3 days counts compared days only.** A kit that stopped before the
   integration's first day therefore still refuses, now with "…; N days are legacy
   gaps". Its pre-boundary history would still be copyable, but nothing would vouch for
   it. Proposal: keep the refusal, or allow it with an explicit acknowledgement. Your
   call.
6. **Hourly-layout edge, not changed**: an advanced-version catch-up written as a single
   hourly row above 100 kWh after frozen hourly rows would be flagged by the M6b scan as a
   jump, and would block unless `exclude_flagged` is used. The advanced version writes
   hourly rows when it catches up, so I don't expect this in practice.
7. The parity report's `days` now counts the whole overlap window, gaps included.
   `compared_days` is the old meaning when there are no gaps.

## 6. Live Amber API calls

**0.**

## 7. Lab state left behind

No clones exist from this task. VM 101 was not touched. No files on any HA instance.

## 8. Recommended next steps

1. Review the branch; if accepted, merge `fix/parity-legacy-gaps` into `v2` and `main`,
   tag `v2.0.1`, and publish a release (release notes can follow the CHANGELOG entry).
2. Your father's install: after updating to 2.0.1, re-run the dry run. It should report
   the trailing gap from 2026-09-21 and pass. Then run it.
3. Decide on items 5 and 6 above for the planning chat.
