# Milestone 17 report: 2.2.0-dev1 (grid meters, appliances, site identity)

Date: 2026-10-23 (Sydney). Branch `feature/v2.2`, from `main` at `ad83126` (2.1.1 and the
M16 report). Pushed; nothing is merged or tagged. **Stopped for review.**

**Summary.**
- **Site identity:**
  - The site device's model line is "<network> · NMI …<last 4>".
  - Setup asks once for a site name (after the site is chosen), which names the site's
    device. Later renames go through Home Assistant's device page; a name set there is
    the site's name wherever the integration shows one.
  - Entity IDs stay pinned.
- **Two new sub-entry types:** **Grid meter** (import, optional export) and **Appliance**
  (one sensor, no reconciliation). The 2.1 "own energy sensor" type still loads and works
  unchanged, but is no longer offered.
- **Calibration offset** sensor for grid meters with import and export. On 9102 it read
  **14.0 W** for a stand-in meter built with a 14 Wh/hour offset, and **0.0 W** for an
  exact one.
- **Conversion of 2.1 mappings** through a fixable Repairs issue. Verified on 9102:
  - statistics byte-identical;
  - Store chain state moved;
  - site entity IDs unchanged;
  - the reconciliation sensors kept their entity IDs.
- **M18 design** (site-wide sources, solar-aware and battery-aware cost) is in DESIGN
  section 22.
- **Account/site sub-entry restructure:** assessed in section 5. **Recommendation: its
  own milestone, before M18's site-wide settings.** M17 records each mapping's site ID so
  that it doesn't get harder.
- **Tests:** 526 pass, 100 % coverage, ruff clean (section 2).

## 1. What was built

| Commit | What |
|---|---|
| `8dc5082` | Grid meters, appliances, calibration offset, the site model line, and the conversion (details below) |
| `6bbf12e` | Site name (config flow step, device name, titles), `site_id` in mapping data, diagnostics redaction, manifest `2.2.0-dev1` |
| `bc91110` | Docs: INSTALL.md, README, CHANGELOG, DESIGN sections 21 and 22 |
| `edecc78` | Calibration attributes count only the days that gave hours (found on 9102) |
| `733e192` | Site name lookup via `async_get_device_by_identifier` (HA 2026.10 deprecation, found in 9102's log) |
| `4f4bc68` | M15 report: the test account's NMI redacted (section 5, point 14) |
| (this) | This report |

**New modules.**
- `meters.py`:
  - `own_sensors_from_entry` (moved from `__init__`; one `OwnSensor` per priced side);
  - `mapping_device_info`, titles and unique IDs;
  - the conversion: `legacy_mappings`, `default_plan`, `plan_with`, `async_convert`,
    `async_update_convert_issue`.
- `calibration.py`: `billed_days`, `async_offset`, `summarise`.

**Changed modules.**
- `config_flow.py`:
  - `GridMeterSubentryFlow` and `ApplianceSubentryFlow`, which replace `OwnSensorSubentryFlow`;
  - the `site_name` config step and `site_details`;
  - shared sensor checks (`sensor_in_use`, `same_sensor`).
- `repairs.py`: `ConvertMappingsFlow` (steps `plan` and `confirm`); fix flows are now
  chosen by issue ID.
- `manager.py`:
  - `OwnSensor.kind`, `role`, `reconciled`; the chain key `own:<sub>:<role>` for grid meters;
  - `async_refresh_calibration` (after every run, and at start-up);
  - `_appliances`; reconciliation keyed by chain key.
- `sensor.py`:
  - one device and entity set per sub-entry: Import/Export reconciliation, Calibration
    offset, appliance Yesterday energy/cost/compensation;
  - site entities link to their device by identifier only (`site_link`).
- `devices.py`: `site_model`, `site_device_name`, `displayed_site_name`; `entry_title` and
  `is_default_title` take the site name.
- `__init__.py`:
  - setup forgets a chain only when its sub-entry is gone;
  - `_follow_site_renames` (a device-registry listener);
  - the `converting` flag, which suppresses reloads during a conversion.
- `storage.py`: `async_rename_chains`.
- `statistics.py`: `own_cost_spec` takes a display label; `CHANNEL_LABELS` is shared.
- `diagnostics.py`: also redacts `site_name` and `name`.

**Data model (section 21 of DESIGN.md has it in full).**
- **Grid meter:**
  - data `{site_id, name, import_entity_id, import_channel, export_entity_id?, export_channel?, entity_ids?}`;
  - title "<name> · grid meter"; unique ID `grid_meter:<import entity>`.
- **Appliance:**
  - data `{site_id, name, entity_id, channel}`;
  - title "<name> · appliance (general E1)"; unique ID `appliance:<entity>`.
- **Cost statistic IDs are unchanged in form** (`<site>_own_<entity slug>_cost`), so a
  conversion keeps them. A sensor can be priced by one mapping only.

## 2. Test results

- **Full suite, 9102 stopped:** `uv run pytest --cov=custom_components.amber_energy_dashboard`
  (CI's command) gives **526 passed**, 0 skipped, 0 failed, **100 % coverage** (4,810
  statements), 0 warnings. It took 7 min 23 s.
  - An earlier full run that day (before the last fixes, also 526 passed and 100 %) gave
    1 warning: a pytest-HA `ResourceWarning` (an unclosed SQLite connection during
    garbage collection) in the unchanged
    `test_recovery::test_retention_reverify_self_corrects[stable]`. It comes from the
    harness, not from this code, and did not recur.
- `ruff check` and `ruff format --check` are clean, and `uv lock --check` is OK.
- **New: `tests/test_meters.py`** (18 tests):
  - the site model line, the site name and device-page renames (titles, entity IDs);
  - both flows and their errors (controlled-load choice, no feed-in, feed-in only);
  - grid meter pricing, reconciliation and the calibration offset (median with an
    outlier hour, gaps left out, too few hours, billed-day selection, a failure that
    must not stop the run);
  - appliance sensors;
  - conversion: default pair, choices and errors, feed-in only, aborts, read-back
    failures, unconverted mappings unchanged.
- **Changed tests:**
  - `test_own.py`: the add-flow and device-at-creation tests now use the new types (the
    2.1 add flow is gone); everything that loads 2.1 mappings passes unchanged.
  - `test_config_flow.py`: the new `site_name` step; the second-site title now shows the
    site name.
  - `test_importer.py`: diagnostics redact the site name.
- **Every failure path in the new code has a test:**
  - conversion: Store move not read back, sub-entries not read back, a non-2.1 mapping,
    not loaded, nothing to convert;
  - calibration: error, too few hours;
  - flows: no import channel, sensor missing, wrong kind or in use, blank name.
- CI on GitHub: not run on this branch before pushing. The run IDs are available from
  the public API once it completes.

## 3. Lab verification (VM 9102, HA 2026.10.0)

**Setup.**
- Snapshot `pre-m17` was taken first.
- Base state: 2.1.1 pre-release with the M15 stand-in template sensors mapped as 2.1 own
  sensors on E9 (general) and B9 (feed-in).
- The stand-ins had no history before Amber's last imported day, so I imported hourly
  statistics for them with `recorder/import_statistics` (10-15 to 10-21):
  - import equal to Amber's E9 hourly energy;
  - export equal to B9 plus 14 Wh per hour.
- One Run now on 2.1.1 then priced both (7 days each, hourly fallback). That made real
  own-cost statistics: 168 rows each.

**Upgrade to the branch** (`git archive`; sha256 of `meters.py` matched the repo):

| Check | Result |
|---|---|
| Entities (by unique ID, 19) | identical entity IDs and device links |
| Own-cost statistics (2 × 168 rows) | identical (`state` and `sum` compared row by row) |
| Site device | name "Amber Energy Dashboard (site)" kept; model "Endeavour Energy · NMI …1234" (placeholder for the test account's last 4 digits) |
| Supported types | `grid_meter`, `appliance` (2.1 mappings still listed by the API) |
| Repairs | `convert_own_sensors_<entry>`, fixable, listing both mappings |

**Conversion** (Repairs fix flow over REST, as the UI would drive it):
- The plan defaulted to E9 as import and B9 as export. I named it "Whole house meter",
  and the confirm step showed the plan.
- Result:
  - one `grid_meter` sub-entry ("Whole house meter · grid meter"), with device "Whole
    house meter", model "Grid meter", linked via the site device;
  - the issue cleared.
- **Statistics:** identical before and after (both IDs, all rows).
- **Store:** `own:<new>:import` and `own:<new>:export`, each with marker and last written
  10-21 and 7 day records. The old keys are gone.
- **Entities:**
  - all 17 site entities unchanged;
  - the two old reconciliation entities were removed and replaced under the **same
    entity IDs**. They are now "Whole house meter Import reconciliation" (0.000 kWh) and
    "Export reconciliation" (0.336 kWh = 24 × 14 Wh, date 10-21).
- **Calibration offset:** **14.0 W**, 168 hours, IQR 0.0. Before the `edecc78` fix it
  reported `days_used` 14 and `first_day` 10-08; after the fix it reports 7 and 10-15.

**New mappings through the flows** (three new template stand-ins, made with HA's template
config flow, each given the same week of history):
- **Appliance:**
  - Using the grid meter's import sensor was refused (`sensor_in_use`).
  - "Pool heat pump" (30 % of E9's shape) was created as "Pool heat pump · appliance
    (general E9)". There was no channel field, since this site has no controlled load.
- **Grid meter** "Inverter meter" with import = E9 and export = B9.
- **Run now** priced all three for 10-15 to 10-21:
  - the heat pump's cost sum is 6.143476, exactly 0.3 × the whole-house 20.478255;
  - the inverter meter's offset is **0.0 W** (168 hours), and its import reconciliation
    is 0.000.
- The appliance's Yesterday sensors read `unknown`, as designed: yesterday (10-22) was
  not yet published by Amber ("no usage for 2026-10-22 yet"), the same rule as the
  site's Yesterday sensors. Their values are covered by tests.
- Statistic names: new mappings show "Pool heat pump cost (Amber prices)" and "Inverter
  meter import cost (Amber prices)". The converted meter's statistics still show "Amber
  own … cost", because nothing has been written since the conversion. The next write
  updates the metadata name (a fresh grid meter's is checked in tests; not yet observed
  for a converted one).

**Device-page rename:** setting the site device's name to "Home" (`name_by_user`) gave
friendly names "Home Import status", etc. All 25 entity IDs were unchanged. The
single-site title stayed (it is "Amber Electric", which this clone keeps as a user title;
see M16). The rename was then reverted.

**Frontend: are 2.1 mappings still shown once their type is no longer offered?** Not
verified in a browser. I checked the frontend code instead: the package pinned by HA
2026.9.3 (`home-assistant-frontend` 20260826.7), from PyPI, used only as data.
- It fetches every entry's sub-entries and renders each with its title and the
  translated `config_subentries.<type>.entry_type`.
- Only the reconfigure button depends on the type being supported.
- So 2.1 mappings stay listed (as "Own energy sensor (2.1)") and can be deleted.
- 9102's API also still lists them.

**Logs:** no errors from the integration. HA 2026.10 warned that
`device_registry.async_get_device` is deprecated (it breaks in 2027.8); fixed in
`733e192`, and the warning was gone after a redeploy and restart.

## 4. Open questions assigned to the milestone

- **Grid meters with export, and the brief's expected values:** the offset uses Amber's
  hourly net minus the meter's net, over complete billed days, as the median of the
  hours. On the lab stand-ins it reproduced the injected 14 W exactly, and 0 W for the
  exact meter. Production's real CT meter (expected about 14 W) can only be checked
  there, after converting or adding a grid meter.
- **The account/site restructure:** section 5, point 1.

## 5. Deviations, decisions and points for the planning chat

1. **Account/site sub-entry restructure: assessment and recommendation.**

   *What it would mean:* the entry is the Amber account (API key). Each site is a "Site"
   sub-entry owning its device ("+ Site" flow). Grid meters and appliances are
   sub-entries linked to a site.
   - Home Assistant's sub-entries are flat: a grid meter can't be a child of a site
     sub-entry, so it would link to its site by data. That is the `site_id` now recorded.
   - With every device in some sub-entry, HA no longer shows "Devices that don't belong
     to a sub-entry".

   *Migration of an existing install* (one entry per site, today):
   - Keep the entry (so its `entry_id`, Store, issue IDs and energy-setup records stay).
   - Create a site sub-entry with the site's ID as unique ID.
   - Move the site device into it with
     `device_registry.async_update_device(add_config_subentry_id=…, remove_config_subentry_id=None)`.
   - Move each of its 17 entities with `entity_registry.async_update_entity(config_subentry_id=…)`.
   - Unique IDs, and so entity IDs, are unchanged, and statistic IDs depend only on the
     site ID. Both APIs exist in 2026.9.3.
   - **Risk: this is exactly the move behind the 2.0.x sensor drops.** If any platform
     adds an entity whose sub-entry differs from its device's, HA reassigns or drops it,
     depending on platform order. HA already warns, and it breaks in 2027.8.
   - The move must therefore:
     - happen in `async_migrate_entry`, before any platform is set up;
     - move the device and every entity together;
     - be idempotent, so that a crash halfway is finished on the next start.
   - It needs a kill test on a clone (the CLAUDE.md procedure).

   *Multi-site:*
   - Today, two sites on one key are two entries with independent runs and budgets, so
     the shared 50/300 s rate limit is drawn on twice. An account entry could run its
     sites in turn under one lock and one budget. That is a real benefit.
   - **Merging existing per-site entries into one account entry is high risk.** It
     would move devices, entities, Stores, YAML-migration records and energy-setup
     records between config entries.
   - Recommendation: never merge. Each existing entry becomes an "account" with one site
     sub-entry, and further sites are added with "+ Site" where the key sees them.

   *Scope:* one manager per site sub-entry instead of per entry. That changes:
   - runtime data, the Store key, services (`config_entry_id` becomes a site target);
   - Repairs issue IDs, the energy fix flow, the YAML migration, diagnostics, buttons;
   - the options flow: usage mode, bill and allowance settings are per site, so they
     move to a site reconfigure flow;
   - the entry title, and every test helper (most of the 526 tests build a per-site
     entry).

   **Recommendation:** not in M17. It is neither low-risk nor contained. Make it its own
   milestone, with a kill-tested migration on a clone, **before** M18's site-wide
   settings. Otherwise those settings land in entry options and have to migrate again.

   M17 avoids making it harder:
   - mappings record `site_id`;
   - mapping devices already live in their own sub-entries;
   - site entity IDs are pinned explicitly, and site entities link to their device by
     identifier only;
   - the site name lives on the device (`name_by_user`) or in data, not in options;
   - chain cleanup keys on sub-entry existence.

2. **Site name:**
   - Asked once in the config flow, as revised. There is no options field: renames,
     areas and labels go through HA's device page (documented).
   - A blank name is refused at setup.
   - Installs from before 2.2 keep "Amber Energy Dashboard (site)".
   - With several sites, a default title follows a device-page rename (a device-registry
     listener). A title the user set is kept.
3. **A bug found and fixed during development:** Home Assistant runs a config entry's
   update listener eagerly.
   - Removing a sub-entry reloaded the entry before the next line ran. The intermediate
     setup saw neither the old nor the new mapping, and dropped the moved chain state as
     "removed mapping".
   - Fixes:
     - setup now forgets a chain only when its sub-entry no longer exists;
     - the conversion suppresses per-change reloads (`converting`) and reloads once at
       the end. That also lets the new reconciliation entities take over the old entity
       IDs (otherwise they would get `_2`).
   - Removing a mapping in the UI behaves as before.
4. **Conversion is all-or-nothing on the Store:**
   - the chain move is read back before any sub-entry changes;
   - on a mismatch nothing else changes, and the flow aborts with the reason;
   - after the sub-entry changes, both sets are read back.
5. **Entity IDs kept on conversion:** a grid meter's reconciliation sensors ask for the
   replaced sensors' entity IDs (`entity_ids` in its data). This goes beyond the brief,
   so dashboards keep working.
6. **A lone 2.1 feed-in mapping** becomes an appliance on feed-in, with "Yesterday
   compensation", per "any other single mapping becomes an appliance". The appliance add
   flow itself offers only general and controlled load.
7. **"Complete billed days"** is interpreted as imported days with no estimated interval,
   in the 14 NEM days ending at the last imported day.
   - Below 24 usable hours there is no value.
   - The attributes count only the days that gave hours.
   - The offset reads Amber's hourly energy statistics, so it needs Full or Recovery
     mode. In Pricing-only mode it is unknown, as reconciliation is.
8. **Grid meter export** is always priced on the site's feed-in channel. The field is
   shown only when the site has one, and the import channel choice only with a
   controlled load.
9. **Statistic display names** for new mappings are "<name> import cost (Amber prices)"
   and similar. Converted 2.1 statistics are renamed at their next write. IDs never
   change.
10. **Unique IDs** are prefixed (`grid_meter:…`, `appliance:…`). HA enforces sub-entry
    unique IDs across all types.
11. **Diagnostics** also redact `site_name` and mapping `name`s, since a user may type an
    address.
12. **Manifest `2.2.0-dev1`** (as 2.1.0-dev1 was). The README banner still says 2.1.1,
    the released version.
13. **Proposed design change:** none beyond point 1.
14. **Privacy:** the pushed M15 report (on `main`, tag `v2.1.1`) contained the test
    account's full NMI in an old entry title. It is redacted on this branch (`4f4bc68`),
    but it **remains in published history**. The maintainer: decide whether that matters (it is
    the test account's). No other NMI-like values are in tracked files.
15. **Screenshots needed** (placeholders in INSTALL.md):
    - `site-name-step.png`;
    - `integration-layout.png` (now showing a grid meter and an appliance);
    - `add-grid-meter.png`;
    - `add-appliance.png`.

## 6. Live Amber API calls

About **11**, all from 9102 with the test key, from 14:01 to 14:12 Sydney (outside
07:17 to 07:27). The suite makes none.
- `/usage`: 4. Two Run now presses (14:03 and 14:09), 2 calls each, as the run summaries
  report.
- `/sites`: about 7, one per setup. That is the boot (14:01), three restarts (14:05,
  14:08, 14:11), the conversion's reload, and the reloads after adding the appliance and
  the grid meter. This is estimated from the setups, not counted.
- A config-flow check of the site-name step on 9102 was not done: the site is already
  configured, so the flow aborts before that step. Tests cover it.

## 7. Lab state left behind

- **9102 `amber-test-m3`: stopped.** It runs this branch (`733e192` code), with:
  - grid meters "Whole house meter" (converted) and "Inverter meter";
  - appliance "Pool heat pump";
  - five template stand-in sensors, with imported history for 10-15 to 10-21.
- New snapshots: `pre-m17` (before) and `m17-lab` (after). The earlier ones are kept.
- No other clones. VM 101 was not touched. Nothing is in `/config/.claude_work`. The
  copied `__pycache__` was removed from the clone.

## 8. Recommended next steps

1. Review the branch, especially:
   - the restructure recommendation (point 1);
   - the conversion's entity-ID takeover (point 5);
   - the feed-in appliance (point 6).
2. Decide the order: the account/site restructure as its own milestone before M18's
   site-wide sources (recommended), or M18 first.
3. Supply the four screenshots.
4. Production, after a pre-release install: convert the two 2.1 mappings with the
   Repairs item, then check the grid meter's Calibration offset against the expected
   ~14 W after a day.
5. Decide on the published M15 NMI (point 14).
