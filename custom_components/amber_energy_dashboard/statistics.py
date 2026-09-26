"""Statistics model (DESIGN section 6).

Statistic IDs are built only from the lowercased Amber site ID and channel identifiers,
never from user input. Every statistic is an external statistic owned by this domain.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from homeassistant.components.recorder.models import StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import valid_statistic_id
from homeassistant.const import UnitOfEnergy

from .const import (
    CHANNEL_CONTROLLED_LOAD,
    CHANNEL_FEED_IN,
    CHANNEL_GENERAL,
    DOMAIN,
    SUPPORTED_CHANNEL_TYPES,
)

CURRENCY = "AUD"

_CHANNEL_LABELS = {
    CHANNEL_GENERAL: "general",
    CHANNEL_CONTROLLED_LOAD: "controlled load",
    CHANNEL_FEED_IN: "feed-in",
}


class Metric(StrEnum):
    """What a statistic measures."""

    ENERGY = "energy"
    """kWh for one channel."""
    COST = "cost"
    """AUD paid for one general or controlled load channel. Positive means paid."""
    COMPENSATION = "compensation"
    """AUD earned for one feed-in channel. Positive means earned."""
    NET_COST = "net_cost"
    """AUD owed for the whole site: Amber's signed cost summed over all channels."""


@dataclass(frozen=True, slots=True)
class ChannelConfig:
    """A metering channel as stored in the config entry."""

    identifier: str
    type: str
    tariff: str | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ChannelConfig:
        """Build from config entry data."""
        return cls(data["identifier"], data["type"], data.get("tariff"))


@dataclass(frozen=True, slots=True)
class StatisticSpec:
    """One external statistic written by the importer."""

    statistic_id: str
    metric: Metric
    channel: str | None
    """Channel identifier, or None for site-level statistics."""
    name: str
    unit: str
    unit_class: str | None

    def metadata(self) -> StatisticMetaData:
        """Return the recorder metadata. mean_type and unit_class are always set."""
        return StatisticMetaData(
            has_sum=True,
            mean_type=StatisticMeanType.NONE,
            name=self.name,
            source=DOMAIN,
            statistic_id=self.statistic_id,
            unit_class=self.unit_class,
            unit_of_measurement=self.unit,
        )


def statistic_id(site_id: str, suffix: str) -> str:
    """Return a statistic ID for a site, validated against HA's rules."""
    value = f"{DOMAIN}:{site_id.lower()}_{suffix.lower()}"
    if not valid_statistic_id(value):
        raise ValueError(f"cannot build a valid statistic ID from site {site_id!r}")
    return value


def build_specs(site_id: str, channels: Iterable[ChannelConfig]) -> list[StatisticSpec]:
    """Return every statistic for a site, in a stable order."""
    specs: list[StatisticSpec] = []
    seen: set[str] = set()
    for channel in channels:
        if channel.type not in SUPPORTED_CHANNEL_TYPES:
            raise ValueError(f"unsupported channel type {channel.type!r}")
        ident = channel.identifier.lower()
        if ident in seen:
            raise ValueError(f"duplicate channel identifier {channel.identifier!r}")
        seen.add(ident)
        label = f"Amber {_CHANNEL_LABELS[channel.type]} {channel.identifier}"
        specs.append(
            StatisticSpec(
                statistic_id(site_id, f"{ident}_energy"),
                Metric.ENERGY,
                channel.identifier,
                f"{label} energy",
                UnitOfEnergy.KILO_WATT_HOUR,
                "energy",
            )
        )
        if channel.type == CHANNEL_FEED_IN:
            specs.append(
                StatisticSpec(
                    statistic_id(site_id, f"{ident}_compensation"),
                    Metric.COMPENSATION,
                    channel.identifier,
                    f"{label} compensation",
                    CURRENCY,
                    None,
                )
            )
        else:
            specs.append(
                StatisticSpec(
                    statistic_id(site_id, f"{ident}_cost"),
                    Metric.COST,
                    channel.identifier,
                    f"{label} cost",
                    CURRENCY,
                    None,
                )
            )
    specs.append(
        StatisticSpec(
            statistic_id(site_id, "net_cost"),
            Metric.NET_COST,
            None,
            "Amber net cost",
            CURRENCY,
            None,
        )
    )
    return specs
