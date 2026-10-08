# Milestone 12 report: 2.1.0-rc1

Date: 2026-10-08 and 09. Branch `feature/v2.1`, pushed, with the annotated tag
**`v2.1.0-rc1`** on its head. `main` was not touched.

**Summary.**
- **9102's 07:15 run:** it imported 2026-10-08 with **3 calls** (2 retention checks + 1 window).
  It measured **1 new day of export aggregates with no extra calls**, and both derived
  statistics extended by exactly that day (24 rows), with every earlier row unchanged.
- **Upgrade-path test, 2.0.1 → rc1, on a fresh clone:** passed.
  - No entity was lost. Statistics were unchanged, and the Store was migrated.
  - The own-sensor device move happened once, with no warning after a second restart.
  - The bill estimate and the allowance were configured, and their sensors appeared.
  - The clone is destroyed.
- **The test found a serious 2.0.1 bug** (section 3.2): adding an own-sensor mapping removes
  all of the integration's other sensors. rc1 fixes it, and the missing sensors come back
  on update.
- Docs: one Energy dashboard table (which statistic, when) shared by the README and
  INSTALL.md; the CHANGELOG 2.1.0-rc1 entry covers everything since 2.0.1; release notes
  include how to turn on the new features.
- **CI on `236ade4`** (rc1 code and docs): Tests, hassfest and HACS validation all
  succeeded. The report commit is tagged **`v2.1.0-rc1`** (annotated, "Amber Energy Dashboard
  2.1.0-rc1") and pushed; CI on it is in section 7.
- Snapshot `pre-m11` on 9102 is deleted.

## 1. What was done

| Commit | What |
|---|---|
| `236ade4` | 2.1.0-rc1: manifest, CHANGELOG since 2.0.1, release notes, final docs pass |
| (this commit, tagged `v2.1.0-rc1`) | This report |

The code is unchanged since M11 (`c1ea2f9`); only the manifest version string differs.
The last full local run (470 passed, 100 % coverage) was on that code.

## 2. VM 9102: the 2026-10-09 07:15 run

Read back at 07:20 AEDT (read only: the HA API, websocket and Store file; no Amber calls by
me). The baseline was recorded the afternoon before. 9102 runs the M11 code (`e15171d`),
which is identical to rc1 apart from the manifest string.

| Check | Expected | Observed |
|---|---|---|
| Run | the scheduled 07:15 run | `scheduled`, 20:15:00 to 20:15:03Z (07:15:00 to 07:15:03 AEDT), **`caught_up`**, `imported_days` ["2026-10-08"] |
| Calls | 2 retention + 1 window, nothing for the aggregates | **`sites_usage` 3**; `RateLimit-Remaining` 47. Retention `verified` with 2 calls (boundary 2026-07-13) |
| Export aggregates | 1 new day, no extra calls | `export.measured` = **["2026-10-08"]**; 88 days now (2026-07-13 to 10-08). The new day: window 4.73 kWh, penalty rate **1.86**, reward 2.659 kWh at 3.4702, loss factor 0.974045, measured |
| Derived statistics | +1 day each | `…_e9_cost_incl_fixed` and `…_b9_compensation_adjusted`: **24 rows written each, from 2026-10-07T14:00Z** (the new NEM day); 2424 → **2448** rows |
| Earlier rows | unchanged | **Identical** for all 7 statistics (the 5 base and the 2 derived) |
| The new day's values | fixed = cost + 2.01762/24; adjusted = billed + refund | Largest deviation 5 × 10⁻⁷ (rounding). Refund 0.087977 = 4.73 kWh × 1.86 c (within the allowance) |
| Repairs | none | none |

## 3. Upgrade-path test: 2.0.1 → 2.1.0-rc1 (VM 9111, destroyed)

**Clone:** linked clone `amber-test-rc1` of the template (HA 2026.9.3). Before creating it:
91.6 % free, and 1 other clone (9102). It ran from 13:43 to 13:53 AEDT on 2026-10-08.

### 3.1 2.0.1 set up as a user would

| Step | Evidence |
|---|---|
| Install `v2.0.1` | `git archive v2.0.1`; manifest `2.0.1`; sha256 of all 20 files matched; restart |
| Stand-in energy sensor | A **template sensor helper** created through HA's own config flow (so it survives restarts): `sensor.stand_in_house_meter`, device class energy, state class total_increasing, kWh |
| Config flow (2.0.1, REST) | Steps user, site, channels, schedule (**no** Energy dashboard option in 2.0.1), with the test key; entry `loaded` |
| First import | `first_setup`, `caught_up`, **87 days, 20 calls** |
| Own-sensor mapping | The sub-entry flow with the stand-in on E9 → `create_entry`. `sensor.amber_energy_dashboard_reconciliation_stand_in_house_meter` was created, named "Amber Energy Dashboard Reconciliation: Stand-in house meter". A `run_now` gave usage `caught_up`, 0 calls; the own-sensor chain was "waiting: the sensor has no statistics yet", as expected for a sensor created minutes before |
| Statistics | 5 statistics, **2088 rows each** (10,440), from 2026-07-12T14:00Z |
| Store | 1.3, without the 2.1 keys |

### 3.2 A 2.0.1 bug: adding a mapping removes the other sensors

After the mapping was added, the entity registry held **only the reconciliation sensor**:
- the import status, last imported date, days behind and next run sensors, and the four
  "yesterday" sensors, were **removed**;
- the site's device had moved into the sub-entry (`{entry: [subentry]}`);
- HA logged "assigns an existing device to a different config subentry".

After a **restart on 2.0.1** it was the same: only the reconciliation sensor, and the
warning again on every start.

This is the M9 finding (a device belongs to one sub-entry in HA 2026.9), but it already
breaks **2.0.0 and 2.0.1** for anyone with an own-sensor mapping, without the buttons. In
M9 I saw it as an entity dropped with the buttons; in 2.0.1 the effect is broader. Users
who added an own sensor have probably lost their status and yesterday sensors.

### 3.3 Upgrade in place to the rc1 tree, and restarts

| Check | Evidence |
|---|---|
| Install | The `feature/v2.1` tree with manifest `2.1.0-rc1` (identical code to `236ade4`); sha256 of all 27 files matched; restart → `RUNNING` |
| **Entities after the first start** | **11**. All 9 site sensors are back **with their original entity IDs** (import status, last imported date, days behind, next scheduled run, 4 "yesterday"), on "Amber Energy Dashboard" (sub-entry none). The 2 new buttons. **The reconciliation sensor kept its entity ID**, now on "Stand-in house meter (own sensor)" (its sub-entry) |
| Warnings, first start | The sub-entry warning **once** (the old device moving back) |
| **Second restart** | **No integration warnings.** The entity list is identical to the first start's (11) |
| **Statistics** | **All 10,440 rows identical** to before the upgrade, and the IDs too |
| **Store** | Still 1.3. Only the 2.1 keys were added: `energy_issue_dismissed` false, `export_days` {}, `first_import_at` (set at the first check), `fixed_schedule` [], `tariff` {Endeavour Energy, E9 N71, B9 N61, known "Endeavour Energy N61"}. Everything else is unchanged |

### 3.4 Configuring the new features

| Step | Evidence |
|---|---|
| Options menu | `settings, bill, allowance, migrate` |
| Bill estimate | Billing day 28, network 0.7019, metering 0.3852, subscription 0.7471, GST 10, fixed statistic on → `create_entry`; the entry reloaded |
| Export allowance step | "Detected Endeavour Energy N61: 8 kWh a day free, exports in solarSponge charged beyond it. Billing day 28". Suggested: on, 8 kWh, billing period, solarSponge. **It was on by default**, without any input |
| `run_now` | `caught_up`, **13 calls**: aggregates measured for 87 days. Both derived statistics written (2088 rows each, from 2026-07-12T14:00Z, the clone's first imported day) |
| **Sensors** | Bill to date **$54.91**, Projected bill $164.74, Days into billing cycle 11 d, Average cost per day $5.49 AUD/d, Export allowance used **30.76 kWh**, remaining **209.24 kWh**, Export charge after allowance $0.00 |
| Cross-check | **The same figures as on 9102** for the current cycle (M11 report section 3.4), from an independent install with different history |
| Warnings | none |

**Destroyed:** stop and delete, both tasks `OK`.

## 4. Docs

- **The Energy dashboard choices** are now in one table in INSTALL.md section 6: field,
  statistic, and when.
  - The default cost and compensation.
  - The **cost including fixed charges**, with a warning: it already includes a controlled
    load's cost, so the controlled-load connection's cost must then be empty, to avoid
    counting it twice. Neither guide said this before.
  - The **compensation (export allowance applied)**.
  - The controlled-load connection.
  - That the optional statistics cover the whole history, and that the automatic setup
    uses the defaults.
- The README's Energy dashboard paragraph summarises the table and links to it. The bill
  estimate and allowance sections in both guides refer to it.
- INSTALL.md section 4 mentions the bill and allowance sensors on the device.
- README banner: "Version 2.1.0-rc1 (release candidate; the current release is 2.0.1)".
- **CHANGELOG:** the three dev entries are consolidated into **`[2.1.0-rc1] - 2026-10-09`**,
  everything since 2.0.1:
  - Added: the bill estimate, the export allowance, setup and maintenance;
  - **Fixed:** the 2.0.1 sensor-removal bug and the options merge;
  - Documentation;
  - plus a release link.
- **`reports/release-notes-v2.1.0-rc1.md`:**
  - what's new, fixed, and who should update (especially own-sensor users);
  - how to install the rc from HACS;
  - **how to turn on the new features after updating**: bill estimate steps, allowance
    (N61 automatic once the billing day is set; others manual), and the Energy dashboard
    choices;
  - known limitations.

## 5. Deviations and points for the planning chat

1. **The 2.0.1 sensor-removal bug** (section 3.2) deserves a mention beyond the rc notes.
   Users with own-sensor mappings on 2.0.x have lost their display sensors, though their
   statistics are unaffected. Options:
   - a 2.0.2 that backports only the device change;
   - or ship 2.1.0 soon and point them to it.

   I'd backport, since it's small (`devices.py` plus the sensor change), but that's your
   call.
2. **The CHANGELOG date is 2026-10-09** (the tag day). Change it if the release is
   published later.
3. The automatic Energy dashboard setup and the Repairs fix use the **default** statistics.
   Switching to the optional ones is manual, and the guides say how.

## 6. Live Amber API calls

**40 by me**, all on 2026-10-08 from 13:45 to 13:53 AEDT on clone 9111. That was outside
the quiet window, and after the 13:15 and 13:22 runs.
- 2 `/sites` in the 2.0.1 config flow and its setup;
- 20 in 2.0.1's first import;
- 5 `/sites` at reloads and restarts: the mapping, 2.0.1 restart, upgrade restart, second
  restart, bill options;
- 13 for the allowance's one-off measurement.

The 0 calls on 2.0.1's `run_now` and at 9102's read-back are not counted.

9102's own scheduled 07:15 run made 3 (not mine).

## 7. Lab state left behind

- **VM 9102** (`amber-test-m3`) is running the M11 code (`e15171d`, manifest 2.1.0-dev3)
  with:
  - the bill estimate, fixed-charges statistic and export allowance on;
  - fixed run times 07:15, 10:15 and 13:15;
  - **no snapshots** (`pre-m11` deleted on 2026-10-08, task `OK`).
- VM 9111 was destroyed. VM 101 was not accessed. No other clones, and no scratch files on
  any HA instance.
- Repository: `feature/v2.1` and the tag `v2.1.0-rc1` are pushed. `main` and `v2` are
  unchanged at `v2.0.1`.

## 8. Recommended next steps

1. Review rc1, and decide on the 2.0.x backport (section 5 item 1).
2. Create the GitHub pre-release from the tag `v2.1.0-rc1` with
   `reports/release-notes-v2.1.0-rc1.md` (there is no `gh` here).
3. Install the rc from HACS on one real system, ideally one with an own-sensor mapping.
4. Screenshots for the new options steps, sensors and the Energy dashboard table.
5. After a soak period: 2.1.0.
