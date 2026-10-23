# Milestone 18 report: 2.2.0-dev2 (account entry with site sub-entries, evening retries)

Date: 2026-10-23 (Sydney). Branch `feature/v2.2`, after M17 (`6c4a22c`). Pushed; nothing
is merged or tagged. **Stopped for review.**

**Summary.**
- **The config entry is now the Amber account** (its API key). Each site is a **Site**
  sub-entry that owns the site's device and entities. Grid meters and appliances stay
  sub-entries linked to their site.
- **Existing installs migrate automatically.** The entry ID, entity IDs, unique IDs and
  statistic IDs are all unchanged.
- **Kill test (9102):** HA core was killed straight after the migration, before the
  registries were saved. The next start finished the move: no entity ID changed, nothing
  was unavailable, and the statistics were byte-identical.
- **Site settings** moved from the entry's options to the site: the cog (asking which
  site when there are several) and the pencil on the site show the same menu. "Refresh
  channels" replaces the entry's Reconfigure. "+ Site" adds another site of the account.
- **Shared runs:** an account's sites take turns on one client, one run lock and one
  rate-limit budget.
- **Evening retries:** a day still waiting for Amber's data after the last daytime
  attempt is retried every 3 hours until 22:00, and the retries stop once it imports.
- **Tests:** 545 pass, 100 % coverage, ruff clean (section 2).
- **Privacy (M17 point 14):** done separately. Identifying data was removed from the
  whole history (rewritten and force-pushed); this milestone followed the privacy rule,
  and every commit was scanned before pushing.

## 1. What was built

| Commit | What |
|---|---|
| `663593a` | Account entry with site sub-entries, and the evening retries (code) |
| `b0b4bc1` | Tests (`tests/test_sites.py`, schedule tests, existing tests on the new layout); manifest `2.2.0-dev2` |
| `ec119a7` | Docs: INSTALL.md, README, DESIGN section 23 and section 10, CHANGELOG |
| (this) | This report |

**New module `sites.py`:**
- the site key, `<entry ID>_<site ID>`, used for the Store and Repairs issue IDs;
- `site_subentries` and `mapping_site_id`;
- `async_migrate_to_sites`, with a read-back (`_check_migrated`);
- `async_ensure_site_ownership`, which runs at **every** setup (section 3).

**Changed modules:**
- `__init__.py`:
  - runtime classes: `SiteRuntime` per site; `AmberRuntimeData` per account holds the
    client, the lock and the sites, with first-site shortcuts so single-site code keeps
    working;
  - per-site setup (`_async_setup_site`);
  - `_resolve_site` for services;
  - a per-site update listener;
  - migration 1.x → 2.1, and Store removal per site.
- `config_flow.py`:
  - entry version 2.1; the entry is created with its first site sub-entry;
  - the shared settings mixin `_SiteSettings` (with the new "Refresh channels" step),
    used by `AmberOptionsFlow` (site picker) and `SiteSubentryFlow` ("+ Site" and
    reconfigure);
  - a site step in the grid meter and appliance flows;
  - reauth checks every site.
- `storage.py`:
  - a per-site Store key;
  - `async_move_store` (copy, read back, remove; idempotent).
- `manager.py`:
  - the site key and label for Repairs issues;
  - evening retries (`_evening`, `_reschedule`, the final-attempt rule).
- `schedule.py`: evening slots, `is_final_attempt` (the last daytime attempt or the last
  evening retry), `evening_retry_after`.
- `api.py`: a run starts from the account's last rate-limit state.
- Per site: `sensor.py`, `button.py`, `diagnostics.py`, `repairs.py`, `energy.py`,
  `migration.py`, `meters.py`.
- `services.yaml` and the strings: a `site` field (the site's device) on every service.

## 2. Test results

- **Full suite, 9102 stopped:** `uv run pytest --cov=custom_components.amber_energy_dashboard`
  gives **545 passed**, 0 skipped, 0 failed, **100 % coverage** (5,178 statements).
  - 2 warnings, both the harness's `ResourceWarning: unclosed database` (an SQLite
    connection found by garbage collection). They land on different tests from run to
    run: here `test_meters` and `test_recovery`; earlier runs today, other tests. They
    come from the harness, not this code.
- `ruff check`, `ruff format --check` and `uv lock --check` are clean.
- **New tests** (`tests/test_sites.py`, 18; `test_schedule.py`, 1):
  - **Migration with existing state:** a 2.1-layout entry with its device and entities in
    the registries (one renamed entity ID, one site entity without a device), a Store at
    the old key, and an old-format issue.
  - **Crash recovery:** the device and entities left outside the site sub-entry are moved
    at the next setup; a second run is idempotent; a Store at both keys (the new one
    wins); a copy that doesn't read back; the read-back naming each problem.
  - **Two sites on one account:**
    - shared client, lock and rate limit (the second site's run is refused at the
      reserve the first left);
    - entity IDs;
    - service targeting by device, site ID or sub-entry ID;
    - removing a site (its Store goes), then the entry;
    - a site the key no longer sees (reauth).
  - **Flows:** "+ Site" (name required, no new sites, cannot connect, unsupported
    channels, a site configured in another entry); the cog with a site picker and the
    pencil; an account without sites; a grid meter's site step; Repairs finding their
    site.
  - **Evening retries:** schedule rules; a Run now import cancels them; none in
    Recovery-only mode. The existing "behind" test now walks 13:15 → 16:15 → 19:15.
  - **Rate-limit carry-over** (unit).
- **Existing tests:** most build 1.x entries, so they now run through the migration at
  every setup. Changed for the new layout:
  - Store and issue keys (`SITE_KEY`);
  - site settings instead of `entry.options` (`_site_options`);
  - "Refresh channels" instead of the entry Reconfigure;
  - diagnostics per site;
  - the "+ channels" menu item;
  - version 2.1, and 3 as the "newer version" case.

## 3. Lab verification (VM 9102, HA 2026.10.0)

**A. Kill test, from 2.2.0-dev1 with a converted grid meter and an appliance**
(snapshot `m17-lab`: grid meters "Whole house meter" and "Inverter meter", appliance "Pool
heat pump", 25 entities, 5 own-cost statistics of 168 rows each).
1. Recorded every entity (unique ID → entity ID, device, sub-entry), the devices, the
   sub-entries and the statistics.
2. Turned SSH add-on protection off (websocket `supervisor/api`), installed dev2
   (`git archive`; the sha256 of `sites.py` matched), and restarted HA.
3. Polled `/api/config` every 0.5 s. As soon as it said `RUNNING`, ran
   `sudo docker kill homeassistant`, then copied `core.config_entries`,
   `core.device_registry` and `core.entity_registry` as the kill left them.
4. **On disk at the kill:**
   - the entry was migrated (version 2.1, `data` = API key only, a `site` sub-entry);
   - the Store had moved to the site key;
   - but **the site device and all 17 site entities were still outside any sub-entry**:
     the registries hadn't been saved (HA saves them about 10 s after a change). This is
     the half-done state the design targets.
5. `ha core start`. **Result:**

   | Check | Result |
   |---|---|
   | Unique IDs (25) | the same |
   | Entity IDs changed | **none** |
   | Device links changed | none |
   | Sub-entries | site device and its 17 entities in the site sub-entry; the mappings unchanged (3 + 2 + 3) |
   | Unavailable entities | none of 25 |
   | Own-cost statistics (5 × 168 rows) | identical |
   | Integration log | no warnings or errors from the integration, nothing about sub-entries |

6. Run now via the old `config_entry_id` field (backward compatibility) imported 10-22
   (Amber had published it by then) with 1 usage call. All chains were caught up. The
   next run was tomorrow 07:15: no evening retry, since the day imported.
7. Protection turned back on (the SSH add-on can no longer reach Docker) and
   `.claude_work` removed. Snapshot `m18-kill-test`.

**B. Upgrade from 2.1.1** (snapshot `pre-m17`: 2.1.1 with two 2.1 own-sensor mappings,
19 entities). Installed dev2 and restarted:
- **Entities:** unique IDs the same, **no entity ID changed**, no device link changed.
  The 17 site entities moved into the site sub-entry; the two mappings' entities stayed
  in their own.
- **Store and Repairs:** the Store is now `amber_energy_dashboard.<entry ID>_<site ID>`.
  The conversion issue is raised under the site key.
- **Sub-entries:** listed as `site` ("Site …1234", placeholder for the test account's
  last 4 digits) and the two `own_sensor` mappings.
- **Conversion** through the Repairs fix flow (plan, confirm) gave one grid meter, whose
  device is in its sub-entry, linked via the site device.
- **"+ Site":** aborts with `no_new_sites` (the test key sees one site).
- **No device without a sub-entry**, so Home Assistant has no "Devices that don't belong
  to a sub-entry" group. Shown from the device registry; not seen in a browser.
- Snapshot `m18-upgrade-211`.

## 4. Open questions assigned to the milestone

- **Integration page groups (item 6):** every device belongs to a sub-entry
  (registry-verified), and the sub-entries are of types Site, Grid meter, Appliance and
  2.1 Own energy sensor. The frontend renders sub-entries by type with their `entry_type`
  label (frontend code read in M17). Not checked visually; the screenshots will show it.

## 5. Deviations, decisions and points for the planning chat

1. **The device and entity move runs at every setup**, not only in
   `async_migrate_entry`. The kill test reproduced exactly the case this covers: the
   config entries are saved within about 1 s, the registries about 10 s later.
2. **Migrated site sub-entries get a fixed ID** (`site_<site id>`), so a crash between
   steps finds the same sub-entry. New sites get HA's usual IDs.
3. **Keys:**
   - Store and Repairs issue IDs use `<entry ID>_<site ID>`; old per-entry issues are
     removed at migration, and the checks raise again any that still apply. A user's
     "ignore" on an old issue is not carried over.
   - The entry's first site keeps the entry's schedule offset, so existing installs keep
     their attempt times; a further site gets its own.
4. **Two ways into the site settings:** the cog (entry options; asks which site when
   there are several) and the pencil on the site (sub-entry reconfigure). Both run the
   same steps. Entry-level options are empty; there is no account-wide setting yet.
5. **Entity IDs of a further site** follow its device name
   (`sensor.beach_house_import_status`). The first site keeps
   `sensor.amber_energy_dashboard_…`.
6. **"+ Site" adds a site with the automatic schedule** and doesn't offer "Add to the
   Energy dashboard" (an account's first site already has it). The `energy_not_used`
   Repairs fix still adds a site's grid sources.
7. **Rate budget:** a run starts from the account's last-seen `RateLimit-Remaining` and
   window end, so the reserve (15) applies across sites. The per-run call cap (25 per
   counter) is still per run, so per site.
8. **A configured site the key no longer sees fails the whole entry (reauth)**, as a
   missing site did before. With several sites, a closed site would block its siblings.
   Proposal: per-site handling (skip that site, with a Repairs issue) in a later
   milestone.
9. **Downgrade:** entry version 2.1 is refused by 2.2.0-dev1 and earlier (they accept
   only 1.x). Going back needs a Home Assistant backup, which the release notes should
   say.
10. **Site sub-entry titles:** a migrated site without a name is titled "Site …1234". The
    device can be renamed on its page, but HA has no UI to rename a sub-entry. Proposal:
    a "Rename site" step in the site settings (title and device name) in M19.
11. **Service `site`** accepts the site's device, Amber site ID or sub-entry ID (the UI
    offers a device picker).
12. **Evening retries:**
    - "Final" now means the last daytime attempt, unless it ends still waiting with an
      evening retry to come; otherwise the last evening retry. The "behind" and
      `incomplete_data` issues follow this.
    - None in Recovery-only mode.
    - A manual Run now that imports cancels the remaining retries.
13. **Diagnostics:** the per-site data is now under `sites`, by sub-entry. The site's
    name, NMI and titles are redacted.
14. **Proposed design change:** points 8 and 10 for M19.

## 6. Live Amber API calls

About **10**, all from 9102 with the test key, 19:03 to 19:12 Sydney (outside 07:17 to
07:27). The suite makes none.
- `/usage`: 1 (Run now, 19:06).
- `/sites`: about 9, one per setup or flow:
  - setups: the boot (19:03), the dev2 start that was killed (19:05), the start after the
    kill (19:06), the `pre-m17` boot (19:09), the dev2 restart (19:10), the conversion's
    reload;
  - flows: "+ Site" (19:11).

  This is estimated from the setups, not counted.

## 7. Lab state left behind

- **9102 `amber-test-m3`: stopped.** It runs dev2 (code at `663593a`) in the state of
  snapshot B: an upgraded 2.1.1 install with one converted grid meter.
- **New snapshots:**
  - `m18-kill-test`: the dev1 lab state after the kill test, on dev2;
  - `m18-upgrade-211`: snapshot B.

  The earlier ones are kept.
- SSH add-on protection is on. `/config/.claude_work` was removed. No other clones. VM
  101 was not touched.

## 8. Recommended next steps

1. Review, especially:
   - points 1 and 3 (keys; ignored issues not carried over);
   - point 8 (a closed site blocks the account);
   - point 9 (no downgrade without a backup).
2. Screenshots can now be taken, since the layout is final for 2.2:
   - existing placeholders: `site-name-step`, `integration-layout`, `add-grid-meter`,
     `add-appliance`;
   - new: `add-site`.
3. M19: the site-wide sources (DESIGN section 22), plus the proposals in points 8 and 10.
4. Production, when a pre-release is wanted:
   - take a Home Assistant backup first (point 9);
   - after the update, confirm that no entity ID changed and that the site appears under
     the entry.
