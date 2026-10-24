# Milestone 21 report: release preparation for 2.2.0

Date: 2026-10-24 (Sydney). Branch `feature/v2.2`, after the 2.2.0b3 beta (`d0ecb3a`).
Pushed; nothing is merged or tagged (the release candidate waits for production to confirm
b3). **Stopped for review.**

**Summary.**
- **Upgrade paths, all clean** (section 3): 2.1.1 with two own-sensor mappings, 2.0.1 with
  a migrated YAML kit, and 2.2.0b2, each upgraded to the 2.2 head. In every case:
  - no entity ID changed and no entity became unavailable;
  - every statistic was byte-identical before and after;
  - the Store moved to the site key with its chains intact;
  - Repairs issues were as expected.

  The conversion of the two 2.1 mappings kept the reconciliation sensors' entity IDs.
  Undo, and a re-run, of a migration recorded by 2.0.1 work on 2.2.
- **Fresh installs** on HA **2026.9.0** (the declared minimum) and **2026.10.3** (the newest
  offered) behaved identically. Their statistics are byte-identical to each other, and
  there was nothing from the integration in either log.
- **Docs** rewritten as one set for new users, with "Upgrading from version 2.1" as its own
  section (INSTALL.md section 9, and a README section), signposted at the top of both.
  **20 screenshots** are needed (section 5).
- **CHANGELOG:** one `[2.2.0] - Unreleased` entry replaces b1 to b3.
  `reports/release-notes-v2.2.0.md` is ready.
- **Review pass:** three fixes.
  - **Diagnostics leaked the grid meter's name.** It is now redacted.
  - **A daily warning in normal operation** ("retention boundary moved", on a rolling
    window) is now info.
  - **Strings** were made consistent.
- **HACS default list:** the repository meets the published requirements. `country: AU`
  was added. What is left is publishing 2.2.0 as a full release and opening the
  `hacs/default` PR (section 4).
- **Tests:** 595 pass, 100 % coverage, ruff clean. CI: section 2.

## 1. What was built

| Commit | What |
|---|---|
| `f13837c` | Diagnostics: the grid meter's name is redacted in each site's part |
| `9e934c5` | A retention boundary move is logged as info, not a warning |
| `501cbc7` | Strings: site settings, evening retries, grid meters and appliances |
| `4e3e78c` | `hacs.json`: `"country": "AU"` |
| `f4243d6` | Docs for 2.2: new users first; upgrading from 2.1 in its own section |
| `368939e` | CHANGELOG: one `[2.2.0]` entry; `reports/release-notes-v2.2.0.md` |
| `a49a2c5` | DESIGN: release preparation in section 16; meter names redacted (section 23) |
| (this) | This report |

**Code changes (review pass):**
- `diagnostics.py`: each site's part goes through `async_redact_data` with `SITE_REDACT`
  (the entry's keys plus `meter`).
  - Before: `sources`, `flows.meter` and `today.meter` carried the grid meter's
    user-given name unredacted. DESIGN section 23 says names are redacted.
- `manager.py`: "Usage retention boundary moved from … to …" changed from warning to info.
  - On the lab account the boundary now rolls: 07-25 → 07-27 in M20, and 89 days back
    again today. So the daily check moves it a day at a time, which would warn every day.
  - A failed check still warns.
- `strings.json` / `translations/en.json` (kept identical):
  - The options/reconfigure **settings step** is titled "Import settings", as in the menu
    (it was "Import schedule").
  - The setup schedule step and the settings step mention the **evening retries**.
  - **Site name step:** "To rename it later, use **Rename site** in the site's settings;
    areas and labels are set on the device's page". It used to send users to the device
    page, which was the advice from before dev3.
  - **Channels step:** "for the NMI ending …1234".
  - **Usage mode and fallback:** "own sensors" became "grid meters and appliances".
  - **Options:** "options" became "the site's settings" in:
    - the energy fix flow's migrate step and its abort;
    - the `bill_estimate` description;
    - two code-built messages (`__init__.py` and `migration.py`).
  - The `probe_retention` action is named "Re-check retention", like its button.
  - Reauth says "Repairs item", as the docs do.
- **Tests:**
  - `test_solar_aware_statistic_and_sensor` and `test_today_so_far_sensors` assert that
    the meter's name is redacted and appears nowhere in the diagnostics. Both fail
    without the fix.
  - `test_probe_retention_record_keeps_previous_boundary` pins the INFO level.

## 2. Test results

- **Full suite, 9102 and 9103 stopped:**
  `uv run pytest --cov=custom_components.amber_energy_dashboard` gives:
  - **595 passed**, 0 skipped, 0 failed, in 8 min 0 s;
  - **100 % coverage** (6,001 statements);
  - 4 warnings, all the harness's known `ResourceWarning: unclosed database`. They were
    on `test_own_sensor_entities_have_their_own_device`,
    `test_retention_reverify_self_corrects[stable]` and `test_repairs_find_their_site`
    (2).
- `ruff check`, `ruff format --check` and `uv lock --check` are clean.
- **CI on GitHub** (`e905d5a`, the head with this report's first version): Tests
  (40094493681), hassfest (40094493663) and HACS validation (40094493666) all succeeded.
  The HACS action ran with `hacs.json`'s new `country` key.

## 3. Lab verification

**Method.**
- A capture script recorded each install's state over the websocket API and SSH:
  - every entity (unique ID → entity ID, device, sub-entry, disabled), the devices, the
    entries and sub-entries, and the states;
  - every statistic of the integration: row count and a hash of all (start, state, sum,
    mean) rows;
  - the Repairs issues;
  - the Store files: version, marker, chains, and a hash of the data.
- Before and after each upgrade were compared.
- Installs were from `git archive` of the stated ref. The sha256 of `__init__.py` matched
  the repository each time.
- HA was restarted through the API. Each check waited for `RUNNING`, and 60 s between
  restarts.

### 3.1 2.1.1 with two own-sensor mappings → 2.2 head, then the conversion (9102, HA 2026.10.0)

**Starting state.** 9102 was rolled back to `pre-m17`: 2.1.1, with the stand-in sensors
mapped as 2.1 own sensors on E9 (general) and B9 (feed-in).
- I imported a week of hourly statistics for the stand-ins (import = Amber's E9 hourly
  energy, export = B9 + 14 Wh/h).
- A **Run now on 2.1.1** then imported 10-22 and 10-23 and priced both mappings for 10-16
  to 10-23 (8 days each, hourly fallback).
- Snapshot `m21-p1-211`. That gave 19 entities, 9 statistics (2 of them own-sensor costs),
  and 0 issues.

**Upgrade to head:**

| Check | Result |
|---|---|
| Entity IDs | **none changed** (19 kept; one user-renamed ID, `sensor.amber_lag`, kept) |
| New entity | `sensor.amber_energy_dashboard_previous_bill_estimate` (the bill estimate is on): **165.71**, `complete` true, cycle from 08-28 |
| Sub-entries | `site` "Site …1234" added; both `own_sensor` mappings unchanged |
| Devices | site device "Amber Energy Dashboard (site)", model "Endeavour Energy · NMI …1234", now in the site sub-entry; both own-sensor devices in their own sub-entries |
| Unavailable entities | none |
| Statistics | **9 of 9 byte-identical** |
| Store | moved from the entry key to `<entry>_<site>`; marker 10-23; both chains: marker and last written 10-23, 8 day records |
| Repairs | `convert_own_sensors_<site key>` raised |
| Log | nothing from the integration (only the custom-integration notice, and zeroconf noting the twin clone) |

**Conversion** (the Repairs fix flow over REST, as the UI drives it):
- The plan step defaulted to E9 as import and B9 as export. I named it "Whole house
  meter".
- The confirm step showed the plan. Submitting it returned `create_entry`.
- **Result:**
  - one `grid_meter` sub-entry, "Whole house meter · grid meter", with device "Whole house
    meter", model "Grid meter";
  - the issue cleared;
  - **9 of 9 statistics byte-identical**;
  - chains moved to `own:<sub>:import` / `:export` (marker 10-23, 8 days each);
  - **both reconciliation sensors kept their entity IDs**
    (`sensor.whole_house_energy_consumed_own_sensor_reconciliation`, `…produced…`) under
    the grid meter's new unique IDs;
  - `sensor.whole_house_meter_calibration_offset` was added.
- **Calibration offset:** 28.3 W, `median_w` **14.0** (192 hours, 8 days).
  - The 6 days built from Amber's data give exactly the injected 14 W.
  - The mean is pulled up by 10-22/10-23, whose stand-in readings are synthetic.
- Snapshot `m21-p1-converted`.

### 3.2 2.0.1 with a migrated YAML kit → 2.2 head (9103, a new clone, HA 2026.9.3)

**Building the 2.0.1 state:**
- A fresh linked clone of the template.
- `v2.0.1` installed and set up with the test key (automatic schedule). Its first run
  imported **07-27 to 10-23 (89 days)** with 15 `/usage` calls; the boundary 07-27 was
  found by bisection.
- **A v1-kit layout, built by hand.** A real v1 install needs about 90 live calls of
  backfill, as in M6b. Instead:
  - **Statistics:** `sensor.amber_energy_import` and `sensor.amber_energy_export` (source
    `recorder`) as daily lumps at local midnight, as the kit writes them.
    - **Synthetic** for 05-01 to 07-26.
    - **Equal to the integration's NEM-day totals** from 07-27 on.
    - 176 lumps each.
  - The kit's cost sensors: one zero row each, as on a real v1 install (M6b).
  - **Helpers and automation:**
    - the two `input_number` running totals;
    - the kit's automation from `legacy/v1/daily_automation.yaml`
      (`automation.amber_usage_daily_statistics_import`, on).
  - **Energy preferences** on the legacy sensors.
- **2.0.1's `migrate_v1`:**
  - The dry run found layout **v1**, parity **passed** (89 of 89 days, 0 mismatches) and
    88 rows to copy per energy statistic.
  - The real run completed:
    - the Energy dashboard was switched to the integration's statistics;
    - the automation was turned off;
    - `legacy_cleanup` was raised;
    - the energy sums ended at the legacy totals (2758.934 / 1344.753 kWh).
- Snapshot `m21-p2-201`.

**Upgrade to head:**

| Check | Result |
|---|---|
| Entity IDs | **none changed** (8 kept); the 2.1 buttons Run now and Re-check retention added |
| Entry | title "Amber …1234" → **"Amber Energy Dashboard"** (the 2.1.1 default-title rule) |
| Sub-entries and device | `site` "Site …1234"; device in it, model "Endeavour Energy · NMI …1234" |
| Statistics | **5 of 5 byte-identical** (the copied history included) |
| Store | moved to the site key. The `migration` record is kept: status completed, changes backup, copy, rebase, energy, automations, completed |
| Repairs | `legacy_cleanup` re-raised under the site key (the helpers and automation still exist) |
| Settings menu | includes **Undo migration** |
| Log | nothing from the integration |

**Undo on head of a migration recorded by 2.0.1:**
- `energy_restored` true: the Energy dashboard is back on `sensor.amber_energy_import`.
- The kit automation is **on** again.
- `legacy_cleanup` was removed.

**Re-run on head:**
- Parity passed and the Energy dashboard was switched again.
- The automation was turned off.
- **Statistics byte-identical** to before the undo (the re-base was a no-op).

### 3.3 2.2.0b2 → 2.2 head (9102, HA 2026.10.0)

- 9102 was rolled back to `m20-before`: the M19 lab state, with a converted grid meter,
  stand-in prices, Today so far running, and the site renamed "Lab site".
- The exact `v2.2.0b2` tree was installed and restarted (snapshot `m21-p3-b2`), then head.

| Check | Result |
|---|---|
| Entity IDs | none changed; only `…_previous_bill_estimate` added (165.71) |
| Statistics | 7 of 7 byte-identical |
| Store and the Today so far Store | **both byte-identical** (no migration) |
| Repairs | none before, none after |
| Today so far | the b2 Store loaded. The next tick (22:50) continued the day: cost 0.65, 4 intervals, `offset_corrected_intervals` 0 (the lab meter's offset is unknown: its stand-ins are constant) |
| Log | nothing from the integration |

### 3.4 Fresh installs (9103, rolled back to the template state each time)

**HA 2026.9.0:** core was downgraded from 2026.9.3 with `ha core update --version
2026.9.0`.
- Config flow over REST: API key → site → **site name "Home"** (placeholders: network,
  "…1234", active from) → channels ("NMI ending …1234"; only the last 4 digits are
  shown) → schedule, with **Add to the Energy dashboard** offered and ticked. The entry
  was titled "Amber Energy Dashboard".
- First run: 89 days, **15 `/usage` calls**.
- Device "Home", model "Endeavour Energy · NMI …1234".
- 10 entities with the pinned `…amber_energy_dashboard_…` IDs; 5 statistics.
- The **grid source was added** to the Energy dashboard (`energy_setup.added`).
- No issues, and nothing from the integration in the log.
- **Flows opened:**
  - Add grid meter (name, import, export);
  - Add appliance (name, sensor, solar-aware);
  - Add site (`no_new_sites`: the test key sees one site);
  - Sources ("none (neither Amber Express nor Amber Electric is set up for this site)").

**HA 2026.10.3** (`ha core update --version 2026.10.3`, the newest the supervisor
offered):
- The same flow, run and checks: 15 `/usage` calls, and the same 10 entity IDs.
- **5 statistics byte-identical to the 2026.9.0 install's.**
- No issues; nothing from the integration in the log (no deprecation warnings).
- Every settings step opened (Import settings, Bill estimate, Export allowance, Rename
  site), as did the three sub-entry flows.

**Differences found between the paths:** none in behaviour. Expected differences only:
- the Previous bill estimate sensor appears where the bill estimate is on;
- 2.0.x installs gain the 2.1 buttons;
- 2.0.x "Amber <NMI>" titles become the default title.

### 3.5 The review changes on the lab (9102, HA 2026.10.0)

- `m21-p1-converted` with the M21 head installed.
- A stand-in general price (a template helper, 0.30 $/kWh) was set in Sources, so Today so
  far runs on "Whole house meter".
- **Downloaded diagnostics** (204 KB):
  - `today.meter` is `**REDACTED**`;
  - "Whole house meter" and the stand-in sensors' names appear nowhere;
  - no ten-digit run in the file is the NMI.
- The settings step is served as **"Import settings"** (`frontend/get_translations`).
- Nothing from the integration in the log.
- Snapshot `m21-lab`.

## 4. Open questions assigned to the milestone

### HACS default-list readiness

Checked against hacs.xyz "Include default repositories" and "Integration".
- **Met:**
  - Public GitHub repository, addable as a custom repository (that is how it is installed
    today).
  - **Description:** set ("Unofficial Home Assistant integration that imports Amber
    Electric usage and cost history into the Energy dashboard.").
  - **Topics:** amber-electric, energy, energy-monitoring, hacs, hacs-integration,
    homeassistant.
  - Issues enabled.
  - Not archived.
  - **Licence:** MIT, recognised by GitHub.
  - **Releases:** full releases exist (2.1.1 is marked Latest; the betas are
    pre-releases).
  - **`hacs.json`:** has `name`; now also `country: "AU"`. HACS asks country-limited
    repositories to declare it, and Amber operates in Australia only.
  - **One integration** in `custom_components/`.
  - **Manifest:** has `domain`, `documentation`, `issue_tracker`, `codeowners`, `name` and
    `version`.
  - **Brand assets:** `custom_components/amber_energy_dashboard/brand/icon.png` (and
    `@2x`).
  - The domain is not a core domain. It doesn't override core, and it isn't a beta of a
    core integration.
  - **Actions:** the HACS action (`hacs/action@main`, category integration, **no
    ignores**) and hassfest run on every push and pass. See section 2 for this head.
- **Still to do** (not done; nothing was submitted):
  1. Publish **2.2.0 as a full release** after its CI is green. HACS requires the release
     to be created after the actions pass. 2.1.1 already qualifies, but the PR should
     point at the current version.
  2. Open a PR on **`hacs/default`**:
     - add `r5e/ha-amber-energy-dashboard` alphabetically to the `integration` file;
     - fill in the template completely;
     - submit from the personal account (the owner), and mark it ready for review.

     HACS warns that reviews can take months.
  3. Optional: the HACS action could be pinned to a release instead of `@main`. It isn't
     required.
- **Not a blocker:** HACS's own list shows "icon not available" for local brand icons (a
  known HACS limitation since HA 2026.3, already in the README).

### Upgrade paths: anything to report?

No defects. Two observations:
- **Rate budget on the lab account.** At 23:00 and 23:06 the run's first response showed
  `RateLimit-Remaining` 22 and 24 after only about 17 of our own calls in the window. So
  another client was also using the shared budget, as noted since M1. Neither run was
  stopped (the reserve is 15).
- **The 2.0.1 lab kit is synthetic** (see 3.2). The real-kit path (published v1 kit, real
  backfill, real migration) was verified in M6b and M8. This milestone checks that a
  migration recorded by 2.0.1 survives the 2.2 layout move.

## 5. Deviations, decisions and points for the planning chat

1. **Diagnostics redaction fix** (`f13837c`): a privacy defect found by the review. It was
   present since b2 (Today so far) and b3 (flows). It should go into the next beta or RC.
2. **Log level** (`9e934c5`): a boundary move is info. "No noisy warnings in normal
   operation" now holds. The remaining warnings are real conditions:
   - a skipped site;
   - an interrupted write;
   - days skipped as older than retention, or as a gap;
   - a retention self-heal;
   - a failed retention check;
   - failed export measuring;
   - exceptions.
3. **String changes are user-visible** but small; no entity or state names changed. The
   `probe_retention` action is now *displayed* as "Re-check retention"; its service ID is
   unchanged.
4. **DESIGN section 16** numbered the b3 work "21". This release preparation is recorded
   under the same item rather than renumbering.
5. **Docs structure:**
   - INSTALL.md sections 1–8 now describe 2.2 only. Everything about updating from 2.1 is
     in the new **section 9**:
     - the backup warning and no downgrade;
     - what changes on the integration page;
     - the conversion, step by step;
     - new things to set up.
   - Both INSTALL.md and README open with a signpost to it. MIGRATION.md has a short note
     for installs migrated before 2.2.
   - The old integration-page screenshot (2.0, "Add own energy sensor") is no longer
     shown. It is replaced by a placeholder.
6. **Release notes** say the betas "ran in production before this release". Adjust it if
   the RC is tagged before production confirms b3.
7. **Screenshots** still needed. The maintainer to supply; paths are relative to
   `docs/images/`, and each place is marked `TODO(screenshot)` in the docs.

   **New images (11):**

   | File | Where | What it should show |
   |---|---|---|
   | `site-name-step.png` | INSTALL 3, step 4 | The "Name this site" step: network, NMI ending, active-from date, a name such as "Home" |
   | `integration-layout.png` | INSTALL 4 | The integration page with the account entry, a **Site** row, a grid meter and an appliance; the buttons Add site, Add grid meter, Add appliance |
   | `add-site.png` | INSTALL 4, "More than one site" | The "Add a site" step (or its `no_new_sites` message on a one-site account) |
   | `add-grid-meter.png` | INSTALL 5 | The "Add a grid meter" form: name, import sensor, export sensor |
   | `add-appliance.png` | INSTALL 5 | The "Add an appliance" form, with **Solar-aware cost** |
   | `solar-aware-cost.png` | INSTALL 5 | An appliance's device page with Yesterday cost and **Yesterday solar-aware cost** |
   | `sources-step.png` | INSTALL 5 | The Sources step: the detected prices listed, the price fields, the solar and battery fields |
   | `today-so-far.png` | INSTALL 5 | The site device with Today's cost so far and Today's feed-in so far |
   | `convert-own-sensors.png` | INSTALL 9 | The Repairs conversion's plan step: the mappings, grid meter import and export, the name |
   | `bill-card.png` | cards/README | The bill card on a dashboard, both parts |
   | `bill-card-previous.png` | cards/README | The previous-bill part close up, with its lines |

   **Retakes on 2.2 (9):**

   | File | What changed |
   |---|---|
   | `options-menu.png` | Now the site settings menu: Import settings, Sources, Bill estimate, Export allowance, Rename site, Refresh channels, Migrate (the current one is 2.1.0-rc1's) |
   | `10-choose-site.png` | Its text still says "Each site is set up as its own entry" |
   | `12-import-schedule.png` | It lacks "Add to the Energy dashboard" and the evening-retry sentence (take it with an empty Energy dashboard) |
   | `13-name-and-assign.png` | The device is now named after the site |
   | `device-page.png` | The device named after the site, with the model line "<network> · NMI …1234" |
   | `new-sensors.png` | It lacks Previous bill estimate (and the Today so far sensors) |
   | `17-import-settings.png` | The new title "Import settings" and the label "Hourly fallback for grid meters and appliances" |
   | `18-migration-dry-run.png`, `19-migration-complete.png`, `20-undo-migration.png` | Check them: they are reached from the site's settings now (the dialog title may differ) |

   `15-integration-page.png` is no longer referenced. It can be deleted once
   `integration-layout.png` exists.

## 6. Live Amber API calls

About **67**, all with the test key, between 22:37 and 23:31 Sydney time (outside 07:17 to
07:27). The test suite makes none.

- **`/usage`: 49**, counted from the run summaries:
  - 4 in the 2.1.1 Run now on 9102 (22:39);
  - 15 in 2.0.1's first run on 9103 (22:49–22:50);
  - 15 in the 2026.9.0 fresh install (23:00);
  - 15 in the 2026.10.3 fresh install (23:06).
- **`/sites`: about 18**, estimated from setups and flows (one each):
  - **9102, 9:**
    - boots at 22:37, 22:42 and 23:28;
    - restarts at 22:40, 22:44, 22:46 and 23:30;
    - the conversion's reload;
    - the Sources reload (23:31).
  - **9103, 9:**
    - on 2.0.1: its config flow, its setup, and the restart onto head;
    - for each fresh install: the config flow's key check, the setup, and the "Add site"
      flow.

## 7. Lab state left behind

- **9102 `amber-test-m3`: stopped**, at the new snapshot **`m21-lab`**:
  - 2.1.1 upgraded to the M21 head and converted ("Whole house meter");
  - a stand-in general price helper set in Sources;
  - the stand-in sensors with hourly statistics for 10-16 to 10-23.
- **New 9102 snapshots:** `m21-p1-211`, `m21-p1-converted`, `m21-p3-b2` and `m21-lab`.
  Earlier ones are kept.
- **9103 `amber-test-m21`: destroyed** (it held the `m21-fresh`, `m21-p2-201` and
  `m21-fresh-2026-10-3` snapshots).
- No `.claude_work` folder was created. SSH add-on protection was not touched. VM 101 was
  not touched. Only 9102 and the template remain in the pool.

## 8. Recommended next steps

1. Review, especially:
   - the diagnostics fix (point 1), which suggests a b4 or going straight to RC1 with it;
   - the docs structure (point 5);
   - the screenshot list (point 7).
2. Once production confirms b3 (and ideally runs the M21 head as b4 for a day):
   - set the manifest version and the CHANGELOG date;
   - tag `v2.2.0rc1` (or `v2.2.0`) with the release notes, after CI is green.
3. Supply the 20 screenshots (point 7).
4. After 2.2.0 is a full release: open the `hacs/default` PR (section 4).
5. 2.3: the battery cost basis (DESIGN section 22).

## Addendum: CI

On `e905d5a`: Tests 40094493681, hassfest 40094493663 and HACS validation 40094493666,
all **success**.
