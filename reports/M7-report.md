# Milestone 7 report: v2.0.0 preparation

Date: 2026-10-03. Branch `v2`, pushed. No merge to `main` and no tag.

**Summary.** All five items are done:
- the installation and migration guides are in `docs/`;
- the `previous_boundary` fix (M6c note A.5.1) is in;
- the cleanup repair now re-checks itself and clears;
- the manifest is `2.0.0`, and the CHANGELOG entry is finished;
- the release notes are written.

**389 tests pass, with 100 % coverage** (3327 statements). ruff is clean, and local
hassfest reports 0 invalid. **CI on `d557c76`: hassfest, HACS validation and Tests all
succeeded.** No live Amber calls were made.

## 1. What was built

| Commit | What |
|---|---|
| `d4f1eba` | Retention discovery keeps `previous_boundary` in the stored record |
| `1166b3e` | The cleanup repair re-checks the YAML kit's leftovers and clears itself |
| `2f6f15c` | `docs/INSTALL.md`, `docs/MIGRATION.md` and `docs/images/` (20 PNGs); the README is trimmed to link them |
| `d557c76` | Version 2.0.0: manifest, CHANGELOG entry and `reports/release-notes-v2.0.0.md` |
| (this commit) | This report |

(`1ac9ceb`, the final brand icon, was pushed earlier today.)

### Item 1: documentation

- **Guides.** `INSTALL.md` and `MIGRATION.md` were copied unchanged from the zip, apart
  from one addition to MIGRATION.md (item 3 below). All 20 images went to `docs/images/`,
  byte-identical (`cmp`).
- **Link check:**
  - all 24 relative links in the two guides resolve: 20 images, `../README.md`, and the
    cross-links between the guides;
  - all 20 images are referenced;
  - all 8 in-page anchors of INSTALL.md's contents list match GitHub heading slugs.
- **README:**
  - The installation section is now **"Installation and setup"**. It opens with a
    prominent link to `docs/INSTALL.md`, then a one-paragraph summary and the manual
    install.
  - Removed as now covered in more detail by the guide:
    - the step-by-step HACS list;
    - the setup step list;
    - the outdated "Show beta versions" note;
    - the "around 20 API calls" figure. INSTALL.md says about 15; the README now gives
      no number.
  - The statistics table, Energy dashboard notes and services list stay, under **"What
    it creates"**. The Energy dashboard notes now link INSTALL.md section 6.
  - The migration section opens with a prominent link to `docs/MIGRATION.md`. The
    "Before you start" and step parts the guide covers are folded into one paragraph.
  - The migration reference detail the guide does not have stays:
    - the parity tolerances;
    - the implausible-row scan;
    - what the migration does;
    - the sign fix;
    - the v1 notes;
    - the old statistics and undo.
  - The banner says "Version 2.0.0", without "release candidate", and links the
    migration guide.
  - **Feed-in versus the bill:** a new Common questions entry, consistent with
    INSTALL.md's FAQ:
    - export charges applied per interval in Amber's data, versus a free export
      allowance applied on the bill (Endeavour N61, 8 kWh a day);
    - import costs match the bill to the cent.

    The supply-and-subscription entry was also added. Both link to the INSTALL.md FAQ.
  - The Repairs table row for "Finish removing the YAML kit" now describes the new
    behaviour.
- `~/amber-docs.zip` and `~/amber-docs/` are deleted.

### Item 2: `previous_boundary` after `probe_retention` (`manager.py`)

`_async_discover_retention` now reads the stored boundary before it runs. It passes that
boundary to `_store_retention(previous=...)` on both store paths (normal, and the
auth/budget fallback).
- After `probe_retention`, or a re-discovery after an unresolved check, the record keeps
  the earlier date. A move logs the existing warning ("Usage retention boundary moved
  from X to Y").
- A first discovery still records `None`.
- The service response is unchanged.

### Item 3: the cleanup repair (`migration.py`, `__init__.py`, `manager.py`)

- **Structured leftovers.** `Layout.cleanup` is now a tuple of `Leftover` items. Each
  item has a text and the things to check:
  - entity IDs: present while they have a state **or** an entity registry entry, so an
    orphaned "unavailable" entity still counts until it is removed in Settings >
    Entities, as MIGRATION.md says;
  - `domain.service` names (`rest_command.amber_fetch_usage`);
  - `secrets.yaml` keys (`amber_api_key`): only top-level key names are matched, and
    values are never kept or logged;
  - file names in the configuration folder (the v1 `amber_backfill.py` and
    `amber_backfill_cache.json`).
- **`async_check_cleanup(hass, manager)`** replaces the static `cleanup_issue`:
  - It rebuilds the list from the stored record's layout key and automations, so the
    existing rc1/rc2 records (production's included) work without a store change.
  - For a manually picked source, it checks the picked legacy sensors. External
    statistic IDs are skipped.
  - It lists only what exists. The package-file hint is added while any config-defined
    leftover or automation remains.
  - It updates the issue, or deletes it (logging once at info) when nothing remains.
  - It does nothing unless the migration is `completed`; undo still deletes the issue.
- **When it runs:**
  - at load, through `async_at_started`, so it waits for Home Assistant to start. At
    load time the kit's YAML entities may not be set up yet, and an immediate check
    could wrongly clear the issue. A test covers this;
  - after each scheduled attempt, through a new `manager.after_scheduled` hook. It also
    runs after a skipped, caught-up attempt, so the retries re-check without API calls;
  - at the end of a migration.
- **The old statistics** are no longer a reason to keep the issue open.
  - The migration's **result screen is unchanged**: it still lists everything, including
    the old-statistics line.
  - The issue text (`strings.json` and `en.json`, kept identical) says it re-checks and
    clears itself, and points to the migration guide for the old statistics.
  - MIGRATION.md gains a paragraph on the Repairs notice, a sentence that the old
    statistics don't keep it open, and the v1 backfill files. The README says the same
    in its migration section.
- **DESIGN.md section 14 ("Cleanup, cautious")** is updated to describe this. It
  previously said the issue is kept while the migration is completed.

### Items 4 and 5: version, CHANGELOG and release notes

- `manifest.json` is `2.0.0`.
- CHANGELOG `## [2.0.0] - 2026-10-03`:
  - one consolidated list of everything since 1.x, stated as final behaviour (for
    example, the scan caps per elapsed hour);
  - "Changed since 2.0.0-rc2" for upgraders;
  - "Replaced".

  The rc1 and rc2 entries stay below for reference. The link list now points to release
  tags.
- `reports/release-notes-v2.0.0.md`:
  - links to the guides at `blob/v2.0.0/...`, which resolve once the tag exists;
  - what you get, and what's new since rc2;
  - upgrading (rc, dev and v1), with the feed-in versus bill note;
  - known limitations, and a CHANGELOG link.

## 2. Test results

- `uv run pytest` at `d557c76`: **389 passed, 0 skipped, 0 failed** (287 s). Coverage
  of `custom_components/amber_energy_dashboard` is **100 %** (3327 statements).
- `ruff check .` and `ruff format --check .` are clean.
- New tests (10 more than rc2's 379):
  - `test_recovery.py`:
    - `test_probe_retention_record_keeps_previous_boundary`: the record and the saved
      store file both hold the earlier date, and the warning is logged. It failed before
      the fix ("assert None == '2026-09-06'").
    - `test_first_discovery_has_no_previous_boundary`.
  - `test_migration.py`:
    - `test_cleanup_issue_lists_what_exists_and_clears`: an advanced kit with an
      automation, a helper, a script, a `rest_command` service, a registry-only template
      sensor and a real `secrets.yaml` in a temp config folder.
      - The issue lists exactly those.
      - Removing some, then a caught-up scheduled attempt, narrows the list, with no
        Amber calls made.
      - With only the secret left, the package hint goes.
      - Once all are removed, the issue clears while the old statistics still exist, and
        it stays cleared after a reload.
    - `test_cleanup_issue_rechecked_after_a_scheduled_run`: an attempt that really runs
      (not caught up) also re-checks.
    - `test_cleanup_check_waits_until_started`: on a reload while not running, the issue
      is not cleared although the leftover is absent. After `EVENT_HOMEASSISTANT_STARTED`
      it is re-checked.
    - `test_cleanup_manual_pick_lists_its_sensors`.
    - `test_cleanup_v1_files_and_secret`: a missing `secrets.yaml`, a commented key, a
      prefixed key and a nested key do not count; a top-level key does, and so do files
      in the config folder.
    - `test_cleanup_check_ignores_records_not_completed`: no record, or an undone one.
  - Updated: `test_advanced_migration` (the issue lists only the existing automation;
    the result still lists everything) and `test_cleanup_issue_survives_a_restart` (it
    now needs a leftover to exist).

## 3. Lab verification

- **hassfest (local):** from HA core tag 2026.9.3, Python 3.14: `Integrations: 1, Invalid
  integrations: 0`, exit 0. This is the same method as before. Two C++-only voice
  packages (`pyspeex-noise`, `pymicro-vad`) were left out of the environment, since this
  container has no compiler.
- **CI on `d557c76`:**
  - Validate with hassfest: success (02:17:23Z);
  - HACS validation (brands check on): success (02:17:33Z);
  - Tests: success (02:22:32Z).
- **No lab run of the new repair behaviour.** VM 9102, the only clone, has no completed
  migration: its Repairs list is empty, and none of the kit's entities exist there. So it
  cannot exercise the repair. A real check needs a clone with a migrated YAML kit, which
  is a substantial setup with live calls. I did not start one, since this milestone was
  to stop at the report. Evidence is the test suite above, which uses the real entity,
  issue and service registries and real files.
- **Screenshot review** (all 20 images viewed before committing them publicly):
  - NMIs and site IDs are masked, and the Amber account name is blacked out.
  - **`02-amber-token-created.png` shows a complete API token** (`psk_` plus 32 hex
    digits). The guide's caption says it was deleted straight after the screenshot. I
    did not, and cannot, verify that without using it. It is now in the public
    repository's history. Please confirm it is revoked on Amber's developer page.
    GitHub secret scanning may also flag it.
  - Screenshots 08, 13, 14, 15 and 16 show the **old bars-and-bolt brand icon**, not the
    final design.

### Observation: VM 9102 was down from 2026-09-29 to today

A read-only check of 9102 (`2.0.0-rc2`, the real account) showed import status
`caught_up`, last imported date **2026-09-28** and **4 days behind**. The last run was the
scheduled 2026-09-29 07:15 run.
- **Cause: the VM was not running.** Proxmox reports 0.4 h of uptime (booted about 11:49
  today). HA's history has no `sun.sun` changes after 2026-09-29. Apart from a brief boot
  on 2026-09-30 at 11:43, which ended before its 13:15 slot, there are no states at all.
  None of those starts were made by my token.
- **Not a code fault.** The next fixed slot, 13:15 today, should catch up the 4 days in
  one window call. I did not trigger a run.
- **A cosmetic point for the planning chat:** after a restart the status sensor still
  shows the last run's `caught_up` while "Days behind" reads 4.

## 4. Open questions assigned to this milestone

- **M6c note A.5.1** (the record keeps `previous_boundary: null`): fixed (item 2).
- None of the section 4 API verifications were assigned to this milestone.

## 5. Deviations and proposed design changes

- **DESIGN.md section 14** is updated for the self-clearing repair, as requested. This is
  not a silent deviation.
- **Decisions for you to confirm:**
  1. **`amber_api_key` in `secrets.yaml` keeps the repair open.** The kit's text says to
     remove it "if nothing else uses it". If another tool still uses the key, the repair
     never clears; the user can still ignore it in Repairs. The alternative is to treat
     the secret as advisory, like the old statistics.
  2. **The v1 backfill files count only if they are in the configuration folder.** The
     v1 README ran the script from a computer, so they are normally not there.
     MIGRATION.md now tells v1 users to delete them wherever they ran it.
  3. **Orphaned registry entries count as present.** They clear when deleted in Settings
     > Entities, as the guide instructs.
  4. **`recorder: exclude` lines are not checked.** They are mentioned with the template
     sensors and are harmless once those sensors are gone.
  5. **A manual pick checks the picked legacy sensors.** If such a sensor is something the
     user keeps, the repair stays until they ignore it.
- **Docs to refresh after the release:**
  - INSTALL.md section 2 step 5 and the FAQ entry on HACS showing a commit code describe
    the pre-release situation. Both read correctly once 2.0.0 is published, but they
    could be shortened later.
  - Reshoot the five screenshots with the old icon, if you want them to match.
- **The CHANGELOG date is 2026-10-03.** Change it if the release is published later.

## 6. Live Amber API calls

**0.** The lab contact was read-only HA API and websocket calls to VM 9102, plus Proxmox
status and task reads.

## 7. Lab state left behind

- **VM 9102** (`amber-test-m3`): running, unchanged, still on `2.0.0-rc2`. Its only
  snapshot entry is `current`. No files were written to it.
- VM 101 was not accessed. No other clones exist.
- Local: the guide sources (`~/amber-docs.zip`, `~/amber-docs/`) and the scratch HA core
  checkout are deleted.
- Repository: `v2` is pushed to `d557c76`, plus this report's commit. No tag, and nothing
  merged to `main`.

## 8. Recommended next steps

1. Confirm that the token in `docs/images/02-amber-token-created.png` is revoked, or
   replace the image with a masked one before the release.
2. Review and decide the five points in section 5.
3. Optionally, a lab check of the cleanup repair on a clone with a migrated YAML kit,
   before or soon after the release.
4. Check that 9102's 13:15 run today caught up the 4 days.
5. Then merge `v2` to `main`, tag `v2.0.0`, and publish the GitHub release with
   `reports/release-notes-v2.0.0.md`. Its guide links point at the `v2.0.0` tag.

---

## Addendum: M7 review decisions (2026-10-03)

Branch `v2`, pushed. Version 2.0.0. No merge and no tag.

### A.1 What was built

| Commit | What |
|---|---|
| `c946c87` | Cleanup repair: the `amber_api_key` secret is advisory |
| `8de09a8` | Import status: "Waiting for next run" instead of "Up to date" while days are due |
| `6bf04d8` | CHANGELOG, release notes, README and DESIGN for both |
| (this commit) | This addendum |

**Decision 1: the secret is advisory** (`migration.py`).
- The two `amber_api_key` `Leftover` items no longer have anything to check, so the
  repair never lists them and never stays open for them.
- `secrets.yaml` is no longer read at all. The secrets parsing was removed, and
  `_on_disk` now checks only the v1 backfill files.
- The line is still on the migration's result screen. `Leftover.render()` returns the text
  unchanged when there are no names.
- MIGRATION.md now says the line isn't checked, because another tool may use it, and to
  remove it once nothing else does. The README and DESIGN section 14 say the same.
- Section 5 items 2 to 5 are unchanged, as accepted.

**Decision 2: the import status** (`manager.py`, `diagnostics.py`, translations).
- **The new state** `waiting_for_next_run` ("Waiting for next run") is added to
  `STATUSES`, so it is in the enum's options, `strings.json` and `en.json`.
- **When it shows:** `AmberManager.display_status` returns it when the last outcome is
  `caught_up` but `caught_up` (the mode-aware "nothing outstanding" check the scheduler
  uses) is false. Otherwise it returns the last outcome.
- **Where it shows:** the sensor snapshot uses it. The stored outcome (`last_run.status`)
  and the run results are unchanged. Diagnostics show both `status` and the new
  `display_status`.
- **Consistency:** the status and "Days behind" come from the same snapshot, so they
  always agree.
- **The midnight rollover:** the snapshot is refreshed at load, on runs and on each
  scheduled attempt, not at midnight. So after the NEM day rolls over, both sensors
  update together at the next attempt.
- **Recovery-only mode** (my interpretation; please confirm):
  - Usage is not imported on a schedule there, so the usage marker is not outstanding
    work, and the status stays "Up to date" while "Days behind" grows.
  - "Waiting for next run" would be wrong, because no run is coming.
  - Pricing-only works the same way for its own chains.
  - A test pins this behaviour.
- **DESIGN section 13** gains a bullet describing this, and the README status table gains
  the row.

**Decision 3:** the CHANGELOG 2.0.0 entry is updated in "Added" (sensors, migration) and in
"Changed since 2.0.0-rc2". The release notes' "What's new since rc2" covers both.

### A.2 Test results

- `uv run pytest`: **392 passed, 0 skipped, 0 failed** (287 s). Coverage is **100 %**
  (3321 statements). ruff is clean.
- New tests:
  - `test_status_after_restart_following_downtime`: a reload 4 days later. The sensor
    shows `waiting_for_next_run`, "Days behind" is 4, and the stored outcome is still
    `caught_up`. The next scheduled attempt catches up, giving `caught_up` and 0.
  - `test_status_up_to_date_only_when_nothing_is_outstanding`: the day rollover turns
    the status to "waiting for next run" with 1 day behind. A run whose outcome is
    `waiting_for_data` is shown as that.
  - `test_every_status_has_a_translation`: `strings.json` and `en.json` translate exactly
    the `STATUSES` list, including "Waiting for next run".
- Changed tests:
  - `test_cleanup_issue_lists_what_exists_and_clears`: the secret stays in
    `secrets.yaml` throughout. It is in the result but never in the issue, and the issue
    clears with it still present.
  - `test_cleanup_v1_files_and_advisory_secret`: replaces the secrets-parsing test.
  - `test_recovery_mode_has_no_schedule`: pins the Recovery-only display status.
  - The diagnostics test checks `display_status`.

### A.3 CI

CI on **`6bf04d8`**: all succeeded.
- Validate with hassfest: success (03:49:45Z). This covers the new translation key.
- HACS validation: success (03:50:09Z).
- Tests: success (03:54:00Z).

### A.4 VM 9102's 13:15 run (decision 4, read-only)

Read at 13:48 AEST through the HA REST API and the diagnostics download. Only selected
fields were printed. No Amber calls were made by me.

| Field | Value |
|---|---|
| trigger | `scheduled (final)` (the fixed 13:15 slot) |
| started / finished | 2026-10-03 03:15:00Z / 03:15:06Z (13:15:00 to 13:15:06 AEST) |
| status | **`caught_up`** |
| imported_days | **2026-09-29, 09-30, 10-01, 10-02** (all 4) |
| calls | `sites_usage` **9** |
| retention | `moved forward 7 days (bisected)`, 8 calls; boundary 2026-06-29 → **2026-07-06** (96 → 89 days) |
| revisions | checked `[]`, changed `[]`, 0 rewritten |
| marker / last_written | 2026-10-02 / 2026-10-02 |
| days behind | **0**; next run 2026-10-03 20:15Z = 2026-10-04 07:15 AEDT (daylight saving starts that night) |
| the 4 days in the store | all `imported`, `estimated_records` 0 |

**It caught up all 4 days.**
- The 9 calls were 8 for retention plus 1 window call for the 4 days.
- While the VM was down, Amber's rolling boundary moved forward 7 days. The daily check
  found that and bisected within its 8-call cap, as designed.
- The VM still runs `2.0.0-rc2`. In the 2 hours before this run it showed exactly the
  case decision 2 fixes: "Up to date" while 4 days behind.

### A.5 Live Amber API calls

**0 by me.** The integration's own 13:15 run on 9102 made 9.

### A.6 Lab state left behind

- **VM 9102** (`amber-test-m3`): running, unchanged, on `2.0.0-rc2`, with no snapshots.
  No files were written to it.
- VM 101 was not accessed. No other clones exist.
- Repository: `v2` is pushed. No tag, and nothing merged to `main`.

### A.7 Still open from the main report

- Confirm that the token in `docs/images/02-amber-token-created.png` is revoked (section
  3), or replace the image.
- Confirm the Recovery-only interpretation in A.1.
- Then merge, tag `v2.0.0`, and publish the release.
