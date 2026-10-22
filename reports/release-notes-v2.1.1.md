# v2.1.1: retention-boundary fix, own-sensor fixes and clearer names

A bug-fix release. The most important fix protects your history during a network or Amber
outage. It also fixes own energy sensors mapped to a feed-in channel, and gives the
integration page clearer names.

Requires **Home Assistant 2026.9 or later**. Your statistics, settings and entity IDs are
kept.

## What's fixed

- **A network or Amber outage no longer moves the retention boundary.** The integration
  records the oldest day Amber still holds (the *retention boundary*), and never fetches
  days older than that. If the daily check of the boundary failed (for example with a DNS
  or connection error) and the follow-up check failed too, 2.1.0 stored a fallback of
  "today − 89 days" in place of the measured boundary. That moved it forward by a day for
  each day the outage lasted. Days between the real boundary and the fallback could then
  be skipped without being fetched: for a new own-sensor mapping, a new price series or a
  backfill. Your already-imported history was never affected. Now a failed check always
  keeps the stored boundary and tries again later. A successful check measures it from
  scratch, so an install that was affected corrects itself at the next check that
  succeeds.

  **To check yours:** Settings > Devices & services > Amber Energy Dashboard > ⋮ >
  **Download diagnostics**, then look at `data.store.retention.method`. If it starts with
  `fallback (Amber…Error)`, the boundary was moved during an outage, and 2.1.1 re-measures
  it at its next run.
- **Own energy sensors on a feed-in channel** now get a **Reconciliation** sensor (your
  sensor's daily kWh minus Amber's billed export kWh, with the percentage and both totals
  as attributes) and their own device. 2.1.0 created neither, so the mapping had no
  device. Controlled-load mappings get one too.
- **Own-sensor devices:** a mapping's Reconciliation sensor sits on the mapping's device as
  soon as the mapping is added. An existing one that isn't is linked at the update.

## What's changed

- **The site's device is now "Amber Energy Dashboard (site)".** Home Assistant lists it
  under its own heading, "Devices that don't belong to a sub-entry"; the new name makes
  clear that it is the site's device. **Entity IDs don't change**, so automations and
  dashboards keep working. Friendly names now start with "Amber Energy Dashboard (site)".
- **The entry's default title is "Amber Energy Dashboard"**, without the NMI. With a
  second site, the titles show the NMI's last four digits, for example "Amber Energy
  Dashboard (…1234)". Entries still titled "Amber <NMI>" are renamed at the update; a
  title you chose yourself is kept.
- The installation guide explains the integration page's layout:
  [How the integration page is laid out](https://github.com/r5e/ha-amber-energy-dashboard/blob/v2.1.1/docs/INSTALL.md#how-the-integration-page-is-laid-out).

## Who should update

Everyone on 2.0.x or 2.1.0, especially if you use own energy sensors, or if your Home
Assistant's network or Amber's API is sometimes unavailable.

## How to update

1. In Home Assistant, open **HACS**, find **Amber Energy Dashboard (unofficial)**, and
   choose **Update** (or open its menu and choose **Redownload** if the update isn't shown
   yet).
2. **Restart Home Assistant.** Your statistics, settings and entity IDs are kept.

Full details are in the [CHANGELOG](https://github.com/r5e/ha-amber-energy-dashboard/blob/v2.1.1/CHANGELOG.md).
