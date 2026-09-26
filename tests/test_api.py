"""Tests for the Amber API client, against anonymised recorded fixtures."""

from datetime import UTC, date, datetime, timedelta, timezone
import json
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.amber_energy_dashboard.api import (
    AmberAuthError,
    AmberClient,
    AmberConnectionError,
    AmberError,
    AmberRateLimitError,
    AmberRequestError,
    AmberResponseError,
    AmberServerError,
    AmberTimeoutError,
    RateLimit,
)

from .conftest import load_fixture, load_json_fixture

API_KEY = "psk_test_not_a_real_key"
SITE_ID = "01FAKESITE0000000000000000"
BASE = "https://api.amber.com.au/v1"
SITES_URL = f"{BASE}/sites"
USAGE_URL = f"{BASE}/sites/{SITE_ID}/usage"
PRICES_URL = f"{BASE}/sites/{SITE_ID}/prices"
DAY = date(2026, 9, 24)
AEST = timezone(timedelta(hours=10))

RATE_LIMIT_HEADERS = {
    "RateLimit-Policy": "50;w=300",
    "RateLimit-Limit": "50",
    "RateLimit-Remaining": "38",
    "RateLimit-Reset": "7",
}


@pytest.fixture
async def client(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> AmberClient:
    """Return a client on the (mocked) shared HA session."""
    return AmberClient(async_get_clientsession(hass), API_KEY)


def _usage_record(**overrides: Any) -> dict[str, Any]:
    record = load_json_fixture("usage_2026-09-24.json")[0]
    record.update(overrides)
    return record


# --- sites ---------------------------------------------------------------------------


async def test_get_sites(client: AmberClient, aioclient_mock: AiohttpClientMocker) -> None:
    """Sites and their channels parse from the recorded response."""
    aioclient_mock.get(SITES_URL, json=load_json_fixture("sites.json"))

    sites = await client.async_get_sites()

    assert len(sites) == 1
    site = sites[0]
    assert site.id == SITE_ID
    assert site.nmi == "FAKENMI000"
    assert site.status == "active"
    assert site.active_from == date(2025, 1, 1)
    assert site.closed_on is None
    assert site.interval_length == 5
    # Real accounts use identifiers such as E9/B9, not only E1/B1: never hardcode them.
    assert [(c.identifier, c.type, c.tariff) for c in site.channels] == [
        ("E9", "general", "N71"),
        ("B9", "feedIn", "N61"),
    ]


async def test_request_headers(client: AmberClient, aioclient_mock: AiohttpClientMocker) -> None:
    """The key is sent as a bearer token and JSON is requested."""
    aioclient_mock.get(SITES_URL, json=[])

    await client.async_get_sites()

    _method, _url, _data, headers = aioclient_mock.mock_calls[0]
    assert headers["Authorization"] == f"Bearer {API_KEY}"
    assert headers["Accept"] == "application/json"


# --- usage ---------------------------------------------------------------------------


async def test_get_usage_recorded_day(
    client: AmberClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """A full recorded NEM day parses into 2 x 288 typed records."""
    raw = load_json_fixture("usage_2026-09-24.json")
    aioclient_mock.get(USAGE_URL, json=raw)

    records = await client.async_get_usage(SITE_ID, DAY, DAY)

    assert len(records) == 576
    assert {r.date for r in records} == {DAY}
    assert {r.duration for r in records} == {5}
    assert {(r.channel_type, r.channel_identifier) for r in records} == {
        ("general", "E9"),
        ("feedIn", "B9"),
    }
    first = records[0]
    assert first.start_time == datetime(2026, 9, 23, 14, 0, 1, tzinfo=UTC)
    assert first.end_time == datetime(2026, 9, 23, 14, 5, tzinfo=UTC)
    assert first.nem_time == datetime(2026, 9, 24, 0, 5, tzinfo=AEST)
    assert first.nem_time == first.end_time  # nemTime is the interval END
    assert first.kwh == raw[0]["kwh"]
    assert first.cost == raw[0]["cost"]
    assert first.per_kwh == raw[0]["perKwh"]
    assert first.quality == "billable"
    assert first.tariff_information == {"period": "offPeak", "season": "nonSummer"}
    feed_in = next(r for r in records if r.channel_type == "feedIn")
    assert feed_in.tariff_information is None  # key absent for feed-in
    assert feed_in.per_kwh < 0


async def test_get_usage_query(client: AmberClient, aioclient_mock: AiohttpClientMocker) -> None:
    """Dates are sent as ISO strings; resolution only when requested."""
    aioclient_mock.get(USAGE_URL, json=[])

    await client.async_get_usage(SITE_ID, date(2026, 9, 17), DAY)
    await client.async_get_usage(SITE_ID, DAY, DAY, resolution=30)

    first_url = aioclient_mock.mock_calls[0][1]
    assert dict(first_url.query) == {"startDate": "2026-09-17", "endDate": "2026-09-24"}
    second_url = aioclient_mock.mock_calls[1][1]
    assert dict(second_url.query) == {
        "startDate": "2026-09-24",
        "endDate": "2026-09-24",
        "resolution": "30",
    }


async def test_get_usage_empty_is_valid(
    client: AmberClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """An empty list is returned as-is; interpreting it is the caller's job."""
    aioclient_mock.get(USAGE_URL, json=[])

    assert await client.async_get_usage(SITE_ID, DAY, DAY) == []


async def test_get_usage_rejects_inverted_range(
    client: AmberClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """An inverted range is a programming error and never reaches the API."""
    with pytest.raises(ValueError, match="before start_date"):
        await client.async_get_usage(SITE_ID, DAY, DAY - timedelta(days=1))
    assert aioclient_mock.call_count == 0
    assert client.request_count == 0


# --- prices --------------------------------------------------------------------------


async def test_get_prices_recorded_day(
    client: AmberClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """Historical prices parse as confirmed ActualIntervals."""
    aioclient_mock.get(PRICES_URL, json=load_json_fixture("prices_2026-09-24.json"))

    prices = await client.async_get_prices(SITE_ID, DAY, DAY)

    assert len(prices) == 576
    assert all(p.is_actual for p in prices)
    assert {p.interval_type for p in prices} == {"ActualInterval"}
    assert {p.channel_type for p in prices} == {"general", "feedIn"}
    assert prices[0].start_time == datetime(2026, 9, 23, 14, 0, 1, tzinfo=UTC)
    assert prices[0].estimate is None


async def test_usage_per_kwh_matches_prices(
    client: AmberClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """Recorded evidence for DESIGN section 4: usage perKwh equals the actual price."""
    aioclient_mock.get(USAGE_URL, json=load_json_fixture("usage_2026-09-24.json"))
    aioclient_mock.get(PRICES_URL, json=load_json_fixture("prices_2026-09-24.json"))

    usage = await client.async_get_usage(SITE_ID, DAY, DAY)
    prices = {
        (p.channel_type, p.start_time): p.per_kwh
        for p in await client.async_get_prices(SITE_ID, DAY, DAY)
    }

    for record in usage:
        assert record.per_kwh == prices[(record.channel_type, record.start_time)]
        # cost is kwh x perKwh, rounded by Amber to 4 decimal places (cents).
        assert record.cost == pytest.approx(record.kwh * record.per_kwh, abs=5.1e-5)


async def test_price_current_interval_estimate(
    client: AmberClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """A current interval is not actual and carries its estimate flag."""
    item = load_json_fixture("prices_2026-09-24.json")[0]
    item.update(type="CurrentInterval", estimate=True)
    aioclient_mock.get(PRICES_URL, json=[item])

    [price] = await client.async_get_prices(SITE_ID, DAY, DAY)

    assert not price.is_actual
    assert price.estimate is True


# --- rate limit headers --------------------------------------------------------------


async def test_rate_limit_headers_recorded(
    client: AmberClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """The latest RateLimit-* headers are exposed for budgeting."""
    aioclient_mock.get(SITES_URL, json=[], headers=RATE_LIMIT_HEADERS)

    await client.async_get_sites()

    assert client.last_rate_limit == RateLimit(limit=50, remaining=38, reset=7.0, policy="50;w=300")
    assert client.request_count == 1


def test_rate_limit_from_headers_absent_or_garbled() -> None:
    """Missing headers give None; unparsable values are ignored, not fatal."""
    assert RateLimit.from_headers({}) is None
    parsed = RateLimit.from_headers({"RateLimit-Remaining": "lots", "RateLimit-Limit": "50"})
    assert parsed == RateLimit(limit=50, remaining=None, reset=None, policy=None)


# --- HTTP error mapping --------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_errors(
    client: AmberClient, aioclient_mock: AiohttpClientMocker, status: int
) -> None:
    """401 and 403 raise AmberAuthError (403 is what an invalid key really returns)."""
    aioclient_mock.get(SITES_URL, status=status, text=load_fixture("error_forbidden.json"))

    with pytest.raises(AmberAuthError, match=f"HTTP {status}") as exc_info:
        await client.async_get_sites()

    assert "not authorized" in str(exc_info.value)
    assert API_KEY not in str(exc_info.value)


async def test_rate_limited_with_retry_after(
    client: AmberClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """429 exposes Retry-After as the backoff hint, plus the rate limit state."""
    aioclient_mock.get(
        USAGE_URL, status=429, text="Too Many Requests", headers={"Retry-After": "42"}
    )

    with pytest.raises(AmberRateLimitError) as exc_info:
        await client.async_get_usage(SITE_ID, DAY, DAY)

    assert exc_info.value.retry_after == 42.0
    assert exc_info.value.rate_limit is None


async def test_rate_limited_falls_back_to_reset(
    client: AmberClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """Without Retry-After, RateLimit-Reset is used as the backoff hint."""
    headers = {**RATE_LIMIT_HEADERS, "RateLimit-Remaining": "0", "RateLimit-Reset": "123"}
    aioclient_mock.get(USAGE_URL, status=429, headers=headers)

    with pytest.raises(AmberRateLimitError) as exc_info:
        await client.async_get_usage(SITE_ID, DAY, DAY)

    assert exc_info.value.retry_after == 123.0
    assert exc_info.value.rate_limit is not None
    assert exc_info.value.rate_limit.remaining == 0
    assert client.last_rate_limit == exc_info.value.rate_limit


async def test_rate_limited_without_hints(
    client: AmberClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """429 with no headers still raises, with no backoff hint."""
    aioclient_mock.get(USAGE_URL, status=429)

    with pytest.raises(AmberRateLimitError) as exc_info:
        await client.async_get_usage(SITE_ID, DAY, DAY)

    assert exc_info.value.retry_after is None


@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_server_errors(
    client: AmberClient, aioclient_mock: AiohttpClientMocker, status: int
) -> None:
    """5xx raises AmberServerError carrying the status."""
    aioclient_mock.get(USAGE_URL, status=status, text="upstream failure")

    with pytest.raises(AmberServerError) as exc_info:
        await client.async_get_usage(SITE_ID, DAY, DAY)

    assert exc_info.value.status == status


async def test_range_too_large(client: AmberClient, aioclient_mock: AiohttpClientMocker) -> None:
    """The recorded 422 for an over-long range raises AmberRequestError with the reason."""
    aioclient_mock.get(USAGE_URL, status=422, text=load_fixture("error_range_too_large.txt"))

    with pytest.raises(AmberRequestError, match="Maximum 7 days") as exc_info:
        await client.async_get_usage(SITE_ID, DAY - timedelta(days=30), DAY)

    assert exc_info.value.status == 422


async def test_error_body_is_truncated(
    client: AmberClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """A huge error body is not copied wholesale into the exception message."""
    aioclient_mock.get(USAGE_URL, status=400, text="x" * 10_000)

    with pytest.raises(AmberRequestError) as exc_info:
        await client.async_get_usage(SITE_ID, DAY, DAY)

    assert len(str(exc_info.value)) < 400


async def test_undecodable_error_body(
    client: AmberClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """An error body that cannot be decoded still maps the status correctly."""
    aioclient_mock.get(USAGE_URL, status=503, content=b"\xff\xfe\xfa")

    with pytest.raises(AmberServerError, match=r"HTTP 503$"):
        await client.async_get_usage(SITE_ID, DAY, DAY)


# --- transport errors ----------------------------------------------------------------


async def test_timeout(client: AmberClient, aioclient_mock: AiohttpClientMocker) -> None:
    """A timeout raises AmberTimeoutError, which is also a connection error."""
    aioclient_mock.get(USAGE_URL, exc=TimeoutError())

    with pytest.raises(AmberTimeoutError) as exc_info:
        await client.async_get_usage(SITE_ID, DAY, DAY)

    assert isinstance(exc_info.value, AmberConnectionError)
    assert client.request_count == 1


@pytest.mark.parametrize(
    "exc",
    [
        aiohttp.ClientConnectionError(),
        aiohttp.ServerDisconnectedError(),
        aiohttp.ClientPayloadError(),
    ],
)
async def test_network_errors(
    client: AmberClient, aioclient_mock: AiohttpClientMocker, exc: Exception
) -> None:
    """aiohttp transport failures raise AmberConnectionError."""
    aioclient_mock.get(USAGE_URL, exc=exc)

    with pytest.raises(AmberConnectionError):
        await client.async_get_usage(SITE_ID, DAY, DAY)


async def test_client_uses_timeout(hass: HomeAssistant) -> None:
    """Every request is sent with a timeout."""
    seen: dict[str, Any] = {}

    class _Session:
        def get(self, url: str, **kwargs: Any) -> Any:
            seen.update(kwargs)
            raise aiohttp.ClientConnectionError

    client = AmberClient(_Session(), API_KEY)  # type: ignore[arg-type]
    with pytest.raises(AmberConnectionError):
        await client.async_get_sites()

    timeout = seen["timeout"]
    assert isinstance(timeout, aiohttp.ClientTimeout)
    assert timeout.total is not None


# --- malformed bodies ----------------------------------------------------------------


async def test_non_json_body(client: AmberClient, aioclient_mock: AiohttpClientMocker) -> None:
    """A 200 with a non-JSON body is an error, never an empty result."""
    aioclient_mock.get(USAGE_URL, text="<html>maintenance</html>")

    with pytest.raises(AmberResponseError, match="not JSON"):
        await client.async_get_usage(SITE_ID, DAY, DAY)


@pytest.mark.parametrize("body", [{"data": []}, "text", None, 3])
async def test_body_not_a_list(
    client: AmberClient, aioclient_mock: AiohttpClientMocker, body: Any
) -> None:
    """A 200 whose JSON is not a list is an error."""
    aioclient_mock.get(USAGE_URL, text=json.dumps(body))

    with pytest.raises(AmberResponseError, match="expected a JSON list"):
        await client.async_get_usage(SITE_ID, DAY, DAY)


async def test_list_of_non_objects(
    client: AmberClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """A list containing non-objects is an error."""
    aioclient_mock.get(USAGE_URL, json=[1, 2])

    with pytest.raises(AmberResponseError, match="expected JSON objects"):
        await client.async_get_usage(SITE_ID, DAY, DAY)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"kwh": None}, "'kwh' is null"),
        ({"kwh": "0.041"}, "'kwh' is not a number"),
        ({"cost": True}, "'cost' is not a number"),
        ({"duration": 5.5}, "'duration' is not an integer"),
        ({"date": "24/09/2026"}, "'date' is not an ISO date"),
        ({"startTime": "yesterday"}, "'startTime' is not an ISO datetime"),
        ({"startTime": "2026-09-23T14:00:01"}, "'startTime' has no UTC offset"),
        ({"channelIdentifier": 9}, "'channelIdentifier' is not a string"),
    ],
)
async def test_malformed_usage_field(
    client: AmberClient,
    aioclient_mock: AiohttpClientMocker,
    overrides: dict[str, Any],
    message: str,
) -> None:
    """Any malformed required field rejects the whole response."""
    aioclient_mock.get(USAGE_URL, json=[_usage_record(), _usage_record(**overrides)])

    with pytest.raises(AmberResponseError, match=message):
        await client.async_get_usage(SITE_ID, DAY, DAY)


@pytest.mark.parametrize("field", ["kwh", "cost", "perKwh", "quality", "channelIdentifier"])
async def test_missing_usage_field(
    client: AmberClient, aioclient_mock: AiohttpClientMocker, field: str
) -> None:
    """A missing required field rejects the whole response."""
    record = _usage_record()
    del record[field]
    aioclient_mock.get(USAGE_URL, json=[record])

    with pytest.raises(AmberResponseError, match=f"missing field '{field}'"):
        await client.async_get_usage(SITE_ID, DAY, DAY)


async def test_optional_usage_fields(
    client: AmberClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """Optional fields may be absent or null."""
    record = _usage_record(spotPerKwh=None, renewables=None)
    for key in ("descriptor", "spikeStatus", "tariffInformation"):
        del record[key]
    aioclient_mock.get(USAGE_URL, json=[record])

    [parsed] = await client.async_get_usage(SITE_ID, DAY, DAY)

    assert parsed.spot_per_kwh is None
    assert parsed.renewables is None
    assert parsed.descriptor is None
    assert parsed.tariff_information is None


async def test_malformed_site(client: AmberClient, aioclient_mock: AiohttpClientMocker) -> None:
    """A site whose channels are not a list is rejected."""
    site = load_json_fixture("sites.json")[0]
    site["channels"] = "E9"
    aioclient_mock.get(SITES_URL, json=[site])

    with pytest.raises(AmberResponseError, match="'channels' is not a list"):
        await client.async_get_sites()


def test_error_hierarchy() -> None:
    """Callers can catch every client failure with one base class."""
    for cls in (
        AmberAuthError,
        AmberConnectionError,
        AmberTimeoutError,
        AmberRateLimitError,
        AmberRequestError,
        AmberResponseError,
        AmberServerError,
    ):
        assert issubclass(cls, AmberError)
