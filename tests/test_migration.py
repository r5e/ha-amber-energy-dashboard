"""Milestone 6a: migration from the YAML kits (DESIGN section 14).

All data is synthetic. Legacy statistics are imported as recorder statistics under the
kits' entity IDs, shaped as the kits write them: the advanced version hourly inside its
window with daily lumps before it, the v1 kit daily lumps throughout.
"""

from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from typing import Any
from unittest.mock import patch

from homeassistant.components.energy.data import async_get_manager
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.db_schema import Statistics
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.core import HomeAssistant, ServiceRegistry
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
import voluptuous as vol

from custom_components.amber_energy_dashboard import (
    importer,
    manager as manager_mod,
    migration,
)
from custom_components.amber_energy_dashboard.api import RunBudget, _parse_usage
from custom_components.amber_energy_dashboard.const import (
    CONF_SCHEDULE_MODE,
    CONF_USAGE_MODE,
    DOMAIN,
    ROLE_EXPORT_COST,
    ROLE_EXPORT_ENERGY,
    ROLE_IMPORT_COST,
    ROLE_IMPORT_ENERGY,
)
from custom_components.amber_energy_dashboard.statistics import ChannelConfig, build_specs

from .fake_amber import FakeAmber
from .synthetic import SITE_ID, make_usage_day
from .test_manager import (  # noqa: F401 - fixtures are used by name
    ALL_IDS,
    ENTRY_ID,
    TODAY,
    YESTERDAY,
    Clock,
    _fast_verify,
    _no_real_timers,
    _preload_store,
    _rows,
    _run,
    _setup,
    _setup_entry,
    _store_key,
    clock,
    sydney,
)

HOUR = timedelta(hours=1)
E1, E1_COST, B1, B1_COMP, NET = ALL_IDS
CHANNELS = (ChannelConfig("E1", "general", "EA116"), ChannelConfig("B1", "feedIn"))
SPECS = build_specs(SITE_ID, CHANNELS)
ADV = migration.ADVANCED.statistics
V1 = migration.V1_KIT.statistics
RETENTION = 4
WINDOW = [TODAY - timedelta(days=d) for d in range(RETENTION, 0, -1)]
BOUNDARY = importer.nem_day_start(WINDOW[0])
PRE = [WINDOW[0] - timedelta(days=d) for d in range(5, 0, -1)]
ADV_AUTOMATION = "automation.amber_daily_statistics_import"
V1_AUTOMATION = "automation.amber_usage_daily_statistics_import"


@pytest.fixture(autouse=True)
async def _sydney(sydney: None) -> None:
    """Every test runs in Australia/Sydney (the kits' lumps are at local midnight)."""


# --- synthetic legacy data -------------------------------------------------------------


def _amounts(day: date) -> dict[str, list[float]]:
    """The integration's own hourly amounts for a synthetic day."""
    records = [_parse_usage(r) for r in make_usage_day(day)]
    by_channel = importer.check_completeness(records, day, CHANNELS)
    return importer.hourly_amounts(by_channel, SPECS, day)


def _legacy_rows(
    ids: dict[str, str],
    *,
    hourly_days: list[date],
    lump_days: list[date],
    export_cost_sign: float = -1.0,
    start: dict[str, float] | None = None,
    tweak: dict[tuple[str, date], float] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Cumulative legacy rows: daily lumps (row at local midnight holding the total through
    the end of that day), then hourly rows equal to the real data."""
    sums = dict(
        start
        or {
            ROLE_IMPORT_ENERGY: 1000.0,
            ROLE_EXPORT_ENERGY: 500.0,
            ROLE_IMPORT_COST: 200.0,
            ROLE_EXPORT_COST: -20.0 * -export_cost_sign,
        }
    )
    rows: dict[str, list[dict[str, Any]]] = {role: [] for role in ids}
    for i, day in enumerate(lump_days):
        a = _amounts(day) if day in WINDOW else None
        inc = {
            ROLE_IMPORT_ENERGY: sum(a[E1]) if a else 10.0 + i,
            ROLE_EXPORT_ENERGY: sum(a[B1]) if a else 5.0 + i,
            ROLE_IMPORT_COST: 3.0 + i * 0.1,
            ROLE_EXPORT_COST: export_cost_sign * (0.5 + i * 0.01),
        }
        for role in ids:
            sums[role] += inc[role] + (tweak or {}).get((role, day), 0.0)
            start_at = importer.nem_day_start(day)
            rows[role].append({"start": start_at, "state": sums[role], "sum": sums[role]})
    for day in hourly_days:
        a = _amounts(day)
        per_role = {
            ROLE_IMPORT_ENERGY: a[E1],
            ROLE_EXPORT_ENERGY: a[B1],
            ROLE_IMPORT_COST: a[E1_COST],
            ROLE_EXPORT_COST: [export_cost_sign * v for v in a[B1_COMP]],
        }
        for role in ids:
            for hour, value in enumerate(per_role[role]):
                extra = (tweak or {}).get((role, day), 0.0) if hour == 0 else 0.0
                sums[role] += value + extra
                start_at = importer.nem_day_start(day) + hour * HOUR
                rows[role].append({"start": start_at, "state": sums[role], "sum": sums[role]})
    return rows


async def _import_legacy(
    hass: HomeAssistant, ids: dict[str, str], rows: dict, units: dict[str, str] | None = None
) -> None:
    recorder = get_instance(hass)
    for role, sid in ids.items():
        energy = role in (ROLE_IMPORT_ENERGY, ROLE_EXPORT_ENERGY)
        meta = {
            "has_sum": True,
            "mean_type": StatisticMeanType.NONE,
            "name": None,
            "source": "recorder",
            "statistic_id": sid,
            "unit_class": "energy" if energy else None,
            "unit_of_measurement": (units or {}).get(role, "kWh" if energy else "AUD"),
        }
        recorder.async_import_statistics(meta, rows[role], Statistics)
    await async_wait_recording_done(hass)


async def _advanced(hass: HomeAssistant, **kw: Any) -> dict[str, list[dict[str, Any]]]:
    rows = _legacy_rows(ADV, hourly_days=WINDOW, lump_days=PRE, **kw)
    await _import_legacy(hass, ADV, rows)
    return rows


async def _prefs(hass: HomeAssistant, imp: str, exp: str, **extra: Any) -> list[dict]:
    grid = {
        "type": "grid",
        "stat_energy_from": imp,
        "stat_energy_to": exp,
        "stat_cost": None,
        "stat_compensation": None,
        "entity_energy_price": None,
        "number_energy_price": None,
        "entity_energy_price_export": None,
        "number_energy_price_export": None,
        "cost_adjustment_day": 0.0,
        **extra,
    }
    sources = [
        grid,
        {"type": "solar", "stat_energy_from": "sensor.solar", "config_entry_solar_forecast": None},
    ]
    energy = await async_get_manager(hass)
    await energy.async_update(
        {"energy_sources": sources, "device_consumption": [], "device_consumption_water": []}
    )
    return sources


async def _automations(hass: HomeAssistant, *items: tuple[str, str, dict]) -> None:
    config = [
        {
            "id": key,
            "alias": alias,
            "triggers": [{"trigger": "event", "event_type": "amber_test"}],
            "actions": [action],
        }
        for key, alias, action in items
    ]
    assert await async_setup_component(hass, "automation", {"automation": config})
    await hass.async_block_till_done()


async def _adv_automation(hass: HomeAssistant) -> None:
    await _automations(
        hass,
        (
            "amber_daily_import",
            "Amber: daily statistics import",
            {"action": "script.amber_daily_import", "data": {"date": "x"}},
        ),
    )


async def _entry(hass, aioclient_mock, hass_storage, fake: FakeAmber | None = None, **kw) -> Any:
    """An entry whose first run has imported WINDOW."""
    _preload_store(hass_storage, retention_days=RETENTION)
    fake = fake or FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    entry = await _setup_entry(hass, aioclient_mock, fake, **kw)
    result = await _run(hass)
    assert result["status"] == "caught_up"
    assert result["imported_days"] == [d.isoformat() for d in WINDOW]
    return entry


async def _migrate(hass: HomeAssistant, **data: Any) -> dict[str, Any]:
    return await hass.services.async_call(
        DOMAIN, "migrate_v1", data, blocking=True, return_response=True
    )


def _by_start(rows: list[dict]) -> dict[datetime, dict]:
    return {datetime.fromtimestamp(r["start"], UTC): r for r in rows}


def _assert_continuous(rows: list[dict]) -> None:
    for prev, cur in pairwise(rows):
        assert cur["sum"] == pytest.approx(prev["sum"] + cur["state"], abs=2e-6)


def _record(hass_storage: dict) -> dict[str, Any] | None:
    return hass_storage[_store_key()]["data"].get("migration")


# --- the advanced version ----------------------------------------------------------------


async def _adv_scenario(hass, aioclient_mock, hass_storage) -> tuple:
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    entry = await _entry(hass, aioclient_mock, hass_storage, fake)
    legacy = await _advanced(hass)
    before_prefs = await _prefs(
        hass,
        ADV[ROLE_IMPORT_ENERGY],
        ADV[ROLE_EXPORT_ENERGY],
        stat_cost=ADV[ROLE_IMPORT_COST],
        stat_compensation=ADV[ROLE_EXPORT_COST],
    )
    await _adv_automation(hass)
    return fake, entry, legacy, before_prefs, await _rows(hass)


async def test_advanced_dry_run_changes_nothing(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """The dry run detects the layout, checks parity and previews every change."""
    _, _, legacy, before_prefs, ours_before = await _adv_scenario(
        hass, aioclient_mock, hass_storage
    )

    report = await _migrate(hass)  # dry run by default

    assert report["dry_run"] is True
    assert report["layout"] == "advanced"
    assert report["sources"][ROLE_IMPORT_ENERGY] == ADV[ROLE_IMPORT_ENERGY]
    assert report["sources"]["export_cost_sign"] == -1.0
    assert report["boundary"] == BOUNDARY.isoformat()
    parity = report["parity"]
    assert parity["passed"] is True
    assert parity["rules"] == "hourly"
    assert parity["days"] == RETENTION
    assert report["copy"][E1] == {
        "rows": len(PRE) + 1,  # the lumps plus the carry row
        "from": importer.nem_day_start(PRE[0]).isoformat(),
        "to": (BOUNDARY - HOUR).isoformat(),
        "baseline": pytest.approx(legacy[ROLE_IMPORT_ENERGY][len(PRE) - 1]["sum"]),
    }
    assert set(report["copy"]) == {E1, E1_COST, B1, B1_COMP, NET}
    assert report["energy"]["changed"] is True
    assert report["energy"]["before"] == [before_prefs[0]]
    assert report["energy"]["after"][0]["stat_energy_from"] == E1
    assert report["energy"]["after"][0]["stat_compensation"] == B1_COMP
    assert report["automations"] == [
        {"entity_id": ADV_AUTOMATION, "name": "Amber: daily statistics import", "state": "on"}
    ]
    assert report["problems"] == []
    assert report["warning"] == migration.BACKUP_WARNING
    assert await _rows(hass) == ours_before  # a dry run changes nothing
    assert _record(hass_storage) is None
    assert hass.states.get(ADV_AUTOMATION).state == "on"

    with pytest.raises(ServiceValidationError, match="backup"):
        await _migrate(hass, dry_run=False)
    assert _record(hass_storage) is None


async def test_advanced_migration(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Copy the pre-window history (compensation negated, net rebuilt), re-base the
    integration's rows, switch the dashboard, turn off the automation, record everything."""
    fake, entry, legacy, before_prefs, ours_before = await _adv_scenario(
        hass, aioclient_mock, hass_storage
    )

    report = await _migrate(hass, dry_run=False, confirm_backup=True)

    assert report["migration_status"] == "completed"
    result = report["result"]
    assert result["rebase"]["rewritten_days"] == RETENTION
    assert result["rebase"]["calls"] == {"sites_usage": 1}
    rows = await _rows(hass)
    for sid in (E1, E1_COST, B1, B1_COMP, NET):
        _assert_continuous(rows[sid])
    by = {sid: _by_start(rows[sid]) for sid in rows}
    # The copy: sums unchanged before the boundary, carry row in the hour before it.
    lumps = legacy[ROLE_IMPORT_ENERGY][: len(PRE)]
    for lump in lumps:
        assert by[E1][lump["start"]]["sum"] == pytest.approx(lump["sum"])
    assert by[E1][BOUNDARY - HOUR]["sum"] == pytest.approx(lumps[-1]["sum"])
    assert by[E1][BOUNDARY - HOUR]["state"] == 0.0
    # Compensation is the negated legacy export cost; net = cost - compensation.
    for lump, comp in zip(
        legacy[ROLE_EXPORT_COST][: len(PRE)], legacy[ROLE_IMPORT_COST], strict=False
    ):
        assert by[B1_COMP][lump["start"]]["sum"] == pytest.approx(-lump["sum"])
        assert by[NET][lump["start"]]["sum"] == pytest.approx(comp["sum"] + lump["sum"])
    assert by[B1_COMP][BOUNDARY - HOUR]["sum"] > 0
    # The seam: the first imported hour continues from the copied baseline.
    first = by[E1][BOUNDARY]
    assert first["sum"] - first["state"] == pytest.approx(lumps[-1]["sum"], abs=1e-6)
    # Exact parity means the new total ends exactly where the legacy total ends.
    assert rows[E1][-1]["sum"] == pytest.approx(legacy[ROLE_IMPORT_ENERGY][-1]["sum"], abs=1e-5)
    assert rows[B1][-1]["sum"] == pytest.approx(legacy[ROLE_EXPORT_ENERGY][-1]["sum"], abs=1e-5)
    assert rows[B1_COMP][-1]["sum"] == pytest.approx(-legacy[ROLE_EXPORT_COST][-1]["sum"], abs=1e-5)
    # Inside the window the new data won: hourly states are the integration's own.
    for sid in (E1, E1_COST, B1, B1_COMP, NET):
        assert [r["state"] for r in rows[sid][-24 * RETENTION :]] == pytest.approx(
            [r["state"] for r in ours_before[sid]], abs=1e-6
        )
    # Dashboard switched; automation off; everything recorded.
    energy = await async_get_manager(hass)
    grid = energy.data["energy_sources"][0]
    assert grid["stat_energy_from"] == E1
    assert grid["stat_cost"] == E1_COST
    assert grid["stat_energy_to"] == B1
    assert grid["stat_compensation"] == B1_COMP
    assert energy.data["energy_sources"][1] == before_prefs[1]
    assert hass.states.get(ADV_AUTOMATION).state == "off"
    record = _record(hass_storage)
    assert record["status"] == "completed"
    assert record["energy_backup"]["energy_sources"] == before_prefs
    assert record["energy_applied"] is True
    assert record["automations"] == [
        {"entity_id": ADV_AUTOMATION, "prior": "on", "disabled_by_migration": True}
    ]
    assert [c["action"] for c in record["changes"]] == [
        "backup",
        "copy",
        "rebase",
        "energy",
        "automations",
        "completed",
    ]
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"legacy_cleanup_{ENTRY_ID}")
    assert issue is not None
    assert "script.amber_daily_import" in issue.translation_placeholders["items"]
    assert entry.runtime_data.manager.store.tail_rewrite is None

    # Imports continue from the re-based sums.
    clock.now += timedelta(days=1)
    fake.latest = TODAY
    assert (await _run(hass))["imported_days"] == [TODAY.isoformat()]
    rows = await _rows(hass)
    _assert_continuous(rows[E1])
    assert len(rows[E1]) == len(PRE) + 1 + 24 * (RETENTION + 1)


async def test_second_run_refused_undo_and_rerun(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """A completed migration never runs twice by accident; undo restores the dashboard and
    re-enables the automation; after undo it can run again (the seam is already
    continuous, so no re-base calls)."""
    await _entry(hass, aioclient_mock, hass_storage)
    await _advanced(hass)
    before_prefs = await _prefs(hass, ADV[ROLE_IMPORT_ENERGY], ADV[ROLE_EXPORT_ENERGY])
    await _adv_automation(hass)
    await _migrate(hass, dry_run=False, confirm_backup=True)
    rows_after = await _rows(hass)

    with pytest.raises(ServiceValidationError, match="Undo it first"):
        await _migrate(hass, dry_run=False, confirm_backup=True)
    assert (await _migrate(hass))["migration_status"] == "completed"

    undo = await hass.services.async_call(
        DOMAIN, "undo_migration", {}, blocking=True, return_response=True
    )
    assert undo["energy_restored"] is True
    assert undo["automations_enabled"] == [ADV_AUTOMATION]
    energy = await async_get_manager(hass)
    assert energy.data["energy_sources"] == before_prefs
    assert hass.states.get(ADV_AUTOMATION).state == "on"
    assert _record(hass_storage)["status"] == "undone"
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"legacy_cleanup_{ENTRY_ID}") is None
    assert await _rows(hass) == rows_after  # copied history and re-based sums stay
    with pytest.raises(ServiceValidationError, match="no migration to undo"):
        await hass.services.async_call(DOMAIN, "undo_migration", {}, blocking=True)

    report = await _migrate(hass, dry_run=False, confirm_backup=True)
    assert report["result"]["rebase"] == {"skipped": "already continuous"}
    assert await _rows(hass) == rows_after
    assert hass.states.get(ADV_AUTOMATION).state == "off"
    assert energy.data["energy_sources"][0]["stat_energy_from"] == E1


async def test_already_disabled_automation_stays_disabled(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """An automation that is already off is recorded as off and left off, also by undo."""
    await _entry(hass, aioclient_mock, hass_storage)
    await _advanced(hass)
    await _adv_automation(hass)
    await hass.services.async_call(
        "automation", "turn_off", {"entity_id": ADV_AUTOMATION}, blocking=True
    )

    report = await _migrate(hass, dry_run=False, confirm_backup=True)

    assert report["result"]["automations"] == [
        {"entity_id": ADV_AUTOMATION, "prior": "off", "turned_off": False}
    ]
    assert any("already off" in line for line in report["cleanup"])
    assert report["result"]["energy"] == {
        "changed": False,
        "reason": "No Energy dashboard preferences are saved.",
    }
    undo = await hass.services.async_call(
        DOMAIN, "undo_migration", {}, blocking=True, return_response=True
    )
    assert undo["automations_enabled"] == []
    assert undo["left_unchanged"] == [ADV_AUTOMATION]
    assert undo["energy_restored"] is False
    assert hass.states.get(ADV_AUTOMATION).state == "off"


# --- the v1 kit ----------------------------------------------------------------------------


async def test_v1_kit_daily_lumps(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """v1: daily lumps throughout, kWh within 0.01 per day, cost only reported (its cost was
    approximate), compensation copied with its own (HA) sign, history marked approximate."""
    await _entry(hass, aioclient_mock, hass_storage)
    ids = dict(V1)
    rows = _legacy_rows(
        ids,
        hourly_days=[],
        lump_days=PRE + WINDOW,
        export_cost_sign=1.0,
        tweak={(ROLE_IMPORT_ENERGY, WINDOW[1]): 0.004},  # rounding noise below 0.01
    )
    await _import_legacy(hass, ids, rows)
    await _prefs(
        hass,
        V1[ROLE_IMPORT_ENERGY],
        V1[ROLE_EXPORT_ENERGY],
        entity_energy_price="sensor.amber_general_price",
    )
    await _automations(
        hass,
        (
            "1700000000000",
            "Amber usage - daily statistics import",
            {
                "action": "input_number.set_value",
                "target": {"entity_id": "input_number.amber_energy_import_running_total"},
                "data": {"value": 1},
            },
        ),
    )

    report = await _migrate(hass)

    assert report["layout"] == "v1"
    assert report["parity"]["passed"] is True
    assert report["parity"]["rules"] == "daily"
    assert report["parity"]["mismatch_count"] == 0
    assert set(report["parity"]["cost_totals"]) == {"import cost", "export cost"}
    assert "approximate" in " ".join(report["notes"])
    assert report["energy"]["after"][0]["entity_energy_price"] is None
    assert report["energy"]["after"][0]["stat_cost"] == E1_COST
    assert report["automations"][0]["entity_id"] == V1_AUTOMATION

    report = await _migrate(hass, dry_run=False, confirm_backup=True)

    assert report["migration_status"] == "completed"
    stored = await _rows(hass)
    by = {sid: _by_start(stored[sid]) for sid in stored}
    first_lump = rows[ROLE_EXPORT_COST][0]
    assert by[B1_COMP][first_lump["start"]]["sum"] == pytest.approx(first_lump["sum"])  # kept
    assert by[B1_COMP][first_lump["start"]]["state"] == 0.0
    second = rows[ROLE_IMPORT_ENERGY][1]
    assert by[E1][second["start"]]["state"] == pytest.approx(
        second["sum"] - rows[ROLE_IMPORT_ENERGY][0]["sum"]
    )  # states are recomputed as the change from the previous row
    first = by[E1][BOUNDARY]
    assert first["sum"] - first["state"] == pytest.approx(
        rows[ROLE_IMPORT_ENERGY][len(PRE) - 1]["sum"]
    )
    assert hass.states.get(V1_AUTOMATION).state == "off"
    for sid in (E1, B1, NET):
        _assert_continuous(stored[sid])


async def test_v1_kwh_beyond_tolerance_fails_parity(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """v1 kWh must agree within 0.01 per day."""
    await _entry(hass, aioclient_mock, hass_storage)
    ids = {ROLE_IMPORT_ENERGY: V1[ROLE_IMPORT_ENERGY], ROLE_EXPORT_ENERGY: V1[ROLE_EXPORT_ENERGY]}
    rows = _legacy_rows(
        ids, hourly_days=[], lump_days=PRE + WINDOW, tweak={(ROLE_EXPORT_ENERGY, WINDOW[2]): 0.02}
    )
    await _import_legacy(hass, ids, rows)

    report = await _migrate(hass)

    assert report["parity"]["passed"] is False
    assert report["parity"]["mismatches"][0]["statistic"] == "grid export kWh"
    assert report["parity"]["mismatches"][0]["day"] == WINDOW[2].isoformat()


# --- detection -----------------------------------------------------------------------------


async def test_both_layouts_picks_active_and_ignores_broken_v1(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """VM 101's case: broken, stale v1 leftovers (a 99999 spike) next to an active advanced
    version. The advanced version is chosen; v1 is reported as ignored; sources never mix."""
    await _entry(hass, aioclient_mock, hass_storage)
    await _advanced(hass)
    v1 = {ROLE_IMPORT_ENERGY: V1[ROLE_IMPORT_ENERGY], ROLE_EXPORT_ENERGY: V1[ROLE_EXPORT_ENERGY]}
    stale = [TODAY - timedelta(days=d) for d in (12, 11, 10, 9)]
    rows = _legacy_rows(v1, hourly_days=[], lump_days=stale)
    rows[ROLE_IMPORT_ENERGY].append(
        {"start": importer.nem_day_start(stale[-1]) + 16 * HOUR, "state": 99999.0, "sum": 99999.0}
    )
    await _import_legacy(hass, v1, rows)

    report = await _migrate(hass)

    detection = report["detection"]
    assert detection["chosen"] == "advanced"
    assert detection["ignored"] == [
        {
            "layout": "v1",
            "title": "v1 kit",
            "reason": f"last data {stale[-1]}, older than the advanced YAML version ({YESTERDAY})",
        }
    ]
    assert set(report["sources"].values()) >= set(ADV.values())
    assert not set(report["sources"].values()) & set(V1.values())
    assert report["parity"]["passed"] is True


async def test_two_recent_layouts_need_manual_pick(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Two layouts with equally recent data are ambiguous: the manual step decides. The
    manual pick's export cost sign is taken from the overlap."""
    await _entry(hass, aioclient_mock, hass_storage)
    await _advanced(hass)
    v1 = {ROLE_IMPORT_ENERGY: V1[ROLE_IMPORT_ENERGY], ROLE_EXPORT_ENERGY: V1[ROLE_EXPORT_ENERGY]}
    await _import_legacy(hass, v1, _legacy_rows(v1, hourly_days=[], lump_days=PRE + WINDOW))

    report = await _migrate(hass)
    assert report["needs_manual_pick"] is True
    assert "More than one" in report["detection"]["reason"]
    with pytest.raises(ServiceValidationError, match="More than one"):
        await _migrate(hass, dry_run=False, confirm_backup=True)

    picks = {role: ADV[role] for role in ADV}
    report = await _migrate(hass, **picks)
    assert report["layout"] == "manual"
    assert report["sources"]["rules"] == "hourly"
    assert report["sources"]["export_cost_sign"] == -1.0
    assert any("from the overlap" in note for note in report["notes"])
    assert report["parity"]["passed"] is True
    assert report["automations"] == []

    report = await _migrate(hass, dry_run=False, confirm_backup=True, **picks)
    assert report["migration_status"] == "completed"
    rows = await _rows(hass)
    assert rows[B1_COMP][0]["sum"] > 0  # negated from Amber's sign


async def test_partial_detection_needs_manual_pick(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """A renamed statistic makes the advanced layout incomplete: ask, do not guess."""
    await _entry(hass, aioclient_mock, hass_storage)
    ids = {**ADV, ROLE_IMPORT_COST: "sensor.my_renamed_cost"}
    await _import_legacy(hass, ids, _legacy_rows(ids, hourly_days=WINDOW, lump_days=PRE))

    report = await _migrate(hass)

    assert report["needs_manual_pick"] is True
    assert "incomplete" in report["detection"]["reason"]
    assert "import cost" in report["detection"]["reason"]
    report = await _migrate(hass, **ids)
    assert report["parity"]["passed"] is True


async def test_nothing_detected(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    await _entry(hass, aioclient_mock, hass_storage)
    report = await _migrate(hass)
    assert report["needs_manual_pick"] is True
    assert report["detection"]["reason"] == "No statistics of a known YAML kit were found."
    assert report["detection"]["candidates"] == []


async def test_manual_pick_validation(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    await _entry(hass, aioclient_mock, hass_storage)
    await _advanced(hass)
    cases = [
        ({ROLE_EXPORT_ENERGY: ADV[ROLE_EXPORT_ENERGY]}, "grid import kWh"),
        (
            {
                ROLE_IMPORT_ENERGY: ADV[ROLE_IMPORT_ENERGY],
                ROLE_EXPORT_ENERGY: ADV[ROLE_IMPORT_ENERGY],
            },
            "only once",
        ),
        ({ROLE_IMPORT_ENERGY: "sensor.nothing"}, "has no statistics"),
        ({ROLE_IMPORT_ENERGY: E1}, "belongs to this integration"),
        ({ROLE_IMPORT_ENERGY: ADV[ROLE_IMPORT_COST]}, "not in kWh"),
        (
            {
                ROLE_IMPORT_ENERGY: ADV[ROLE_IMPORT_ENERGY],
                ROLE_IMPORT_COST: ADV[ROLE_EXPORT_ENERGY],
            },
            "not in AUD",
        ),
    ]
    for picks, message in cases:
        with pytest.raises(ServiceValidationError, match=message):
            await _migrate(hass, **picks)


# --- safety ------------------------------------------------------------------------------


async def test_parity_failure_changes_nothing(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """A daily kWh difference (advanced: exact) or cost beyond 0.01 stops the migration."""
    await _entry(hass, aioclient_mock, hass_storage)
    await _advanced(
        hass, tweak={(ROLE_IMPORT_ENERGY, WINDOW[1]): 0.001, (ROLE_EXPORT_COST, WINDOW[2]): -0.02}
    )
    prefs = await _prefs(hass, ADV[ROLE_IMPORT_ENERGY], ADV[ROLE_EXPORT_ENERGY])
    await _adv_automation(hass)
    before = await _rows(hass)

    report = await _migrate(hass)
    parity = report["parity"]
    assert parity["passed"] is False
    assert parity["mismatch_count"] == 2
    assert {m["statistic"] for m in parity["mismatches"]} == {"grid import kWh", "export cost"}
    # The tweak also moves the following day's first-hour baseline; only the day differs.
    assert "differ" in parity["reason"]

    with pytest.raises(ServiceValidationError, match="Parity check failed"):
        await _migrate(hass, dry_run=False, confirm_backup=True)

    assert await _rows(hass) == before
    assert (await async_get_manager(hass)).data["energy_sources"] == prefs
    assert hass.states.get(ADV_AUTOMATION).state == "on"
    assert _record(hass_storage) is None


async def test_too_few_overlap_days(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    await _entry(hass, aioclient_mock, hass_storage)
    ids = dict(ADV)
    await _import_legacy(hass, ids, _legacy_rows(ids, hourly_days=WINDOW[:2], lump_days=PRE))
    report = await _migrate(hass)
    assert report["parity"]["passed"] is False
    assert report["parity"]["reason"] == "only 2 comparable days (at least 3 needed)"


async def test_missing_day_on_one_side_is_a_mismatch(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    await _entry(hass, aioclient_mock, hass_storage)
    ids = dict(ADV)
    rows = _legacy_rows(ids, hourly_days=WINDOW, lump_days=PRE)
    gap_day = importer.nem_day_start(WINDOW[2])
    rows[ROLE_EXPORT_COST] = [
        r for r in rows[ROLE_EXPORT_COST] if not gap_day <= r["start"] < gap_day + 24 * HOUR
    ]
    await _import_legacy(hass, ids, rows)
    report = await _migrate(hass)
    assert report["parity"]["mismatches"][0]["problem"] == "missing on one side"


async def test_decreasing_legacy_energy_is_refused(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """A drop in the history to copy (for example a test spike) refuses the migration."""
    await _entry(hass, aioclient_mock, hass_storage)
    rows = _legacy_rows(ADV, hourly_days=WINDOW, lump_days=PRE)
    spike = rows[ROLE_IMPORT_ENERGY][1]
    rows[ROLE_IMPORT_ENERGY][1] = {**spike, "sum": 99999.0, "state": 99999.0}
    await _import_legacy(hass, ADV, rows)

    report = await _migrate(hass)
    assert any("decreases" in p for p in report["problems"])
    with pytest.raises(ServiceValidationError, match="possible corruption"):
        await _migrate(hass, dry_run=False, confirm_backup=True)
    assert _record(hass_storage) is None


async def test_preconditions(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Refusals before anything is read: mode, no data, interrupted write, rewrite running,
    a boundary already older than retention."""
    entry = await _entry(hass, aioclient_mock, hass_storage)
    await _advanced(hass)
    store = entry.runtime_data.manager.store

    await store.async_set_tail_rewrite(
        {"from": "x", "next": WINDOW[0].isoformat(), "to": WINDOW[-1].isoformat()}
    )
    report = await _migrate(hass)
    assert report["problems"] == ["A range rewrite is in progress; let a run finish it first."]
    await store.async_set_tail_rewrite(None)

    await store.async_set_pending(TODAY)
    assert "interrupted" in (await _migrate(hass))["problems"][0]
    await store.async_clear_pending()

    clock.now += timedelta(days=2)
    report = await _migrate(hass)
    assert "older than Amber's retention" in report["problems"][0]
    clock.now -= timedelta(days=2)

    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "settings"}
    )
    await hass.config_entries.options.async_configure(
        flow["flow_id"], {CONF_SCHEDULE_MODE: "automatic", CONF_USAGE_MODE: "pricing"}
    )
    await hass.async_block_till_done()
    assert "Pricing-only" in (await _migrate(hass))["problems"][0]


async def test_no_data_yet_and_site_without_feed_in(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    _preload_store(hass_storage, retention_days=RETENTION)
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    await _setup_entry(hass, aioclient_mock, fake)
    await _advanced(hass)
    report = await _migrate(hass)
    assert report["problems"] == [
        "The integration has not imported any data yet; wait for its first run."
    ]
    manager = hass.config_entries.async_get_entry(ENTRY_ID).runtime_data.manager
    manager.ctx = manager.ctx.__class__(
        manager.ctx.client,
        manager.ctx.site_id,
        manager.ctx.channels[:1],
        tuple(s for s in manager.ctx.specs if s.channel != "B1"),
        manager.ctx.lock,
    )
    report = await _migrate(hass)
    assert "no feed-in channel" in " ".join(report["problems"])


async def test_site_channel_shape_problems(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    await _entry(hass, aioclient_mock, hass_storage)
    await _advanced(hass)
    manager = hass.config_entries.async_get_entry(ENTRY_ID).runtime_data.manager
    extra = (ChannelConfig("E2", "general"), ChannelConfig("B2", "feedIn"))
    channels = manager.ctx.channels + extra
    manager.ctx = manager.ctx.__class__(
        manager.ctx.client,
        manager.ctx.site_id,
        channels,
        tuple(build_specs(SITE_ID, channels)),
        manager.ctx.lock,
    )
    problems = (await _migrate(hass))["problems"]
    assert "2 general channels" in problems[0]
    assert "2 feed-in channels" in problems[1]


async def test_backup_check_failure_changes_nothing(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    await _entry(hass, aioclient_mock, hass_storage)
    await _advanced(hass)
    before = await _rows(hass)
    with (
        patch(
            "custom_components.amber_energy_dashboard.storage.AmberStore.async_read_back_migration",
            return_value=None,
        ),
        pytest.raises(ServiceValidationError, match="did not read back"),
    ):
        await _migrate(hass, dry_run=False, confirm_backup=True)
    assert _record(hass_storage) is None
    assert await _rows(hass) == before


async def test_interrupted_rebase_resumes(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """The re-base stops on the call budget: the copy is recorded, the rewrite progress is
    stored, nothing else changes; running the migration again finishes it."""
    await _entry(hass, aioclient_mock, hass_storage)
    legacy = await _advanced(hass)
    await _prefs(hass, ADV[ROLE_IMPORT_ENERGY], ADV[ROLE_EXPORT_ENERGY])
    await _adv_automation(hass)

    def tiny_budget() -> RunBudget:
        return RunBudget(max_calls_per_counter=0)

    with patch.object(manager_mod, "RunBudget", tiny_budget):
        report = await _migrate(hass, dry_run=False, confirm_backup=True)
    assert "paused" in report["result"]["paused"]
    record = _record(hass_storage)
    assert record["status"] == "in_progress"
    assert set(record["steps"]) == {"copy"}
    assert hass.states.get(ADV_AUTOMATION).state == "on"
    energy = await async_get_manager(hass)
    assert energy.data["energy_sources"][0]["stat_energy_from"] == ADV[ROLE_IMPORT_ENERGY]
    assert entry_tail(hass) is not None

    with pytest.raises(ServiceValidationError, match="other statistics is in progress"):
        await _migrate(hass, **{ROLE_IMPORT_ENERGY: V1[ROLE_IMPORT_ENERGY]})
    assert (await _migrate(hass))["migration_status"] == "in_progress"

    report = await _migrate(hass, dry_run=False, confirm_backup=True)
    assert report["migration_status"] == "completed"
    assert report["result"]["rebase"]["rewritten_days"] == RETENTION
    rows = await _rows(hass)
    _assert_continuous(rows[E1])
    assert rows[E1][-1]["sum"] == pytest.approx(legacy[ROLE_IMPORT_ENERGY][-1]["sum"], abs=1e-5)
    assert entry_tail(hass) is None


def entry_tail(hass: HomeAssistant) -> Any:
    return hass.config_entries.async_get_entry(ENTRY_ID).runtime_data.manager.store.tail_rewrite


async def test_rebase_waiting_and_seam_failure(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """A waiting re-base pauses; a seam that still breaks after re-basing is reported."""
    entry = await _entry(hass, aioclient_mock, hass_storage)
    await _advanced(hass)
    manager = entry.runtime_data.manager

    async def waiting(first: date) -> dict:
        return {"outcome": {"reason": "no usage yet"}, "calls": {}, "rewritten_days": 0}

    with patch.object(manager, "async_rebase", waiting):
        report = await _migrate(hass, dry_run=False, confirm_backup=True)
    assert report["result"]["paused"] == "re-base waiting: no usage yet"

    async def no_op(first: date) -> dict:
        return {"outcome": None, "calls": {}, "rewritten_days": 0}

    with (
        patch.object(manager, "async_rebase", no_op),
        pytest.raises(ServiceValidationError, match="do not continue at the seam"),
    ):
        await _migrate(hass, dry_run=False, confirm_backup=True)


async def test_energy_fallbacks(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Not used by the dashboard; a schema failure; a read-back mismatch; and legacy
    statistics used elsewhere: exact instructions instead of a change."""
    await _entry(hass, aioclient_mock, hass_storage)
    await _advanced(hass)

    await _prefs(hass, "sensor.other_import", "sensor.other_export")
    report = await _migrate(hass)
    assert report["energy"]["changed"] is False
    assert report["energy"]["reason"] == "The Energy dashboard does not use the legacy statistics."

    energy = await async_get_manager(hass)
    await _prefs(hass, ADV[ROLE_IMPORT_ENERGY], ADV[ROLE_EXPORT_ENERGY])
    await energy.async_update(
        {"device_consumption": [{"stat_consumption": ADV[ROLE_IMPORT_ENERGY]}]}
    )
    with patch.object(migration, "ENERGY_SOURCE_SCHEMA", side_effect=vol.Invalid("bad")):
        report = await _migrate(hass)
    assert report["energy"]["safe"] is False
    assert report["energy"]["other_uses"] == [ADV[ROLE_IMPORT_ENERGY]]
    assert "do not validate" in report["energy"]["reason"]
    assert report["energy"]["instructions"][1].startswith(
        f"Grid consumption: replace {ADV[ROLE_IMPORT_ENERGY]} with {E1}"
    )
    text = migration.format_report(report)
    assert "Cannot be changed automatically" in text

    with patch.object(migration, "ENERGY_SOURCE_SCHEMA", side_effect=vol.Invalid("bad")):
        report = await _migrate(hass, dry_run=False, confirm_backup=True)
    assert report["result"]["energy"]["changed"] is False
    assert "Energy dashboard not changed" in migration.format_result(report)
    assert energy.data["energy_sources"][0]["stat_energy_from"] == ADV[ROLE_IMPORT_ENERGY]


async def test_energy_read_back_mismatch_restores(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    await _entry(hass, aioclient_mock, hass_storage)
    await _advanced(hass)
    prefs = await _prefs(hass, ADV[ROLE_IMPORT_ENERGY], ADV[ROLE_EXPORT_ENERGY])
    energy = await async_get_manager(hass)
    real_update = energy.async_update
    calls: list[dict] = []

    async def lossy(update: dict) -> None:
        calls.append(update)
        if len(calls) == 1:  # the switch "does not stick"
            update = {
                "energy_sources": [{**update["energy_sources"][0], "stat_energy_from": "sensor.x"}]
            }
        await real_update(update)

    with patch.object(energy, "async_update", lossy):
        report = await _migrate(hass, dry_run=False, confirm_backup=True)
    assert report["result"]["energy"]["reason"] == "the change did not read back; restored"
    assert energy.data["energy_sources"] == prefs


async def test_energy_manager_unavailable(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    await _entry(hass, aioclient_mock, hass_storage)
    await _advanced(hass)
    with patch.object(migration, "async_get_manager", side_effect=RuntimeError("boom")):
        report = await _migrate(hass)
    assert report["energy"]["reason"] == "Energy preferences unavailable: boom"


async def test_delete_legacy_statistics(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Explicit and only after completion; afterwards undo is refused."""
    await _entry(hass, aioclient_mock, hass_storage)
    await _advanced(hass)
    with pytest.raises(ServiceValidationError, match="completed migration"):
        await hass.services.async_call(
            DOMAIN, "delete_legacy_statistics", {"confirm": True}, blocking=True
        )
    await _migrate(hass, dry_run=False, confirm_backup=True)
    with pytest.raises(ServiceValidationError, match="Set confirm"):
        await hass.services.async_call(
            DOMAIN, "delete_legacy_statistics", {"confirm": False}, blocking=True
        )

    result = await hass.services.async_call(
        DOMAIN, "delete_legacy_statistics", {"confirm": True}, blocking=True, return_response=True
    )

    assert result == {"deleted": sorted(ADV.values())}
    assert all(not rows for rows in (await _rows(hass, list(ADV.values()))).values())
    assert (await _rows(hass))[E1]  # the new statistics are untouched
    with pytest.raises(ServiceValidationError, match="cannot be undone"):
        await hass.services.async_call(DOMAIN, "undo_migration", {}, blocking=True)


async def test_delete_legacy_reports_leftovers(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    await _entry(hass, aioclient_mock, hass_storage)
    await _advanced(hass)
    await _migrate(hass, dry_run=False, confirm_backup=True)
    with (
        patch.object(get_instance(hass), "async_clear_statistics"),
        pytest.raises(ServiceValidationError, match="Still present"),
    ):
        await hass.services.async_call(
            DOMAIN, "delete_legacy_statistics", {"confirm": True}, blocking=True
        )


async def test_undo_failures(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    await _entry(hass, aioclient_mock, hass_storage)
    await _advanced(hass)
    await _prefs(hass, ADV[ROLE_IMPORT_ENERGY], ADV[ROLE_EXPORT_ENERGY])
    await _adv_automation(hass)
    await _migrate(hass, dry_run=False, confirm_backup=True)
    energy = await async_get_manager(hass)

    async def ignore(update: dict) -> None:
        return None

    with (
        patch.object(energy, "async_update", ignore),
        pytest.raises(ServiceValidationError, match="did not read back"),
    ):
        await hass.services.async_call(DOMAIN, "undo_migration", {}, blocking=True)

    real_call = ServiceRegistry.async_call

    async def swallow(self, domain: str, service: str, *args: Any, **kwargs: Any) -> Any:
        if domain == "automation":
            return None
        return await real_call(self, domain, service, *args, **kwargs)

    with (
        patch.object(ServiceRegistry, "async_call", swallow),
        pytest.raises(migration.MigrationRefused, match="did not turn back on"),
    ):
        await migration.async_undo(
            hass, hass.config_entries.async_get_entry(ENTRY_ID).runtime_data.manager
        )


# --- options flow --------------------------------------------------------------------------


async def test_options_flow_migrate_and_undo(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    entry = await _entry(hass, aioclient_mock, hass_storage)
    await _advanced(hass)
    await _prefs(hass, ADV[ROLE_IMPORT_ENERGY], ADV[ROLE_EXPORT_ENERGY])
    await _adv_automation(hass)

    flow = await hass.config_entries.options.async_init(entry.entry_id)
    assert flow["type"] is FlowResultType.MENU
    assert flow["menu_options"] == ["settings", "migrate"]
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "migrate"}
    )
    assert flow["step_id"] == "migrate_confirm"
    text = flow["description_placeholders"]["report"]
    assert "Parity (hourly rules)" in text
    assert "Energy dashboard, before:" in text
    assert f"Automation {ADV_AUTOMATION} (on): will be turned off." in text
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"confirm_backup": False}
    )
    assert flow["errors"] == {"base": "backup_not_confirmed"}
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"confirm_backup": True}
    )
    assert flow["step_id"] == "migrate_result"
    assert "Migration status: completed." in flow["description_placeholders"]["result"]
    assert "Energy dashboard switched" in flow["description_placeholders"]["result"]
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {})
    assert flow["type"] is FlowResultType.CREATE_ENTRY

    flow = await hass.config_entries.options.async_init(entry.entry_id)
    assert flow["menu_options"] == ["settings", "migrate", "undo_migration"]
    migrate = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "migrate"}
    )
    assert "already completed" in migrate["description_placeholders"]["result"]

    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "undo_migration"}
    )
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"confirm": False})
    assert flow["errors"] == {"base": "not_confirmed"}
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"confirm": True})
    assert "Energy dashboard restored: True" in flow["description_placeholders"]["result"]
    assert hass.states.get(ADV_AUTOMATION).state == "on"

    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "migrate"}
    )
    with patch.object(
        migration, "_async_execute", side_effect=migration.MigrationRefused("x", "stopped")
    ):
        flow = await hass.config_entries.options.async_configure(
            flow["flow_id"], {"confirm_backup": True}
        )
    assert flow["description_placeholders"]["result"] == "The migration did not run: stopped"


async def test_options_flow_manual_pick_and_blocked(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    entry = await _entry(hass, aioclient_mock, hass_storage)
    ids = {**ADV, ROLE_IMPORT_COST: "sensor.my_renamed_cost"}
    await _import_legacy(
        hass,
        ids,
        _legacy_rows(
            ids, hourly_days=WINDOW, lump_days=PRE, tweak={(ROLE_IMPORT_ENERGY, WINDOW[1]): 1.0}
        ),
    )

    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "migrate"}
    )
    assert flow["step_id"] == "migrate_pick"
    assert "incomplete" in flow["description_placeholders"]["reason"]
    schema = {str(k): k for k in flow["data_schema"].schema}
    assert set(schema) == {
        ROLE_IMPORT_ENERGY,
        ROLE_EXPORT_ENERGY,
        ROLE_IMPORT_COST,
        ROLE_EXPORT_COST,
    }
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {ROLE_IMPORT_ENERGY: ADV[ROLE_EXPORT_ENERGY], ROLE_EXPORT_ENERGY: ADV[ROLE_EXPORT_ENERGY]},
    )
    assert flow["errors"] == {"base": "invalid_pick"}
    assert "only once" in flow["description_placeholders"]["error"]
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], ids)
    assert flow["step_id"] == "migrate_result"
    text = flow["description_placeholders"]["result"]
    assert text.startswith("The migration cannot run; nothing was changed.")
    assert "FAILED" in text


async def test_options_flow_needs_loaded_entry(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    entry = await _entry(hass, aioclient_mock, hass_storage)
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    assert flow["menu_options"] == ["settings", "migrate"]
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "migrate"}
    )
    assert flow["type"] is FlowResultType.ABORT
    assert flow["reason"] == "not_loaded"


async def test_format_report_manual_and_undo_refused_in_flow(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    await _entry(hass, aioclient_mock, hass_storage)
    report = await _migrate(hass)
    assert "Pick the legacy statistics" in migration.format_report(report)
    paused = migration.format_result(
        {"migration_status": "in_progress", "result": {"paused": "re-base paused: x"}}
    )
    assert "Paused: re-base paused: x" in paused


async def test_manual_daily_pick_with_statistics_automation(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """A manual pick of renamed v1-style statistics: daily rules, sign assumed without an
    export cost, and the automation found by what it imports."""
    await _entry(hass, aioclient_mock, hass_storage)
    ids = {ROLE_IMPORT_ENERGY: "sensor.my_import", ROLE_EXPORT_ENERGY: "sensor.my_export"}
    await _import_legacy(hass, ids, _legacy_rows(ids, hourly_days=[], lump_days=PRE + WINDOW))
    await _automations(
        hass,
        (
            "a1",
            "My Amber import",
            {
                "action": "import_statistics.import_from_json",
                "data": {"entities": [{"id": "sensor.my_import"}]},
            },
        ),
        ("a2", "Unrelated", {"action": "light.turn_on", "target": {"entity_id": "light.x"}}),
    )
    report = await _migrate(hass, **ids)
    assert report["sources"]["rules"] == "daily"
    assert report["sources"]["export_cost_sign"] == -1.0
    assert [a["entity_id"] for a in report["automations"]] == ["automation.my_amber_import"]
    assert report["parity"]["passed"] is True


async def test_unit_and_boundary_problems(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    entry = await _entry(hass, aioclient_mock, hass_storage)
    rows = _legacy_rows(ADV, hourly_days=WINDOW, lump_days=PRE)
    await _import_legacy(hass, ADV, rows, units={ROLE_IMPORT_COST: "USD"})
    report = await _migrate(hass)
    assert report["problems"] == [f"{ADV[ROLE_IMPORT_COST]} is in USD, expected AUD or EUR."]

    spec = next(s for s in entry.runtime_data.manager.ctx.specs if s.statistic_id == E1)
    async_add_external_statistics(
        hass, spec.metadata(), [{"start": BOUNDARY - 5 * HOUR, "state": 0.0, "sum": 0.0}]
    )
    await async_wait_recording_done(hass)
    report = await _migrate(hass)
    assert "is not a NEM day start" in " ".join(report["problems"])


async def test_export_only_switch_and_report_text(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """A grid source using only the legacy export is switched alone; the report text lists
    ignored layouts and problems."""
    await _entry(hass, aioclient_mock, hass_storage)
    await _advanced(hass)
    await _prefs(hass, "sensor.other_import", ADV[ROLE_EXPORT_ENERGY])
    v1 = {ROLE_IMPORT_ENERGY: V1[ROLE_IMPORT_ENERGY], ROLE_EXPORT_ENERGY: V1[ROLE_EXPORT_ENERGY]}
    stale = [TODAY - timedelta(days=d) for d in (12, 11, 10)]
    await _import_legacy(hass, v1, _legacy_rows(v1, hourly_days=[], lump_days=stale))

    report = await _migrate(hass)
    after = report["energy"]["after"][0]
    assert after["stat_energy_from"] == "sensor.other_import"
    assert after["stat_energy_to"] == B1
    text = migration.format_report(report)
    assert "Ignored: the v1 kit" in text
    report["problems"] = ["Something."]
    assert "Problem: Something." in migration.format_report(report)


async def test_seam_and_verify_helpers(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    await _entry(hass, aioclient_mock, hass_storage)
    plan = migration._Plan({}, None, {}, BOUNDARY, {"sensor.none": []}, None, None, [], [], [], [])
    assert await migration._seam_continuous(hass, plan) is False
    with (
        patch.object(importer, "VERIFY_TIMEOUT", 0),
        pytest.raises(importer.VerificationFailedError, match="do not read back"),
    ):
        await importer.async_verify_range(
            hass,
            {E1: [{"start": BOUNDARY - HOUR, "state": 1.0, "sum": 1.0}]},
            BOUNDARY - HOUR,
            BOUNDARY,
        )


async def test_options_flow_undo_refused(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    entry = await _entry(hass, aioclient_mock, hass_storage)
    await _advanced(hass)
    await _migrate(hass, dry_run=False, confirm_backup=True)
    await hass.services.async_call(
        DOMAIN, "delete_legacy_statistics", {"confirm": True}, blocking=True
    )
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "undo_migration"}
    )
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"confirm": True})
    assert flow["description_placeholders"]["result"].startswith("Undo did not run:")
