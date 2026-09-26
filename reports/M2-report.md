# Milestone 2 report: one day end to end

Date: 2026-09-26, 18:20 to about 19:00 AEST. Branch `v2`, not pushed.

**Summary.**
- **Built:** the config flow with reauth, the section 6 statistics model, the
  `import_day` service (append-only safety, Guard 3, baseline, write, read-back
  verification) and the diagnostics platform.
- **Tests:** 141 pass, with 100 % coverage and synthetic data only. hassfest passes.
- **Lab:** on a fresh clone, 3 consecutive real days were imported and the last one
  re-imported. The statistics match totals computed independently from the raw API, to
  the last decimal, hour by hour. The Energy dashboard validation shows no issues.
- **The clone is left running for inspection: http://192.168.94.111/** (VM 9101,
  `amber-test-m2`).
- **Pre-M2 decisions:**
  - A: the history rewrite is done and verified.
  - B: `docs/DESIGN.md` sections 4 and 8 are updated. The re-probe was not due, because
    today is still 2026-09-26.
  - C: the `RUNNING` rule is in CLAUDE.md.

## 1. What was built

### Commits (on `v2`, newest first, since M1 sign-off)

| Commit | Content |
|---|---|
| *(this commit)* | This report |
| `f8b4495` | Refusals raised as `ServiceValidationError` (found in the lab, section 5.3) |
| `8b7e84a` | Milestone 2: config flow, statistics model, `import_day`, diagnostics, tests |
| `9762615` | DESIGN sections 4 and 8 retention update; M1 report hash tables after the rewrite |

Before these, `filter-branch` rewrote the M1 history (decision A; section 4.1).

### Files in `custom_components/amber_energy_dashboard/`

- **`manifest.json`:** `config_flow: true`, version `2.0.0-dev1`.
- **`config_flow.py`:**
  - `user` step: API key, validated live with `GET /sites`. 401 and 403 give
    `invalid_auth`; 5xx, 429 and timeouts give `cannot_connect`; unexpected responses
    give `unknown`.
  - `site` step: active sites only, labelled `NMI <nmi> (<network>)`.
    `async_set_unique_id(site.id)` plus `_abort_if_unique_id_configured`.
  - `channels` step: the discovered channels, for confirmation. It aborts on an
    unsupported channel type.
  - The entry title is `Amber <NMI>`. The data holds the key, site ID, NMI and channels.
  - `reauth` / `reauth_confirm`: the new key must still see the entry's site
    (`site_not_found` otherwise). Uses `async_update_reload_and_abort(data_updates=…)`.
- **`statistics.py`:**
  - `ChannelConfig`, `StatisticSpec` and `Metric`
    (`energy`, `cost`, `compensation`, `net_cost`).
  - `statistic_id()` builds `amber_energy_dashboard:{site.lower()}_{suffix}` and
    rejects anything `valid_statistic_id` would reject.
  - `build_specs()` returns, per channel, energy plus cost (general and controlled
    load) or compensation (feed-in), then the site's `net_cost`.
  - `metadata()` always sets `mean_type=NONE` and `unit_class`: `"energy"` for kWh,
    `None` for AUD.
- **`importer.py`:** `async_import_day()`, run under a per-entry lock.
  1. **Plan (append-only safety), before any API call:**
     - read the last row of every statistic;
     - no history at all gives mode `first` (baseline 0);
     - history for some statistics only, or different end hours, is refused as
       `inconsistent_history`;
     - a latest hour that is not a NEM day's last hour is refused as
       `partial_history`;
     - latest = the hour before D gives mode `append`, with the baseline taken from
       that row;
     - latest = D's last hour gives mode `reimport`, which needs all 24 stored hours
       of D. Its baseline is the row before D, cross-checked against the stored first
       hour's `sum − state`. If there is no row before D, the baseline is
       `sum − state`.
     - anything else is refused as `not_next_day`, with a message naming the two
       allowed dates.
  2. **Fetch** one day, with `RunBudget` active.
  3. **Guard 3** (`check_completeness`), each failure with its own reason code:
     `no_data`, `unexpected_channel`, `missing_channel`, `channel_type_mismatch`,
     `wrong_date`, `mixed_duration`, `invalid_duration`, `duplicate_interval`,
     `incomplete_day` (the count must equal `1440 / duration`) and
     `interval_outside_day` (every start, floored to the minute, lies on the
     duration grid inside D).
  4. **Group** by `floor_hour(startTime)` in UTC into 24 buckets, using `math.fsum`.
     Cost is cents / 100. Compensation is −cents / 100. Net cost is all channels'
     cents / 100.
  5. **Rows:** `state` is the hour's amount and `sum` is `fsum(baseline + amounts so
     far)`, both rounded to 6 dp only when written.
  6. **Write:** one `async_add_external_statistics` call per statistic, with 24 rows.
  7. **Verify:** poll the read-back of D, every 0.5 s for up to 60 s, until every row's
     `start`, `state` and `sum` matches and the latest row is D's last hour. Otherwise
     it raises `VerificationFailedError`.
  8. It returns a summary: mode, record count, estimated count, durations, and per
     statistic the day total, baseline and end sum.
- **`__init__.py`:**
  - The service `import_day(date, config_entry_id?)`, registered in `async_setup` with
    an optional response. `config_entry_id` is needed only when several sites are
    loaded.
  - It maps errors:
    - auth starts reauth;
    - rate-limit reserve, 429, unavailable and other API errors give clear messages;
    - every stop is recorded in `runtime_data.last_error` with its reason.
  - `async_setup_entry` makes **no API call**, to save the shared budget (section 5.2).
- **`diagnostics.py`:** the entry with `api_key`, `nmi` and `title` redacted (the title
  contains the NMI), the statistic list, request count and last rate limit, and the
  last import and last error.
- **Also added:** `services.yaml`, `icons.json`, `strings.json` and
  `translations/en.json`.

### Tests

`tests/synthetic.py` generates Amber usage days with the recorded record structure, the
`:01` start offset, `cost = round(kwh × perKwh, 4)` and integer zeros. All kWh and
price values are invented. Test files:
- `test_config_flow.py`
- `test_importer.py`
- `test_statistics_model.py`
- the M1 files: `test_api.py`, `test_statistics_schema.py`, `test_init.py`

## 2. Test results

- `uv run pytest`: **141 passed, 0 skipped, 0 failed** (30 s).
  - Config flow: 16 tests.
  - Importer: 51 tests.
  - Statistics model: 7 tests.
  - M1 tests: 67 (client 60, statistics contract 6, setup 1).
- Coverage of the integration: **100 %** (696 statements).
- `ruff check` and `ruff format --check` are clean.
- **hassfest** (HA core 2026.9.3 script, run locally): `Integrations: 1, Invalid
  integrations: 0`. This covers the config flow, services, icons, translations and
  dependencies.

Required coverage from the M2 brief:

| Requirement | Tests |
|---|---|
| Config flow: success, bad key (401 and 403), no sites, already configured, reauth | `test_full_flow`, `test_bad_key[401/403]`, `test_no_active_sites[3]`, `test_already_configured`, `test_reauth_success`, `test_reauth_wrong_site_then_bad_key`; also cannot-connect, unknown, closed-site filtering and unsupported channel |
| Every Guard 3 failure path | `test_guard3_stops_first_import`: 11 cases covering all 10 reason codes, and asserting nothing is written. `test_guard3_leaves_history_untouched` checks that a failed append leaves the prior day's rows unchanged |
| Baseline continuity | `test_consecutive_days_continue_sums`: 72 rows, and `sum[i] = sum[i-1] + state[i]` everywhere |
| Re-import overwrites, not duplicates | `test_reimport_latest_day_overwrites`: still 48 rows, D1 untouched, D2 replaced; a repeat is a no-op. `test_reimport_of_only_day_uses_zero_baseline` |
| Append-only refusal | `test_refuses_anything_but_next_or_latest_day`: an earlier day or a gap is refused with **no API call** and no change. Also `earlier_day`, `partial_history` (mid-day end, and missing hours), `inconsistent_history` (some statistics only, different end hours, broken continuity) |
| Feed-in sign | `test_first_import_writes_model` (compensation is positive when Amber's cost is negative). `test_cents_to_aud_and_signs` covers negative prices, where compensation goes negative and import cost goes negative |
| Cents to AUD | `test_cents_to_aud_and_signs`: 1234.5678 c becomes 12.345678 AUD exactly |
| DST, October 2026 and April 2027, Sydney and Adelaide | `test_dst_transitions`, 4 cases. Each imports 3 days spanning the transition: 72 consecutive UTC hours, and the local-day (`period="day"`) totals still add up |
| Diagnostics redaction | `test_diagnostics_redacts_key_and_nmi` |
| Energy dashboard | `test_energy_dashboard_validation`: the unified grid source gives `energy_sources == [[]]` |

Other tests cover the 30-minute interval path, controlled load, the estimated-record
count, auth errors starting reauth, each API failure class, verification failure,
budget refusal and multi-entry service resolution.

**Mutation check** (a temporary edit, reverted and re-tested):
- Setting the append baseline to 0 failed 5 tests: continuity and DST.
- Removing the not-next-day refusal failed 3 refusal tests. The read-back verification
  also timed out, which shows it catches a wrong write.

## 3. Lab verification

The clone is VM **9101** `amber-test-m2`, a linked clone of the template. Before
cloning: `local-lvm` free 92.0 %, and 0 existing clones. Times are UTC; add 10 h for
AEST. The template VMID and SSH user are shown as placeholders.

```
08:37:36 clone: <template> -> 9101 (amber-test-m2), linked      exitstatus=OK
08:37:54 guest agent: enp6s18 ipv4 192.168.94.111 after 14s
08:38:12 HA state=RUNNING after 19s (version 2026.9.3, tz Australia/Sydney, currency AUD)
08:38:48 install: 12 files copied via SSH (tar | sudo tar -C /config/custom_components); sha256 match local: True
08:50:19 restart -> state=RUNNING after 9s
```

**Config flow through the REST flow API** (`POST /api/config/config_entries/flow…`):

```
08:50:48 flow start: type=form step=user fields=['api_key']
08:50:48 user step -> step=site; options=[{'value': '<site_id>', 'label': 'NMI <nmi> (<network>)'}]
08:50:48 site step -> step=channels placeholders={'channels': '- E9: general (tariff N71)\n- B9: feed-in (tariff N61)'}
08:50:48 channels step -> type=create_entry entry state=loaded
08:50:48 services registered: ['import_day']
```

**Imports** (REST, `?return_response`), in order:

| Call | Mode | Records | Estimated |
|---|---|---|---|
| 2026-09-23 | first | 576 | 0 |
| 2026-09-24 | append | 576 | 0 |
| 2026-09-25 | append | 576 | 0 |
| 2026-09-25 again | reimport | 576 | 0 |

The re-import produced baselines and sums identical to the first import of 09-25.

**Refusals**, through the websocket `call_service` after the fix in section 5.3:

```
2026-09-23: success=False code=service_validation_error message=Validation error: 2026-09-23 cannot be
  imported: the latest imported day is 2026-09-25. Only 2026-09-26 (the next day) or 2026-09-25
  (re-import of the latest day) can be imported now.
2026-09-27: success=False code=service_validation_error message=(same, for 2026-09-27)
```

The client's `requests_since_start` in diagnostics was still **0** after both
refusals, so no API call was made.

**Read-back over the websocket and independent comparison:**
- One separate raw `GET /usage` for 2026-09-23 to 25 (1728 records) was summed with
  plain Python, not integration code, per day, per channel and per UTC hour.
- `recorder/statistics_during_period` (hour, `state` and `sum`) returned **360 rows**:
  5 statistics × 72 hours, at exactly the expected UTC hours.
- For every row, `state` equals the independently computed hourly amount (to 1e-9),
  and `sum` equals the previous `sum` plus `state`. The first `sum` equals the first
  `state`, since the baseline was 0.
- **Problems: none. Per-day mismatches: none.**
- `recorder/get_statistics_metadata`:
  - all five have `source=amber_energy_dashboard`, `has_sum=True` and `mean_type=0`;
  - `e9_energy` and `b9_energy`: `kWh`, `unit_class=energy`;
  - `e9_cost`, `b9_compensation` and `net_cost`: `AUD`, `unit_class=None`.

**Energy dashboard:**
- `energy/save_prefs` with one unified grid source (`stat_energy_from=e9_energy`,
  `stat_energy_to=b9_energy`, `stat_cost=e9_cost`, `stat_compensation=b9_compensation`)
  returned `success=True`.
- `energy/get_prefs` matches what was saved.
- `energy/validate` returned
  `{"energy_sources": [[]], "device_consumption": [], "device_consumption_water": []}`.
  That means **no issues**.
- The HA currency is AUD, which matches the cost statistics.

**Diagnostics**, live (`GET /api/diagnostics/config_entry/<id>`):
- `api_key`, `nmi` and `title` are `**REDACTED**`;
- neither the real key nor the NMI appears anywhere in the output;
- `last_import` was `None`, because HA had been restarted after the imports (see
  section 5.4).

## 4. Answers to the questions assigned to this milestone

### 4.1 History rewrite (decision A)

Command: `git filter-branch --prune-empty --index-filter` over `main..v2`, replacing
`tests/fixtures/usage_2026-09-24.json` with the synthetic blob `be1115e` in every
commit that contains it. Results:
- `git rev-list --objects v2 | grep -c <real blob 2d1bb11>` gives **0**.
- After I deleted `refs/original`, `--all` also gives **0**.
- All 7 commits containing the file now hold the synthetic blob. The "replace fixture"
  commit became empty and was dropped.
- `main` is unchanged at `cc179b7`.
- The rewritten client commit `3842117` passes its own tests (51) in a temporary
  worktree.
- Old to new hashes are recorded in M1 report addendum A9, and its hash tables are
  updated.

### 4.2 Retention (decision B)

- DESIGN section 4 now reads: usage retention observed at 89 days (earliest 2026-06-29
  on 2026-09-26); 86 was the configured v1 value.
- DESIGN section 8: discover the boundary by bisection at setup (at most 8 calls,
  fallback 89); re-verify daily with 2 calls (the boundary day and the day before);
  self-correct in either direction.
- The 89-day recovery call count in section 9 is updated (13 calls).
- **Re-probe: not done**, because the date is still 2026-09-26. It is due in the first
  session on a later date.
- I left one detail out of the design text, as it was not in the decision: how far to
  move when the 2-call check shows a change. Proposed for M4 in section 5.

### 4.3 Per-day totals for comparison with production

These are Amber's figures from the raw API, which the imported statistics match
exactly. The general channel is E9 and feed-in is B9. Money values are Amber's
`cost`: cents in the raw data, AUD in HA.

| NEM date | Import kWh (E9) | Import cost | Export kWh (B9) | Feed-in (raw) | Compensation (HA) | Net cost (HA) |
|---|---|---|---|---|---|---|
| 2026-09-23 | 19.855 | 511.6579 c = $5.116579 | 3.746 | −40.4248 c | $0.404248 | $4.712331 |
| 2026-09-24 | 19.509 | 549.5601 c = $5.495601 | 4.256 | −18.9938 c | $0.189938 | $5.305663 |
| 2026-09-25 | 15.550 | 410.6772 c = $4.106772 | 8.025 | −24.5222 c | $0.245222 | $3.861550 |
| **3 days** | **54.914** | **$14.718952** | **16.027** | | **$0.839408** | **$13.879544** |

The 3-day row is the cumulative `sum` in HA at 2026-09-25 13:00Z. Every channel had
288 records per day, all `billable`.

## 5. Deviations and proposed design changes

1. **Config flow steps.** DESIGN section 12 also lists usage mode (step 4) and schedule
   (step 5). The M2 scope had only key, site and channels, so those steps come in
   M3/M5.
2. **No API call in `async_setup_entry`.** Validating the key on every HA start would
   cost a call on the shared budget. Instead, an auth failure during an import starts
   reauth. For M3: the scheduler's first run of the day is where a revoked key will
   surface.
3. **Refusals are `ServiceValidationError`** (commit `f8b4495`, found in the lab). They
   first surfaced as generic internal errors. Note: HA's REST API returns HTTP 500 for
   *any* service exception, `ServiceValidationError` included, so REST callers cannot
   see the message. The websocket and the UI show it. This is HA behaviour, not ours.
4. **In-memory status.** `last_import` and `last_error` are lost on restart until the
   M3 Store exists. The statistics themselves are the source of truth, and
   append-only planning reads them, so a restart cannot cause a wrong import.
5. **First import of any day is allowed** when no statistics exist. That is how the lab
   started at 2026-09-23. M3's catch-up decides the real starting day, which will be
   the retention boundary.
6. **An unexpected channel in usage is a Guard 3 failure** (`unexpected_channel`), not
   ignored. A new channel on the account, such as a newly installed controlled load,
   will stop imports until the entry is reconfigured. *Proposed:* M3/M4 add a Repairs
   issue for this, plus a reconfigure flow that adds statistics for the new channel.
7. **Estimated data is imported and counted**, but not yet tracked for revision (M4).
8. **Verification polls the read-back** rather than waiting on the recorder queue.
   HA documents `Recorder.block_till_done` as tests-only.
9. **Proposed for M4 (retention):** when the daily 2-call check detects a move, step
   one day at a time if it is one day, or bisect if it moved more (still within the
   8-call cap).
10. **Lab install needs `sudo`.** On the template, `/config` (a link to
    `/homeassistant`) is writable only by root. The SSH user is in `wheel` with
    passwordless sudo. *Proposed CLAUDE.md note:* installs over SSH use `sudo`.
11. **Lab observation:** one `homeassistant.restart` call over REST, the second of
    three, returned an HTTP error and HA did not restart. A retry worked. The first
    and third calls restarted normally (the connection drops, then `RUNNING` about
    9 s later). Cause unknown. Lab scripts now log the status and retry.

## 6. Live Amber API calls

**M2 total: 6 calls**, all with `AMBER_TEST_API_KEY`, between 08:50:48 and 08:53:28 UTC
on 2026-09-26 (18:50 to 18:53 AEST):
- 1 `/sites` call in the config flow;
- 4 `import_day` fetches;
- 1 independent raw 3-day usage fetch for the comparison. `RateLimit-Remaining` was 48
  at that point.

None returned 429. The refusal checks made no calls. The decision B re-probe was not
due. Running total since M1 began: 44.

## 7. Lab state left behind

- **VM 9101 `amber-test-m2` is running**, left for Robert: **http://192.168.94.111/**
  (template token and template user).
  - It has the integration installed in `/config/custom_components/amber_energy_dashboard/`
    at commit `f8b4495`, with one config entry for the real site.
  - Statistics for 2026-09-23 to 25: 5 statistics × 72 hours.
  - The Energy dashboard is configured with the grid source above.
  - No other files were written. Nothing was put in `/config/.claude_work/`, since no
    backups or scratch files were needed.
  - Destroying the VM removes everything.
- **No other clones.** The template was not modified, and VM 101 was not touched.
- **claude-dev:**
  - the scratchpad holds the lab scripts and logs, the M2 raw usage capture (real data,
    session-local only) and a lab state file;
  - `local/` (excluded from git) holds the real M1 sample;
  - `CLAUDE.md` is updated with the `RUNNING` rule (local only).
- **Git:** `v2` is 4 commits ahead of M1 sign-off (including this report). Nothing is
  pushed, and `main` is untouched.

## 8. Recommended next steps

1. **Robert:** compare the per-day totals in 4.3 with production for 2026-09-23 to 25.
   Inspect the clone (Energy dashboard, Developer tools → Statistics), then tell me
   whether to keep or destroy VM 9101.
2. **Robert / planning chat:** section 5 items 2, 5, 6, 9 and 10.
3. The first session on a date after 2026-09-26: the 2-call retention re-probe
   (2026-06-28 and 2026-06-29), as decision B requires.
4. Then Milestone 3 (scheduler, catch-up loop, Store, Guards 1 and 3), after sign-off.

---

## Addendum: post-sign-off session (2026-09-26, 19:58 AEST onwards)

**Status: stopped before pushing.** The pre-push history scan found a configuration
value in an old version of a committed file (A3). Following the instruction to stop and
report if anything fails, nothing has been pushed. The CI runs and repository settings,
which depend on the push, are not done yet.

### A0. Production comparison (from Robert)

Import and export kWh and import cost match production exactly for 2026-09-23 to 25.
The 0.01 kWh display difference on the 09-25 export is rounding of exactly 8.025 kWh.
Dev's net cost is correct. Production's is wrong: v1's compensation statistic carries
Amber's negative sign, so the Energy dashboard adds feed-in earnings to cost. This is
recorded in DESIGN section 14 (A2).

### A1. VM 9101 destroyed

```
09:58:49 stop: task finished exitstatus=OK
09:58:51 destroy: task finished exitstatus=OK
09:58:51 destroy: VM 9101 present after delete: False; clones now: []
```

No clones exist.

### A2. Planning-chat decisions implemented

| Commit | Content |
|---|---|
| `c72ea39` | **Item 2 reversed:** `async_setup_entry` validates the key with one `GET /sites` (details below). |
| `e80a7cc` | **DESIGN updates** (details below). |

**Setup validation (`c72ea39`):**
- 401 or 403 raises `ConfigEntryAuthFailed`, which starts reauth.
- A timeout or other network error, 5xx or 429 raises `ConfigEntryNotReady`, so HA
  retries setup.
- Two small additions beyond the decision, flagged here for review:
  - a key that no longer lists this site also raises `ConfigEntryAuthFailed`, because
    reauth asks for a key that sees the site;
  - an unexpected or malformed response raises `ConfigEntryNotReady`.
- New tests in `test_init.py`:
  - setup makes exactly one call, with the entry's key;
  - 401 and 403 give `SETUP_ERROR` and a reauth flow;
  - a missing site gives reauth;
  - timeout, 500, 503, 429 and a malformed response give `SETUP_RETRY` and no reauth.
- Importer tests now mock `/sites` for setup. The refusal tests assert "no usage
  request" rather than "no request".

**DESIGN updates (`e80a7cc`):**
- Section 14: the migration **negates legacy compensation** (hourly `state` and
  cumulative `sum`) when copying history, and the parity check compares compensation
  **by magnitude**. Energy and import cost are still compared exactly.
- Item 6 (section 7, Guard 3): `unexpected_channel` gets a Repairs issue and a
  reconfigure flow in M3/M4, and imports stay stopped until then.
- Item 9 (section 8): a retention move of one day steps one day. A larger move is
  found by bisection, within the 8-call cap.
- Section 12: setup-time key validation.
- Items 5 and 10 were accepted. Item 5 needs no code change. Item 10's `sudo` note is
  in CLAUDE.md, which is local only.

**Tests: 150 passed, 0 skipped**, with 100 % coverage (706 statements). `ruff check`
and `ruff format --check` are clean.

### A3. Pre-push checks

**Identity: PASS.** All 16 commits in `main..v2` have author and committer
`r5e <6285489+r5e@users.noreply.github.com>`. Their only trailer is
`Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

**Leak scan across everything reachable from `v2`** (19 commits, 60 blobs, plus all
commit metadata and messages):

| Check | Result |
|---|---|
| Secrets (`PVE_TOKEN_SECRET`, `HA_TEMPLATE_TOKEN`, `HA_LAB101_TOKEN`, `AMBER_TEST_API_KEY`) | none |
| Real site ID (either case) and real NMI | none |
| Real network name, the owner's surname, employer email domain | none |
| Real usage data: the real M1 fixture blob | not reachable |
| Real usage data: record-by-record comparison of every usage-shaped JSON blob against both real captures (the M1 day and the M2 lab's 3 days) | 1 usage blob (the synthetic fixture): 4 of 576 records coincide, the known chance collisions of 3-decimal kWh noted in the M1 addendum |
| Commit messages and metadata | none |
| Other configuration values | **2 hits**, below |

- **`uv.lock`:** the template VMID's digits occur inside three PyPI package hash URLs.
  This is coincidence, not a leak.
- **`reports/M1-report.md`, blob `a4a0ab9`:** the first-session version of the M1
  report contains the **template VMID value** 4 times (in the clone API path and three
  mentions of the pool contents). That version was written before configuration values
  were ruled out of committed files. It was the file's content in commits `552c607`,
  `e0f3cee`, `98476bd` and `84866fb` (hashes before the A9 rewrite; now `6f980d7`,
  `bd18c5b`, `5a28eff` and `94c2831`). The current version (from `78aad33`, now
  `7ce3b49`) uses
  placeholders.

The push gate's listed criteria all pass: no secrets, no real site ID or NMI, no real
usage data. But CLAUDE.md says configuration values never go into committed files, and
a push would publish this old blob permanently. So I stopped. **Options for Robert:**
1. **Accept and push.** It is the Proxmox VMID of the lab template, which is low
   sensitivity.
2. **Approve a second history rewrite before the first push** (recommended, since it is
   cheap now and impossible after publishing). It would replace the value with
   `<template>` in `reports/M1-report.md` in the four commits that contain it.
   Hashes from `552c607` (pre-rewrite) onwards change, and the report hash tables need updating
   again. I would then re-run both checks and push.

### A4. Push, CI and repository settings: not started

These wait on A3. For when they go ahead:
- The push is `git push -u origin v2` only. `main` is never pushed.
- CI: all three workflows run on push to any branch (`on: push`).
  - The HACS action may validate the repository as a whole, including the default
    branch, which is still the v1 kit on `main`. If it fails for that reason on `v2`,
    I will report it rather than change `main`.
- **Repository description and topics cannot be set with a deploy key.** A deploy key
  gives git access only, not the GitHub API, and `gh` is not installed here. Please set
  them on the repository page, via the gear icon next to **About**:
  - **Description:** `Unofficial Home Assistant integration that imports Amber Electric
    usage and cost history into the Energy dashboard.`
  - **Topics:** `home-assistant`, `homeassistant`, `hacs`, `hacs-integration`,
    `amber-electric`, `energy`, `energy-monitoring`
  - HACS also expects **Issues** to be enabled, under Settings → General → Features,
    and the repository not to be archived.

### A5. Retention re-probe: not due

The date is still 2026-09-26, so the 2-call re-probe of 2026-06-28 and 2026-06-29 was
not run.

### A6. Live Amber API calls

**0 in this session.** The M2 total stays at 6, and the running total since M1 began
at 44.

### A7. Lab state

- **No VMs** other than the template. VM 9101 was destroyed. VM 101 was not touched.
- **Git:** 17 commits on `v2` ahead of `main` (including this addendum). **Nothing
  pushed.**
- **claude-dev:**
  - `local/` (excluded from git) holds the real M1 sample;
  - the scratchpad holds lab scripts and logs, and the M2 raw capture, which the leak
    scan used as its fingerprint source and which will be deleted once the push is
    done;
  - `CLAUDE.md` has the `sudo` note (local only).

### A8. Next steps

1. **Robert:** choose option 1 or 2 in A3.
2. I then push `v2`, check the hassfest, HACS and pytest runs and fix anything that
   fails, and add the results to this addendum.
3. **Robert:** set the description, topics and Issues setting from A4.
4. The retention re-probe in the first session dated after 2026-09-26.
5. M3 after sign-off.
