# Milestone 15 report: 2.1.1 (own-sensor fixes, naming, retention guard)

Date: 2026-10-23 (Sydney). Branch `fix/own-sensor-devices`, 8 commits on `6c137d0`
(the 2.1.0 release). Nothing is merged, tagged or pushed. **Stopped for review before
merging and tagging.**

**Summary.**
- **Item 1 (E9 sensor not on its device):** production's registry was correct (entity →
  device `1803efa0…`, "Whole House Energy Consumed (own sensor)", 1 entity), and after a
  hard refresh the device page lists the sensor. This was treated as transient, so no
  re-link code was written. A creation-time test (general and feed-in) and an upgrade test
  (an entity with no device gets linked) cover it.
- **Item 2 (B9 had no sensor and no device):** 2.1.0 made reconciliation for general
  channels only (`sensor.py` and the manager both skipped other channel types), so no
  entity, and therefore no device, was created. Every mapping now gets a reconciliation
  sensor (own kWh − Amber's kWh on the mapped channel; billed export for feed-in) and its
  device.
- **Item 3 (naming):**
  - The site device is now "Amber Energy Dashboard (site)", and entity IDs are unchanged
    on existing and new installs.
  - The default entry title is "Amber Electric", or "Amber Electric (…1234)" with several
    sites. Titles the integration chose are migrated, and a user's title is kept.
  - INSTALL.md has a new page-layout section, with a screenshot placeholder.
- **Retention guard (the 9102 `AmberConnectionError` runs):** confirmed as a real bug,
  present since the M4 fallback. A failed check never moves a stored boundary now.
  Reproduced and verified live on 9102 under a simulated outage.
- **Tests:** 507 pass and ruff is clean. One coverage test was added afterwards (section 2).
- **9102:** the upgrade from 2.1.0 with E9 and B9 mappings passed, with no entity ID
  changed. The VM is stopped.

## 1. What was built

| Commit | What |
|---|---|
| `1cf8a11` | Reconcile feed-in own sensors too (`manager._reconciliation`, `sensor.py`: no channel-type filter). Tests: feed-in reconciliation against billed export, and "a fresh mapping is on its device at creation" for E1 and B1 |
| `9bb17a9` | Never move the retention boundary on a failed check (`manager.py`: `_keep_retention`, the `last_verified` argument of the retention setter, no daily check while `needs_discovery`, and `probe_retention` treats `unresolved (...)` like a fallback). `FakeAmber.error`, and the new `tests/test_retention_errors.py` |
| `b3cd6d9` | Default entry title without the NMI (`devices.entry_title`, `is_default_title`, `__init__.async_update_titles` at setup and removal, `config_flow`). A title-only entry update no longer reschedules the manager (`AmberRuntimeData.options`) |
| `343de1a` | Site device "Amber Energy Dashboard (site)" (`devices.SITE_DEVICE_NAME`). `devices.async_site_entity_ids` suggests the 2.1.0 entity ID for new site entities. Tests for a new install and for an upgrade from the 2.1.0 state |
| `c82518a` | Docs: the INSTALL.md page layout, README reconciliation, and DESIGN sections 8, 11 and 18 as built |
| `599457e` | 2.1.1: manifest, CHANGELOG (`[2.1.1] - Unreleased`), README banner |
| (this) | Coverage test for the start-date raise on a first failed discovery, and this report |

**How the IDs are kept.** In HA 2026.9 and later, a new entity's ID is built from its
device's name plus the entity's name, so renaming the device alone would give new
installs `sensor.amber_energy_dashboard_site_*`.
- Each site entity sets `entity_id` to `<platform>.slugify("Amber Energy Dashboard <English
  name>")` before it is added. HA treats that only as a suggestion for a new registry
  entry.
- Existing registry entries keep their IDs, including IDs the user renamed. Friendly
  names do follow the device ("Amber Energy Dashboard (site) Days behind").

**Re-linking.** No dedicated code was needed. At every setup, HA's entity platform sets a
registry entry's `device_id` from the entity's `device_info`. The upgrade test sets the
general mapping's `device_id` to none and checks that it is linked after setup.

## 2. Test results

- `uv run pytest`: **507 passed**, 0 skipped, 0 failed (7 min 2 s), before the coverage test below (508 with it).
- 14 new test functions (15 cases; the creation-time test runs for E1 and B1), across:
  - `test_retention_errors.py` (new file);
  - `test_own.py`: feed-in reconciliation; creation-time device ×2; upgrade from 2.1.0;
    new-install names and IDs;
  - `test_init.py`: 4 title tests;
  - `test_config_flow.py`: the second-site title;
  - `test_retention_heal.py`: the first discovery stopped on a young site.
- Changed expectations:
  - the title "Amber FAKENMI000" became "Amber Electric" (`test_config_flow`,
    `test_energy_setup`);
  - the button friendly names gained "(site)" (`test_buttons`);
  - `test_retention_heal::test_failed_discovery_on_a_young_site`: a budget failure on
    rediscovery now keeps the provisional start-date boundary, recorded as
    `unresolved (AmberBudgetExhaustedError)` instead of re-storing a fallback.
- `ruff check` and `ruff format --check` are clean, and `uv lock --check` is OK.
- Coverage (CI's `--cov` run): the full run gave 99 % (4345 statements, 1 missed: `manager.py:1277`, which raises a fallback boundary to the site's start date). The budget-failure test that used to reach it now keeps the boundary. I added `test_first_discovery_stopped_on_a_young_site` (no stored boundary yet), which covers it; the retention test files then pass (15) with 1277 covered, which brings it back to 100 %. The full suite was not re-run after that one test.
- CI on GitHub: not run (no push access). The hassfest and HACS actions can't run
  locally. The manifest changed only in `version`, and no translation keys changed.

## 3. Lab verification on VM 9102

HA 2026.10.0, test site with channels E9/B9. Base: snapshot `m15-ha2026-10` (2.1.0, no
mappings), after saving the post-hang state as `post-hang-1023`.

**On 2.1.0 (reproducing production):**
- Created two template helpers through HA's config flow: `sensor.whole_house_energy_consumed`
  and `sensor.whole_house_energy_produced` (energy, total_increasing, kWh).
- Mapped them to E9 and B9 through the "own_sensor" sub-entry flow.
- Result:
  - E9's `sensor.whole_house_energy_consumed_own_sensor_reconciliation` is on its own
    device.
  - **B9: no entity, no device** (production's item 2).
  - Title "Amber <NMI>" (the full NMI, redacted here), device "Amber Energy Dashboard".
- Renamed `sensor.amber_energy_dashboard_days_behind` to `sensor.amber_lag`, recorded all
  18 entity IDs by unique ID, and took snapshot `m15-210-mapped`.

**Install 2.1.1** (`git archive` of `599457e`; the clone's `devices.py` sha256 matched the
repo) and restart. Read back:

| Check | Result |
|---|---|
| Entry title | `Amber Electric` |
| Site device | `Amber Energy Dashboard (site)`, sub-entries `[None]` |
| Entity IDs changed (by unique ID, 18 compared) | **none**; `sensor.amber_lag` kept |
| New entity | `sensor.whole_house_energy_produced_own_sensor_reconciliation` |
| B9 device | "Whole House Energy Produced (own sensor)", its sub-entry only, via the site device |
| E9 | unchanged, on "Whole House Energy Consumed (own sensor)", via the site device |
| Friendly names | "Amber Energy Dashboard (site) Run now", etc. |

Both reconciliation sensors read `unknown`, as expected: the stand-ins were created minutes
earlier and have no statistics. The values are covered by tests.

**Outage check (the retention guard).** The 2.1.0 store in the snapshot already had the
moved boundary:

```
boundary 2026-07-25, previous_boundary 2026-07-24, method "fallback (AmberConnectionError)",
needs_discovery true, provisional true, last_verified 2026-10-22
```

- With SSH add-on protection off, I appended `127.0.0.1 api.amber.com.au` to the HA
  container's `/etc/hosts` and pressed **Run now** (08:26). (The first attempt, changing
  the DNS plugin's servers, still resolved through the router and was reset.)

  ```
  WARNING ... Usage retention check failed (unresolved (AmberConnectionError)); keeping the boundary 2026-07-25
  boundary 2026-07-25, method "unresolved (AmberConnectionError)", measured_on 2026-10-23,
  last_verified 2026-10-22, needs_discovery true, provisional true
  last_run: api_unavailable, ClientConnectorError
  ```

  2.1.0 would have stored today − 89 = **2026-07-26** here.
- Restored `/etc/hosts` (0 `amber` lines) and re-enabled protection (`"protected": true`).
  A first restore attempt with `cp` failed because `/etc/hosts` is a bind mount, so one
  more run (08:27) failed the same way; `cat >` fixed it.
- Run now at 08:29: `waiting_for_data` (no usage for 2026-10-22 yet), 12 days imported
  (10-10 to 10-21) with 2 usage calls, boundary still 2026-07-25.
- Rediscovery did not run again that day: it runs at most once a day, and today's attempt
  was the failed one. The 07:15 run tomorrow will measure the real boundary.

## 4. Open questions assigned to the milestone

**Could production be affected by the retention bug? Yes, in principle, with limited
impact.**
- The path exists in 2.0.x and 2.1.0. A daily check that fails with an API, connection or
  budget error requests rediscovery. If the next day's rediscovery also fails, the
  boundary is stored as today − 89, which moves a verified older boundary forward. Each
  further day of failure moves it one more day; on 9102: 07-23 → 07-24 (10-21), → 07-25
  (10-22).
- Days older than the stored boundary are skipped without being fetched. That matters
  only for chains still walking those days: a new own-sensor mapping, a new price series,
  or a backfill. Production's usage walk is long past them.
- A successful discovery measures from scratch, so the error lasts only while the outage
  does, and it is at most the gap between the real boundary and today − 89 (Amber keeps
  about 90 days, so a day or two).
- There is no sign that production hit this: I have no access, and production's
  `diagnostics` `retention.method` would show `fallback (Amber…Error)` if it had.
- **Suggested production check:** download diagnostics and look at `retention.method`.

**Item 1:** answered by the maintainer's production output (the registry link is correct). The
device page shows the sensor after a hard refresh, so it was treated as transient (most
likely a stale frontend). Tests added as requested.

## 5. Deviations and points for the planning chat

1. **Controlled-load mappings also get reconciliation.** Removing the general-only filter
   covers every channel type, which goes slightly beyond the brief (general and feed-in).
2. **Friendly names change** to start "Amber Energy Dashboard (site) …". Entity IDs don't
   change. Dashboards that show friendly names will see the new prefix.
3. **New-install entity IDs are pinned** to the 2.1.0 form (from the English names). This
   is a small mechanism (`async_site_entity_ids`) the brief didn't spell out. Without it,
   new installs would get `_site_` IDs that differ from the documentation and from
   existing installs.
4. **The title "Amber Electric" matches HA's built-in Amber Electric integration's name.**
   On the integrations page the entry sits under "Amber Energy Dashboard", so it reads
   fine there. Elsewhere (repairs: "The Energy dashboard isn't using … Amber Electric")
   it could be confused with the built-in integration. Consider "Amber Energy Dashboard"
   or "Amber site" if that matters.
5. **Retry timing after a failed rediscovery** stays at once a day (DESIGN section 8). A
   run later the same day with the network back keeps the old boundary until the next
   day's discovery. That is safe, since the boundary never moves on failure.
6. **DESIGN.md** sections 8, 11 and 18 were updated as built (as in earlier milestones).
7. **CHANGELOG** is dated "Unreleased": set the date when tagging.

## 6. Live Amber API calls

4 in total, all from 9102 with the test key, from 08:21 to 08:29 Sydney (outside 07:17 to
07:27):
- `/sites` at setup, at the 08:21 boot and the 08:23 restart (2);
- `/usage` at 08:29 (2).

3 more attempts (08:26 ×2, 08:27 ×1) were blocked inside the container and never reached
Amber. The test suite makes no live calls.

## 7. Lab state left behind

- **9102 `amber-test-m3`: stopped** (not destroyed), running 2.1.1 with the two stand-ins
  and mappings. Snapshots:
  - `pre-m15`, `m15-ha2026-10` (earlier);
  - **new:** `post-hang-1023` (the state after the container hang, with the original
    DNS-fallback evidence), `m15-210-mapped`, `m15-211-lab`.

  The DNS plugin is back to defaults (`ha dns reset`: servers `[]`, fallback on), the
  container's `/etc/hosts` is restored, and SSH add-on protection is on.
- No other clones. VM 101 was not touched. Nothing is left in `/config/.claude_work`.

## 8. Recommended next steps

1. Review the branch, especially the title (point 4) and friendly-name (point 2) choices.
2. Optionally start 9102 after 07:15 tomorrow: its scheduled run should rediscover and
   replace the 2026-07-25 boundary with a measured one (`method` no longer `unresolved`).
3. On sign-off: set the CHANGELOG date, merge to `main`, tag `v2.1.1` and push, once
   GitHub access is given.
4. The maintainer: supply `docs/images/integration-layout.png` for the INSTALL.md placeholder.
5. Production: check diagnostics `retention.method` (section 4).
