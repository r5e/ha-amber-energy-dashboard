# Milestone 19 report: 2.2.0-dev3 (site sources, Today so far, mean offset, skipped sites, Rename site)

Date: 2026-10-24 (Sydney). Branch `feature/v2.2`, after the 2.2.0b1 beta (`4962dac`).
Pushed; nothing is merged or tagged. **Stopped for review.**

**Summary.**
- **Sources** (site settings): live general and feed-in prices, found automatically from
  Amber Express (first) or the core Amber Electric integration for the same site, or
  chosen by hand, or none. Units are checked. Optional solar (power and/or energy) and
  battery (charge/discharge) sensors are stored for M20.
- **Today so far:** **Today's cost so far (estimated)** and **Today's feed-in so far
  (estimated)**, from the grid meter priced every 5 minutes at the live price.
  - Display only; never written into statistics.
  - Each day's estimate is kept and compared with the billed day
    (`yesterday_estimate_vs_billed`).
  - **Bill to date** gets `including_today_estimated`.
- **Calibration offset:** the state is now the **mean**; `median_w` and `iqr_w` are
  attributes.
- **A site the key no longer sees is skipped** with a Repairs issue; the account's other
  sites keep running (M18 point 8).
- **Rename site** step (M18 point 10).
- **Tests:** 567 pass, 100 % coverage, ruff clean.
- **Lab (9102):** both "Today so far" intervals priced exactly as expected; the state
  survived a restart; a meter reset was dropped; the rename kept every entity ID.

## 1. What was built

| Commit | What |
|---|---|
| `d3d35b9` | Calibration offset: the mean, with the median as an attribute |
| `94e38de` | Skip a site the key no longer sees, with a Repairs issue |
| `5590e9c` | Rename site step |
| `dc75afe` | Site sources and Today so far (code and tests); manifest `2.2.0-dev3` |
| `2eecd4b` | Docs: INSTALL.md, README, DESIGN (sections 16, 21, 22, 23), CHANGELOG |
| (this) | This report |

**New module `sources.py`:**
- `detect_prices`: looks for the current price sensors of the two Amber integrations in
  the entity registry, by unique ID, matching the site ID case-insensitively. Disabled
  entities are skipped.
  - Amber Express (`amber_express`): `<site>_general_price` and `<site>_feed_in_price`.
  - Amber Electric (`amberelectric`): `<site>-current-general` and `<site>-current-feed_in`.
- `resolve`: gives the site's sources from its options.
  - A price key absent from the options means detect it.
  - A price key set to None means no price.
- `price_factor` and `price_from_state`: $/kWh, AUD/kWh, c/kWh, ¢/kWh, $/MWh and
  AUD/MWh, converted to $/kWh.
- `today_meters` and `today_meter`: the site's grid meters with their import on a
  general channel, and the one "Today so far" uses.

**New module `today.py`** (`TodayTracker`, one per site):
- **Pricing:**
  - On each 5-minute UTC boundary, each side's energy since the last boundary is priced
    at the last price state before the boundary.
  - Price states are sampled by a state listener, so a price arriving just after a
    boundary belongs to the next interval.
  - Energy readings are converted to kWh with HA's `EnergyConverter`.
- **NEM days:** at the first boundary of a new day, the old day goes into a 14-day
  history. Readings carry over only if the previous interval was the day's last.
- **Edge cases:**
  - Energy without a usable price is counted as `unpriced_kwh`.
  - A meter reset drops that interval.
  - After a restart the same day, energy used while HA was down is priced at the first
    interval's price; across midnight the readings start afresh.
- **Store:** its own small Store, `amber_energy_dashboard.<site key>_today`, saved after
  a 30 s delay and at shutdown. It is removed with the site.
- **`compare`:** takes the latest history day before today that has billed data and
  compares it with the bill (estimate minus billed, as amount and kWh, with `complete`
  when all 288 intervals were priced).

**Changed modules:**
- `config_flow.py`:
  - **Sources** step, with checks: `price_unit`, `not_power`, `not_cumulative_energy`,
    `sensor_not_found`, `same_sensor`.
  - **Rename site** step.
  - The menu is now Import settings, Sources, Bill estimate, Export allowance,
    Rename site, Refresh channels, Migrate.
  - Import settings keeps the source keys.
  - Reauth accepts a key that sees at least one site.
- `__init__.py`:
  - resolves sources and builds the tracker per site;
  - skipped sites (`AmberRuntimeData.skipped`) and the `site_not_found` issue;
  - reloads when the source settings change;
  - the site title for Repairs labels follows a rename;
  - a removed site's Stores and issue are removed.
- `manager.py`:
  - `today_snapshot` (with the comparison) and `_bill_including_today`;
  - the snapshot's `today` key;
  - `publish()`;
  - `site_title` made public.
- `sensor.py`: the two "Today so far" sensors, and the bill's new attributes.
  - Entity IDs are `sensor.amber_energy_dashboard_today_cost_estimated` and
    `…_today_feed_in_estimated` on the first site, and `<device name slug>_…` on others.
  - The sensors are removed when the feature is off.
- `calibration.py`: the mean, with `median_w`.
- `diagnostics.py`: `sources` and `today` per site.
- `strings.json`, `translations/en.json`, `icons.json`.

## 2. Test results

- **Full suite, 9102 stopped:**
  `uv run pytest --cov=custom_components.amber_energy_dashboard` gives
  - **567 passed**, 0 skipped, 0 failed;
  - **100 % coverage** (5,606 statements);
  - 1 warning: the harness's known `ResourceWarning: unclosed database` (an SQLite
    connection found by garbage collection), this time on `test_mapping_flows_ask_which_site`.
- `ruff check`, `ruff format --check` and `uv lock --check` are clean.
- **CI on GitHub** (`ef54841`, the branch head with this report's first version): Tests
  40067627265, hassfest 40067627270 and HACS validation 40067627271 all succeeded.
- **New tests** (`tests/test_sources.py`, 21):
  - **Units and detection:**
    - price units, and price states (unknown, unavailable, not a number, wrong unit);
    - detection order (Express first, core as a fallback per channel);
    - another site's sensors and disabled ones are ignored;
    - `resolve`: configured, None, detected, and no feed-in channel.
  - **Tracker:**
    - the price at the end of the interval (a price arriving 2 s after the boundary goes
      to the next interval);
    - c/kWh and Wh;
    - unavailable price and unavailable readings, resets, non-numeric readings and
      non-energy units;
    - no price state at all yet;
    - the 5-minute timer;
    - midnight roll-over with readings carried over, and the 14-day history cap;
    - save and restore across a restart; a changed sensor; downtime across midnight;
    - Store removal.
  - **`compare`:** the latest billed day; a fallback to an older day; no billed data;
    zero billed; missing statistic IDs.
  - **On a site:**
    - the sensors (no state class, on the site device, attributes);
    - the bill's `including_today_estimated`: cycle start, a missing day, and controlled
      load or feed-in left out;
    - yesterday's comparison;
    - diagnostics;
    - no "Today so far" without a price, with a price set to none, or with only a
      controlled-load meter;
    - the Sources step: suggestions, every error, save with an explicit none, the reload,
      Import settings keeping the sources, a site without feed-in or a meter, and the
      meter choice with two grid meters;
    - the Store removed with the entry.
- **New tests elsewhere:**
  - `test_sites.py`:
    - a skipped site (issue, the other site runs, no reload loop, the issue clears when
      the site is seen again);
    - deleting a skipped site;
    - all sites unseen leads to reauth (the new key must see at least one), and the issue
      goes with the entry;
    - Rename site via the pencil and the cog (title, device name, `name_by_user`
      cleared, Repairs label, entry title, entity IDs unchanged).
  - `test_meters.py`: the mean against the median (an outlier hour; a "switching" set of
    44 × 14 W and 4 × −40 W gives a mean of 9.5 W and a median of 14 W).
- **Changed tests:** the menu lists (Sources, Rename site); the calibration expectations;
  `_no_real_timers` now also stops the tracker's timer.

## 3. Lab verification (VM 9102, HA 2026.10.0)

9102 started from its M18 state (snapshot `m18-upgrade-211`: an upgraded 2.1.1 install
with one converted grid meter, "Whole house meter", whose sensors are constant template
helpers at 1234.5 kWh). A snapshot `m19-before` was taken first.

1. **Install:** dev3 installed from `git archive HEAD`; the sha256 of `today.py` and
   `sources.py` matched the repository. HA restarted and was `RUNNING` in 42 s. The log
   showed no warnings or errors from the integration. `median_w` was present on the
   calibration offset.
   - The offset itself is unknown here (`hours_used` 0): the lab's meter sensors are
     constants, so there are no usable hours. The mean is verified by the tests only.
2. **No price source:** no "Today so far" sensors. The Sources step showed
   "- none (neither Amber Express nor Amber Electric is set up for this site)" and the
   meter "Whole house meter · grid meter".
3. **Stand-in prices:** two template helpers, `sensor.stand_in_general_price` (0.30 $/kWh)
   and `sensor.stand_in_feed_in_price` (0.05 $/kWh).
   - Submitting the meter's energy sensor as the general price gave `price_unit`.
   - Submitting a price as solar power gave `not_power`.
   - Submitting the two stand-ins saved them; the site reloaded.
4. **Sensors created** on the site device:
   - `sensor.amber_energy_dashboard_today_cost_estimated` and
     `…_today_feed_in_estimated`, both 0.0;
   - `expected_intervals` 184 at 16:21 AEDT (15:21 NEM: correct);
   - `yesterday_estimate_vs_billed` null.
5. **Intervals:** the meter readings and the price were driven with REST state writes.

   | Time (AEDT) | Action | Cost / feed-in | Expected |
   |---|---|---|---|
   | 16:25 | first readings (1234.5 / 1234.5) | 0.00 / 0.00, 0 intervals | baseline only |
   | 16:27 | import +1.0 kWh, export +0.2 kWh | | |
   | 16:29:30 | general price → 0.40 | | |
   | 16:30 | | **0.40 / 0.01**, 1 interval | 1.0 × 0.40; 0.2 × 0.05 |
   | 16:30:20 | general price → 0.20, import +0.5 kWh | | |
   | 16:35 | | **0.50 / 0.01**, 2 intervals | 0.40 + 0.5 × 0.20 |

6. **Restart** (16:35:25; `RUNNING` at 16:35:56):
   - Both sensors came back at 0.50 / 0.01 with 2 intervals.
   - The Store `amber_energy_dashboard.<entry>_<site>_today` held the day, the last tick
     (05:35 UTC) and the readings (1236.0 / 1234.7).
   - After the restart the template helpers rendered 1234.5 again (a lower reading).
   - At 16:40 that interval was dropped as a meter reset (totals unchanged).
   - +0.5 kWh at the template's 0.30 gave **0.65** at 16:45, as expected.
7. **Rename site** (options flow; suggested "Site …1234", placeholder for the test
   account's NMI tail), renamed to "Lab site":
   - the site sub-entry's title and `site_name` are "Lab site";
   - the device name changed (friendly names now start "Lab site");
   - the entity ID list was identical before and after (same hash);
   - no reload;
   - the entry title, "Amber Electric", was set by hand on this lab and was rightly kept.
8. **Diagnostics:**
   - `sources.prices` lists both stand-ins with origin `configured`;
   - `today` names the date and the meter;
   - the API requests since start: 1 (the setup's `/sites`).
9. Snapshot `m19-lab`, then 9102 was shut down.

**Not verified in the lab:**
- **Auto-detection with a real Amber Express or Amber Electric install.** That would mean
  installing one of them with the test key, which costs more live calls. It is covered by
  tests built from both integrations' source code (unique IDs, units and sign checked in
  each repository).
- **A skipped site:** the test key sees one site. Covered by tests.
- **The comparison with a billed day:** it needs a full day of readings and the next
  morning's import. Covered by tests.

## 4. Open questions assigned to the milestone

- **Price sources (point 1):** checked against each integration's source code.
  - Amber Express: unique ID `<site_id>_<channel>_price`, unit `$/kWh`, feed-in negated
    (positive when paid), site ID from its `site` sub-entry.
  - Core Amber Electric (HA 2026.9.3): unique ID `<site_id>-current-<channel>`, unit
    `$/kWh`, feed-in negated.
  - So no sign or unit conversion is needed for either. A hand-picked sensor must follow
    the same convention, as the docs say.
- **Which sensors "Today so far" reads (point 2):** the grid meter's mapped sensors, as
  they are. Since the calibration offset needs raw sensors, the docs say to map raw ones.
  The estimate doesn't apply the offset, so it should differ from the bill by about the
  offset × hours (10 W is about 0.24 kWh a day). `yesterday_estimate_vs_billed` will show
  how much.
- **Mean against median (point 3):** done. With the maintainer's figures (typical hours
  14–15 W, a mean of about 10 W) the state will now read about 10 W.

## 5. Deviations, decisions and points for the planning chat

1. **Reauth accepts a key that sees at least one configured site** (it was every site).
   Otherwise a closed site would block reauth. Any site the new key doesn't see is
   skipped, with the issue. Only a key that sees none of the sites fails setup with
   reauth.
2. **Auto-detection until the step is saved:** a price key absent from the options is
   detected at every setup; once the step is saved, the choice (including none) is kept.
   A price integration installed later is picked up at the next reload.
3. **"Today so far" needs a grid meter whose import is on a general channel.** With
   several, the Sources step asks which (default: the first added). A controlled-load
   meter isn't used: there is no controlled-load price field.
4. **"Today" is the NEM day**, as Amber bills it (01:00 to 01:00 local time during
   daylight saving), so the comparison with the billed day is like for like.
5. **The price for an interval** is the last price state before its end, not a
   time-weighted average.
6. **`yesterday_estimate_vs_billed`** shows the latest estimated day before today that
   has billed data. Before the morning import that can be two days ago; the `date`
   attribute says which. It is computed from the stored estimates (14 days) and the
   billed day records, so it is "recorded" as long as both are kept.
7. **`including_today_estimated`:**
   - It is shown only when the bill's data runs through yesterday, or when today is the
     cycle's first day.
   - It adds one day of fixed charges.
   - It leaves out the controlled load, the export allowance's penalty, and feed-in when
     there is no export side; `including_today_leaves_out` lists the first and the last.
   - On the lab (data through 10-21) it was correctly None.
8. **Solar and battery sensors are only stored** (and shown in diagnostics), as planned.
9. **Rename site clears a name given on the device page** (`name_by_user`), so the new
   name shows everywhere.
10. **Version ordering:** `2.2.0-dev3` sorts **below** `2.2.0b1` (AwesomeVersion:
    `2.2.0-dev3 > 2.2.0b1` is False). A production test of this work should be tagged
    `2.2.0b2`, which sorts above b1.
11. **Proposed design changes:**
    - Consider letting "Today so far" apply the calibration offset (add the offset to the
      import while importing, and take it from the export while exporting). Decide once
      a few days of `yesterday_estimate_vs_billed` show the size of the error.
    - Consider a controlled-load live price, so the bill including today can cover it.

## 6. Live Amber API calls

**4**, all `/sites` from 9102 with the test key, between 16:19 and 16:36 Sydney time
(outside 07:17 to 07:27):
- the boot (16:19);
- the restart after installing (16:20);
- the reload after saving Sources (16:21);
- the persistence restart (16:35).

Diagnostics confirmed one request per setup. There were no `/usage` calls, and the test
suite makes none.

## 7. Lab state left behind

- **9102 `amber-test-m3`: stopped**, on dev3 (code at `2eecd4b`), in the state of the new
  snapshot `m19-lab`:
  - the site renamed "Lab site";
  - two stand-in price template helpers;
  - Sources set to them;
  - one day's "Today so far" state.
- **Readings written by the check:** the REST writes put the meter readings at
  1235.5 / 1236.0 / 1235.0 for a few minutes (16:27 to about 16:46). Their 5-minute
  statistics (and any own-sensor cost for 2026-10-24 on this clone) include those jumps.
  Roll back to `m19-before` for a clean clone.
- **New snapshots:**
  - `m19-before`: the M18 state on dev2, before this milestone;
  - `m19-lab`: as above.

  The earlier ones are kept.
- No `.claude_work` folder was created. SSH add-on protection was not touched. No other
  clones exist. VM 101 was not touched.

## 8. Recommended next steps

1. Review, especially:
   - point 1 (reauth with at least one site);
   - point 2 (auto-detection until saved);
   - point 7 (what "including today" leaves out);
   - point 10 (tag a production test as `2.2.0b2`).
2. Screenshots for the new placeholders:
   - `sources-step`;
   - `today-so-far`;
   - a retaken `options-menu` (it now has Sources and Rename site).
3. In production, after an update with Amber Express or Amber Electric set up:
   - check that Sources shows the detected prices;
   - after a full day, read `yesterday_estimate_vs_billed` the next morning to size the
     estimate's error (and decide on point 11).
4. M20: solar- and battery-aware appliance cost (DESIGN section 22), using the stored
   sources.
