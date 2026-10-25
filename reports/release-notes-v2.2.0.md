# v2.2.0: grid meters and appliances, solar-aware cost, Today so far, account and sites

Version 2.2 makes the integration entry your **Amber account**, with each site under it;
replaces own energy sensors with **grid meters** (with a calibration offset) and
**appliances** (with an optional **solar-aware cost**); adds **Today so far** estimates from
your grid meter and a live price, a **Previous bill estimate**, and evening retries when
Amber publishes a day late. It brings together the four betas (b1 to b4) and the release
candidate (rc1), which ran in production before this release.

Requires **Home Assistant 2026.9 or later**. Your statistics, settings and entity IDs are
kept.

> [!WARNING]
> **Take a Home Assistant backup before updating from 2.1.1 or earlier.** At its first
> start, 2.2 moves the entry to the new account and site layout. Version 2.1.1 and earlier
> can't read that layout, so **there is no downgrade**: to go back, restore the backup
> (Settings > System > Backups). Installing 2.1.1 over 2.2 leaves the integration unable
> to load ("Migration error"). Updating from a 2.2 beta needs no migration.

## What's new

### Your Amber account, with its sites

- **The entry is your Amber account** (its API key). Each site is a **Site** under it, with
  its own device, sensors and settings. Setup asks for a friendly **site name** (for
  example "Home"), and **Rename site** changes it later. The device's model line shows the
  network and the NMI's last four digits.
- **More than one site:** **Add site** sets up another site of the same account. The sites
  take turns and share Amber's rate limit. A site the key no longer sees (for example a
  closed one) is skipped with a Repairs item, and the others keep running.
- **Site settings** open from the cog next to the entry (it asks which site when there are
  several) or the pencil on the site's row: Import settings, **Sources**, Bill estimate,
  Export allowance, **Rename site**, **Refresh channels** (it replaces Reconfigure) and
  the YAML-kit migration.

### Grid meters and appliances

- A **grid meter** is a meter on the main supply: a CT clamp or energy monitor on the
  mains, or an inverter or battery system's grid meter. Its import is priced at Amber's
  billed rate for each 5-minute interval (general or controlled load), its export at the
  feed-in rate. Its device has **Import reconciliation**, **Export reconciliation** and a
  **Calibration offset**: the mean, over the last 14 complete billed days, of Amber's
  hourly net energy minus the meter's, in watts (the correction that makes the meter's
  totals match the bill). **Map the raw sensors**, not ones you have already corrected.
- An **appliance** is any single energy sensor (a smart plug, a circuit, a heat pump, an EV
  charger), priced the same way, with **Yesterday energy** and **Yesterday cost**.

### Solar-aware appliance cost

Per appliance, off by default. When the sun shines, part of an appliance's energy comes
from your own solar: it costs nothing to import, but you give up the feed-in price.
**Solar-aware cost** adds a second statistic, **"<name> cost (solar-aware)"**, and a
**Yesterday solar-aware cost** sensor: for every 5-minute interval the house's load is
worked out from the grid meter's import and export and your solar sensor (and the battery,
if set), covered by solar first, then the battery, then the grid; the appliance's solar
part is valued at the **feed-in price** (with the export allowance's refund while within
the allowance), the rest at the **import price**. It needs a solar sensor in **Sources**, a
grid meter with import and export, and an appliance on the general channel. Turn it on
when adding an appliance, or with an existing appliance's **Reconfigure**.

### Sources and Today so far

- **Sources** finds the live price for the general and feed-in channels from
  [Amber Express](https://github.com/hass-energy/amber-express) or Home Assistant's Amber
  Electric integration for the same site, also when they are set up later. It also holds
  optional solar and battery sensors.
- With a grid meter and a live price, the site's device gets **Today's cost so far
  (estimated)** and **Today's feed-in so far (estimated)**, priced every 5 minutes.
  Display only, never in statistics. Once Amber publishes a day,
  `yesterday_estimate_vs_billed` compares the estimate with the bill, and **Bill to date**
  gains `including_today_estimated`.

### Previous bill estimate and a bill card

With the bill estimate on, **Previous bill estimate** shows the last cycle's total with its
lines, dates and `complete`. An example
[bill card](https://github.com/r5e/ha-amber-energy-dashboard/blob/v2.2.0/docs/cards/README.md)
(needs button-card) shows the current cycle and the previous bill.

### Evening retries

When yesterday's usage still isn't published after the day's last attempt, the
integration retries every 3 hours until 22:00, and stops as soon as the day is imported.

## Updating from 2.1

1. **Take a Home Assistant backup** (see the warning above).
2. In HACS, open **Amber Energy Dashboard (unofficial)** and click **Update**.
3. Restart Home Assistant.

Then:
- Your site appears as **Site …1234** under the entry, with its device still named
  **Amber Energy Dashboard (site)**. Give it a name with **Rename site** in its settings.
  No entity ID changes.
- **Own energy sensors from 2.1 keep working.** A Repairs item, **"Convert own energy
  sensors to grid meters and appliances"**, converts them when you are ready: an import
  and a feed-in sensor become one grid meter, every other one an appliance. Statistics are
  kept, and the grid meter's reconciliation sensors keep the old entity IDs.
- **Repairs items you ignored in 2.1 come back** if they still apply (they are now kept per
  site).
- A YAML-kit migration done with 2.0 or 2.1 is kept, with its undo.

The [installation guide's section 9](https://github.com/r5e/ha-amber-energy-dashboard/blob/v2.2.0/docs/INSTALL.md#9-upgrading-from-version-21)
has the details and the new features to set up.

## Known limitations

- **No downgrade** to 2.1.1 without restoring a backup (see above).
- **Solar-aware cost:**
  - battery discharge is valued at the import price; valuing it at the cost of the energy
    that charged the battery is planned for 2.3;
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

Full details are in the
[CHANGELOG](https://github.com/r5e/ha-amber-energy-dashboard/blob/v2.2.0/CHANGELOG.md)
and the [installation guide](https://github.com/r5e/ha-amber-energy-dashboard/blob/v2.2.0/docs/INSTALL.md).
