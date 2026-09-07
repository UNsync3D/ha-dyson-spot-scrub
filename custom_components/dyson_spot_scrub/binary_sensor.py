"""Binary sensor entities for the Dyson Spot+Scrub AI."""
from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, CONF_SERIAL, CONF_DEVICE_NAME, CONF_PRODUCT_TYPE
from .coordinator import DysonCoordinator
from .dyson_mqtt import is_docked, is_charging, has_fault

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: DysonCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            DysonDockSensor(coordinator, entry),
            DysonChargingSensor(coordinator, entry),
            DysonFaultSensor(coordinator, entry),
        ]
    )


class _DysonBinarySensorBase(BinarySensorEntity):
    """Shared base for all Dyson binary sensors."""

    _attr_has_entity_name = True
    _attr_should_poll     = False

    def __init__(
        self,
        coordinator: DysonCoordinator,
        entry: ConfigEntry,
        key: str,
    ) -> None:
        self._coordinator = coordinator
        serial            = entry.data[CONF_SERIAL]
        device_name       = entry.data[CONF_DEVICE_NAME]
        product_type      = entry.data.get(CONF_PRODUCT_TYPE, "RB0S")

        self._attr_unique_id   = f"{serial}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            name=device_name,
            manufacturer="Dyson",
            model=product_type,
            serial_number=serial,
        )

    # ── HA lifecycle ──────────────────────────────────────────────────────────

    async def async_added_to_hass(self) -> None:
        self._coordinator.async_add_listener(self)

    async def async_will_remove_from_hass(self) -> None:
        self._coordinator.async_remove_listener(self)

    # ── Availability ─────────────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        """True only when the MQTT connection is live."""
        return self._coordinator.mqtt is not None and self._coordinator.mqtt.connected

    # ── Helpers ───────────────────────────────────────────────────────────────

    @property
    def _mqtt_state(self) -> dict:
        if self._coordinator.mqtt:
            return self._coordinator.mqtt.state
        return {}

    @callback
    def async_write_ha_state(self) -> None:
        """Called by coordinator on every MQTT state change."""
        super().async_write_ha_state()


class DysonDockSensor(_DysonBinarySensorBase):
    """True when the robot is sitting in the dock (charging or fully charged)."""

    _attr_name         = "Docked"
    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY

    def __init__(self, coordinator: DysonCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "docked")

    @property
    def is_on(self) -> bool | None:
        state = self._mqtt_state
        return is_docked(state) or is_charging(state)


class DysonChargingSensor(_DysonBinarySensorBase):
    """True while the battery is actively charging."""

    _attr_name         = "Charging"
    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING

    def __init__(self, coordinator: DysonCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "charging")

    @property
    def is_on(self) -> bool | None:
        return is_charging(self._mqtt_state)


class DysonFaultSensor(_DysonBinarySensorBase):
    """True when the robot has reported a fault or error."""

    _attr_name         = "Fault"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: DysonCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "fault")

    @property
    def is_on(self) -> bool | None:
        return has_fault(self._mqtt_state)
