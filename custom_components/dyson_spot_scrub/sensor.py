"""Sensor entities for the Dyson Spot+Scrub AI."""
from __future__ import annotations

import logging
import math

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, CONF_SERIAL
from .coordinator import DysonCoordinator
from .dyson_mqtt import battery_level
from .entity_helpers import device_info

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: DysonCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        DysonBatterySensor(coordinator, entry),
        *(DysonConsumableSensor(coordinator, entry, kind, key, name)
          for kind, key, name in CONSUMABLES),
        DysonCleaningSolutionSensor(coordinator, entry),
        DysonDryingCountdownSensor(coordinator, entry),
    ])


class DysonBatterySensor(SensorEntity):
    """Battery level sensor for the Dyson Spot+Scrub AI.

    Also the base class for the other sensors here: subclasses inherit the
    coordinator listener wiring and override unique_id/value/availability.
    """

    _attr_has_entity_name  = True
    _attr_name             = "Battery"
    _attr_device_class     = SensorDeviceClass.BATTERY
    _attr_state_class      = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_should_poll      = False

    def __init__(self, coordinator: DysonCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._attr_unique_id   = f"{entry.data[CONF_SERIAL]}_battery"
        self._attr_device_info = device_info(entry)

    # ── HA lifecycle ──────────────────────────────────────────────────────────

    async def async_added_to_hass(self) -> None:
        self._coordinator.async_add_listener(self)

    async def async_will_remove_from_hass(self) -> None:
        self._coordinator.async_remove_listener(self)

    # ── State ─────────────────────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        """True only while connected and the robot is reachable."""
        return self._coordinator.mqtt is not None and self._coordinator.mqtt.available

    @property
    def native_value(self) -> int | None:
        if self._coordinator.mqtt:
            return battery_level(self._coordinator.state)
        return None

    @callback
    def async_write_ha_state(self) -> None:
        super().async_write_ha_state()


# Stable keys are independent of entity names and user-assigned entity IDs.
CONSUMABLES = (
    ("brushBar", "brush_bar", "Brush bar usage"),
    ("mopRoller", "mop_roller", "Mop roller usage"),
    ("sideBrushes", "side_brushes", "Side brushes usage"),
    ("robotFilter", "robot_filter", "Robot filter usage"),
    ("dockFilter", "dock_filter", "Dock filter usage"),
)


def consumable_record(state: dict, kind: str) -> dict:
    """Return one unambiguous record; missing/malformed data is unknown."""
    records = state.get("consumables")
    if not isinstance(records, list):
        return {}
    matches = [item for item in records
               if isinstance(item, dict) and item.get("type") == kind]
    return matches[0] if len(matches) == 1 else {}


class DysonConsumableSensor(DysonBatterySensor):
    """Consumed lifespan in percent, supplied by existing MQTT push updates."""

    _attr_device_class = None
    _attr_icon = "mdi:progress-wrench"

    def __init__(self, coordinator, entry, kind: str, key: str, name: str) -> None:
        super().__init__(coordinator, entry)
        self._kind = kind
        self._attr_name = name
        self._attr_unique_id = f"{entry.data[CONF_SERIAL]}_consumable_{key}_usage"
        self._attr_extra_state_attributes = {
            "consumable_type": kind,
            "value_kind": "usage_percent",
        }

    @property
    def native_value(self) -> int | float | None:
        if not self.available:
            return None
        value = consumable_record(self._coordinator.state, self._kind).get("usage")
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not 0 <= value <= 100):
            return None
        return value


class DysonCleaningSolutionSensor(DysonBatterySensor):
    """Refill status; the robot does not report a liquid percentage."""

    _attr_name = "Cleaning solution"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_state_class = None
    _attr_native_unit_of_measurement = None
    _attr_options = ["ok", "refill_required"]
    _attr_translation_key = "cleaning_solution"
    _attr_icon = "mdi:bottle-tonic-outline"
    _attr_extra_state_attributes = {
        "consumable_type": "cleaningSolution",
        "value_kind": "refill_status",
    }

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.data[CONF_SERIAL]}_consumable_cleaning_solution"

    @property
    def native_value(self) -> str | None:
        if not self.available:
            return None
        value = consumable_record(self._coordinator.state, "cleaningSolution").get("needsRefill")
        if value is True:
            return "refill_required"
        if value is False:
            return "ok"
        return None


class DysonDryingCountdownSensor(DysonBatterySensor):
    """Seconds remaining in the dock's mop-roller drying cycle.

    The dock pushes work_time props (drying_remaining_seconds,
    drying_total_seconds, drying_updated_at) during DRYING_MOP, which
    _merge_jdm_props() in dyson_mqtt.py already stores on coordinator.state.
    This sensor is only available while dockState == 'DRYING_MOP'.
    """

    _attr_name             = "Mop drying time remaining"
    _attr_device_class     = SensorDeviceClass.DURATION
    _attr_state_class      = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_icon             = "mdi:fan-clock"

    def __init__(self, coordinator: DysonCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.data[CONF_SERIAL]}_drying_remaining"

    @property
    def available(self) -> bool:
        """True only while MQTT is live and the dock is actively drying."""
        mqtt = self._coordinator.mqtt
        if not (mqtt and mqtt.available):
            return False
        return self._coordinator.state.get("dockState") == "DRYING_MOP"

    @property
    def native_value(self) -> int | None:
        remaining = self._coordinator.state.get("drying_remaining_seconds")
        if isinstance(remaining, (int, float)) and math.isfinite(remaining) and remaining >= 0:
            return int(remaining)
        return None

    @property
    def extra_state_attributes(self) -> dict:
        state = self._coordinator.state
        attrs: dict = {}
        total = state.get("drying_total_seconds")
        if isinstance(total, (int, float)) and math.isfinite(total):
            attrs["total_seconds"] = int(total)
        updated_at = state.get("drying_updated_at")
        if updated_at is not None:
            attrs["updated_at"] = updated_at
        return attrs
