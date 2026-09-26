"""Tests for the statistics model (IDs and metadata)."""

from homeassistant.components.recorder.models import StatisticMeanType
import pytest

from custom_components.amber_energy_dashboard.statistics import (
    ChannelConfig,
    Metric,
    build_specs,
    statistic_id,
)


def test_ids_from_lowercased_site_and_channels_only() -> None:
    """IDs are lowercase site ID plus channel identifier; nothing else."""
    specs = build_specs(
        "01ABCDEF",
        [
            ChannelConfig("E1", "general"),
            ChannelConfig("E2", "controlledLoad"),
            ChannelConfig("B9", "feedIn"),
        ],
    )
    assert [(s.statistic_id, s.metric, s.channel, s.unit, s.unit_class) for s in specs] == [
        ("amber_energy_dashboard:01abcdef_e1_energy", Metric.ENERGY, "E1", "kWh", "energy"),
        ("amber_energy_dashboard:01abcdef_e1_cost", Metric.COST, "E1", "AUD", None),
        ("amber_energy_dashboard:01abcdef_e2_energy", Metric.ENERGY, "E2", "kWh", "energy"),
        ("amber_energy_dashboard:01abcdef_e2_cost", Metric.COST, "E2", "AUD", None),
        ("amber_energy_dashboard:01abcdef_b9_energy", Metric.ENERGY, "B9", "kWh", "energy"),
        ("amber_energy_dashboard:01abcdef_b9_compensation", Metric.COMPENSATION, "B9", "AUD", None),
        ("amber_energy_dashboard:01abcdef_net_cost", Metric.NET_COST, None, "AUD", None),
    ]
    meta = specs[0].metadata()
    assert meta == {
        "has_sum": True,
        "mean_type": StatisticMeanType.NONE,
        "name": "Amber general E1 energy",
        "source": "amber_energy_dashboard",
        "statistic_id": "amber_energy_dashboard:01abcdef_e1_energy",
        "unit_class": "energy",
        "unit_of_measurement": "kWh",
    }
    assert specs[5].name == "Amber feed-in B9 compensation"
    assert specs[3].name == "Amber controlled load E2 cost"


@pytest.mark.parametrize("site", ["", "site id", "01-ABC", "_x", "a__b"])
def test_invalid_site_ids_rejected(site: str) -> None:
    """A site ID that cannot form a valid statistic ID is rejected, not mangled."""
    with pytest.raises(ValueError, match="valid statistic ID"):
        statistic_id(site, "e1_energy")


def test_unsupported_or_duplicate_channels_rejected() -> None:
    """Unknown channel types and duplicate identifiers are errors."""
    with pytest.raises(ValueError, match="unsupported channel type"):
        build_specs("01abc", [ChannelConfig("X1", "battery")])
    with pytest.raises(ValueError, match="duplicate channel identifier"):
        build_specs("01abc", [ChannelConfig("E1", "general"), ChannelConfig("e1", "general")])
