# Amber Energy Dashboard (unofficial)

A Home Assistant custom integration, installed through HACS, that imports your **actual
metered usage and cost** from Amber Electric into Home Assistant's long-term statistics.
The Energy dashboard then shows what Amber really billed, hour by hour.

This is an unofficial community project. It is not affiliated with or endorsed by
Amber Electric.

> **Version 2.0.0.** Version 2 replaces the earlier YAML kit (v1) with a proper
> integration: set up in the UI, with no YAML at all. If you use the v1 kit, see the
> **[migration guide](docs/MIGRATION.md)**. The v1 kit itself is kept in
> [`legacy/v1/`](legacy/v1/).

## Why

Home Assistant's core Amber Electric integration provides live and forecast **prices**
only. Two things are not available anywhere in Home Assistant core:
- Amber's metered **usage**, which Amber publishes about a day later;
- the real per-interval **cost** of that usage.

This integration fills that gap. It runs **alongside** the core integration, not instead
of it: keep the core integration (or Amber Express) for live prices.

What you get:
- **Hourly history** of grid import (general and controlled load) and grid export
  (feed-in) energy, with cost and feed-in compensation. It is written as external
  statistics that the Energy dashboard uses directly.
- **Automatic backfill** of everything Amber still holds (about three months), and
  **automatic catch-up** after outages.
- **Revision handling:** estimated days are re-checked, and history is corrected when
  Amber revises them.
- **Controlled load** support, detected from your site's channels.
- **Own-sensor cost (optional):** your own meter (CT clamps, smart plugs) priced at
  Amber's billed rate for every 5-minute interval.
- **Price series (optional):** an hourly price series.
- **Migration** from the v1 YAML kit, with a dry run and undo.

Out of scope: live and forecast prices, and battery or device control.

## Requirements

- **Home Assistant 2026.9 or later**, with the recorder (on by default).
- **HACS**, to install and update the integration. A manual install also works.
- **An Amber Electric API key.** Create one on Amber's developer page:
  <https://app.amber.com.au/developers>, or in the Amber app under Settings > Developer
  mode.

## Installation and setup

**➡️ Follow the [installation guide](docs/INSTALL.md).** It walks through creating the
Amber API key, installing through HACS (as a custom repository), adding the integration,
the settings, and the Energy dashboard, step by step with screenshots.

In short: add `https://github.com/r5e/ha-amber-energy-dashboard` to HACS as a custom
repository of type **Integration**, download **Amber Energy Dashboard (unofficial)**,
restart, then add it under **Settings > Devices & services**. Setup asks for the API key,
the site, the metering channels (always taken from Amber; for example `E1` general, `E2`
controlled load, `B1` feed-in) and the schedule (**Automatic** is recommended). The first
run then imports everything Amber still holds, about three months; after that, each day
costs a few API calls.

**Manual install:** copy `custom_components/amber_energy_dashboard` into your
`/config/custom_components/`, then restart.

## What it creates

**Statistics created** (`<site>` is your Amber site ID in lower case, `<ch>` the channel):

| Statistic | Unit | Meaning |
|---|---|---|
| `amber_energy_dashboard:<site>_<ch>_energy` | kWh | Energy per hour, for every channel |
| `amber_energy_dashboard:<site>_<ch>_cost` | AUD | Cost per hour on general and controlled-load channels. Positive means paid |
| `amber_energy_dashboard:<site>_<ch>_compensation` | AUD | Feed-in earnings per hour. **Positive means earned**, as the Energy dashboard expects |
| `amber_energy_dashboard:<site>_net_cost` | AUD | All channels together, in Amber's sign: what you owe |

**Energy dashboard** ([guide, section 6](docs/INSTALL.md#6-add-it-to-the-energy-dashboard)):
under **Settings > Dashboards > Energy**, in the grid section, set:
- "Grid consumption" to the general channel's energy, with "Use an entity tracking the
  total costs" set to its cost;
- for controlled load, a second grid consumption with its own energy and cost;
- "Return to grid" to the feed-in energy, with "Use an entity tracking the total
  received" set to its compensation.

Do **not** use "current price" here: that multiplies usage by a live price, and gives the
approximate figures this integration exists to replace.

**Display sensors** (these are not statistics, so they never disturb the history):
- import status, last imported date, days behind, and next scheduled run;
- yesterday's energy, cost and compensation per channel.

**Services:**
- `amber_energy_dashboard.run_now`: run the import now;
- `probe_retention`: find Amber's retention boundary again from scratch (at most 8 API
  calls). Normally not needed, because the daily check follows the boundary;
- `import_day`: import one day;
- `backfill`: import a date range; see Recovery-only below;
- `migrate_v1`, `undo_migration` and `delete_legacy_statistics`: see the migration guide.

## How importing works

- Amber publishes a day's usage about a day later. Each morning the integration imports
  every complete day it does not have yet.
  - Amber's 5-minute records are grouped into full hours, on Amber's own calendar day
    (00:00 to 24:00 AEST, all year). Daylight saving can never create a duplicated or
    missing hour.
  - A day is written only when it is complete: every interval is present, with no
    duplicates.
  - Every write is read back and checked before the integration records the day as done.
- **After an outage,** the next run catches up on all the missing days. It fetches 7 days
  per API call and stays well inside Amber's rate limit (50 calls per 5 minutes, shared
  with your other Amber tools).
- **Retention:** Amber keeps roughly three months of usage history. The integration finds
  the earliest available date at setup, then checks it daily with 2 API calls. If the date
  moves, it follows it. Days older than that can no longer be fetched, and are skipped.
- **Revisions:** days that contain Amber *estimated* data are re-checked daily for 14 days
  (configurable). If Amber revises them, the history from that day on is rewritten, so
  every later total stays correct.
- **If a day stays empty** while later days have data, it is treated as a gap after 7
  separate days of trying (configurable). It is then skipped, so one missing day cannot
  block everything after it.

## Usage modes

You choose the mode in the integration's options (**Configure > Import settings**). The
default is **Full**.

- **Full:** imports your Amber usage and cost every day, as described above.
- **Recovery-only:** imports nothing on a schedule. Instead, call
  `amber_energy_dashboard.backfill` with a `start_date` and `end_date` whenever you need a
  range. For example, when your own metering is your main source, you can fill a period
  where it failed.
  - A range that overlaps existing history is rewritten, with every later total
    re-derived.
  - A range after the existing history is appended.
- **Pricing-only:** writes no usage statistics. Usage is still fetched for its prices, to
  drive own-sensor cost and the price series.

**Switching modes never deletes statistics.** Statistics that a mode does not update simply
stop, and resume where they left off when you switch back.

## Own-sensor cost (for example CT clamps)

If you measure energy yourself, you can price it at the rate Amber actually billed you.
Examples: CT clamps on the mains, a smart plug, or another integration's cumulative energy
sensor.

1. Open the integration and choose **Add own energy sensor**.
2. Pick the sensor. It must be an energy sensor with state class `total` or
   `total_increasing`.
3. Pick the Amber channel whose price applies: general, controlled load or feed-in.

**How the cost is worked out.** Each 5-minute interval of your sensor's energy is
multiplied by Amber's billed price for that interval. The result is summed per hour into
`amber_energy_dashboard:<site>_own_<sensor>_cost` (AUD).
- Feed-in mappings are recorded as **positive earnings**.
- Home Assistant keeps 5-minute data for only about 10 days. Older days are priced from
  hourly energy at the hour's average price, and are marked "lower precision". An option
  can skip them instead.
- History starts from the later of your sensor's first statistics and Amber's retention
  boundary.
- Each mapping appears as its own entry under the integration, and can be deleted there.
  Deleting a mapping keeps its statistic.

**Energy dashboard with your own meter (the CT-clamp use case).** In the grid section:
- set "Grid consumption" to **your own sensor**;
- under "Use an entity tracking the total costs", pick the **own-sensor cost statistic**.

The dashboard then shows your own meter's energy, in real time, priced at Amber's real
billed rates (the cost arrives a day later, when Amber publishes it). This was verified on
Home Assistant 2026.9: the Energy dashboard accepts the statistic and reports no issues.

**Reconciliation.** When a whole-house sensor is mapped to the general channel, a sensor
named "Reconciliation: <your sensor>" appears. It shows, for the most recent day both
have, your meter's kWh minus Amber's kWh (the percentage and both totals are attributes).
It catches CT calibration drift or a sensor that stopped reporting.

## Price series (optional)

This option writes an hourly price statistic per channel,
`amber_energy_dashboard:<site>_<ch>_price`. It is in AUD/kWh and holds the mean, minimum
and maximum of Amber's billed 5-minute prices, with Amber's sign (feed-in prices are
negative).

It is useful for charts, but an hourly **mean price is not a cost rate**. Your usage is
not spread evenly across the hour, so hourly energy × mean price does not give your bill.
Use the cost statistics for that.

Enabling the option fills the history by fetching Amber's full retention window once
(about 13 API calls for 90 days). After that it shares the daily fetch, at no extra cost.

## Migrating from the YAML kits

If you used the earlier YAML kit, the integration can take over its history and your
Energy dashboard settings. It works with both the published **v1 kit** (`legacy/v1/`) and
the unpublished "advanced" YAML version.

**➡️ Follow the [migration guide](docs/MIGRATION.md).** It covers the backup, the dry
run, running the migration, checking the result, undo, and removing the old kit, with
screenshots. Install the integration and let its first import finish first, and leave the
old kit running until then: the migration needs a few days where both have data, to
compare them.

The rest of this section explains what the migration checks and does, in more detail.

### What the dry run checks

The migration is under **Configure > Migrate from the YAML kit**, or the
`amber_energy_dashboard.migrate_v1` action, which is always a dry run unless you set
`dry_run: false` (and `confirm_backup: true`). The dry run changes nothing. It shows:

- **Which kit it found.** It recognises the kit by its entity names and uses the one with
  recent data. If it cannot tell (renamed entities, or two kits both recent), you pick the
  old statistics from lists.
- **Parity:** a comparison of the daily totals over the days both have. The advanced
  version must match exactly (cost within 1 cent a day). The v1 kit must be within
  0.01 kWh a day; its cost was approximate, so cost differences are only reported. If
  they do not match, the migration will not run.
- **Implausible rows** in the old statistics:
  - more energy between two rows than 100 kWh per hour between them;
  - more cost between two rows than 500 per hour between them (a real price spike hour
    stays well below this, and a cost that goes down is never flagged, since negative
    prices can do that);
  - an energy total that goes down;
  - a sudden reset.

  One example is the `99999` test value that the v1 README's recorder test suggested
  importing. Each row found is listed, and by default the migration refuses to run. If you
  agree they are corruption, tick "Leave the flagged rows out" (the `exclude_flagged`
  option). The dry run is then repeated without them, and the totals around them are
  re-derived.
- **What will be copied,** and the exact Energy dashboard settings before and after.
- Which automation will be turned off.

### What it does

1. **Copies your older history** (from before the integration's first day) into the new
   statistics. The Energy dashboard then shows one continuous series; the old and new
   totals join with no jump. Where both have data, the integration's own hourly data
   wins.
2. **Switches the Energy dashboard's grid source** to the new statistics: energy, cost and
   compensation. Any "current price" setting is cleared. Your old settings are saved
   first.
3. **Turns off** the kit's daily automation. It is never deleted.
4. **Lists everything else for you to remove by hand,** in the result and in a Repairs
   item: the helpers, scripts, YAML blocks (`rest:`, template sensors, `recorder:
   exclude`) and the `amber_api_key` secret if nothing else uses it. The Repairs item is
   re-checked when Home Assistant starts and after each scheduled import, lists only what
   is still there, and clears itself once everything is gone.

If the migration is interrupted, running it again resumes where it stopped.

**The compensation sign fix.** The Energy dashboard expects feed-in compensation to be
**positive when you earn money**. The advanced YAML version stored Amber's own sign,
which is negative when earned, so the dashboard showed earnings as a cost. The migration
flips that history to positive-when-earned. The v1 kit's compensation was created by the
dashboard itself, so it already has the right sign and is copied as it is.

**v1 kit notes.**
- v1 history is **daily totals**, not hourly, and it stays that way.
- The v1 kit had no real cost history. Its cost came from the dashboard's "current price"
  setting, applied to a sensor that never changes, so it recorded nothing. The migration
  detects this ("v1 cost history appears empty; not copied").
- Your new cost history therefore starts with the integration's own data, which covers
  about the last three months, at Amber's billed rates.

**Controlled load:** neither kit had a controlled-load channel. Controlled-load
statistics start with the integration's own data.

### The old statistics, and undo

- **Old statistics are kept** unless you delete them. They are optional to remove, so
  they do not keep the Repairs item open. When you are happy, you can remove them with
  `amber_energy_dashboard.delete_legacy_statistics` (`confirm: true`). After that, undo is
  no longer possible.
- **Undo migration** (in the integration's Configure menu, or the `undo_migration`
  action):
  - restores your saved Energy dashboard settings;
  - turns back on only the automations the migration turned off.
  - The copied older history stays in the new statistics; it is harmless.
- After an undo you can run the migration again.

## Troubleshooting and diagnostics

**The import status sensor** shows what the last run did:

| Status | Meaning |
|---|---|
| Up to date | All complete days are imported |
| Waiting for Amber data | Yesterday is not published yet. This is normal early in the morning; later attempts retry |
| Paused (rate limit budget) / Paused (rate limited) | The shared Amber rate limit was low. The next attempt continues |
| Amber unavailable | Network error or an Amber server error. The next attempt retries |
| Authentication failed | Home Assistant asks you to re-enter the API key (reauthentication) |
| Needs attention | See Repairs |

**Repairs items** (Settings > System > Repairs):

| Item | Meaning and what to do |
|---|---|
| Amber import is behind | The day's last attempt still left days missing. It clears itself once caught up |
| Amber usage data is incomplete | A day failed the completeness check on the day's last attempt. Nothing was written; the next attempt fetches it again, and the item clears once the day is complete |
| Amber reports a new metering channel | For example, a newly installed controlled load. Imports stop until you open the integration and choose **Reconfigure** to add it |
| Amber import stopped: marker and statistics disagree | The stored progress and the statistics disagree, for example after restoring an old database backup. Nothing is written until this is resolved; download diagnostics and open an issue |
| Amber import could not be verified | A day was written but did not read back as expected. Progress was not advanced; the next attempt rewrites the day |
| Finish removing the YAML kit | After a migration: the old kit items still present, to remove by hand. It is re-checked when Home Assistant starts and after each scheduled import, and clears itself once they are gone. The old statistics do not count; delete them later if you wish |

**Diagnostics.** Open the integration, then the three-dot menu, then **Download
diagnostics**. The file contains:
- the import progress, the per-day status and the last run's summary;
- the retention and revision details;
- the schedule;
- the API call counts and the last rate-limit reading.

Your **API key and NMI are redacted**. Your usage figures are personal data, so check
the file before you attach it to an issue.

**Debug logging:**
```yaml
logger:
  logs:
    custom_components.amber_energy_dashboard: debug
```

**Common questions** (more in the [installation guide's FAQ](docs/INSTALL.md#8-troubleshooting-and-faq)):
- *Feed-in earnings are lower than the export credit on my bill.* Amber's usage data
  applies your network's export charges to every interval. Some networks give a free
  export allowance instead, for example Endeavour Energy's two-way tariff N61, with
  8 kWh per day free before a midday export charge applies. Your bill applies that
  allowance, but the usage data does not, so the dashboard can show lower feed-in earnings
  than the bill. Import costs match the bill to the cent.
- *Supply and subscription charges are missing.* Amber's usage data covers usage only.
  Daily network supply, metering and Amber's subscription are separate charges on your
  bill.
- *Today and yesterday are missing.* Amber publishes each day about a day late. Yesterday
  normally appears in the morning.
- *The first days are missing.* Amber only keeps about three months, so anything older
  cannot be fetched. If you used the YAML kit, the migration copies its older history.
- *Why AUD and not cents?* Home Assistant statistics use your currency; the cents Amber
  reports are converted once, when written.

Please report problems at <https://github.com/r5e/ha-amber-energy-dashboard/issues>. Include
the diagnostics file, after checking it.

## Privacy

Your usage history is personal data. It stays in your Home Assistant; this integration
talks only to Amber's API.

## Credits and prior art

This project builds on ideas and lessons from earlier community work:

- [Tmbao/amberelectric-usages](https://github.com/Tmbao/amberelectric-usages): the same
  foundation (external statistics plus a config flow). Its feed-in compensation sign and
  per-channel-identifier keying are adopted here, and its limitations shaped much of this
  design.
- [danVnest/amberelectric-usage](https://github.com/danVnest/amberelectric-usage):
  yesterday's figures as live sensors, which showed the demand for simple "yesterday"
  display sensors.
- [melvanderwal/HA-Amber-Electric-Usage-Charts](https://github.com/melvanderwal/HA-Amber-Electric-Usage-Charts):
  a live cost estimate from your own power sensor times the live price, the approximate
  ancestor of the own-sensor cost feature here.
- [hass-energy/amber-express](https://github.com/hass-energy/amber-express) and
  [nickw444/ha-amberelectric](https://github.com/nickw444/ha-amberelectric): ideas for
  confirmation-aware polling and jittered scheduling.
- [madpilot/hass-amber-electric](https://github.com/madpilot/hass-amber-electric): the
  original Amber component. Its usage sensor was dropped during the merge into Home
  Assistant core, which is the gap this project fills.
- **ha-amber-energy-dashboard v1**: the YAML predecessor of this integration, from this
  same repository. It is preserved in [`legacy/v1/`](legacy/v1/) and is the reference for
  the migration tool.

## Licence

[MIT](LICENSE)
