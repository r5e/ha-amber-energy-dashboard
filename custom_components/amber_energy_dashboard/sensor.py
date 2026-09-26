"""Display-only sensors (DESIGN section 6).

None of these has a state_class, so Home Assistant never compiles them into statistics.
The Energy dashboard uses the external statistics, not these sensors.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import EntityCategory, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from . import AmberConfigEntry
from .const import CHANNEL_FEED_IN, DOMAIN
from .manager import STATUSES
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
        value_fn=lambda d: d["marker"],
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


def _yesterday_value(statistic_id: str) -> Callable[[dict[str, Any]], float | None]:
    def value(data: dict[str, Any]) -> float | None:
        totals = data.get("yesterday_totals")
        return None if totals is None else totals.get(statistic_id)

    return value


def _yesterday_attrs(data: dict[str, Any]) -> dict[str, Any]:
    day: date = data["yesterday"]
    return {"date": day.isoformat()}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AmberConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the display sensors for one site."""
    manager = entry.runtime_data.manager
    channel_types = {c.identifier: c.type for c in manager.ctx.channels}
    descriptions: list[AmberSensorDescription] = list(STATUS_SENSORS)
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
    async_add_entities(
        AmberSensor(manager.coordinator, entry, description) for description in descriptions
    )


class AmberSensor(CoordinatorEntity[DataUpdateCoordinator[dict[str, Any]]], SensorEntity):
    """A display sensor backed by the manager snapshot."""

    entity_description: AmberSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[dict[str, Any]],
        entry: AmberConfigEntry,
        description: AmberSensorDescription,
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        site = entry.runtime_data.context.site_id
        self._attr_unique_id = f"{site}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, site)},
            name="Amber Energy Dashboard",
            manufacturer="Amber Electric (unofficial integration)",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def available(self) -> bool:
        """Available once the manager has published a snapshot."""
        return self.coordinator.data is not None

    @property
    def native_value(self) -> str | int | float | date | datetime | None:
        """The current value."""
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Extra context (last run details, or the date a 'yesterday' value is for)."""
        return self.entity_description.attrs_fn(self.coordinator.data)
