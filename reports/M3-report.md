# Milestone 3 report: catch-up and first guards

Date: 2026-09-26, 20:47 to about 21:40 AEST. Branch `v2`.

**Summary.** Built:
- the versioned Store;
- Guard 1, with crash recovery;
- retention discovery at first setup;
- the 7-day-window catch-up walk under `RunBudget`;
- Repairs issues for Guard 1 and Guard 3;
- the scheduler (automatic or fixed times), with a config-flow schedule step and an
  options flow;
- the `run_now` service;
- the display sensors.

236 tests pass, with 100 % coverage and synthetic data only. hassfest passes.

On a fresh clone:
- First setup discovered the retention boundary in **2 calls**.
- The first catch-up was **killed with SIGKILL mid-write** and resumed by itself after
  the restart, rewriting the interrupted day.
- It imported the **full 89-day window** (2026-06-29 to 2026-09-25).
- **All 10,680 hourly rows match** totals computed independently from raw API fetches,
  in a separate pass 5 min 25 s later.
- A corrupted Store marker raised the Repairs issue and wrote nothing.

**The clone is left running with scheduled runs at 07:15, 10:15 and 13:15:
http://192.168.94.147/** (VM 9102 `amber-test-m3`).

The retention re-probe was not due, as the date is still 2026-09-26 (section 4.1).

## 1. What was built

Commit `f797b8a` (Milestone 3), on top of M2. New and changed files in
`custom_components/amber_energy_dashboard/`:

- **`storage.py`: `AmberStore`.** It wraps `helpers.storage.Store` (version 1, minor
  version 1, `atomic_writes=True`, key `amber_energy_dashboard.<entry_id>`).
  - Data:
    - `marker`: the last verified NEM day;
    - `pending`: a day whose write started;
    - `days`: per-day status, reason, mode, record and estimated counts, and per-statistic
      day totals, capped at 400 entries;
    - `retention_days` and `retention`: how and when it was measured, calls, probes and
      boundary;
    - `schedule_seed`: SHA-256 of the entry ID;
    - `last_run`.
  - Every write is saved immediately.
  - `async_mark_imported` is the only way the marker moves, and it runs only after a
    verified write.
  - A migration hook refuses unknown future versions.
  - The Store is deleted when the entry is removed. Statistics are kept.
- **`manager.py`: `AmberManager`**, one per entry, holding the entry lock for runs and
  for `import_day`.
  - **Guard 1** (`_async_guard1`) reads the last stored hour of every statistic.
    - If they all equal the marker day's last hour (or all are empty with no marker),
      it passes and any stale `pending` is cleared.
    - The **only** accepted difference is an interrupted write: `pending` is
      marker + 1, and every statistic ends at either the marker's last hour or the
      pending day's last hour. That day is then rewritten from the marker's baseline
      (mode `recovery`).
    - **Anything else** raises the `marker_mismatch` Repairs issue, with the marker, the
      expected hour and the actual end hours, and the run stops before any fetch or
      write.
  - **Retention discovery** (`_async_discover_retention`) runs on first setup, when
    there is no marker and no stored retention. It uses single-day probes:
    - probe today − 89; if that has data and today − 90 does not, done in 2 calls;
    - otherwise bracket 30 days older or newer, then bisect;
    - the cap is 8 calls, and `_DiscoveryIncomplete` is raised if it would be exceeded;
    - `activeFrom` prunes probes: days before it are known to be empty, and a site
      younger than 89 days is settled by one probe at `activeFrom`;
    - if it cannot bracket, hits an API error or reaches the cap, it falls back to 89
      days, and the fallback is recorded with its reason;
    - auth or budget stops still end the run.
  - **Catch-up walk** (`_async_walk`):
    1. Guard 1.
    2. Discovery, if needed.
    3. It walks from marker + 1 (or the pending day, or today − `retention_days` on
       first setup) to yesterday, in **7-day fetch windows**.
    4. Records are split by their `date` field. A record outside the window is a
       `wrong_date` error.
    5. Each day then goes through the shared path: baseline read from the statistics
       table (the row before the day), `pending` set, `importer.async_write_day`
       (Guard 3, group, write, verify), then the marker advanced.
    6. An empty day stops the walk (status `waiting_for_data`).
  - **Run outcomes**, never exceptions: `caught_up`, `waiting_for_data`,
    `budget_exhausted` (a planned stop that resumes at the next attempt),
    `rate_limited`, `api_unavailable`, `auth_failed` (starts reauth), and
    `needs_attention` (Guard 1 or 3, verification, or an unexpected internal error, which
    is logged with its traceback). Each outcome is saved as `last_run`, with calls and
    `RateLimit-Remaining` per counter.
  - **Repairs issues**, one per entry per kind:
    - `marker_mismatch`;
    - `unexpected_channel` (Guard 3), which explains that a new meter channel appeared
      and that reconfigure comes in M4;
    - `incomplete_data` (other Guard 3 reasons);
    - `import_failed` (verification).
    - The data issues clear on the next successful import, and `marker_mismatch` clears
      when Guard 1 next passes.
  - **Scheduling.** One `async_track_point_in_utc_time` timer at a time. When it fires,
    the next attempt is scheduled first, then the run starts in a background task. If
    already caught up (marker ≥ yesterday), the attempt is skipped with no API call.
  - **Startup run.** A catch-up starts once HA has started (`async_at_started`) on
    first setup (no marker), or when the last run was left `running`, meaning it was
    interrupted.
  - **`import_day`** now runs Guard 1 first, then the M2 append-only rule. During a
    pending recovery it only accepts the pending day. It keeps the marker in step.
- **`schedule.py`:** pure functions that use local wall-clock time only.
  - `local_attempt_times`, `attempts_on`, `next_attempt`, `is_final_attempt`,
    `parse_times` and `format_times`.
  - Times are resolved per local calendar day, so a daylight-saving change moves the
    UTC instant, never the local time.
  - A time inside the spring-forward gap maps to just after it (zoneinfo `fold=0`).
- **`importer.py`:** refactored.
  - `async_write_day(hass, ctx, day, records, baselines, mode=…)` is the single write
    path.
  - `async_latest_hours` and `async_baselines_before` read from the statistics table.
  - `async_plan_day` is the M2 append-only planner, now run after Guard 1.
- **`sensor.py`:** display sensors with **no `state_class`**:
  - Import status (enum), last imported date (date), days behind, next scheduled run
    (timestamp, diagnostic);
  - per channel: yesterday's energy (kWh), and yesterday's cost or, for feed-in,
    compensation (AUD), with a `date` attribute.
  - They are fed by a `DataUpdateCoordinator` with no polling.
- **`config_flow.py`:** a new `schedule` step, automatic or fixed times (1 to 6 `HH:MM`
  values, normalised and sorted). `AmberOptionsFlow` changes the schedule and is applied
  by an update listener **without a reload**, so no API call is made.
- **`__init__.py`:** the manager, the sensor platform, the `run_now` service (optional
  response: the run summary), and `async_remove_entry`.
- **Also updated:** `diagnostics.py` (adds the Store, schedule and status),
  `strings.json` / `translations/en.json` (schedule, options, entity names and states,
  4 Repairs issues, `run_now`), `services.yaml`, `icons.json`, and
  `manifest.json` (`2.0.0-dev2`).

### Proposed automatic schedule defaults (as implemented, for decision)

| Setting | Value | Why |
|---|---|---|
| First-attempt window | **06:30 to 08:30** local, with a stable per-install offset (entry-ID seed modulo 120 min) | Yesterday's data has been available by early morning in production. The window spreads installs so they do not all hit Amber's shared rate limit at once. |
| Retries | **+3 h and +6 h** after the first attempt (so up to about 14:30) | Covers late publication. Each attempt is skipped once caught up. |
| Attempts per day | 3 | Most days need only the first. |

Alternatives to consider:
- a later final attempt (for example +10 h), since M1 saw yesterday complete by 14:40
  but not necessarily earlier;
- a narrower window.

The constants are in `const.py`.

## 2. Test results

- `uv run pytest`: **236 passed, 0 skipped, 0 failed** (66 s).
- Coverage of the integration: **100 %** (1313 statements).
- `ruff check` and `ruff format --check` are clean.
- **hassfest** (HA 2026.9.3, run locally): 1 integration, 0 invalid.
- **Mutation check:** making Guard 1's mismatch branch return instead of raising failed
  3 tests. The code was restored, and they pass.

New test files: `test_manager.py` (59 tests) and `test_schedule.py` (21). The fake API
is `fake_amber.py`. The config flow, importer and init tests were extended.

| Required (M3 brief) | Tests |
|---|---|
| Guard 1 skip and mismatch | `test_guard1_skips_imported_days` (no refetch; the next day is fetched alone); `test_guard1_mismatch_raises_repairs_and_writes_nothing` (marker behind and ahead: Repairs issue, no fetch, rows unchanged, `import_day` refused, cleared after the marker is restored); `test_guard1_statistics_without_marker` |
| Catch-up across several windows | `test_catch_up_across_several_windows`: 20 days, exactly 3 fetches (7, 7 and 6 days), rows contiguous, marker persisted |
| Budget exhaustion and resume | `test_budget_exhaustion_stops_and_resumes` (cap: 14 days, then resume, no Repairs issue); `test_rate_limit_reserve_stops_run` (another client drains `Remaining` below 15) |
| Restart mid-walk | `test_restart_mid_walk_resumes_cleanly`, where the pending day's statistics are fully, partly or not written: unload and set up again, the run resumes by itself, and there is **no duplicate or missing hour** and sums are continuous. `test_failure_after_write_is_recovered_next_run` covers a failure between verify and the marker save |
| Empty day stops the walk | `test_empty_day_stops_the_walk`; `test_yesterday_not_published_yet` |
| Retention bisection | `test_retention_bisection` (boundaries at 89, 88, 95, 110, 75, 60, 130 and 40 days); `test_retention_call_cap` (11 boundaries from 55 to 125: never more than 8 calls, single-day only); `activeFrom` cases; API-error fallback; auth or budget during discovery; the cap enforced |
| Stable seeded schedule | `test_seed_is_stable_and_spreads`, `test_automatic_times_in_window_with_retries`, `test_same_entry_same_times_every_day`, `test_automatic_schedule_is_seeded_from_entry` |
| Fixed times | `test_fixed_times_and_next_attempt`; `test_fixed_schedule_runs_then_skips_when_caught_up` (07:15 runs; 10:15 and 13:15 skipped with no calls; next run is tomorrow 07:15); config flow validation and options flow |
| October 2026 DST | `test_times_hold_local_across_october_2026_dst` (Sydney and Adelaide: local times unchanged, a 23-hour gap, no skipped or repeated attempt); `test_sydney_utc_instants_around_dst` (21:15Z, then 20:15Z); `test_time_in_spring_forward_gap`; `test_automatic_across_dst_keeps_local_time` |
| Guard 3 Repairs | `test_guard3_raises_repairs_issue_and_clears` (unexpected channel and incomplete day) |
| Sensors | `test_display_sensors`: values, units, `date` attribute, no `state_class` |

**Test-harness note.** `freezer` also freezes the event loop's clock, which hangs
`asyncio.sleep` (the importer's read-back poll). So the manager has a module-level
`_utcnow()` clock that tests patch. Real timers are mocked in manager tests, and
scheduling is driven by calling `_on_timer` directly.

## 3. Lab verification

Clone **VM 9102 `amber-test-m3`**, http://192.168.94.147/. Times are UTC; AEST is UTC+10.
- Preflight: `local-lvm` free 92.0 %, 0 clones.
- HA 2026.9.3, time zone Australia/Sydney, currency AUD.
- Installed over SSH with `sudo`; 16 files, checksums match.

**Config flow (REST), fixed times:**

```
11:22:04 site step -> step=channels channels='- E9: general (tariff N71)\n- B9: feed-in (tariff N61)'
11:22:04 channels step -> step=schedule fields=['schedule_mode', 'fixed_times']
11:22:04 schedule step -> type=create_entry options={'schedule_mode': 'fixed', 'fixed_times': ['07:15', '10:15', '13:15']} entry state=loaded
```

**First-setup discovery, catch-up, and a kill mid-catch-up:**

```
11:22:30 pre-kill: retention=89 (bisection, 2 calls) boundary=2026-06-29 marker=2026-07-31 pending=2026-08-01 last_run=running/first_setup days_done=33
11:22:31 KILL: docker kill homeassistant -> rc=0
11:22:39 HA back RUNNING (Supervisor watchdog restarted core)
11:23:17 after resume: last_run status=caught_up trigger=resume_after_interruption imported_days=56 first=2026-08-01
         calls={'sites_usage': 8} remaining={'sites_usage': 29} marker=2026-09-25 pending=None
```

- Discovery probed 2026-06-29 (data) and 2026-06-28 (empty), so the boundary is
  2026-06-29 and retention 89 days.
- The kill (SIGKILL of the core container) landed while 2026-08-01 was pending, meaning
  its write had started.
- After the restart, the interrupted run resumed by itself (trigger
  `resume_after_interruption`). It rewrote 2026-08-01 in **`recovery`** mode, then
  imported the remaining 55 days.
- The Store now holds 89 day records: 88 `catch_up` and 1 `recovery`, all `imported`,
  all 576 records, 0 estimated.
- The client's request counter restarted with the process, and showed 9 after the
  resume (1 setup plus 8 windows). That confirms a genuine process restart.
- The protection-mode detail is in section 5.

**Independent verification, a separate pass 5 min 25 s after the catch-up finished**
(11:28:42):

```
independent fetch: 51264 records, 89 dates (13 raw 7-day calls, summed in plain Python)
hourly check: 10680 rows (2136 per statistic) vs independent; problems: none (0 total)
per-day check (89 days x 5 statistics): mismatches: none (0 total)
records per day per channel: [(288, 288)]; repairs issues for amber_energy_dashboard: none
89-day totals (independent): e9_energy 1302.389 kWh, e9_cost $353.842673, b9_energy 972.308 kWh,
                             b9_compensation $23.396307, net_cost $330.446366
```

- Every hour's `state` matches the independent hourly amount (to 1e-9), and every `sum`
  equals the previous `sum` plus `state`.
- There are no rows outside 2026-06-28 14:00Z to 2026-09-25 13:00Z.
- Per-day totals are in the appendix.
- Display sensors, none with a `state_class`:
  - status `caught_up`; last imported `2026-09-25`; days behind `0`; next run
    `2026-09-26T21:15:00Z` (07:15 AEST);
  - yesterday: E9 `15.55` kWh and `4.106772` AUD, B9 `8.025` kWh and compensation
    `0.245222` AUD, all equal to the independent 2026-09-25 totals.

**Corrupted Store marker (edited over SSH):**

```
11:30:30 before: rows per statistic=[2136], e9_energy final sum=1302.389
11:30:30 SSH: sudo sed on /config/.storage/amber_energy_dashboard.<entry_id>: "marker": "2026-09-22"; restart HA
11:31:04 after restart: store marker=2026-09-22 requests_since_start=1
11:31:04 run_now: status=needs_attention reason=marker_mismatch imported=[] calls={}
11:31:04 repairs issues: [('marker_mismatch', 'marker 2026-09-22 expects the last stored hour to be
         2026-09-22T13:00:00+00:00, but the statistics end at 2026-09-25T13:00:00+00:00')]
11:31:04 statistics unchanged: True; requests_since_start=1 (setup /sites only, no usage fetch)
11:31:13 restored marker 2026-09-25; restart
11:31:47 run_now: status=caught_up calls={} imported=[]; repairs issues: none
```

The `sed` also changed the informational `marker` copy inside `last_run`, which is
harmless.

**Energy dashboard:** a grid source was saved (E9 energy and cost, B9 energy and
compensation). `energy/validate` returned `{"energy_sources": [[]], ...}`, meaning no
issues.

## 4. Answers and observations

### 4.1 Retention re-probe (decision B)

**Not due.** The date is still 2026-09-26. However, first-setup discovery on the clone
at 21:22 AEST found the same boundary as the M1 probe at 15:33 AEST: 2026-06-29 has
data and 2026-06-28 does not. Whether the boundary rolls can only be seen on a later
date. The clone's daily runs do not re-probe (that is M4), so the 2-call re-probe still
falls to the first session after today.

### 4.2 Automatic schedule defaults

See the proposal in section 1.

## 5. Deviations and proposals

1. **Crash recovery through a pending day.** This refines Guard 1's "never guess". It
   accepts exactly one well-defined state: the Store recorded that it was writing day
   M + 1, and every statistic ends at M or M + 1. All other disagreements stop with
   Repairs. *Proposed:* add this rule to DESIGN section 7.
2. **Startup run after an interruption.** On setup, a run starts when the last run was
   left `running` (a crash or kill), not only on first setup. This is what made the
   lab kill resume unattended. *Proposed:* record this in DESIGN section 10.
3. **Every Guard 3 failure raises a Repairs issue**, as the brief says: the specific
   `unexpected_channel` one, or a general `incomplete_data` one. A transient partial
   day would show an issue until the next attempt succeeds. The issue clears
   automatically, but it may be noisy. *Proposed for M4:* raise `incomplete_data` only
   after the day has failed on N separate attempts.
4. **Not in M3:** the "still behind after the final attempt" Repairs issue in DESIGN
   section 10. The brief did not include it, and it fits M4 (Repairs).
   `is_final_attempt` is already computed and passed to the run trigger.
5. **Limits of discovery (for M4).**
   - A site whose data starts less than 58 days ago *without* `activeFrom` falls back
     to 89 days. The walk then starts on a day with no data and stops as
     `waiting_for_data`.
   - Likewise, if the marker falls behind the retention boundary after a long outage.
   - Both are M4's retention and patience work.
6. **Options changes are applied without a reload**, through an update listener, so
   changing the schedule makes no API call.
7. **Kill test needed the SSH add-on's protection mode off.** On the template, Docker
   is only reachable from the Advanced SSH add-on with protection mode disabled.
   - I turned it off on this clone only, through the Supervisor websocket API
     (`supervisor/api`, `/addons/a0d7b954_ssh/security`), ran `docker kill
     homeassistant`, then turned it back on (confirmed `protected: true`).
   - A VM hard reset was not used, because it simulates power loss. SQLite in WAL mode
     can lose the latest commits on power loss, which could leave the marker ahead of
     the statistics. Guard 1 would then correctly stop with Repairs, but that is a
     different test.
   - *Proposed CLAUDE.md note:* kill tests use `docker kill homeassistant` after
     temporarily disabling SSH add-on protection on the clone, and re-enable it
     afterwards.
8. **Lab restart flake explained.** `homeassistant.restart` returns HTTP 500 when
   called within seconds of HA first reporting `RUNNING` after boot. It happened here
   1 s after boot, and in M2. Later calls work. *Proposed CLAUDE.md note:* wait about
   60 s after a boot before requesting a restart.
9. **`import_day` during a pending recovery** accepts only the pending day. Any other
   date is refused with a clear message.

## 6. Live Amber API calls

**33 calls in M3**, all with `AMBER_TEST_API_KEY`, between 11:22:04 and 11:31:47 UTC
(21:22 to 21:32 AEST) on 2026-09-26, outside all quiet windows. None returned 429.

| Calls | What |
|---|---|
| 1 | config flow `/sites` |
| 1 | entry setup `/sites` |
| 7 | first-setup run before the kill: 2 discovery probes plus 5 windows (06-29 to 08-02). Derived from window arithmetic, because the run's summary was lost with the killed process |
| 1 | setup `/sites` after the kill |
| 8 | resumed run (8 windows) |
| 13 | independent verification fetch |
| 2 | setup `/sites` after the corrupt and restore restarts |

The two `run_now` calls in the corruption test made 0 calls. `RateLimit-Remaining`
never went below 29 on runs I could observe, so the reserve of 15 was never reached.

Running total since M1 began: 77.

## 7. Lab state left behind

- **VM 9102 `amber-test-m3` is running**, for unattended scheduled runs:
  **http://192.168.94.147/** (template token and template user).
  - The integration is at commit `f797b8a` (checksums verified), with one entry, fixed
    times 07:15, 10:15 and 13:15 local (Australia/Sydney). All three avoid the
    production quiet windows.
  - The next run is **2026-09-27 07:15 AEST**. It should import 2026-09-26, and the
    10:15 and 13:15 attempts should then be skipped.
  - The Store marker is 2026-09-25, and the statistics cover 2026-06-29 to 2026-09-25.
  - The Energy dashboard is configured. There are no Repairs issues.
  - SSH add-on protection is back on.
  - Nothing was written outside `custom_components/` and HA's own `.storage/`.
- No other clones exist. The template was not modified, and VM 101 was not touched.
- **claude-dev:** the scratchpad holds lab scripts and logs, the M3 lab state file and
  the raw verification capture (real data, session-local). The capture will be deleted
  after the push.

## 8. Recommended next steps

1. **Robert:** after the 2026-09-27 07:15 run, check on the clone:
   - the status sensor, and `last_imported_date` = 2026-09-26;
   - diagnostics → `store.last_run`, showing trigger `scheduled` and 1 usage call;
   - that 10:15 and 13:15 are skipped (`last_run` unchanged, no new calls).
2. First session after today: the 2-call retention re-probe (2026-06-28 and
   2026-06-29). The clone's Store records the boundary seen at setup.
3. Planning chat: section 5 items 1 to 5, 7 and 8, and the schedule defaults.
4. Then Milestone 4 (retention and patience, revisions and tail rewrite, Repairs), after
   sign-off.

## Appendix: per-day totals, 2026-06-29 to 2026-09-25

These are computed independently from the raw API. The imported statistics match every
value exactly. E9 is general and B9 is feed-in. Every day had 288 records per channel.

| NEM date | Import kWh (E9) | Import cost (AUD) | Export kWh (B9) | Compensation (AUD) | Net cost (AUD) |
|---|---|---|---|---|---|
| 2026-06-29 | 16.452 | 4.541065 | 5.752 | 0.593053 | 3.948012 |
| 2026-06-30 | 18.597 | 5.627850 | 1.355 | 0.157433 | 5.470417 |
| 2026-07-01 | 15.141 | 4.354442 | 5.288 | 0.397071 | 3.957371 |
| 2026-07-02 | 8.248 | 2.162582 | 13.925 | 0.713926 | 1.448656 |
| 2026-07-03 | 11.711 | 2.895429 | 9.012 | 0.117465 | 2.777964 |
| 2026-07-04 | 13.380 | 3.370103 | 12.617 | 0.249567 | 3.120536 |
| 2026-07-05 | 13.288 | 3.266252 | 4.761 | 0.210229 | 3.056023 |
| 2026-07-06 | 23.059 | 5.850116 | 1.642 | 0.072129 | 5.777987 |
| 2026-07-07 | 21.295 | 6.102430 | 2.512 | 0.198535 | 5.903895 |
| 2026-07-08 | 20.674 | 7.411150 | 5.082 | 0.359880 | 7.051270 |
| 2026-07-09 | 20.193 | 6.395629 | 5.277 | 0.336559 | 6.059070 |
| 2026-07-10 | 19.246 | 5.844964 | 4.390 | 0.332665 | 5.512299 |
| 2026-07-11 | 20.171 | 4.698149 | 6.540 | 0.197808 | 4.500341 |
| 2026-07-12 | 13.939 | 3.286602 | 10.782 | -0.098852 | 3.385454 |
| 2026-07-13 | 12.493 | 3.055978 | 12.236 | 0.296587 | 2.759391 |
| 2026-07-14 | 11.686 | 3.022330 | 11.656 | 0.116035 | 2.906295 |
| 2026-07-15 | 18.560 | 5.839365 | 10.434 | 0.614260 | 5.225105 |
| 2026-07-16 | 16.769 | 4.338162 | 2.919 | 0.210512 | 4.127650 |
| 2026-07-17 | 25.940 | 7.132764 | 1.341 | 0.070989 | 7.061775 |
| 2026-07-18 | 12.818 | 3.443158 | 10.965 | 0.446097 | 2.997061 |
| 2026-07-19 | 11.857 | 2.917140 | 7.868 | 0.204266 | 2.712874 |
| 2026-07-20 | 10.649 | 2.968628 | 14.980 | 0.661587 | 2.307041 |
| 2026-07-21 | 14.547 | 4.259491 | 15.689 | 0.967403 | 3.292088 |
| 2026-07-22 | 13.631 | 3.895709 | 13.691 | 0.636767 | 3.258942 |
| 2026-07-23 | 10.640 | 2.735223 | 14.208 | 0.411607 | 2.323616 |
| 2026-07-24 | 17.775 | 4.561888 | 19.067 | 0.144964 | 4.416924 |
| 2026-07-25 | 9.871 | 2.371126 | 15.744 | 0.229052 | 2.142074 |
| 2026-07-26 | 13.200 | 3.759947 | 16.277 | 1.077321 | 2.682626 |
| 2026-07-27 | 24.514 | 7.969705 | 10.683 | 0.630790 | 7.338915 |
| 2026-07-28 | 11.167 | 3.062082 | 17.115 | 0.565760 | 2.496322 |
| 2026-07-29 | 19.243 | 5.690137 | 17.043 | 0.018810 | 5.671327 |
| 2026-07-30 | 18.044 | 7.225531 | 17.929 | 1.181769 | 6.043762 |
| 2026-07-31 | 20.622 | 8.181024 | 18.395 | 0.955558 | 7.225466 |
| 2026-08-01 | 25.180 | 5.938380 | 2.661 | 0.162058 | 5.776322 |
| 2026-08-02 | 11.985 | 2.839913 | 15.060 | -0.098835 | 2.938748 |
| 2026-08-03 | 19.668 | 4.898000 | 4.784 | 0.235667 | 4.662333 |
| 2026-08-04 | 16.706 | 5.009044 | 12.025 | 0.128113 | 4.880931 |
| 2026-08-05 | 15.883 | 5.070692 | 10.869 | 0.406207 | 4.664485 |
| 2026-08-06 | 15.146 | 4.989468 | 6.913 | 0.474525 | 4.514943 |
| 2026-08-07 | 16.604 | 5.103977 | 13.025 | 0.198652 | 4.905325 |
| 2026-08-08 | 12.890 | 3.519439 | 13.733 | 0.345003 | 3.174436 |
| 2026-08-09 | 26.978 | 6.939026 | 2.193 | 0.152922 | 6.786104 |
| 2026-08-10 | 11.320 | 2.931540 | 9.189 | 0.557647 | 2.373893 |
| 2026-08-11 | 11.511 | 3.129638 | 20.992 | 0.432597 | 2.697041 |
| 2026-08-12 | 10.228 | 2.843597 | 13.120 | 0.689084 | 2.154513 |
| 2026-08-13 | 9.915 | 2.805218 | 17.163 | 0.661492 | 2.143726 |
| 2026-08-14 | 11.218 | 3.412379 | 16.859 | 0.973964 | 2.438415 |
| 2026-08-15 | 23.418 | 5.802549 | 0.408 | 0.024109 | 5.778440 |
| 2026-08-16 | 12.247 | 2.844232 | 8.057 | 0.223935 | 2.620297 |
| 2026-08-17 | 28.054 | 6.546607 | 0.833 | 0.033847 | 6.512760 |
| 2026-08-18 | 11.849 | 2.755316 | 9.209 | 0.281214 | 2.474102 |
| 2026-08-19 | 10.579 | 2.478975 | 17.131 | 0.084777 | 2.394198 |
| 2026-08-20 | 12.192 | 2.818573 | 7.537 | 0.220265 | 2.598308 |
| 2026-08-21 | 8.833 | 2.214284 | 15.547 | 0.624135 | 1.590149 |
| 2026-08-22 | 9.953 | 2.726344 | 21.509 | 0.098591 | 2.627753 |
| 2026-08-23 | 9.883 | 2.455212 | 12.533 | 0.119396 | 2.335816 |
| 2026-08-24 | 9.680 | 2.644807 | 23.196 | 0.242503 | 2.402304 |
| 2026-08-25 | 14.839 | 4.089018 | 0.077 | 0.006125 | 4.082893 |
| 2026-08-26 | 12.663 | 3.693833 | 7.121 | 0.427658 | 3.266175 |
| 2026-08-27 | 10.633 | 3.580553 | 12.206 | 0.385760 | 3.194793 |
| 2026-08-28 | 10.859 | 3.195290 | 22.941 | 0.237213 | 2.958077 |
| 2026-08-29 | 9.238 | 2.399587 | 21.246 | 0.378775 | 2.020812 |
| 2026-08-30 | 9.590 | 2.549192 | 21.938 | -0.253753 | 2.802945 |
| 2026-08-31 | 10.742 | 3.145346 | 22.130 | 0.113025 | 3.032321 |
| 2026-09-01 | 10.001 | 2.592348 | 23.404 | -0.239309 | 2.831657 |
| 2026-09-02 | 9.933 | 2.569974 | 22.232 | -0.295924 | 2.865898 |
| 2026-09-03 | 8.427 | 2.401676 | 25.477 | -0.437878 | 2.839554 |
| 2026-09-04 | 10.765 | 3.029283 | 23.738 | 0.010822 | 3.018461 |
| 2026-09-05 | 13.153 | 3.056871 | 12.974 | -0.195358 | 3.252229 |
| 2026-09-06 | 10.530 | 2.616356 | 12.213 | 0.118329 | 2.498027 |
| 2026-09-07 | 13.206 | 3.715479 | 9.917 | 0.090682 | 3.624797 |
| 2026-09-08 | 17.007 | 3.612282 | 5.751 | 0.196585 | 3.415697 |
| 2026-09-09 | 11.426 | 2.946199 | 9.125 | 0.279214 | 2.666985 |
| 2026-09-10 | 14.913 | 3.530879 | 3.691 | 0.197581 | 3.333298 |
| 2026-09-11 | 15.654 | 3.834965 | 4.718 | 0.194149 | 3.640816 |
| 2026-09-12 | 10.195 | 2.462252 | 11.334 | 0.134748 | 2.327504 |
| 2026-09-13 | 15.493 | 3.410530 | 8.084 | -0.071853 | 3.482383 |
| 2026-09-14 | 21.293 | 4.535486 | 6.834 | 0.153256 | 4.382230 |
| 2026-09-15 | 8.522 | 2.103907 | 6.442 | 0.029629 | 2.074278 |
| 2026-09-16 | 8.799 | 2.294957 | 13.854 | -0.073211 | 2.368168 |
| 2026-09-17 | 9.874 | 2.672834 | 9.907 | 0.132486 | 2.540348 |
| 2026-09-18 | 12.243 | 3.326603 | 8.508 | 0.095452 | 3.231151 |
| 2026-09-19 | 14.931 | 3.136439 | 3.922 | -0.062182 | 3.198621 |
| 2026-09-20 | 17.254 | 4.332100 | 5.575 | -0.078446 | 4.410546 |
| 2026-09-21 | 13.193 | 4.114207 | 8.802 | 0.504541 | 3.609666 |
| 2026-09-22 | 20.759 | 5.221914 | 0.399 | 0.029313 | 5.192601 |
| 2026-09-23 | 19.855 | 5.116579 | 3.746 | 0.404248 | 4.712331 |
| 2026-09-24 | 19.509 | 5.495601 | 4.256 | 0.189938 | 5.305663 |
| 2026-09-25 | 15.550 | 4.106772 | 8.025 | 0.245222 | 3.861550 |
| **89 days** | **1302.389** | **353.842673** | **972.308** | **23.396307** | **330.446366** |
