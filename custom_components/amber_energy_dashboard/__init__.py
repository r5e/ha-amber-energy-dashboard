"""Amber Energy Dashboard (unofficial).

Imports Amber Electric usage and cost history into Home Assistant external statistics.

Milestone 1 scaffold: only the Amber API client exists so far. Setup, the config flow and
the importer arrive in Milestone 2.
"""

from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the integration (nothing to do until Milestone 2)."""
    return True
