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
  Amber's confirmed per-interval prices.
- Optional: historical price series.
- A guided migration and clean-up path from the v1 YAML kit.
- Configured entirely in the UI. No YAML.

Live and forecast prices, and battery or device control, are out of scope. Use the core
Amber Electric integration (or similar) for those.

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
