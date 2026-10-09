# v2.1.0-rc2: retention self-heal, and bill charges as on your Amber bill

The second **release candidate** for 2.1. It fixes new Amber customers getting stuck on
"Waiting for Amber data", and simplifies the bill estimate's charges to match the Amber
bill. Everything in [rc1](https://github.com/r5e/ha-amber-energy-dashboard/blob/v2.1.0-rc2/reports/release-notes-v2.1.0-rc1.md)
is included.

Requires **Home Assistant 2026.9 or later**. Your statistics and settings are kept.

## Fixed

- **Stuck on "Waiting for Amber data" after joining Amber recently.** If the integration
  could not find the start of your Amber history at setup, it assumed about three months.
  It then waited, day by day, for usage from before you joined. Now:
  - it uses your site's start date from Amber as the earliest possible day;
  - a guess is re-checked the next day;
  - empty days at the very start of the history are skipped at once as soon as later days
    have data.

  **An install that is stuck recovers by itself at its next run**, with no action needed.
  (If you ran `probe_retention` to fix it, nothing more is needed.)

## Changed

- **Bill estimate charges, as on your Amber bill's charges summary** (all excluding GST):
  - **Daily supply charge:** the "Network Daily Supply Charges" rate. It already includes
    metering, so don't add the metering line again.
  - **Amber subscription**, per day, from the Amber Fees section. **Enter 0 if your
    subscription is currently free** (for example a first-year offer), and update it when
    the offer ends.
  - **Other daily charges**, if any (0 by default).

  If you set up the bill estimate with rc1, your values are **moved over automatically**:
  network + metering become the daily supply charge, and the subscription becomes the
  Amber subscription. The estimate is unchanged. Each field now says where to find it on
  the bill, and the installation guide has a new section,
  [Finding these on your bill](https://github.com/r5e/ha-amber-energy-dashboard/blob/v2.1.0-rc2/docs/INSTALL.md#finding-these-on-your-bill).

## How to install the release candidate

In HACS, open **Amber Energy Dashboard (unofficial)**, open the menu, choose **Redownload**,
open **Need a different version?**, pick **v2.1.0-rc2**, and click **Download**. Restart
Home Assistant.

## How to turn on the new 2.1 features after updating

The bill estimate and the export allowance are **off until you configure them**:
1. **Bill estimate:** cog > **Bill estimate**. Enter the billing day (the first day of the
   period your bill covers), the daily supply charge, the Amber subscription per day, any
   other daily charges, and GST (10 %). Optionally tick **Also write cost including fixed
   charges**.
2. **Export allowance:**
   - **On Endeavour Energy N61** it turns on by itself once the billing day is set.
   - **On another two-way tariff,** cog > **Export allowance**, then turn it on and enter
     the allowance per day.
3. **Energy dashboard (optional):** select "import cost including fixed charges" as the
   cost, and "compensation (export allowance applied)" as the compensation. See the
   [table in the installation guide](https://github.com/r5e/ha-amber-energy-dashboard/blob/v2.1.0-rc2/docs/INSTALL.md#6-add-it-to-the-energy-dashboard).

## Known limitations

As in rc1: the bill estimate runs a day behind and leaves out one-off charges; the export
allowance table has Endeavour Energy N61 only; adjusted compensation starts with Amber's
interval history (about three months).

Full details are in the
[CHANGELOG](https://github.com/r5e/ha-amber-energy-dashboard/blob/v2.1.0-rc2/CHANGELOG.md).
