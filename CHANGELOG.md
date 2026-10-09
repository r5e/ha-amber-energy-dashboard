# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [2.1.0-rc2] - 2026-10-09

### Fixed

- **Stuck on "Waiting for Amber data" after joining Amber recently.** When the first
  discovery of Amber's history fell back to 89 days, the import waited on each day before
  you joined. Now:
  - the site's start date from Amber is a lower bound for the history;
  - a fallback is provisional and is discovered again the next day;
  - empty days at the very start of the history are skipped at once when later days have
    data.

  A stuck install recovers by itself at its next run.

### Changed

- **Bill estimate charges as on the Amber bill:** **Daily supply charge** (the summary's
  "Network Daily Supply Charges" rate, which includes metering), **Amber subscription**
  (from the Amber Fees; 0 if it is currently free, for example a first-year offer) and
  **Other daily charges** (optional, default 0), all in $ per day excluding GST, with help
  text on where to find each. The daily supply charge and the subscription have no
  default and must be entered.
  rc1's network, metering and subscription values are migrated automatically (network +
  metering become the daily supply charge); the estimate is unchanged.

### Documentation

- Installation guide: "Finding these on your bill".

## [2.1.0-rc1] - 2026-10-09

Release candidate for 2.1. Everything since 2.0.1. The bill estimate and the export
allowance are off until configured (the allowance turns itself on for Endeavour Energy N61
once a billing day is set).

### Added

**Bill estimate** (options: Bill estimate)
- Options: the billing day (1 to 28), the daily fixed charges excluding GST as named on the
  bill (network daily, metering, Amber subscription, other; simplified in rc2), and the
  GST rate (10 %).
- Sensors: **Bill to date** (with each line in its attributes), **Projected bill**, **Days
  into billing cycle** and **Average cost per day**. Cycle days are Amber's (AEST) days, as
  on the bill. Only complete imported days count; the attributes say which day the data
  runs to. One-off charges (such as card fees) are not included.
- The `bill_estimate` action returns the estimate for any cycle still in the day records.
- Optional statistic **import cost including fixed charges**: the import cost (general and
  controlled load) plus the fixed charges, built from the integration's own statistics. It
  covers the whole history (including history copied from the YAML kit) with no API
  calls, and follows every rewrite of the cost. Changed charges apply from the day after
  the last imported day.

**Export allowance** (options: Export allowance)
- For two-way network tariffs with a free export allowance applied on the bill. Endeavour
  Energy N61 (8 kWh a day, over the billing period) is detected from Amber at setup and
  turned on once a billing day is set; other tariffs can be configured (allowance, daily or
  billing-period totalling, charged period).
- The export charge and the peak reward are measured from Amber's data each day, by the
  tariff period Amber gives for each interval.
- Statistic **compensation (export allowance applied)**: feed-in earnings with the charge
  refunded within the allowance, over the whole history.
- Sensors **Export allowance used**, **Export allowance remaining** and **Export charge
  after allowance**. The bill estimate uses the adjusted earnings, with the export charge
  after the allowance as its own line.

**Setup and maintenance**
- **Buttons** on the device: **Run now** and, as a diagnostic entity, **Re-check
  retention**.
- **Add to the Energy dashboard** at the end of setup, when the Energy dashboard has no grid
  connection (on by default). An existing grid connection is never changed; the Energy
  dashboard settings are saved and read back first.
- **Repairs item "The Energy dashboard isn't using Amber Energy Dashboard"**, a day after
  the first import if nothing in the Energy dashboard uses the statistics. Its fix adds
  them, points to the migration (YAML kit detected), or lets you dismiss it. It clears
  itself once the statistics are in use.
- **Migration: unverifiable history.** When the YAML kit's data doesn't overlap the
  integration's (for example a kit that stopped before the integration's first day), the
  dry run explains why and names the days neither has; the migration then runs only with
  an explicit acknowledgement (options checkbox, or `acknowledge_unverified: true`).
  Differences still stop it.

### Fixed

- **Own-sensor mappings removed the integration's other sensors (2.0.1).** Home Assistant
  gives a device one config sub-entry; the reconciliation sensor of an own-sensor mapping
  shared the site's device, so adding a mapping moved the device into the sub-entry and
  Home Assistant removed the import status, "yesterday" and other sensors (and, with the
  new buttons, could remove those instead). Each mapping now has its own device, "<name>
  (own sensor)", linked to the site's; its reconciliation sensor is named
  "Reconciliation", with the same entity ID. After updating, the removed sensors come back
  with their old entity IDs.
- The import settings step no longer drops options set in other steps.

### Documentation

- README and installation guide: the bill estimate, the export allowance, and a table of
  which statistics to select in the Energy dashboard, and when.
- README: why an hour of yesterday's usage appears after midnight in summer (NEM days), and
  HACS's "icon not available".
- Installation guide: the version step is now a footnote for pre-releases; the new setup
  option, the buttons and the Repairs item. The pre-release-era FAQ entry about HACS
  offering a commit code, and the unused version-picker screenshot, are removed.

## [2.0.1] - 2026-10-07

### Fixed

- Migration: a YAML kit that stopped importing before the integration's last day no longer
  fails the parity check. Days the kit has no data for (no rows, or 0 kWh import and
  export while the integration has data) are legacy gaps: reported, not compared, and
  covered by the integration's own data. A pause followed by a catch-up lump is compared
  as one total over the pause and the lump. Genuine differences on days both sides have
  data still fail.
- Migration dry run and result: gaps are stated explicitly ("The YAML kit stopped
  importing after ...; N days will come from the integration's own data"), and failures
  are counted by category before the first 10 are listed.

## [2.0.0] - 2026-10-03

Version 2: a rewrite of the v1 YAML kit as a Home Assistant custom integration, installed
through HACS. It needs Home Assistant 2026.9 or later. This entry covers everything since
the 1.x YAML kit; the release candidates are listed below for reference.

### Added

**Setup and schedule**
- UI setup:
  - the API key, checked live;
  - the site, one entry per site;
  - the channels, detected from Amber (general, controlled load, feed-in);
  - the schedule.
- Reauthentication when the key is rejected. Reconfigure when Amber reports new channels.
- Automatic daily schedule: a stable time between 06:30 and 08:30, with retries 3 and 6
  hours later. Fixed times are an option. A catch-up also starts after an interrupted run.

**Statistics**
- Hourly external statistics per channel: energy (kWh), cost (AUD) and, for feed-in,
  compensation (positive when earned). A site-wide net cost in Amber's sign is added.
- Statistics IDs come only from Amber's site ID and channel identifiers, never from user
  input.

**Import safety**
- Guarded daily import:
  - a day is written only when it is complete;
  - every write is read back before progress is recorded;
  - an interrupted write is recovered on the next run;
  - disagreements between progress and statistics are raised in Repairs instead of
    guessed at.
- Days are Amber's calendar days (00:00 to 24:00 AEST) in UTC hours, so daylight saving
  cannot duplicate or drop an hour.

**Catch-up and data handling**
- Automatic catch-up after outages, fetched in 7-day windows, within a per-run budget
  that respects Amber's shared rate limit (reading `RateLimit-Remaining`, backing off on
  HTTP 429).
- Retention kept as a boundary date: discovered at setup (at most 8 calls), then checked
  daily with 2 calls (the boundary day and the day before), whatever the day count. Moves
  in either direction are followed, including a boundary far in the past, which is
  approached over the next days within the call cap. Days older than the boundary are
  skipped.
- Patience for empty days: a day that stays empty while later days have data is skipped
  as a gap after 7 separate days (configurable).
- Revision handling: estimated days are re-checked for 14 days (configurable), and history
  is rewritten from the first changed day, with every later total re-derived.
- Usage modes:
  - **Full**;
  - **Recovery-only**, with the `backfill` service;
  - **Pricing-only**.

  Switching modes never deletes statistics.

**Optional features**
- Own-sensor cost (config sub-entries): any cumulative energy sensor priced at Amber's
  billed 5-minute rates.
  - An hourly fallback, flagged "lower precision", once 5-minute data is gone.
  - A reconciliation sensor for whole-house sensors.
  - Works with the Energy dashboard's "entity tracking the total costs" (the CT-clamp use
    case).
- An optional hourly price series (mean, minimum and maximum, AUD/kWh).

**Sensors and services**
- Display sensors:
  - import status ("Up to date" only while nothing is outstanding; otherwise, after a
    caught-up run, "Waiting for next run"), last imported date, days behind, next run;
  - yesterday's energy, cost and compensation per channel.
- Services: `run_now`, `import_day`, `backfill` and `probe_retention` (finds the retention
  boundary again from scratch; if it fails, the stored boundary is kept).
- Repairs items and a diagnostics download, with the API key and NMI redacted.

**Migration from the YAML kits** (the v1 kit and the advanced YAML version)
- Detection, and a parity check before any change.
- A scan of the old statistics for implausible rows before copying: more than 100 kWh or
  more than 500 (AUD) per elapsed hour between rows, a decreasing energy total, or a
  reset. A cost decrease is never flagged. The dry run lists them, a real run refuses by
  default, and `exclude_flagged` leaves them out and re-derives the totals around them.
- A copy of the older history with continuous totals.
- The Energy dashboard switched, with a saved copy of its settings.
- Legacy automations turned off, never deleted.
- A cleanup list in the result, and a Repairs item that lists only the kit's leftovers
  still present and clears itself once they are gone (the `amber_api_key` secret and the
  old statistics are advisory).
- Undo, and a separate, explicit deletion of the old statistics.
- The advanced version's feed-in cost is flipped to positive-when-earned (the dashboard
  sign fix).
- The v1 kit's empty cost history is detected and not copied.

**Documentation**
- An installation guide (`docs/INSTALL.md`) and a migration guide (`docs/MIGRATION.md`),
  with screenshots. The README links them, and explains why feed-in earnings can be lower
  than the export credit on the bill (per-interval export charges in Amber's data versus a
  free export allowance applied on the bill, for example Endeavour N61, 8 kWh a day).

**Other**
- A brand icon, shipped with the integration.

### Changed since 2.0.0-rc2

- **Cleanup repair re-checks itself.** "Finish removing the YAML kit" was a fixed list
  that never cleared. It is now re-checked when Home Assistant starts and after each
  scheduled import, lists only the leftovers still present (entities, the
  `rest_command`, and the v1 backfill files in the configuration folder), and clears
  itself once none remain. Two parts are advisory and never keep it open: the
  `amber_api_key` line in `secrets.yaml` (still listed in the migration result and the
  migration guide, since another tool may use it) and the optional old statistics.
- **Import status no longer shows "Up to date" while days are outstanding.** After a
  restart following downtime, the last run's outcome was "caught up" while "Days behind"
  was above 0. The sensor now shows **Waiting for next run** until the next scheduled
  attempt imports the outstanding days. The stored outcome is unchanged, and diagnostics
  show both (`status` and `display_status`). Recovery-only mode, which imports no usage
  on a schedule, is unaffected.
- **Retention record after `probe_retention`.** The stored record (shown in diagnostics)
  now keeps the boundary from before the probe as `previous_boundary`, instead of
  `null`, and a move is logged as a warning.
- The brand icon's final design.
- The installation and migration guides, and the README trimmed to link them.

### Replaced

- The v1 YAML kit (REST sensor, template sensors, helpers, the daily automation and the
  backfill script). It is kept for reference in `legacy/v1/`. Existing v1 users can
  migrate; see `docs/MIGRATION.md`.

## [2.0.0-rc2] - 2026-09-28

### Added

- `amber_energy_dashboard.probe_retention` action: finds Amber's retention boundary again
  from scratch (at most 8 API calls). If it fails, the stored boundary is kept.

### Changed

- **Retention with a boundary far in the past.**
  - Discovery that cannot bracket the boundary within 30 days now stores the oldest day it
    saw with data, instead of today − 89.
  - A backward move beyond the daily check's bracket is accepted up to the bracket end
    ("moved back at least N days"), and the next days continue from there.
  - A fixed boundary far back now converges within the call cap, and then costs 2 calls a
    day. Before, it alternated between a failed check and a fresh discovery every day,
    for about 3 to 4 extra calls a day.
- **Migration scan:** the energy cap is now 100 kWh per elapsed **hour** between rows
  (it was per row, scaled by days). A large household's daily lump, such as 150 kWh, is
  never flagged. The `99999` test spike still is.
- **Migration scan:** the cost cap is now 500 per elapsed **hour** between rows (it was 100
  per row, scaled by days), so a real wholesale price spike hour (around 17.50/kWh at the
  market cap) is never flagged. A cost decrease is never flagged (before, a fall of more
  than 100 in a row was), since negative prices make the cost fall legitimately.

### Removed

- `rebuild_from_anchor` from the design. It was never implemented; `backfill` with the
  guarded tail rewrite covers it.

## [2.0.0-rc1] - 2026-09-28

The first release candidate of version 2: a rewrite of the v1 YAML kit as a Home
Assistant custom integration, installed through HACS. Version 2 needs Home Assistant
2026.9 or later.

### Added

**Setup and schedule**
- UI setup:
  - the API key, checked live;
  - the site, one entry per site;
  - the channels, detected from Amber (general, controlled load, feed-in);
  - the schedule.
- Reauthentication when the key is rejected. Reconfigure when Amber reports new channels.
- Automatic daily schedule: a stable time between 06:30 and 08:30, with retries 3 and 6
  hours later. Fixed times are an option. A catch-up also starts after an interrupted run.

**Statistics**
- Hourly external statistics per channel: energy (kWh), cost (AUD) and, for feed-in,
  compensation (positive when earned). A site-wide net cost in Amber's sign is added.
- Statistics IDs come only from Amber's site ID and channel identifiers, never from user
  input.

**Import safety**
- Guarded daily import:
  - a day is written only when it is complete;
  - every write is read back before progress is recorded;
  - an interrupted write is recovered on the next run;
  - disagreements between progress and statistics are raised in Repairs instead of
    guessed at.
- Days are Amber's calendar days (00:00 to 24:00 AEST) in UTC hours, so daylight saving
  cannot duplicate or drop an hour.

**Catch-up and data handling**
- Automatic catch-up after outages, fetched in 7-day windows, within a per-run budget
  that respects Amber's shared rate limit (reading `RateLimit-Remaining`, backing off on
  HTTP 429).
- Retention discovery, with a daily 2-call check. Days older than Amber's retention are
  skipped.
- Patience for empty days: a day that stays empty while later days have data is skipped
  as a gap after 7 separate days (configurable).
- Revision handling: estimated days are re-checked for 14 days (configurable), and history
  is rewritten from the first changed day, with every later total re-derived.
- Usage modes:
  - **Full**;
  - **Recovery-only**, with the `backfill` service;
  - **Pricing-only**.

  Switching modes never deletes statistics.

**Optional features**
- Own-sensor cost (config sub-entries): any cumulative energy sensor priced at Amber's
  billed 5-minute rates.
  - An hourly fallback, flagged "lower precision", once 5-minute data is gone.
  - A reconciliation sensor for whole-house sensors.
  - Works with the Energy dashboard's "entity tracking the total costs" (the CT-clamp use
    case).
- An optional hourly price series (mean, minimum and maximum, AUD/kWh).

**Sensors and services**
- Display sensors:
  - import status, last imported date, days behind, next run;
  - yesterday's energy, cost and compensation per channel.
- Services: `run_now`, `import_day` and `backfill`.
- Repairs items and a diagnostics download, with the API key and NMI redacted.

**Migration from the YAML kits** (the v1 kit and the advanced YAML version)
- Detection, and a parity check before any change.
- A copy of the older history with continuous totals.
- The Energy dashboard switched, with a saved copy of its settings.
- Legacy automations turned off, never deleted, plus a cleanup list.
- Undo, and a separate, explicit deletion of the old statistics.
- The advanced version's feed-in cost is flipped to positive-when-earned (the dashboard
  sign fix).
- The v1 kit's empty cost history is detected and not copied.

**Other**
- A brand icon, shipped with the integration.

### Changed since the development builds (2.0.0-dev7)

- **Retention is kept as a boundary date.**
  - The daily check probes the boundary date and the day before: exactly 2 calls, whatever
    the day count. A boundary that stays fixed no longer counts as a daily "move".
  - The day count is now derived, for display.
  - The store upgrades itself (1.2 to 1.3).
- **The migration scans the legacy statistics for implausible rows** before copying:
  - an energy step above 100 kWh per row, scaled by the days between rows (per elapsed hour from rc2);
  - decreasing energy totals;
  - resets;
  - a cost change larger than 100 in one row (from rc2: an increase above 500 per elapsed hour).

  The dry run lists them, and a real run refuses by default. The `exclude_flagged` option
  leaves them out and re-derives the totals around them.

### Replaced

- The v1 YAML kit (REST sensor, template sensors, helpers, the daily automation and the
  backfill script). It is kept for reference in `legacy/v1/`. Existing v1 users can
  migrate; see the README.

## [1.x] - v1 YAML kit

The v1 YAML kit: a daily automation and a backfill script that wrote daily totals through
the Import Statistics integration. See `legacy/v1/README.md`.

[2.1.0-rc2]: https://github.com/r5e/ha-amber-energy-dashboard/releases/tag/v2.1.0-rc2
[2.1.0-rc1]: https://github.com/r5e/ha-amber-energy-dashboard/releases/tag/v2.1.0-rc1
[2.0.1]: https://github.com/r5e/ha-amber-energy-dashboard/releases/tag/v2.0.1
[2.0.0]: https://github.com/r5e/ha-amber-energy-dashboard/releases/tag/v2.0.0
[2.0.0-rc2]: https://github.com/r5e/ha-amber-energy-dashboard/releases/tag/v2.0.0-rc2
[2.0.0-rc1]: https://github.com/r5e/ha-amber-energy-dashboard/releases/tag/v2.0.0-rc1
