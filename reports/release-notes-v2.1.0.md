# v2.1.0: bill estimate and export allowance

Version 2.1 adds an estimate of your Amber bill and support for the free export allowance
of two-way network tariffs (Endeavour Energy N61). It also fixes the 2.0.x problem of
own-sensor mappings removing the integration's other sensors, and an install stuck on
"Waiting for Amber data" after joining Amber recently.

Requires **Home Assistant 2026.9 or later**. Your statistics and settings are kept.

## What's new

- **Bill estimate** (cog > **Bill estimate**): enter your billing day and the daily charges
  from your Amber bill, and the device shows **Bill to date**, **Projected bill**, **Days
  into billing cycle** and **Average cost per day**. Each field says where to find it on
  the bill; the installation guide has an annotated bill:
  [Finding these on your bill](https://github.com/r5e/ha-amber-energy-dashboard/blob/v2.1.0/docs/INSTALL.md#finding-these-on-your-bill).
  The `bill_estimate` action returns the estimate for any recent cycle.
- **Import cost including fixed charges** (optional, in the same step): a statistic with
  your import cost plus the fixed charges, for an Energy dashboard whose totals match the
  bill. It covers your whole history, with no API calls.
- **Export allowance** (cog > **Export allowance**): some networks charge for exports in a
  midday window, but only beyond a free allowance applied on the bill. Amber's usage data
  charges every kWh in that window, so feed-in earnings look lower than on the bill. The
  integration measures that charge from your data and refunds it within the allowance:
  - sensors **Export allowance used**, **Export allowance remaining** and **Export charge
    after allowance**;
  - a statistic **compensation (export allowance applied)**, which matches the export
    credit on your bill;
  - the bill estimate uses it.

  Endeavour Energy N61 is detected automatically. Turning it on measures your imported
  days straight away, in the background; you don't need to press **Run now**.
- **Run now** and **Re-check retention** buttons on the device.
- **Add to the Energy dashboard** at the end of setup (when the Energy dashboard has no
  grid connection yet), and a Repairs item if nothing in the Energy dashboard uses the
  integration's statistics a day after the first import.
- **Migration:** when the YAML kit's data can't be compared with the integration's (no
  overlapping days), the dry run explains why, and the migration runs only with an
  explicit acknowledgement.

## Fixed

- **Own-sensor mappings removed the integration's other sensors** (2.0.x): adding an
  own-sensor mapping could make the import status, "yesterday" and other sensors
  disappear. Each mapping now has its own device. After updating, the removed sensors come
  back with their old entity IDs.
- **Stuck on "Waiting for Amber data" after joining Amber recently:** the integration now
  uses your site's start date from Amber, re-checks a guessed history start the next day,
  and skips empty days at the very start of the history. **A stuck install recovers by
  itself at its next run.**
- The import settings step no longer drops options set in other steps.

## Who should update

- **Everyone on 2.0.x who uses own energy sensors**: this fixes the missing sensors.
- **Anyone stuck on "Waiting for Amber data"** after joining Amber recently.
- Anyone who wants the bill estimate or, on Endeavour Energy N61, feed-in earnings that
  match the bill.
- Otherwise nothing changes in your imports; update when convenient. If you tried a
  release candidate, 2.1.0 carries over its settings (rc1's bill charges are moved to the
  new fields automatically, with the same estimate).

## How to update

In HACS, open **Amber Energy Dashboard (unofficial)** and click **Update** (or open the
menu and choose **Redownload**). Restart Home Assistant.

## How to turn on the new features

The bill estimate and the export allowance are **off until you configure them**:
1. **Bill estimate:** cog > **Bill estimate**. Enter the billing day (the first day of the
   period your bill covers), the daily supply charge, the Amber subscription per day (0 if
   it is currently free), any other daily charges, and GST (10 %). Optionally turn on
   **Also write cost including fixed charges**.
2. **Export allowance:**
   - **On Endeavour Energy N61** it turns on by itself once the billing day is set.
   - **On another two-way tariff,** cog > **Export allowance**, then turn it on and enter
     the allowance per day.
3. **Energy dashboard (optional):** select "import cost including fixed charges" as the
   cost, and "compensation (export allowance applied)" as the compensation. See the
   [table in the installation guide](https://github.com/r5e/ha-amber-energy-dashboard/blob/v2.1.0/docs/INSTALL.md#6-add-it-to-the-energy-dashboard).

## Known limitations

- The bill estimate runs a day behind (only complete imported days count) and leaves out
  one-off charges such as card fees.
- The export allowance table has Endeavour Energy N61 only; other tariffs are configured
  by hand.
- The adjusted compensation's refunds start with Amber's interval history (about three
  months); older days are carried unchanged.

Full details are in the
[CHANGELOG](https://github.com/r5e/ha-amber-energy-dashboard/blob/v2.1.0/CHANGELOG.md).
