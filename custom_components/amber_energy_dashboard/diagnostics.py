"""Diagnostics, with the API key and NMI redacted."""

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant

from . import AmberConfigEntry
from .const import CONF_NMI

# The title is redacted too, because it contains the NMI.
TO_REDACT = {CONF_API_KEY, CONF_NMI, "title"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AmberConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    runtime = entry.runtime_data
    client = runtime.context.client
    rate_limit = client.last_rate_limit
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "statistics": [
            {
                "statistic_id": s.statistic_id,
                "metric": s.metric.value,
                "channel": s.channel,
                "unit": s.unit,
                "unit_class": s.unit_class,
            }
            for s in runtime.context.specs
        ],
        "api": {
            "requests_since_start": client.request_count,
            "last_rate_limit": None
            if rate_limit is None
            else {
                "limit": rate_limit.limit,
                "remaining": rate_limit.remaining,
                "reset": rate_limit.reset,
                "policy": rate_limit.policy,
            },
        },
        "last_import": runtime.last_import,
        "last_error": runtime.last_error,
        "store": runtime.manager.store.as_dict(),
        "schedule": {
            "mode": runtime.manager.schedule.mode,
            "fixed_times": [t.strftime("%H:%M") for t in runtime.manager.schedule.fixed_times],
            "next_run": runtime.manager.next_run.isoformat() if runtime.manager.next_run else None,
        },
        "status": runtime.manager.status,
    }
