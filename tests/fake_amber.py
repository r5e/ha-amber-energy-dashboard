"""A fake Amber /usage endpoint for walk and discovery tests (synthetic data only)."""

from collections.abc import Callable, Iterable
from datetime import date, timedelta
from typing import Any

from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from .synthetic import SITE_ID, make_usage_day, site_json

BASE = "https://api.amber.com.au/v1"
USAGE_URL = f"{BASE}/sites/{SITE_ID}/usage"
SITES_URL = f"{BASE}/sites"


class FakeAmber:
    """Serves usage for days in [earliest, latest]; other days return []."""

    def __init__(self, earliest: date, latest: date) -> None:
        """Create the fake with a data range."""
        self.earliest = earliest
        self.latest = latest
        self.empty: set[date] = set()
        self.overrides: dict[date, list[dict[str, Any]]] = {}
        self.mutate: Callable[[date, list[dict[str, Any]]], None] | None = None
        self.remaining: int | None = None
        """If set, sent as RateLimit-Remaining (decremented per call)."""
        self.calls: list[tuple[date, date]] = []

    def has(self, day: date) -> bool:
        """True if the fake has data for ``day``."""
        return self.earliest <= day <= self.latest and day not in self.empty

    def records(self, start: date, end: date) -> list[dict[str, Any]]:
        """Records for the inclusive range, as the API would return them."""
        out: list[dict[str, Any]] = []
        day = start
        while day <= end:
            if self.has(day):
                recs = self.overrides.get(day) or make_usage_day(day)
                if self.mutate:
                    recs = [dict(r) for r in recs]
                    self.mutate(day, recs)
                out += recs
            day += timedelta(days=1)
        return out

    async def _handler(self, method: str, url, data: Any) -> AiohttpClientMockResponse:
        start = date.fromisoformat(url.query["startDate"])
        end = date.fromisoformat(url.query["endDate"])
        self.calls.append((start, end))
        headers = {}
        if self.remaining is not None:
            self.remaining -= 1
            headers = {
                "RateLimit-Limit": "50",
                "RateLimit-Remaining": str(self.remaining),
                "RateLimit-Reset": "200",
                "RateLimit-Policy": "50;w=300",
            }
        return AiohttpClientMockResponse(
            method, url, json=self.records(start, end), headers=headers
        )

    def install(
        self, aioclient_mock: AiohttpClientMocker, sites: Iterable[dict[str, Any]] | None = None
    ) -> None:
        """Register /sites and /usage on the mocker."""
        aioclient_mock.get(SITES_URL, json=list(sites) if sites is not None else [site_json()])
        aioclient_mock.get(USAGE_URL, side_effect=self._handler)
