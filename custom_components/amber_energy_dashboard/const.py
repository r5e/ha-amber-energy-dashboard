"""Constants for the Amber Energy Dashboard integration."""

from datetime import timedelta, timezone
from typing import Final

DOMAIN: Final = "amber_energy_dashboard"

API_BASE_URL: Final = "https://api.amber.com.au/v1"

CONF_SITE_ID: Final = "site_id"
CONF_NMI: Final = "nmi"
CONF_CHANNELS: Final = "channels"

SERVICE_IMPORT_DAY: Final = "import_day"
ATTR_DATE: Final = "date"
ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"

NEM_TZ: Final = timezone(timedelta(hours=10), "NEM")
"""National Electricity Market time: UTC+10 all year, no daylight saving."""

CHANNEL_GENERAL: Final = "general"
CHANNEL_CONTROLLED_LOAD: Final = "controlledLoad"
CHANNEL_FEED_IN: Final = "feedIn"
SUPPORTED_CHANNEL_TYPES: Final = (CHANNEL_GENERAL, CHANNEL_CONTROLLED_LOAD, CHANNEL_FEED_IN)
