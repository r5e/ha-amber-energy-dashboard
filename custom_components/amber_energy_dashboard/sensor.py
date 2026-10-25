"""Display-only sensors (DESIGN section 6).

None of these has a state_class, so Home Assistant never compiles them into statistics.
The Energy dashboard uses the external statistics, not these sensors.
"""

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfPower, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator
from homeassistant.util import slugify

from . import AmberConfigEntry, SiteRuntime, undetected_prices
from .const import (
    CHANNEL_FEED_IN,
    CONF_ENTITY_IDS,
    DOMAIN,
    ROLE_EXPORT,
    ROLE_IMPORT,
    ROLE_SOLAR_AWARE,
    SUBENTRY_APPLIANCE,
    SUBENTRY_GRID_METER,
)
from .devices import ENTITY_ID_PREFIX, async_site_entity_ids, site_link
from .manager import STATUSES, OwnSensor
from .meters import mapping_device_info
from .statistics import CURRENCY, Metric

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class AmberSensorDescription(SensorEntityDescription):
    """A sensor computed from the manager snapshot."""

    value_fn: Callable[[dict[str, Any]], Any]
    attrs_fn: Callable[[dict[str, Any]], dict[str, Any] | None] = lambda _: None


def _last_run_attrs(data: dict[str, Any]) -> dict[str, Any] | None:
    last = data.get("last_run") or {}
    keys = ("trigger", "reason", "finished", "imported_days", "waiting_for", "calls")
    return {k: last.get(k) for k in keys if last.get(k) is not None} or None


STATUS_SENSORS = (
    AmberSensorDescription(
        key="status",
        translation_key="status",
        device_class=SensorDeviceClass.ENUM,
        options=STATUSES,
        value_fn=lambda d: d["status"],
        attrs_fn=_last_run_attrs,
    ),
    AmberSensorDescription(
        key="last_imported_date",
        translation_key="last_imported_date",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda d: d["last_imported"],
    ),
    AmberSensorDescription(
        key="days_behind",
        translation_key="days_behind",
        value_fn=lambda d: d["days_behind"],
    ),
    AmberSensorDescription(
        key="next_run",
        translation_key="next_run",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d["next_run"],
    ),
)


def _bill(key: str) -> Callable[[dict[str, Any]], Any]:
    def value(data: dict[str, Any]) -> Any:
        return None if data.get("bill") is None else data["bill"][key]

    return value


def _bill_attrs(keys: tuple[str, ...]) -> Callable[[dict[str, Any]], dict[str, Any] | None]:
    def attrs(data: dict[str, Any]) -> dict[str, Any] | None:
        bill = data.get("bill")
        return None if bill is None else {k: bill[k] for k in keys}

    return attrs


_CYCLE = ("cycle_start", "cycle_end", "days_in_cycle")
_DATA = ("data_from", "data_through", "days_elapsed", "complete_from_cycle_start")
BILL_SENSORS = (
    AmberSensorDescription(
        key="bill_to_date",
        translation_key="bill_to_date",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY,
        suggested_display_precision=2,
        value_fn=_bill("bill_to_date"),
        attrs_fn=_bill_attrs(
            (
                "lines",
                *_CYCLE,
                *_DATA,
                "missing_days",
                "gst_percent",
                "including_today_estimated",
                "including_today_leaves_out",
            )
        ),
    ),
    AmberSensorDescription(
        key="projected_bill",
        translation_key="projected_bill",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY,
        suggested_display_precision=2,
        value_fn=_bill("projected_bill"),
        attrs_fn=_bill_attrs((*_CYCLE, *_DATA, "days_imported")),
    ),
    AmberSensorDescription(
        key="days_into_cycle",
        translation_key="days_into_cycle",
        native_unit_of_measurement=UnitOfTime.DAYS,
        value_fn=_bill("days_into_cycle"),
        attrs_fn=_bill_attrs(_CYCLE),
    ),
    AmberSensorDescription(
        key="average_cost_per_day",
        translation_key="average_cost_per_day",
        native_unit_of_measurement=f"{CURRENCY}/{UnitOfTime.DAYS}",
        suggested_display_precision=2,
        value_fn=_bill("average_cost_per_day"),
        attrs_fn=_bill_attrs(("data_through", "days_elapsed")),
    ),
)


def _previous_bill_value(data: dict[str, Any]) -> float | None:
    previous = data.get("previous_bill")
    return None if previous is None else previous["bill_to_date"]


def _previous_bill_attrs(data: dict[str, Any]) -> dict[str, Any] | None:
    previous = data.get("previous_bill")
    if previous is None:
        return None
    keys = (
        "lines",
        *_CYCLE,
        *_DATA,
        "days_imported",
        "missing_days",
        "complete",
        "gst_percent",
    )
    return {k: previous[k] for k in keys}


PREVIOUS_BILL_SENSOR = AmberSensorDescription(
    key="previous_bill",
    translation_key="previous_bill",
    device_class=SensorDeviceClass.MONETARY,
    native_unit_of_measurement=CURRENCY,
    suggested_display_precision=2,
    value_fn=_previous_bill_value,
    attrs_fn=_previous_bill_attrs,
)
BILL_SENSORS = (*BILL_SENSORS, PREVIOUS_BILL_SENSOR)


def _allowance(key: str) -> Callable[[dict[str, Any]], Any]:
    def value(data: dict[str, Any]) -> Any:
        return None if data.get("allowance") is None else data["allowance"][key]

    return value


def _allowance_attrs(data: dict[str, Any]) -> dict[str, Any] | None:
    summary = data.get("allowance")
    if summary is None:
        return None
    return {
        k: v
        for k, v in summary.items()
        if k not in ("allowance_used_kwh", "allowance_remaining_kwh", "export_charge")
    }


ALLOWANCE_SENSORS = (
    AmberSensorDescription(
        key="allowance_used",
        translation_key="allowance_used",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        value_fn=_allowance("allowance_used_kwh"),
        attrs_fn=_allowance_attrs,
    ),
    AmberSensorDescription(
        key="allowance_remaining",
        translation_key="allowance_remaining",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        value_fn=_allowance("allowance_remaining_kwh"),
        attrs_fn=_allowance_attrs,
    ),
    AmberSensorDescription(
        key="export_charge",
        translation_key="export_charge",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY,
        suggested_display_precision=2,
        value_fn=_allowance("export_charge"),
        attrs_fn=_allowance_attrs,
    ),
)


def _today(role: str) -> Callable[[dict[str, Any]], Any]:
    def value(data: dict[str, Any]) -> Any:
        side = (data.get("today") or {}).get(role)
        return None if side is None else side["amount"]

    return value


def _today_attrs(role: str) -> Callable[[dict[str, Any]], dict[str, Any] | None]:
    def attrs(data: dict[str, Any]) -> dict[str, Any] | None:
        today = data.get("today") or {}
        side = today.get(role)
        if side is None:
            return None
        common = ("date", "expected_intervals", "last_interval_end", "meter", "offset_w")
        return {
            **{k: today[k] for k in common},
            "energy_kwh": side["kwh"],
            **{k: v for k, v in side.items() if k not in ("amount", "kwh")},
        }

    return attrs


TODAY_SENSORS = {
    ROLE_IMPORT: AmberSensorDescription(
        key="today_cost",
        translation_key="today_cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY,
        suggested_display_precision=2,
        value_fn=_today(ROLE_IMPORT),
        attrs_fn=_today_attrs(ROLE_IMPORT),
    ),
    ROLE_EXPORT: AmberSensorDescription(
        key="today_feed_in",
        translation_key="today_feed_in",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY,
        suggested_display_precision=2,
        value_fn=_today(ROLE_EXPORT),
        attrs_fn=_today_attrs(ROLE_EXPORT),
    ),
}
_TODAY_IDS = {"today_cost": "today_cost_estimated", "today_feed_in": "today_feed_in_estimated"}
"""Entity IDs for Today so far, after the site's prefix (the names are long)."""


def _remove(hass: HomeAssistant, site: str, descriptions: tuple[Any, ...]) -> None:
    """Remove sensors of a feature that is off, rather than leave them unavailable."""
    registry = er.async_get(hass)
    for description in descriptions:
        if entity_id := registry.async_get_entity_id("sensor", DOMAIN, f"{site}_{description.key}"):
            registry.async_remove(entity_id)


def _remove_solar_aware_off(
    hass: HomeAssistant, site: str, subentry_id: str, sensors: list[OwnSensor]
) -> None:
    """Remove an appliance's solar-aware cost sensor once that cost is turned off."""
    if any(s.role == ROLE_SOLAR_AWARE for s in sensors):
        return
    registry = er.async_get(hass)
    unique_id = f"{site}_yesterday_solar_aware_cost_{subentry_id}"
    if entity_id := registry.async_get_entity_id("sensor", DOMAIN, unique_id):
        registry.async_remove(entity_id)


def _yesterday_value(statistic_id: str) -> Callable[[dict[str, Any]], float | None]:
    def value(data: dict[str, Any]) -> float | None:
        totals = data.get("yesterday_totals")
        return None if totals is None else totals.get(statistic_id)

    return value


def _yesterday_attrs(data: dict[str, Any]) -> dict[str, Any]:
    day: date = data["yesterday"]
    return {"date": day.isoformat()}


def _reconciliation_value(key: str) -> Callable[[dict[str, Any]], float | None]:
    def value(data: dict[str, Any]) -> float | None:
        result = (data.get("reconciliation") or {}).get(key)
        return None if result is None else result["difference_kwh"]

    return value


def _reconciliation_attrs(key: str) -> Callable[[dict[str, Any]], dict[str, Any] | None]:
    def attrs(data: dict[str, Any]) -> dict[str, Any] | None:
        result = (data.get("reconciliation") or {}).get(key)
        if result is None:
            return None
        return {k: v for k, v in result.items() if k != "difference_kwh"}

    return attrs


def _mapping_value(group: str, key: str, field: str) -> Callable[[dict[str, Any]], float | None]:
    def value(data: dict[str, Any]) -> float | None:
        result = (data.get(group) or {}).get(key)
        return None if result is None else result[field]

    return value


def _mapping_attrs(
    group: str, key: str, field: str
) -> Callable[[dict[str, Any]], dict[str, Any] | None]:
    def attrs(data: dict[str, Any]) -> dict[str, Any] | None:
        result = (data.get(group) or {}).get(key)
        return None if result is None else {k: v for k, v in result.items() if k != field}

    return attrs


def _mapping_descriptions(sensor: OwnSensor) -> list[AmberSensorDescription]:
    """A mapping's sensors: reconciliation (grid meters and 2.1 mappings, section 11), or
    yesterday's energy and cost (appliances, section 21)."""
    if sensor.role == ROLE_SOLAR_AWARE:
        return [
            AmberSensorDescription(
                key=f"yesterday_solar_aware_cost_{sensor.subentry_id}",
                translation_key="appliance_yesterday_solar_aware_cost",
                device_class=SensorDeviceClass.MONETARY,
                native_unit_of_measurement=CURRENCY,
                suggested_display_precision=2,
                value_fn=_mapping_value("appliances", sensor.key, "cost"),
                attrs_fn=_mapping_attrs("appliances", sensor.key, "cost"),
            )
        ]
    if sensor.kind == SUBENTRY_APPLIANCE:
        feed_in = sensor.channel.type == CHANNEL_FEED_IN
        return [
            AmberSensorDescription(
                key=f"yesterday_energy_{sensor.subentry_id}",
                translation_key="appliance_yesterday_energy",
                device_class=SensorDeviceClass.ENERGY,
                native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                suggested_display_precision=3,
                value_fn=_mapping_value("appliances", sensor.key, "energy_kwh"),
                attrs_fn=_mapping_attrs("appliances", sensor.key, "energy_kwh"),
            ),
            AmberSensorDescription(
                key=f"yesterday_cost_{sensor.subentry_id}",
                translation_key="appliance_yesterday_compensation"
                if feed_in
                else "appliance_yesterday_cost",
                device_class=SensorDeviceClass.MONETARY,
                native_unit_of_measurement=CURRENCY,
                suggested_display_precision=2,
                value_fn=_mapping_value("appliances", sensor.key, "cost"),
                attrs_fn=_mapping_attrs("appliances", sensor.key, "cost"),
            ),
        ]
    if sensor.kind == SUBENTRY_GRID_METER:
        key, translation_key = (
            f"{sensor.role}_reconciliation_{sensor.subentry_id}",
            f"{sensor.role}_reconciliation",
        )
    else:
        key, translation_key = f"reconciliation_{sensor.subentry_id}", "reconciliation"
    return [
        AmberSensorDescription(
            key=key,
            translation_key=translation_key,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            suggested_display_precision=3,
            value_fn=_reconciliation_value(sensor.key),
            attrs_fn=_reconciliation_attrs(sensor.key),
        )
    ]


def _calibration_description(subentry_id: str) -> AmberSensorDescription:
    """A grid meter's calibration offset (W), with import and export (section 21)."""
    return AmberSensorDescription(
        key=f"calibration_offset_{subentry_id}",
        translation_key="calibration_offset",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=1,
        value_fn=_mapping_value("calibration", subentry_id, "offset_w"),
        attrs_fn=_mapping_attrs("calibration", subentry_id, "offset_w"),
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AmberConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the display sensors for each site (in its sub-entry, section 23)."""
    first = True
    for site in entry.runtime_data.sites.values():
        await _async_add_site_sensors(hass, entry, site, async_add_entities, pinned_ids=first)
        first = False


async def _async_add_site_sensors(
    hass: HomeAssistant,
    entry: AmberConfigEntry,
    site_runtime: SiteRuntime,
    async_add_entities: AddConfigEntryEntitiesCallback,
    *,
    pinned_ids: bool,
) -> None:
    """One site's sensors, and its mappings'. Only the entry's first site asks for the
    2.1.0 entity IDs; a further site's follow its device's name."""
    manager = site_runtime.manager
    channel_types = {c.identifier: c.type for c in manager.ctx.channels}
    descriptions: list[AmberSensorDescription] = list(STATUS_SENSORS)
    site = manager.ctx.site_id
    for enabled, group in (
        (manager.bill is not None, BILL_SENSORS),
        (manager.allowance is not None, ALLOWANCE_SENSORS),
    ):
        if enabled:
            descriptions.extend(group)
        else:
            _remove(hass, site, group)
    today_roles = set(manager.today.sides) if manager.today is not None else set()
    descriptions.extend(d for role, d in TODAY_SENSORS.items() if role in today_roles)
    if not undetected_prices(entry, site_runtime):  # else kept for a price found later
        _remove(hass, site, tuple(d for r, d in TODAY_SENSORS.items() if r not in today_roles))
    added_today = set(today_roles)
    for spec in manager.ctx.specs:
        if spec.channel is None:
            continue
        ident = spec.channel
        if spec.metric is Metric.ENERGY:
            key, device_class, unit = (
                f"yesterday_{ident.lower()}_energy",
                SensorDeviceClass.ENERGY,
                UnitOfEnergy.KILO_WATT_HOUR,
            )
            translation_key = "yesterday_energy"
        else:
            key, device_class, unit = (
                f"yesterday_{ident.lower()}_{spec.metric.value}",
                SensorDeviceClass.MONETARY,
                CURRENCY,
            )
            translation_key = (
                "yesterday_compensation"
                if channel_types[ident] == CHANNEL_FEED_IN
                else "yesterday_cost"
            )
        descriptions.append(
            AmberSensorDescription(
                key=key,
                translation_key=translation_key,
                translation_placeholders={"channel": ident},
                device_class=device_class,
                native_unit_of_measurement=unit,
                suggested_display_precision=3 if spec.metric is Metric.ENERGY else 2,
                value_fn=_yesterday_value(spec.statistic_id),
                attrs_fn=_yesterday_attrs,
            )
        )
    entity_ids = await async_site_entity_ids(hass, "sensor", descriptions) if pinned_ids else {}
    device = dr.async_get(hass).async_get(site_runtime.device_id)
    prefix = slugify(ENTITY_ID_PREFIX if pinned_ids or device is None else device.name)
    entity_ids.update({key: f"sensor.{prefix}_{name}" for key, name in _TODAY_IDS.items()})
    async_add_entities(
        (
            AmberSensor(
                manager.coordinator, site, description, entity_id=entity_ids.get(description.key)
            )
            for description in descriptions
        ),
        config_subentry_id=site_runtime.subentry_id,
    )

    @callback
    def _add_today_sensors() -> None:
        """Today so far's sensors for a live price found after setup (2.2.0-dev4)."""
        roles = set(manager.today.sides) if manager.today is not None else set()
        new = [TODAY_SENSORS[role] for role in sorted(roles - added_today)]
        added_today.update(roles)
        async_add_entities(
            [
                AmberSensor(manager.coordinator, site, d, entity_id=entity_ids.get(d.key))
                for d in new
            ],
            config_subentry_id=site_runtime.subentry_id,
        )

    site_runtime.add_today_sensors = _add_today_sensors
    by_subentry: dict[str, list[OwnSensor]] = defaultdict(list)
    for sensor in manager.own_sensors:
        by_subentry[sensor.subentry_id].append(sensor)
    for subentry_id, sensors in by_subentry.items():
        descriptions = [d for sensor in sensors for d in _mapping_descriptions(sensor)]
        if sensors[0].kind == SUBENTRY_APPLIANCE:
            _remove_solar_aware_off(hass, site, subentry_id, sensors)
        if {s.role for s in sensors} == {ROLE_IMPORT, ROLE_EXPORT}:
            descriptions.append(_calibration_description(subentry_id))
        # A converted mapping's entities ask for the entity IDs of the ones they replace.
        hints = entry.subentries[subentry_id].data.get(CONF_ENTITY_IDS) or {}
        device = mapping_device_info(site, sensors[0], site_runtime.device_id)
        async_add_entities(
            [
                AmberSensor(
                    manager.coordinator,
                    site,
                    description,
                    device,
                    entity_id=hints.get(description.translation_key),
                )
                for description in descriptions
            ],
            config_subentry_id=subentry_id,
        )


class AmberSensor(CoordinatorEntity[DataUpdateCoordinator[dict[str, Any]]], SensorEntity):
    """A display sensor backed by the manager snapshot."""

    entity_description: AmberSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[dict[str, Any]],
        site: str,
        description: AmberSensorDescription,
        device: DeviceInfo | None = None,
        entity_id: str | None = None,
    ) -> None:
        """Initialise the sensor (on the site's device unless ``device`` is given;
        ``entity_id`` is only a suggestion for a new one)."""
        super().__init__(coordinator)
        self.entity_description = description
        if entity_id is not None:
            self.entity_id = entity_id
        self._attr_unique_id = f"{site}_{description.key}"
        self._attr_device_info = device or site_link(site)

    @property
    def available(self) -> bool:
        """Available once the manager has published a snapshot."""
        return self.coordinator.data is not None

    @property
    def native_value(self) -> str | int | float | date | datetime | None:
        """The current value (an amount that shows as zero is 0.0, never "-A$0.00")."""
        value = self.entity_description.value_fn(self.coordinator.data)
        description = self.entity_description
        if (
            description.device_class == SensorDeviceClass.MONETARY
            and isinstance(value, float)
            and abs(value) < 0.5 * 10 ** -(description.suggested_display_precision or 0)
        ):
            return 0.0
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Extra context (last run details, or the date a 'yesterday' value is for)."""
        return self.entity_description.attrs_fn(self.coordinator.data)
