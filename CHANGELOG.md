# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

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

[2.0.0-rc2]: https://github.com/r5e/ha-amber-energy-dashboard/compare/v2.0.0-rc1...v2
[2.0.0-rc1]: https://github.com/r5e/ha-amber-energy-dashboard/releases/tag/v2.0.0-rc1
