# v2.0.1: migration fix for stalled YAML kits

A bug-fix release for the **migration from the YAML kit**. Nothing else changes: imports,
statistics and settings work exactly as in 2.0.0.

Requires **Home Assistant 2026.9 or later**.

## What's fixed

- **A YAML kit that stopped importing no longer blocks the migration.** If your kit
  stopped some time ago (for example because its Amber key stopped working, so it kept
  adding 0 kWh each day), 2.0.0's parity check counted every day since then as a
  difference and refused, for example "FAILED: 32 daily totals differ". Days the kit has
  no data for are now treated as **gaps in the old data**, not disagreements: the
  integration already has those days, and they come from its own data. This applies
  whether the kit stopped for good or paused for a while and resumed, and also when it
  later caught up on the missed days in one lump. That lump is compared as one total.
  Parity still fails where both sides have data and disagree.
- **Clearer dry-run explanations.** The dry run and the result now say plainly what they
  found, for example "The YAML kit stopped importing after 2026-09-20; 16 days will come
  from the integration's own data". When there are real differences, they are counted by
  type before the first ones are listed, so a long list is no longer confusing.

## Who should update

- **Anyone migrating from either YAML kit** (the v1 kit or the advanced version),
  especially if the 2.0.0 dry run refused with "daily totals differ". Update first, then
  run the dry run again.
- If you have already migrated, or never used the YAML kit, nothing changes for you. You
  can update whenever convenient.

## How to update

1. In Home Assistant, open **HACS**, find **Amber Energy Dashboard (unofficial)**, and
   choose **Update** (or open its menu and choose **Redownload** if the update isn't shown
   yet).
2. **Restart Home Assistant.** Your statistics and settings are kept.
3. If you are migrating: open the integration's options (the cog), choose **Migrate from
   the YAML kit**, and check the dry run again. See the
   [migration guide](https://github.com/r5e/ha-amber-energy-dashboard/blob/v2.0.1/docs/MIGRATION.md).
   Take a full Home Assistant backup first.

Full details are in the [CHANGELOG](https://github.com/r5e/ha-amber-energy-dashboard/blob/v2.0.1/CHANGELOG.md).
