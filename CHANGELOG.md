# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Release versions follow
[Semantic Versioning](https://semver.org/) (major.minor.patch); pre-releases are written
the [PEP 440](https://peps.python.org/pep-0440/) way (for example `2.2.0b2`), because Home
Assistant and HACS compare versions that way.

## [Unreleased] (2.2.0-dev4)

### Added

- **Solar-aware appliance cost** (per appliance, off by default): a second statistic,
  "<name> cost (solar-aware)", and a **Yesterday solar-aware cost** sensor. The part of
  the appliance's energy that the site's solar covered (worked out every 5 minutes from
  the grid meter's import and export and the solar sensor) is valued at the feed-in
  price, the income given up, and the rest at the import price. With the export
  allowance, the feed-in value includes the refunded network charge while within the
  allowance. Needs a solar sensor in Sources and a grid meter with import and export.
  Existing appliances turn it on with **Reconfigure**. With battery sensors, battery
  discharge is shown as its own share (valued at the import price for now).
- **Previous bill estimate** (with the bill estimate on): the last billing cycle's total,
  with its lines, dates and a `complete` flag.
- An example **bill card** (button-card based) in `docs/cards/`: the current cycle and
  the previous bill.
- **Today so far:** `offset_corrected` (and `offset_corrected_kwh`, `offset_w`) shows the
  estimate with the grid meter's calibration offset applied, and
  `yesterday_estimate_vs_billed` compares both with the bill. The estimate itself is
  unchanged.

### Fixed

- **Today so far** didn't start, and its sensors weren't created, until the Sources step
  was saved when Amber Express's (or Amber Electric's) price sensors were registered after
  this integration started, or the price integration was installed later. A missing price
  is now looked for again once Home Assistant has started, whenever their sensors are
  registered, and every minute for 15 minutes; Today so far then starts without a reload.

## [2.2.0b2] - 2026-10-24

Second beta for 2.2: everything in 2.2.0b1, plus the changes below. **Take a Home
Assistant backup before updating from 2.1.1 or earlier:** the entry is migrated to a
layout that earlier versions can't read (see 2.2.0b1). Updating from 2.2.0b1 needs no
migration.

### Added

- **Sources** (site settings): a live price for the general and feed-in channels, found
  automatically from Amber Express or Home Assistant's Amber Electric integration for the
  same site (you can choose another sensor, or none; $/kWh, c/kWh and $/MWh are accepted),
  and optional solar (power and/or energy) and battery (charge and discharge) sensors,
  stored for a coming feature.
- **Today so far:** with a grid meter and a live price, **Today's cost so far
  (estimated)** and **Today's feed-in so far (estimated)**: the meter's energy since
  midnight (NEM time), priced every 5 minutes at the live price. Display only, never in
  statistics. Once Amber publishes a day, `yesterday_estimate_vs_billed` compares that
  day's estimate with the bill, and **Bill to date** gains `including_today_estimated`.
- **Rename site** (site settings): renames the site's row and its device; entity IDs
  don't change.

### Changed

- **Calibration offset** now shows the mean of the hourly differences (the correction
  that makes the meter's totals match the bill); the median is the `median_w` attribute.
- **A site the API key no longer sees** (for example a closed site) is skipped with a
  Repairs item, and the account's other sites keep running. Re-authentication needs a key
  that sees at least one of the sites.

## [2.2.0b1] - 2026-10-24

Beta for 2.2. Everything since 2.1.1. **Take a Home Assistant backup before updating:** the
entry is migrated to a new layout that earlier versions can't read, so the only way back is
restoring the backup.

### Added

- **Grid meters:** a meter on the main supply (a CT clamp or energy monitor on the mains,
  or an inverter or battery system's grid meter). An import sensor priced on the general
  (or controlled load) channel, and an optional export sensor priced at the feed-in rate.
  Its device has **Import reconciliation**, **Export reconciliation** and, with both, a
  **Calibration offset** sensor: the 14-day median of Amber's hourly net energy minus the
  meter's, in watts, from complete billed days, leaving out hours where the meter's
  statistics have gaps.
- **Appliances:** any single energy sensor (a smart plug, a circuit, a heat pump, an EV
  charger), priced on the general or controlled load channel, with **Yesterday energy**
  and **Yesterday cost**. Appliances are not reconciled.
- **Site name:** setup shows the site's details (network, NMI ending, active from) and
  asks for a friendly name, which names the site's device. A name given later on Home
  Assistant's device page is used as the site's name. Entity IDs don't change with it.
  The site device's model line shows the network and the NMI's last four digits, for
  example "Endeavour Energy · NMI …1234".
- **More than one site:** **Add site** sets up another site of the same account. The
  account's sites take turns, sharing Amber's rate limit. Grid meters and appliances ask
  which site they belong to.
- **Evening retries:** when yesterday's usage still isn't published after the day's last
  attempt, the integration retries every 3 hours until 22:00 (for example 16:22 and
  19:22), and stops as soon as the day is imported. Days that import normally get no
  extra attempts.

### Changed

- **Account and sites:** the entry is the Amber account (its API key), and each site is
  a **Site** under it, owning the site's device and entities. Existing installs are
  migrated automatically on update: the entry keeps its ID, and no entity ID, unique ID or
  statistic changes. Home Assistant no longer shows "Devices that don't belong to a
  sub-entry".
- **Site settings** (import settings, bill estimate, export allowance, refreshing the
  channels, the YAML-kit migration) belong to the site: the cog asks which site when there
  are several, and the pencil on a site opens the same menu. **Refresh channels** replaces
  the entry's Reconfigure.
- **Own energy sensors** are replaced by grid meters and appliances. Existing ones keep
  working unchanged; a Repairs item converts them, keeping every statistic and the
  reconciliation sensors' entity IDs.
- **Services** take an optional **site** (the site's device). `config_entry_id` still
  works for an account with one site.
- With several sites, a new entry's title shows the site's name instead of the NMI's last
  four digits.
- **Repairs items** are now kept per site. A Repairs item you ignored in 2.1 is not
  carried over: if it still applies, it appears again and can be ignored again.

## [2.1.1] - 2026-10-23

Fixes from the first production use of own-sensor mappings, and naming that is easier to
follow on the integration page.

### Fixed

- **Feed-in own sensors:** a mapping to a feed-in channel now gets a **Reconciliation**
  sensor (your sensor's kWh minus Amber's billed export kWh, with the same attributes as a
  general mapping) and its own device. 2.1.0 created neither, so the mapping showed no
  device.
- **Own-sensor devices:** a mapping's Reconciliation sensor is linked to the mapping's
  device when the mapping is added, and an existing one that isn't is linked on update.
  Entity IDs don't change.
- **Retention boundary:** a failed retention check (for example a network or DNS outage)
  no longer moves the stored boundary. Before, each failed rediscovery stored a fallback
  of today − 89 days, which could move a verified boundary forward a day at a time, and
  older days Amber still had were then skipped. The boundary is now kept, and discovery is
  tried again later.

### Changed

- The site's device is named **Amber Energy Dashboard (site)**. Home Assistant lists it
  under "Devices that don't belong to a sub-entry"; the name now says what it is. Entity
  IDs are unchanged, on existing and new installs; friendly names start with the new name.
- The entry's default title is **Amber Energy Dashboard**, without the NMI. With a second
  site, titles show the NMI's last 4 digits, for example **Amber Energy Dashboard
  (…1234)**. Existing entries titled "Amber <NMI>" are renamed; a title you chose is kept.
- INSTALL.md explains the integration page's layout.

## [2.1.0] - 2026-10-10

Everything since 2.0.1 (release candidates rc1 and rc2 merged). The bill estimate and the
export allowance are off until configured (the allowance turns itself on for Endeavour
Energy N61 once a billing day is set).

### Added

**Bill estimate** (options: Bill estimate)
- Options, as on the Amber bill: the **billing day** (1 to 28), the **daily supply
  charge** (the charges summary's "Network Daily Supply Charges" rate, which includes
  metering), the **Amber subscription** (from the Amber Fees; 0 if it is currently free,
  for example a first-year offer) and **other daily charges** (optional, default 0), all in
  $ per day excluding GST, and the **GST rate** (10 %). The daily supply charge and the
  subscription have no default and must be entered. Each field says where to find it on
  the bill.
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
  tariff period Amber gives for each interval. Turning the allowance or the bill estimate
  on, or changing either, measures the imported days straight away in the background,
  without waiting for an import run.
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
- **Stuck on "Waiting for Amber data" after joining Amber recently.** When the first
  discovery of Amber's history fell back to 89 days, the import waited on each day before
  you joined. Now:
  - the site's start date from Amber is a lower bound for the history;
  - a fallback is provisional and is discovered again the next day;
  - empty days at the very start of the history are skipped at once when later days have
    data.

  A stuck install recovers by itself at its next run.
- The import settings step no longer drops options set in other steps.

### Documentation

- README and installation guide: the bill estimate, the export allowance, and a table of
  which statistics to select in the Energy dashboard, and when.
- Installation guide: "Finding these on your bill", with an annotated bill; new pictures
  of the device page, the options menu, the bill estimate and export allowance steps, and
  the new sensors.
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

[2.1.1]: https://github.com/r5e/ha-amber-energy-dashboard/releases/tag/v2.1.1
[2.1.0]: https://github.com/r5e/ha-amber-energy-dashboard/releases/tag/v2.1.0
[2.0.1]: https://github.com/r5e/ha-amber-energy-dashboard/releases/tag/v2.0.1
[2.0.0]: https://github.com/r5e/ha-amber-energy-dashboard/releases/tag/v2.0.0
[2.0.0-rc2]: https://github.com/r5e/ha-amber-energy-dashboard/releases/tag/v2.0.0-rc2
[2.0.0-rc1]: https://github.com/r5e/ha-amber-energy-dashboard/releases/tag/v2.0.0-rc1
