# Milestone 9 report: v2.1 usability (2.1.0-dev1)

Date: 2026-10-08. Branch `feature/v2.1`, from `main` at `v2.0.1` (`b512d13`), pushed. Not
merged, no tag.

**Summary.**
- **Part 1:** the v2.1 design is recorded in DESIGN.md: section 18 (M9), section 19
  (M10, bill estimate) and section 20 (M11, export allowance), with the milestones list
  updated.
- **Part 2:** M9 is implemented:
  - (a) the device buttons;
  - (b) Energy dashboard setup, its Repairs issue and fix flow;
  - (c) the migration acknowledgement;
  - (d) the documentation.
- **436 tests pass, with 100 % coverage** (3696 statements). ruff check and format are
  clean. **CI on `9ef0ff3`: Tests, Validate with hassfest and HACS validation all
  succeeded.**
- **Lab:** everything new was checked on a disposable clone (HA 2026.9.3) with the real
  account. That check also confirmed a bug the buttons exposed, now fixed (section 5,
  item 1).
- **One discrepancy in the M10 brief:** without the allowance adjustment, the estimate
  comes out about **$3.25 higher**, not lower (section 5, item 9).

## 1. What was built

| Commit | What |
|---|---|
| `eb8e835` | DESIGN sections 18–20 (v2.1), milestones 7–11 |
| `52fd507` | Buttons: Run now and Re-check retention |
| `7743fdf` | Energy dashboard setup, its Repairs issue and fix flow, and the migration acknowledgement |
| `03bb151` | 2.1.0-dev1: README, INSTALL.md, MIGRATION.md, CHANGELOG, DESIGN cross-references |
| `9ef0ff3` | Own-sensor mappings get their own device (fixes an entity dropped at startup) |
| (this commit) | This report |

### (a) Buttons (`button.py`, `devices.py`)

- **Run now** (`button.amber_energy_dashboard_run_now`, no category, so it shows under
  Controls) calls `manager.async_run("button")`, the same run as the `run_now` service.
- **Re-check retention** (`button.amber_energy_dashboard_re_check_retention`, a
  diagnostic entity) calls `async_probe_retention()`:
  - the service and the button now share this function in `__init__.py`;
  - a failure raises `HomeAssistantError`, which the UI shows, and the stored boundary is
    kept.
- Names come from `entity.button` translations, and icons from `icons.json`
  (`mdi:play-circle-outline`, `mdi:calendar-search`).

### (b) Energy dashboard setup (`energy.py`, `repairs.py`, `config_flow.py`, `storage.py`)

**Shared energy-preference code (`energy.py`).** `validate_sources` (Home Assistant's
`ENERGY_SOURCE_SCHEMA`) and `async_save_sources` (save, read back the changed sources,
restore on a mismatch) are moved out of the migration. The migration now uses them, with
unchanged behaviour (its tests pass unchanged, apart from the patch target).

**Grid sources (`grid_sources`).**
- One grid source holds the general channel's energy and cost, plus the feed-in's energy
  and compensation. Price fields are empty and `cost_adjustment_day` is 0.
- A controlled-load channel gets a second grid source (energy and cost).
- A site without a general channel puts the feed-in on a source with no import meter.

**Adding them (`async_add_to_energy`).**
- Never when any grid source exists ("grid_exists"). Otherwise:
  1. the full preferences are saved in the Store (`energy_setup.backup`) and read back
     from disk;
  2. the new sources are validated, saved, and read back;
  3. on a mismatch, the sources from before are restored.
- The outcome is always recorded in the Store: when, who asked ("setup" or "repair"),
  what was added, and the reason if nothing was.

**Config flow.**
- The schedule step (the last one) shows **Add to the Energy dashboard**, suggested on,
  only when the preferences have no grid source.
- The choice is stored in the entry's data (`add_to_energy`). It is carried out once, at
  the entry's first setup; the Store record stops it repeating.
- If the preferences can't be read, the option isn't offered.

**Repairs issue `energy_not_used`** (warning, fixable).
- `async_check_energy_issue` raises it 24 h after the first successful import, when no
  energy preference (any source or device) references any of the entry's statistics.
  - The Store gets `first_import_at`. An install that already had data gets the time of
    its first check after the upgrade.
- It is checked:
  - when Home Assistant has started;
  - after every run (`manager.after_run`, new);
  - after each scheduled attempt (alongside the cleanup re-check);
  - on every energy preference change, through one energy-manager listener per Home
    Assistant run (the manager has no unsubscribe).
- It is cleared as soon as the statistics are used. It is never raised in Pricing-only
  mode, nor after a dismissal.

**Fix flow (`repairs.py`).**
- **No grid source:** shows the sources to add; submitting adds them and resolves the
  issue.
- **A YAML kit is detected** (`migration.async_detect` finds a candidate): explains the
  migration and where to find it. Submitting aborts with `use_migration`, and the issue
  stays until the dashboard uses the statistics.
- **Otherwise:** explains which statistics to choose. Submitting dismisses the issue for
  that entry, stored as `energy_issue_dismissed`.
- Aborts: `not_loaded`, `energy_unavailable`, `add_failed` (with the reason).

### (c) Migration acknowledgement (`migration.py`, `config_flow.py`, `__init__.py`, `services.yaml`)

**When parity is unverified.** `_parity` sets `unverified` when there are fewer than 3
compared days, no difference, and at least one legacy gap (or no overlap days at all).
- `unverified_reason` is either:
  - "The YAML kit's last data is D, before the integration's first day (B), so the old
    history can't be checked against Amber's data", or
  - "Only N days have data in both the YAML kit and the integration (at least 3 are
    needed), so …".
- `no_data` names the days neither source covers. Its text: "Neither covers X to Y (n
  days); the Energy dashboard will show no data for them."
- `_last_legacy_data` finds the last day the legacy series moved, so the rows a stalled
  kit keeps writing don't count as data.

**Running it.**
- The real-run checks moved into `_check_ready`.
- An unverified parity refuses without the acknowledgement, with a message naming
  `acknowledge_unverified`.
- A failed parity that is not unverified still refuses as before.
- The choice is stored in the run's sources, so a resumed run keeps it.

**Options flow and service.**
- The confirm step adds the checkbox "I understand the old history can't be checked
  against Amber's data" when parity is unverified; error `not_acknowledged`.
- Service field `acknowledge_unverified`.
- The dry run shows "Unverified: … A real run needs your acknowledgement; everything else
  is checked as usual."

### Own-sensor devices (`devices.py`, `sensor.py`, `__init__.py`): a bug the buttons exposed

**Cause.** Home Assistant 2026.9 gives a device exactly one config sub-entry. The
reconciliation sensor (in an own-sensor sub-entry) shared the site's device. HA had
already warned about this since M5: "assigns an existing device to a different config
subentry … This will stop working in Home Assistant 2027.8.0". With the buttons, platform
setup order decides which sub-entry the device ends in, and Home Assistant removes the
other side's entities.
- **In CI:** `test_reconciliation_name_falls_back_to_entity_id` failed (locally it
  failed 2 times in 26 runs).
- **On the lab clone, with the pre-fix build:** adding a mapping removed the **Run now
  button** from the entity registry.

**Fix.**
- Each mapping now has its own device, "<name> (own sensor)", model "Own energy sensor".
  It is linked by `via_device_id`, because `DeviceInfo(via_device=…)` is deprecated in
  this HA, and the lab logged that too.
- The site's device is created before the platforms, so its ID is known.
- The entity is named "Reconciliation", and entity IDs are unchanged.
- **Regression test:** `test_own_sensor_entities_have_their_own_device`. It fails 3 of 3
  times on the old `sensor.py` and passes on the new one.

### (d) Documentation

- **README:**
  - the Energy dashboard paragraph: the setup option;
  - a Buttons line;
  - a Repairs table row for the new issue;
  - the migration's unverified history and acknowledgement;
  - the FAQ "Why does an hour of yesterday's usage appear after midnight?";
  - troubleshooting for HACS's "icon not available";
  - the reconciliation naming.
- **INSTALL.md:**
  - section 2's version step is now a footnote for pre-releases;
  - section 3 step 5 covers the new option;
  - section 4 covers the two buttons;
  - section 6 covers the option and the Repairs item.
- **MIGRATION.md:** a paragraph on kits that stopped before the integration's first day,
  and the acknowledgement.
- **CHANGELOG:** `[Unreleased] - 2.1.0-dev1` (Added, Changed, Documentation).
- **DESIGN:** sections 11, 12 and 14 cross-reference section 18.
- `manifest.json` is `2.1.0-dev1`.

## 2. Test results

`uv run pytest --cov=custom_components.amber_energy_dashboard`: **436 passed**, 0 skipped,
0 failed (363 s). Coverage **100 %** (3696 statements). That is 401 at v2.0.1, plus 35:

| File | New | What |
|---|---:|---|
| `test_buttons.py` | 5 | entities, unique IDs, categories, the device, friendly names; Run now catches up (trigger `button`); Re-check retention re-discovers (previous boundary kept in the record); failure (503 and 403) raises and keeps the boundary |
| `test_energy_setup.py` | 20 | added once at setup (read back, never repeated after a reload); an existing grid source is never changed; controlled load as a second source (schema-validated); sites without a general channel; failures × 4 (read-back restore, backup read-back, schema, unavailable); issue timing (not at 23:59, raised at 24:00, cleared by a device preference that uses net cost); upgrade path and scheduled attempt; not raised × 3 (pricing, dismissed, unavailable); one listener per run; fix flow: add, add failure, migration, other plus dismissal, aborts × 2 |
| `test_config_flow.py` | +4 | offered when there is no grid source (default on, or turned off); not offered with a grid source, or when the preferences are unreadable |
| `test_migration.py` | +5 | stopped before the first day (unverified, exact reason and no-data days, refused without the acknowledgement, runs with it, carry row and continuity); stalled with 0-kWh rows plus the options-flow checkbox; few days without a gap still refuses; a difference with few days refuses even with the acknowledgement; one compared day before a stop is unverified |
| `test_own.py` | +1 | the own-sensor device (sub-entries, via device, no HA warnings) |

**Changed tests:**
- `test_full_flow`: the entry data now includes `add_to_energy: True`.
- `test_energy_fallbacks`: patches `energy.ENERGY_SOURCE_SCHEMA`.
- `test_too_few_overlap_days`: now also asserts the unverified state.
- Three reconciliation name assertions: the new device and entity name.
- `_setup_entry` in `test_manager.py` gained a `data` argument.

## 3. Lab verification

**Clone:** VM 9110 `amber-test-m9`, a linked clone of the template; HA 2026.9.3. Before
creating it: `local-lvm` was 91.6 % free, with 1 other clone (9102). It was destroyed at
the end.

**Install:** the branch build, copied over SSH; sha256 of all files matched. Then a restart
and `RUNNING`.

| Check | Evidence |
|---|---|
| Starting point | `energy/get_prefs` → `not_found: No prefs` (no Energy dashboard preferences at all) |
| Config flow (REST) | Steps user, site, channels, schedule. **The schedule step offered `add_to_energy`, suggested `True`.** Created with fixed times and the option on; entry `loaded` |
| First import | `first_setup`, `caught_up`, **87 days, 20 calls** |
| Energy preferences | One grid source: `…_e9_energy`, `…_e9_cost`, `…_b9_energy`, `…_b9_compensation`, prices null, adjustment 0.0. **`energy/validate` reports no issues** for it (the statistics exist after the import) |
| Store on disk | `energy_setup`: `requested_by` setup, `added` [e9], `reason` null, `backup` null (there were no preferences). `first_import_at` set at the end of the first run |
| Buttons | Registry: Run now with category none, Re-check retention with category `diagnostic`; the translation keys are right. **Run now** pressed → `caught_up`, trigger `button`, **0 calls**. **Re-check retention** pressed → `bisection`, **7 calls**, boundary 2026-07-13 (previous 2026-07-13), `last_verified` 2026-10-08 |
| Repairs issue | Grid source removed (no issue: < 24 h). `first_import_at` moved back 2 days in the Store file, then the entry reloaded → **`energy_not_used_<entry>` raised**, fixable, warning |
| Fix flow: add (REST `/api/repairs/issues/fix`) | Step `add`, listing the 4 statistics → submit → `create_entry`; 1 grid source using e9; issue gone; `energy/validate` clean; Store: `requested_by` repair, `backup.energy_sources` `[]` |
| Listener and fix flow: other | Saving a grid source with `sensor.some_other_meter` → **the issue was re-raised immediately** (listener). The fix flow went to step `other` → submit → `create_entry`; the preferences were unchanged; Store `energy_issue_dismissed: true`; after a reload, still no issue |
| Device bug, pre-fix build | Added an own-sensor mapping (a stand-in state with an energy device class). HA logged "assigns an existing device to a different config subentry", **and `button.…_run_now` was gone from the entity registry** |
| Device fix, after upgrade | Both buttons are on "Amber Energy Dashboard" (sub-entry none). `sensor.amber_energy_dashboard_reconciliation_fake_house_meter` kept its entity ID, now on "Fake house meter (own sensor)" (its sub-entry), linked to the site's device. The sub-entry warning appeared once during this first start (the old device moving back). **After the next restart: no warnings.** The `via_device_id` build: **no integration warnings or errors** in `system_log` |

**Not checked in the lab:**
- the fix flow's migration branch (it needs legacy statistics);
- the migration acknowledgement (it needs a stopped YAML kit).

Both are covered by the tests above, which use real recorder statistics.

## 4. Open questions assigned

None. No section 4 API verifications were assigned to M9.

## 5. Deviations and proposed design changes

1. **Own-sensor devices (a bug fix beyond the brief).** Each mapping now has its own
   device, and the reconciliation sensor is named "Reconciliation". The friendly name
   changes from "Amber Energy Dashboard Reconciliation: X" to "X (own sensor)
   Reconciliation"; entity IDs don't change. DESIGN section 11 is updated. Without this,
   the buttons make Home Assistant drop the reconciliation sensor or the Run now button,
   depending on startup order. On the first start after the upgrade, Home Assistant moves
   the old shared device once; I saw no data loss on the clone.
2. **Controlled load is a second grid source.** Home Assistant's grid source has one
   import meter, so "one grid source … (controlled load channel too)" can't be literal.
3. **The setup option is carried out at the first setup, not inside the config flow**
   (stored in the entry's data). That way the saved copy of the preferences lives in the
   entry's Store, as in the migration. The option is not offered when the preferences
   can't be read.
4. **When the issue is checked:** after every run, every scheduled attempt, at start, and
   on preference changes. An upgraded install's 24 h start at its first check.
5. **"Dismiss" is stored** (`energy_issue_dismissed`), so the issue never returns for
   that entry, even if the grid source is later removed. The migration branch does not
   dismiss: the issue stays until the migration switches the dashboard.
6. **The acknowledgement condition is slightly broader than the brief.** It also covers 1
   or 2 compared days with no difference, when a gap exists (a kit that stopped shortly
   after our first day; `test_too_few_overlap_days` is such a case). Fewer than 3 days
   with no gap still refuses (waiting fixes it), and any difference refuses. If you want
   strictly "no compared day", it is a one-line change.
7. **README FAQ wording:** "totals over a billing period don't [shift], because the bill
   uses the same NEM days", instead of "monthly totals don't". The Energy dashboard's
   monthly view groups by local time, so the boundary hour of a month does move in
   summer. A South Australia half-hour note was added.
8. **INSTALL.md:**
   - screenshot 07 (version picker) is no longer referenced; the file is still in the
     repository;
   - the FAQ entry "HACS shows an update with a long code" describes the pre-release era
     and was left unchanged. I propose removing it.
9. **M10 acceptance (DESIGN section 19 records this):**
   - with M11's adjustment: 108.5424 − 5.3749 + 62.5462 = **$165.71** (bill $165.74
     excluding the card fee);
   - without it: 108.5424 − 2.1238 + 62.5462 = **$168.96**, so **$3.25 higher**, not
     lower. Less export credit makes the bill higher.
10. **Store:** three new keys (`first_import_at`, `energy_setup`,
    `energy_issue_dismissed`), filled with defaults on load. There is no store version
    bump, as with the `migration` key.

**Screenshots to retake** (Robert to supply):

| Image | Why |
|---|---|
| `08-add-integration.png` | old brand icon |
| `12-import-schedule.png` | the new "Add to the Energy dashboard" option (when there is no grid connection) |
| `13-name-and-assign.png` | old brand icon |
| `14-device-page.png` | old brand icon; now shows the **Controls** section with Run now, and Re-check retention under Diagnostic |
| `15-integration-page.png` | old brand icon |
| `16-options-menu.png` | old brand icon |
| *(new, optional)* | the Repairs item and its fix-flow "add" step, for INSTALL.md section 6 |
| `07-hacs-choose-version.png` | no longer used; delete it, or keep it for the footnote |

## 6. Live Amber API calls

**36**, on 2026-10-08 from 08:25 to 08:47 AEDT, all on the `/sites`+`/usage` counter.
This was outside the 07:17–07:27 quiet window, and well before production's 10:22 run.
- 2 `/sites` in the config flow (the first attempt stopped on a script error at the site
  step; there was no retry storm);
- 7 `/sites` at entry setups (creation, 2 reloads, 1 reload after adding the mapping,
  3 restarts);
- 20 in the first import (retention discovery plus usage windows);
- 0 for Run now;
- 7 for Re-check retention.

## 7. Lab state left behind

- **VM 9110 was destroyed** (stop and delete, both task `OK`). The pool now holds the
  template 9000 (stopped) and **VM 9102 `amber-test-m3`** (running, untouched by this
  milestone).
- VM 101 was not accessed. There are no files on any HA instance.
- Repository: `feature/v2.1` is pushed (this report is its last commit). `main` and `v2`
  are unchanged at `v2.0.1`.

## 8. Recommended next steps

1. Review M9, especially section 5 items 1, 5 and 6, and the M10 sign correction (item 9).
2. Supply the screenshots listed above.
3. For M10: upgrade VM 9102 (which holds this account's history) to the v2.1 branch in
   place, with a snapshot, before the bill-estimate acceptance test.
4. Decide on removing the stale INSTALL.md FAQ entry and screenshot 07.
