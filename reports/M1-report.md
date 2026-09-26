# Milestone 1 report: scaffolding and verification

Date: 2026-09-26. First session 14:28 to about 15:10 AEST; follow-up session 15:25 to
about 15:45 AEST (see the **Addendum** at the end). Branch `v2`, not pushed.

**Summary.** Scaffolding, dev environment, CI files, API client and tests are done.
All five API verifications in DESIGN section 4 are answered. The StatisticMetaData
schema is verified. After the Proxmox permission fix, **the lab smoke test passed**
(addendum A1). The follow-up session also applied the planning-chat decisions:
- design changes in `docs/DESIGN.md`
- the per-run rate-limit budget
- a synthetic usage fixture
- the usage retention probe (**earliest usage date 2026-06-29, 89 days back**)

Sections 1 to 8 are the original report, corrected where the follow-up changed them.
The addendum holds the new evidence.

## 1. What was built

Commits on `v2` (newest first):

| Commit | Content |
|---|---|
| `2f99b30` | Tests pinning the HA 2026.9 external statistics contract |
| `3842117` | Async Amber API client, tests and anonymised fixtures |
| `b461bea` | uv dev environment (`pyproject.toml`, `uv.lock`, `.python-version`) and CI workflows |
| `0076035` | Integration scaffold, `hacs.json`, `.gitignore`, README draft |
| `88dabd9` | `git mv` of the v1 kit into `legacy/v1/` (4 files, pure renames, 0 line changes) |

Files:

- `legacy/v1/`: `README.md`, `amber_backfill.py`, `configuration_snippet.yaml`,
  `daily_automation.yaml`, unchanged. `LICENSE` stays at the root.
- `custom_components/amber_energy_dashboard/`
  - `manifest.json`: domain `amber_energy_dashboard`, name "Amber Energy Dashboard
    (unofficial)", `dependencies: ["recorder"]`, `iot_class: cloud_polling`,
    `integration_type: service`, no requirements, `version: 2.0.0-dev0`,
    `config_flow: false` for now (see section 5).
  - `__init__.py`: `CONFIG_SCHEMA = config_entry_only_config_schema`, plus a no-op
    `async_setup`.
  - `const.py`: `DOMAIN`, `API_BASE_URL`.
  - `api.py`: the client.
    - `AmberClient(session, api_key, *, base_url, timeout)` with
      `async_get_sites()`, `async_get_usage(site_id, start, end, resolution=None)`
      and `async_get_prices(...)`.
    - It exposes `request_count` and `last_rate_limit` (parsed `RateLimit-*` headers).
    - Models are frozen, slotted dataclasses: `Site`, `Channel`, `UsageRecord`,
      `PriceInterval` (with `is_actual`) and `RateLimit`. Datetimes must carry an
      offset. Numbers are type-checked, and a bool is not accepted as a number.
    - Errors all derive from `AmberError`:
      - `AmberAuthError` (401/403)
      - `AmberRateLimitError` (429; `retry_after` comes from `Retry-After`, falling
        back to `RateLimit-Reset`)
      - `AmberServerError` (5xx)
      - `AmberRequestError` (other statuses, for example the 422 for an over-long range)
      - `AmberConnectionError`, with subclass `AmberTimeoutError`
      - `AmberResponseError` (non-JSON body, non-list body, missing, null or wrongly
        typed field)
    - An empty list is returned as-is. Deciding what it means is the caller's job
      (Guard 2).
    - Default timeout is 30 s total, 10 s connect. Error bodies are truncated to 300
      characters, and the API key never appears in exception text.
- `hacs.json`: name, `homeassistant: "2026.9.0"`, `render_readme`.
- `.gitignore`: `.env*`, `*.env`, `env`, `secrets.yaml`, key files, `amber-lab/`,
  `amber_backfill_cache.json`, `raw_captures/`, `*.raw.json`, plus Python and tool caches.
- `README.md`: v2 draft with an under-development banner, scope, requirements, a
  Credits section covering all prior art from DESIGN section 2 (all URLs checked, HTTP
  200), and pointers to `legacy/v1/`.
- `.github/workflows/`:
  - `hassfest.yml` uses `home-assistant/actions/hassfest@master`.
  - `hacs.yml` uses `hacs/action@main` with `category: integration` and
    `ignore: brands`, since brand assets arrive in M6.
  - `tests.yml` uses `setup-uv@v10`, runs `uv sync --locked`, `ruff check`,
    `ruff format --check` and pytest with coverage.
- `pyproject.toml`: Python `>=3.14.2`, dev group
  `pytest-homeassistant-custom-component==0.13.366` (pins `homeassistant==2026.9.3`,
  the newest 2026.9.x) and `ruff`. Ruff rules: E, W, F, I, B, UP, SIM, ASYNC, RUF, PL,
  TRY, DTZ. `legacy/` is excluded.
- `tests/`:
  - `test_api.py`: client tests.
  - `test_statistics_schema.py`: recorder contract.
  - `test_init.py`: the scaffold loads with the recorder.
  - `fixtures/`: anonymised recordings, listed in section 4.

## 2. Test results

- `uv run pytest`: **57 passed, 0 skipped, 0 failed** (1.5 s).
- Coverage of `custom_components/amber_energy_dashboard`: **100 %** (212 statements).
- `ruff check .`: clean. `ruff format --check .`: clean.
- **hassfest**, run locally from HA core tag `2026.9.3` in a throwaway venv:
  `Integrations: 1, Invalid integrations: 0`, exit 0. The CI action uses hassfest
  `master`, which may be stricter.
- **HACS validation was not run locally.** The action is a container that also checks
  GitHub-side metadata (repository description, topics, releases). I checked the
  local parts by hand: one integration under `custom_components/`, the manifest keys
  HACS needs, and a valid `hacs.json`. The first real run happens when the branch is
  pushed. Robert: the repository needs a description and topics on GitHub.

Harness note for later milestones: `recorder_mock` must be set up before `hass`. An
autouse fixture that depends on `hass` (the usual `auto_enable_custom_integrations`)
breaks recorder tests, so tests request `enable_custom_integrations` explicitly.

## 3. Lab verification

**Clone smoke test (first session): blocked. No VM was created.** It was re-run and
passed after the permission fix; see addendum A1.

```
POST /nodes/<node>/qemu/<template>/clone  newid=9100 name=amber-test-smoke pool=<pool> full=0
-> {"message":"Permission check failed (/storage/local-lvm, Datastore.AllocateSpace)\n","data":null}
```

- API auth works (`GET /version` returned 9.1.6).
- `GET /access/permissions` shows the token's privileges exist only on `/pool/<pool>`:
  `Datastore.AllocateSpace`, `Datastore.AllocateTemplate`, `Datastore.Audit`,
  `Pool.Audit`, `VM.Allocate`, `VM.Audit`, `VM.Clone`, `VM.Config.*`, `VM.Console`,
  `VM.GuestAgent.Audit`, `VM.PowerMgmt`, `VM.Snapshot`, `VM.Snapshot.Rollback`.
- The pool's only member is the template VM. No storage is in the pool, so the pool-level
  `Datastore.AllocateSpace` never applies to `local-lvm`.
- `GET /nodes/<node>/storage` returns an empty list for this token.
- Both template disks (`scsi0`, `efidisk0`) are on `local-lvm`. Its NIC is on `vmbr0`,
  and the guest agent is enabled.
- After the failure, the pool still contains only the template. The template was not modified.

I stopped at this point rather than trying workarounds. The fix is in section 8.

**VM 101, read-only (allowed by CLAUDE.md).** This shows the SSH key and the HA token
path work from `claude-dev`:

- `GET /api/` returned `{"message":"API running."}`.
- `GET /api/config` returned HA **2026.9.3**, time zone `Australia/Sydney`, state `RUNNING`.
- SSH with `$LAB_SSH_KEY` as `$LAB101_SSH_USER` ran one read-only command: login OK,
  Alpine Linux v3.24 (the SSH add-on container).
- Nothing was written on VM 101. The only side effect is a new `known_hosts` entry on
  `claude-dev`.

**Statistics contract, in the test harness** (real recorder, SQLite): see section 4,
item 6. This is not a substitute for the lab. It runs on clones from M2.

## 4. Answers to the Milestone 1 questions

All live observations are from one test key. The account has one site with channels
`E9` (general, tariff N71) and `B9` (feedIn, N61) and 5-minute intervals. The network name and
`activeFrom` are withheld here, as in the fixtures.

### 4.1 Maximum date range per call

**Usage and prices both allow `endDate − startDate ≤ 7`, which is 8 inclusive days.**
The error text says "Maximum 7 days", but 8 inclusive days succeeds.

| Call | Result |
|---|---|
| usage 09-24 to 09-24 (1 day) | 200, 576 records |
| usage 09-18 to 09-24 (7 days) | 200, 4032 records (7 × 288 × 2) |
| usage 09-17 to 09-24 (8 days) | 200, 4608 records (8 × 288 × 2), every date complete |
| usage 09-16 to 09-24 (9 days) | **422**, body `Range requested is too large. Maximum 7 days.` |
| usage 31 days, usage 100 days | 422, same body |
| prices 09-17 to 09-24 (8 days) | 200, 4608 intervals |
| prices 9 days, prices 31 days | 422, same body |

The 422 body is plain text, not JSON. Recommendation: use windows of 7 inclusive days,
as the documented limit says, so an Amber-side off-by-one fix cannot break us. An
86-day recovery then takes 13 usage calls (11 with 8-day windows).

### 4.2 How far back `/prices` returns actual intervals

**Earliest date with data: 2025-03-01.** 2025-02-28 and every earlier date probed
return `200 []`. That is 574 days before today. Every date returned was complete:
576 intervals, all `ActualInterval`, 5-minute.

The history goes back about 5 months before the site's real `activeFrom`. So it is
not bounded by when the account joined. It looks like a fixed start of Amber's
data store, or a long rolling window. One observation cannot tell those apart, so
re-probe in M5.

Probes: 2026-06-01, 2025-09-24, the day before `activeFrom` and 2025-05-12 had data; 2024-09-24 was
empty. Bisection then gave 2025-02-24 empty, 04-03 data, 03-15 data, 03-05 data,
02-28 empty, 03-02 data, 03-01 data.

Implication: Amber's price history will not limit own-sensor cost backfill in
practice. HA's retention of the user's own statistics will.

### 4.3 Is the rate limit per key or per account?

**Partly answered.** Here is what the headers show.

- Every authorised response carries `RateLimit-Policy: 50;w=300`, `RateLimit-Limit: 50`,
  `RateLimit-Remaining` and `RateLimit-Reset` (IETF draft names). There is no
  `Retry-After`, but I did not trigger a 429.
- The 403 for an invalid key carries no rate-limit headers.
- **The window is fixed, not sliding.** `Reset` counts down to the end of the window.
  The first request after that shows `Remaining 49`, `Reset 300`.
- **There are two independent counters:**
  - Counter A covers `/sites` and `/sites/{id}/usage`. Interleaved sites and usage
    calls gave 40, 39, 38, 37.
  - Counter B covers `/sites/{id}/prices`. Within one window it went 49, 48, 47 … 34,
    strictly consecutive across 16 calls, with sites and usage calls interleaved and
    not affecting it.
- **Something other than me was drawing on counter A.** My first call showed
  `Remaining 40`, so 10 calls had already been used in that window. Between 04:32:54
  (46) and 04:35:12 (40), counter A dropped by 5 more with no calls from me. Counter B
  never showed outside use.

Conclusion: the budget is not private to this test key, at least for counter A. It is
shared with some other client, which is consistent with "per account", but "per
source IP" would look the same from here. **Questions for Robert:**

- What else calls Amber about twice a minute (core `amberelectric`, Amber Express,
  other scripts), and with which key?
- Is the test key used anywhere else?
- Do production and the lab share a public IP?

A definitive test needs a second key on the same account, used from here.

### 4.4 Usage `date` is the NEM date, and a NEM day is 24 UTC hours

**Confirmed.** For 2026-09-24:

- All 576 records have `date = 2026-09-24`.
- The earliest `startTime` is `2026-09-23T14:00:01Z` and the latest `endTime` is
  `2026-09-24T14:00:00Z`.
- Flooring `startTime` gives exactly 24 distinct UTC hours, `2026-09-23T14:00Z` to
  `2026-09-24T13:00Z`.
- Every `startTime` has a seconds value of `:01`, on usage and on prices. Every
  `endTime` is `:00`.
- `nemTime` has offset `+10:00` and equals `endTime` as an instant, so it is the
  interval end.
- All 8 days in the 8-day fetch, plus 09-22, 09-23 and 09-25, had exactly 288 records
  per channel. Quality was `billable` throughout.
- Yesterday (09-25) was already complete and `billable` at 14:40 AEST.

### 4.5 What `cost` includes

**`cost` = `kwh × perKwh`, rounded to 4 decimal places, in cents.** The maximum
difference over 576 records was 5.0e-5.

**`perKwh` is the full retail per-kWh rate.** It is identical to the `/prices`
`ActualInterval` `perKwh` for all 576 intervals.

Within each network time-of-use period, 8 days of data fit exactly (residual ≤ 1e-5
c/kWh):

| Period (general, `tariffInformation`) | `perKwh` = |
|---|---|
| offPeak | 1.07145 × `spotPerKwh` + 16.1713 |
| solarSponge | 1.07145 × `spotPerKwh` + 8.2530 |
| peak | 1.07145 × `spotPerKwh` + 19.9885 |

So `cost` includes:
- the wholesale energy component: spot times a constant 1.07145, which looks like loss
  factors;
- the network time-of-use charge: the intercept changes by period;
- fixed per-kWh adders: market and environmental charges, and Amber's margin if any.

Amber documents `perKwh` as GST-inclusive. The data cannot confirm GST independently.
There is no daily supply charge or membership fee: the only record `type` is `Usage`,
and the only channel types are `general` and `feedIn`.

Feed-in: `perKwh` and `cost` are negative (earned). It is not a single linear fit
against spot (residual up to 4.1 c), which does not matter for us.

Other observations:
- `tariffInformation` is **absent** (not null) on feed-in records.
- Usage records also carry `type: "Usage"`.
- Channel identifiers are `E9`/`B9`, not `E1`/`B1`.

### 4.6 StatisticMetaData in HA 2026.9.3

Source: `homeassistant/components/recorder/models/statistics.py`.

```python
class StatisticMetaData(TypedDict):
    has_mean: NotRequired[bool]      # deprecated ("will be removed in 2026.4", still present)
    mean_type: StatisticMeanType     # IntEnum: NONE=0, ARITHMETIC=1, CIRCULAR=2
    has_sum: bool
    name: str | None
    source: str
    statistic_id: str
    unit_class: str | None           # unit converter class, or None
    unit_of_measurement: str | None
```

`async_add_external_statistics` (in `statistics.py`):
- It checks `valid_statistic_id`, and requires `source` to equal the ID's domain.
- A missing `mean_type` or `unit_class` calls `report_usage(...,
  breaks_in_ha_version="2026.11")`. In the test harness that raises `RuntimeError`.
  **Always pass both.**
- A non-None `unit_class` must name a known converter, and the unit must be one of its
  `VALID_UNITS`.
- Timestamps must be timezone-aware and exactly on the hour.

Values for our statistics:

| Unit | `unit_class` |
|---|---|
| `kWh` | `"energy"` |
| `AUD` | `None` |
| `AUD/kWh` | `None` |

Sums use `mean_type=NONE, has_sum=True`. The optional price series uses
`mean_type=ARITHMETIC, has_sum=False`.

Verified by tests with a real recorder (`test_statistics_schema.py`):
- All three metadata shapes import and read back field-for-field.
- 24 hourly rows read back with correct sums.
- **Re-importing the same hours overwrites the rows** (DESIGN section 7's re-check).
  After the second write, `state`, `sum` and `get_last_statistics` all show the new
  values, with 24 rows, not 48.
- Uppercase IDs, a mismatched source, a wrong unit for its `unit_class`, and a `:01`
  timestamp are all rejected. `valid_statistic_id` also rejects double underscores and
  a leading underscore in the object ID.

Minimum HA version: **2026.9 is sufficient.** Nothing requires newer.

### Fixtures saved (all in `tests/fixtures/`)

| File | Content |
|---|---|
| `sites.json` | The real response with `id` → `01FAKESITE0000000000000000`, `nmi` → `FAKENMI000`, `network` → `Example Network`, `activeFrom` → `2025-01-01` |
| `usage_2026-09-24.json` | **Synthetic since the follow-up session** (addendum A4). Originally the full real day, 576 records |
| `prices_2026-09-24.json` | The same day's prices |
| `error_range_too_large.txt` | The real 422 body |
| `error_forbidden.json` | The real 403 body for an invalid key |

An automated scan confirmed that none of the secret env var values, the real site ID
(any case) or the real NMI appear anywhere in the repository. Raw captures were kept
only in the session scratchpad and have been deleted.

## 5. Deviations and proposed design changes

Deviations and choices:

1. **`config_flow: false`** in the M1 manifest. With `true`, hassfest requires a
   `config_flow.py`, which is M2 work. It flips to `true` in M2.
2. **`CLAUDE.md` and `DESIGN.md` are left untracked.** `CLAUDE.md` contains lab
   infrastructure details (node name, VMIDs, environment layout) that do not belong in
   a public repository. `DESIGN.md` is probably fine to publish. Robert to decide.
3. **`amber-technical-reference.md` is not in the repository.** It is cited in DESIGN
   section 2 and CLAUDE.md. If it should be published, it needs adding. The README does
   not link to it yet.
4. **Fixture privacy.** The committed usage fixture is one real day of household kWh
   (2026-09-24), with the site ID and NMI removed. I also replaced the network and
   `activeFrom`, which CLAUDE.md does not require. If one real day of load profile is
   too much to publish, I can scale or synthesise it in M2 while keeping the
   `cost = kwh × perKwh` relationship.
5. **Tooling.** `uv` was not installed on `claude-dev`. I installed 0.12.19 to
   `~/.local/bin` with Astral's official installer, and Python 3.14.7 through uv.
6. **Configuration values printed in conversation output** (never in files or commits):
   - a Proxmox permissions dump printed the value of `$PVE_POOL`;
   - the VM 101 SSH check echoed `whoami`, the value of `$LAB101_SSH_USER`.

   Under the narrowed secret rule agreed afterwards, only `PVE_TOKEN_SECRET`,
   `HA_TEMPLATE_TOKEN`, `HA_LAB101_TOKEN` and `AMBER_TEST_API_KEY` are secrets. Both
   of these are configuration variables, so **neither was a violation**. No secret
   value was printed.

Proposed changes to DESIGN.md:

- **Section 4, facts.**
  - The range limit is `endDate − startDate ≤ 7`, for both usage and prices. Plan
    7-day windows.
  - An over-long range returns 422 with a plain-text body.
  - An invalid key returns **403** (JSON `message`), not 401. Treat both as auth
    failures (the client already does).
  - `startTime` offsets are always `:01`, so treat the 1-second offset as the rule,
    not an occasional quirk.
  - `tariffInformation` can be absent.
  - Real channel identifiers are not only `E1`/`B1` (this account has `E9`/`B9`).
- **Section 4, item 5 (cost).** Record the finding: `cost` = `kwh × perKwh` (4 dp,
  cents). `perKwh` is the all-in retail rate: energy, network TOU, market charges,
  GST-inclusive. No supply charge or fees. This means own-sensor cost (section 11) is
  comparable like-for-like with Amber's `cost`.
- **Section 9 (rate limiting).**
  - Budget per counter: sites plus usage on one counter, prices on another.
  - Read `RateLimit-Remaining` from the first response of each run, and stop early if
    something else is draining the shared counter. Here, something outside used about
    2 calls a minute.
  - "Target ≤ 25 calls per run" still fits: an 86-day recovery needs 13 usage calls.
- **Section 6.** Set `unit_class` to `"energy"` for kWh and `None` for AUD and
  AUD/kWh. Always pass `mean_type` and `unit_class` (omitting either breaks in
  2026.11). The minimum stays 2026.9.
- **Section 7.** The overwrite behaviour for external statistics is re-confirmed.

## 6. Live Amber API calls

**30 calls in total:**
- 29 with `AMBER_TEST_API_KEY`: 11 on counter A (sites and usage), 18 on counter B
  (prices).
- 1 with an obviously fake key, to capture the 403.

All were on 2026-09-26:
- 27 calls between 04:32:06 and 04:35:55 UTC (14:32 to 14:36 AEST)
- 2 calls at 04:40:21 UTC (14:40 AEST), an end-to-end check of `AmberClient` on real
  aiohttp

That is outside all production quiet windows. No call returned 429.

The highest use of one window was 16 calls on counter B. Remaining never went below 34.

## 7. Lab state left behind

- **No VMs created.** Pool members are unchanged (template only). The template was
  not modified.
- **VM 101:** read-only access only. No files written, no `.claude_work` folder.
- **`claude-dev`:**
  - a `known_hosts` entry for VM 101 in `~/.ssh/known_hosts`
  - `uv` in `~/.local/bin`
  - the repository `.venv/`, which is gitignored
  - the session scratchpad holds only the call log (no response bodies) and helper
    scripts
- **Git:** 6 new commits on `v2` at the time (including this report; hashes updated after the A9 rewrite), nothing pushed, `main` untouched. `CLAUDE.md` and
  `DESIGN.md` are untracked (item 5.2).

## 8. Recommended next steps

1. **Robert: fix the Proxmox permission** so linked clones can be created. Either:
   - add storage `local-lvm` as a member of the pool (Datacenter → Pools → *pool* →
     Members → Add → Storage), so the existing pool-level `Datastore.AllocateSpace`
     applies; or
   - grant the token `Datastore.AllocateSpace` (the `PVEDatastoreUser` role) on
     `/storage/local-lvm`.

   Proxmox 8 and later may also check `SDN.Use` on `/sdn/zones/localnetwork/vmbr0` for
   the cloned NIC. If the retry fails on that, grant the `PVESDNUser` role there. I
   will then run the smoke test: clone to 9100, get the IP from the guest agent, check
   HA on port 80 with the template token, check SSH, destroy. I'll append the evidence
   to this report.
2. **Robert:** answer the rate-limit questions in 4.3, and if possible issue a second
   key on the same account so the per-key versus per-account question can be settled.
3. **Robert:** decide on committing `DESIGN.md` and `CLAUDE.md`, on adding
   `amber-technical-reference.md`, and on the fixture privacy question in 5.4.
4. **Robert:** carry the section 5 design changes to the planning chat.
5. When GitHub access is available: push `v2`, check the first hassfest, HACS and test
   runs, and set the repository description and topics that HACS needs.
6. Then Milestone 2 (config flow, statistics model, one-day import service), after
   sign-off.

---

## Addendum: follow-up session (2026-09-26, 15:25 to 15:45 AEST)

This session applied the planning-chat decisions that followed the first report.

### A0. Commits added in this session

| Commit | Content |
|---|---|
| `e0f3cee` | `DESIGN.md` moved to `docs/DESIGN.md` and committed unchanged |
| `98476bd` | All accepted section 5 changes applied to `docs/DESIGN.md`, plus a note that the technical reference is private and not in the repository |
| `84866fb` | `RunBudget`: per-run rate-limit budget in the client (A3). Ruff now skips `reports/` and `docs/` |
| *(removed)* | Synthetic usage fixture replaces the real one (A4). Dropped by the history rewrite (A9): the fixture is now synthetic from its first commit, so this commit became empty |
| `78aad33` | Report update |

Hashes in this report are post-rewrite (A9). The first-session report commit is
`552c607`.

Local-only changes (not committed):
- **`CLAUDE.md`:**
  - the narrowed secret rule;
  - the two new clone rules: check `local-lvm` free space and abort under 20 %, and
    never more than 2 clones at once;
  - the DESIGN path changed to `docs/DESIGN.md`;
  - the technical reference marked private.
- **`.git/info/exclude`** now lists `CLAUDE.md`, `amber-technical-reference.md` and
  `local/`. `.gitignore` is unchanged.

Tests: **67 passed, 0 skipped**, with 100 % coverage (268 statements). `ruff check`
and `ruff format --check` are clean.

### A1. Lab smoke test: PASSED

Script output from the evidence log. Times are UTC; 05:31 UTC is 15:31 AEST. The
template VMID and the SSH user are shown as placeholders, since configuration values
stay out of committed files.

```
05:31:53 preflight: local-lvm total=1710.0GiB used=137.5GiB avail=1572.5GiB free=92.0%
05:31:53 preflight: existing clones in 9100-9199: []
05:31:53 clone: <template> -> 9100 (amber-test-smoke), linked, pool member
05:31:55 clone: task finished exitstatus=OK in 2s
05:31:55 clone config: name=amber-test-smoke scsi0=local-lvm:vm-9100-disk-1 net0 bridge=['bridge=vmbr0']
05:31:57 start: task finished exitstatus=OK in 2s
05:32:11 guest agent: interface enp6s18 ipv4 192.168.94.121 after 14s
05:32:27 HA http://192.168.94.121/api/ -> {'message': 'API running.'} after 16s
05:32:27 HA /api/config: version=2026.9.3 time_zone=Australia/Sydney state=NOT_RUNNING unit_system=km
05:32:27 HA bad-token check: HTTP 401 (rejected as expected)
05:32:27 SSH <TEMPLATE_SSH_USER>@192.168.94.121: rc=0 output: user=<TEMPLATE_SSH_USER> | PRETTY_NAME="Alpine Linux v3.24"
05:32:27 RESULT: smoke test PASSED
05:32:29 stop: task finished exitstatus=OK in 2s
05:32:31 destroy: task finished exitstatus=OK in 2s
05:32:31 destroy: VM 9100 present after delete: False; clones now: []
```

- The two new CLAUDE.md rules ran as preflight checks before the clone:
  - free space on `local-lvm` was 92.0 % (the abort threshold is under 20 %);
  - 0 clones already existed (the limit is 2).
- The clone is a linked clone on `local-lvm`, with its NIC on `vmbr0`. The new
  `PVESDNUser` grant was enough; no other permission error came up.
- HA answered on **port 80** with `$HA_TEMPLATE_TOKEN` 16 s after the IP appeared. It
  rejected a bad token with 401.
- The template runs **HA 2026.9.3**, the same version as the test harness pin, with
  time zone Australia/Sydney.
- Observation: at +16 s HA reported `state=NOT_RUNNING`, meaning startup was not yet
  finished. From M2, lab scripts should wait for `state=RUNNING` before installing or
  testing anything.
- SSH with `$LAB_SSH_KEY` works (into the SSH add-on container). The command was run
  with `UserKnownHostsFile=/dev/null`, so this local machine's `known_hosts` file was
  not changed.
- **After the test: 0 clones exist.** The pool contains the template and the
  `local-lvm` storage only.

### A2. Secret rule

CLAUDE.md now names four secrets: `PVE_TOKEN_SECRET`, `HA_TEMPLATE_TOKEN`,
`HA_LAB101_TOKEN` and `AMBER_TEST_API_KEY`. Other variables are configuration: they
may appear in command output, but never in committed files. The two items in section
5.6 were therefore not violations. This session printed configuration values only:
the clone IP and the SSH user in the smoke log. None of them is in a committed file:
- in this report, the template VMID and the SSH user appear as placeholders;
- a pre-commit scan checks all committed files for the values of all 13 variables.

### A3. Rate limit: shared-budget handling (decision 5)

The client now has `RunBudget`. A run starts with `client.start_run(RunBudget(...))`
and ends with `client.end_run()`.
- **Two counters**, matching the observed API behaviour: `sites_usage` (for `/sites`
  and `/usage`) and `prices`.
- **Per-run cap:** `max_calls_per_counter` (default 25). A request over the cap is
  refused before it is sent.
- **Reserve:** after every response, the budget records `RateLimit-Remaining` for that
  counter. If it is below `reserve` (default 15) and the window's `RateLimit-Reset`
  has not yet elapsed, the next request on that counter is refused, again without
  being sent. The first response of a run is therefore the start-of-run check.
- A refusal raises `AmberBudgetExhaustedError`, with `.counter` naming the counter.
  M3 treats this as "stop, resume at the next attempt", not as a failure.
- Transport failures still count against the cap. A 429 that reports `Remaining: 0`
  also blocks the counter.
- 10 new tests cover:
  - no budget means no limit;
  - the first response below the reserve stops the run;
  - the reserve boundary (a Remaining equal to the reserve still allows the next call);
  - counters are independent;
  - the per-counter cap;
  - a window reset clears a stale Remaining;
  - failed requests count;
  - a 429 updates the budget;
  - restarting gives fresh counts;
  - headers without Remaining leave the budget unchanged.
- The retention probe in A5 used this mechanism (`RunBudget(max_calls_per_counter=12,
  reserve=15)`).
- DESIGN section 9 describes it under the name `RunBudget`.

### A4. Fixtures: synthetic usage data (decision 6)

`tests/fixtures/usage_2026-09-24.json` is now synthetic:
- **Kept from the recording:** the same 576 records in the same order, with identical
  field sets and field order, and every field other than `kwh` and `cost` unchanged
  (timestamps, `perKwh`, `spotPerKwh`, `renewables`, `descriptor`, `spikeStatus`,
  `tariffInformation`, `quality`). These are market data, not household data.
- **Invented kWh.** The generator was seeded, so the output is reproducible. The profile:
  - base load 0.35 kW;
  - morning and evening peaks;
  - a lunchtime bump;
  - random ±20 % variation and occasional appliance spikes;
  - a 5 kW-peak solar curve from 06:15 to 17:45, which gives feed-in by day and
    suppresses import around midday.
- Three-decimal kWh values, `cost = round(kwh × perKwh, 4)`, and zeros encoded as JSON
  integers `0`, exactly as in the real data.
- Day totals: general 12.190 kWh, 387.60 c; feed-in 24.278 kWh, −102.98 c.
- Checked by script: structure identical record for record, and the cost rule holds
  within 5e-5 everywhere. All tests pass unchanged.
- The real sample and the generator live only in `local/`, which is excluded via
  `.git/info/exclude`.

**Git history: resolved (A9).** At the time of this addendum, the real day was still in
the client commit and the history after it. I tried to rewrite
the unpushed `v2` commits (`git filter-branch`, limited to `main..v2`) so that the file
is synthetic from its first commit. Claude Code's permission system blocked that, and I
did not look for another way to do it. Options:
- **Rewrite before the first push** (recommended). If you run it or approve it, the
  command replaces the file's contents in every `main..v2` commit that contains it.
  Commit hashes from `b253941` onwards will change, and this report's hash tables will
  need updating.
- **Accept it in history.** It is one day of household load with the site ID and NMI
  removed.

Robert approved the rewrite after this addendum was written; see A9.

### A5. Usage retention probe (separate from price history)

One `/sites` call served as the start-of-run Remaining check, followed by 7 single-day
usage requests: 8 calls in total, within the limit of 12. They ran at 15:33 AEST, on
2026-09-26.

```
05:33:15 /sites (start-of-run check)  Remaining=42 Reset=115
05:33:15 2026-07-10 (today-78)  records=576  E9=288 B9=288  billable=576  Remaining=41
05:33:17 2026-06-20 (today-98)  records=0                                 Remaining=40
05:33:20 2026-06-30 (today-88)  records=576  E9=288 B9=288  billable=576  Remaining=39
05:33:22 2026-06-25 (today-93)  records=0                                 Remaining=38
05:33:24 2026-06-27 (today-91)  records=0                                 Remaining=37
05:33:27 2026-06-28 (today-90)  records=0                                 Remaining=36
05:33:29 2026-06-29 (today-89)  records=576  E9=288 B9=288  billable=576  Remaining=35
```

**Result: the earliest date with usage data is 2026-06-29, 89 days before 2026-09-26.**
2026-06-28 (90 days back) and every earlier date probed returned `200 []`. The boundary
day itself was complete: 576 records, all `billable`, not partial.

Remaining never went below 35, so the reserve of 15 was never approached.

This does not match the 86 days in DESIGN section 4. As instructed, I have not changed
the retention design. Points for the planning chat:
- The technical reference says the production rebuild covered "86 days (2026-06-29
  through yesterday relative to the rebuild date)". That puts the rebuild at about
  2026-09-23. So 2026-06-29 was the start of that window then, and is still the
  earliest available date today, 3 days later.
- Two readings fit:
  - that rebuild's start was set by the configured `retention_days = 86`, not by
    Amber's real boundary, so the boundary may already have been older than 86 days;
  - or the boundary has not moved since.
- The data I have cannot tell these apart. Re-probing 2026-06-28 and 2026-06-29 on a
  later day (2 calls) would show whether the boundary rolls.

Price history (section 4.2) is separate: it goes back to 2025-03-01.

### A6. Live Amber API calls (running total)

This session: **8 calls** at 05:33:15 to 05:33:29 UTC (15:33 AEST), on counter
`sites_usage` only. None returned 429.

Milestone total: **38 calls**, of which 37 used `AMBER_TEST_API_KEY` and 1 used a fake
key. All were outside production quiet windows.

### A7. Lab state now

- **VMs:** no clones exist. VM 9100 was created and destroyed. The template was not
  modified. VM 101 was not touched in this session.
- **Proxmox pool:** the template plus storage `local-lvm`, the membership Robert added.
- **`claude-dev`:** the `local/` folder (excluded) holds the real usage sample
  and the synthetic generator. The session scratchpad holds call
  and smoke logs plus helper scripts, and no response bodies.
- **Git:** 10 commits on `v2` after the A9 rewrite (the fixture-replacement commit was dropped), nothing pushed, `main`
  untouched. `CLAUDE.md` and `amber-technical-reference.md` are excluded through
  `.git/info/exclude`.

### A8. Next steps

1. **Robert:** decide on the history rewrite in A4 before the first push.
2. **Robert:** take the usage retention finding in A5 to the planning chat. A 2-call
   re-probe on a later day would show whether the boundary rolls.
3. **Robert:** Milestone 1 sign-off. Then M2 (config flow, statistics model, one-day
   import service, verified on a lab clone), with no work started until then.
4. When GitHub access is available: push, check the first CI runs, and set the
   repository description and topics.

### A9. History rewrite (approved after sign-off)

Robert approved the rewrite once Milestone 1 was signed off. I ran:

```
git filter-branch --prune-empty --index-filter \
  'if the tree contains tests/fixtures/usage_2026-09-24.json, set it to the synthetic blob be1115e' \
  -- main..v2
```

Old to new hashes:

| Old | New | Commit |
|---|---|---|
| `88dabd9`, `0076035`, `b461bea` | unchanged | before the fixture existed |
| `b253941` | `3842117` | Async Amber API client with tests |
| `9ddb76c` | `2f99b30` | Statistics contract tests |
| `0cd05ae` | `552c607` | Milestone 1 report |
| `1cebcec` | `e0f3cee` | docs/DESIGN.md |
| `77f320e` | `98476bd` | Design findings |
| `375ab11` | `84866fb` | RunBudget |
| `d61fa2c` | dropped | became empty (`--prune-empty`) |
| `005f6e9` | `78aad33` | Follow-up report |

Verification:
- **No object reachable from `v2` is the real blob:**
  `git rev-list --objects v2 | grep -c 2d1bb11…` gives `0`.
- I then deleted the backup ref `refs/original/refs/heads/v2`. After that, no ref at
  all reaches the real blob (`--all` also gives `0`).
- Every one of the 7 commits containing the file holds the synthetic blob `be1115e`.
- `main` is unchanged at `cc179b7`.
- The rewritten client commit `3842117`, checked out in a temporary worktree, passes
  its own tests (51 passed).
- The unreachable old objects remain in the local object store until git's normal
  garbage collection. They can never be pushed, because no ref points to them.
