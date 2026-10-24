# Milestone 20 report: 2.2.0-dev4 (solar-aware appliance cost, previous bill, Today so far fixes)

Date: 2026-10-24 (Sydney). Branch `feature/v2.2`, after the 2.2.0b2 beta (`b8cc35e`).
Pushed; nothing is merged or tagged. **Stopped for review.**

**Summary.**
- **Fix: Today so far after an update.** A live price that isn't found at setup is now
  looked for again:
  - once Home Assistant has started;
  - whenever an Amber Express or Amber Electric entity is registered or changed;
  - every minute for 15 minutes.

  A price found starts Today so far in place, with its sensors, without a reload.
- **Solar-aware appliance cost** (per appliance, off by default):
  - a second statistic, "<name> cost (solar-aware)", and a **Yesterday solar-aware cost**
    sensor;
  - each 5-minute interval's solar share (from the grid meter's import and export and
    the solar sensor) is valued at the feed-in price, or the allowance-adjusted feed-in
    value with the export allowance; the rest at the import price.
- **Battery:** the data path is built. Charge and discharge are in the split, and the
  battery's share is reported (`battery_kwh`). **The cost basis is left for M21:**
  battery discharge is valued at the import price for now.
- **Previous bill estimate** sensor, and an example **bill card** (button-card) in
  `docs/cards/`.
- **Today so far:** an `offset_corrected` value alongside the unchanged estimate.
- **A second fix, found on the lab:** a reload less than 30 s after an interval lost that
  interval. The state is now saved on unload.
- **Tests:** 593 pass, 100 % coverage, ruff clean.
- **Lab (9102):**
  - the solar-aware statistic matched an independent calculation to within 5e-7 AUD per
    hour, over 96 hours, with the allowance refund applied;
  - the previous bill came out at **$165.71, complete**, for 28 Aug to 27 Sep: the M11
    acceptance figure.

## 1. What was built

| Commit | What |
|---|---|
| `fb751c1` | Today so far: a live price registered after setup is found, without a reload |
| `af199e4` | Today so far: an offset-corrected value alongside the estimate |
| `23458f8` | Previous bill estimate sensor |
| `5a7ad7d` | Solar-aware appliance cost (with the battery's data path) |
| `3944762` | Docs: INSTALL.md, README, DESIGN (16, 19, 22), CHANGELOG, bill card; manifest `2.2.0-dev4` |
| `b51dbb7` | Today so far: save the state on unload, so a reload keeps the last interval |
| (this) | This report, and the CHANGELOG line for `b51dbb7` |

**Point 1: live prices found after setup** (`__init__.py`):
- `undetected_prices`: the site's price channels left to auto-detection that have no
  price yet. It is empty when the site has no grid meter Today so far could use.
- `_watch_for_prices`, per site, while some price is undetected. It checks:
  - at `async_at_started`;
  - on `EVENT_ENTITY_REGISTRY_UPDATED` (create or update) for an entity whose platform
    is `amber_express` or `amberelectric`;
  - every minute (`PRICE_RETRY`) for 15 minutes after the start (`PRICE_RETRY_FOR`).

  It stops once everything is found, and on unload.
- `_async_redetect_prices`: resolves the sources again. When they changed:
  - the old tracker is stopped and saved;
  - a new tracker loads the same Store (the day carries over);
  - the sensor platform's callback (`SiteRuntime.add_today_sensors`, set in `sensor.py`)
    adds the sensors the site doesn't have yet.
- While a price may still turn up, Today so far's sensors are **kept** in the entity
  registry. They are no longer removed at setup, so a dashboard's entity keeps its ID and
  customisations.

**Point 2: solar-aware cost.** New module `solar.py`:
- `SiteFlows` and `site_flows`, the site's flow sensors:
  - the grid meter (Today so far's if it has an export, else the first general-channel
    meter that has one: `flows_meter`);
  - solar energy, or else solar power;
  - battery charge and discharge, when both are set.
- `split(I, E, S, C, D)`: the house load L = I + S + D − E − C (at least 0), covered by
  solar first, then the battery, then the grid.
- `solar_aware_day`: values one day per interval.
  - The solar share is valued at the feed-in record's −perKwh, plus the allowance extra.
  - Grid and battery are valued at the general record's perKwh.
  - 30-minute records price each of their buckets.
  - An hourly fallback (hourly shares and the hour's mean prices) is flagged
    `lower_precision`.
  - Errors are `SensorDataUnavailable` (naming the sensor) and `PreciseDataUnavailable`.
- `async_power_energy`: a power sensor's mean statistics, converted to kWh per bucket.
- `allowance_extra`, with `export.refund_fractions`: the penalty rate times the share of
  the hour's window export still within the allowance.
- `solar_aware_problem`: the flow checks.

**Point 2: changes elsewhere:**
- `meters.own_sensors_from_entry`: an appliance with `solar_aware` on a general channel
  gets a second `OwnSensor`, with role `solar_aware` and chain `own:<sub>:solar_aware`.
- `statistics.solar_aware_spec`: `…_own_<slug>_cost_solar_aware`.
- `manager.py`:
  - `_async_solar_write` and `_allowance_extra`;
  - the chain waits when the site has no flows;
  - `_appliances` adds `solar_kwh`, `battery_kwh`, `grid_kwh`, `solar_share` and
    `allowance_adjusted`;
  - **export aggregates are now measured before the chains in a run**, so the day just
    imported is adjusted.
- `sensor.py`: **Yesterday solar-aware cost**. It is removed when the option is turned
  off; the statistic and the chain state are kept.
- `config_flow.py`:
  - the appliance form's **Solar-aware cost** checkbox, with the errors
    `solar_aware_channel`, `solar_aware_no_solar` and `solar_aware_no_grid_meter`;
  - a new appliance **reconfigure** step, which toggles the option. A change reloads,
    since the update listener now compares (sub-entry, role).
- `diagnostics.py`: `flows`.
- Strings, translations and icons.

**Point 4: previous bill:**
- `manager.previous_bill_estimate`: the cycle before the current one, with
  `complete` (`data_from` = cycle start, `data_through` = cycle end, no missing days).
- The **Previous bill estimate** sensor (`previous_bill`): the state is that cycle's
  `bill_to_date`, with the lines, dates, days and `complete` as attributes.
- `docs/cards/bill-card.yaml`: a vertical stack of two button-cards.
  - **This bill so far:** the total, "incl. today (est.)", projected, the cycle and the
    data dates.
  - **Previous bill (estimate):** the total, complete or partial, the dates, and a table
    of the lines.
- `docs/cards/README.md`: setup and screenshot placeholders.

**Point 5: offset-corrected value** (`today.py`):
- `offset_corrected(deltas, correction)` (see section 4).
- The tracker takes `offset_w` (the grid meter's current calibration offset).
- Each side accumulates `corrected_kwh`, `corrected_amount` and `corrected_intervals`.
- The summary gives `offset_w`, `offset_corrected`, `offset_corrected_kwh` and
  `offset_corrected_intervals`.
- `compare` adds `estimated_offset_corrected`, `error_offset_corrected` and
  `offset_corrected_intervals`.
- A state saved by b2 (without these keys) loads.

**Unload fix** (`today.py`, `__init__.py`):
- `TodayTracker.async_shutdown` (stop, then save) is the unload callback; Home Assistant
  awaits coroutine unload callbacks.
- A removed site's tracker is marked removed first, so the unload doesn't recreate its
  deleted Store.

## 2. Test results

- **Full suite, 9102 stopped:**
  `uv run pytest --cov=custom_components.amber_energy_dashboard` gives
  - **593 passed**, 0 skipped, 0 failed;
  - **100 % coverage** (5,981 statements);
  - 1 warning: the harness's known `ResourceWarning: unclosed database`. An earlier run in
    this milestone showed 4 of them, on different tests; they come from garbage
    collection of the recorder's SQLite connections.
- `ruff check`, `ruff format --check` and `uv lock --check` are clean.
- **New tests** (26):
  - `tests/test_solar.py` (16):
    - `split` (exporting, no load, disagreeing sensors, battery discharge, charging from
      solar or the grid, a negative reading);
    - the day's valuation against an independent sum, with battery and no load, the
      hourly fallback and refusals, and 30-minute records;
    - `refund_fractions` and `allowance_extra`;
    - on a site:
      - the statistic matching the independent sum, the sensor and its attributes, its
        device, diagnostics, and turning it off;
      - a solar power sensor with battery sensors;
      - waiting without a grid meter;
      - a day without the solar sensor's statistics (skipped, named);
      - a day without feed-in records;
      - lower precision, and its refusal with the fallback off;
      - the export allowance, with a preloaded export day (hours fully, half and not
        within a 2.5 kWh allowance);
    - the appliance flow's three errors, the reconfigure step (off, on, an error, a gone
      channel), and `flows_meter`.
  - `tests/test_sources.py` (8):
    - a price registered after setup (no reload, other platforms and removals ignored,
      no checks once found);
    - the minute retries and their end after 15 minutes;
    - a feed-in price found after the general one (the export side added, the day
      carried over, the sensor kept until then);
    - `offset_corrected` (import, export, passing to the other side, one side only);
    - the tracker's corrected totals (no offset, offset, restart catch-up, a b2 Store);
    - the comparison;
    - the sensor attributes;
    - a reload right after an interval.
  - `tests/test_bill.py` (2): the previous bill (partial: 4 of 31 days), and complete
    (equal to the `bill_estimate` action; a missing day makes it incomplete).
  - `tests/test_sites.py`: removing a site doesn't recreate its Today so far Store.
- **Changed tests:**
  - `test_compare`, for the three new keys;
  - `test_subentry_flow_validation_and_removal`, for `solar_aware: False` in the
    appliance data;
  - `test_feed_in_price_found_later_extends_today`: Today so far's sensors are now kept
    while a price may still turn up.

## 3. Lab verification (VM 9102, HA 2026.10.0)

9102 started from its M19 state (snapshot `m19-lab`); snapshot **`m20-before`** was taken
first.

1. **Install:** dev4 from `git archive HEAD`. The sha256 of `solar.py` and `today.py`
   matched the repository. HA restarted.
   - The integration logged nothing but the expected "custom integration" notice, and
     later one warning: "Usage retention boundary moved from 2026-07-25 to 2026-07-27
     (bisection)", from the run's due rediscovery.
2. **Previous bill estimate:** **165.71**, `complete: true`, cycle 2026-08-28 to
   2026-09-27. That is the M11 acceptance figure ($165.71 with the allowance adjustment,
   against the bill's $165.74 without the card fee).
3. **Stand-in sensors** (template helpers, constant states), with hourly statistics for
   20–23 October imported through `recorder/import_statistics` (synthetic profiles: solar
   2.4 kWh/h at noon, an evening peak):
   - grid import and export, as a new grid meter "Lab grid meter";
   - solar energy;
   - a pool pump (30 % of the load).
4. **Appliance flow:** "Pool pump" with solar-aware on, before the site had a solar
   sensor, gave `solar_aware_no_solar`. Sources was then saved with the stand-in solar
   sensor and "Lab grid meter" as Today so far's meter, and the appliance was created.
5. **`run_now`** (18:45):
   - imported 10-22 and 10-23 (4 `/usage` calls, retention rediscovery included);
   - wrote the solar-aware chain for 10-20 to 10-23 (`lower_precision`, as expected: the
     imported statistics are hourly only);
   - export aggregates for 10-22 and 10-23 were measured in the same run, before the
     chains.
6. **Independent check:** the same days' records were fetched directly (1 call) and the
   solar-aware cost recomputed by hand. Per hour: shares from the stand-in profiles, the
   hour's mean general price, and the mean feed-in value (−perKwh, plus the measured
   1.86 c/kWh in `solarSponge` intervals, all within the allowance).

   | NEM day | Solar-aware (written) | Expected | Normal cost (written) | Expected | Solar kWh |
   |---|---|---|---|---|---|
   | 10-20 | 1.6230 | 1.6230 | 1.9401 | 1.9401 | 2.327 |
   | 10-21 | 1.3888 | 1.3888 | 1.6487 | 1.6487 | 1.824 |
   | 10-22 | 1.4905 | 1.4905 | 1.7872 | 1.7872 | 2.087 |
   | 10-23 | 1.6565 | 1.6565 | 1.9841 | 1.9841 | 2.327 |

   - The largest difference over the 96 hours was 4.98e-7 for both statistics (the
     hourly state's rounding).
   - The final sum was 6.158792, as expected.
   - The metadata is "Pool pump cost (solar-aware)", in AUD, with a sum.
7. **Sensors:** **Pool pump Yesterday solar-aware cost** read 1.656485, against
   **Yesterday cost** 1.984117. Its attributes: `solar_kwh` 2.327295, `grid_kwh` 4.38983,
   `battery_kwh` 0.0, `solar_share` 0.3465, `allowance_adjusted` true and
   `lower_precision` true. Diagnostics `flows` named the lab meter and the solar sensor.
8. **Offset-corrected value:**
   - The lab meter's calibration offset came out at −139.6 W, from 96 hours. It is
     meaningless (synthetic meter against real data), but it is a value.
   - At 18:50 a tick set the readings; +0.5 kWh was then written to the import.
   - At 18:55 the tick gave a cost of 0.80 (0.65 carried + 0.5 × 0.30) and
     `offset_corrected` **0.1465** = (0.5 − 139.6 W × 5 min) × 0.30, with
     `offset_corrected_kwh` 0.488.
9. **The unload fix:**
   - Before it, the 18:45 tick was lost by the reload that saving Sources caused;
     `last_interval_end` stayed at M19's 16:45.
   - With the fix installed, a reload 3 s after the 18:55 tick kept it
     (`last_interval_end` 07:55Z, 0.80).
10. **Reconfigure:** off removed the sensor (HTTP 404); on brought it back with the same
    value (the chain state is kept).
11. Snapshot **`m20-lab`**, then 9102 was shut down.

**Not verified in the lab:**
- **Price detection after setup:** the lab's prices are configured stand-ins. Detection
  needs an entity registered by Amber Express or Amber Electric, which would mean
  installing one of them (more live calls). Covered by tests.
- **The precise 5-minute path:** `recorder/import_statistics` writes hourly statistics
  only, so the lab used the hourly fallback. The precise path is covered by tests.
- **Battery sensors:** covered by tests (the split and the series); no stand-in battery.

## 4. Open questions assigned to the milestone

- **Point 1, the start-up race.** Entity registry entries persist across restarts, so on
  a plain restart detection should find Amber Express's entities even before it is set
  up. The likely causes are:
  - Amber Express was installed (or its entities first registered) after this
    integration's setup;
  - or Amber Express changes its entities' unique IDs at its own setup.

  Both are covered by the fix. After updating, the log line "Live price found for …"
  says when and from where the price was found.
- **Point 2, how the share is defined:**
  - From the net flows per interval: L = import + solar + discharge − export − charge.
  - Solar first: Ls = min(S, L); then the battery, Lb = min(D, L − Ls); the rest from the
    grid.
  - The appliance takes the house's proportions.
  - With L = 0, its energy counts as grid.

  The documented limits (INSTALL.md, "Solar-aware cost"):
  - netting within the interval;
  - the house-average share;
  - sensors must agree (the calibration offset isn't applied);
  - battery valued at the import price until M21;
  - lower precision after about 10 days;
  - a negative feed-in price gives the solar part a negative cost;
  - general channel only.
- **Point 2, the allowance-adjusted feed-in value.** For an interval in the penalty
  period, the value is perKwh plus the day's measured penalty rate × the share of that
  hour's window export still within the allowance:
  - 1 while the period's window export so far, the hour included, is within it;
  - the part within it, for the hour that passes it;
  - 0 after.

  This is the marginal value of a further exported kWh. It is consistent with the
  adjusted compensation, which refunds in time order. A day without export aggregates is
  valued unadjusted and flagged (`allowance_adjusted` false).
- **Point 3, battery:** data path only; the cost basis is M21 (see section 5).
- **Point 5, the offset:**
  - The correction is the offset × the time since the previous boundary (5 minutes; the
    downtime after a restart).
  - It goes on the import when the interval's import is at least its export, else comes
    off the export.
  - A side driven below zero passes the rest to the other side, so the net changes by
    exactly the correction.
  - The offset used is the current one at each tick; it changes after each morning run.

## 5. Deviations, decisions and points for the planning chat

1. **Battery cost basis deferred to M21**, as the brief allowed. The charge and
   discharge series, the battery share and `battery_kwh` are in place. The pool needs
   more:
   - a site-wide running state per day, processed in date order across all appliances;
   - rebuilds after revisions (every later day's basis changes);
   - its own tests.

   Done properly, that is a milestone of its own.
2. **Today so far's sensors are kept while a price may still turn up** (they were removed
   at setup when no price was found). If no price ever appears, the two entities stay in
   the registry as "not provided" until a price is set to none in Sources.
3. **Export aggregates are measured before the chains in a run** (they were after), so
   the solar-aware value of the day just imported includes the refund.
4. **The flows' grid meter** is Today so far's meter if it has an export, else the first
   general-channel meter with one. There is no separate choice. A site with two grid
   meters picks via the Sources step's existing "Grid meter for Today so far".
5. **Without the flows at run time** (the solar sensor or the grid meter removed), the
   solar-aware chain **waits** rather than skipping days. Days can then age out of
   retention.
6. **The appliance reconfigure step** toggles solar-aware cost only. Name, sensor and
   channel stay fixed, as before.
7. **Turning solar-aware cost on for an existing appliance** starts its statistic at the
   appliance's chain start: the later of retention and the sensor's first statistics.
   Days older than about 10 days use the hourly path (lower precision). A day whose site
   sensors have no statistics is skipped.
8. **A second fix, from the lab:** Today so far lost the last interval on a reload within
   30 s of it. This was pre-existing since dev3.
9. **Version:** the manifest says `2.2.0-dev4`, which sorts **below** `2.2.0b2`. A
   production test of this work should be tagged `2.2.0b3`.
10. **Proposed design changes:**
    - M21: the battery cost basis (section 22's pool), including how revisions rebuild
      it.
    - Decide on applying the offset to Today so far once a few days of
      `error_offset_corrected` against `error` are in.
    - Consider whether solar-aware cost should also cover a 2.1 own-sensor mapping or a
      converted feed-in appliance (it is general-channel appliances only).

## 6. Live Amber API calls

**14**, all with the test key, between 18:41 and 18:56 Sydney time (outside 07:17 to
07:27):
- 9 `/sites`, one per setup of 9102's entry:
  - boot 18:41 and restart 18:43;
  - adding the grid meter 18:44, saving Sources 18:45 and adding the appliance 18:45;
  - restart 18:48 and reload 18:55;
  - two reconfigures 18:56.
- 4 `/usage` in `run_now` (18:45:30 to 18:45:43): two new days and the retention
  rediscovery (the run's own count, `sites_usage=4`).
- 1 `/usage` fetched directly from claude-dev for the independent check (18:45:56).

The test suite makes none.

## 7. Lab state left behind

- **9102 `amber-test-m3`: stopped**, on dev4 (code at `b51dbb7`), in the state of the new
  snapshot `m20-lab`:
  - a second grid meter "Lab grid meter", and an appliance "Pool pump" with solar-aware
    cost;
  - four stand-in template helpers (lab import, lab export, solar energy, pool pump) with
    imported hourly statistics for 2026-10-20 to 10-23;
  - Sources: the stand-in prices, "Lab grid meter" for Today so far, and the stand-in
    solar sensor.
  - Statistics written by the check: the lab meter's and the pool pump's cost statistics
    and the solar-aware statistic for 10-20 to 10-23. A REST write put the lab import at
    +0.5 kWh briefly.

  Roll back to `m20-before` for the M19 state.
- **New snapshots:** `m20-before` (the M19 lab state on dev3) and `m20-lab`. The earlier
  ones are kept.
- No `.claude_work` folder was created. SSH add-on protection was not touched. No other
  clones exist. VM 101 was not touched.

## 8. Recommended next steps

1. Review, especially:
   - point 1 (the battery cost basis as M21);
   - point 2 (sensors kept while a price may turn up);
   - point 5 (waiting without flows);
   - point 9 (tag `2.2.0b3` for production).
2. Screenshots for the new placeholders: `solar-aware-cost`, `bill-card` and
   `bill-card-previous`.
3. In production, after updating:
   - look for "Live price found for …" in the log, and check that Today so far starts on
     its own after a restart;
   - after a few days, compare `error` with `error_offset_corrected` in
     `yesterday_estimate_vs_billed`;
   - turn on solar-aware cost for an appliance and check yesterday's split against
     intuition (a daytime appliance should show a large `solar_share`).
4. M21: the battery cost basis (DESIGN section 22).
