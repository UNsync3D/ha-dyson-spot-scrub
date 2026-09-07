"""Sensor entities for the Dyson Spot+Scrub AI."""
from __future__ import annotations

import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, CONF_SERIAL, CONF_DEVICE_NAME, CONF_PRODUCT_TYPE
from .coordinator import DysonCoordinator
from .dyson_mqtt import battery_level

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: DysonCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([DysonBatterySensor(coordinator, entry)])


class DysonBatterySensor(SensorEntity):
    """Battery level sensor for the Dyson Spot+Scrub AI."""

    _attr_has_entity_name  = True
    _attr_name             = "Battery"
    _attr_device_class     = SensorDeviceClass.BATTERY
    _attr_state_class      = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_should_poll      = False

    def __init__(self, coordinator: DysonCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        serial       = entry.data[CONF_SERIAL]
        device_name  = entry.data[CONF_DEVICE_NAME]
        product_type = entry.data.get(CONF_PRODUCT_TYPE, "RB0S")

        self._attr_unique_id   = f"{serial}_battery"
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

    # ── State ─────────────────────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        """True only when the MQTT connection is live."""
        return self._coordinator.mqtt is not None and self._coordinator.mqtt.connected

    @property
    def native_value(self) -> int | None:
        if self._coordinator.mqtt:
            return battery_level(self._coordinator.mqtt.state)
        return None

    @callback
    def async_write_ha_state(self) -> None:
        super().async_write_ha_state()
