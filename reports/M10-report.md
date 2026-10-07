# Milestone 10 report: bill estimate (2.1.0-dev2)

Date: 2026-10-08. Branch `feature/v2.1`, pushed. Not merged, no tag.

**Summary.**
- **Acceptance passes on real data:** VM 9102, billing day 28, the three charges, GST
  10 % → **$168.96** for 28 Aug – 27 Sep 2026 (target $168.96 ± $0.05).
- VM 9102 was upgraded in place from 2.0.0-rc2 to the branch build, following the M4
  section 9 procedure. Its 12,120 statistic rows were unchanged throughout.
- The options, the four sensors, the `bill_estimate` action and the optional "cost
  including fixed charges" statistic are implemented. The statistic was checked hour by
  hour on 9102's real history.
- **455 tests pass, with 100 % coverage**, and ruff is clean. **CI on `6af158e`: Tests,
  hassfest and HACS validation all succeeded.**
- The M9 review's item 8 is done: the stale FAQ entry and screenshot 07 are removed.

## 1. What was built

| Commit | What |
|---|---|
| `82a8f4f` | INSTALL.md: the pre-release HACS FAQ entry and `docs/images/07-hacs-choose-version.png` removed (M9 review item 8) |
| `7ef795d` | Bill estimate: options, sensors, `bill_estimate` action, and the statistic including fixed charges |
| `6af158e` | 2.1.0-dev2: README, INSTALL.md, CHANGELOG, DESIGN section 19 (as built) |
| (this commit) | This report |

**Item 8 (screenshots).** No remaining guide references image 07; only historical reports
mention it. I did not renumber files 08–20: they are about to be retaken, and renaming
would only churn links. So there is now a gap between 06 and 08. Say if you want them
renumbered.

### `bill.py` (pure logic)

- `cycle_for(day, billing_day)`: the cycle containing a day, from the billing day to the
  day before it next month. It handles year ends and February; billing days are 1–28 only.
- `BillSettings`: the billing day, the named charges (`network_daily`, `metering_daily`,
  `subscription_daily`, `other_daily`, AUD per day excluding GST), the GST percent
  (default 10) and the statistic flag.
- `estimate(...)`, computed from the Store's per-day records (exact totals, kept current
  by revisions):
  - **usage**: the sum of the cost statistics, on general and controlled-load channels;
  - **export credit**: the sum of feed-in compensation;
  - **fixed lines**: each charge × days covered × (1 + GST);
  - **bill to date** = usage − credit + fixed;
  - **projected** = (usage − credit) per imported day × days in cycle + all fixed charges
    for the cycle;
  - **average per day** = bill to date ÷ days covered.

  Attributes: cycle start and end, `days_in_cycle`, `days_into_cycle`, `data_from`,
  `data_through`, `days_elapsed` (days covered), `days_imported`, `missing_days`,
  `complete_from_cycle_start`, the GST rate and the lines.

### The edge cases from the brief

- **A cycle starting before the integration's data:** only imported days count. Fixed
  charges apply from `data_from` to `data_through`, and `complete_from_cycle_start` is
  false. The sensors show which day the data runs to.
- **A skipped day inside the covered range:** it is listed in `missing_days` and still
  bears its fixed charges (the bill charges every day).
- **Changing the billing day mid-cycle:** the estimate is recomputed at once, because
  nothing is stored per cycle. Turning the estimate on or off reloads the entry, which
  adds or removes its sensors; removed sensors are also deleted from the entity registry,
  not left unavailable.
- **DST:** cycle dates are **NEM days**, as Amber bills (its meter data and bill use AEST
  days). In summer a cycle starts at 01:00 local time, and the local hour 00:00–01:00 on
  the billing day belongs to the previous cycle. "Today" for days into cycle is the NEM
  date. A test pins this: at 00:30 AEDT on the billing day the old cycle continues; at
  01:00 the new one starts.
- **Pricing-only:** there is no usage, so the sensors are unknown, the action refuses, and
  the statistic is not written. **Recovery-only:** uses whatever days are imported.

### Options, sensors, action and statistic

- **Options flow:** a new menu entry, **Bill estimate**. It has billing day (1–28; empty
  turns it off), the four charges (`$/day`, any step), GST (%) and the statistic
  checkbox. **Fix found on the way:** the existing "Import settings" step rebuilt the
  options from scratch, which would have wiped the bill options. It now keeps them.
- **Sensors:** Bill to date, Projected bill (AUD, monetary), Days into billing cycle (d),
  Average cost per day (AUD/d). They are display only (no state class), with translations
  and icons.
- **Action:** `amber_energy_dashboard.bill_estimate(date)` returns the estimate for the
  cycle containing `date` (default today), from the Store's day records (about 400 days).
  It is how the acceptance cycle was computed after it had ended.
- **Statistic:** `…_e9_cost_incl_fixed` (general channel) is a **secondary chain**
  (`fixed`), so it shares revisions, tail rewrites, crash recovery and retention with the
  price series and own sensors.
  - Each hour = that hour's general cost from the usage records + the daily fixed charges
    including GST ÷ 24.
  - It starts at the retention boundary, so turning it on costs about 13 calls once.
  - The fixed amount is recorded per day. Changed charges apply from the next day written.
  - The own-sensor write path was factored into `_async_write_sum_chain`, which both
    chains now use.

## 2. Test results

`uv run pytest --cov=custom_components.amber_energy_dashboard`: **455 passed**, 0 skipped
(370 s). Coverage is 100 % for every file. That is 436 at M9, plus 19 in the new
`tests/test_bill.py`:

| Test | What |
|---|---|
| `test_cycle_for` ×7 | the acceptance cycle, the day it rolls over, across a year end, February, a leap day, billing day 1, December to January |
| `test_estimate_from_day_records` | the exact dict: lines, a missing day bearing fixed charges, projection, average |
| `test_estimate_without_data_and_after_the_cycle` | unknown with no data; days into cycle capped and clamped; no fixed lines when no charges |
| `test_bill_sensors_match_the_imported_days` | the four sensors against an **independent sum of the synthetic records** plus charges; attributes; units; the action for a past cycle and for today |
| `test_cycle_starting_mid_import` | `data_from` after the cycle start; `complete_from_cycle_start` false |
| `test_daylight_saving_boundary_uses_nem_days` | 00:30 AEDT on the billing day is still the old cycle; at 01:00 the new one starts |
| `test_bill_in_other_usage_modes` ×2 | Pricing-only (unknown, action refuses), Recovery-only |
| `test_service_without_a_billing_day` | no sensors; the action refuses |
| `test_options_turn_on_change_and_turn_off` | on reloads and adds sensors; the billing day changes in place mid-cycle; import settings keep the bill options; suggested values; off removes the sensors from the registry |
| `test_cost_including_fixed_charges_statistic` | each hour = cost + fixed/24; continuous sums; the per-day record; the next day continues |
| `test_fixed_statistic_not_written_in_pricing_mode` | |
| `test_no_fixed_statistic_without_a_general_channel` | |

Changed: three migration options-flow tests now expect "bill" in the menu.

## 3. Lab verification (VM 9102)

### 3.1 In-place upgrade, 2.0.0-rc2 → feature/v2.1 (M4 section 9 procedure)

09:36 to 09:39 AEDT. That is outside the quiet window, and more than 10 minutes from
9102's 07:15, 10:15 and 13:15 attempts.

| Step | Evidence |
|---|---|
| Preconditions | `local-lvm` 91.6 % free; status `caught_up` (07:15 run), not running; no Repairs issues |
| Snapshot | **`pre-m10`** (no RAM), task `OK`. Description: "M10: 2.0.0-rc2 (store 1.3), before in-place upgrade to the feature/v2.1 build". **Kept** until your sign-off |
| Before | Five statistics, **2424 hourly rows each** (12,120 in all), from 2026-06-28T14:00Z to 2026-10-07T13:00Z. Final sums: e9 energy 1505.196, e9 cost 404.686764, b9 energy 1067.938, b9 compensation 28.010201, net 376.676563. Store 1.3: marker and last_written 2026-10-07, boundary 2026-07-13, 101 day records. 8 entities. Energy preferences use the integration's statistics. **No own-sensor mappings, so the M9 device change does not apply on 9102** |
| Install | `82a8f4f` (M9 code), the old folder removed first; **sha256 of all 24 files matched**. Restart → `RUNNING` |
| After | **All 12,120 rows identical**, and the statistic IDs too. The 8 pre-existing entities are unchanged (entity ID, unique ID, device); +2 buttons. Store still 1.3; **only the two new keys differ**: `energy_issue_dismissed` false, and `first_import_at` set at the first check (as designed for an upgrade). No Repairs issues; the Energy dashboard already uses our statistics, so no `energy_not_used` issue is due |
| `run_now` | `caught_up`, trigger `run_now`, **0 calls** (today's retention check was done at 07:15). Rows still identical |

No rollback was needed.

### 3.2 The M10 build and the acceptance

- 09:47: installed the M10 code (identical to `6af158e` apart from the manifest version
  string; see 3.4). sha256 of 25 files matched. Restarted; rows identical to before the
  upgrade.
- **Options flow over the REST API:** menu `settings, bill, migrate`; the `bill` step
  fields are billing_day, network_daily, metering_daily, subscription_daily, other_daily,
  gst_percent and fixed_statistic. Submitted billing day 28, 0.7019 / 0.3852 / 0.7471,
  GST 10 → `create_entry`; the entry reloaded and is loaded.
- **`bill_estimate(date: 2026-09-27)`**, verbatim:

```
cycle 2026-08-28 to 2026-09-27, 31 days; data 2026-08-28 to 2026-09-27,
31 days imported, missing none, complete_from_cycle_start true
lines:
  usage                108.5424
  export_credit         -2.1238
  network_daily         23.9348
  metering_daily        13.1353
  subscription_daily    25.4761
bill_to_date          168.96
projected_bill        168.96
average_cost_per_day    5.45
```

**$168.96, the target exactly.**
- Usage matches the reconciliation's E9 $108.542390.
- The credit matches the B9 compensation $2.123809.
- Fixed charges: 1.8342 × 31 × 1.1 = 62.5462.
- After M11's adjustment the credit becomes $5.3749, and the estimate $165.71.

**The live sensors for the current cycle** (28 Sep – 27 Oct), at 09:48:

| Sensor | Value |
|---|---|
| Bill to date | **$55.49**. Lines: usage 39.8276, export credit −4.5174, network 7.7209, metering 4.2372, subscription 8.2181. Data 2026-09-28 to 2026-10-07, 10 days, none missing |
| Projected bill | $166.46 |
| Days into billing cycle | 11 d |
| Average cost per day | $5.55 AUD/d |

The current cycle spans the 2026-10-04 DST change. No integration warnings or errors were
logged.

### 3.3 The statistic including fixed charges, on real data

- 09:48: turned on in the options (applied in place, no reload), then `run_now`. The
  `fixed` chain was `caught_up`: **87 days written, 2026-07-13 to 2026-10-07** (from
  Amber's current retention boundary; older days can't be fetched again), in **13
  calls**.
- Read back:
  - 2088 hourly rows;
  - **every hour = e9 cost + 2.0176 ÷ 24** (largest deviation 5 × 10⁻⁷, the 6-decimal
    rounding of written rows);
  - over the acceptance cycle the statistic moves by **171.088610** = cost **108.542390**
    + **62.546220** (expected 62.546220).
- All 12,120 pre-existing rows are still identical, and there are 14 entities (+4 bill
  sensors).
- The statistic starts at 2026-07-13, while the other statistics start at 2026-06-28.
  Amber no longer holds the earlier days. It could be extended from our own cost
  statistic instead of usage records; see section 5.

### 3.4 Version string, and the 10:15 attempt

- **The 10:15 scheduled attempt** was skipped as caught up: the last run is still the
  09:49 `run_now`. It made no calls.
- **10:26** (after the 10:15 attempt and production's 10:22): reinstalled the final tree
  to update the manifest string to `2.1.0-dev2`. sha256 of all 25 files matches `HEAD`.
  Restarted → `RUNNING` (10:27).
- Then:
  - `bill_estimate(2026-09-27)` again gives **$168.96**, with the same lines;
  - no integration warnings or errors, and no Repairs issues;
  - **the original 12,120 rows are still identical**;
  - the fixed statistic's 2088 rows are unchanged since 09:49.

## 4. Open questions assigned

None.

## 5. Deviations and proposed design changes

1. **The statistic starts at Amber's retention boundary**, not at the start of the
   integration's history. It is built from usage records, like the other secondary
   chains, so it follows revisions in the same way. On 9102 it begins 2026-07-13, while
   the cost statistic begins 2026-06-28. Alternative: build it from our own stored cost
   statistic, which would cover the whole history (including migrated YAML history) with
   no API calls. That would mean a different rewrite trigger. I'd keep the current way
   unless you want full-history coverage.
2. **Changed charges apply to the statistic from the next day written**, not
   retroactively. The bill sensors always use the current charges. Rewriting the current
   cycle on a charge change is possible (≤ 5 calls). Say if you want it.
3. **Fixed charges cover `data_from` to `data_through`**, not the whole elapsed cycle, when
   the data starts late or days are missing at the start. This keeps "bill to date"
   consistent with the usage it includes. Days missing in the middle still bear their
   charges.
4. **"Average cost per day" includes the fixed charges** (bill to date ÷ days covered). The
   brief didn't say which; this matches what a day costs on the bill.
5. **The import settings step now keeps the bill options.** Before, it rebuilt the options
   from scratch. This is a bug fix in M9 code paths, needed for M10.
6. **Screenshots 08–20 keep their numbers** after deleting 07 (see section 1).

**New screenshots, optional:** the options menu now has "Bill estimate"
(`16-options-menu.png` is on the retake list already), and the bill step itself.

## 6. Live Amber API calls

**17**, on 2026-10-08 from 09:38 to 10:27 AEDT, all on the `/sites`+`/usage` counter.
This was outside the 07:17–07:27 quiet window, and none fell between 10:05 and 10:25.
- 4 `/sites` at entry setups: 3 restarts (upgrade, M10 build, version string) and 1
  reload when the bill estimate was turned on;
- 13 for the fixed-charges chain's first backfill (09:48–09:49);
- 0 for the upgrade's `run_now`, the bill estimate actions, or the skipped 10:15
  attempt.

## 7. Lab state left behind

- **VM 9102** (`amber-test-m3`) is running **2.1.0-dev2** (the `6af158e` tree) with:
  - the bill estimate on (billing day 28, the three charges, GST 10 %);
  - the fixed-charges statistic on;
  - fixed run times 07:15, 10:15 and 13:15, unchanged;
  - **snapshot `pre-m10` kept** (2.0.0-rc2, before the upgrade) until your sign-off.
- No other clones (the pool holds the template 9000 and 9102). VM 101 was not accessed.
  No scratch files on any HA instance.
- Repository: `feature/v2.1` is pushed. `main` and `v2` are unchanged at `v2.0.1`.

## 8. Recommended next steps

1. Review M10, especially section 5 items 1 to 4.
2. After sign-off, delete snapshot `pre-m10` on 9102.
3. M11 (export allowance): 9102 now has the bill options set, which M11's acceptance
   ($165.71) can reuse.
