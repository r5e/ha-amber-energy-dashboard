"""Milestone 18 (2.2.0-dev2): the config entry is the Amber account, each site a sub-entry
(DESIGN section 23): the migration from 2.2.0-dev1 and earlier, its crash recovery,
several sites on one account, the flows, and the evening retries. Synthetic data only."""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.amber_energy_dashboard import async_migrate_entry, sites as sites_mod
from custom_components.amber_energy_dashboard.api import RunBudget
from custom_components.amber_energy_dashboard.const import DOMAIN, MODE_RECOVERY
from custom_components.amber_energy_dashboard.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.amber_energy_dashboard.sites import (
    SiteMigrationError,
    _check_migrated,
    async_migrate_to_sites,
    mapping_site_id,
)
from custom_components.amber_energy_dashboard.storage import async_move_store

from .fake_amber import FakeAmber
from .synthetic import SITE_ID, site_json
from .test_manager import (  # noqa: F401 - fixtures are used by name
    ENTRY_ID,
    SITE_KEY,
    TODAY,
    YESTERDAY,
    Clock,
    _fast_verify,
    _no_real_timers,
    _preload_store,
    _run,
    _setup,
    _setup_entry,
    _site_options,
    _site_sub,
    _store_key,
    clock,
)

SITE_2 = "01FAKESITE0000000000000002"
SITE_2_KEY = f"{ENTRY_ID}_{SITE_2.lower()}"
SITE_SUB = f"site_{SITE_ID.lower()}"
CHANNELS = [
    {"identifier": "E1", "type": "general", "tariff": "EA116"},
    {"identifier": "B1", "type": "feedIn", "tariff": None},
]
SITES = [site_json(), site_json(id=SITE_2, nmi="FAKENMI456")]
PINNED = "sensor.amber_energy_dashboard_import_status"


def _site_data(site_id: str, name: str | None, **options: Any) -> dict[str, Any]:
    return {
        "data": {
            "site_id": site_id,
            "nmi": "FAKENMI000" if site_id == SITE_ID else "FAKENMI456",
            "site_name": name,
            "channels": CHANNELS,
            "add_to_energy": False,
            "options": {"schedule_mode": "automatic", **options},
        },
        "subentry_type": "site",
        "title": name or "Site …I000",
        "unique_id": site_id,
    }


def _site_options_data(entry: MockConfigEntry, site_id: str) -> dict[str, Any]:
    return dict(entry.subentries[_sub_id(entry, site_id)].data)


def _copy_store(hass_storage: dict, key: str) -> None:
    """A second site's preloaded store, like the first's."""
    store = dict(hass_storage[_store_key()])
    hass_storage[f"{DOMAIN}.{key}"] = {**store, "key": f"{DOMAIN}.{key}"}


async def _account(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict,
    *,
    subentries: list[dict[str, Any]] | None = None,
    sites: list[dict[str, Any]] | None = None,
) -> tuple[MockConfigEntry, FakeAmber, FakeAmber]:
    """An account entry (2.1) with two sites, "Home" and "Beach house"."""
    _preload_store(hass_storage, retention_days=2)
    _copy_store(hass_storage, SITE_2_KEY)
    first = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    second = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    first.install(aioclient_mock, sites if sites is not None else SITES)
    second.install_usage(aioclient_mock, SITE_2)
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id=ENTRY_ID,
        unique_id=SITE_ID,
        title="Amber Energy Dashboard",
        version=2,
        minor_version=1,
        data={CONF_API_KEY: "psk_test"},
        subentries_data=subentries
        if subentries is not None
        else [_site_data(SITE_ID, "Home"), _site_data(SITE_2, "Beach house")],
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.amber_energy_dashboard.manager.AmberManager.needs_startup_run",
        new=False,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry, first, second


def _sub_id(entry: MockConfigEntry, site_id: str) -> str:
    return next(
        s.subentry_id
        for s in entry.subentries.values()
        if s.subentry_type == "site" and s.data["site_id"] == site_id
    )


# --- migration ---------------------------------------------------------------------------


def _v1_entry(hass: HomeAssistant, **kwargs: Any) -> MockConfigEntry:
    """An entry as 2.2.0-dev1 and earlier stored it: one site, settings in options."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id=ENTRY_ID,
        unique_id=SITE_ID,
        title="Amber Energy Dashboard",
        version=1,
        minor_version=2,
        data={
            CONF_API_KEY: "psk_test",
            "site_id": SITE_ID,
            "nmi": "FAKENMI000",
            "channels": CHANNELS,
        },
        options={"schedule_mode": "fixed", "fixed_times": ["07:15", "10:15"]},
        **kwargs,
    )
    entry.add_to_hass(hass)
    return entry


def _registry_as_2_1(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, str]:
    """The site's device and entities as 2.1.1 left them (no sub-entry), one site entity
    without a device, and a mapping's entity in its own sub-entry."""
    devices, entities = dr.async_get(hass), er.async_get(hass)
    device = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, SITE_ID)},
        name="Amber Energy Dashboard (site)",
    )
    ids = {}
    for key, object_id, on_device in (
        ("status", "amber_energy_dashboard_import_status", True),
        ("run_now", "amber_lag", True),  # a user-renamed entity ID
        ("days_behind", "amber_energy_dashboard_days_behind", False),
    ):
        ids[key] = entities.async_get_or_create(
            "button" if key == "run_now" else "sensor",
            DOMAIN,
            f"{SITE_ID}_{key}",
            config_entry=entry,
            device_id=device.id if on_device else None,
            suggested_object_id=object_id,
        ).entity_id
    ids["mapping"] = entities.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{SITE_ID}_reconciliation_sub_house",
        config_entry=entry,
        config_subentry_id="sub_house",
        suggested_object_id="house_meter_own_sensor_reconciliation",
    ).entity_id
    return ids


async def test_upgrade_moves_the_site_into_a_sub_entry(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """A 2.1-layout entry becomes an account with a site sub-entry: the same entry ID,
    settings moved, mappings linked to the site, device and entities moved with their
    entity IDs, the Store moved to the site's key, and old per-entry issues removed."""
    _preload_store(hass_storage, retention_days=2)
    hass_storage[f"{DOMAIN}.{ENTRY_ID}"] = {
        **hass_storage.pop(_store_key()),
        "key": f"{DOMAIN}.{ENTRY_ID}",
    }
    entry = _v1_entry(
        hass,
        subentries_data=[
            {
                "data": {"entity_id": "sensor.house", "channel": "E1"},
                "subentry_id": "sub_house",
                "subentry_type": "own_sensor",
                "title": "House (general E1)",
                "unique_id": "sensor.house",
            }
        ],
    )
    ids = _registry_as_2_1(hass, entry)
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"behind_{ENTRY_ID}",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="behind",
    )
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    fake.install(aioclient_mock, [site_json()])
    with patch(
        "custom_components.amber_energy_dashboard.manager.AmberManager.needs_startup_run",
        new=False,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert (entry.entry_id, entry.version, entry.minor_version) == (ENTRY_ID, 2, 1)
    assert dict(entry.data) == {CONF_API_KEY: "psk_test"}
    assert dict(entry.options) == {}
    site = entry.subentries[SITE_SUB]
    assert (site.subentry_type, site.unique_id, site.title) == ("site", SITE_ID, "Site …I000")
    assert site.data["options"] == {"schedule_mode": "fixed", "fixed_times": ["07:15", "10:15"]}
    assert entry.subentries["sub_house"].data["site_id"] == SITE_ID
    devices, entities = dr.async_get(hass), er.async_get(hass)
    device = devices.async_get_device_by_identifier((DOMAIN, SITE_ID), entry.entry_id)
    assert device.config_subentry_id == SITE_SUB
    for key in ("status", "run_now", "days_behind"):
        entity = entities.async_get(ids[key])
        assert entity is not None and entity.config_subentry_id == SITE_SUB, key
    assert entities.async_get(ids["mapping"]).config_subentry_id == "sub_house"
    assert ids["run_now"] == "button.amber_lag"
    assert f"{DOMAIN}.{ENTRY_ID}" not in hass_storage
    assert hass_storage[_store_key()]["data"]["retention_boundary"] is not None
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"behind_{ENTRY_ID}") is None
    manager = entry.runtime_data.manager
    assert manager.schedule.mode == "fixed"
    assert (await _run(hass))["status"] == "caught_up"


async def test_a_kill_before_the_registries_are_saved_is_finished_at_the_next_start(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Home Assistant saves the registries after the config entries: after a kill the
    entry is migrated but the device and entities are not. The next setup moves them,
    before any platform adds an entity, with no entity ID changed."""
    _preload_store(hass_storage, retention_days=2)
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY)
    entry = await _setup_entry(hass, aioclient_mock, fake)
    entities, devices = er.async_get(hass), dr.async_get(hass)
    before = {
        e.unique_id: e.entity_id for e in er.async_entries_for_config_entry(entities, ENTRY_ID)
    }
    await hass.config_entries.async_unload(entry.entry_id)
    device = devices.async_get_device_by_identifier((DOMAIN, SITE_ID), ENTRY_ID)
    devices.async_update_device(device.id, new_config_subentry_id=None)
    for entity in er.async_entries_for_config_entry(entities, ENTRY_ID):
        entities.async_update_entity(entity.entity_id, config_subentry_id=None)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert devices.async_get(device.id).config_subentry_id == SITE_SUB
    after = er.async_entries_for_config_entry(entities, ENTRY_ID)
    assert {e.unique_id: e.entity_id for e in after} == before
    assert {e.config_subentry_id for e in after} == {SITE_SUB}
    assert all(hass.states.get(e.entity_id) is not None for e in after)


async def test_migration_is_idempotent(
    hass: HomeAssistant, clock: Clock, hass_storage: dict
) -> None:
    """Run again after a crash halfway: an existing site sub-entry is kept, and a Store
    already at the site's key wins (the old one is removed)."""
    _preload_store(hass_storage, retention_days=2, marker="2026-09-20")
    hass_storage[f"{DOMAIN}.{ENTRY_ID}"] = {
        **hass_storage[_store_key()],
        "key": f"{DOMAIN}.{ENTRY_ID}",
        "data": {**hass_storage[_store_key()]["data"], "marker": "2000-01-01"},
    }
    entry = _v1_entry(hass)
    await async_migrate_to_sites(hass, entry)
    assert f"{DOMAIN}.{ENTRY_ID}" not in hass_storage
    assert hass_storage[_store_key()]["data"]["marker"] == "2026-09-20"  # the new one won
    hass.config_entries.async_update_entry(
        entry,
        data={CONF_API_KEY: "psk_test", "site_id": SITE_ID, "nmi": "N", "channels": CHANNELS},
        version=1,
        minor_version=2,
    )
    await async_migrate_to_sites(hass, entry)  # again, with the sub-entry in place
    assert [s.subentry_id for s in entry.subentries.values()] == [SITE_SUB]
    assert await async_move_store(hass, "nothing-here", "nor-here") == "nothing"


async def test_migration_failures(hass: HomeAssistant, clock: Clock, hass_storage: dict) -> None:
    """A Store copy that doesn't read back, or a step that doesn't, fails the migration
    (logged; Home Assistant retries it at the next start); nothing is half-claimed."""
    _preload_store(hass_storage, retention_days=2)
    hass_storage[f"{DOMAIN}.{ENTRY_ID}"] = {
        **hass_storage.pop(_store_key()),
        "key": f"{DOMAIN}.{ENTRY_ID}",
    }
    entry = _v1_entry(hass)
    real = sites_mod.async_move_store

    async def unreadable(hass: HomeAssistant, old: str, new: str, today: Any) -> str:
        with patch(
            "custom_components.amber_energy_dashboard.storage.Store.async_load",
            side_effect=[hass_storage[f"{DOMAIN}.{ENTRY_ID}"]["data"], None, {"other": 1}],
        ):
            return await real(hass, old, new, today)

    with patch.object(sites_mod, "async_move_store", unreadable):
        assert await async_migrate_entry(hass, entry) is False
    assert entry.version == 1
    assert f"{DOMAIN}.{ENTRY_ID}" in hass_storage  # the old store is kept
    with patch.object(sites_mod, "async_ensure_site_ownership"):
        devices = dr.async_get(hass)
        devices.async_get_or_create(config_entry_id=ENTRY_ID, identifiers={(DOMAIN, SITE_ID)})
        assert await async_migrate_entry(hass, entry) is False  # the device didn't move
    assert await async_migrate_entry(hass, entry) is True
    assert entry.version == 2


async def test_check_migrated_reports_every_problem(hass: HomeAssistant, clock: Clock) -> None:
    """The read-back names each part that didn't migrate."""
    entry = _v1_entry(
        hass,
        subentries_data=[
            {
                "data": {"entity_id": "sensor.house", "channel": "E1"},
                "subentry_id": "sub_house",
                "subentry_type": "own_sensor",
                "title": "House",
                "unique_id": "sensor.house",
            }
        ],
    )
    _registry_as_2_1(hass, entry)
    with pytest.raises(SiteMigrationError) as err:
        _check_migrated(hass, entry, SITE_ID, SITE_SUB)
    assert str(err.value) == (
        "migration to site sub-entries: site sub-entry, mappings without a site, site device, "
        "site entities, entry version or data"
    )
    assert mapping_site_id(entry, {}) is None  # no site yet
    assert mapping_site_id(entry, {"site_id": SITE_2}) == SITE_2


# --- several sites on one account --------------------------------------------------------


async def test_two_sites_share_the_account(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Each site has its manager, Store, device and entities in its sub-entry; they share
    the client, the run lock and the rate limit. The first site keeps the 2.1.0 entity
    IDs; the second's follow its device's name."""
    entry, first, _ = await _account(hass, aioclient_mock, hass_storage)
    runtime = entry.runtime_data
    home, beach = runtime.sites.values()
    assert home.context.site_id == SITE_ID and beach.context.site_id == SITE_2
    assert home.context.client is beach.context.client is runtime.client
    assert home.context.lock is beach.context.lock is runtime.lock
    assert (home.manager.site_key, beach.manager.site_key) == (SITE_KEY, SITE_2_KEY)
    assert beach.manager.site_label == "Amber Energy Dashboard (Beach house)"
    devices, entities = dr.async_get(hass), er.async_get(hass)
    assert devices.async_get(home.device_id).name == "Home"
    assert devices.async_get(beach.device_id).config_subentry_id == beach.subentry_id
    assert entities.async_get(PINNED).config_subentry_id == home.subentry_id
    status = entities.async_get("sensor.beach_house_import_status")
    assert status.config_subentry_id == beach.subentry_id
    assert entities.async_get("button.beach_house_run_now") is not None
    assert runtime.last_import is None and runtime.last_error is None

    # Runs take turns on one rate limit: the second starts from what the first saw.
    first.remaining = 3
    result = await hass.services.async_call(
        DOMAIN, "run_now", {"site": home.device_id}, blocking=True, return_response=True
    )
    assert result["rate_limit_remaining"] == {"sites_usage": 2}
    result = await hass.services.async_call(
        DOMAIN, "run_now", {"site": SITE_2.lower()}, blocking=True, return_response=True
    )
    assert result["status"] == "budget_exhausted"
    assert result["calls"] == {}

    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert set(diag["sites"]) == {home.subentry_id, beach.subentry_id}


async def test_services_target_a_site(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """With several sites, site (device, Amber site ID or sub-entry ID) chooses one;
    config_entry_id alone is no longer enough."""
    entry, _, _ = await _account(hass, aioclient_mock, hass_storage)
    beach = entry.runtime_data.sites[_sub_id(entry, SITE_2)]

    async def run(**data: Any) -> dict[str, Any]:
        return await hass.services.async_call(
            DOMAIN, "run_now", data, blocking=True, return_response=True
        )

    with pytest.raises(ServiceValidationError, match="pass site"):
        await run(config_entry_id=ENTRY_ID)
    with pytest.raises(ServiceValidationError, match="No loaded amber_energy_dashboard site x"):
        await run(site="x")
    assert (await run(site=beach.subentry_id, config_entry_id=ENTRY_ID))["status"] == "caught_up"
    assert beach.manager.store.marker == YESTERDAY


async def test_removing_a_site_removes_its_store(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Removing a site sub-entry deletes its Store and reloads; statistics are kept. Then
    removing the entry deletes the other site's Store."""
    entry, _, _ = await _account(hass, aioclient_mock, hass_storage)
    hass.config_entries.async_remove_subentry(entry, _sub_id(entry, SITE_2))
    await hass.async_block_till_done()
    assert f"{DOMAIN}.{SITE_2_KEY}" not in hass_storage
    assert list(entry.runtime_data.sites) == [_sub_id(entry, SITE_ID)]
    assert hass.states.get("sensor.beach_house_import_status") is None
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert _store_key() not in hass_storage


async def test_a_site_the_key_no_longer_sees_is_skipped(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """A configured site the key no longer sees (a closed site) is skipped with a Repairs
    issue for that site; the account's other sites keep running (2.2.0-dev3)."""
    entry, _, _ = await _account(hass, aioclient_mock, hass_storage, sites=[site_json()])
    assert entry.state is ConfigEntryState.LOADED
    runtime = entry.runtime_data
    assert list(runtime.sites) == [_sub_id(entry, SITE_ID)]
    assert runtime.skipped == {_sub_id(entry, SITE_2): SITE_2}
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"site_not_found_{SITE_2_KEY}")
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.translation_placeholders == {
        "entry": "Amber Energy Dashboard",
        "site": "Beach house",
    }
    assert issue.data == {"entry_id": ENTRY_ID, "site_id": SITE_2}
    assert not any(
        flow["context"]["source"] == "reauth" for flow in hass.config_entries.flow.async_progress()
    )
    # The remaining site runs, and services find it without naming it.
    result = await _run(hass)
    assert result["status"] == "caught_up"
    # A settings change on the running site doesn't reload for the skipped one.
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        hass.config_entries.async_update_subentry(
            entry,
            entry.subentries[_sub_id(entry, SITE_ID)],
            data={
                **_site_options_data(entry, SITE_ID),
                "options": {"schedule_mode": "automatic", "patience_days": 5},
            },
        )
        await hass.async_block_till_done()
    reload.assert_not_called()
    # Once the key sees the site again, the next setup clears the issue.
    aioclient_mock.clear_requests()
    FakeAmber(TODAY - timedelta(days=30), YESTERDAY).install(aioclient_mock, SITES)
    FakeAmber(TODAY - timedelta(days=30), YESTERDAY).install_usage(aioclient_mock, SITE_2)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.skipped == {}
    assert len(entry.runtime_data.sites) == 2
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"site_not_found_{SITE_2_KEY}") is None


async def test_deleting_a_skipped_site(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """Deleting a skipped site removes its Store and its issue; the entry reloads with
    the remaining site."""
    entry, _, _ = await _account(hass, aioclient_mock, hass_storage, sites=[site_json()])
    assert f"{DOMAIN}.{SITE_2_KEY}" in hass_storage
    assert hass.config_entries.async_remove_subentry(entry, _sub_id(entry, SITE_2))
    await hass.async_block_till_done()
    assert f"{DOMAIN}.{SITE_2_KEY}" not in hass_storage
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"site_not_found_{SITE_2_KEY}") is None
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.skipped == {}
    assert len(entry.runtime_data.sites) == 1


async def test_a_key_that_sees_none_of_the_sites_starts_reauth(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """With none of the configured sites visible, setup starts reauth; the new key must
    see at least one of them, and a skipped site's issue is removed with the entry."""
    other = site_json(id="01FAKESITE0000000000000009")
    entry, _, _ = await _account(hass, aioclient_mock, hass_storage, sites=[other])
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(
        flow["context"]["source"] == "reauth" for flow in hass.config_entries.flow.async_progress()
    )
    flow = await entry.start_reauth_flow(hass)
    flow = await hass.config_entries.flow.async_configure(flow["flow_id"], {CONF_API_KEY: "k"})
    assert flow["errors"] == {"base": "site_not_found"}
    aioclient_mock.clear_requests()
    aioclient_mock.get("https://api.amber.com.au/v1/sites", json=[site_json()])
    flow = await hass.config_entries.flow.async_configure(flow["flow_id"], {CONF_API_KEY: "k2"})
    assert flow["reason"] == "reauth_successful"
    assert entry.data[CONF_API_KEY] == "k2"
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"site_not_found_{SITE_2_KEY}") is not None
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"site_not_found_{SITE_2_KEY}") is None


# --- flows -------------------------------------------------------------------------------


async def test_add_site_flow(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """ "+ Site" offers the sites the key sees that aren't set up anywhere, asks for a
    name, confirms the channels and adds the site (automatic schedule)."""
    entry, _, _ = await _account(
        hass, aioclient_mock, hass_storage, subentries=[_site_data(SITE_ID, "Home")]
    )

    async def start() -> dict[str, Any]:
        return await hass.config_entries.subentries.async_init(
            (entry.entry_id, "site"), context={"source": "user"}
        )

    flow = await start()
    options = flow["data_schema"].schema["site_id"].config["options"]
    assert options == [{"value": SITE_2, "label": "NMI …I456 (Example Network)"}]
    flow = await hass.config_entries.subentries.async_configure(
        flow["flow_id"], {"site_id": SITE_2}
    )
    assert flow["step_id"] == "site_name"
    flow = await hass.config_entries.subentries.async_configure(flow["flow_id"], {"site_name": " "})
    assert flow["errors"] == {"site_name": "name_required"}
    flow = await hass.config_entries.subentries.async_configure(
        flow["flow_id"], {"site_name": "Beach house"}
    )
    assert flow["step_id"] == "new_channels"
    assert flow["description_placeholders"]["nmi"] == "…I456"
    flow = await hass.config_entries.subentries.async_configure(flow["flow_id"], {})
    assert flow["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    beach = entry.subentries[_sub_id(entry, SITE_2)]
    assert beach.title == "Beach house"
    assert beach.data["options"] == {"schedule_mode": "automatic"}
    assert len(entry.runtime_data.sites) == 2
    assert hass.states.get("sensor.beach_house_import_status") is not None

    flow = await start()
    assert (flow["type"], flow["reason"]) == (FlowResultType.ABORT, "no_new_sites")
    aioclient_mock.clear_requests()
    aioclient_mock.get("https://api.amber.com.au/v1/sites", status=500)
    flow = await start()
    assert flow["reason"] == "cannot_connect"


async def test_add_site_refuses_unsupported_channels_and_configured_sites(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """A site with a channel type the integration can't model is refused; a site set up
    in another entry can't be set up again from the config flow."""
    odd = site_json([("E1", "general", None), ("X1", "battery", None)], id=SITE_2)
    entry, _, _ = await _account(
        hass,
        aioclient_mock,
        hass_storage,
        subentries=[_site_data(SITE_ID, "Home")],
        sites=[site_json(), odd],
    )
    flow = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "site"), context={"source": "user"}
    )
    flow = await hass.config_entries.subentries.async_configure(
        flow["flow_id"], {"site_id": SITE_2}
    )
    flow = await hass.config_entries.subentries.async_configure(
        flow["flow_id"], {"site_name": "Odd"}
    )
    assert (flow["type"], flow["reason"]) == (FlowResultType.ABORT, "unsupported_channels")

    # SITE_2 is a site of another account entry (whose own unique ID is its first site).
    MockConfigEntry(
        domain=DOMAIN,
        unique_id="01SOMEONEELSE",
        version=2,
        minor_version=1,
        data={CONF_API_KEY: "other"},
        subentries_data=[_site_data(SITE_2, "Elsewhere")],
    ).add_to_hass(hass)
    aioclient_mock.clear_requests()
    aioclient_mock.get("https://api.amber.com.au/v1/sites", json=SITES)
    flow = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    flow = await hass.config_entries.flow.async_configure(flow["flow_id"], {CONF_API_KEY: "k"})
    flow = await hass.config_entries.flow.async_configure(flow["flow_id"], {"site_id": SITE_2})
    assert (flow["type"], flow["reason"]) == (FlowResultType.ABORT, "already_configured")


async def test_site_settings_from_the_cog_and_the_pencil(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """The entry's options ask which site, then show its settings; the site's reconfigure
    flow shows the same menu. Either saves the site's settings, applied in place."""
    entry, _, _ = await _account(hass, aioclient_mock, hass_storage)
    beach_id = _sub_id(entry, SITE_2)
    beach = entry.runtime_data.sites[beach_id].manager

    flow = await hass.config_entries.options.async_init(entry.entry_id)
    assert flow["step_id"] == "site"
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"site": beach_id})
    assert flow["step_id"] == "init"  # the chosen site's menu
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "settings"}
    )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"schedule_mode": "fixed", "fixed_times": "09:00"}
    )
    await hass.async_block_till_done()
    assert flow["type"] is FlowResultType.CREATE_ENTRY
    assert entry.subentries[beach_id].data["options"]["fixed_times"] == ["09:00"]
    assert beach.schedule.mode == "fixed"
    assert _site_options(entry)["schedule_mode"] == "automatic"  # Home unchanged

    home_id = _sub_id(entry, SITE_ID)
    flow = await entry.start_subentry_reconfigure_flow(hass, home_id)
    assert flow["step_id"] == "reconfigure"
    assert flow["menu_options"] == [
        "settings",
        "sources",
        "bill",
        "allowance",
        "rename",
        "channels",
        "migrate",
    ]
    flow = await hass.config_entries.subentries.async_configure(
        flow["flow_id"], {"next_step_id": "settings"}
    )
    flow = await hass.config_entries.subentries.async_configure(
        flow["flow_id"], {"schedule_mode": "automatic", "patience_days": 9}
    )
    assert (flow["type"], flow["reason"]) == (FlowResultType.ABORT, "reconfigure_successful")
    await hass.async_block_till_done()
    assert entry.subentries[home_id].data["options"]["patience_days"] == 9
    assert entry.runtime_data.sites[home_id].manager.patience_days == 9

    flow = await entry.start_subentry_reconfigure_flow(hass, home_id)
    handler = hass.config_entries.subentries._progress[flow["flow_id"]]
    handler._sub_id = home_id
    result = await handler.async_step_migrate_result({})
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "settings_closed")


async def test_rename_site(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """ "Rename site" (2.2.0-dev3) sets the sub-entry's title and the device's name,
    replacing a name given on the device page; entity IDs don't change. A default entry
    title that names the first site follows."""
    other = MockConfigEntry(
        domain=DOMAIN, unique_id="01OTHERSITE", title="Amber Energy Dashboard (…9999)"
    )
    other.add_to_hass(hass)
    entry, _, _ = await _account(hass, aioclient_mock, hass_storage)
    assert entry.title == "Amber Energy Dashboard (Home)"
    devices, entities = dr.async_get(hass), er.async_get(hass)
    beach_id = _sub_id(entry, SITE_2)
    beach = entry.runtime_data.sites[beach_id]
    devices.async_update_device(beach.device_id, name_by_user="Shack")
    await hass.async_block_till_done()
    before = sorted(
        e.entity_id for e in er.async_entries_for_config_entry(entities, entry.entry_id)
    )

    flow = await entry.start_subentry_reconfigure_flow(hass, beach_id)
    flow = await hass.config_entries.subentries.async_configure(
        flow["flow_id"], {"next_step_id": "rename"}
    )
    assert flow["step_id"] == "rename"
    flow = await hass.config_entries.subentries.async_configure(
        flow["flow_id"], {"site_name": "  "}
    )
    assert flow["errors"] == {"site_name": "name_required"}
    flow = await hass.config_entries.subentries.async_configure(
        flow["flow_id"], {"site_name": "Holiday house"}
    )
    assert (flow["type"], flow["reason"]) == (FlowResultType.ABORT, "reconfigure_successful")
    await hass.async_block_till_done()
    sub = entry.subentries[beach_id]
    assert (sub.title, sub.data["site_name"]) == ("Holiday house", "Holiday house")
    device = devices.async_get(beach.device_id)
    assert (device.name, device.name_by_user) == ("Holiday house", None)
    assert beach.manager.site_label == "Amber Energy Dashboard (Home) (Holiday house)"
    assert entry.title == "Amber Energy Dashboard (Home)"  # names the first site

    # The first site, from the cog: the entry's default title follows.
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"site": _sub_id(entry, SITE_ID)}
    )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "rename"}
    )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"site_name": "Town house"}
    )
    assert flow["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.subentries[_sub_id(entry, SITE_ID)].title == "Town house"
    assert devices.async_get(entry.runtime_data.device_id).name == "Town house"
    assert entry.title == "Amber Energy Dashboard (Town house)"
    after = sorted(e.entity_id for e in er.async_entries_for_config_entry(entities, entry.entry_id))
    assert after == before
    assert entry.state is ConfigEntryState.LOADED


async def test_options_need_a_site(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """An account without sites has no settings to show, and no grid meter or appliance
    to add."""
    entry, _, _ = await _account(hass, aioclient_mock, hass_storage, subentries=[])
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    assert (flow["type"], flow["reason"]) == (FlowResultType.ABORT, "no_sites")
    flow = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "appliance"), context={"source": "user"}
    )
    assert (flow["type"], flow["reason"]) == (FlowResultType.ABORT, "no_sites")


async def test_mapping_flows_ask_which_site(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """With several sites, a grid meter (or appliance) first asks which site it belongs
    to; its device links to that site's device."""
    entry, _, _ = await _account(hass, aioclient_mock, hass_storage)
    beach_id = _sub_id(entry, SITE_2)
    hass.states.async_set(
        "sensor.beach_import",
        "1",
        {"device_class": "energy", "state_class": "total_increasing", "unit_of_measurement": "kWh"},
    )
    flow = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "grid_meter"), context={"source": "user"}
    )
    assert flow["step_id"] == "site"
    flow = await hass.config_entries.subentries.async_configure(flow["flow_id"], {"site": beach_id})
    assert flow["step_id"] == "user"
    flow = await hass.config_entries.subentries.async_configure(
        flow["flow_id"], {"name": "Beach meter", "import_entity_id": "sensor.beach_import"}
    )
    assert flow["data"]["site_id"] == SITE_2
    await hass.async_block_till_done()
    beach = entry.runtime_data.sites[beach_id]
    assert [s.entity_id for s in beach.manager.own_sensors] == ["sensor.beach_import"]
    assert entry.runtime_data.site.manager.own_sensors == []
    recon = er.async_get(hass).async_get("sensor.beach_meter_import_reconciliation")
    device = dr.async_get(hass).async_get(recon.device_id)
    assert device.via_device_id == beach.device_id


async def test_repairs_find_their_site(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """A fix flow finds its site by site_id (the entry's first site without one); an
    unknown site aborts."""
    await _account(hass, aioclient_mock, hass_storage)
    assert await async_setup_component(hass, "repairs", {})
    manager = repairs_flow_manager(hass)
    for data, outcome in (
        ({"entry_id": ENTRY_ID, "site_id": "01NOWHERE"}, "not_loaded"),
        ({"entry_id": ENTRY_ID}, "add"),
    ):
        issue_id = f"energy_not_used_{len(data)}"
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="energy_not_used",
            data=data,
        )
        flow = await manager.async_init(DOMAIN, data={"issue_id": issue_id})
        assert flow.get("reason", flow.get("step_id")) == outcome


# --- evening retries ---------------------------------------------------------------------


async def test_a_manual_import_cancels_the_evening_retries(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: Clock, hass_storage: dict
) -> None:
    """While waiting after the last daytime attempt the next attempt is an evening retry;
    once the day imports (here a Run now) the next attempt is tomorrow's first. Recovery-
    only mode, which schedules nothing, has no evening retries."""
    await hass.config.async_set_time_zone("Australia/Sydney")
    _preload_store(hass_storage, retention_days=2)
    fake = FakeAmber(TODAY - timedelta(days=30), YESTERDAY - timedelta(days=1))
    entry = await _setup_entry(
        hass,
        aioclient_mock,
        fake,
        options={"schedule_mode": "fixed", "fixed_times": ["07:15", "10:15", "13:15"]},
    )
    mgr = entry.runtime_data.manager
    last_daytime = datetime(2026, 9, 26, 3, 15, tzinfo=UTC)  # 13:15 AEST
    clock.now = last_daytime
    mgr._on_timer(last_daytime)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mgr.status == "waiting_for_data"
    assert mgr.next_run == last_daytime + timedelta(hours=3)
    fake.latest = YESTERDAY
    clock.now += timedelta(hours=1)
    assert (await _run(hass))["status"] == "caught_up"
    assert mgr.next_run == datetime(2026, 9, 26, 21, 15, tzinfo=UTC)  # tomorrow 07:15
    mgr.usage_mode = MODE_RECOVERY
    assert mgr._evening_retry_after(last_daytime) is False


def test_run_budget_carries_the_accounts_rate_limit() -> None:
    """A run starts from the last rate-limit state the account's previous run saw."""
    from custom_components.amber_energy_dashboard.api import AmberClient  # noqa: PLC0415

    client = AmberClient(None, "k")  # type: ignore[arg-type]
    first = client.start_run(RunBudget())
    first.remaining["sites_usage"] = 7
    first._window_ends["sites_usage"] = 123.0
    client.end_run()
    second = client.start_run()
    assert second.remaining == {"sites_usage": 7}
    assert second._window_ends == {"sites_usage": 123.0}
    assert second.calls == {}


def test_store_default_clock_is_the_nem_date() -> None:
    """A Store opened without the manager's clock uses today's NEM date."""
    from custom_components.amber_energy_dashboard.manager import nem_today  # noqa: PLC0415
    from custom_components.amber_energy_dashboard.storage import _nem_today  # noqa: PLC0415

    assert _nem_today() == nem_today()
