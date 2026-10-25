# v2.2.0rc1: release candidate for 2.2

The **release candidate** for 2.2: everything from the four betas, with the polish found
while taking the new screenshots. If nothing turns up, 2.2.0 will be the same code with
its version number changed.

Requires **Home Assistant 2026.9 or later**. Your statistics, settings and entity IDs are
kept. Updating from a 2.2 beta needs no migration.

> [!WARNING]
> **Take a Home Assistant backup before updating from 2.1.1 or earlier.** At its first
> start, 2.2 moves the entry to the new account and site layout. Version 2.1.1 and earlier
> can't read that layout, so **there is no downgrade**: to go back, restore the backup
> (Settings > System > Backups). Installing 2.1.1 over 2.2 leaves the integration unable
> to load ("Migration error"). See the installation guide's
> [Upgrading from version 2.1](https://github.com/r5e/ha-amber-energy-dashboard/blob/v2.2.0rc1/docs/INSTALL.md#9-upgrading-from-version-21).

## What's new since 2.1.1

- **Your Amber account, with its sites:** the entry is the account (its API key), and
  each site is a **Site** under it, with its own device, sensors and settings. Setup asks
  for a friendly **site name**; **Rename site** changes it later. **Add site** sets up
  another site of the same account; the sites share Amber's rate limit.
- **Grid meters** (a meter on the main supply, with reconciliation and a **calibration
  offset** in watts) and **appliances** (any single energy sensor, with **Yesterday
  energy** and **Yesterday cost**) replace 2.1's own energy sensors. A Repairs item
  converts existing ones, keeping every statistic.
- **Solar-aware appliance cost** (per appliance, off by default): the part of an
  appliance's energy covered by your solar is valued at the feed-in price, the rest at
  the import price. Solar inverters that go offline at night are handled.
- **Sources:** live prices found from Amber Express or the Amber Electric integration,
  and optional solar and battery sensors.
- **Today so far:** **Today's cost so far (estimated)** and **Today's feed-in so far
  (estimated)** from your grid meter and the live price. Display only.
- **Previous bill estimate**, and an example
  [bill card](https://github.com/r5e/ha-amber-energy-dashboard/blob/v2.2.0rc1/docs/cards/README.md)
  (needs button-card).
- **Evening retries** when Amber publishes a day late: every 3 hours until 22:00.

The [CHANGELOG](https://github.com/r5e/ha-amber-energy-dashboard/blob/v2.2.0rc1/CHANGELOG.md)
has the full list.

## What changed since 2.2.0b4

### Fixed

- **Choose a site** (setup) showed the site's full NMI. It now shows "NMI …1234
  (network)", as everywhere else.
- A money sensor could read **−A$0.00** (for example **Export charge after allowance**).
  An amount below half a cent now reads 0.00.

### Changed

- **The YAML-kit migration's screens are readable.** The dry run and the result:
  - name the statistics in words, for example "Grid import energy (Home)", instead of
    their IDs (which hold your Amber site ID);
  - show the Energy dashboard's grid connection before and after as a short list, instead
    of raw settings;
  - describe the re-base in a sentence, for example "Re-based 90 days from stored hourly
    amounts, with no API calls".

  The service response and diagnostics keep the statistic IDs.
- **Messages never show your site ID.** Errors shown in Repairs, on the Import status
  sensor and by actions name a statistic as, for example, "statistic e1_energy", and an
  Amber request as "/sites/…/usage". The Energy dashboard's Repairs item lists the
  statistics by the names its pickers show.
- **Bill card:** amounts read "−$5.37" (not "$-5.37"), and the previous bill's lines
  have their labels on the left and amounts on the right.
- **Guides:** new screenshots throughout.

## How to install the release candidate

1. **Take a Home Assistant backup** if you are on 2.1.1 or earlier.
2. In HACS, open **Amber Energy Dashboard (unofficial)**, open the menu, choose
   **Redownload**, open **Need a different version?**, pick **v2.2.0rc1**, and click
   **Download**.
3. Restart Home Assistant.

## Known limitations

- **No downgrade** to 2.1.1 without restoring a backup (see above).
- **Solar-aware cost:**
  - **battery discharge is valued at the import price.** Valuing it at the cost of the
    energy that charged the battery (the battery cost basis) is planned for 2.3;
  - the solar share is the whole house's for each 5-minute interval, not the appliance's
    own, and flows within an interval are netted;
  - your solar and grid meter sensors must agree; the calibration offset is not applied;
  - general-channel appliances only (not unconverted own energy sensors from 2.1);
  - when the feed-in price is negative, the solar part has a negative cost.
- **Today so far** is an estimate: general import and feed-in only (no controlled load),
  without the calibration offset (`offset_corrected` shows it with the offset, for
  comparison).
- **Calibration offset** needs at least a day of complete billed hours with the meter's
  statistics, so a new grid meter shows no value at first.
- **No evening retries in Recovery-only mode.**
- The **bill card** is an example YAML card that needs button-card. A built-in card is
  planned for 2.3.
- As in 2.1: the bill estimate runs a day behind and leaves out one-off charges; the export
  allowance table has Endeavour Energy N61 only; adjusted compensation starts with Amber's
  interval history (about three months).
