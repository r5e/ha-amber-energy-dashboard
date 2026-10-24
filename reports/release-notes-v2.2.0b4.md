# v2.2.0b4: solar-aware cost with inverters that sleep at night; diagnostics fix

The fourth **beta** of 2.2. Two fixes, plus the polish from the 2.2 release review.
Everything in [2.2.0b3](https://github.com/r5e/ha-amber-energy-dashboard/blob/v2.2.0b4/reports/release-notes-v2.2.0b3.md)
is included.

Requires **Home Assistant 2026.9 or later**. Updating from a 2.2 beta needs no migration.

> [!WARNING]
> **Take a Home Assistant backup before updating from 2.1.1 or earlier.** 2.2 moves the
> entry to the account and site layout, which 2.1.1 can't read: going back means
> restoring the backup. See the installation guide's
> [Upgrading from version 2.1](https://github.com/r5e/ha-amber-energy-dashboard/blob/v2.2.0b4/docs/INSTALL.md#9-upgrading-from-version-21).

## Fixed

### Solar-aware cost skipped every day

Most solar inverters (for example Fronius) go offline at night, so their sensors are
unavailable and Home Assistant records no statistics for those hours. b3 treated that as
missing data and skipped every day ("no complete statistics … from" your solar sensor).

Now, for the site's **solar sensor only**, a gap counts as **no production** when:
- the solar energy total is the same before and after it; or
- it is **at night**: from an hour before sunset to an hour after sunrise, at your Home
  Assistant's home location. It may end up to three hours after sunrise, for an inverter
  that wakes late on a dull or shaded morning;
- for a solar **power** sensor, also when it reads 0 W on both sides of the gap.

A gap that starts in daylight while the total changes (the inverter's connection dropped
while it was producing) can't be placed, and that day is still skipped. Grid meters and
appliances still need complete statistics.

**The days b3 skipped are worked out again** at the first import run after updating (or
press **Run now**), as long as Amber still holds them (about three months). Nothing else
is needed.

### Diagnostics showed a grid meter's name

The Sources, solar-aware and Today so far sections of the diagnostics file showed your
grid meter's name. It is now redacted, like the site's and other names.

## Also in this beta

- A move of Amber's retention boundary is no longer logged as a warning. On an account
  whose window rolls, it moved every day.
- Clearer wording:
  - the settings step is titled **Import settings**;
  - the schedule help mentions the evening retries;
  - texts say "the site's settings" and "grid meters and appliances";
  - the `probe_retention` action is shown as **Re-check retention**, like its button.
- The guides are rewritten for 2.2, with **Upgrading from version 2.1** in its own
  section.

## How to install the beta

1. In HACS, open **Amber Energy Dashboard (unofficial)**, open the menu, choose
   **Redownload**, open **Need a different version?**, pick **v2.2.0b4**, and click
   **Download**.
2. Restart Home Assistant.
3. With solar-aware cost on, press **Run now** (or wait for the next import). The
   appliance's solar-aware cost statistic then covers the days that were skipped.

Full details are in the
[CHANGELOG](https://github.com/r5e/ha-amber-energy-dashboard/blob/v2.2.0b4/CHANGELOG.md).
