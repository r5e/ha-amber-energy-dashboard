"""import_day service tests: Guard 3, append-only safety, baselines, DST. All data synthetic."""

from collections.abc import Callable
import copy
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
import math
from typing import Any
from unittest.mock import patch

from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_metadata,
    list_statistic_ids,
    statistics_during_period,
)
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.amber_energy_dashboard import importer
from custom_components.amber_energy_dashboard.const import (
    CONF_CHANNELS,
    CONF_NMI,
    CONF_SITE_ID,
    DOMAIN,
)
from custom_components.amber_energy_dashboard.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .synthetic import SITE_ID, expected_totals, make_usage_day, site_json

USAGE_URL = f"https://api.amber.com.au/v1/sites/{SITE_ID}/usage"
SID = SITE_ID.lower()
PREFIX = f"{DOMAIN}:{SID}"
E1_ENERGY, E1_COST = f"{PREFIX}_e1_energy", f"{PREFIX}_e1_cost"
B1_ENERGY, B1_COMP = f"{PREFIX}_b1_energy", f"{PREFIX}_b1_compensation"
NET = f"{PREFIX}_net_cost"
ALL_IDS = [E1_ENERGY, E1_COST, B1_ENERGY, B1_COMP, NET]
D1, D2, D3 = date(2026, 9, 22), date(2026, 9, 23), date(2026, 9, 24)
CHANNELS = [
    {"identifier": "E1", "type": "general", "tariff": "EA116"},
    {"identifier": "B1", "type": "feedIn", "tariff": None},
]


OTHER_SITE = "01FAKESITE0000000000000009"


@pytest.fixture(autouse=True)
def _setup(
    recorder_mock, enable_custom_integrations: None, aioclient_mock: AiohttpClientMocker
) -> None:
    """Recorder first (it must precede hass), then allow custom integrations.

    Entry setup validates the key with GET /sites, so both fake sites are listed.
    """
    aioclient_mock.get(
        "https://api.amber.com.au/v1/sites",
        json=[site_json(), site_json(id=OTHER_SITE, nmi="FAKENMI009")],
    )


async def _add_entry(hass: HomeAssistant, site_id: str = SITE_ID, **kw: Any) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=site_id,
        title="Amber FAKENMI000",
        data={
            CONF_API_KEY: "psk_secret_test_key",
            CONF_SITE_ID: site_id,
            CONF_NMI: "FAKENMI000",
            CONF_CHANNELS: kw.get("channels", CHANNELS),
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.fixture
async def entry(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> MockConfigEntry:
    """A loaded config entry for the synthetic site (on the mocked HTTP session)."""
    return await _add_entry(hass)


def _mock_day(
    aioclient_mock: AiohttpClientMocker, day: date, records: list[dict], site: str = SITE_ID
) -> None:
    aioclient_mock.get(
        f"https://api.amber.com.au/v1/sites/{site}/usage",
        params={"startDate": day.isoformat(), "endDate": day.isoformat()},
        json=records,
    )


async def _import(hass: HomeAssistant, day: date, **extra: Any) -> dict[str, Any]:
    return await hass.services.async_call(
        DOMAIN,
        "import_day",
        {"date": day.isoformat(), **extra},
        blocking=True,
        return_response=True,
    )


async def _rows(hass: HomeAssistant, ids=ALL_IDS) -> dict[str, list[dict]]:
    await async_wait_recording_done(hass)
    return await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2020, 1, 1, tzinfo=UTC),
        None,
        set(ids),
        "hour",
        None,
        {"state", "sum"},
    )


def _usage_calls(aioclient_mock: AiohttpClientMocker) -> int:
    return sum(1 for _m, url, _d, _h in aioclient_mock.mock_calls if url.path.endswith("/usage"))


def _start(row: dict) -> datetime:
    return datetime.fromtimestamp(row["start"], UTC)


def _hour_start(day: date, hour: int) -> datetime:
    return importer.nem_day_start(day) + timedelta(hours=hour)


# --- happy path ------------------------------------------------------------------------


async def test_first_import_writes_model(
    hass: HomeAssistant, entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """One day: 24 rows per statistic, correct units, signs and cents-to-AUD."""
    records = make_usage_day(D3)
    _mock_day(aioclient_mock, D3, records)

    result = await _import(hass, D3)

    assert result["mode"] == "first"
    assert result["records"] == 576
    rows = await _rows(hass)
    assert set(rows) == set(ALL_IDS)
    for sid in ALL_IDS:
        assert [_start(r) for r in rows[sid]] == [_hour_start(D3, h) for h in range(24)]
    assert _start(rows[E1_ENERGY][0]) == datetime(2026, 9, 23, 14, tzinfo=UTC)

    totals = expected_totals(records)
    assert rows[E1_ENERGY][-1]["sum"] == pytest.approx(totals["E1"]["kwh"], abs=1e-6)
    assert rows[B1_ENERGY][-1]["sum"] == pytest.approx(totals["B1"]["kwh"], abs=1e-6)
    assert rows[E1_COST][-1]["sum"] == pytest.approx(totals["E1"]["cents"] / 100, abs=1e-6)
    # Feed-in cost is negative in Amber's data; compensation is positive (earned).
    assert totals["B1"]["cents"] < 0
    assert rows[B1_COMP][-1]["sum"] == pytest.approx(-totals["B1"]["cents"] / 100, abs=1e-6)
    assert rows[B1_COMP][-1]["sum"] > 0
    assert rows[NET][-1]["sum"] == pytest.approx(
        (totals["E1"]["cents"] + totals["B1"]["cents"]) / 100, abs=1e-6
    )

    # Every hour holds exactly the records whose floored startTime falls in it.
    for hour in range(24):
        start = _hour_start(D3, hour)
        in_hour = [
            r
            for r in records
            if r["channelIdentifier"] == "E1"
            and start <= datetime.fromisoformat(r["startTime"]) < start + timedelta(hours=1)
        ]
        assert len(in_hour) == 12
        assert rows[E1_ENERGY][hour]["state"] == pytest.approx(
            math.fsum(r["kwh"] for r in in_hour), abs=1e-9
        )

    meta = await hass.async_add_executor_job(get_metadata, hass)
    for sid in ALL_IDS:
        stored = meta[sid][1]
        assert stored["source"] == DOMAIN
        assert stored["has_sum"] is True
        assert stored["mean_type"] == StatisticMeanType.NONE
    assert meta[E1_ENERGY][1]["unit_class"] == "energy"
    assert meta[E1_ENERGY][1]["unit_of_measurement"] == "kWh"
    for sid in (E1_COST, B1_COMP, NET):
        assert meta[sid][1]["unit_class"] is None
        assert meta[sid][1]["unit_of_measurement"] == "AUD"


async def test_cents_to_aud_and_signs(
    hass: HomeAssistant, entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """Cents divide by 100 once; negative prices flip signs as Amber reports them."""
    records = make_usage_day(D3)
    for r in records:
        r["kwh"], r["cost"] = 0, 0
    first_e1 = next(r for r in records if r["channelIdentifier"] == "E1")
    first_b1 = next(r for r in records if r["channelIdentifier"] == "B1")
    first_e1.update(kwh=1.0, cost=1234.5678)  # paid
    first_b1.update(kwh=0.5, cost=6.25)  # exporting at a negative price: paid, not earned
    second_e1 = [r for r in records if r["channelIdentifier"] == "E1"][12]
    second_e1.update(kwh=0.2, cost=-3.5)  # importing at a negative price: earned
    _mock_day(aioclient_mock, D3, records)

    await _import(hass, D3)

    rows = await _rows(hass)
    assert rows[E1_COST][0]["state"] == pytest.approx(12.345678, abs=1e-9)
    assert rows[E1_COST][1]["state"] == pytest.approx(-0.035, abs=1e-9)
    assert rows[B1_COMP][0]["state"] == pytest.approx(-0.0625, abs=1e-9)
    assert rows[NET][0]["state"] == pytest.approx(12.345678 + 0.0625, abs=1e-9)
    assert rows[NET][-1]["sum"] == pytest.approx(12.345678 + 0.0625 - 0.035, abs=1e-9)


async def test_consecutive_days_continue_sums(
    hass: HomeAssistant, entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """Each day's baseline is the last sum before it, so sums run on across days."""
    days = {d: make_usage_day(d) for d in (D1, D2, D3)}
    for d, recs in days.items():
        _mock_day(aioclient_mock, d, recs)

    modes = [(await _import(hass, d))["mode"] for d in (D1, D2, D3)]

    assert modes == ["first", "append", "append"]
    rows = await _rows(hass)
    for sid in ALL_IDS:
        r = rows[sid]
        assert len(r) == 72
        assert [_start(x) for x in r] == [
            _hour_start(D1, 0) + timedelta(hours=h) for h in range(72)
        ]
        for prev, cur in pairwise(r):
            assert cur["sum"] == pytest.approx(prev["sum"] + cur["state"], abs=1e-6)
    grand = sum(expected_totals(recs)["E1"]["kwh"] for recs in days.values())
    assert rows[E1_ENERGY][-1]["sum"] == pytest.approx(grand, abs=1e-6)


async def test_reimport_latest_day_overwrites(
    hass: HomeAssistant, entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """Re-importing the latest day replaces its rows; it never duplicates or drifts."""
    d1, d2 = make_usage_day(D1), make_usage_day(D2)
    _mock_day(aioclient_mock, D1, d1)
    _mock_day(aioclient_mock, D2, d2)
    await _import(hass, D1)
    await _import(hass, D2)
    before = await _rows(hass)

    revised = make_usage_day(D2, seed=7)
    aioclient_mock.clear_requests()
    _mock_day(aioclient_mock, D2, revised)
    result = await _import(hass, D2)

    assert result["mode"] == "reimport"
    rows = await _rows(hass)
    d1_total = expected_totals(d1)["E1"]["kwh"]
    for sid in ALL_IDS:
        assert len(rows[sid]) == 48
        assert rows[sid][:24] == before[sid][:24]  # D1 untouched
    assert result["statistics"][E1_ENERGY]["baseline"] == pytest.approx(d1_total, abs=1e-6)
    assert rows[E1_ENERGY][-1]["sum"] == pytest.approx(
        d1_total + expected_totals(revised)["E1"]["kwh"], abs=1e-6
    )
    assert rows[E1_ENERGY][-1]["sum"] != pytest.approx(before[E1_ENERGY][-1]["sum"])

    # Re-importing the same data again is a no-op on values.
    again = await _import(hass, D2)
    assert again["mode"] == "reimport"
    assert await _rows(hass) == rows


async def test_reimport_of_only_day_uses_zero_baseline(
    hass: HomeAssistant, entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """With a single imported day, re-import starts again from zero."""
    _mock_day(aioclient_mock, D1, make_usage_day(D1))
    await _import(hass, D1)

    result = await _import(hass, D1)

    assert result["mode"] == "reimport"
    assert result["statistics"][E1_ENERGY]["baseline"] == 0


async def test_thirty_minute_intervals(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The expected count comes from duration: 48 records for 30-minute data."""
    await _add_entry(hass)
    records = make_usage_day(D3, duration=30)
    _mock_day(aioclient_mock, D3, records)

    result = await _import(hass, D3)

    assert result["records"] == 96
    assert result["durations"] == [30]
    rows = await _rows(hass)
    assert rows[E1_ENERGY][-1]["sum"] == pytest.approx(
        expected_totals(records)["E1"]["kwh"], abs=1e-6
    )


async def test_estimated_records_counted(
    hass: HomeAssistant, entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """Estimated data imports, and the summary says how much of it was estimated."""
    _mock_day(aioclient_mock, D3, make_usage_day(D3, quality="estimated"))

    result = await _import(hass, D3)

    assert result["estimated_records"] == 576


async def test_controlled_load_channel(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Controlled load gets energy and cost (not compensation) and joins net cost."""
    channels = [
        {"identifier": "E1", "type": "general", "tariff": None},
        {"identifier": "E2", "type": "controlledLoad", "tariff": None},
    ]
    await _add_entry(hass, channels=channels)
    records = make_usage_day(D3, [("E1", "general", None), ("E2", "controlledLoad", None)])
    _mock_day(aioclient_mock, D3, records)

    await _import(hass, D3)

    ids = [f"{PREFIX}_e2_energy", f"{PREFIX}_e2_cost", NET]
    rows = await _rows(hass, ids)
    totals = expected_totals(records)
    assert rows[f"{PREFIX}_e2_cost"][-1]["sum"] == pytest.approx(totals["E2"]["cents"] / 100)
    assert rows[NET][-1]["sum"] == pytest.approx(
        (totals["E1"]["cents"] + totals["E2"]["cents"]) / 100
    )


# --- Guard 3 ---------------------------------------------------------------------------


def _drop_one(recs: list[dict]) -> None:
    recs.pop(100)


def _dup(recs: list[dict]) -> None:
    recs[101]["startTime"] = recs[100]["startTime"]


def _off_grid(recs: list[dict]) -> None:
    recs[287]["startTime"] = recs[287]["startTime"].replace(":55:01", ":57:01")


def _outside_day(recs: list[dict]) -> None:
    recs[287]["startTime"] = "2026-09-24T14:00:01Z"


def _all_b1(field: str, value: Any) -> Callable[[list[dict]], None]:
    def apply(recs: list[dict]) -> None:
        for r in recs:
            if r["channelIdentifier"] == "B1":
                r[field] = value

    return apply


GUARD3_CASES = [
    ("no_data", lambda recs: recs.clear()),
    (
        "unexpected_channel",
        lambda recs: recs.extend({**r, "channelIdentifier": "E2"} for r in recs[:288]),
    ),
    ("missing_channel", lambda recs: recs.__setitem__(slice(None), recs[:288])),
    ("channel_type_mismatch", _all_b1("channelType", "general")),
    ("wrong_date", lambda recs: recs[5].update(date="2026-09-23")),
    ("mixed_duration", lambda recs: recs[5].update(duration=30)),
    ("invalid_duration", _all_b1("duration", 7)),
    ("duplicate_interval", _dup),
    ("incomplete_day", _drop_one),
    ("interval_outside_day", _off_grid),
    ("interval_outside_day", _outside_day),
]


@pytest.mark.parametrize(("reason", "mutate"), GUARD3_CASES)
async def test_guard3_stops_first_import(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    reason: str,
    mutate: Callable[[list[dict]], None],
) -> None:
    """Every Guard 3 failure stops the import and writes nothing."""
    records = make_usage_day(D3)
    mutate(records)
    _mock_day(aioclient_mock, D3, records)

    with pytest.raises(importer.IncompleteDataError) as exc_info:
        await _import(hass, D3)

    assert exc_info.value.reason == reason
    await async_wait_recording_done(hass)
    assert await hass.async_add_executor_job(list_statistic_ids, hass) == []
    assert entry.runtime_data.last_error["reason"] == reason


@pytest.mark.parametrize(("reason", "mutate"), GUARD3_CASES[::3])
async def test_guard3_leaves_history_untouched(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    reason: str,
    mutate: Callable[[list[dict]], None],
) -> None:
    """A failed append leaves the earlier day exactly as it was."""
    _mock_day(aioclient_mock, D2, make_usage_day(D2))
    await _import(hass, D2)
    before = await _rows(hass)
    bad = make_usage_day(D3)
    mutate(bad)
    _mock_day(aioclient_mock, D3, bad)

    with pytest.raises(importer.IncompleteDataError):
        await _import(hass, D3)

    assert await _rows(hass) == before


def test_guard3_unit_checks_order() -> None:
    """Duplicates are reported as duplicates, not as a wrong count."""
    from custom_components.amber_energy_dashboard.api import _parse_usage  # noqa: PLC0415
    from custom_components.amber_energy_dashboard.statistics import (  # noqa: PLC0415
        ChannelConfig,
    )

    recs = make_usage_day(D3, [("E1", "general", None)])
    recs.append(copy.deepcopy(recs[0]))
    parsed = [_parse_usage(r) for r in recs]
    with pytest.raises(importer.IncompleteDataError) as exc_info:
        importer.check_completeness(parsed, D3, [ChannelConfig("E1", "general")])
    assert exc_info.value.reason == "duplicate_interval"


# --- append-only safety ----------------------------------------------------------------


@pytest.mark.parametrize("target", [D3, date(2026, 9, 21), date(2026, 9, 25)])
async def test_refuses_anything_but_next_or_latest_day(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    target: date,
) -> None:
    """With D1 and D2 imported, only D3 (next) or D2 (latest) are allowed."""
    for d in (D1, D2):
        _mock_day(aioclient_mock, d, make_usage_day(d))
        await _import(hass, d)
    before = await _rows(hass)
    calls = aioclient_mock.call_count

    if target == D3:
        _mock_day(aioclient_mock, D3, make_usage_day(D3))
        assert (await _import(hass, D3))["mode"] == "append"
        return

    with pytest.raises(importer.ImportRefusedError) as exc_info:
        await _import(hass, target)
    assert exc_info.value.reason == "not_next_day"
    assert isinstance(exc_info.value, ServiceValidationError)
    assert "Only 2026-09-24 (the next day) or 2026-09-23" in str(exc_info.value)
    assert aioclient_mock.call_count == calls  # refused before any API call
    assert await _rows(hass) == before


async def test_refuses_earlier_day_than_latest(
    hass: HomeAssistant, entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """A day before the latest imported day cannot be re-imported on its own."""
    for d in (D1, D2):
        _mock_day(aioclient_mock, d, make_usage_day(d))
        await _import(hass, d)

    with pytest.raises(importer.ImportRefusedError, match="cannot be imported"):
        await _import(hass, D1)


def _external(hass: HomeAssistant, sid: str, name: str, starts: list[datetime]) -> None:
    unit, unit_class = ("kWh", "energy") if sid.endswith("energy") else ("AUD", None)
    async_add_external_statistics(
        hass,
        {
            "has_sum": True,
            "mean_type": StatisticMeanType.NONE,
            "name": name,
            "source": DOMAIN,
            "statistic_id": sid,
            "unit_class": unit_class,
            "unit_of_measurement": unit,
        },
        [{"start": s, "state": 1.0, "sum": float(i + 1)} for i, s in enumerate(starts)],
    )


async def test_refuses_partial_history(
    hass: HomeAssistant, entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """History that ends mid-day (not written by this service) is not extended."""
    starts = [_hour_start(D2, h) for h in range(10)]
    for sid in ALL_IDS:
        _external(hass, sid, sid, starts)
    await async_wait_recording_done(hass)

    for target in (D2, D3):
        with pytest.raises(importer.ImportRefusedError) as exc_info:
            await _import(hass, target)
        assert exc_info.value.reason == "partial_history"
    assert _usage_calls(aioclient_mock) == 0


async def test_refuses_inconsistent_history(
    hass: HomeAssistant, entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """Some statistics with history and some without: refuse, do not guess."""
    _external(hass, E1_ENERGY, "x", [_hour_start(D2, h) for h in range(24)])
    await async_wait_recording_done(hass)

    with pytest.raises(importer.ImportRefusedError) as exc_info:
        await _import(hass, D3)
    assert exc_info.value.reason == "inconsistent_history"


async def test_refuses_mismatched_end_hours(
    hass: HomeAssistant, entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """Statistics ending at different hours are inconsistent."""
    for i, sid in enumerate(ALL_IDS):
        _external(hass, sid, sid, [_hour_start(D2, h) for h in range(24 - (i == 0))])
    await async_wait_recording_done(hass)

    with pytest.raises(importer.ImportRefusedError) as exc_info:
        await _import(hass, D3)
    assert exc_info.value.reason == "inconsistent_history"


async def test_refuses_reimport_with_broken_continuity(
    hass: HomeAssistant, entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """If the latest day's first hour does not continue from the hour before, refuse."""
    for sid in ALL_IDS:
        _external(hass, sid, sid, [_hour_start(D1, h) for h in range(48)])
    await async_wait_recording_done(hass)
    # Overwrite D2's first hour with a sum that does not follow D1's last sum.
    for sid in ALL_IDS:
        unit, unit_class = ("kWh", "energy") if sid.endswith("energy") else ("AUD", None)
        async_add_external_statistics(
            hass,
            {
                "has_sum": True,
                "mean_type": StatisticMeanType.NONE,
                "name": sid,
                "source": DOMAIN,
                "statistic_id": sid,
                "unit_class": unit_class,
                "unit_of_measurement": unit,
            },
            [{"start": _hour_start(D2, 0), "state": 1.0, "sum": 999.0}],
        )
    await async_wait_recording_done(hass)

    with pytest.raises(importer.ImportRefusedError) as exc_info:
        await _import(hass, D2)
    assert exc_info.value.reason == "inconsistent_history"


# --- failures around the write ---------------------------------------------------------


async def test_verification_failure_is_reported(
    hass: HomeAssistant, entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """If the read-back never matches, the import fails loudly."""
    _mock_day(aioclient_mock, D3, make_usage_day(D3))

    with (
        patch.object(importer, "async_add_external_statistics"),
        patch.object(importer, "VERIFY_TIMEOUT", 0.2),
        pytest.raises(importer.VerificationFailedError, match="read-back does not match"),
    ):
        await _import(hass, D3)
    assert entry.runtime_data.last_error["reason"] == "verification_failed"


async def test_auth_error_starts_reauth(
    hass: HomeAssistant, entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """A 403 during import starts the reauth flow and writes nothing."""
    aioclient_mock.get(USAGE_URL, status=403, text='{"message":"denied"}')

    with pytest.raises(HomeAssistantError, match="Re-authenticate"):
        await _import(hass, D3)

    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == ["reauth"]
    assert await hass.async_add_executor_job(list_statistic_ids, hass) == []


@pytest.mark.parametrize(
    ("mock", "match"),
    [
        ({"status": 503}, "unavailable"),
        ({"exc": TimeoutError()}, "unavailable"),
        ({"status": 429, "headers": {"Retry-After": "30"}}, "retry after 30"),
        ({"status": 422, "text": "Range requested is too large."}, "Amber API error"),
        ({"text": "not json"}, "Amber API error"),
    ],
)
async def test_api_failures(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    mock: dict,
    match: str,
) -> None:
    """API failures stop the import with a clear error and write nothing."""
    aioclient_mock.get(USAGE_URL, **mock)

    with pytest.raises(HomeAssistantError, match=match):
        await _import(hass, D3)
    assert await hass.async_add_executor_job(list_statistic_ids, hass) == []


# --- service entry resolution ----------------------------------------------------------


async def test_service_needs_entry(hass: HomeAssistant) -> None:
    """Without a loaded entry the service refuses."""
    assert await async_setup_component(hass, DOMAIN, {})
    with pytest.raises(ServiceValidationError, match="No loaded"):
        await _import(hass, D3)


async def test_service_with_two_entries(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """With several sites, config_entry_id is required and selects the site."""
    await _add_entry(hass)
    other = OTHER_SITE
    second = await _add_entry(hass, other)
    _mock_day(aioclient_mock, D3, make_usage_day(D3), site=other)

    with pytest.raises(ServiceValidationError, match="pass config_entry_id"):
        await _import(hass, D3)
    with pytest.raises(ServiceValidationError, match="No loaded"):
        await _import(hass, D3, config_entry_id="nope")
    result = await _import(hass, D3, config_entry_id=second.entry_id)
    assert f"{DOMAIN}:{other.lower()}_e1_energy" in result["statistics"]


# --- DST ---------------------------------------------------------------------------------


@pytest.mark.parametrize("tz", ["Australia/Sydney", "Australia/Adelaide"])
@pytest.mark.parametrize(
    "days",
    [
        (date(2026, 10, 3), date(2026, 10, 4), date(2026, 10, 5)),  # DST starts 4 Oct 2026
        (date(2027, 4, 3), date(2027, 4, 4), date(2027, 4, 5)),  # DST ends 4 Apr 2027
    ],
    ids=["dst-start-2026-10", "dst-end-2027-04"],
)
async def test_dst_transitions(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    tz: str,
    days: tuple[date, date, date],
) -> None:
    """Every NEM day is 24 UTC hours whatever HA's local time zone does."""
    await hass.config.async_set_time_zone(tz)
    await _add_entry(hass)
    all_records = []
    for d in days:
        recs = make_usage_day(d)
        all_records += recs
        _mock_day(aioclient_mock, d, recs)
        await _import(hass, d)

    rows = await _rows(hass)
    first = importer.nem_day_start(days[0])
    for sid in ALL_IDS:
        starts = [_start(r) for r in rows[sid]]
        assert starts == [first + timedelta(hours=h) for h in range(72)]
    assert rows[E1_ENERGY][-1]["sum"] == pytest.approx(
        expected_totals(all_records)["E1"]["kwh"], abs=1e-6
    )
    # The local-day view (23- or 25-hour days on the transition) still adds up.
    daily = await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        first,
        first + timedelta(hours=72),
        {E1_ENERGY},
        "day",
        None,
        {"change"},
    )
    assert math.fsum(r["change"] for r in daily[E1_ENERGY]) == pytest.approx(
        rows[E1_ENERGY][-1]["sum"], abs=1e-6
    )


# --- energy dashboard and diagnostics -------------------------------------------------------


async def test_energy_dashboard_validation(
    hass: HomeAssistant, entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """The statistics are accepted as a grid source with no validation issues."""
    from homeassistant.components.energy.data import async_get_manager  # noqa: PLC0415
    from homeassistant.components.energy.validate import async_validate  # noqa: PLC0415

    _mock_day(aioclient_mock, D3, make_usage_day(D3))
    await _import(hass, D3)
    await async_wait_recording_done(hass)
    assert await async_setup_component(hass, "energy", {})
    manager = await async_get_manager(hass)
    await manager.async_update(
        {
            "energy_sources": [
                {
                    "type": "grid",
                    "stat_energy_from": E1_ENERGY,
                    "stat_energy_to": B1_ENERGY,
                    "stat_cost": E1_COST,
                    "stat_compensation": B1_COMP,
                    "entity_energy_price": None,
                    "number_energy_price": None,
                    "entity_energy_price_export": None,
                    "number_energy_price_export": None,
                    "cost_adjustment_day": 0,
                }
            ]
        }
    )

    validation = (await async_validate(hass)).as_dict()

    assert validation["energy_sources"] == [[]]


async def test_diagnostics_redacts_key_and_nmi(
    hass: HomeAssistant, entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """Diagnostics carry the model and last import, never the key or NMI."""
    _mock_day(aioclient_mock, D3, make_usage_day(D3))
    await _import(hass, D3)

    diag = await async_get_config_entry_diagnostics(hass, entry)

    text = repr(diag)
    assert "psk_secret_test_key" not in text
    assert "FAKENMI000" not in text
    assert diag["entry"]["data"]["api_key"] == "**REDACTED**"
    assert diag["entry"]["data"]["nmi"] == "**REDACTED**"
    assert diag["entry"]["title"] == "**REDACTED**"
    assert [s["statistic_id"] for s in diag["statistics"]] == ALL_IDS
    assert diag["last_import"]["date"] == "2026-09-24"
    assert diag["api"]["requests_since_start"] == 2  # setup validation + one import


async def test_refuses_reimport_when_latest_day_has_gaps(
    hass: HomeAssistant, entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """The latest day must have all 24 hours before it can be re-imported."""
    for sid in ALL_IDS:
        _external(hass, sid, sid, [_hour_start(D2, h) for h in range(12, 24)])
    await async_wait_recording_done(hass)

    with pytest.raises(importer.ImportRefusedError) as exc_info:
        await _import(hass, D2)
    assert exc_info.value.reason == "partial_history"
    assert _usage_calls(aioclient_mock) == 0


async def test_budget_refusal_is_reported(
    hass: HomeAssistant, entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """A rate-limit reserve stop is surfaced as a clear, retryable error."""
    from custom_components.amber_energy_dashboard.api import (  # noqa: PLC0415
        AmberBudgetExhaustedError,
    )

    with (
        patch.object(
            entry.runtime_data.context.client,
            "async_get_usage",
            side_effect=AmberBudgetExhaustedError("reserve reached", "sites_usage"),
        ),
        pytest.raises(HomeAssistantError, match="protect the shared Amber rate limit"),
    ):
        await _import(hass, D3)
    assert entry.runtime_data.last_error["reason"] == "rate_limit_reserve"


def test_compare_reports_misplaced_rows() -> None:
    """The read-back comparison names the first mismatch it finds."""
    want = importer.build_rows(D3, [1.0] * 24, 0.0)
    got = [{"start": r["start"].timestamp(), "state": r["state"], "sum": r["sum"]} for r in want]
    assert importer._compare({"x": want}, {"x": got}) is None
    shifted = [{**g, "start": g["start"] + 3600} for g in got]
    assert "expected" in importer._compare({"x": want}, {"x": shifted})
    wrong = [*got[:-1], {**got[-1], "sum": 99.0}]
    assert "sum 99.0" in importer._compare({"x": want}, {"x": wrong})
    assert "0 rows" in importer._compare({"x": want}, {})


async def test_nothing_after_check(
    hass: HomeAssistant, entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """Verification fails if rows exist after the imported day."""
    for sid in ALL_IDS:
        _external(hass, sid, sid, [_hour_start(D2, h) for h in range(24)])
    await async_wait_recording_done(hass)
    want = {sid: importer.build_rows(D1, [0.0] * 24, 0.0) for sid in ALL_IDS}

    problem = await importer._async_check_nothing_after(hass, want)

    assert "latest row is not the last hour" in problem
