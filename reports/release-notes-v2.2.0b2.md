# v2.2.0b2: Sources, Today so far, Rename site, skipped sites

The second **beta** of 2.2. A site's new **Sources** step finds your live price
automatically, and the site device gets **Today's cost so far (estimated)** and **Today's
feed-in so far (estimated)**. The calibration offset becomes the mean. A site your key no
longer sees is skipped instead of stopping the account. Sites can now be renamed.
Everything in [2.2.0b1](https://github.com/r5e/ha-amber-energy-dashboard/blob/v2.2.0b2/reports/release-notes-v2.2.0b1.md)
is included.

Requires **Home Assistant 2026.9 or later**. Your statistics, settings and entity IDs are
kept.

> [!WARNING]
> **Take a Home Assistant backup before updating from 2.1.1 or earlier.** At its first
> start, 2.2 moves the entry to the new account and site layout. Version 2.1.1 and earlier
> can't read that layout, so **there is no downgrade**: to go back to 2.1.1, restore the
> backup (Settings > System > Backups). Installing 2.1.1 over 2.2 leaves the integration
> unable to load ("Migration error"). Updating from 2.2.0b1 needs no further migration,
> but a backup is still a good idea with any beta.

## What's new since 2.2.0b1

### Sources

**Sources** is a new step in a site's settings. It lists the sensors the site's live
estimates can use. Billed figures always come from Amber's usage data and don't change.

- **General and feed-in live price:** found automatically. The step looks first for
  [Amber Express](https://github.com/hass-energy/amber-express), then for Home Assistant's
  built-in **Amber Electric** integration, set up for the same site, and shows what it
  found. Until you save the step, the sensors it finds are used. You can choose another
  sensor, or clear a field to use no live price for that channel. Prices in $/kWh, c/kWh
  and $/MWh are accepted.
- **Solar** (a power sensor and/or an energy sensor) and **battery** (energy charged and
  discharged), both optional: stored for a coming feature (solar- and battery-aware
  appliance cost). Nothing uses them yet.
- If you install a price integration later, reload this integration (or open and save
  the step) to pick it up.

### Today so far (estimated)

When a site has a **grid meter** (with its import on the general channel) and a **general
live price**, its device gets two new sensors:

- **Today's cost so far (estimated):** the meter's import since midnight (NEM time),
  priced every 5 minutes at the live price.
- **Today's feed-in so far (estimated)**, when the meter has an export sensor and there is
  a feed-in live price: the meter's export, priced at the live feed-in price.

They are estimates, for display only, and are **never written into long-term
statistics**. Once Amber publishes a day (normally the next morning), the
**`yesterday_estimate_vs_billed`** attribute compares that day's estimate with the bill.
With the bill estimate on, **Bill to date** gains **`including_today_estimated`**, and
`including_today_leaves_out` lists what it can't see (for example the controlled load).

Today so far uses the grid meter's sensors as mapped (map the raw meter sensors), and
doesn't apply the calibration offset, so it differs from the bill by about the offset
times the hours elapsed.

### Calibration offset is now the mean

The **Calibration offset** sensor now shows the **mean** of the hourly differences over
the 14-day window: the correction that makes the meter's totals match the bill. The
median and the spread are attributes (`median_w`, `iqr_w`). Hours where the meter's
statistics have gaps are still left out. Expect the value to move a little from what b1
showed.

### A site the key no longer sees is skipped

If the account's API key no longer sees one of its sites (for example a closed site), that
site is skipped and a Repairs item names it. The account's other sites keep running.
Re-authentication is needed only when the key sees none of the sites, and then accepts a
key that sees at least one.

### Rename site

**Rename site** in a site's settings renames the site's row on the integration page and
its device. Entity IDs don't change.

## How to install the beta

1. **Take a Home Assistant backup** (see the warning above).
2. In HACS, open **Amber Energy Dashboard (unofficial)**, open the menu, choose
   **Redownload**, open **Need a different version?**, pick **v2.2.0b2**, and click
   **Download**.
3. Restart Home Assistant.
4. Check:
   - Settings > Devices & services > Amber Energy Dashboard shows your site under the
     entry, and your Amber entities and Energy dashboard work as before.
   - The site's settings menu has **Sources** and **Rename site**. Sources shows the live
     price sensors it found.
   - With a grid meter and a live price, the site device shows the two Today so far
     sensors, which start counting from the next 5-minute mark.

## Known limitations

- **No downgrade** to 2.1.1 without restoring a backup (see above).
- **Today so far** is an estimate:
  - It covers the general channel's import and the feed-in only, not a controlled load.
  - It doesn't apply the calibration offset.
  - Energy used while Home Assistant was down is priced at the first price seen afterwards.
  - The first day after updating starts part-way through, so its
    `yesterday_estimate_vs_billed` is marked incomplete.
- **Solar and battery sources** are stored but not used yet.
- **Calibration offset** needs at least a day of complete billed hours with the meter's
  statistics, so a new grid meter shows no value at first.
- **No evening retries in Recovery-only mode.**
- Screenshots of the new setup steps (including Sources and Today so far) are still to
  come in the installation guide.
- As in 2.1: the bill estimate runs a day behind and leaves out one-off charges. The export
  allowance table has Endeavour Energy N61 only. Adjusted compensation starts with Amber's
  interval history (about three months).

Full details are in the
[CHANGELOG](https://github.com/r5e/ha-amber-energy-dashboard/blob/v2.2.0b2/CHANGELOG.md)
and the [installation guide](https://github.com/r5e/ha-amber-energy-dashboard/blob/v2.2.0b2/docs/INSTALL.md).
