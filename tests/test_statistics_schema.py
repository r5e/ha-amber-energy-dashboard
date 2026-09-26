"""Milestone 1 verification of the HA 2026.9 external statistics contract.

These tests pin the recorder behaviour the design relies on (DESIGN sections 6 and 7),
using the metadata shapes planned for Milestone 2. They exercise Home Assistant itself,
not integration code, so a failure here after an HA bump means the design needs review.
"""

from datetime import UTC, datetime, timedelta

from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
import pytest
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.amber_energy_dashboard.const import DOMAIN

SITE = "01fakesite0000000000000000"
START = datetime(2026, 9, 23, 14, 0, tzinfo=UTC)  # first hour of NEM day 2026-09-24

ENERGY_META = {
    "has_sum": True,
    "mean_type": StatisticMeanType.NONE,
    "name": "Amber E9 energy",
    "source": DOMAIN,
    "statistic_id": f"{DOMAIN}:{SITE}_e9_energy",
    "unit_class": "energy",
    "unit_of_measurement": "kWh",
}
COST_META = {
    "has_sum": True,
    "mean_type": StatisticMeanType.NONE,
    "name": "Amber E9 cost",
    "source": DOMAIN,
    "statistic_id": f"{DOMAIN}:{SITE}_e9_cost",
    "unit_class": None,
    "unit_of_measurement": "AUD",
}
PRICE_META = {
    "has_sum": False,
    "mean_type": StatisticMeanType.ARITHMETIC,
    "name": "Amber E9 price",
    "source": DOMAIN,
    "statistic_id": f"{DOMAIN}:{SITE}_e9_price",
    "unit_class": None,
    "unit_of_measurement": "AUD/kWh",
}


def _rows(values: list[float], base: float = 0.0) -> list[dict]:
    rows, total = [], base
    for hour, value in enumerate(values):
        total += value
        rows.append({"start": START + timedelta(hours=hour), "state": value, "sum": total})
    return rows


async def _read(hass: HomeAssistant, statistic_id: str, types: set[str]) -> list[dict]:
    result = await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        START - timedelta(hours=1),
        START + timedelta(days=1),
        {statistic_id},
        "hour",
        None,
        types,
    )
    return result.get(statistic_id, [])


async def test_planned_metadata_accepted_and_read_back(
    recorder_mock, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Sum and mean statistics with mean_type and unit_class import without warnings."""
    energy = [0.5] * 24
    cost = [0.1234] * 24
    async_add_external_statistics(hass, ENERGY_META, _rows(energy))
    async_add_external_statistics(hass, COST_META, _rows(cost))
    async_add_external_statistics(
        hass,
        PRICE_META,
        [
            {"start": START + timedelta(hours=h), "mean": 0.3, "min": 0.2, "max": 0.4}
            for h in range(24)
        ],
    )
    await async_wait_recording_done(hass)

    # Omitting mean_type or unit_class is deprecated (breaks in 2026.11); under the test
    # harness it raises RuntimeError at the call above, so reaching here proves the
    # planned metadata is complete. The log check covers the non-test code path.
    assert "doesn't specify" not in caplog.text

    meta = await hass.async_add_executor_job(
        get_metadata,
        hass,
    )
    for expected in (ENERGY_META, COST_META, PRICE_META):
        _id, stored = meta[expected["statistic_id"]]
        for key in ("has_sum", "mean_type", "name", "source", "unit_class", "unit_of_measurement"):
            assert stored[key] == expected[key], (expected["statistic_id"], key)

    energy_rows = await _read(hass, ENERGY_META["statistic_id"], {"state", "sum"})
    assert len(energy_rows) == 24
    assert energy_rows[-1]["sum"] == pytest.approx(12.0)
    price_rows = await _read(hass, PRICE_META["statistic_id"], {"mean"})
    assert len(price_rows) == 24
    assert price_rows[0]["mean"] == pytest.approx(0.3)


async def test_reimport_overwrites_same_hours(recorder_mock, hass: HomeAssistant) -> None:
    """Re-importing the same timestamps overwrites the rows (DESIGN section 7 re-check)."""
    statistic_id = ENERGY_META["statistic_id"]
    async_add_external_statistics(hass, ENERGY_META, _rows([1.0] * 24))
    await async_wait_recording_done(hass)
    async_add_external_statistics(hass, ENERGY_META, _rows([2.0] * 24))
    await async_wait_recording_done(hass)

    rows = await _read(hass, statistic_id, {"state", "sum"})
    assert len(rows) == 24
    assert rows[0]["state"] == pytest.approx(2.0)
    assert rows[-1]["sum"] == pytest.approx(48.0)

    last = await hass.async_add_executor_job(
        get_last_statistics, hass, 1, statistic_id, True, {"sum"}
    )
    assert last[statistic_id][0]["sum"] == pytest.approx(48.0)


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"statistic_id": f"{DOMAIN}:01FAKESITE_e9_energy"}, "Invalid statistic_id"),
        ({"source": "recorder"}, "Invalid source"),
        ({"unit_class": "energy", "unit_of_measurement": "AUD"}, "Unsupported unit"),
    ],
)
async def test_metadata_rejections(
    recorder_mock, hass: HomeAssistant, change: dict, error: str
) -> None:
    """Uppercase IDs, a mismatched source and a wrong unit_class are refused outright."""
    with pytest.raises(HomeAssistantError, match=error):
        async_add_external_statistics(hass, {**ENERGY_META, **change}, _rows([1.0]))


async def test_non_hour_timestamp_rejected(recorder_mock, hass: HomeAssistant) -> None:
    """Only top-of-hour starts are accepted (Amber's :01 offsets must be floored)."""
    rows = [{"start": START + timedelta(seconds=1), "state": 1.0, "sum": 1.0}]
    with pytest.raises(HomeAssistantError, match="top of the hour"):
        async_add_external_statistics(hass, ENERGY_META, rows)
