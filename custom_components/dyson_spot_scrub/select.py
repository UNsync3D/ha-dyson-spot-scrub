"""Select entity — cleaning mode for the Dyson Spot+Scrub AI.

Room selection is handled by the per-room switch entities (switch.py).
"""
from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    CONF_SERIAL,
    CONF_DEVICE_NAME,
    CONF_PRODUCT_TYPE,
    CLEANING_MODES,
    DEFAULT_MODE,
)
from .coordinator import DysonCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: DysonCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([DysonCleaningModeSelectEntity(coordinator, entry)])


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.data[CONF_SERIAL])},
        name=entry.data[CONF_DEVICE_NAME],
        manufacturer="Dyson",
        model=entry.data.get(CONF_PRODUCT_TYPE, "RB0S"),
        serial_number=entry.data[CONF_SERIAL],
    )


# ── Cleaning mode picker ──────────────────────────────────────────────────────

class DysonCleaningModeSelectEntity(SelectEntity):
    """Drop-down for selecting the cleaning mode (Vacuum, Vacuum and Mop, etc.)."""

    _attr_has_entity_name = True
    _attr_name = "Vacuum Mode"
    _attr_icon = "mdi:robot-vacuum"
    _attr_should_poll = False
    _attr_options = CLEANING_MODES

    def __init__(self, coordinator: DysonCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.data[CONF_SERIAL]}_cleaning_mode"
        self._attr_device_info = _device_info(entry)
        self._attr_current_option = coordinator.current_mode or DEFAULT_MODE

    async def async_added_to_hass(self) -> None:
        self._coordinator.async_add_listener(self)

    async def async_will_remove_from_hass(self) -> None:
        self._coordinator.async_remove_listener(self)

    @property
    def available(self) -> bool:
        """True only when the MQTT connection is live."""
        return self._coordinator.mqtt is not None and self._coordinator.mqtt.connected

    @property
    def current_option(self) -> str:
        return self._coordinator.current_mode or DEFAULT_MODE

    async def async_select_option(self, option: str) -> None:
        """Update the coordinator's mode; if cleaning now, switch immediately."""
        from .const import MODE_TO_INT
        from .dyson_mqtt import is_any_cleaning

        if option not in CLEANING_MODES:
            _LOGGER.warning("Unknown cleaning mode: %s", option)
            return

        self._coordinator.current_mode = option
        self.async_write_ha_state()

        # Also keep the vacuum entity's fan_speed in sync
        if self._coordinator.mqtt and is_any_cleaning(self._coordinator.mqtt.state):
            mode_int = MODE_TO_INT[option]
            await self.hass.async_add_executor_job(
                self._coordinator.mqtt.start_mode, mode_int
            )

    @callback
    def async_write_ha_state(self) -> None:
        super().async_write_ha_state()
