# Installation guide: Amber Energy Dashboard (unofficial)

This guide walks through installing and setting up the integration. If you used the
earlier YAML kit, see the separate [migration guide](MIGRATION.md) once the integration is
set up. For a short overview, see the [README](../README.md).

**Contents**
1. [Before you start](#1-before-you-start)
2. [Install through HACS](#2-install-through-hacs)
3. [Add the integration](#3-add-the-integration)
4. [After setup](#4-after-setup)
5. [Settings](#5-settings)
6. [Add it to the Energy dashboard](#6-add-it-to-the-energy-dashboard)
7. [Migrating from the YAML kit](#7-migrating-from-the-yaml-kit) (separate guide)
8. [Troubleshooting and FAQ](#8-troubleshooting-and-faq)

---

## 1. Before you start

You need:

- **Home Assistant 2026.9 or later.**
- **[HACS](https://hacs.xyz/)** installed.
- **An Amber Electric API key** (see below).

### Creating an Amber API key

1. Sign in to Amber's website and open **For Developers** (https://app.amber.com.au/developers).
   Turn on **Developer mode** if it's off.
2. Click **Generate a new Token**, give it a recognisable name (for example
   "HomeAssistantAmberEnergyDash"), and click **Generate**.

   <img src="images/01-amber-generate-token.png" alt="Naming a new Amber token" width="637">

3. **Copy the token straight away** and store it in your password manager. Amber only shows
   it once; it disappears when you refresh the page. It starts with `psk_`.

   <img src="images/02-amber-token-created.png" alt="The new token, shown once" width="545">

   *(The token in this screenshot was deleted straight after it was taken. Never share a
   working token.)*

The integration works alongside Home Assistant's built-in **Amber Electric** integration,
which provides live prices. You can keep both.

## 2. Install through HACS

1. In HACS, open the **three-dot menu** (top right) and choose **Custom repositories**.

   <img src="images/03-hacs-custom-repositories.png" alt="The HACS menu" width="699">

2. Enter `https://github.com/r5e/ha-amber-energy-dashboard`, choose the type
   **Integration**, and click **Add**. Then close the dialog.

   <img src="images/04-hacs-add-repository.png" alt="Adding the custom repository" width="215">

3. Search HACS for **amber**, and open **Amber Energy Dashboard (unofficial)**. (Other
   Amber-related entries in the list are separate projects.)

   <img src="images/05-hacs-search.png" alt="Finding the repository" width="435">

4. Click **Download** (bottom right).

   <img src="images/06-hacs-download.png" alt="The repository page" width="694">

5. Click **Download**, then **restart Home Assistant** when prompted.

   HACS offers the latest release.¹

¹ *Pre-releases (release candidates) are not offered by default. To try one, open **Need a
different version?** in the download dialog and pick it from the list.*

## 3. Add the integration

1. Go to **Settings > Devices & services > Add integration**, and search for **Amber**.
   Choose **Amber Energy Dashboard (unofficial)**, not the built-in Amber Electric
   integration.

   <img src="images/08-add-integration.png" alt="Choosing the integration" width="523">

2. **Connect to Amber:** paste your API key and click **Submit**.

   <img src="images/09-api-key.png" alt="Entering the API key" width="329">

3. **Choose a site:** if your Amber account has more than one site, pick the one to set
   up. Each site becomes its own entry. The site is shown with its NMI and network.

   <img src="images/10-choose-site.png" alt="Choosing the site" width="331">

4. **Confirm channels:** the integration lists the metering channels it found, for
   example a general (import) channel and a feed-in (export) channel, each with its
   tariff code. Usage, cost and feed-in compensation are imported for each one.

   <img src="images/11-confirm-channels.png" alt="Confirming channels" width="336">

5. **Import schedule:** keep **Automatic** unless you have a reason not to. It picks a
   stable time for your installation each morning (between 06:30 and 08:30), with retries
   3 and 6 hours later. Once the day is imported, the retries are skipped without any API
   calls. **Fixed times** lets you choose up to 6 times yourself.

   If the Energy dashboard has no grid connection yet, this step also offers **Add to the
   Energy dashboard** (on by default). It adds a grid connection with this site's
   consumption and cost and, with feed-in, its return to grid and compensation (a
   controlled load gets a second grid connection), so you can skip section 6. An existing
   grid connection is never changed, and the option isn't shown then.

   <img src="images/12-import-schedule.png" alt="Import schedule" width="332">

6. **Name and assign:** optionally rename the device or assign it to an area, then click
   **Finish**.

   <img src="images/13-name-and-assign.png" alt="Name and assign" width="333">

The integration then imports your available history straight away (Amber keeps about 90
days). That takes a minute or two and about 15 API calls.

## 4. After setup

Open the integration's device to see its status:

<img src="images/device-page.png" alt="The device page after setup" width="716">

- **Import status** reads **Up to date** when everything is imported.
- **Last imported date** is normally yesterday, because Amber publishes each day's usage
  about a day later.
- **Days behind** is 0 when it's up to date.
- **Yesterday** sensors show the previous day's energy, cost and feed-in compensation per
  channel.
- **Next scheduled run** (under Diagnostic) shows when it will next check.
- **Run now** (under Controls) runs the import straight away, the same as a scheduled run.
  You rarely need it.
- **Re-check retention** (under Diagnostic) finds the oldest day Amber still holds again
  from scratch (at most 8 API calls). The daily check normally follows it by itself.
- With the **bill estimate** or the **export allowance** turned on (section 5), their
  sensors appear here too:

  <img src="images/new-sensors.png" alt="The sensors with the bill estimate and the export allowance on" width="291">
- **Download diagnostics** produces a file for bug reports. Your API key and NMI are
  redacted from it.

The integration's own page (Settings > Devices & services > Amber Energy Dashboard)
shows the entry for each site:

<img src="images/15-integration-page.png" alt="The integration page" width="758">

## 5. Settings

On the integration's page, click the **cog** next to your site's entry to open the
options:

<img src="images/options-menu.png" alt="The options menu" width="681">

**Import settings** contains:

<img src="images/17-import-settings.png" alt="Import settings" width="333">

- **Schedule:** Automatic or Fixed times, as during setup.
- **Usage mode:**
  - **Full** (default): imports usage and cost every day.
  - **Recovery-only:** imports nothing on a schedule. Use the `backfill` service to fill a
    date range on demand.
  - **Pricing-only:** writes no usage statistics, only prices and own-sensor cost.

  Switching mode never deletes statistics; ones no longer updated simply stop.
- **Price series:** optionally write an hourly price series per channel. Turning it on
  fetches the full retention window once (about 13 API calls).
- **Hourly fallback for own sensors:** see "Own energy sensors" below.
- **Patience days** and **Revision days:** how long to wait for missing days, and how long
  to re-check days Amber marked as estimated. The defaults suit almost everyone.

### Bill estimate (optional)

Choose **Bill estimate** in the options menu:

<img src="images/bill-estimate-step.png" alt="The bill estimate step" width="294">

Enter your **billing day**, the **daily supply charge**, the **Amber subscription** per
day, any **other daily charges** (all excluding GST), and the **GST rate** (10 %). See
[Finding these on your bill](#finding-these-on-your-bill) below. The device then shows
**Bill to date**, **Projected bill**, **Days into billing cycle** and **Average cost per
day** for the current cycle. The estimate runs a day behind, because only complete
imported days count, and it leaves out one-off charges such as card fees. Leave the
billing day empty to turn it off.

**Cost including fixed charges** (a checkbox in the same step) adds a statistic with your
import cost plus the fixed charges, for an Energy dashboard whose totals match the bill (see
section 6 for where to select it). It covers your whole history. If you change the charges
later, the new amounts apply from the day after the last imported day; earlier days keep
the old ones.

#### Finding these on your bill

<img src="images/bill-fields-explained.png" alt="Where each field comes from on an Amber bill" width="568">

- **Billing day:** the first day of your bill period. For "28 Aug – 27 Sep", enter
  **28**.
- **Daily supply charge** ($ per day, excluding GST): the **Network Daily Supply Charges**
  rate in your bill's **charges summary** (for example 1.0871). It **already includes
  metering**, so don't add the metering line separately (see the picture above).
- **Amber subscription** ($ per day, excluding GST): from your bill's **Amber Fees**
  section (for example 0.7471). **Enter 0 if your subscription is currently free** (for
  example a first-year offer), and update it when the offer ends (see the picture
  above).
- **Other daily charges** ($ per day, excluding GST): any other fixed daily charge on your
  bill. Optional; 0 by default.
- **GST:** 10 % by default. The bill adds GST to these charges; the integration does the
  same. Amber's usage figures already include GST, and export credits carry none.
- **Not included:** one-off charges, such as card payment fees.

### Export allowance (optional)

Some networks charge for exports in a midday window beyond a free allowance applied on the
bill (Endeavour Energy's N61: 8 kWh a day, over the billing period). Amber's usage data
charges every kWh in that window, so feed-in earnings look lower than on your bill. For N61
with a billing day set, the integration turns this on by itself; otherwise choose **Export
allowance** in the options menu and enter the allowance per day:

<img src="images/export-allowance-step.png" alt="The export allowance step, with N61 detected" width="416">

It adds the sensors **Export allowance used**, **Export allowance remaining** and **Export
charge after allowance**, uses the allowance in the bill estimate, and writes an
**adjusted compensation** statistic. To show the adjusted earnings in the Energy dashboard,
select it as the grid connection's compensation (section 6). Turning the allowance on (or
changing it) measures your imported days straight away, in the background, with a few API
calls; you don't need to press **Run now**.

### Own energy sensors (optional)

If you measure your own consumption (for example with CT clamps or smart plugs), click
**Add own energy sensor** on the integration's page. The integration then calculates that
sensor's cost at Amber's real 5-minute prices. A whole-house sensor can even be used as
the Energy dashboard's cost source.

## 6. Add it to the Energy dashboard

If you kept **Add to the Energy dashboard** on during setup (section 3), this is already
done. A day after the first import, if nothing in the Energy dashboard uses the
integration's statistics, a Repairs item **"The Energy dashboard isn't using Amber Energy
Dashboard"** appears. Its **Fix** adds them when there is no grid connection, points to
the [migration guide](MIGRATION.md) when the YAML kit is in use, or lets you dismiss it.

To set it up by hand, go to **Settings > Dashboards > Energy**, and in the **Electricity
grid** section, add or edit a grid connection. For cost and compensation, always choose
**Use an entity tracking the total costs** (or **the total received**), never "current
price". Choose the statistics like this (`<ch>` is the channel, for example `e1`):

| Field | Statistic | When |
|---|---|---|
| Grid consumption | general `…_<ch>_energy` | Always |
| Cost | general `…_<ch>_cost` | The default: your usage at Amber's billed prices |
| Cost | `…_<ch>_cost_incl_fixed` ("import cost including fixed charges") | If you turned it on under **Bill estimate** and want the dashboard's cost to match your bill. It already includes a controlled load's cost, so then leave the controlled-load connection's cost empty |
| Return to grid | feed-in `…_<ch>_energy` | Always, if you export |
| Compensation | feed-in `…_<ch>_compensation` | The default: your feed-in earnings as Amber's data pays them |
| Compensation | `…_<ch>_compensation_adjusted` ("compensation (export allowance applied)") | With the **Export allowance** on (two-way tariffs such as N61): matches the export credit on your bill |
| Controlled load | a second grid connection: controlled-load `…_<ch>_energy`, cost `…_<ch>_cost` | If you have a controlled load (leave its cost empty with the cost including fixed charges, as above) |

The optional statistics cover your whole history, so switching to them also changes how
past days are shown. "Add to the Energy dashboard" and the Repairs fix use the default
statistics; switch to the optional ones by hand. Give the connection a clear display name,
such as "Amber grid".

## 7. Migrating from the YAML kit

Only needed if you used the earlier YAML kit. See the separate
**[migration guide](MIGRATION.md)**: it carries your history across and fixes the kit's
feed-in sign, with a dry run and undo.

## 8. Troubleshooting and FAQ

**My feed-in earnings are lower than the export credit on my Amber bill.**
Amber's usage data applies your network's export charges to every interval. Some networks
offer a free export allowance (for example Endeavour Energy's two-way tariff N61, which has
8 kWh per day free before a midday export charge applies), and your bill applies that
allowance, but the usage data doesn't. So the dashboard can show lower feed-in earnings
than the bill. The **Export allowance** option (section 5) corrects for it. Import costs match the bill to the cent (they include GST; the bill adds GST
at the end).

**My dashboard total doesn't include supply and subscription charges.**
Amber's usage data covers usage only. The daily supply charge (network and metering) and
Amber's subscription are separate charges on your bill. The **Bill estimate** option
(section 5) adds them.

**Something else.**
Download diagnostics from the integration's device page, and open an issue on GitHub with
the file attached. Your API key and NMI are redacted automatically.
