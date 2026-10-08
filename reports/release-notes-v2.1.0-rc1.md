# v2.1.0-rc1: bill estimate, export allowance, and setup improvements

A **release candidate** for 2.1. It adds an estimate of your Amber bill, corrects feed-in
earnings on two-way network tariffs with a free export allowance (such as Endeavour Energy
N61), and makes setup easier. It also fixes a 2.0.1 bug that removed the integration's
sensors when an own energy sensor was added.

Requires **Home Assistant 2026.9 or later**. Your statistics and settings are kept.

## What's new

- **Bill estimate:** **Bill to date**, **Projected bill**, **Days into billing cycle** and
  **Average cost per day** for your billing cycle, from Amber's billed usage plus your
  daily fixed charges. The line breakdown is in the attributes, and the `bill_estimate`
  action works for any recent cycle. Optionally, an **import cost including fixed
  charges** statistic, for an Energy dashboard whose cost totals match your bill.
- **Export allowance** (two-way network tariffs):
  - Amber's usage data charges every kWh you export in the midday window. Your bill
    charges only the exports beyond a free allowance (N61: 8 kWh a day, over the billing
    period).
  - The integration measures the charge from your data and writes **compensation (export
    allowance applied)**, which matches the export credit on your bill.
  - It shows **Export allowance used**, **Export allowance remaining** and **Export charge
    after allowance**, and uses the adjusted earnings in the bill estimate.
  - Checked against a real bill: September 2026, $5.37 against the bill's $5.38. The bill
    estimate came to $165.71 against $165.74 (the bill without a one-off card fee).
- **Buttons:** **Run now**, and **Re-check retention** (under Diagnostic).
- **Add to the Energy dashboard:** when you set up the integration and the Energy dashboard
  has no grid connection, one is added for you (you can turn this off). A Repairs item
  reminds you a day later if nothing in the Energy dashboard uses the statistics; its fix
  adds them.
- **Migration from the YAML kit:**
  - If the kit's data doesn't overlap the integration's (for example the kit stopped
    before the integration's first day), the migration explains why it can't check the
    old history and runs once you acknowledge that.
  - 2.0.1's handling of a kit that stopped importing is included.

## Fixed

- **Adding an own energy sensor removed the integration's other sensors (2.0.1).** After
  you mapped an own energy sensor, the import status, "yesterday" and other sensors
  disappeared, because they shared a device with the new reconciliation sensor.
  - Each own sensor now has its own device, "<name> (own sensor)", holding its
    **Reconciliation** sensor (same entity ID; the friendly name changes).
  - **After updating, the missing sensors come back with their old entity IDs.**
  - Home Assistant may log one warning about a device moving on the first start after
    the update; it does not repeat.

## Who should update

- Anyone who wants the bill estimate or the export allowance, especially on Endeavour
  Energy's N61 or a similar two-way tariff.
- **Anyone who has added an own energy sensor** in 2.0.0 or 2.0.1: this restores the
  missing sensors.
- This is a release candidate. If you prefer, wait for 2.1.0.

## How to install the release candidate

In HACS, open **Amber Energy Dashboard (unofficial)**, open the menu, choose **Redownload**,
open **Need a different version?**, pick **v2.1.0-rc1**, and click **Download**. Restart
Home Assistant. Your statistics, settings and history are kept.

## How to turn on the new features after updating

The bill estimate and the export allowance are **off until you configure them**.

1. **Bill estimate:**
   1. Go to **Settings > Devices & services > Amber Energy Dashboard**, click the cog of your
      site, and choose **Bill estimate**.
   2. Enter your **billing day** (the first day of each cycle on your bill), the **daily
      fixed charges excluding GST** as printed on your bill (network daily charge,
      metering, Amber subscription, other) and the **GST rate** (10 %).
   3. Tick **Also write cost including fixed charges** if you want that statistic.
   4. Submit. The bill sensors appear on the device.
2. **Export allowance:**
   - **On Endeavour Energy N61** it turns on by itself once the billing day is set (the
     integration detects the tariff from Amber). It measures your recent history once
     (about 13 API calls), at the next run or when you press **Run now**.
   - **On another two-way tariff,** choose **Export allowance** in the same menu, turn it
     on, and enter the free allowance per day (and the charged period's name, if it isn't
     `solarSponge`).
3. **Energy dashboard (optional):** to show the new figures there, go to **Settings >
   Dashboards > Energy**, edit the grid connection, and select:
   - **cost**: "import cost including fixed charges" instead of the plain cost. It already
     includes a controlled load's cost, so then leave the controlled-load connection's cost
     empty;
   - **compensation**: "compensation (export allowance applied)" instead of the plain
     compensation.

   Both cover your whole history. The
   [installation guide](https://github.com/r5e/ha-amber-energy-dashboard/blob/v2.1.0-rc1/docs/INSTALL.md#6-add-it-to-the-energy-dashboard)
   has a table of which to select, and when.

## Known limitations

- The bill estimate runs a day behind (only complete imported days count). It leaves out
  one-off charges such as card fees.
- The export allowance table has Endeavour Energy N61 only. Other networks are configured
  by hand until verified.
- Adjusted compensation needs Amber's interval data, so days older than Amber's history
  (about three months) are carried unadjusted.

Full details are in the
[CHANGELOG](https://github.com/r5e/ha-amber-energy-dashboard/blob/v2.1.0-rc1/CHANGELOG.md).
