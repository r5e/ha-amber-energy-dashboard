"""Tests for the integration scaffold."""

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.amber_energy_dashboard.const import DOMAIN


async def test_setup_component(
    recorder_mock, enable_custom_integrations: None, hass: HomeAssistant
) -> None:
    """The integration loads (with its recorder dependency) and sets up cleanly."""
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    assert DOMAIN in hass.config.components
