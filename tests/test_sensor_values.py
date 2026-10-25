"""Sensor states: a monetary amount that shows as zero is 0.0, never "-A$0.00" (M22)."""

from types import SimpleNamespace

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import UnitOfEnergy
import pytest

from custom_components.amber_energy_dashboard.sensor import AmberSensor, AmberSensorDescription
from custom_components.amber_energy_dashboard.statistics import CURRENCY


def _sensor(value, **kwargs) -> AmberSensor:
    description = AmberSensorDescription(key="x", value_fn=lambda _: value, **kwargs)
    return AmberSensor(SimpleNamespace(data={}), "site", description)


MONEY = {
    "device_class": SensorDeviceClass.MONETARY,
    "native_unit_of_measurement": CURRENCY,
    "suggested_display_precision": 2,
}


@pytest.mark.parametrize(
    ("value", "expected"),
    [(-0.0, 0.0), (-0.0001, 0.0), (-0.0049, 0.0), (0.0049, 0.0), (-0.005, -0.005), (-5.37, -5.37)],
)
def test_monetary_negative_zero(value: float, expected: float) -> None:
    """An amount that rounds to zero at the display precision is a plain 0.0."""
    state = _sensor(value, **MONEY).native_value
    assert state == expected
    assert str(state) != "-0.0"


def test_other_values_untouched() -> None:
    """Energy, None and non-float amounts are passed through as they are."""
    energy = _sensor(
        -0.0,
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=3,
    )
    assert str(energy.native_value) == "-0.0"
    assert _sensor(None, **MONEY).native_value is None
    assert _sensor(0, **MONEY).native_value == 0
