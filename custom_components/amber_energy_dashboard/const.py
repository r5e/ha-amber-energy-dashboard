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

SERVICE_RUN_NOW: Final = "run_now"

# Schedule options (config entry options)
CONF_SCHEDULE_MODE: Final = "schedule_mode"
CONF_FIXED_TIMES: Final = "fixed_times"
SCHEDULE_AUTOMATIC: Final = "automatic"
SCHEDULE_FIXED: Final = "fixed"

# Automatic schedule defaults (local wall-clock time)
AUTO_WINDOW_START_MINUTES: Final = 6 * 60 + 30
"""First attempt window opens at 06:30 local."""
AUTO_WINDOW_LENGTH_MINUTES: Final = 120
"""...and closes at 08:30; each install gets a stable offset inside it."""
AUTO_RETRY_OFFSETS_MINUTES: Final = (180, 360)
"""Retries 3 h and 6 h after the first attempt."""
MAX_FIXED_TIMES: Final = 6

# Retention discovery
RETENTION_FALLBACK_DAYS: Final = 89
RETENTION_DISCOVERY_MAX_CALLS: Final = 8
RETENTION_BRACKET_DAYS: Final = 30

FETCH_WINDOW_DAYS: Final = 7
"""Inclusive days per usage call (the API accepts 8; one day of slack)."""
