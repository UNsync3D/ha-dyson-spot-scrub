"""Button entities for the Dyson Spot+Scrub AI."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, CONF_SERIAL, CONF_DEVICE_NAME, CONF_PRODUCT_TYPE, MODE_TO_INT
from .coordinator import DysonCoordinator
from .dyson_mqtt import start_block_reason

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: DysonCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        DysonStartButton(coordinator, entry),
        DysonEmptyBinButton(coordinator, entry),
        DysonWashMopButton(coordinator, entry),
    ])


class DysonStartButton(ButtonEntity):
    """Press to start cleaning the selected room (or a full clean if no room chosen)."""

    _attr_has_entity_name = True
    _attr_name = "Vacuum Start"
    _attr_icon = "mdi:play-circle"
    _attr_should_poll = False  # push-updated via the coordinator listener

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

    async def async_added_to_hass(self) -> None:
        self._coordinator.async_add_listener(self)

    async def async_will_remove_from_hass(self) -> None:
        self._coordinator.async_remove_listener(self)

    @callback
    def async_write_ha_state(self) -> None:
        super().async_write_ha_state()

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

        reason = start_block_reason(self._coordinator.mqtt.state, self.available)
        if reason:
            raise HomeAssistantError(f"Cannot start a new clean: {reason.replace('_', ' ')}")
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


class DysonDockActionButton(ButtonEntity):
    """Base class for dock station-action buttons (empty bin, wash mop)."""

    _attr_has_entity_name = True
    _attr_should_poll     = False

    def __init__(self, coordinator: DysonCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        serial       = entry.data[CONF_SERIAL]
        device_name  = entry.data[CONF_DEVICE_NAME]
        product_type = entry.data.get(CONF_PRODUCT_TYPE, "RB0S")

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            name=device_name,
            manufacturer="Dyson",
            model=product_type,
            serial_number=serial,
        )

    @property
    def available(self) -> bool:
        """True only when MQTT is live and the dock is not already busy."""
        mqtt = self._coordinator.mqtt
        if not (mqtt and mqtt.connected):
            return False
        dock = self._coordinator.state.get("dockState")
        return dock not in {"COLLECTING_DUST", "WASHING_MOP", "DRYING_MOP"}

    async def async_added_to_hass(self) -> None:
        self._coordinator.async_add_listener(self)

    async def async_will_remove_from_hass(self) -> None:
        self._coordinator.async_remove_listener(self)

    @callback
    def async_write_ha_state(self) -> None:
        super().async_write_ha_state()


class DysonEmptyBinButton(DysonDockActionButton):
    """Trigger the dock to collect dust from the robot's bin."""

    _attr_name = "Empty Bin"
    _attr_icon = "mdi:trash-can-outline"

    def __init__(self, coordinator: DysonCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.data[CONF_SERIAL]}_empty_bin"

    async def async_press(self) -> None:
        if not (self._coordinator.mqtt and self._coordinator.mqtt.connected):
            _LOGGER.warning("Cannot empty bin — MQTT not connected")
            return
        success = await self.hass.async_add_executor_job(
            self._coordinator.mqtt.empty_bin
        )
        if not success:
            raise HomeAssistantError("Cannot empty bin: dock is already busy")


class DysonWashMopButton(DysonDockActionButton):
    """Trigger the dock to wash then air-dry the mop roller."""

    _attr_name = "Wash Mop"
    _attr_icon = "mdi:washing-machine"

    def __init__(self, coordinator: DysonCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.data[CONF_SERIAL]}_wash_mop"

    async def async_press(self) -> None:
        if not (self._coordinator.mqtt and self._coordinator.mqtt.connected):
            _LOGGER.warning("Cannot wash mop — MQTT not connected")
            return
        success = await self.hass.async_add_executor_job(
            self._coordinator.mqtt.wash_mop
        )
        if not success:
            raise HomeAssistantError("Cannot wash mop: dock is already busy")
