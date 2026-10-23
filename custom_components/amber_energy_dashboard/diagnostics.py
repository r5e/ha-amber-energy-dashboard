"""Diagnostics, with the API key, the NMI and user-given names redacted."""

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_API_KEY, CONF_NAME
from homeassistant.core import HomeAssistant

from . import AmberConfigEntry, SiteRuntime
from .const import CONF_NMI, CONF_SITE_NAME

# The title is redacted too, because it may contain the NMI or the site's name; the site's
# name and the mappings' names because a user may have typed an address into them (2.2).
TO_REDACT = {CONF_API_KEY, CONF_NMI, CONF_SITE_NAME, CONF_NAME, "title"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AmberConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry (an Amber account) and each of its sites."""
    runtime = entry.runtime_data
    client = runtime.client
    rate_limit = client.last_rate_limit
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
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
        "sites": {sub_id: _site(site) for sub_id, site in runtime.sites.items()},
    }


def _site(site: SiteRuntime) -> dict[str, Any]:
    manager = site.manager
    return {
        "statistics": [
            {
                "statistic_id": s.statistic_id,
                "metric": s.metric.value,
                "channel": s.channel,
                "unit": s.unit,
                "unit_class": s.unit_class,
            }
            for s in site.context.specs
        ],
        "last_import": site.last_import,
        "last_error": site.last_error,
        "store": manager.store.as_dict(),
        "schedule": {
            "mode": manager.schedule.mode,
            "fixed_times": [t.strftime("%H:%M") for t in manager.schedule.fixed_times],
            "next_run": manager.next_run.isoformat() if manager.next_run else None,
        },
        "status": manager.status,
        "display_status": manager.display_status,
        "export_measurement": manager.last_export_measurement,
    }
