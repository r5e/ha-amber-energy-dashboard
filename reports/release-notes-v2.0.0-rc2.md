# v2.0.0-rc2: Amber Energy Dashboard 2.0, release candidate 2

**Pre-release.** This is the second release candidate of version 2. Please try it and
[report problems](https://github.com/r5e/ha-amber-energy-dashboard/issues). Take a Home
Assistant backup first, especially before migrating from the YAML kit.

Version 2 is a **Home Assistant custom integration** that imports your actual metered
usage and the real per-interval cost from Amber Electric into Home Assistant's long-term
statistics, for the Energy dashboard. See the
[rc1 release notes](https://github.com/r5e/ha-amber-energy-dashboard/releases/tag/v2.0.0-rc1)
for the full feature list. Requires **Home Assistant 2026.9 or later**.

## What's new since rc1

- **New action `amber_energy_dashboard.probe_retention`.** It finds how far back Amber still
  holds your usage history, from scratch, in at most 8 API calls, and returns the result.
  If it fails, the date you had is kept.
- **Retention that reaches further back.** If Amber holds more history than the setup
  search covers, the integration now follows it back over the next days, within its usual
  call limits, and then checks it with 2 calls a day. Before, such a date was re-searched
  every day at a few extra calls.
- **Fewer false alarms in the YAML-kit migration scan.** The scan looks for corrupt rows
  in the old statistics before copying them:
  - energy: more than 100 kWh per hour between two rows. A large household's daily total,
    such as 150 kWh, is no longer flagged;
  - cost: more than 500 per hour between two rows. A real wholesale price spike hour
    (around $17.50/kWh at the market cap) is no longer flagged;
  - a cost that goes down is never flagged, since negative prices make it fall
    legitimately.

  The `99999` test value from the v1 README's recorder test is still caught.

## Upgrading

- **From rc1:** update in HACS and restart Home Assistant. Nothing else is needed; your
  statistics and settings are kept.
- **From a development build (2.0.0-dev):** the same. The stored settings upgrade
  themselves on the first start. This was checked on a test install with three months of
  real Amber data, upgraded from 2.0.0-dev5: the stored retention date was kept, and
  every existing statistics row was unchanged.
- **New installs and upgrades from the v1 YAML kit:** follow the
  [rc1 release notes](https://github.com/r5e/ha-amber-energy-dashboard/releases/tag/v2.0.0-rc1)
  and the README. In HACS, turn on **Show beta versions** for this repository to see
  release candidates.

## Known limitations

These are unchanged from rc1:
- Amber keeps about three months of usage history. Older days cannot be fetched, except
  from the YAML kit's own history through the migration.
- Legacy history from the kits has no controlled-load channel.
- The migration supports a site with one general channel and at most one feed-in channel.

## Changes

See [CHANGELOG.md](https://github.com/r5e/ha-amber-energy-dashboard/blob/v2.0.0-rc2/CHANGELOG.md).

*Unofficial community project; not affiliated with or endorsed by Amber Electric.*
