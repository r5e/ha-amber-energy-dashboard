# Milestone 4 report: recovery

Date: 2026-09-26, 21:52 to about 23:10 AEST. Branch `v2`.

**Summary.** Built:
- the daily retention re-verify, self-correcting in both directions;
- skipping of days older than the boundary, including after a long outage;
- Guard 2 patience;
- revisions with a crash-safe tail rewrite;
- the "still behind after the final attempt" Repairs issue;
- `incomplete_data` raised only on the final attempt;
- a reconfigure flow for new channels;
- `patience_days` and `revision_days` options;
- Store 1.2, which separates `last_written` from the marker, with a migration from
  1.1.

**282 tests pass, with 100 % coverage** and synthetic data only. hassfest passes.

**Lab.** On a fresh clone (VM 9103, now destroyed), every M4 path ran against the
**real Amber API**:
- a genuine daily re-probe, plus self-correction from staged retention values in both
  directions (1 day and many);
- a staged 8-month outage: 148 days skipped as unavailable, 89 real days re-imported;
- a **genuinely empty** day written off as a gap after patience;
- a real scheduled final attempt raising the `behind` issue;
- a tail rewrite of real data that left the statistics identical;
- a revision with no change, which wrote nothing;
- the reconfigure flow.

A final independent check of all 10,680 real hourly rows found no problems.

**VM 9102 was not touched**, apart from one read-only Proxmox status read. Upgrade steps
are in section 9.

The retention re-probe was not due, since today is still 2026-09-26 (section 4.1).

## 1. What was built

Commits since M3 (`4d3d93c`):
- `9285c2e`: M3 decisions recorded in DESIGN sections 7 and 10;
- `e943651`: Milestone 4;
- this report.

**Also recorded:**
- **DESIGN section 7:** pending-day crash recovery.
- **DESIGN section 10:**
  - the startup run after an interruption;
  - the accepted schedule defaults (06:30 to 08:30, +3 h, +6 h);
  - `incomplete_data` raised only on the final attempt, with `unexpected_channel` still
    immediate.
- **CLAUDE.md (local)** has the kill-test note, and the restart note, now worded as
  "about 60 s after **any** `RUNNING`, boot or restart": the lab showed the HTTP 500
  also happens straight after a restart.

**Store 1.2** (`storage.py`). New fields:
- `last_written`: the last day with statistics. The `marker` is the last *resolved* day,
  imported or skipped.
- `empty_seen`: day → distinct NEM dates on which it returned empty.
- `revisions`: day → {since, fingerprint}.
- `revisions_checked`.
- `tail_rewrite`: {from, next, to} progress.
- `channel_since`: identifier → first day, for channels added later.

The migration from 1.1 sets `last_written = marker`, which is correct because in M3
every resolved day was imported. `async_mark_skipped` resolves days without statistics.

**`manager.py`.** The run order is now:

1. **Guard 1**, checked against `last_written`.
   - It is per statistic: a channel added later has no rows before its `channel_since`
     day.
   - New: the **marker must equal `last_written`, or be ahead of it only by days recorded
     as skipped**. Otherwise `marker_mismatch`. This closed a hole that a test exposed: a
     marker corrupted to a date behind `last_written` passed the M3-style check.
   - The pending-day recovery is unchanged.
2. **Tail-rewrite resume**, if a previous rewrite stopped part-way.
3. **Retention.**
   - Discovery on first setup, or when the previous check could not resolve a move.
   - Otherwise the **daily 2-call check** (`_async_verify_retention`): the boundary
     day B and B − 1.
     - B has data and B − 1 doesn't: verified.
     - B is empty but B − 1 has data: a gap, not a move; the value is kept.
     - Forward (B empty): probe B + 1; if it has data, that's a **1-day step**.
       Otherwise bracket at B + 16 and **bisect**.
     - Back (B − 1 has data): probe B − 2 for a 1-day step. Otherwise bracket at
       B − 17 and bisect.
     - The 15-day bracket keeps the worst case at exactly **8 calls**.
     - If a move cannot be bracketed: keep the value, flag `needs_discovery`, and run
       full discovery on the next day.
   - Probes are cached within a check, and `activeFrom` prunes them.
4. **Revisions**, once per NEM day (`_async_check_revisions`).
   - Expired entries are dropped: first import + `revision_days` has passed.
   - The remaining days are re-fetched, in shared 7-day windows.
   - A day is **changed** when its fingerprint differs. The fingerprint is a SHA-256 of
     (channel, floored start, kWh, cost) per interval. Quality is excluded, so
     estimated → billable with the same numbers is not a change.
   - Unchanged days with no estimated records left are dropped from the set, and
     nothing is written.
   - If any day changed: **tail rewrite** (`_async_tail_rewrite`) from the earliest
     changed day to `last_written`.
     - Each day goes through the shared per-day path (Guard 3, group, write, verify,
       with `expect_latest=False`).
     - Baselines come from the previous imported day's last row, so every sum is
       re-derived.
     - Skipped days stay skipped.
     - Progress is saved after each day. A crash or fetch failure resumes from `next` on
       the following run, before anything else is written.
5. **The walk.**
   - Days before the boundary are marked `skipped_unavailable` in one step and the
     marker moves past them. This also covers a marker left behind by a long outage.
   - A pending recovery day older than the boundary cannot be re-fetched, so it stops
     with `recovery_unavailable`.
   - **Patience** (`_async_patience`) for an empty day:
     - record the NEM date (a distinct calendar day) in `empty_seen`;
     - below `patience_days`, the run waits;
     - at or above it, the day is written off as `skipped_gap` only if a later day has
       data. That check uses the same window, or **one probe** of the next up to 7 days
       when the empty day ends its window;
     - otherwise it keeps waiting (a feed-wide outage).
   - Estimated days join the revision set as they are imported.

**Repairs.**
- **`behind`** (warning): raised by the day's **final scheduled attempt** when not
  caught up, with the last day, days behind, status and reason. It is deleted by any run
  or skipped attempt that finds the import caught up.
- **`incomplete_data`**: only on the final attempt. `import_day` and `run_now` never
  raise it.
- **`unexpected_channel`**: immediate, and cleared by reconfigure.

**Reconfigure flow** (`config_flow.async_step_reconfigure`):
1. Fetch sites with the entry's key.
2. Abort if the site is missing, has unsupported channel types, or has no changes.
3. Otherwise show the added and removed channels.
4. On confirm: record `channel_since = marker + 1` for new channels (also when the
   entry is not loaded), clear `unexpected_channel`, update the channel list, and
   reload.

New channels get statistics from that day, with baseline 0. Statistics of removed
channels are kept but no longer written.

**Options:** `patience_days` (1 to 30, default 7) and `revision_days` (0 to 60, default
14), applied without a reload. They are optional in the form; omitted means keep the
current value.

**Other changes:**
- `importer.py`: `revision_fingerprint`, `async_sums_at`, and `expect_latest` on the
  write path. `async_plan_day` is now only the re-import planner (dead append branches
  removed).
- `import_day` after skipped days: day = marker + 1 appends from `last_written`'s sums.
  Re-import is allowed only when marker = `last_written`.
- Sensors: "last imported date" is now `last_written`, and "days behind" counts from the
  marker.
- Manifest `2.0.0-dev3`.

## 2. Test results

- **282 passed, 0 skipped, 0 failed** (114 s). Coverage is **100 %** (1682 statements).
  `ruff check` and `ruff format --check` are clean. hassfest reports 1 integration, 0
  invalid.
- **Mutation check:** making the tail rewrite stop after the changed day failed **4**
  tail and revision tests. The code was restored, and all 8 pass.
- `tests/test_recovery.py` has 46 tests. M3 tests were adjusted for M4 semantics:
  - preloaded Stores are "verified today" so the daily check is not due;
  - `incomplete_data` issues are expected only on the final attempt;
  - the migration now adds `last_written`.

| Required (M4 brief) | Tests |
|---|---|
| Retention moving both ways, 1 day and many | `test_retention_reverify_self_corrects`: stable, forward 1, forward many (10 days), back 1, back many (10 days); `test_retention_move_beyond_bracket_triggers_discovery`; `test_reverify_gap_on_boundary_day`; `test_reverify_api_errors` |
| 8-call cap | `test_retention_reverify_call_cap` (13 boundaries from 3 to 39 days ago: never more than 8 calls); M3 discovery cap tests |
| Marker behind the boundary after an 8-month outage | `test_marker_behind_boundary_after_eight_month_outage`: 237 days `skipped_unavailable`, sums carried across, Guard 1 accepts the result |
| Patience write-off vs waiting | `test_patience_write_off_after_distinct_days` (same-day repeats count once; skipped on the 3rd distinct day); `test_patience_keeps_waiting_without_later_data`; `test_patience_probes_later_days_beyond_the_window` |
| Revision mid-history (tail rewrite) | `test_revision_mid_history_rewrites_tail` (days before untouched; no duplicate or missing hour; sums continuous; new totals); `test_interrupted_tail_rewrite_resumes`; `test_tail_day_unavailable_stops_and_keeps_progress`; `test_revision_before_new_channel_rewrites_without_it` |
| Revision with no change | `test_revision_without_change_writes_nothing` (0 statistics writes); `test_revision_estimated_becomes_billable_unchanged`; `test_revision_window_expires`; `test_two_estimated_days_in_one_window_and_unavailable_recheck` |
| Final-attempt Repairs | `test_behind_issue_only_after_final_attempt` (07:15 and 10:15: none; 13:15: raised; cleared when caught up); `test_incomplete_data_issue_only_on_final_attempt` |
| Reconfigure flow | `test_reconfigure_adds_new_channel` (issue immediate; flow shows "E2 (controlled load)"; `channel_since` = the failed day; E2 statistics start at 0 there; net cost includes E2; Guard 1 accepts); `test_reconfigure_without_changes_aborts`; `test_reconfigure_refusals`; `test_reconfigure_cannot_connect_and_unloaded_entry` |
| Store migration (upgrade path) | `test_store_minor_version_1_is_migrated` |

## 3. Lab verification (VM 9103 `amber-test-m4`, destroyed)

**Setup:**
- Preflight: 92.0 % free; one existing clone (9102). The limit of 2 was respected.
- HA 2026.9.3, Australia/Sydney, AUD. Installed over SSH with `sudo`: 16 files,
  checksums match.
- Config flow over REST with fixed times 07:45, 10:45 and 13:45.

Times are UTC; add 10 h for AEST.

**First setup (real):**

```
12:23:34 first setup run: status=caught_up trigger=first_setup imported=89 calls={'sites_usage': 15} remaining=27
         retention=89 (bisection, 2 calls, probes {'2026-06-29': True, '2026-06-28': False})
12:23:34 stats: 2136 hourly rows per statistic, 2026-06-28 14:00Z to 2026-09-25 13:00Z, e9 final sum 1302.389; problems: none
```

**A. Daily re-probe and self-correction (real API).** The Store edits make the check due
and set a wrong retention value.

```
A1 unchanged (89)  -> verified                          2 calls  probes 06-29 True, 06-28 False
A2 set to 88       -> moved back 1 day                  3 calls  probes 06-30 True, 06-29 True, 06-28 False
A3 set to 95       -> moved forward 6 days (bisected)   8 calls  probes 06-23 F, 06-22 F, 06-24 F, 07-09 T, 07-01 T, 06-27 F, 06-29 T, 06-28 F
A4 set to 80       -> moved back 9 days (bisected)      8 calls  probes 07-08 T, 07-07 T, 07-06 T, 06-21 F, 06-28 F, 07-02 T, 06-30 T, 06-29 T
```

Every case corrected to 89 days, and no case exceeded 8 calls.

**B. 8-month outage (staged Store and seed, real re-import):**
- The statistics were cleared, and one synthetic NEM day 2026-01-31 was seeded (E9 sum
  12.0).
- The Store was set to marker = `last_written` = 2026-01-31.

```
run_now: status=caught_up, skipped_unavailable={'from': '2026-02-01', 'to': '2026-06-28', 'days': 148}, calls=13, imported=89 days; repairs=none
stats: 2160 rows per statistic (24 seeded + 2136 real), no duplicates, no discontinuity; e9 final 1314.389 = 12.0 + 1302.389
seeded end sum 12.0 -> first real hour sum 12.67 = 12.0 + 0.67
follow-up run: caught_up, 0 calls (Guard 1 accepts the 148 skipped days)
```

**C/D. Patience on a genuinely empty day, and the `behind` issue on a real scheduled
final attempt:**
- Seeded synthetic 2026-06-27, marker there.
- Retention was set to 100 so 2026-06-28 falls inside the boundary.
- 2026-06-28 **really** returns empty from Amber, while 2026-06-29 onwards has data.

```
C1 run_now: waiting_for_data, waiting_for=2026-06-28, empty_days_seen=2 (1 earlier + today), 1 call
D  options flow -> fixed time 22:42 (no reload); scheduled (final) attempt fired at 22:42:
   status=waiting_for_data, empty_days_seen=2 (same day: not counted again);
   behind issue: severity warning, last_date 2026-06-27, days_behind 90, status waiting_for_data
C2 (6 earlier distinct days recorded) run_now: caught_up, skipped_gap=['2026-06-28'], 13 calls, imported 89 days; repairs=none (behind cleared)
   0 rows inside 2026-06-28; first real hour sum 12.67 = 12.0 + 0.67; no duplicates, no discontinuity
   store 2026-06-28 = skipped_gap, "empty on 7 separate days while later days have data"
C3 follow-up: caught_up (Guard 1 accepts marker = last_written after the skipped day)
```

**E. Revisions on real data.** The revision-set entries were staged.

```
E1 2026-09-20 with a stale fingerprint, retention 100 due for its check:
   retention -> "moved forward 11 days (bisected)", 8 calls, 100 -> 89
   revisions: changed ['2026-09-20'], rewritten_days 6 (09-20 to 09-25, one fetch reused), 9 calls in total
   statistics identical before and after (same underlying data); modes 09-20..09-25 = revision; tail_rewrite None; no discontinuity
E2 2026-09-21 with its correct fingerprint (computed locally from a raw fetch with the integration's function):
   revisions: checked ['2026-09-21'], changed [], rewritten 0, 1 call; statistics unchanged; dropped from the set (billable)
```

**F. Reconfigure flow (REST, `entry_id`, so source = reconfigure):** `abort`,
`channels_unchanged`. That is correct: the account has no new channel. The add-a-channel
path is covered by tests only.

**G. Final independent check**, 5 min 13 s after the last write:

```
13 raw 7-day fetches, 51264 records; 89 real days x 24 h x 5 statistics = 10680 rows compared:
every hourly state equals the independent amount, sums continuous from the seeded day across the gap,
final sums = seed + independent totals; problems: none
```

**Not exercised in the lab (tests only):**
- `incomplete_data` on a final attempt, because no genuinely incomplete real day was
  available;
- reconfigure with a real new channel;
- an interrupted tail rewrite.

The clone was destroyed at 12:52:52 UTC (`present after delete: False`).

## 4. Answers and observations

### 4.1 Retention re-probe (decision B)

**Not due**, since the date is still 2026-09-26. For the record, the genuine daily
re-probe on the clone at 22:24 AEST found 2026-06-29 with data and 2026-06-28 empty, the
same as at 15:33 and 21:22. Whether the boundary rolls can only be seen on a later date:
- The first session after today should run the 2-call re-probe.
- **VM 9102 will do it by itself once upgraded to M4.** Its daily check logs `verified`
  or `moved forward 1 day` with the probes.

### 4.2 Budget

The heaviest single run was 15 calls (first setup). The lowest `RateLimit-Remaining`
seen was 16, during E1, with the external consumer active. The reserve of 15 was never
reached.

## 5. Deviations and proposals

1. **Revision window runs from the first import**, not from the day's own date. Each
   estimated day gets `revision_days` of daily checks after it is imported, so an old
   estimated day found during catch-up is still checked. *Proposed:* record this in
   DESIGN section 8.
2. **The re-verify bracket is 15 days**, so the worst case is exactly 8 calls
   (2 checks + 1 step + 1 bracket + 4 bisection). A larger move is "unresolved": the old
   value is kept and full discovery (30-day bracket) runs the next day.
3. **Boundary day empty but the day before has data** is treated as a gap on the
   boundary day, not as a move, so the retention value is kept.
4. **`last_written` is separate from the marker**, and Guard 1 checks their
   consistency. This is needed for skipped days. It also caught the marker-behind
   corruption case more precisely.
5. **A pending day that is now older than retention** stops with `recovery_unavailable`
   (needs attention). It cannot be re-fetched, and guessing is not allowed.
6. **The tail rewrite re-fetches the tail** rather than reusing stored hourly states. It
   goes through the shared path as decided. The cost is at most `ceil(tail / 7)` calls,
   and fetches already made for the revision check are reused.
7. **`unexpected_channel` is not a fixable Repairs issue.** Its text points to
   **Reconfigure**, rather than adding a separate Repairs fix-flow platform.
8. **Lab staging.** Two synthetic seeded days (2026-01-31 and 2026-06-27), plus Store
   edits, created the outage and gap preconditions. Everything else was real API data.
9. **Restart note widened** in CLAUDE.md: the HTTP 500 also happens right after a
   restart. It happened once this session, and none happened after adding the 60 s
   wait.

## 6. Live Amber API calls

**100 calls in M4**, all with `AMBER_TEST_API_KEY`, between 12:22:24 and 12:52:35 UTC on
2026-09-26 (22:22 to 22:53 AEST). That is outside all quiet windows, and none returned
429.

| Calls | What |
|---|---|
| 1 | config flow `/sites` |
| 1 | entry setup `/sites` |
| 15 | first-setup run (2 discovery + 13 windows) |
| 9 | setup `/sites` after each Store-edit restart |
| 21 | scenario A (2 + 3 + 8 + 8) |
| 13 | scenario B |
| 2 | scenarios C1 and D (1 + 1) |
| 13 | scenario C2 |
| 10 | scenario E (9 + 1) |
| 1 | fingerprint fetch |
| 1 | reconfigure flow `/sites` |
| 13 | independent verification |

Running total since M1 began: 177.

## 7. Lab state left behind

- **VM 9103 destroyed.** Only **VM 9102** exists.
  - It is untouched: I made one read-only Proxmox status GET, which showed it running
    since the M3 session, with no restart.
  - It still runs **M3** (`f797b8a`), with fixed times 07:15, 10:15 and 13:15, for the
    2026-09-27 07:15 check.
- The template was not modified, and VM 101 was not touched.
- **claude-dev:** lab scripts, logs and lab state files are in the scratchpad. No raw
  usage captures were saved in M4.

## 8. Recommended next steps

1. **Robert:** check 9102's 2026-09-27 07:15 run (as in the M3 report), then sign off
   before the upgrade.
2. **Upgrade 9102 to M4** using the steps in section 9, in a separate session.
3. Planning chat: section 5 items 1 to 3 and 5 to 7.
4. First session after 2026-09-26: the retention re-probe (or read it from 9102's daily
   check after the upgrade).
5. Then Milestone 5 (usage modes, own-sensor cost, reconciliation, optional price
   series), after sign-off.

## 9. In-place upgrade of VM 9102 from M3 to M4 (steps only, not performed)

Preconditions:
- Robert has checked the 2026-09-27 07:15 run.
- Run the upgrade **outside** the production quiet windows, and **not within 10 minutes
  of 9102's own attempts** (07:15, 10:15, 13:15). For example, 14:00 to 17:00 AEST.

1. **Snapshot** (the Proxmox token has `VM.Snapshot` on the pool):
   `POST /nodes/<node>/qemu/9102/snapshot` with `snapname=pre-m4`, then wait for the
   task to finish with `OK`.
2. **Record the "before" evidence:**
   - diagnostics: `store.marker`, `store.last_run`, `schedule`;
   - websocket `recorder/statistics_during_period` for the 5 statistics: row counts,
     first and last hour, final sums;
   - `repairs/list_issues`.
   - Confirm the status sensor is **not** `running`.
3. **Install M4 over SSH:**
   ```
   tar czf - --exclude=__pycache__ -C custom_components amber_energy_dashboard |
     ssh <user>@<ip> 'sudo rm -rf /config/custom_components/amber_energy_dashboard &&
                      sudo tar xzf - -C /config/custom_components'
   ```
   Then compare `sha256sum` against the local files at the M4 commit.
4. **Restart HA:** wait at least 60 s since the last `RUNNING`, call
   `homeassistant.restart`, and wait for `/api/config` to report `state: RUNNING`. Setup
   makes 1 `/sites` call.
5. **Verify the migration:**
   - `/config/.storage/amber_energy_dashboard.<entry_id>` has `"minor_version": 2`, and
     `last_written` equals the previous `marker`;
   - diagnostics show `retention.last_verified` absent (so today's check is due);
   - there are no Repairs issues.
6. **First M4 run:** call `run_now` over the websocket. Expected:
   - `retention` `verified`, 2 calls (or a 1-day move if the boundary has rolled);
   - `revisions` checked `[]` (M3 imported no estimated days);
   - status `caught_up`, or 1 window if a new day is due;
   - Guard 1 passes.
7. **Compare "after" with "before":**
   - all pre-existing rows are **identical**, because M4 rewrites nothing unless a
     revision changes;
   - any new day is contiguous, with sums continuing;
   - the schedule is unchanged (fixed 07:15, 10:15 and 13:15);
   - `patience_days` and `revision_days` are at the defaults, 7 and 14.
8. If any check fails: **roll back** with
   `POST /nodes/<node>/qemu/9102/snapshot/pre-m4/rollback`, start the VM, wait for
   `RUNNING`, and report.
9. After Robert's sign-off, delete the `pre-m4` snapshot.

Expected Amber calls: about 4 (setup, 2 for the daily check, and at most 1 window).
