"""Milestone 9: Energy dashboard setup and its Repairs issue (DESIGN section 18)."""

from datetime import timedelta
from typing import Any
from unittest.mock import patch

from homeassistant.components.energy.data import ENERGY_SOURCE_SCHEMA, async_get_manager
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
import voluptuous as vol

from custom_components.amber_energy_dashboard import energy as energy_mod
from custom_components.amber_energy_dashboard.const import (
    CONF_ADD_TO_ENERGY,
    CONF_CHANNELS,
    CONF_USAGE_MODE,
    DOMAIN,
    ROLE_EXPORT_ENERGY,
    ROLE_IMPORT_ENERGY,
)

from .fake_amber import FakeAmber
from .synthetic import DEFAULT_CHANNELS, site_json
from .test_manager import (  # noqa: F401 - fixtures are used by name
    ALL_IDS,
    ENTRY_ID,
    NOW,
    TODAY,
    YESTERDAY,
    Clock,
    _fast_verify,
    _no_real_timers,
    _preload_store,
    _run,
    _setup,
    _setup_entry,
    clock,
)

E1, E1_COST, B1, B1_COMP, NET = ALL_IDS
ISSUE = f"energy_not_used_{ENTRY_ID}"
OURS = {
    "type": "grid",
    "stat_energy_from": E1,
    "stat_energy_to": B1,
    "stat_cost": E1_COST,
    "entity_energy_price": None,
    "number_energy_price": None,
    "stat_compensation": B1_COMP,
    "entity_energy_price_export": None,
    "number_energy_price_export": None,
    "cost_adjustment_day": 0.0,
}
V1_IMPORT = "sensor.amber_energy_import"


@pytest.fixture
def energy_clock(clock: Clock):
    """The energy module's clock follows the manager's."""
    with patch.object(energy_mod, "_utcnow", clock):
        yield clock


def _grid(imp: str, exp: str | None = None) -> dict[str, Any]:
    return {
        **OURS,
        "stat_energy_from": imp,
        "stat_energy_to": exp,
        "stat_cost": None,
        "stat_compensation": None,
    }


async def _set_sources(hass: HomeAssistant, *sources: dict[str, Any]) -> None:
    energy = await async_get_manager(hass)
    await energy.async_update(
        {"energy_sources": list(sources), "device_consumption": [], "device_consumption_water": []}
    )


async def _sources(hass: HomeAssistant) -> list[dict[str, Any]] | None:
    data = (await async_get_manager(hass)).data
    return None if data is None else data["energy_sources"]


def _issue(hass: HomeAssistant) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, ISSUE)


async def _entry(hass, aioclient_mock, hass_storage, *, days: int = 4, **kw: Any) -> Any:
    """An entry that has imported its first days."""
    _preload_store(hass_storage, retention_days=days)
    entry = await _setup_entry(
        hass, aioclient_mock, FakeAmber(TODAY - timedelta(days=30), YESTERDAY), **kw
    )
    await _run(hass)
    return entry


# --- adding the grid sources -----------------------------------------------------------


async def test_setup_adds_grid_source_once(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, energy_clock: Clock, hass_storage
) -> None:
    """Chosen in the config flow: one grid source with import, export, cost and
    compensation is added at the first setup, after a read-back backup; never again."""
    entry = await _entry(hass, aioclient_mock, hass_storage, data={CONF_ADD_TO_ENERGY: True})

    assert await _sources(hass) == [OURS]
    ENERGY_SOURCE_SCHEMA(await _sources(hass))
    store = entry.runtime_data.manager.store
    record = store.energy_setup
    assert record["added"] == [OURS]
    assert record["requested_by"] == "setup"
    assert record["backup"] is None  # there were no preferences before
    assert record["reason"] is None
    assert (await store.async_read_back("energy_setup"))["added"] == [OURS]
    assert _issue(hass) is None

    # Removing it and reloading does not add it again: it was carried out once.
    await _set_sources(hass)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert await _sources(hass) == []


async def test_setup_never_changes_an_existing_grid_source(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, energy_clock: Clock, hass_storage
) -> None:
    """A grid source that appeared after the config flow is left alone, silently."""
    await _set_sources(hass, _grid("sensor.other_meter"))
    entry = await _entry(hass, aioclient_mock, hass_storage, data={CONF_ADD_TO_ENERGY: True})
    assert await _sources(hass) == [_grid("sensor.other_meter")]
    assert entry.runtime_data.manager.store.energy_setup["reason"] == "grid_exists"


async def test_controlled_load_gets_a_second_grid_source(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, energy_clock: Clock, hass_storage
) -> None:
    channels = (*DEFAULT_CHANNELS, ("E2", "controlledLoad", None))
    await _entry(
        hass,
        aioclient_mock,
        hass_storage,
        site=site_json(channels),
        data={
            CONF_ADD_TO_ENERGY: True,
            CONF_CHANNELS: [{"identifier": i, "type": t, "tariff": tf} for i, t, tf in channels],
        },
    )
    sources = await _sources(hass)
    prefix = E1.removesuffix("_e1_energy")
    assert sources == [
        OURS,
        {
            **OURS,
            "stat_energy_from": f"{prefix}_e2_energy",
            "stat_cost": f"{prefix}_e2_cost",
            "stat_energy_to": None,
            "stat_compensation": None,
        },
    ]
    ENERGY_SOURCE_SCHEMA(sources)


def test_grid_sources_without_a_general_channel() -> None:
    """A site with only a controlled load and a feed-in: the feed-in gets its own source
    with no import meter; a site with only a feed-in likewise."""

    class _Ctx:
        def __init__(self, channels):
            from custom_components.amber_energy_dashboard.statistics import (  # noqa: PLC0415
                ChannelConfig,
                build_specs,
            )

            self.channels = tuple(ChannelConfig(*c) for c in channels)
            self.specs = tuple(build_specs("site", self.channels))

    class _Manager:
        def __init__(self, channels):
            self.ctx = _Ctx(channels)

    both = energy_mod.grid_sources(_Manager([("E2", "controlledLoad"), ("B1", "feedIn")]))
    assert [s["stat_energy_from"] for s in both] == [None, f"{DOMAIN}:site_e2_energy"]
    assert both[0]["stat_compensation"] == f"{DOMAIN}:site_b1_compensation"
    only = energy_mod.grid_sources(_Manager([("B1", "feedIn")]))
    assert [(s["stat_energy_from"], s["stat_energy_to"]) for s in only] == [
        (None, f"{DOMAIN}:site_b1_energy")
    ]
    ENERGY_SOURCE_SCHEMA(both)
    ENERGY_SOURCE_SCHEMA(only)
    assert energy_mod.describe_sources(only) == (
        f"- return to grid {DOMAIN}:site_b1_energy, compensation {DOMAIN}:site_b1_compensation"
    )


@pytest.mark.parametrize("failure", ["read_back", "backup", "invalid", "unavailable"])
async def test_add_failures_change_nothing(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    energy_clock: Clock,
    hass_storage: dict,
    failure: str,
) -> None:
    """A change that doesn't read back is restored; a backup that doesn't read back, a
    schema failure, or unavailable preferences change nothing. The reason is recorded."""
    await _set_sources(hass, {"type": "solar", "stat_energy_from": "sensor.solar"})
    before = await _sources(hass)
    energy = await async_get_manager(hass)
    real_update = energy.async_update
    calls: list[dict] = []

    async def lossy(update: dict) -> None:
        calls.append(update)
        if len(calls) == 1:
            update = {"energy_sources": update["energy_sources"][:-1]}
        await real_update(update)

    patches = {
        "read_back": patch.object(energy, "async_update", lossy),
        "backup": patch(
            "custom_components.amber_energy_dashboard.storage.AmberStore.async_read_back",
            return_value=None,
        ),
        "invalid": patch.object(energy_mod, "ENERGY_SOURCE_SCHEMA", side_effect=vol.Invalid("bad")),
        "unavailable": patch.object(
            energy_mod, "async_get_manager", side_effect=RuntimeError("boom")
        ),
    }
    reasons = {
        "read_back": "The change did not read back; the preferences were restored",
        "backup": "The saved copy of the preferences did not read back",
        "invalid": "The changed preferences do not validate: bad",
        "unavailable": "Energy preferences unavailable",
    }
    with patches[failure]:
        entry = await _entry(hass, aioclient_mock, hass_storage, data={CONF_ADD_TO_ENERGY: True})
    assert await _sources(hass) == before
    record = entry.runtime_data.manager.store.energy_setup
    assert record["reason"] == reasons[failure]
    assert record["added"] is None


# --- the Repairs issue -----------------------------------------------------------------


async def test_issue_raised_24_hours_after_first_import(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, energy_clock: Clock, hass_storage
) -> None:
    """Not before 24 h; then raised (fixable) by the next check; cleared as soon as the
    preferences use the statistics."""
    entry = await _entry(hass, aioclient_mock, hass_storage)
    manager = entry.runtime_data.manager
    assert manager.store.first_import_at == NOW
    assert _issue(hass) is None

    energy_clock.now = NOW + timedelta(hours=23, minutes=59)
    await _run(hass)
    assert _issue(hass) is None

    energy_clock.now = NOW + timedelta(hours=24)
    await _run(hass)
    issue = _issue(hass)
    assert issue is not None
    assert issue.is_fixable
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_placeholders == {"entry": "Amber FAKENMI000"}

    # Any preference that references one of the entry's statistics clears it at once
    # (the energy manager's update listener), here a device using the net cost.
    energy = await async_get_manager(hass)
    await energy.async_update({"device_consumption": [{"stat_consumption": NET}]})
    assert _issue(hass) is None


async def test_issue_after_a_scheduled_attempt_and_on_upgrade(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, energy_clock: Clock, hass_storage
) -> None:
    """An install that already had data (an upgrade) starts its 24 h when the check first
    runs; a skipped, caught-up scheduled attempt also checks."""
    _preload_store(hass_storage, retention_days=4, marker="2026-09-25", last_written="2026-09-25")
    entry = await _setup_entry(
        hass, aioclient_mock, FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    )
    manager = entry.runtime_data.manager
    assert manager.store.first_import_at == NOW  # set by the check once started
    energy_clock.now = NOW + timedelta(days=1)
    await manager._async_scheduled(final=False)
    assert _issue(hass) is not None


@pytest.mark.parametrize("why", ["pricing", "dismissed", "unavailable"])
async def test_issue_not_raised(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    energy_clock: Clock,
    hass_storage: dict,
    why: str,
) -> None:
    """Never in Pricing-only mode or once dismissed (and an existing issue is removed);
    nothing happens while the preferences can't be read."""
    entry = await _entry(hass, aioclient_mock, hass_storage)
    manager = entry.runtime_data.manager
    energy_clock.now = NOW + timedelta(days=2)
    await energy_mod.async_check_energy_issue(hass, manager)
    assert _issue(hass) is not None
    if why == "pricing":
        manager.usage_mode = "pricing"
    elif why == "dismissed":
        await manager.store.async_dismiss_energy_issue()
    ir.async_delete_issue(hass, DOMAIN, ISSUE)
    if why == "unavailable":
        with patch.object(energy_mod, "async_get_manager", side_effect=RuntimeError("boom")):
            await energy_mod.async_check_energy_issue(hass, manager)
        assert _issue(hass) is None
        return
    await energy_mod.async_check_energy_issue(hass, manager)
    assert _issue(hass) is None
    if why == "pricing":
        assert entry.options.get(CONF_USAGE_MODE) is None


async def test_listener_registered_once(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, energy_clock: Clock, hass_storage
) -> None:
    """One energy listener per Home Assistant run, whatever the reloads; none when the
    preferences can't be read."""
    entry = await _entry(hass, aioclient_mock, hass_storage)
    energy = await async_get_manager(hass)
    count = len(energy._update_listeners)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert len(energy._update_listeners) == count
    hass.data.pop(f"{DOMAIN}_energy_listener")
    with patch.object(energy_mod, "async_get_manager", side_effect=RuntimeError("boom")):
        await energy_mod.async_listen_for_changes(hass)
    assert f"{DOMAIN}_energy_listener" not in hass.data


# --- the fix flow ----------------------------------------------------------------------


async def _fix_flow(hass: HomeAssistant) -> dict[str, Any]:
    assert await async_setup_component(hass, "repairs", {})
    manager = repairs_flow_manager(hass)
    assert manager is not None
    return await manager.async_init(DOMAIN, data={"issue_id": ISSUE})


async def _submit(hass: HomeAssistant, flow: dict[str, Any]) -> dict[str, Any]:
    manager = repairs_flow_manager(hass)
    assert manager is not None
    return await manager.async_configure(flow["flow_id"], {})


async def _raised(hass, aioclient_mock, hass_storage, clock: Clock) -> Any:
    entry = await _entry(hass, aioclient_mock, hass_storage)
    clock.now = NOW + timedelta(days=2)
    await energy_mod.async_check_energy_issue(hass, entry.runtime_data.manager)
    assert _issue(hass) is not None
    return entry


async def test_fix_flow_adds_the_grid_source(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, energy_clock: Clock, hass_storage
) -> None:
    """No grid source: the fix adds ours (with the saved copy) and resolves the issue."""
    entry = await _raised(hass, aioclient_mock, hass_storage, energy_clock)
    await _set_sources(hass, {"type": "solar", "stat_energy_from": "sensor.solar"})

    flow = await _fix_flow(hass)
    assert flow["step_id"] == "add"
    assert f"consumption {E1}, cost {E1_COST}" in flow["description_placeholders"]["sources"]
    result = await _submit(hass, flow)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert (await _sources(hass))[1] == OURS
    record = entry.runtime_data.manager.store.energy_setup
    assert record["requested_by"] == "repair"
    assert record["backup"]["energy_sources"] == [
        {"type": "solar", "stat_energy_from": "sensor.solar"}
    ]
    assert _issue(hass) is None


async def test_fix_flow_add_failure_aborts(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, energy_clock: Clock, hass_storage
) -> None:
    await _raised(hass, aioclient_mock, hass_storage, energy_clock)
    flow = await _fix_flow(hass)
    with patch.object(energy_mod, "ENERGY_SOURCE_SCHEMA", side_effect=vol.Invalid("bad")):
        result = await _submit(hass, flow)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "add_failed"
    assert result["description_placeholders"] == {
        "reason": "The changed preferences do not validate: bad"
    }
    assert _issue(hass) is not None


async def test_fix_flow_points_to_the_migration(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, energy_clock: Clock, hass_storage
) -> None:
    """A YAML kit is detected: the fix explains the migration; the issue stays."""
    from .test_migration import V1, _import_legacy, _legacy_rows  # noqa: PLC0415

    await _raised(hass, aioclient_mock, hass_storage, energy_clock)
    ids = {ROLE_IMPORT_ENERGY: V1[ROLE_IMPORT_ENERGY], ROLE_EXPORT_ENERGY: V1[ROLE_EXPORT_ENERGY]}
    days = [TODAY - timedelta(days=d) for d in range(6, 0, -1)]
    await _import_legacy(hass, ids, _legacy_rows(ids, hourly_days=[], lump_days=days))
    await _set_sources(hass, _grid(V1_IMPORT, V1[ROLE_EXPORT_ENERGY]))

    flow = await _fix_flow(hass)
    assert flow["step_id"] == "migrate"
    result = await _submit(hass, flow)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "use_migration"
    assert _issue(hass) is not None


async def test_fix_flow_other_grid_source_dismisses(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, energy_clock: Clock, hass_storage
) -> None:
    """Another grid source: explain how to pick the statistics; submitting dismisses the
    issue for good."""
    entry = await _raised(hass, aioclient_mock, hass_storage, energy_clock)
    await _set_sources(hass, _grid("sensor.other_meter"))

    flow = await _fix_flow(hass)
    assert flow["step_id"] == "other"
    assert f"compensation {B1_COMP}" in flow["description_placeholders"]["sources"]
    result = await _submit(hass, flow)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert await _sources(hass) == [_grid("sensor.other_meter")]
    manager = entry.runtime_data.manager
    assert manager.store.energy_issue_dismissed
    await energy_mod.async_check_energy_issue(hass, manager)
    assert _issue(hass) is None


@pytest.mark.parametrize("why", ["not_loaded", "energy_unavailable"])
async def test_fix_flow_aborts(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    energy_clock: Clock,
    hass_storage: dict,
    why: str,
) -> None:
    entry = await _raised(hass, aioclient_mock, hass_storage, energy_clock)
    if why == "not_loaded":
        await hass.config_entries.async_unload(entry.entry_id)
        flow = await _fix_flow(hass)
    else:
        with patch.object(energy_mod, "async_get_manager", side_effect=RuntimeError("boom")):
            flow = await _fix_flow(hass)
    assert flow["type"] is FlowResultType.ABORT
    assert flow["reason"] == why
