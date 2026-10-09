# Milestone 13 report: 2.1.0-rc2

Date: 2026-10-09. Branch `feature/v2.1`, pushed, with the annotated tag **`v2.1.0-rc2`**
on its head. `main` was not touched.

**Summary.**
- **Retention self-heal** (the forum report): the site's start date is a lower bound; a
  fallback boundary is provisional and rediscovered once a day; and a run of empty days at
  the very start of the history is skipped at once when later days have data. **The forum
  case recovers at the next run, with no user action** (a test reproduces rc1's stored
  state).
- **Bill estimate fields, to your final spec (sent mid-milestone):**
  - billing day;
  - **daily supply charge** and **Amber subscription**: no default, must be entered, and 0
    is valid for a free subscription;
  - **other daily charges** (default 0);
  - **GST** (default 10).

  Your exact help texts are on every field, and the step links to "Finding these on your
  bill". Existing options are migrated (0.7019 + 0.3852 → **1.0871**; 0.7471 → Amber
  subscription). The figures are unchanged: **$168.96** without and **$165.71** with the
  allowance, for the September cycle (a test and the lab).
- INSTALL.md has "Finding these on your bill", with image placeholders.
- **9102 lab check passed:** the options migrated with identical figures, the self-heal did
  not fire, and all 17,136 rows are unchanged.
- **484 tests pass, with 100 % coverage**, and ruff is clean. **CI on `3daf490`: Tests,
  hassfest and HACS validation all succeeded.** hassfest had failed on `6d3677b` (a URL in a
  translation string); fixed in `35937fb`.

## 1. What was built

| Commit | What |
|---|---|
| `6f181c8` | Retention self-heal and the site's start date; bill charges as on the Amber bill (with the options migration) |
| `6d3677b` | 2.1.0-rc2: manifest, CHANGELOG, DESIGN sections 8 and 19, "Finding these on your bill" |
| `35937fb` | The step's help link as a description placeholder (**hassfest failed on `6d3677b`**: translation strings may not contain URLs) |
| `3daf490` | Bill fields to the final spec: help texts, no defaults for daily supply and subscription (0 valid), other 0, GST 10; tests for 0 subscription and the September figures; docs |
| (this commit, tagged `v2.1.0-rc2`) | Release notes and this report |

### 1.1 Retention self-heal (`manager.py`, `__init__.py`, `storage.py`)

**The forum case**, as I read it:
- The site's `activeFrom` was recent, but the start day itself had no usage. Amber's start
  date can precede the first metered day.
- So rc1's discovery found no data at `activeFrom`, returned "not bracketed", and fell back
  to today − 89, before the customer joined.
- The walk then waited with 7-day patience on each empty pre-join day, one at a time.
- `probe_retention` fixed it for them only because its timing differed.

**(b) The site's start date.**
- The API field is **`activeFrom`** (and **`closedOn`**), confirmed in the recorded
  `/sites` fixture and already parsed. On 9102: active from 2025-07-29.
- It is now a lower bound:
  - `_store_retention` raises any boundary to it (recorded as `site_start_applied`);
  - the walk never starts before it, which also covers boundaries stored by rc1.
- If the start day has no usage, discovery uses the start date ("site start date (no usage
  on it)") instead of falling back.
- Setup records `{active_from, closed_on}` in the Store (`site`, so in diagnostics) and logs
  it when it changes.
- `closedOn` is recorded and shown only. No behaviour depends on it yet: a closed site
  can't be set up, and an entry whose site closes keeps waiting as before.

**(a) Provisional fallback.**
- Any fallback (discovery not bracketed, call cap, API error, rate-limit budget or auth)
  is stored with `provisional: true` and `needs_discovery: true`.
- Discovery runs again on the next run, **at most once a day**: a provisional record
  measured today is not retried until tomorrow.
- A successful discovery, or the self-heal, replaces it. A failed discovery on a young site
  uses the start date but stays provisional: "fallback (…), from the site start date".

**(c) Leading empty run.**
- While nothing has been imported (`last_written` is None), empty days at the start of the
  walk are collected without patience.
- At the first day with data:
  - they are marked `skipped_unavailable` ("before the first day with usage data") at
    once;
  - the boundary moves to that day;
  - the retention record gets `self_heal` (when, from, to, days, the previous method), and
    a warning is logged ("Retention self-heal: …").
- If no day has data up to yesterday, the run waits on the first empty day, skipping
  nothing.
- Interior gaps (after something was imported) keep the patience rule.

**On (a)'s wording, "probe the newest available day, then run discovery again":** the walk
has already fetched those days, so the first day with data is known without another probe.
Moving the boundary there is exactly what a discovery would find, at no extra calls. A
separate automatic discovery runs for provisional (fallback) boundaries, once a day.

### 1.2 Bill estimate charges (`bill.py`, `config_flow.py`, `__init__.py`, strings)

Built to your final specification, which arrived mid-milestone and replaced item 2's
original text.

- `CHARGES` = `daily_supply`, `amber_subscription`, `other_daily`.
- **Help text (data_description), verbatim from your spec:**
  - **billing day:** "The first day of your bill period (for '28 Aug – 27 Sep', enter
    28).";
  - **daily supply charge:** "The Network Daily Supply Charges rate in your bill's charges
    summary. It already includes metering; don't add the metering line separately.";
  - **Amber subscription:** "From your bill's Amber Fees section. Enter 0 if your
    subscription is currently free (for example a first-year offer), and update it when
    the offer ends.";
  - other daily charges and GST have their own help text too.

  Labels carry "($/day, excluding GST)".
- **Defaults:** none for the daily supply charge and the subscription. With a billing day
  set, both must be entered (error "Enter this charge (0 is fine if it is currently
  free)."), and **0 is accepted**. Other daily charges are suggested as 0, and GST as 10.
- The step description links to INSTALL.md's "Finding these on your bill" through a
  description placeholder (`bill_help_url`), because hassfest forbids URLs in translation
  strings.
- **Migration:** config entry **1.1 → 1.2** (`async_migrate_entry`, `migrate_charges`):
  network + metering → `daily_supply`; subscription → `amber_subscription`; other is kept.
  The fixed-charges statistic's schedule compares the total daily amount, which is
  unchanged, so nothing is rewritten.

### 1.3 Docs

- **INSTALL.md**, a new subsection "Finding these on your bill", matching the help texts:
  - the billing day ("28 Aug – 27 Sep" → 28);
  - the Network Daily Supply Charges rate (includes metering; don't add it separately;
    excluding GST);
  - the Amber subscription from the Amber Fees (0 if currently free; update when the
    offer ends);
  - other daily charges (default 0);
  - GST (default 10);
  - one-off charges not included;
  - and two **marked image placeholders**: HTML comments naming
    `docs/images/bill-summary.png` and `docs/images/bill-amber-fees.png`, and what each
    should show.

  The bill estimate subsection and the FAQ use the new names.
- **README:** the new field names, with a link.
- **DESIGN:** section 8 (self-heal and start date); section 19 (fields and migration).
- **CHANGELOG:** `[2.1.0-rc2]`, plus a note on rc1's field names.
- **Release notes:** `reports/release-notes-v2.1.0-rc2.md`.

## 2. Test results

`uv run pytest --cov=custom_components.amber_energy_dashboard`: **484 passed**, 0 skipped
(409 s). Every file is at 100 %. That is 470 at rc1, plus 14 (and one test changed):

| File | New | What |
|---|---:|---|
| `test_retention_heal.py` | 8 | **A customer who started 30 days ago:** discovery at the start date, 30 days imported in one run, ≤ 7 calls. **A start day without usage:** 3 leading days skipped at once, boundary moved, self-heal recorded and logged. **The stuck forum install** (rc1's fallback record, nothing imported, no start date in the API): **recovers at the next run**, 30 days imported, 59 days skipped. An old boundary before the start date is bounded (nothing older fetched). **A provisional fallback:** not rediscovered again the same day, rediscovered the next day (bisection, real boundary). A failed discovery on a young site: start date, provisional; a budget stop stores the fallback raised to the start date. **An interior gap still waits with patience.** Nothing published yet: waits from the start date, skipping nothing |
| `test_bill.py` | +6 | **rc1 options migrate** (0.7019 + 0.3852 → 1.0871; 0.7471; other kept; minor version 2), with the sensor lines equal; `migrate_charges` edge cases; a newer entry version is refused. **0 subscription:** no charge defaults suggested (only other 0 and GST 10); a missing subscription is refused with a billing day; 0 is saved, and the estimate has no subscription line. **September acceptance** ×2 (migrated fields; day totals adding up to the real cycle's): **$168.96** without and **$165.71** with the allowance; lines daily supply 37.0701, subscription 25.4761 |
| `test_manager.py` | changed | `test_young_site_without_data_starts_at_its_start_date`: it pinned the old fallback, which was the bug |
| `test_bill.py`, `test_allowance.py` | changed | the charges use the new fields; the figures are unchanged |

## 3. Lab check on VM 9102 (12:27 to 12:29 AEDT)

This was outside the quiet window, and more than 45 minutes before 9102's 13:15 and
production's 13:22.

| Step | Evidence |
|---|---|
| Snapshot | **`pre-rc2`** (the M11 code), task `OK`. **Kept** until your sign-off |
| Before | Entry 1.1. Options: network 0.7019, metering 0.3852, subscription 0.7471, other 0.0, GST 10, billing day 28. September `bill_estimate`: **165.71** (with the allowance), lines network 23.9348, metering 13.1353, subscription 25.4761. Current cycle: 57.95, projected 158.05. Retention `verified`, boundary 2026-07-13 |
| Install | `6d3677b`, sha256 of all 27 files matched; restart → `RUNNING` |
| **Options migrated** | Entry **1.2**. Options: **`daily_supply` 1.0871**, **`amber_subscription` 0.7471**, `other_daily` 0.0; the old keys are gone |
| **Figures identical** | September: **165.71**, projected 165.71; lines `daily_supply` **37.0701** (= 23.9348 + 13.1353), `amber_subscription` 25.4761; fixed total identical. Current cycle: 57.95 / 158.05, identical |
| **Self-heal does not fire** | `run_now`: `caught_up`, **0 calls**, no `skipped_leading`, no rediscovery. The retention record is unchanged (`verified`, last verified today; `self_heal` none) |
| Store | `site` = {active_from **2025-07-29**, closed_on null} |
| **Statistics** | **All 17,136 rows identical** (7 statistics × 2448) |
| Logs and Repairs | no integration warnings or errors; no issues |

**Reinstalls** (`35937fb` at 12:37, then the final tree, see below): each time the sha256
of all files matched. The bill step showed the new fields with the migrated values, and
the help link was filled in. September stayed **$165.71**, with no warnings.

The $168.96 figure (without the allowance) is covered by the tests. 9102 has the allowance
on, and the line arithmetic is identical (1.0871 × 31 × 1.1 + 0.7471 × 31 × 1.1 =
62.5462).

## 4. Deviations and points for the planning chat

1. **(a) is implemented through (c) and the provisional fallback**, not as a separate probe
   followed by discovery (section 1.1). It's the same result with no extra calls. Say if
   you want the explicit probe-then-discover path as well.
2. **`closedOn` is recorded, not acted on.**
3. **The skipped leading days are marked `skipped_unavailable`.** If Amber later publishes
   them (unlikely: they precede the first metered day), a `backfill` imports them.
4. **The options-step link points to INSTALL.md on `main`**, so it resolves once 2.1.0 is
   merged; until then it shows 2.0.1's guide.
5. Image placeholders: `docs/images/bill-summary.png` and `bill-amber-fees.png` are to be
   supplied.

## 5. Final release step for 2.1.0 (prepared, not applied)

1. Manifest: `2.1.0`.
2. CHANGELOG: one `## [2.1.0] - 2026-10-10` entry (the rc1 and rc2 entries merged into
   "since 2.0.1", with rc2's changes folded in); links updated.
3. README banner: "**Version 2.1.0.**" (drop "release candidate"); the HACS-icon and NEM
   FAQ entries stay.
4. `reports/release-notes-v2.1.0.md`: what's new, fixed (the 2.0.x own-sensor sensors,
   the retention self-heal), who should update, how to turn on the new features, known
   limitations. Links at `blob/v2.1.0/...`.
5. CI green on the release commit.
6. Merge: `main` is at `v2.0.1` (`b512d13`), an ancestor of `feature/v2.1`, so
   `git merge --ff-only feature/v2.1` on `main`, then fast-forward `v2` to `main`.
7. Tag `v2.1.0` (annotated, "Amber Energy Dashboard 2.1.0") on `main`'s head. Push `main`,
   `v2` and the tag. Confirm CI on `main`. Delete `feature/v2.1` after the merge, if you
   want that.
8. Lab: install 2.1.0 on 9102 (the checksum check), then delete snapshot `pre-rc2`. You
   create the GitHub release (there is no `gh` here).

## 6. Live Amber API calls

**3**, all `/sites` at 9102 restarts on 2026-10-09:
- 12:28 (the upgrade);
- 12:37 (`35937fb`);
- 12:53 (`3daf490`, the tagged code).

The `run_now` and the `bill_estimate` actions made 0. All were outside the quiet window
and before 9102's 13:15 and production's 13:22 runs.

## 7. Lab state left behind

- **VM 9102** is running **2.1.0-rc2** (the tagged tree), with the bill estimate (migrated
  fields), the fixed-charges statistic and the export allowance. **Snapshot `pre-rc2`** is
  kept until your sign-off.
- No other clones. VM 101 was not accessed. No scratch files on any HA instance.
- Repository: `feature/v2.1` and the tag `v2.1.0-rc2` are pushed. `main` and `v2` are at
  `v2.0.1`.

## 8. Recommended next steps

1. Review rc2, and supply the two bill images.
2. Optionally, the forum user tries rc2 (or simply 2.1.0).
3. Tomorrow: the final release step in section 5.
