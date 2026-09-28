# v2.0.0-rc1: Amber Energy Dashboard 2.0, release candidate 1

**Pre-release.** This is the first release candidate of version 2. Please try it and
[report problems](https://github.com/r5e/ha-amber-energy-dashboard/issues). Take a Home
Assistant backup first, especially before migrating from the YAML kit.

Version 2 rebuilds the Amber Energy Dashboard as a **Home Assistant custom integration**,
installed through HACS and set up entirely in the UI. It imports your **actual metered
usage and the real per-interval cost** from Amber Electric into Home Assistant's
long-term statistics. The Energy dashboard then shows what Amber billed, hour by hour.
It runs alongside the core Amber Electric integration, which provides live prices.

Requires **Home Assistant 2026.9 or later**.

## Highlights

- **Hourly history, from Amber's own data:** grid import (general and controlled load),
  grid export, cost and feed-in compensation, written as statistics the Energy dashboard
  uses directly. Compensation is positive when earned.
- **Hands-off:**
  - backfills everything Amber still holds (about three months) at setup;
  - imports each new day every morning;
  - catches up automatically after outages;
  - corrects history when Amber revises estimated days.
- **Safe by design:**
  - a day is written only when it is complete;
  - every write is read back;
  - progress advances only after a verified write;
  - any disagreement stops with a Repairs item rather than guessing;
  - daylight saving cannot create duplicate or missing hours.
- **Rate-limit aware:** 7-day fetch windows and a per-run budget. It respects Amber's shared
  limit of 50 calls per 5 minutes.
- **Own-sensor cost:** price your own meter (CT clamps, smart plugs) at Amber's billed rate
  for every 5-minute interval. Use it in the Energy dashboard as the grid consumption's
  cost. A reconciliation sensor compares your meter with Amber's.
- **Usage modes:** Full, Recovery-only (with a `backfill` action) or Pricing-only.
- **Optional hourly price series.**
- **Migration from the v1 YAML kit:**
  - a dry run, and a parity check before any change;
  - a scan for corrupt legacy rows (such as a `99999` test spike);
  - a copy of your older history with continuous totals;
  - the Energy dashboard switched over, with your old settings saved;
  - the old automation turned off (never deleted), plus a cleanup list;
  - undo.
- Diagnostics with the API key and NMI redacted.

## Installing

1. HACS > three-dot menu > **Custom repositories**: add
   `https://github.com/r5e/ha-amber-energy-dashboard`, with type **Integration**.
2. In the repository's settings in HACS, turn on **Show beta versions**. This is needed
   to see this release candidate.
3. Download **Amber Energy Dashboard (unofficial)**, and restart Home Assistant.
4. Go to **Settings > Devices & services > Add integration > Amber Energy Dashboard
   (unofficial)**. Enter your Amber API key, pick the site, confirm the channels and
   choose the schedule.

The first import (about three months) takes around 20 API calls. After that, each day
costs a few calls.

## Upgrading from the v1 YAML kit

1. Take a full backup.
2. Install the integration and let its first import finish.
3. Open **Configure > Migrate from the YAML kit** and read the dry run.
4. Tick the backup confirmation to run it.

What to expect:
- v1 history stays daily. The v1 kit's cost history was empty (its cost came from a
  sensor that never changes), so it is not copied. Cost history starts with the
  integration's own data.
- Nothing is deleted. Old statistics are kept until you remove them with
  `delete_legacy_statistics`.
- **Undo migration** restores your previous Energy dashboard settings.

Full guide: see the README section "Migrating from the YAML kits".

## Known limitations

- Amber keeps about three months of usage history. Older days cannot be fetched, except
  from the YAML kit's own history through the migration.
- Legacy history from the kits has no controlled-load channel.
- The migration supports a site with one general channel and at most one feed-in channel.

## Changes

See [CHANGELOG.md](https://github.com/r5e/ha-amber-energy-dashboard/blob/v2.0.0-rc1/CHANGELOG.md).

## Credits

This builds on earlier community work. The README lists it: Tmbao/amberelectric-usages,
danVnest/amberelectric-usage, melvanderwal/HA-Amber-Electric-Usage-Charts,
hass-energy/amber-express, nickw444/ha-amberelectric and madpilot/hass-amber-electric.

*Unofficial community project; not affiliated with or endorsed by Amber Electric.*
