"""Vacuum entity for the Dyson Spot+Scrub AI."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.vacuum import (
    StateVacuumEntity,
    VacuumEntityFeature,
    VacuumActivity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    CONF_SERIAL,
    CONF_DEVICE_NAME,
    CONF_PRODUCT_TYPE,
    CLEANING_MODES,
    MODE_TO_INT,
    DEFAULT_MODE,
)
from .coordinator import DysonCoordinator
from .dyson_mqtt import (
    is_any_cleaning,
    is_docked,
    is_charging,
    has_fault,
    RUNNING_STATES,
)

SERVICE_CLEAN_ROOM   = "clean_room"
SERVICE_CLEAN_ROOMS  = "clean_rooms"
ATTR_ROOM            = "room"
ATTR_ROOMS           = "rooms"

_CLEAN_ROOM_SCHEMA  = vol.Schema({vol.Required(ATTR_ROOM): cv.string})
_CLEAN_ROOMS_SCHEMA = vol.Schema({vol.Required(ATTR_ROOMS): vol.All(cv.ensure_list, [cv.string])})

_LOGGER = logging.getLogger(__name__)

SUPPORTED_FEATURES = (
    VacuumEntityFeature.START
    | VacuumEntityFeature.STOP
    | VacuumEntityFeature.RETURN_HOME
    | VacuumEntityFeature.FAN_SPEED
    | VacuumEntityFeature.STATE
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: DysonCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([DysonVacuumEntity(coordinator, entry)])

    # Register the clean_room service once (it covers all robots in the domain).
    if not hass.services.has_service(DOMAIN, SERVICE_CLEAN_ROOM):

        async def _handle_clean_room(call: ServiceCall) -> None:
            room = call.data[ATTR_ROOM]
            for coord in hass.data.get(DOMAIN, {}).values():
                if isinstance(coord, DysonCoordinator) and coord.mqtt and coord.mqtt.connected:
                    await hass.async_add_executor_job(coord.mqtt.start_room, room)

        hass.services.async_register(
            DOMAIN,
            SERVICE_CLEAN_ROOM,
            _handle_clean_room,
            schema=_CLEAN_ROOM_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_CLEAN_ROOMS):

        async def _handle_clean_rooms(call: ServiceCall) -> None:
            rooms = call.data[ATTR_ROOMS]
            for coord in hass.data.get(DOMAIN, {}).values():
                if isinstance(coord, DysonCoordinator) and coord.mqtt and coord.mqtt.connected:
                    hass.async_create_task(coord.async_clean_rooms_sequential(rooms))

        hass.services.async_register(
            DOMAIN,
            SERVICE_CLEAN_ROOMS,
            _handle_clean_rooms,
            schema=_CLEAN_ROOMS_SCHEMA,
        )


class DysonVacuumEntity(StateVacuumEntity):
    """Represents the Dyson Spot+Scrub AI robot vacuum."""

    _attr_has_entity_name       = True
    _attr_name                  = None          # uses device name as entity name
    _attr_supported_features    = SUPPORTED_FEATURES
    _attr_fan_speed_list        = CLEANING_MODES
    _attr_should_poll           = False

    def __init__(self, coordinator: DysonCoordinator, entry: ConfigEntry) -> None:
        self._coordinator   = coordinator
        self._entry         = entry
        self._serial        = entry.data[CONF_SERIAL]
        self._device_name   = entry.data[CONF_DEVICE_NAME]
        self._product_type  = entry.data.get(CONF_PRODUCT_TYPE, "RB0S")

        self._attr_unique_id = f"{self._serial}_vacuum"
        self._attr_fan_speed = coordinator.current_mode

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._serial)},
            name=self._device_name,
            manufacturer="Dyson",
            model=self._product_type,
            serial_number=self._serial,
        )

    # ── HA lifecycle ──────────────────────────────────────────────────────────

    async def async_added_to_hass(self) -> None:
        self._coordinator.async_add_listener(self)

    async def async_will_remove_from_hass(self) -> None:
        self._coordinator.async_remove_listener(self)

    # ── State properties ──────────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        """True only when the MQTT connection is live."""
        return self._coordinator.mqtt is not None and self._coordinator.mqtt.connected

    @property
    def activity(self) -> VacuumActivity | None:
        state = self._mqtt_state
        if has_fault(state):
            return VacuumActivity.ERROR
        robot_state = state.get("state", "")
        if robot_state in RUNNING_STATES:
            return VacuumActivity.CLEANING
        if robot_state in {"RETURNING_TO_BASE", "MAPPING"}:
            return VacuumActivity.RETURNING
        if is_charging(state):
            return VacuumActivity.DOCKED
        if is_docked(state):
            return VacuumActivity.DOCKED
        return VacuumActivity.IDLE

    @property
    def fan_speed(self) -> str:
        return self._attr_fan_speed

    @property
    def _mqtt_state(self) -> dict:
        if self._coordinator.mqtt:
            return self._coordinator.mqtt.state
        return {}

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose room names so automations can reference them."""
        attrs: dict[str, Any] = {}
        if self._coordinator.mqtt:
            rooms = self._coordinator.mqtt.room_names
            if rooms:
                attrs["rooms"] = rooms
        return attrs

    # ── Commands ──────────────────────────────────────────────────────────────

    async def async_start(self) -> None:
        mode_int = MODE_TO_INT.get(self._attr_fan_speed, 0)
        await self.hass.async_add_executor_job(
            self._coordinator.mqtt.start_mode, mode_int
        )

    async def async_stop(self, **kwargs: Any) -> None:
        await self.hass.async_add_executor_job(self._coordinator.mqtt.stop)

    async def async_return_to_base(self, **kwargs: Any) -> None:
        await self.hass.async_add_executor_job(self._coordinator.mqtt.return_to_base)

    async def async_set_fan_speed(self, fan_speed: str, **kwargs: Any) -> None:
        """Select a cleaning mode. If already cleaning, starts the new mode immediately."""
        if fan_speed not in CLEANING_MODES:
            _LOGGER.warning("Unknown fan speed/mode: %s", fan_speed)
            return
        self._attr_fan_speed = fan_speed
        self._coordinator.current_mode = fan_speed
        self.async_write_ha_state()

        # If the robot is currently cleaning, switch to the new mode immediately
        if is_any_cleaning(self._mqtt_state):
            mode_int = MODE_TO_INT[fan_speed]
            await self.hass.async_add_executor_job(
                self._coordinator.mqtt.start_mode, mode_int
            )

    @callback
    def async_write_ha_state(self) -> None:
        """Called by coordinator on every MQTT state change."""
        super().async_write_ha_state()
