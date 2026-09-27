# Amber Energy Dashboard (unofficial)

> **Status: under development (v2).** This branch is being rebuilt as a Home Assistant
> custom integration. It is not ready for use yet. The working v1 YAML kit is on the
> `main` branch, and a copy is kept in [`legacy/v1/`](legacy/v1/) on this branch.

A Home Assistant custom integration (installable through HACS) that imports your
**actual metered usage and cost** from Amber Electric into Home Assistant's long-term
statistics, so the Energy dashboard shows what Amber really billed, hour by hour.

This is an unofficial community project. It is not affiliated with or endorsed by
Amber Electric.

## Why

Home Assistant's core Amber Electric integration provides live and forecast **prices**.
Amber's metered **usage** (published about a day later) and the real per-interval cost of
that usage are not available anywhere in core. This integration fills that gap. It runs
alongside the core integration, not instead of it.

## Planned features

- Hourly grid import and export energy, cost and feed-in compensation history, imported as
  external statistics that the Energy dashboard can use directly.
- Automatic backfill of Amber's usage retention window (about 86 days) and automatic
  catch-up after outages.
- Handling of Amber's later revisions to estimated data.
- Controlled load support, detected automatically from your site's channels.
- Optional: cost of your own locally metered energy (CT clamps, smart plugs) priced with
  Amber's billed per-interval prices (see below).
- Optional: hourly price series.
- A guided migration and clean-up path from the v1 YAML kit.
- Configured entirely in the UI. No YAML.

Live and forecast prices, and battery or device control, are out of scope. Use the core
Amber Electric integration (or similar) for those.

## Usage modes

Chosen in the integration's options (default **Full**):

- **Full**: imports your Amber usage and cost every day.
- **Recovery-only**: imports nothing on a schedule. Use the `amber_energy_dashboard.backfill`
  service to import a date range when you need it, for example to cover an outage of
  your own metering.
- **Pricing-only**: writes no usage statistics; only own-sensor cost and the optional
  price series (below).

Switching modes never deletes statistics. Statistics a mode no longer updates simply stop.

## Own-sensor cost (for example CT clamps)

If you measure energy yourself (CT clamps on the mains, a smart plug, another
integration's cumulative energy sensor), you can price it at the rate Amber actually
billed you:

1. Open the integration and choose **Add own energy sensor**.
2. Pick the sensor (it must be an energy sensor with state class `total` or
   `total_increasing`) and the Amber channel whose price applies (general, controlled
   load or feed-in).

Each five-minute interval of your sensor's energy is multiplied by Amber's billed price for
that interval and recorded as `amber_energy_dashboard:<site>_own_<sensor>_cost` (AUD).
History older than about 10 days only has hourly sensor data; those days are priced at the
hour's average price and flagged as lower precision (an option can skip them instead).
Feed-in mappings are recorded as positive earnings.

In the Energy dashboard you can then use your own sensor as the grid consumption and this
statistic as the **entity tracking the total costs**, so the dashboard shows your own
meter priced at Amber's real rates. When a whole-house sensor is mapped to the general
channel, a **reconciliation** sensor shows the daily difference between your meter and
Amber's (in kWh and per cent), which catches CT calibration drift or a dropped sensor.

## Price series (optional)

An optional hourly price statistic per channel (AUD/kWh: mean, min and max of Amber's
billed five-minute prices). It is useful for charts, but an hourly **mean price is not a
cost rate**: your usage is not spread evenly across the hour, so multiplying hourly energy
by the mean price does not give your bill. Use the cost statistics for that.

Enabling the option fetches Amber's full retention window once to fill the history (about
13 API calls for 90 days); after that it costs no extra calls.

## Requirements

- Home Assistant 2026.9 or later.
- An Amber Electric API key, created on Amber's developer page (https://app.amber.com.au/developers).

## Installation

Not yet released. Installation instructions (HACS custom repository, then the
integration's config flow) will be added with the first release.

## Privacy

Your usage history is personal data. Diagnostics output from this integration will redact
your API key and NMI. Please check anything you attach to a bug report.

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
