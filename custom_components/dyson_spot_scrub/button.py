"""Button entities for the Dyson Spot+Scrub AI."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, CONF_SERIAL, CONF_DEVICE_NAME, CONF_PRODUCT_TYPE, MODE_TO_INT
from .coordinator import DysonCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: DysonCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([DysonStartButton(coordinator, entry)])


class DysonStartButton(ButtonEntity):
    """Press to start cleaning the selected room (or a full clean if no room chosen)."""

    _attr_has_entity_name = True
    _attr_name = "Vacuum Start"
    _attr_icon = "mdi:play-circle"

    def __init__(self, coordinator: DysonCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.data[CONF_SERIAL]}_start_button"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_SERIAL])},
            name=entry.data[CONF_DEVICE_NAME],
            manufacturer="Dyson",
            model=entry.data.get(CONF_PRODUCT_TYPE, "RB0S"),
            serial_number=entry.data[CONF_SERIAL],
        )

    @property
    def available(self) -> bool:
        """True only when the MQTT connection is live."""
        return self._coordinator.mqtt is not None and self._coordinator.mqtt.connected

    async def async_press(self) -> None:
        """Start cleaning.

        If any room switches are ON, clean those rooms sequentially in map order.
        Otherwise start a full clean of the whole house in the current mode.
        """
        if not (self._coordinator.mqtt and self._coordinator.mqtt.connected):
            _LOGGER.warning("Cannot start — MQTT not connected")
            return

        enabled = self._coordinator.enabled_rooms
        if enabled:
            # Sort by map order so cleaning feels predictable
            map_order = self._coordinator.mqtt.room_names
            ordered = [r for r in map_order if r in enabled]
            ordered += [r for r in enabled if r not in ordered]
            _LOGGER.info("Start button pressed — sequential clean: %s", ordered)
            self.hass.async_create_task(
                self._coordinator.async_clean_rooms_sequential(ordered)
            )
            return

        # No rooms toggled — full clean in current mode
        from .const import MODE_TO_INT
        mode_int = MODE_TO_INT.get(self._coordinator.current_mode, 0)
        _LOGGER.info("Start button pressed — full clean in mode '%s'", self._coordinator.current_mode)
        await self.hass.async_add_executor_job(
            self._coordinator.mqtt.start_mode, mode_int
        )
