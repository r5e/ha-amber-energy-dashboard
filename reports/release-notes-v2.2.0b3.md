# v2.2.0b3: solar-aware appliance cost, previous bill estimate

The third **beta** of 2.2. Appliances can now have a **solar-aware cost** that values the
energy your own solar covered at the feed-in price, the site gets a **Previous bill
estimate**, and there is an example **bill card** for your dashboard. Today so far now
starts by itself when the live price turns up after Home Assistant starts. Everything in
[2.2.0b2](https://github.com/r5e/ha-amber-energy-dashboard/blob/v2.2.0b3/reports/release-notes-v2.2.0b2.md)
and [2.2.0b1](https://github.com/r5e/ha-amber-energy-dashboard/blob/v2.2.0b3/reports/release-notes-v2.2.0b1.md)
is included.

Requires **Home Assistant 2026.9 or later**. Your statistics, settings and entity IDs are
kept.

> [!WARNING]
> **Take a Home Assistant backup before updating from 2.1.1 or earlier.** At its first
> start, 2.2 moves the entry to the new account and site layout. Version 2.1.1 and earlier
> can't read that layout, so **there is no downgrade**: to go back to 2.1.1, restore the
> backup (Settings > System > Backups). Installing 2.1.1 over 2.2 leaves the integration
> unable to load ("Migration error"). Updating from 2.2.0b1 or b2 needs no further
> migration, but a backup is still a good idea with any beta.

## What's new since 2.2.0b2

### Solar-aware appliance cost

An appliance's normal cost prices all of its energy at the import price Amber billed. But
when the sun is shining, part of that energy comes from your own solar: it costs nothing
to import, and what you give up is the feed-in price you would have earned by exporting
it. **Solar-aware cost** (per appliance, off by default) records that view as well:

- a second statistic, **"<name> cost (solar-aware)"**, next to the normal cost statistic
  (which doesn't change);
- a **Yesterday solar-aware cost** sensor on the appliance's device, with yesterday's
  split as attributes: `solar_kwh`, `grid_kwh`, `battery_kwh`, `solar_share`.

**How it works.** For every 5-minute interval, the house's load is worked out from your
grid meter's import and export and your solar sensor (and the battery's charge and
discharge, if you set those sensors). The load is covered by solar first, then the
battery, then the grid, and the appliance gets the same shares. Its solar part is valued
at that interval's **feed-in price**, the rest at the **import price**. With the export
allowance on (two-way tariffs), the feed-in value in the penalty window includes the
network's export charge refunded while you are within the allowance.

**What it needs:**
- a **solar sensor** in the site's **Sources** (solar energy produced is best; a solar
  power sensor works too);
- a **grid meter with import and export**;
- an appliance on the **general** channel.

**How to turn it on:**
- For a **new appliance:** tick **Solar-aware cost** in the **Add appliance** form.
- For an **existing appliance:** open the integration page, find the appliance's row and
  click its pencil (**Reconfigure**), tick **Solar-aware cost** and submit. The
  integration reloads.

The statistic starts from the earliest day all the sensors have statistics for. Days older
than about 10 days are worked out from hourly statistics and marked "lower precision", as
for the normal cost. If the solar sensor or the grid meter goes missing later, the
solar-aware cost waits, and after more than 2 days a **Repairs** item names what is
missing, so you can fix it before the days fall out of Amber's retention.

### Previous bill estimate

With the bill estimate on, the site gets **Previous bill estimate**: the last complete
billing cycle's total, with each line (usage, export credit, daily charges, GST included),
the cycle's dates and `complete` (true once every day of the cycle has been imported,
normally the morning after it ends) as attributes. If you made a template sensor of your
own for last cycle's total, this replaces it.

### A bill card for your dashboard

[`docs/cards/bill-card.yaml`](https://github.com/r5e/ha-amber-energy-dashboard/blob/v2.2.0b3/docs/cards/bill-card.yaml)
is an example card showing **This bill so far** (bill to date, including today's
estimate, projected bill, cycle dates) and the **Previous bill** with its lines. It needs
[button-card](https://github.com/custom-cards/button-card) from HACS (Frontend). See
[the cards guide](https://github.com/r5e/ha-amber-energy-dashboard/blob/v2.2.0b3/docs/cards/README.md)
for how to add it, and how to change the entity IDs for a second site.

### Today so far

- **Starts by itself.** If Amber Express's (or Amber Electric's) price sensors appear
  after this integration has started (or you install one later), the price is now found
  within a few minutes and **Today so far** starts without a reload. Until b2 it waited
  until you saved the Sources step.
- **A reload no longer loses the last 5 minutes** (for example after saving a setting).
- **Offset-corrected value, for comparison:** `offset_corrected` shows the estimate with
  the grid meter's calibration offset applied, and `yesterday_estimate_vs_billed` compares
  both with the bill. The sensor's value is unchanged; a few days of these figures will
  show which is closer.

## How to install the beta

1. **Take a Home Assistant backup** (see the warning above).
2. In HACS, open **Amber Energy Dashboard (unofficial)**, open the menu, choose
   **Redownload**, open **Need a different version?**, pick **v2.2.0b3**, and click
   **Download**.
3. Restart Home Assistant.
4. Check:
   - your Amber entities and the Energy dashboard work as before;
   - with the bill estimate on, the site device shows **Previous bill estimate**;
   - with Amber Express or Amber Electric set up, **Today so far** is running a few
     minutes after the restart (the log says "Live price found for …" if it was found
     late);
   - to try solar-aware cost, add your solar sensor in the site's **Sources**, then turn
     it on for an appliance (see above). Its first values appear after the next import
     run (or press **Run now**).

## Known limitations

- **No downgrade** to 2.1.1 without restoring a backup (see above).
- **Solar-aware cost:**
  - **Battery discharge is valued at the import price.** It is shown as its own share
    (`battery_kwh`), but valuing it at the cost of the energy that charged the battery
    (solar at the feed-in price, grid at the import price) is planned for **2.3**.
  - The solar share is the whole house's for each 5-minute interval, not the appliance's
    own, and flows within an interval are netted.
  - Your solar and grid meter sensors must agree with each other; the calibration offset
    is not applied.
  - General-channel appliances only (not a controlled load, and not unconverted own
    energy sensors from 2.1: convert them first).
  - When the feed-in price is negative, the solar part has a negative cost (using the
    energy saved you the export charge).
- **Today so far** is an estimate: general import and feed-in only (no controlled load),
  without the calibration offset (see `offset_corrected` for comparison).
- **Calibration offset** needs at least a day of complete billed hours with the meter's
  statistics, so a new grid meter shows no value at first.
- **No evening retries in Recovery-only mode.**
- Screenshots of the new steps (Sources, Today so far, solar-aware cost, the bill card)
  are still to come in the guides.
- As in 2.1: the bill estimate runs a day behind and leaves out one-off charges. The export
  allowance table has Endeavour Energy N61 only. Adjusted compensation starts with Amber's
  interval history (about three months).

Full details are in the
[CHANGELOG](https://github.com/r5e/ha-amber-energy-dashboard/blob/v2.2.0b3/CHANGELOG.md)
and the [installation guide](https://github.com/r5e/ha-amber-energy-dashboard/blob/v2.2.0b3/docs/INSTALL.md).
