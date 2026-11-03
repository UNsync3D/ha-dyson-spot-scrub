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
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    CONF_SERIAL,
    CLEANING_MODES,
    MODE_TO_INT,
    DEFAULT_MODE,
    FAULT_CODE_MESSAGES,
)
from .coordinator import DysonCoordinator
from .entity_helpers import device_info
from .dyson_mqtt import (
    is_any_cleaning,
    is_docked,
    is_charging,
    is_paused,
    has_fault,
    active_fault_code,
    dock_needs_maintenance,
    RUNNING_STATES,
    start_block_reason,
)

SERVICE_CLEAN_ROOM   = "clean_room"
SERVICE_CLEAN_ROOMS  = "clean_rooms"
SERVICE_CANCEL_DRYING = "cancel_drying"
SERVICE_SAVE_ROOM_SETTINGS = "save_room_settings"
SERVICE_START_ROOMS_WITH_SETTINGS = "start_rooms_with_settings"
ATTR_ROOM            = "room"
ATTR_ROOMS           = "rooms"
ATTR_ENTITY_ID       = "entity_id"
ATTR_ROOM_IDS        = "room_ids"
ATTR_ROOM_SETTINGS   = "room_settings"

_CARD_ENTITY_SCHEMA = vol.All(cv.ensure_list, [cv.entity_id])
# entity_id is optional on these two for existing automations: with one robot
# configured it may be omitted; with several it is required.
_CLEAN_ROOM_SCHEMA  = vol.Schema({
    vol.Optional(ATTR_ENTITY_ID): _CARD_ENTITY_SCHEMA,
    vol.Required(ATTR_ROOM): cv.string,
})
_CLEAN_ROOMS_SCHEMA = vol.Schema({
    vol.Optional(ATTR_ENTITY_ID): _CARD_ENTITY_SCHEMA,
    vol.Required(ATTR_ROOMS): vol.All(cv.ensure_list, [cv.string]),
})
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


def _require_start_ready(coordinator: DysonCoordinator) -> None:
    """Reject new jobs before changing preferences or scheduling room work."""
    mqtt = coordinator.mqtt
    reason = start_block_reason(mqtt.state if mqtt else {}, bool(mqtt and mqtt.connected))
    if reason:
        raise HomeAssistantError(f"Cannot start a new clean: {reason.replace('_', ' ')}")

SUPPORTED_FEATURES = (
    VacuumEntityFeature.START
    | VacuumEntityFeature.STOP
    | VacuumEntityFeature.PAUSE
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

    # Domain services are registered once, by the first entry to load, and
    # resolve their target robot at call time. Every service acts on exactly
    # one robot. clean_room / clean_rooms used to act on EVERY connected robot;
    # they now take an optional entity_id, required when several are set up.
    if not hass.services.has_service(DOMAIN, SERVICE_CLEAN_ROOM):

        async def _handle_clean_room(call: ServiceCall) -> None:
            coordinator = _get_room_service_coordinator(call)
            _require_start_ready(coordinator)
            room = call.data[ATTR_ROOM]
            started = await hass.async_add_executor_job(coordinator.mqtt.start_room, room)
            if not started:
                raise HomeAssistantError(f"Could not start cleaning {room!r}")

        hass.services.async_register(
            DOMAIN,
            SERVICE_CLEAN_ROOM,
            _handle_clean_room,
            schema=_CLEAN_ROOM_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_CLEAN_ROOMS):

        async def _handle_clean_rooms(call: ServiceCall) -> None:
            coordinator = _get_room_service_coordinator(call)
            _require_start_ready(coordinator)
            hass.async_create_task(
                coordinator.async_clean_rooms_sequential(call.data[ATTR_ROOMS]))

        hass.services.async_register(
            DOMAIN,
            SERVICE_CLEAN_ROOMS,
            _handle_clean_rooms,
            schema=_CLEAN_ROOMS_SCHEMA,
        )

    def _get_room_service_coordinator(call: ServiceCall) -> DysonCoordinator:
        """The robot for clean_room / clean_rooms: the named one, or the only one."""
        if call.data.get(ATTR_ENTITY_ID):
            return _get_target_coordinator(call)
        robots = [c for c in hass.data.get(DOMAIN, {}).values()
                  if isinstance(c, DysonCoordinator)]
        if len(robots) != 1:
            raise HomeAssistantError(
                "Several Dyson robots are set up: choose one with entity_id"
                if robots else "No Dyson robot is set up")
        coordinator = robots[0]
        if not coordinator.mqtt or not coordinator.mqtt.connected:
            raise HomeAssistantError("The Dyson vacuum is disconnected")
        return coordinator

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

    if not hass.services.has_service(DOMAIN, SERVICE_CANCEL_DRYING):

        async def _handle_cancel_drying(call: ServiceCall) -> None:
            coordinator = _get_target_coordinator(call)
            if coordinator.state.get("dockState") != "DRYING_MOP":
                raise HomeAssistantError("The selected Dyson vacuum is not drying its mop roller")
            sent = await hass.async_add_executor_job(coordinator.mqtt.cancel_drying)
            if not sent:
                raise HomeAssistantError("Could not send the drying cancellation command")
            # Do not fake IDLE or zero remaining time: wait for robot telemetry.

        hass.services.async_register(
            DOMAIN,
            SERVICE_CANCEL_DRYING,
            _handle_cancel_drying,
            schema=vol.Schema({vol.Required(ATTR_ENTITY_ID): _CARD_ENTITY_SCHEMA}),
        )

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
            _require_start_ready(coordinator)
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
        self._attr_unique_id = f"{self._serial}_vacuum"

        self._attr_device_info = device_info(entry)

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
        if robot_state == "FULL_CLEAN_PAUSED":
            return VacuumActivity.PAUSED
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
        return self._coordinator.current_mode or DEFAULT_MODE

    @property
    def _mqtt_state(self) -> dict:
        return self._coordinator.state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose room names and fault diagnostics for automations."""
        reason = start_block_reason(self._mqtt_state, self.available)
        attrs: dict[str, Any] = {
            "ready_to_start": reason is None,
            "start_block_reason": reason,
        }
        if self._coordinator.mqtt:
            state = self._coordinator.state

            # Room list
            rooms = self._coordinator.mqtt.room_names
            if rooms:
                attrs["rooms"] = rooms

            # Actionable fault — human-readable string + raw code
            code = active_fault_code(state)
            if code is not None:
                attrs["fault_code"] = code
                attrs["fault"] = FAULT_CODE_MESSAGES.get(code, f"Fault {code}")

            # Dock maintenance flag (code 2103 — informational, not ERROR)
            if dock_needs_maintenance(state):
                attrs["dock_maintenance"] = True

            # Dock cycle state (IDLE / WASHING_MOP / DRYING_MOP / COLLECTING_DUST)
            dock_state = state.get("dockState")
            if dock_state:
                attrs["dock_state"] = dock_state
            if dock_state == "DRYING_MOP" and self.available:
                for key in ("drying_total_seconds", "drying_remaining_seconds", "drying_updated_at"):
                    if key in state:
                        attrs[key] = state[key]

            # Active cleaning type (VACUUMING / MOPPING / VACUUMING_AND_MOPPING)
            full_clean_action = state.get("fullCleanAction")
            if full_clean_action and full_clean_action != "NONE":
                attrs["full_clean_action"] = full_clean_action

        return attrs

    # ── Commands ──────────────────────────────────────────────────────────────

    async def async_start(self) -> None:
        """Start or resume cleaning.

        If the robot is currently paused, sends only the JDM resume command
        (service.set_room_clean ctrl_value:1) — the Dyson app does not send
        a general START on resume, which would start a new job from scratch.
        If the robot is not paused, uses the existing full start flow.
        """
        if is_paused(self._mqtt_state):
            await self.hass.async_add_executor_job(
                self._coordinator.mqtt.resume
            )
        else:
            _require_start_ready(self._coordinator)
            mode_int = MODE_TO_INT.get(self._coordinator.current_mode, 0)
            await self.hass.async_add_executor_job(
                self._coordinator.mqtt.start_mode, mode_int
            )

    async def async_pause(self) -> None:
        """Pause an active clean."""
        await self.hass.async_add_executor_job(self._coordinator.mqtt.pause)

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
        if is_any_cleaning(self._mqtt_state):
            # The existing mode-switch path starts a new job, not a resume.
            _require_start_ready(self._coordinator)
        self._coordinator.current_mode = fan_speed
        self._coordinator._async_notify_listeners()

        # If the robot is currently cleaning, switch to the new mode immediately.
        # NOTE: start_block_reason() reports "cleaning_job_active" for every
        # running state, so _require_start_ready above rejects this path today.
        # Behaviour is intentionally unchanged: whether the robot supports a
        # mid-clean mode change (and with which command) needs investigating
        # on hardware before this guard is relaxed or replaced.
        if is_any_cleaning(self._mqtt_state):
            mode_int = MODE_TO_INT[fan_speed]
            await self.hass.async_add_executor_job(
                self._coordinator.mqtt.start_mode, mode_int
            )

    @callback
    def async_write_ha_state(self) -> None:
        """Called by coordinator on every MQTT state change."""
        super().async_write_ha_state()
