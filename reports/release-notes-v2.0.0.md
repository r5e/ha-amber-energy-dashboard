# v2.0.0: Amber Energy Dashboard 2.0

Version 2 is a **Home Assistant custom integration**, installed through HACS, that imports
your actual metered usage and the real per-interval cost from Amber Electric into Home
Assistant's long-term statistics. The Energy dashboard then shows what Amber really
billed, hour by hour. It replaces the v1 YAML kit: setup is in the UI, with no YAML at all.

Requires **Home Assistant 2026.9 or later**.

**➡️ New here? Follow the [installation guide](https://github.com/r5e/ha-amber-energy-dashboard/blob/v2.0.0/docs/INSTALL.md).**
**➡️ Used the YAML kit? Follow the [migration guide](https://github.com/r5e/ha-amber-energy-dashboard/blob/v2.0.0/docs/MIGRATION.md)** once the integration is set up.

## What you get

- **Hourly history** of grid import (general and controlled load) and grid export
  (feed-in) energy, with cost and feed-in compensation, as statistics the Energy
  dashboard uses directly.
- **Automatic backfill** of everything Amber still holds (about three months) at setup,
  and **automatic catch-up** after outages, within Amber's shared rate limit.
- **Safe imports:** a day is written only when it is complete, every write is read back,
  and daylight saving can never duplicate or drop an hour.
- **Revision handling:** days with estimated data are re-checked, and history is
  corrected when Amber revises them.
- **Own-sensor cost (optional):** your own meter (CT clamps, smart plugs) priced at Amber's
  billed rate for every 5-minute interval, usable as the Energy dashboard's cost source.
- **Price series (optional):** an hourly price series per channel.
- **Migration from the YAML kit** (v1 or the advanced version), with a dry run, a parity
  check, a scan for corrupt rows, the feed-in sign fix, and undo.
- Status and "yesterday" sensors, Repairs items, and a diagnostics download with the API
  key and NMI redacted.

It runs **alongside** Home Assistant's built-in Amber Electric integration, which provides
live prices. Keep both.

## What's new since rc2

- **The "Finish removing the YAML kit" repair clears itself.** It used to be a fixed list
  that never went away. It is now re-checked when Home Assistant starts and after each
  scheduled import, lists only the parts of the kit that are still there, and clears
  itself once they are all gone. The old statistics and the `amber_api_key` line in
  `secrets.yaml` are optional to remove (another tool may use the key), so they no
  longer keep it open; the migration guide still mentions both.
- **Import status no longer says "Up to date" while days are outstanding.** After Home
  Assistant has been off for a while, it now shows **Waiting for next run** until the
  next scheduled import catches up; "Days behind" shows how many days.
- **Diagnostics after `probe_retention`** now show the previous retention date, and a
  move is logged as a warning.
- **Installation and migration guides**, step by step with screenshots.
- **The final brand icon.**

## Upgrading

- **From rc1 or rc2:** update in HACS and restart Home Assistant. Your statistics and
  settings are kept. If you turned on **Show beta versions** for this repository to get
  the release candidates, you can turn it off again.
- **From a development build (2.0.0-dev):** the same. The stored settings upgrade
  themselves on the first start.
- **From the v1 YAML kit:** install the integration, let its first import finish, then
  follow the [migration guide](https://github.com/r5e/ha-amber-energy-dashboard/blob/v2.0.0/docs/MIGRATION.md).
  Take a full Home Assistant backup first.

## Good to know

- **Feed-in earnings can be lower than the export credit on your bill.** Amber's usage
  data applies your network's export charges to every interval. Some networks give a free
  export allowance instead (for example Endeavour Energy's two-way tariff N61, with
  8 kWh per day free before a midday export charge applies). Your bill applies that
  allowance, but the usage data does not. Import costs match the bill to the cent.
- **Supply and subscription charges are not included.** Amber's usage data covers usage
  only; daily network supply, metering and Amber's subscription are separate charges on
  your bill.

## Known limitations

- Amber keeps about three months of usage history. Older days cannot be fetched, except
  from the YAML kit's own history through the migration.
- Legacy history from the kits has no controlled-load channel.
- The migration supports a site with one general channel and at most one feed-in channel.

## Changes

See [CHANGELOG.md](https://github.com/r5e/ha-amber-energy-dashboard/blob/v2.0.0/CHANGELOG.md).

*Unofficial community project; not affiliated with or endorsed by Amber Electric.*
