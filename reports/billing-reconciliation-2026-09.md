# Billing reconciliation: 2026-08-28 to 2026-09-27

Read-only analysis. No code changes. This compares the Amber usage API (fetched today) and
the integration's statistics on VM 9102 with the bill for this period.

## Sources

- **Amber API (today):** `/sites/{id}/usage` fetched on 2026-09-30 at 12:28 AEST with the
  test key (same account and site as production). 5 windows: 08-28..09-03, 09-04..09-10,
  09-11..09-17, 09-18..09-24 and 09-25..09-27. All returned HTTP 200. That gave 17,856
  records: 31 days × 288 five-minute intervals × 2 channels (E9 `general`, B9 `feedIn`),
  all with `duration` 5.
- **VM 9102** (`2.0.0-rc2`, same account): recorder hourly statistics read over the
  websocket for 2026-08-27T13:00Z to 09-27T13:00Z (744 hourly changes, 28 Aug 00:00 to
  28 Sep 00:00 AEST), plus the integration's store file. Read only.
- **Bill lines** as provided: E9 $98.68 ex GST; "Wholesale Export Credit/Charge"
  347.94 kWh at 0.0149 $/kWh = $5.20; "Export Reward" 5.15 kWh at 0.0349 $/kWh = $0.18; no
  GST on exports.

API `cost` and `perKwh` are in cents. For B9, a negative value means the customer earns.
Below, "earned rate" means `−perKwh` and "compensation" means `−cost` / 100.

## Summary

The API data and the integration agree exactly, and they also reconcile with the bill.
**B9 feed-in `perKwh` is exactly:**

```
earned rate = 0.97404 × spotPerKwh + network export component
network export component = −1.86 c/kWh in solarSponge (10:00–14:00)
                           +3.47 c/kWh in peak (weekdays 16:00–20:00)
                            0         in offPeak
```

(The periods are taken from E9's `tariffInformation` for the same interval.) This holds
for all 8,928 B9 intervals, with a maximum residual below 0.0001 c/kWh.

| Component | kWh | Rate | AUD | Bill line |
|---|---:|---:|---:|---|
| 0.97404 × spot (wholesale, loss-adjusted) | 347.935 | 1.4934 c (kWh-weighted) | **+5.1960** | Wholesale Export Credit/Charge 347.94 × 0.0149 = **$5.20** ✔ |
| Peak export reward | 5.152 | 3.4703 c | **+0.1788** | Export Reward 5.15 × 0.0349 = **$0.18** ✔ |
| solarSponge export charge | 174.789 | −1.8600 c | **−3.2511** | *not among the lines provided* |
| **Total (API = integration)** | 347.935 | 0.6104 c | **+2.1238** | |

5.1960 + 0.1788 − 3.2511 = 2.1237. The API sum is 2.123809; the small difference is from
rounding the rate constants.

**The whole gap between the bill's $5.38 (5.20 + 0.18) and the integration's $2.12 is
the solarSponge export charge: 174.789 kWh × 1.86 c = $3.25.** The API deducts it. That
leaves two possibilities, and **the bill decides which** (see section 6):
- If the bill has a line of about 174.79 kWh at about 0.0186 $/kWh (about −$3.25), for
  example a network export charge, then the integration is correct and matches the bill
  in total.
- If the bill has no such line, Amber's API deducts a charge that Amber did not bill, and
  the API (and so the integration) understates compensation by $3.25 for the period. That
  would be a question for Amber, not an integration bug. The integration records what the
  API returns.

## 1. General (E9)

| | API today | 9102 stored | Bill |
|---|---:|---:|---:|
| kWh | 433.458 | 433.458 | — |
| Cost (AUD) | **108.542390** (10,854.2390 c) | 108.542390 | 98.68 × 1.1 = **108.548** |

- The difference from the bill is **−$0.0057**. That fits the bill's ex-GST line amounts
  being rounded to the cent before GST is applied (a 0.52 c ex-GST rounding on $98.68).
  **Confirmed, to within rounding.**
- `cost` = `kwh × perKwh` per interval to within 0.00005 c. The kWh-weighted `perKwh` is
  25.041 c; the kWh-weighted `spotPerKwh` is 8.707 c.
- All 8,928 intervals are `billable`.

## 2. Feed-in (B9)

- **Total:** 347.935 kWh (bill 347.94 ✔). Total cost −212.3809 c, which is
  **$2.123809 compensation** (9102 stored: 2.123809; you quoted $2.12 ✔).

| Group | Intervals | …with kWh > 0 | kWh | Cost (c) | AUD | kWh-weighted perKwh | kWh-weighted spot |
|---|---:|---:|---:|---:|---:|---:|---:|
| cost < 0 (earned) | 1,725 | 1,725 | 172.227 | −493.9093 | −4.939093 | −2.86777 | 3.42231 |
| cost > 0 (paid) | 1,112 | 1,112 | 170.458 | +281.5284 | +2.815284 | +1.65161 | −0.32830 |
| cost = 0 | 6,091 | 44 | 5.250 | 0 | 0 | ≈ 0 | ≈ 0 |
| **All** | 8,928 | 2,881 | 347.935 | −212.3809 | −2.123809 | **−0.61040** | **1.53320** |

- **Overall kWh-weighted averages:** `perKwh` **−0.61040 c/kWh** (earned 0.6104 c) and
  `spotPerKwh` **1.53320 c/kWh**.
- In the zero-cost group, 6,047 intervals have kWh 0. The other 44 intervals
  (5.250 kWh) have `perKwh` 0 or within 0.0012 c of it, because spot was 0 to 0.0012 c.
  Their 5.250 kWh is close to the bill's 5.15 kWh Export Reward, but that is a
  coincidence: the reward kWh is exactly the peak-period export (5.152 kWh, section 5).
- The "paid" group (cost > 0) is almost entirely solarSponge intervals, where spot is low
  or negative and the −1.86 c charge applies. Across all exporting intervals, spot > 0
  covers 217.079 kWh (Σ kWh × spot = $6.2003) and spot ≤ 0 covers 130.856 kWh
  (Σ = −$0.8657).

## 3. Record quality

**All 8,928 B9 intervals, and all 8,928 E9 intervals, are `billable` now; none are
`estimated`.**

In 9102's store, every day from 08-28 to 09-27 shows `estimated_records: 0` at import.
The last writes were 2026-09-26T11:23Z (08-28..09-25, catch-up), 09-26T21:15Z (09-26) and
09-27T21:15Z (09-27). The revision set is empty and no tail rewrite is pending.

## 4. Other fields in the usage records

- **`tariffInformation`:** present on E9 only. B9 records have no such key, which is why
  the B9 period comes from E9's record for the same interval. For E9, `season` is always
  `nonSummer`, and `period` is `offPeak` 6,432, `solarSponge` 1,488 or `peak` 1,008. The
  medians of E9 `perKwh − spot` by period are offPeak 16.77 c, solarSponge 8.34 c and
  peak 20.78 c.
  - The B9 export component follows the same periods exactly: offPeak 0 (6,432
    intervals, 167.994 kWh), solarSponge −1.86 c (1,488, 174.789 kWh) and peak +3.47 c
    (1,008, 5.152 kWh).
  - Peak means weekdays 16:00–20:00, measured on interval end times 16:05 to 20:00. On
    weekends that window is offPeak.
  - **This is the field that explains the per-kWh difference.**
- **`spotPerKwh`:** identical on E9 and B9 for every interval (8,928 of 8,928).
- **Loss factor:** outside solarSponge and peak, `−perKwh / spotPerKwh` is 0.974044 to
  0.974046 wherever |spot| > 5 c. So B9 pays spot scaled by a constant 0.97404, which
  looks like a loss factor (MLF × DLF); it is the same across the whole period.
- **`descriptor`** (B9): `high` 6,442, `low` 1,396 and `extremelyLow` 1,090. This is a
  price label only and does not affect cost.
- **`renewables`:** identical on both channels. It is a grid-wide share (0.23 to 109.08,
  mean 37.35) with no bearing on cost.
- **`spikeStatus`:** `none` for every interval.
- There are no other fields. Every record has exactly: `type`, `duration`, `date`,
  `startTime`, `endTime`, `nemTime`, `quality`, `kwh`, `perKwh`, `cost`, `spotPerKwh`,
  `channelType`, `channelIdentifier`, `renewables`, `spikeStatus`, `descriptor`, plus
  `tariffInformation` on E9 only.

## 5. Hypotheses

**(a) Bill 0.0149 = kWh-weighted spot, and the API subtracts about 0.9 c/kWh (like the
0.96 c import market fee). Partly right: the bill uses loss-adjusted spot, and there is
no flat fee deduction.**
- The raw kWh-weighted spot is 1.53320 c, which gives 347.935 × 1.53320 c = **$5.3345**,
  not $5.20. The loss-adjusted value is 0.97404 × 1.53320 = **1.4934 c**, and
  347.935 × 1.4934 c = **$5.1960**, which rounds to **$5.20** and to the rate 0.0149 ✔.
  (Bill: 5.20 / 347.94 = 1.4945 c.)
- The average gap between spot and the API's earned rate is 1.53320 − 0.61040 = 0.9228 c,
  but it is **not** a per-kWh fee. It is made up of:
  - the loss factor: (1 − 0.97404) × 1.53320 = 0.0398 c;
  - the solarSponge charge: 1.86 × 174.789 / 347.935 = 0.9344 c;
  - minus the peak reward: 3.47 × 5.152 / 347.935 = 0.0514 c.

  Total: 0.0398 + 0.9344 − 0.0514 = 0.9228 c ✔.
- The residual (earned rate − 0.97404 × spot) takes only the values 0, −1.86 and +3.47 c,
  each tied to a tariff period. None is 0.96 c (the import market fee ex GST) or 1.056 c
  (with GST). Tests: spot − 0.96 gives $1.99 and spot − 1.056 gives $1.66, and neither is
  $2.1238. **The import market fee is not involved.**

**(b) Values changed from estimated to billable after import. Rejected.**
- Today's fetch, grouped to hours, matches 9102's stored hourly changes in all 5
  statistics for all 744 hours. The maximum absolute difference is 2.2 × 10⁻¹³ (float
  noise).
- The period totals are identical: E9 433.458 kWh and $108.542390; B9 347.935 kWh and
  $2.123809; net $106.418581.
- 9102 imported every day with 0 estimated records, and every record is billable today.
- 9102 cannot show production's import history. But production's figure matches this
  ($2.12), so it has the same values.

**(c) GST handling of export values. No GST on exports in the API: consistent with the
bill.**
- E9 API values include GST: they match $98.68 × 1.1.
- B9 values do not. The wholesale line matches 0.97404 × spot with no 1.1 factor. With a
  GST adjustment, spot / 1.1 gives $4.8496 and (spot − 0.96) × 1.1 gives $2.1938, and
  neither matches.
- The reward rate in the API is 3.4703 c against the bill's 3.49 c. 3.47 × 1.1 = 3.82, so
  the difference is not GST. It makes no difference to the amount: 5.152 kWh × 3.4703 c =
  $0.1788, and 5.15 × 3.49 c = $0.1797; both round to $0.18.

**(d) The Export Reward is absent from the API. Rejected.**
- It is built into B9 `perKwh` as +3.4703 c/kWh on weekday peak intervals.
- 1,008 intervals, **5.152 kWh** (bill 5.15), **$0.1788** (bill $0.18).
- The reward applies on all 21 weekdays in the period (21 × 48 = 1,008 intervals); weekends get
  no reward.

## 6. Open point for Robert

Please check the bill for a solarSponge or daytime **export charge**. The API implies about
**174.79 kWh at about 0.0186 $/kWh ≈ $3.25**, possibly labelled as a network export or
two-way tariff charge.
- **If the bill has it:** everything reconciles. Bill feed-in net = 5.20 + 0.18 − 3.25 =
  $2.13, against the integration's $2.12; the 1 c is per-line rounding. No change to the
  integration is needed. It might be worth a README note explaining why the dashboard's
  feed-in rate is lower than the wholesale line on the bill.
- **If the bill does not have it:** Amber's API deducts a charge it did not bill. The
  integration would understate compensation by $3.25 for the month. That should be raised
  with Amber; I would not propose a workaround in the integration until Amber confirms.

## 7. Live Amber API calls

**5**, at 12:28:21 to 12:28:34 AEST on 2026-09-30, all `/usage`. `RateLimit-Remaining`
went from 46 to 41. This was after the production schedule changed (runs at 07:22, 10:22
and 13:22; quiet window 07:17–07:27). There were no retries.

## 8. Lab state

- VM 9102 was only read (websocket statistics, store file, and the config entry over SSH
  for the site ID). No files were written on it. It is still running `2.0.0-rc2`.
- VM 101 was not accessed. No clones were created.
- The raw API data and the analysis scripts are in the session scratchpad only, not in the
  repository. The site ID and key are not in this report.
