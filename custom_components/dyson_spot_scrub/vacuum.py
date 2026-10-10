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
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
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
SERVICE_SAVE_ROOM_SETTINGS = "save_room_settings"
SERVICE_START_ROOMS_WITH_SETTINGS = "start_rooms_with_settings"
ATTR_ROOM            = "room"
ATTR_ROOMS           = "rooms"
ATTR_ENTITY_ID       = "entity_id"
ATTR_ROOM_IDS        = "room_ids"
ATTR_ROOM_SETTINGS   = "room_settings"

_CLEAN_ROOM_SCHEMA  = vol.Schema({vol.Required(ATTR_ROOM): cv.string})
_CLEAN_ROOMS_SCHEMA = vol.Schema({vol.Required(ATTR_ROOMS): vol.All(cv.ensure_list, [cv.string])})
_CARD_ENTITY_SCHEMA = vol.All(cv.ensure_list, [cv.entity_id])
_SAVE_ROOM_SETTINGS_SCHEMA = vol.Schema({
    vol.Required(ATTR_ENTITY_ID): _CARD_ENTITY_SCHEMA,
    vol.Required(ATTR_ROOM_SETTINGS): dict,
})
_START_ROOMS_WITH_SETTINGS_SCHEMA = vol.Schema({
    vol.Required(ATTR_ENTITY_ID): _CARD_ENTITY_SCHEMA,
    vol.Required(ATTR_ROOM_IDS): vol.All(cv.ensure_list, [cv.string]),
    vol.Optional(ATTR_ROOM_SETTINGS, default={}): dict,
})

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

    def _get_target_coordinator(call: ServiceCall) -> DysonCoordinator:
        """Resolve a card action to exactly one configured vacuum."""
        entity_ids = call.data[ATTR_ENTITY_ID]
        if len(entity_ids) != 1:
            raise HomeAssistantError("Select exactly one Dyson vacuum entity")
        registry_entry = er.async_get(hass).async_get(entity_ids[0])
        if (
            registry_entry is None
            or registry_entry.platform != DOMAIN
            or not registry_entry.config_entry_id
            or not registry_entry.entity_id.startswith("vacuum.")
        ):
            raise HomeAssistantError("The selected entity is not a Dyson vacuum")
        coordinator = hass.data.get(DOMAIN, {}).get(registry_entry.config_entry_id)
        if not isinstance(coordinator, DysonCoordinator) or not coordinator.mqtt:
            raise HomeAssistantError("The selected Dyson vacuum is unavailable")
        if not coordinator.mqtt.connected:
            raise HomeAssistantError("The selected Dyson vacuum is disconnected")
        return coordinator

    if not hass.services.has_service(DOMAIN, SERVICE_SAVE_ROOM_SETTINGS):

        async def _handle_save_room_settings(call: ServiceCall) -> None:
            coordinator = _get_target_coordinator(call)
            saved = await hass.async_add_executor_job(
                coordinator.mqtt.save_room_settings,
                call.data[ATTR_ROOM_SETTINGS],
            )
            if not saved:
                raise HomeAssistantError("Could not save the room settings")
            coordinator._async_update_rooms_and_notify()

        hass.services.async_register(
            DOMAIN,
            SERVICE_SAVE_ROOM_SETTINGS,
            _handle_save_room_settings,
            schema=_SAVE_ROOM_SETTINGS_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_START_ROOMS_WITH_SETTINGS):

        async def _handle_start_rooms_with_settings(call: ServiceCall) -> None:
            coordinator = _get_target_coordinator(call)
            started = await hass.async_add_executor_job(
                coordinator.mqtt.start_rooms_with_settings,
                call.data[ATTR_ROOM_IDS],
                call.data[ATTR_ROOM_SETTINGS],
            )
            if not started:
                raise HomeAssistantError("Could not start cleaning with the supplied room settings")
            coordinator._async_update_rooms_and_notify()

        hass.services.async_register(
            DOMAIN,
            SERVICE_START_ROOMS_WITH_SETTINGS,
            _handle_start_rooms_with_settings,
            schema=_START_ROOMS_WITH_SETTINGS_SCHEMA,
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
        """Stop cleaning; also cancels any in-flight sequential room task (P1 #2)."""
        await self._coordinator.async_stop()

    async def async_return_to_base(self, **kwargs: Any) -> None:
        """Return to base; also cancels any in-flight sequential room task (P1 #2)."""
        await self._coordinator.async_return_to_base()

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
