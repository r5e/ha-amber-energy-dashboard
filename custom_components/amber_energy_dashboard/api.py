"""Thin async client for the Amber Electric public API.

Deliberately independent of the ``amberelectric`` SDK so it can never conflict with the
version pinned by HA core. It runs on a caller-supplied aiohttp session (in Home Assistant,
the shared session from ``async_get_clientsession``).

Every request has a timeout. Failures are mapped onto a small error hierarchy so callers
can decide between reauth (auth), backoff (rate limit) and retry-later (server, network).
Responses that do not have the documented shape raise ``AmberResponseError``: an
unexpected body is never silently treated as an empty result.

The rate limit (50 calls per 300 s) is shared with other clients and is counted separately
for ``/sites`` plus ``/usage`` and for ``/prices``. A ``RunBudget`` started with
``AmberClient.start_run`` caps the calls per counter in one run and stops the run once
``RateLimit-Remaining`` drops below a reserve.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
import logging
import time
from typing import Any, Final

import aiohttp

from .const import API_BASE_URL

_LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT: Final = aiohttp.ClientTimeout(total=30, connect=10)
_ERROR_BODY_LIMIT: Final = 300

COUNTER_SITES_USAGE: Final = "sites_usage"
COUNTER_PRICES: Final = "prices"
DEFAULT_MAX_CALLS_PER_COUNTER: Final = 25
DEFAULT_RATE_LIMIT_RESERVE: Final = 15


class AmberError(Exception):
    """Base class for all Amber API errors."""


class AmberConnectionError(AmberError):
    """The API could not be reached (DNS, TCP, TLS or similar)."""


class AmberTimeoutError(AmberConnectionError):
    """The request did not complete within its timeout."""


class AmberAuthError(AmberError):
    """The API key was rejected (HTTP 401 or 403). Start a reauth flow."""


class AmberRateLimitError(AmberError):
    """HTTP 429. Back off until ``retry_after`` seconds have passed."""

    def __init__(
        self, message: str, retry_after: float | None, rate_limit: RateLimit | None
    ) -> None:
        """Store the backoff hint alongside the message."""
        super().__init__(message)
        self.retry_after = retry_after
        self.rate_limit = rate_limit


class AmberServerError(AmberError):
    """HTTP 5xx. Retry at the next scheduled attempt."""

    def __init__(self, message: str, status: int) -> None:
        """Store the HTTP status alongside the message."""
        super().__init__(message)
        self.status = status


class AmberRequestError(AmberError):
    """Any other non-success HTTP status (for example 400 for an invalid date range)."""

    def __init__(self, message: str, status: int) -> None:
        """Store the HTTP status alongside the message."""
        super().__init__(message)
        self.status = status


class AmberResponseError(AmberError):
    """The response body was not the documented shape."""


class AmberBudgetExhaustedError(AmberError):
    """The run's call budget is spent. Stop now and resume at the next attempt.

    Raised before a request is sent, so no call is made. This is a planned stop, not a
    failure of the API.
    """

    def __init__(self, message: str, counter: str) -> None:
        """Store which rate-limit counter ran out."""
        super().__init__(message)
        self.counter = counter


@dataclass(frozen=True, slots=True)
class RateLimit:
    """Rate limit state reported by the API (IETF ``RateLimit-*`` headers)."""

    limit: int | None
    remaining: int | None
    reset: float | None
    """Seconds until the current window resets."""
    policy: str | None
    """Raw policy string, for example ``50;w=300``."""

    @classmethod
    def from_headers(cls, headers: Mapping[str, str]) -> RateLimit | None:
        """Parse the rate limit headers, or return None if none are present."""
        limit = _header_number(headers, "RateLimit-Limit")
        remaining = _header_number(headers, "RateLimit-Remaining")
        reset = _header_number(headers, "RateLimit-Reset")
        policy = headers.get("RateLimit-Policy")
        if limit is None and remaining is None and reset is None and policy is None:
            return None
        return cls(
            limit=None if limit is None else int(limit),
            remaining=None if remaining is None else int(remaining),
            reset=reset,
            policy=policy,
        )


@dataclass(slots=True)
class RunBudget:
    """Call budget for one import run, tracked per rate-limit counter.

    A request is refused (``AmberBudgetExhaustedError``, nothing sent) when the run has
    already made ``max_calls_per_counter`` calls on that counter, or when the last
    ``RateLimit-Remaining`` seen for that counter is below ``reserve`` and its window has
    not yet reset. The first response of a run therefore acts as the start-of-run check.
    """

    max_calls_per_counter: int = DEFAULT_MAX_CALLS_PER_COUNTER
    reserve: int = DEFAULT_RATE_LIMIT_RESERVE
    clock: Callable[[], float] = time.monotonic
    calls: dict[str, int] = field(default_factory=dict)
    """Requests sent in this run, per counter."""
    remaining: dict[str, int] = field(default_factory=dict)
    """Last ``RateLimit-Remaining`` seen, per counter."""
    _window_ends: dict[str, float] = field(default_factory=dict)

    def check(self, counter: str) -> None:
        """Raise if another request on ``counter`` would exceed the budget."""
        calls = self.calls.get(counter, 0)
        if calls >= self.max_calls_per_counter:
            raise AmberBudgetExhaustedError(
                f"run budget spent: {calls} calls on {counter}", counter
            )
        remaining = self.remaining.get(counter)
        if remaining is None or remaining >= self.reserve:
            return
        window_end = self._window_ends.get(counter)
        if window_end is not None and self.clock() >= window_end:
            # The window has reset since we looked; the old figure no longer applies.
            del self.remaining[counter]
            return
        raise AmberBudgetExhaustedError(
            f"rate limit reserve reached on {counter}: {remaining} remaining, "
            f"reserve {self.reserve}",
            counter,
        )

    def record_call(self, counter: str) -> None:
        """Count a request that is about to be sent."""
        self.calls[counter] = self.calls.get(counter, 0) + 1

    def record_rate_limit(self, counter: str, rate_limit: RateLimit) -> None:
        """Remember the rate limit state reported for ``counter``."""
        if rate_limit.remaining is None:
            return
        self.remaining[counter] = rate_limit.remaining
        if rate_limit.reset is None:
            self._window_ends.pop(counter, None)
        else:
            self._window_ends[counter] = self.clock() + rate_limit.reset


@dataclass(frozen=True, slots=True)
class Channel:
    """A metering channel on a site."""

    identifier: str
    """For example ``E1`` or ``B1``."""
    type: str
    """``general``, ``controlledLoad`` or ``feedIn``."""
    tariff: str | None


@dataclass(frozen=True, slots=True)
class Site:
    """An Amber site (one NMI)."""

    id: str
    nmi: str
    network: str | None
    status: str
    """``active``, ``pending`` or ``closed``."""
    active_from: date | None
    closed_on: date | None
    interval_length: int | None
    """Metering interval in minutes (5 or 30)."""
    channels: tuple[Channel, ...]


@dataclass(frozen=True, slots=True)
class UsageRecord:
    """One metered usage interval for one channel.

    ``cost`` is in cents and follows Amber's sign convention (feed-in is negative).
    ``start_time`` may carry a one-second offset; callers must floor it, not trust it.
    ``nem_time`` is the interval END in NEM time (UTC+10).
    """

    date: date
    """NEM calendar date the interval belongs to."""
    nem_time: datetime
    start_time: datetime
    end_time: datetime
    duration: int
    """Interval length in minutes."""
    channel_type: str
    channel_identifier: str
    kwh: float
    cost: float
    per_kwh: float
    spot_per_kwh: float | None
    renewables: float | None
    quality: str
    """``estimated`` or ``billable``."""
    descriptor: str | None
    spike_status: str | None
    tariff_information: Mapping[str, Any] | None


@dataclass(frozen=True, slots=True)
class PriceInterval:
    """One price interval (actual, current or forecast) for one channel.

    Prices are in c/kWh.
    """

    interval_type: str
    """``ActualInterval``, ``CurrentInterval`` or ``ForecastInterval``."""
    date: date
    nem_time: datetime
    start_time: datetime
    end_time: datetime
    duration: int
    channel_type: str
    per_kwh: float
    spot_per_kwh: float | None
    renewables: float | None
    descriptor: str | None
    spike_status: str | None
    estimate: bool | None
    tariff_information: Mapping[str, Any] | None

    @property
    def is_actual(self) -> bool:
        """Return True for a confirmed (historical) price."""
        return self.interval_type == "ActualInterval"


class AmberClient:
    """Minimal async client for the endpoints this integration needs."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_key: str,
        *,
        base_url: str = API_BASE_URL,
        timeout: aiohttp.ClientTimeout = DEFAULT_TIMEOUT,
    ) -> None:
        """Initialise the client. The session is owned by the caller."""
        self._session = session
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self.request_count = 0
        """Requests attempted by this client (for call budgeting and logs)."""
        self.last_rate_limit: RateLimit | None = None
        self.budget: RunBudget | None = None
        """Budget of the current run, if one was started."""

    def start_run(self, budget: RunBudget | None = None) -> RunBudget:
        """Start a run with a fresh budget (default limits unless one is given)."""
        self.budget = budget if budget is not None else RunBudget()
        return self.budget

    def end_run(self) -> None:
        """Stop applying a run budget."""
        self.budget = None

    async def async_get_sites(self) -> list[Site]:
        """Return all sites visible to the API key."""
        data = await self._request("/sites")
        return [_parse_site(item) for item in _expect_list(data)]

    async def async_get_usage(
        self,
        site_id: str,
        start_date: date,
        end_date: date,
        *,
        resolution: int | None = None,
    ) -> list[UsageRecord]:
        """Return usage records for an inclusive range of NEM dates.

        An empty list is a valid response: it means either "not published yet" or
        "outside retention". The API gives no way to tell the two apart.
        """
        params = _date_params(start_date, end_date, resolution)
        data = await self._request(f"/sites/{site_id}/usage", params)
        return [_parse_usage(item) for item in _expect_list(data)]

    async def async_get_prices(
        self,
        site_id: str,
        start_date: date,
        end_date: date,
        *,
        resolution: int | None = None,
    ) -> list[PriceInterval]:
        """Return price intervals for an inclusive range of NEM dates."""
        params = _date_params(start_date, end_date, resolution)
        data = await self._request(f"/sites/{site_id}/prices", params)
        return [_parse_price(item) for item in _expect_list(data)]

    async def _request(self, path: str, params: Mapping[str, str] | None = None) -> Any:
        """Perform a GET and return the decoded JSON body, or raise an AmberError."""
        url = f"{self._base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }
        counter = _counter_for(path)
        budget = self.budget
        if budget is not None:
            budget.check(counter)
            budget.record_call(counter)
        self.request_count += 1
        try:
            async with self._session.get(
                url, params=params, headers=headers, timeout=self._timeout
            ) as resp:
                rate_limit = RateLimit.from_headers(resp.headers)
                if rate_limit is not None:
                    self.last_rate_limit = rate_limit
                    if budget is not None:
                        budget.record_rate_limit(counter, rate_limit)
                if resp.status == 200:
                    try:
                        return await resp.json(content_type=None)
                    except ValueError as err:
                        raise AmberResponseError(f"{path}: response is not JSON") from err
                body = await _safe_text(resp)
                _raise_for_status(path, resp.status, body, resp.headers, rate_limit)
        except TimeoutError as err:
            raise AmberTimeoutError(f"{path}: request timed out") from err
        except aiohttp.ClientError as err:
            raise AmberConnectionError(f"{path}: {type(err).__name__}") from err
        raise AssertionError("unreachable")  # pragma: no cover


def _counter_for(path: str) -> str:
    """Return the rate-limit counter a request path is charged to."""
    return COUNTER_PRICES if path.endswith("/prices") else COUNTER_SITES_USAGE


def _raise_for_status(
    path: str,
    status: int,
    body: str,
    headers: Mapping[str, str],
    rate_limit: RateLimit | None,
) -> None:
    detail = f"{path}: HTTP {status}"
    if body:
        detail = f"{detail}: {body}"
    if status in (401, 403):
        raise AmberAuthError(detail)
    if status == 429:
        retry_after = _header_number(headers, "Retry-After")
        if retry_after is None and rate_limit is not None:
            retry_after = rate_limit.reset
        raise AmberRateLimitError(detail, retry_after, rate_limit)
    if status >= 500:
        raise AmberServerError(detail, status)
    raise AmberRequestError(detail, status)


async def _safe_text(resp: aiohttp.ClientResponse) -> str:
    try:
        text = await resp.text()
    except aiohttp.ClientError, UnicodeDecodeError:
        return ""
    return text.strip()[:_ERROR_BODY_LIMIT]


def _header_number(headers: Mapping[str, str], name: str) -> float | None:
    value = headers.get(name)
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _date_params(start_date: date, end_date: date, resolution: int | None) -> dict[str, str]:
    if end_date < start_date:
        raise ValueError(f"end_date {end_date} is before start_date {start_date}")
    params = {"startDate": start_date.isoformat(), "endDate": end_date.isoformat()}
    if resolution is not None:
        params["resolution"] = str(resolution)
    return params


# --- parsing -------------------------------------------------------------------------


def _expect_list(data: Any) -> list[Mapping[str, Any]]:
    if not isinstance(data, list):
        raise AmberResponseError(f"expected a JSON list, got {type(data).__name__}")
    for item in data:
        if not isinstance(item, Mapping):
            raise AmberResponseError(f"expected JSON objects, got {type(item).__name__}")
    return data


def _req(item: Mapping[str, Any], key: str) -> Any:
    try:
        value = item[key]
    except KeyError:
        raise AmberResponseError(f"missing field {key!r}") from None
    if value is None:
        raise AmberResponseError(f"field {key!r} is null")
    return value


def _str(item: Mapping[str, Any], key: str) -> str:
    value = _req(item, key)
    if not isinstance(value, str):
        raise AmberResponseError(f"field {key!r} is not a string")
    return value


def _opt_str(item: Mapping[str, Any], key: str) -> str | None:
    value = item.get(key)
    return value if isinstance(value, str) else None


def _num(item: Mapping[str, Any], key: str) -> float:
    value = _req(item, key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise AmberResponseError(f"field {key!r} is not a number")
    return float(value)


def _opt_num(item: Mapping[str, Any], key: str) -> float | None:
    if item.get(key) is None:
        return None
    return _num(item, key)


def _int(item: Mapping[str, Any], key: str) -> int:
    value = _num(item, key)
    if not value.is_integer():
        raise AmberResponseError(f"field {key!r} is not an integer")
    return int(value)


def _date(item: Mapping[str, Any], key: str) -> date:
    try:
        return date.fromisoformat(_str(item, key))
    except ValueError:
        raise AmberResponseError(f"field {key!r} is not an ISO date") from None


def _opt_date(item: Mapping[str, Any], key: str) -> date | None:
    if item.get(key) is None:
        return None
    return _date(item, key)


def _datetime(item: Mapping[str, Any], key: str) -> datetime:
    try:
        value = datetime.fromisoformat(_str(item, key))
    except ValueError:
        raise AmberResponseError(f"field {key!r} is not an ISO datetime") from None
    if value.tzinfo is None:
        raise AmberResponseError(f"field {key!r} has no UTC offset")
    return value


def _opt_mapping(item: Mapping[str, Any], key: str) -> Mapping[str, Any] | None:
    value = item.get(key)
    return value if isinstance(value, Mapping) else None


def _parse_site(item: Mapping[str, Any]) -> Site:
    channels = _req(item, "channels")
    if not isinstance(channels, list):
        raise AmberResponseError("field 'channels' is not a list")
    return Site(
        id=_str(item, "id"),
        nmi=_str(item, "nmi"),
        network=_opt_str(item, "network"),
        status=_str(item, "status"),
        active_from=_opt_date(item, "activeFrom"),
        closed_on=_opt_date(item, "closedOn"),
        interval_length=None
        if item.get("intervalLength") is None
        else _int(item, "intervalLength"),
        channels=tuple(
            Channel(
                identifier=_str(ch, "identifier"),
                type=_str(ch, "type"),
                tariff=_opt_str(ch, "tariff"),
            )
            for ch in _expect_list(channels)
        ),
    )


def _parse_usage(item: Mapping[str, Any]) -> UsageRecord:
    return UsageRecord(
        date=_date(item, "date"),
        nem_time=_datetime(item, "nemTime"),
        start_time=_datetime(item, "startTime"),
        end_time=_datetime(item, "endTime"),
        duration=_int(item, "duration"),
        channel_type=_str(item, "channelType"),
        channel_identifier=_str(item, "channelIdentifier"),
        kwh=_num(item, "kwh"),
        cost=_num(item, "cost"),
        per_kwh=_num(item, "perKwh"),
        spot_per_kwh=_opt_num(item, "spotPerKwh"),
        renewables=_opt_num(item, "renewables"),
        quality=_str(item, "quality"),
        descriptor=_opt_str(item, "descriptor"),
        spike_status=_opt_str(item, "spikeStatus"),
        tariff_information=_opt_mapping(item, "tariffInformation"),
    )


def _parse_price(item: Mapping[str, Any]) -> PriceInterval:
    estimate = item.get("estimate")
    return PriceInterval(
        interval_type=_str(item, "type"),
        date=_date(item, "date"),
        nem_time=_datetime(item, "nemTime"),
        start_time=_datetime(item, "startTime"),
        end_time=_datetime(item, "endTime"),
        duration=_int(item, "duration"),
        channel_type=_str(item, "channelType"),
        per_kwh=_num(item, "perKwh"),
        spot_per_kwh=_opt_num(item, "spotPerKwh"),
        renewables=_opt_num(item, "renewables"),
        descriptor=_opt_str(item, "descriptor"),
        spike_status=_opt_str(item, "spikeStatus"),
        estimate=estimate if isinstance(estimate, bool) else None,
        tariff_information=_opt_mapping(item, "tariffInformation"),
    )
