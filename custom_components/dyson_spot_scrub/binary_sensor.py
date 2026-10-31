"""Binary sensor entities for the Dyson Spot+Scrub AI."""
from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, CONF_SERIAL
from .coordinator import DysonCoordinator
from .dyson_mqtt import is_docked, is_charging, has_fault
from .entity_helpers import device_info

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
        self._attr_unique_id   = f"{entry.data[CONF_SERIAL]}_{key}"
        self._attr_device_info = device_info(entry)

    # ── HA lifecycle ──────────────────────────────────────────────────────────

    async def async_added_to_hass(self) -> None:
        self._coordinator.async_add_listener(self)

    async def async_will_remove_from_hass(self) -> None:
        self._coordinator.async_remove_listener(self)

    # ── Availability ─────────────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        """True only while connected and the robot is reachable."""
        return self._coordinator.mqtt is not None and self._coordinator.mqtt.available

    # ── Helpers ───────────────────────────────────────────────────────────────

    @property
    def _mqtt_state(self) -> dict:
        return self._coordinator.state

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
        # is_charging() already implies is_docked(), so one check suffices.
        return is_docked(self._mqtt_state)


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
