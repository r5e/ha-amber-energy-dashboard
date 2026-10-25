# Milestone 22 report: release candidate preparation for 2.2.0

Date: 2026-10-25 (Sydney). Branch `feature/v2.2`, after the 2.2.0b4 beta (`27bba71`).
Pushed. **Not tagged**: the tag waits for one decision from production data (whether to
apply the calibration offset to Today so far). **Stopped for review.**

**Summary.**
- **Screenshots:** all 21 supplied images, plus the new `rename-site.png`, are in
  `docs/images/`. Every `TODO(screenshot)` placeholder is replaced, and every image is
  shown at half its width. `15-integration-page.png` is deleted. All 34 image links
  resolve, and no image is unreferenced. OCR and a visual check found no identifying
  text.
- **Polish (item 2):**
  - **Choose a site** now shows "NMI …1234 (network)".
  - Money sensors no longer read −0.00.
  - **Bill card:** money is formatted as "−$5.37", and the previous-bill table is aligned.
  - The **migration screens** use friendly names, a readable before/after and a re-base
    sentence.
  - A sweep found **two more leaks of the site ID**, now fixed: error messages and the
    Energy dashboard Repairs flow.
- **DESIGN:**
  - milestone 22 is recorded;
  - a **2.3 roadmap** lists the battery cost basis and a built-in "Amber bill" card.
- **Release files:**
  - manifest `2.2.0rc1`;
  - CHANGELOG: b4 merged into one `[2.2.0] - Unreleased` entry;
  - `reports/release-notes-v2.2.0rc1.md`.
- **Tests:** 616 pass, 100 % coverage, ruff clean.
- **CI:** green on `70fbccd`, after a lint fix (section 2).
- **Lab (9102):** 2a, 2b, 2d and 2e were confirmed on the rc1 head. 2b was first
  reproduced on the old code.

## 1. What was built

| Commit | What |
|---|---|
| `586a28e` | Docs: the 2.2 screenshots (22 images in, 1 out, placeholders replaced) |
| `0cba830` | Choose a site: "NMI …<last 4> (<network>)" |
| `8a8bd71` | Monetary sensors: an amount that shows as zero is 0.0 |
| `20ad99b` | Bill card: money format and previous-bill alignment |
| `96580e5` | Migration screens: friendly statistic names, readable dashboard change, re-base sentence |
| `9dbf134` | Messages never show the site ID (errors, API paths, Energy Repairs flow) |
| `a813bf5` | DESIGN: milestone 22 and the 2.3 roadmap |
| `3fa5787` | 2.2.0rc1: manifest, CHANGELOG, release notes (rc1, and the 2.2.0 notes updated) |
| `70fbccd` | Tests: raw string for a `match=` pattern (ruff RUF043, see section 2) |
| (this) | This report |

### 1.1 Screenshots (item 1)

- **Source:** `screenshots.zip` held four nested zips plus `convert-own-sensors.png`,
  22 PNGs in all. They were extracted with Python's `zipfile`, into an empty scratch
  folder (there is no `unzip` on claude-dev).
- **Names:** all matched the M21 list. `17-import-settings.png` replaced the old file of
  the same name.
- **`rename-site.png`** is new. It is under "Renaming the site, areas and labels"
  (INSTALL section 4), after the first paragraph.
- **Widths:** every `<img>` in INSTALL.md, MIGRATION.md and cards/README.md now has
  `width` = round(pixel width / 2), including the older images (01–09, 11, the bill and
  allowance steps) for consistency.
  - Before, the older ones were shown at about 40–50 %. 01, 03 and 06 now display at
    796–874 px, which GitHub scales to the column.
- **Placeholders:**
  - The retake comments were removed.
  - The "to supply" comments and their "*(Screenshot to follow.)*" lines became `<img>`
    tags with alt text.
  - No `TODO(screenshot)` or "Screenshot to follow" remains in the repository's docs.
- **Link check** (a script over every non-legacy, non-report `.md`): 34 references, all
  resolve; 34 files in `docs/images/`, none unreferenced.
- **Privacy check before committing:**
  - **OCR:** `rapidocr-onnxruntime`, installed in a scratch venv; no sudo for apt
    tesseract. Then a pattern scan of the text:
    - ULID-like site IDs;
    - 10+ digit runs;
    - e-mail addresses;
    - the maintainer's names and email domain, `duckdns`, `nuclab`, `192.168`, `.local`;
    - NMI tails other than 1234.

    **0 hits.** The only NMI text is "…1234". The statistic IDs in 18 and 19 are blurred.
  - **Visual check:** I viewed images 10, 18, 19, integration-layout,
    convert-own-sensors, device-page, sources-step, site-name-step, rename-site,
    new-sensors and both bill-card images. The rest were checked by their OCR text.
  - **Embedded metadata:** none of the new PNGs carries text chunks.
  - After the lab check, the lab's real NMI tail was also searched for: 0 hits in the
    OCR text and in the outgoing diff.
- `~/screenshots` was deleted after the commit, as were the scratch copies of the zips.

### 1.2 Polish (item 2)

- **2a. Choose a site.**
  - `config_flow.async_step_site` labelled each site `NMI <full NMI>`.
  - A new `sites.site_choice_label(nmi, network)` now gives "NMI …<last 4> (<network>)".
  - It is used by both setup and **Add site**, which already masked the NMI.
- **2b. Negative zero.**
  - `AmberSensor.native_value`: a monetary state whose absolute value is below half a
    unit of its display precision (0.005 for every money sensor) is returned as `0.0`.
  - Energy and other sensors are untouched.
  - This covers every monetary sensor: they all go through `AmberSensor`.
- **2c. Bill card** (`docs/cards/bill-card.yaml`).
  - Every amount goes through one helper, repeated in each template because button-card
    templates are separate functions: it rounds to cents, then writes `−$X.XX` (U+2212)
    for negatives and never "−$0.00".
  - Previous-bill table: `<td style="text-align:left">` for labels and right alignment
    for amounts. button-card centres text by default.
- **2d/2e. Migration screens** (`migration.py`).
  - **Labels:**
    - `_labels()` maps each target statistic ID to a label, "<role> (<site name>)", for
      example "Grid import energy (Home)". The site name is the site sub-entry's title.
    - It also maps each ID to the statistic's picker name.
    - The labels are stored in the plan and in the report as `labels`.
  - **Dry run** (`format_report`): role lines, "Copy … rows into …", and the Energy
    dashboard **before / After** as a list. For example:
    - "Grid consumption: sensor.amber_energy_import, no cost"
    - "Return to grid: Grid export energy (Home), compensation from Grid export
      compensation (Home)"

    Prices read "at the price in <entity>" or "at a fixed price of N per kWh". With
    several grid connections, each line is prefixed "Grid connection N:".
  - **Result** (`format_result`): copy lines use labels.
  - **Re-base** (`_rebase_text`) becomes a sentence:
    - "Re-based 90 days from stored hourly amounts, with no API calls.";
    - "Re-base not needed (already continuous).";
    - older records with API calls give "with N API calls".
  - **Manual instructions** (Energy dashboard not changeable automatically) name the
    statistics as the Energy dashboard's pickers show them (for example "Amber general
    E1 energy"), so a user can find them.
  - **What keeps the raw IDs:**
    - `targets`, `copy` and the stored migration record: the `migrate_v1` service
      response, diagnostics and the log;
    - the dashboard settings in `energy.before/after`.
- **2f. Sweep for other identifiers.** Checked:
  - every placeholder in `strings.json`, and what fills it;
  - every f-string embedding `sid`, `statistic_id`, `site_id`, `nmi` or a request path;
  - service errors and Repairs details.

  Fixed:
  - **Error messages with statistic IDs.** `importer` (7 messages), `manager` (2) and
    `migration` (2) build errors that reach the user: Repairs `import_failed` and
    `marker_mismatch` details, the Import status sensor's reason, and service errors.
    - These now use `statistics.statistic_text(sid)`, which gives "statistic e1_energy":
      the site ID is dropped, and the suffix still identifies the series.
    - Legacy IDs (`sensor.*`) are unchanged.
  - **API error messages carried the request path `/sites/<site ID>/usage`.** These
    reach the same places, through `HomeAssistantError(f"Amber API error: {err}")` and
    others. `api._shown(path)` now gives `/sites/…/usage`.
  - **The Energy dashboard Repairs flow** ("Add to the Energy dashboard" and "Choose the
    statistics") listed statistic IDs. `describe_sources(sources, names)` now uses the
    statistics' picker names.

  Left as they are, all harmless:
  - the entry and site titles (user-chosen, or "Site …1234");
  - device model "<network> · NMI …1234";
  - channel identifiers;
  - legacy kit entity IDs;
  - chain keys in marker messages. These hold Home Assistant sub-entry IDs, not Amber
    identifiers.

### 1.3 Tests added or changed

- `tests/test_sensor_values.py` (new): money near-zero cases (−0.0, −0.0001, ±0.0049 →
  0.0; −0.005 and −5.37 kept), with energy, `None` and `int` passed through. 4 of its
  tests fail without the fix.
- `test_config_flow.py`: the site picker labels are now "NMI …I000 (Example Network)" and
  "NMI …I001".
- `test_allowance.py`: Export charge after allowance's state is the string `"0.0"`.
- `test_migration.py`:
  - **the flow's dry run and result:** exact before/after list lines, labels, the re-base
    sentence, no `01fakesite` and no `{`;
  - the manual instructions by picker name;
  - unit tests for `_stat_text`, `_energy_lines` (entity/fixed price, several
    connections) and `_rebase_text`, and for `_site_name`'s fallback.
- `test_energy_setup.py`: the Repairs flow lists statistics by name, with no site ID.
- `test_api.py`: errors read `/sites/…/usage: HTTP 503` and `…: response is not JSON`.
- `test_importer.py`: a verification message starts "statistic e1_energy:" and has no
  site ID.
- `test_statistics_model.py`: `statistic_text`.

## 2. Test results

- **Full suite, with 9102 stopped** (head `9dbf134`; later commits changed only docs, the
  manifest and one test's string prefix):
  `uv run pytest --cov=custom_components.amber_energy_dashboard` gives:
  - **616 passed**, 0 skipped, 0 failed, in 8 min 18 s;
  - **100 % coverage** (6,198 statements);
  - 2 warnings: the harness's known `ResourceWarning: unclosed database`, on
    `test_meters.py::test_site_name_names_the_device` and
    `test_solar_gaps.py::test_fill_solar_sums_gap_rules`.
- `ruff check`, `ruff format --check` and `uv lock --check` are clean on `70fbccd`.
- **CI:**
  - **`3fa5787` failed on Ruff lint** (hassfest and HACS passed). After editing
    `test_api.py`, I re-ran pytest but not ruff: a `match=` pattern needed a raw string
    (RUF043). This was my process slip.
  - **`70fbccd`** fixes it: Tests (40164823191), hassfest (40164823285) and HACS
    validation (40164823391) all **succeeded**.

## 3. Lab verification (9102, HA 2026.10.0)

**Setup:**
- Snapshot `m22-rc1-start` (the `m22-b4-lab` state), then 9102 was started.
- The installed tree's manifest said `2.2.0b3`: the b4-lab install predates the b4
  version bump.
- Head `3fa5787` was installed by `git archive`. The `__init__.py` sha256 matched the
  repository (`0c3abdfe…`). `manifest/get` reported **2.2.0rc1**.

| Item | Evidence |
|---|---|
| **2b**, before (old code) | Export allowance turned on through the site settings (8 kWh/day, billing period, solarSponge; N61 detected). `sensor.amber_energy_dashboard_export_charge_after_allowance` state **`'-0.0'`**, attributes `penalty` 1.7992, `refund` 1.7992 |
| **2b**, after (rc1 head, restarted) | The same sensor, the same penalty and refund (1.7992 / 1.7992): state **`'0.0'`** |
| **2a** | Config flow over REST, test key → step `site`. Option label **"NMI …1234 (Endeavour Energy)"** (the lab's real last four digits, written here as the placeholder). The flow was then aborted |
| **2d** (dry run) | v1 kit seeded (below). Site settings → Migrate → `migrate_confirm`. The report shows: "grid import kWh: sensor.amber_energy_import -> Grid import energy (Site …1234)" (×4 roles); "Parity (daily rules): 118 days (2026-06-29 to 2026-10-24), passed."; "Copy 31 rows into Grid import energy (Site …1234) (2026-05-29 to 2026-06-28), baseline 450.0."; and the before/after list ("Grid consumption: sensor.amber_energy_import, no cost" → "Grid consumption: Grid import energy (Site …1234), cost from Grid import cost (Site …1234)", and the same for return to grid). **No statistic ID and no JSON** |
| **2e** (result) | Submitted with the backup box: `migrate_result` shows "Migration status: completed.", two "Copied 31 rows into Grid … energy (Site …1234) (baseline …)." lines, **"Re-based 118 days from stored hourly amounts, with no API calls."**, "Energy dashboard switched to the new statistics.", and the cleanup list |
| Read-back | `energy/get_prefs`: the grid source's four `stat_*` now point at the integration's `…_e9_energy` / `_e9_cost` / `_b9_energy` / `_b9_compensation`. Diagnostics (125 KB) still carry the raw IDs (`_e9_energy` 124 times) and the stored `rebase` dict (`rewritten_days`). Nothing new from the integration in `ha core logs` (the only lines are warnings from 10-21 to 10-24, from runs before the snapshot) |

**The v1 kit seed** (`recorder/import_statistics`, as M21 3.2):
- `sensor.amber_energy_import` and `_export`: 148 daily lumps each at local midnight.
  - From 06-29 on, they equal the integration's NEM-day totals from its hourly changes.
  - The 30 days before that are synthetic: 15 and 8 kWh a day.
- One zero row each for the two kit cost sensors.
- An Energy dashboard grid source on the two legacy sensors (flat format).
- No helpers or automation were created. So the dry run lists no automation, and the
  cleanup list has no automation line. This differs from the screenshot.

The site on this snapshot was never renamed, so its labels read "(Site …1234)". With a
named site they read "(Home)", as in the tests.

## 4. Open questions assigned to the milestone

None were assigned. Two observations:

- **Three screenshots now show the pre-rc1 wording.** They were taken on b4, before
  these fixes:
  - `18-migration-dry-run.png` (raw IDs, blurred, and raw JSON);
  - `19-migration-complete.png` (the raw re-base dict);
  - `device-page.png` and `new-sensors.png` (Export charge "-A$0.00");
  - `bill-card-previous.png` ("$-5.37", centred labels).

  They are correct in substance but not in detail. Retaking them on rc1 is
  recommended (section 8). The lab states to do it are described in section 7.
- **`bill-card.png` shows only the "This bill so far" part.** cards/README describes the
  card with both parts. The second image shows the previous-bill part, so the pair
  covers it.

## 5. Deviations, decisions and points for the planning chat

1. **All images at 50 %, not just the new ones.** The older images were at about 40–50 %;
   now all are 50 %. Revert 01–09 if they look too large on GitHub.
2. **Negative zero is normalised below half a cent**, not only exactly −0.0. The
   allowance's −0.0 came from a sum that rounds to zero, and a state of −0.0001 would also
   display "−A$0.00". The state is changed only where the displayed value is 0.00 anyway.
3. **Item 2f went beyond the migration screens.** The site ID reached the user through
   error messages: statistic IDs, and the API request path. Both now drop it ("statistic
   e1_energy", "/sites/…/usage"). The log lines built from these exceptions lose it too.
   The log keeps the site context elsewhere (site key, the run summaries), and
   diagnostics keep every ID.
4. **Manual Energy dashboard instructions** name statistics by their **picker names**
   ("Amber general E1 energy"), not the friendly labels. The user has to find them in
   that picker.
5. **The migration labels use the site sub-entry's title.** On an install upgraded from
   2.1 and never renamed, that is "Site …1234".
6. **DESIGN:** milestone 22 is recorded, and the b4 work is noted under item 21. "Roadmap:
   2.3" is a new subsection at the end of section 16. The battery cost basis is
   cross-referenced to section 22.
7. **Release notes:** `release-notes-v2.2.0.md` now says the "four betas (b1 to b4) and
   the release candidate (rc1)" ran in production. Adjust it if 2.2.0 is tagged
   differently. Both notes list the YAML bill card as a known limitation, with the
   built-in card planned for 2.3.
8. **CHANGELOG:** the `[2.2.0b4]` section and its link were removed; its content was
   already in `[2.2.0]`. New entries:
   - **Changed:** migration screens; messages without the site ID; guides and
     screenshots;
   - **Fixed** (also relative to 2.1.1): the Choose a site NMI; −A$0.00.

## 6. Live Amber API calls

All with the test key, between 16:07 and 16:11 Sydney time (outside 07:17–07:27). The test
suite makes none.

- **`/sites`: 4.** One each for:
  - the 9102 boot (16:07);
  - the reload after turning on the export allowance (16:08);
  - the restart onto head (16:09);
  - the config flow's key check, for 2a (16:10).
- **`/usage`: not counted.** Turning on the export allowance runs
  `async_measure_export` for the cycle's unmeasured days. Its call count is held in memory
  only, and I didn't read it before shutting down. By the M21 rate (15 calls for 89
  days), it was at most about 15.
- The migration's re-base made **no calls** (its result says so).
- The **screenshot session** before this milestone (the maintainer's config flow on 9103)
  is counted in `local/screenshot-session-2026-10-25.md`.

## 7. Lab state left behind

- **9102 `amber-test-m3`: stopped**, rolled back to **`m22-rc1-start`** (= `m22-b4-lab`:
  the b4-era tree, allowance off, no kit).
- **New snapshots:**
  - `m22-rc1-start`;
  - **`m22-lab`**: rc1 head installed, export allowance on, v1 kit seeded and migrated.
    This is the state for retaking 18/19/device-page/new-sensors. 18 needs an undo
    first, or a rollback and a re-seed.
- Earlier snapshots are kept. Pool: 9000 (template, untouched) and 9102 only.
- No `.claude_work` folder was created. SSH add-on protection was not touched. VM 101 was
  not touched.
- **Local:**
  - `~/screenshots` is deleted;
  - scratch OCR venv and lab scripts are in the session scratchpad (not in the repo);
  - the lab HA login `docs` exists only on the `screens-211-b4` snapshot.

## 8. Recommended next steps

1. Review, especially:
   - the 2f sweep (deviation 3);
   - the image widths (deviation 1).
2. Optional: retake `18-migration-dry-run.png`, `19-migration-complete.png`,
   `device-page.png`, `new-sensors.png` and `bill-card-previous.png` on rc1. Start from
   snapshot `m22-lab`; the labels would read "(Home)" after a Rename site.
3. **The open decision:** whether Today so far applies the calibration offset, from
   production's `yesterday_estimate_vs_billed` (error with and without the offset). Then:
   - tag `v2.2.0rc1` (annotated, with `reports/release-notes-v2.2.0rc1.md`) after CI is
     green;
   - let production run rc1;
   - set the CHANGELOG date and the manifest to `2.2.0` for the release.
4. After 2.2.0 is a full release: the `hacs/default` PR (M21 section 4).
5. 2.3: the battery cost basis, and the built-in "Amber bill" card (DESIGN section 16,
   "Roadmap: 2.3").
