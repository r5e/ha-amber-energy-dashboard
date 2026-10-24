"""A site's site-wide sources (DESIGN section 22, 2.2.0-dev3), set in the site settings'
"Sources" step and kept in the site's options:

- **Live price** for the site's general and feed-in channels: for live estimates only
  ("Today so far"); billed figures always come from Amber's usage data. A price that was
  never set is auto-detected: Amber Express (``amber_express``) first, then the core
  Amber Electric integration (``amberelectric``), matched by this site's Amber site ID in
  the price sensor's unique ID. A price set to none stays off. Both integrations give
  $/kWh, with the feed-in price positive when you are paid; c/kWh and $/MWh are
  converted.
- **Solar production**: a power and/or an energy sensor (for example an inverter's).
- **Battery**: charge and discharge energy sensors (solar-aware appliance cost, `solar.py`).
- **Today so far's grid meter**, when the site has several on the general channel.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er

from .const import (
    CHANNEL_FEED_IN,
    CHANNEL_GENERAL,
    CONF_CHANNELS,
    CONF_IMPORT_CHANNEL,
    CONF_SITE_ID,
    SUBENTRY_GRID_METER,
)

CONF_PRICE_GENERAL: Final = "live_price_general"
CONF_PRICE_FEED_IN: Final = "live_price_feed_in"
CONF_SOLAR_POWER: Final = "solar_power"
CONF_SOLAR_ENERGY: Final = "solar_energy"
CONF_BATTERY_CHARGE: Final = "battery_charge"
CONF_BATTERY_DISCHARGE: Final = "battery_discharge"
CONF_TODAY_METER: Final = "today_grid_meter"
PRICE_KEYS: Final = {CHANNEL_GENERAL: CONF_PRICE_GENERAL, CHANNEL_FEED_IN: CONF_PRICE_FEED_IN}
SOURCE_KEYS: Final = frozenset(
    {
        CONF_PRICE_GENERAL,
        CONF_PRICE_FEED_IN,
        CONF_SOLAR_POWER,
        CONF_SOLAR_ENERGY,
        CONF_BATTERY_CHARGE,
        CONF_BATTERY_DISCHARGE,
        CONF_TODAY_METER,
    }
)

AMBER_EXPRESS: Final = "amber_express"
AMBER_ELECTRIC: Final = "amberelectric"
CONFIGURED: Final = "configured"
_PRICE_IDS: Final = (
    (AMBER_EXPRESS, {CHANNEL_GENERAL: "{}_general_price", CHANNEL_FEED_IN: "{}_feed_in_price"}),
    (
        AMBER_ELECTRIC,
        {CHANNEL_GENERAL: "{}-current-general", CHANNEL_FEED_IN: "{}-current-feed_in"},
    ),
)
"""Where each integration's current price sensor is (its unique ID), in detection order."""
SOURCE_NAMES: Final = {AMBER_EXPRESS: "Amber Express", AMBER_ELECTRIC: "Amber Electric"}

_PRICE_UNITS: Final = {
    "$/kwh": 1.0,
    "aud/kwh": 1.0,
    "c/kwh": 0.01,
    "¢/kwh": 0.01,
    "$/mwh": 0.001,
    "aud/mwh": 0.001,
}
"""Accepted price units, and the factor to $/kWh."""


@dataclass(frozen=True, slots=True)
class PriceSource:
    """A live price sensor, and where it came from (configured, or the integration it was
    detected from)."""

    entity_id: str
    origin: str


@dataclass(frozen=True, slots=True)
class Sources:
    """A site's sources, as resolved at setup."""

    prices: dict[str, PriceSource] = field(default_factory=dict)
    """By channel type (general, feedIn); a channel without a price is absent."""
    solar_power: str | None = None
    solar_energy: str | None = None
    battery_charge: str | None = None
    battery_discharge: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """For diagnostics."""
        return {
            "prices": {
                channel: {"entity_id": p.entity_id, "origin": p.origin}
                for channel, p in self.prices.items()
            },
            "solar_power": self.solar_power,
            "solar_energy": self.solar_energy,
            "battery_charge": self.battery_charge,
            "battery_discharge": self.battery_discharge,
        }


def detect_prices(hass: HomeAssistant, site_id: str) -> dict[str, PriceSource]:
    """The current price sensors of the integrations that know this site, by channel type:
    Amber Express first, then Amber Electric. Disabled entities are left out."""
    wanted = {
        (platform, pattern.format(site_id).lower()): (channel, platform)
        for platform, patterns in _PRICE_IDS
        for channel, pattern in patterns.items()
    }
    hits: dict[tuple[str, str], str] = {}
    for entity in er.async_get(hass).entities.values():
        key = (entity.platform, entity.unique_id.lower())
        if entity.domain == "sensor" and key in wanted and not entity.disabled:
            hits[key] = entity.entity_id
    found: dict[str, PriceSource] = {}
    for key, (channel, origin) in wanted.items():  # in detection order
        entity_id = hits.get(key)
        if entity_id is not None and channel not in found:
            found[channel] = PriceSource(entity_id, origin)
    return found


def resolve(
    hass: HomeAssistant, site_id: str, options: Mapping[str, Any], channel_types: set[str]
) -> Sources:
    """The site's sources: a price set in the options (none turns it off), else the
    detected one, for each of the site's general and feed-in channels."""
    detected = detect_prices(hass, site_id)
    prices: dict[str, PriceSource] = {}
    for channel, key in PRICE_KEYS.items():
        if channel not in channel_types:
            continue
        if key in options:
            if options[key]:
                prices[channel] = PriceSource(options[key], CONFIGURED)
        elif channel in detected:
            prices[channel] = detected[channel]
    return Sources(
        prices,
        options.get(CONF_SOLAR_POWER) or None,
        options.get(CONF_SOLAR_ENERGY) or None,
        options.get(CONF_BATTERY_CHARGE) or None,
        options.get(CONF_BATTERY_DISCHARGE) or None,
    )


def price_factor(unit: str | None) -> float | None:
    """The factor from ``unit`` to $/kWh, or None for a unit that isn't a price per
    energy this integration knows."""
    return _PRICE_UNITS.get((unit or "").replace(" ", "").lower())


def price_from_state(state: State | None) -> float | None:
    """A price state in $/kWh, or None when it is missing, not a number or in an unknown
    unit."""
    if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        return None
    factor = price_factor(state.attributes.get("unit_of_measurement"))
    try:
        value = float(state.state)
    except ValueError:
        return None
    return None if factor is None else value * factor


def today_meters(entry: ConfigEntry, site: Mapping[str, Any]) -> list[ConfigSubentry]:
    """The site's grid meters whose import is priced on a general channel: the ones
    "Today so far" can use, in the order they were added."""
    from .sites import mapping_site_id  # noqa: PLC0415 - sites imports the manager

    general = {c["identifier"] for c in site[CONF_CHANNELS] if c["type"] == CHANNEL_GENERAL}
    return [
        sub
        for sub in entry.subentries.values()
        if sub.subentry_type == SUBENTRY_GRID_METER
        and mapping_site_id(entry, sub.data) == site[CONF_SITE_ID]
        and sub.data.get(CONF_IMPORT_CHANNEL) in general
    ]


def today_meter(
    entry: ConfigEntry, site: Mapping[str, Any], options: Mapping[str, Any]
) -> ConfigSubentry | None:
    """The grid meter "Today so far" reads: the one chosen in the options, else the
    first."""
    meters = today_meters(entry, site)
    chosen = options.get(CONF_TODAY_METER)
    return next((m for m in meters if m.subentry_id == chosen), meters[0] if meters else None)
