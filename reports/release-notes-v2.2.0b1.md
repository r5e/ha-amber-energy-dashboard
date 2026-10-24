# v2.2.0b1: account and sites, grid meters and appliances, evening retries

The first **beta** of 2.2. The integration entry becomes your Amber account, with each site
under it; own energy sensors become **grid meters** (with a calibration offset) and
**appliances**; and a day Amber publishes late is retried in the evening. Everything
since [2.1.1](https://github.com/r5e/ha-amber-energy-dashboard/blob/v2.2.0b1/reports/release-notes-v2.1.1.md)
is included.

Requires **Home Assistant 2026.9 or later**. Your statistics, settings and entity IDs are
kept.

> [!WARNING]
> **Take a Home Assistant backup before updating.** At its first start, 2.2 moves the
> entry to the new account and site layout. Version 2.1.1 and earlier can't read that
> layout, so **there is no downgrade**: to go back to 2.1.1, restore the backup
> (Settings > System > Backups). Installing 2.1.1 over 2.2 leaves the integration unable
> to load ("Migration error").

## What's new

### Your Amber account, with its sites

- **The entry is now the Amber account** (its API key). Each site is a **Site** under it,
  and owns the site's device and entities.
- **Existing installs are migrated automatically.** The entry keeps its ID, and no entity
  ID, unique ID or statistic changes. If Home Assistant stops in the middle of the
  migration, the next start finishes it.
- **Site settings** (import settings, bill estimate, export allowance, the YAML-kit
  migration) now belong to the site. The cog asks which site when there are several, and
  the pencil on a site opens the same menu. **Refresh channels** replaces the entry's
  Reconfigure.
- **More than one site:** **Add site** sets up another site of the same account. The
  account's sites take turns and share Amber's rate limit. Services and actions take the
  site's device as **site**; `config_entry_id` still works for an account with one site.
- Every device now belongs to a site, grid meter or appliance, so the integration page no
  longer shows "Devices that don't belong to a sub-entry".

### Grid meters, with a calibration offset

A **grid meter** is a meter on the main supply: a CT clamp or energy monitor on the mains,
or an inverter or battery system's grid meter. Map its import sensor (priced on the
general or controlled load channel) and, optionally, its export sensor (priced at the
feed-in rate). Its device has:

- **Import reconciliation** and **Export reconciliation:** your meter's kWh minus Amber's
  billed kWh, for the latest day both have.
- **Calibration offset** (with import and export): for each hour of the last 14 complete
  billed days, Amber's net energy minus the meter's, in watts; the sensor shows the
  median. Hours where the meter's statistics have a gap are left out. A CT clamp
  typically shows a small, steady offset (tens of watts); a revenue-grade meter close to
  0 W. **Map the raw sensors**, not ones you have already corrected, or the correction
  feeds back into itself.

### Appliances

An **appliance** is any single energy sensor: a smart plug, a circuit, a heat pump, an EV
charger. It is priced on the general or controlled load channel, and its device has
**Yesterday energy** and **Yesterday cost**. Appliances are not reconciled.

### Site name

Setup shows the site's details (network, NMI ending, active from) and asks for a friendly
name, which names the site's device. A name you give later on Home Assistant's device page
is used too. Entity IDs don't change with it. The device's model line shows the network
and the NMI's last four digits, for example "Endeavour Energy · NMI …1234".

### Evening retries

When yesterday's usage still isn't published after the day's last attempt (for example
13:22), the integration retries every 3 hours until 22:00 (then 16:22 and 19:22), and stops as soon as the day is imported. Days that import normally get no
extra attempts, and a **Run now** that imports the day cancels the remaining retries.

## Converting your 2.1 own energy sensors

Own energy sensors added in 2.1 **keep working unchanged** after the update: the same
statistics, entity IDs and reconciliation sensors. They appear on the integration page as
**Own energy sensor (2.1)**.

A Repairs item, **"Convert own energy sensors to grid meters and appliances"**
(Settings > System > Repairs), converts them when you're ready. It shows the plan first,
then asks you to confirm:

- an import mapping (general or controlled load) and a feed-in mapping become **one grid
  meter**, named as you choose;
- every other mapping becomes an **appliance**, keeping its name.

The cost statistics and their history are kept, and the grid meter's reconciliation
sensors keep the old sensors' entity IDs. Appliances get Yesterday sensors in place of
reconciliation. You can also ignore the Repairs item and keep the old mappings.

**Repairs items you ignored in 2.1 are not carried over.** Repairs items are now kept per
site, so an item you ignored before the update appears again if it still applies, and can
be ignored again.

## How to install the beta

1. **Take a Home Assistant backup** (see the warning above).
2. In HACS, open **Amber Energy Dashboard (unofficial)**, open the menu, choose
   **Redownload**, open **Need a different version?**, pick **v2.2.0b1**, and click
   **Download**.
3. Restart Home Assistant.
4. Check: Settings > Devices & services > Amber Energy Dashboard shows your site under the
   entry, and your Amber entities and Energy dashboard work as before (no entity ID
   changes).

## Known limitations

- **No downgrade** without restoring a backup (see above).
- **A site your API key can no longer see** (for example a closed site) stops the whole
  account and asks for re-authentication, so with several sites it also stops the others.
  Per-site handling is planned.
- **A site can't be renamed on the integration page:** a site without a name is listed as
  "Site …1234" (the NMI's last four digits). You can rename its device, but Home Assistant
  has no way to rename the site entry itself yet. A "Rename site" step is planned.
- **Calibration offset** needs at least a day of complete billed hours with the meter's
  statistics, so a new grid meter shows no value at first, and the median settles over
  the 14-day window.
- **No evening retries in Recovery-only mode.**
- Screenshots of the new setup steps and integration page are still to come in the
  installation guide.
- As in 2.1: the bill estimate runs a day behind and leaves out one-off charges; the export
  allowance table has Endeavour Energy N61 only; adjusted compensation starts with Amber's
  interval history (about three months).

Full details are in the
[CHANGELOG](https://github.com/r5e/ha-amber-energy-dashboard/blob/v2.2.0b1/CHANGELOG.md)
and the [installation guide](https://github.com/r5e/ha-amber-energy-dashboard/blob/v2.2.0b1/docs/INSTALL.md).
