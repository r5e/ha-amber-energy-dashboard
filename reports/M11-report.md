# Milestone 11 report: export allowance, and the M10 review's item 1 (2.1.0-dev3)

Date: 2026-10-08. Branch `feature/v2.1`, pushed. Not merged, no tag.

**Summary.**
- **Acceptance on VM 9102 passes:** cycle 28 Aug – 27 Sep 2026.
  - Adjusted compensation **$5.3749** (billed $2.1238 + refund $3.2511).
  - Allowance **248 kWh**, with **174.789 kWh** used; export charge after the allowance
    **$0.00**.
  - **Bill estimate $165.71**, against the bill's $165.74 excluding the card fee.
- **M10 item 1:** "cost including fixed charges" is now built from our own cost statistics
  with no API calls. On 9102 it starts with the cost statistic (**2026-06-28T14:00Z**), and
  every one of its 2424 hours equals cost + 2.01762/24. Snapshot `pre-m10` is deleted.
- **470 tests pass, with 100 % coverage**, and ruff is clean. **CI on `e15171d`: Tests,
  hassfest and HACS validation all succeeded.**

## 1. What was built

| Commit | What |
|---|---|
| `c1ea2f9` | Derived statistics from our own data (`derived.py`); the export allowance (`export.py`, detection, options, sensors, bill estimate) |
| `e15171d` | 2.1.0-dev3: README, INSTALL.md, CHANGELOG, DESIGN sections 19 and 20 (as built) |
| (this commit) | This report |

### M10 item 1: derived statistics (`derived.py`)

- **What a derived statistic is:** its sum at each row = the base statistics' sums
  (carried forward) + the accumulated extra amounts; its state is the change from the
  previous row.
  - **Cost including fixed charges:** the base is the import cost (general and controlled
    load). The extra is the fixed charges, accruing with the time between rows: an hourly
    row adds daily ÷ 24, a copied daily lump a whole day, and the first row its own hour.
  - **Adjusted compensation (M11):** the base is the feed-in compensation. The extra is each
    hour's refund.
- **Sync by difference:**
  - The expected rows are recomputed from the stored base rows.
  - The statistic is rewritten from the first row that differs, and read back
    (`importer.async_verify_range`).
  - If it holds a row the base no longer has, it is cleared and written in full.
  - **When it runs:** after every run (inside the run lock), after every options change (a
    background task under the lock), and after every migration.
  - So revisions, tail rewrites, crash recovery, backfills and migrations are all followed
    without hooks in each write path, with **no API calls**.
- **Changed charges (item 2):** the Store keeps a schedule (`fixed_schedule`: amount + the
  hour it applies from). A new amount applies from the day after the last imported day.
  Documented in the README and INSTALL.md.
- M10's records-based `fixed` chain is removed, and its Store state is forgotten at setup.
  The statistic ID is unchanged; its name is now "Amber import cost including fixed
  charges".

### M11: the export allowance (`export.py`, `manager.py`, `config_flow.py`, `sensor.py`, `bill.py`)

**Detection.**
- Done at every setup, from the `/sites` answer the setup already fetches; recorded in the
  Store as `tariff`.
- Real values from the test account: network **"Endeavour Energy"**, general **N71**,
  feed-in **N61**. N61 is the feed-in channel's code, so detection looks for the tariff
  code on any channel.
- The table (`KNOWN_TARIFFS`) holds Endeavour Energy N61 only: 8 kWh a day, penalty period
  `solarSponge`, reward period `peak`, billing-period totalling, with its source.

**Settings** (`allowance_from_options`).
- On by default for a known tariff with a billing day; otherwise off.
- The options step "Export allowance" overrides: on/off, kWh per day, billing-period or
  daily totalling, and the penalty period name.
- Billing-period totalling needs the billing day: the step refuses it, and at runtime the
  feature is then off. Daily totalling works without a billing day.
- An unknown tariff needs the kWh per day.
- The step shows what was detected, and the billing day.

**Per-day aggregates** (Store `export_days`, for every imported day with usage records).
- They hold:
  - window export per NEM hour and in total;
  - the loss factor;
  - the penalty and reward rates (c/kWh) and amounts (AUD);
  - a fingerprint of the day's usage.
- The period of each interval comes from the **general channel's
  `tariffInformation.period` for the same interval**.
- **The loss factor** is the median of earned ÷ spot over off-period intervals with |spot|
  ≥ 5 c, falling back to the last measured one. **The component** is earned − loss factor ×
  spot. The rates are its medians per period.
- **When they're measured:** in each run, for imported days that lack them (a one-off 13
  calls for 87 days on 9102, using the run's 7-day windows), and for days whose usage
  changed (a revision's rewrite fetches them anyway).

**Refunds.**
- For each allowance period, the allowance = kWh per day × the period's days.
- Window exports are set against it hour by hour, in time order. The penalty on the part
  within the allowance is refunded at that day's measured rate, so refunds stop once the
  period's window export passes the allowance.
- Days without aggregates count nothing and are listed as unadjusted.

**Outputs.**
- **Adjusted compensation** statistic `…_b9_compensation_adjusted`: a derived statistic over
  the whole history. Days before retention are carried unchanged.
- **Sensors** for the current period, up to yesterday:
  - Export allowance used (kWh);
  - Export allowance remaining (kWh);
  - Export charge after allowance (AUD).

  Their attributes: the period, totalling, allowance, window export, measured rates (median
  of the last 30 days), data through, and unadjusted days.
- **Bill estimate:**
  - `export_credit` = −(billed compensation + the full penalty), which is what the bill
    credits before the network's charge;
  - `network_export_charge` = penalty − refund;
  - together they come to the adjusted compensation.

  `bill_estimate` also returns the period's `export_allowance` figures.

## 2. Test results

`uv run pytest --cov=custom_components.amber_energy_dashboard`: **470 passed**, 0 skipped
(380 s). Every file is at 100 %. That is 455 at M10, plus 15:

| File | New or changed | What |
|---|---|---|
| `test_bill.py` | `test_cost_including_fixed_charges_statistic` (rewritten) | each hour = cost + fixed/24, first hour included; the schedule; a second sync writes nothing and makes **no API calls**; a charge change applies from the day after the last imported day |
| | `test_fixed_statistic_follows_history_and_rewrites` (new) | copied daily-lump history before the first day (a whole day of charges per lump); controlled-load cost included; **a rewritten cost day is followed from that day (24 rows)**; a stray row → cleared and rewritten in full |
| `test_allowance.py` (new, 14) | detection | Endeavour Energy N61, case-insensitive; other network; N71 only; no network |
| | settings | default on and off, overrides, daily without a billing day, unknown tariff |
| | aggregates | **loss factor 0.97404, 1.86 and 3.47 measured** from N61-shaped synthetic data; window per hour; no reward on Saturday; loss-factor fallback; unmeasured day; no feed-in |
| | refunds | refunds stop at the allowance (31- and 30-day cycles, a partly used allowance, daily totalling); period summary with unadjusted days |
| | end to end | aggregates for all 10 days; adjusted − billed = window × 1.86 c (all within); sensors against an **independent window sum**; bill lines; the action's figures; no extra calls on a quiet run |
| | used up | 0.5 kWh a day (15 kWh): used 15, remaining 0, the export charge = excess × 1.86 c, and the bill line |
| | daily totalling without a billing day | |
| | **revision** | estimated data that changes → the day is measured again and the adjusted statistic follows |
| | Pricing-only, or no feed-in channel | off; sensors unknown |
| | options step | detection text, suggested values, errors, off → sensors removed, import settings keep the options, billing-period totalling refused without a billing day |
| | unknown tariff; no adjusted spec without a feed-in; a day without feed-in; pruning | |

Changed: three migration options-flow tests now expect "allowance" in the menu.

## 3. Lab verification (VM 9102)

### 3.1 Upgrade 2.1.0-dev2 → 2.1.0-dev3

Run at 13:26 to 13:27 AEDT: after 9102's 13:15 attempt (skipped as caught up) and
production's 13:22.

| Step | Evidence |
|---|---|
| Snapshot | **`pre-m11`** (2.1.0-dev2 with the bill estimate and M10's chain), task `OK`. **Kept** until your sign-off |
| Before | 5 base statistics, 2424 rows each; the old fixed statistic: 2088 rows from 2026-07-13; Store chains `['fixed']`; marker 2026-10-07 |
| Install | `e15171d`, sha256 of all 27 files matched; restart → `RUNNING` |
| `run_now` | `caught_up`, **13 calls**: export aggregates measured for **87 days, 2026-07-13 to 10-07**. Derived: `…_e9_cost_incl_fixed` **2424 rows written from 2026-06-28T14:00Z**; `…_b9_compensation_adjusted` 2424 rows from 2026-06-28T14:00Z |
| Base statistics | **All 12,120 rows still identical to the very first M10 snapshot** (before the rc2 upgrade) |
| Store | the `fixed` chain is gone; `tariff` = Endeavour Energy, {E9: N71, B9: N61}, known "Endeavour Energy N61"; `fixed_schedule` = [{from: null, 2.01762}] |
| Logs and Repairs | no integration warnings or errors; no issues |

### 3.2 M10 item 1 on real data

- Fixed-charges statistic: **2424 rows, first 2026-06-28T14:00Z**, the same as the cost
  statistic. Before, it had 2088 rows from 2026-07-13.
- **Every hour = cost + 2.01762 ÷ 24**: the largest deviation over 2424 hours is 5 × 10⁻⁷,
  the 6-decimal rounding of the written rows.
- Over 28 Aug – 27 Sep: cost 108.542390; with fixed charges 171.088610; **difference
  62.546220** (31 × 2.01762).

### 3.3 M11 acceptance: 28 Aug – 27 Sep 2026

**Measured aggregates for the cycle (31 days):**

| Figure | Measured | Reconciliation |
|---|---:|---:|
| Window export | **174.789 kWh** | 174.789 |
| Penalty | **$3.2511** at **1.86 c** | $3.2511 |
| Peak reward | 5.152 kWh, $0.1788, measured **3.4702 c** | 5.152 kWh, $0.1788, 3.4703 c (kWh-weighted) |
| Loss factor | **0.974045** on every day | 0.97404 |

No day was unmeasured, and only one penalty rate (1.86) occurred in 87 days.

**From the statistics:**
- billed compensation 2.123809;
- **adjusted compensation 5.374885**;
- refund 3.251076.

The target was 2.1238 + 3.2511 = 5.3749.

**`bill_estimate(date: 2026-09-27)`**, verbatim:

```
lines:  usage 108.5424 | export_credit -5.3749 | network_export_charge 0.0
        network_daily 23.9348 | metering_daily 13.1353 | subscription_daily 25.4761
bill_to_date 165.71   projected_bill 165.71   average_cost_per_day 5.35
export_allowance: allowance_kwh 248.0, window_export_kwh 174.789,
  allowance_used_kwh 174.789, allowance_remaining_kwh 73.211,
  penalty 3.2511, refund 3.2511, export_charge 0.0, unadjusted_days [],
  penalty_rate_c 1.86, reward_rate_c 3.4702
```

**$165.71, the target exactly** (the bill excluding the card fee is $165.74). The −5.3749
credit line plus the 0.0 charge line equals the adjusted compensation.

**Before 2026-07-13** (the retention boundary): the adjusted statistic equals the
compensation for all 336 hours, carried unadjusted as specified.

### 3.4 Current cycle (28 Sep – 27 Oct), data through 2026-10-07

| Figure | Value |
|---|---|
| Bill to date | **$54.91**. Lines: usage 39.8276, export credit −5.0895, network export charge 0.00, network 7.7209, metering 4.2372, subscription 8.2181 |
| Projected bill | $164.74 |
| Average per day | $5.49 |
| Export allowance used | **30.76 kWh** of **240** |
| Remaining | **209.24 kWh** |
| Export charge after allowance | **$0.00** (penalty 0.5721, all refunded) |

The cycle includes the 2026-10-04 DST change.

## 4. Open questions assigned

None. **One observation:** the allowance is used by window exports only. In September,
174.79 of 248 kWh were used. So on N61 the allowance rarely runs out in winter months, but
it may in summer, when the export charge after the allowance becomes non-zero.

## 5. Deviations and proposed design changes

1. **Fixed charges accrue by elapsed time between rows.** For copied v1 daily lumps, each
   lump adds a whole day, and the first row adds one hour. Totals over any span are exact
   (verified: 31 × 2.01762 over the cycle). The split between a lump and the carry row
   around the seam is by time, not by calendar day.
2. **Derived statistics sync by difference** (recompute and compare) rather than by hooks
   in each write path. This reads the base statistics' whole history on each sync (about
   2.4k rows each on 9102; trivial). A stray row triggers a full clear-and-rewrite.
3. **The refund uses each day's own measured penalty rate**, not a 30-day median. The
   median appears only in the sensor attributes. With a constant component the two are
   identical (1.86 on every 9102 day). Per-day rates also handle a mid-period tariff change
   (1 July).
4. **Detection matches the tariff code on any channel** (N61 is on the feed-in), plus the
   network name, case-insensitively.
5. **"On by default" for N61 needs the billing day.** Without one, the feature stays off
   until the user turns it on with daily totalling. Daily totalling can't reproduce N61's
   billing-period rule.
6. **The daily-totalling sensors show yesterday's day**, since the period is one day.
7. **The bill estimate's credit line now shows the credit before the network's charge**
   (billed + full penalty), with the charge after the allowance as its own line, as the
   bill does. With the allowance off, the M10 lines are unchanged.

## 6. Live Amber API calls

**15**, on 2026-10-08, outside the 07:17–07:27 quiet window and outside 9102's and
production's 13:15 and 13:22 runs:
- 1 `/sites` at 12:44 (to read the network name for the table; only the network and tariff
  fields were printed);
- 1 `/sites` at the 13:27 setup;
- 13 usage windows at 13:27 for the one-off measurement of 87 days of aggregates.

## 7. Lab state left behind

- **VM 9102** (`amber-test-m3`) is running **2.1.0-dev3** (`e15171d`), with:
  - the bill estimate (billing day 28, the three charges, GST 10 %);
  - the fixed-charges statistic;
  - the export allowance (N61, on by default);
  - fixed run times 07:15, 10:15 and 13:15.
- **Snapshots:** `pre-m10` **deleted** after item 1 was verified (task `OK`). **`pre-m11`
  kept** until your sign-off.
- No other clones. VM 101 was not accessed. No scratch files on any HA instance.
- Repository: `feature/v2.1` is pushed. `main` and `v2` are unchanged at `v2.0.1`.

## 8. Recommended next steps

1. Review M11, especially section 5 items 1, 3 and 5.
2. After sign-off, delete `pre-m11` on 9102.
3. Screenshots to add: the "Export allowance" options step, the new sensors on the device
   page, and the Energy dashboard compensation choice. `16-options-menu.png` is already on
   the retake list; the menu now also shows "Export allowance".
4. Watch 9102 over the next days: 07:15 runs should measure 1 day of aggregates each, with
   no extra calls (the day's window is already fetched).
5. Then the v2.1 release preparation.
