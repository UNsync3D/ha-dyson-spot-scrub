"""Select platform for Dyson Spot+Scrub.

Provides:
  • One global cleaning-mode select (applies when cleaning all rooms at once)
  • Four per-room selects, created dynamically when the robot reports its map:
      - Room <name> Cleaning Mode
      - Room <name> Cleaning Strategy
      - Room <name> Water Level
      - Room <name> Mop Repetitions

Per-room array-index mapping (confirmed from FutuRazor MQTT captures, Oct 2026):
  [3]  cleaningMode      0=Vacuum  1=Vacuum+Mop  2=Mop  3=Vacuum then Mop
  [4]  cleaningStrategy  0=Auto  1=Boost  2=Quiet  3=Quick
  [5]  waterLevel        99=Very Low  0=Low  1=Medium  2=High
  [6]  mopRepetitions    0=One pass  1=Two passes
"""
from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    CONF_SERIAL,
    CLEANING_MODES,
    DEFAULT_MODE,
    MODE_TO_INT,
)
from .coordinator import DysonCoordinator
from .entity_helpers import device_info, room_slug
from .station_entity import StationEntity
from .station_settings import SELECTS
from .dyson_mqtt import _room_display_name, is_any_cleaning

_LOGGER = logging.getLogger(__name__)


# ── Per-room option maps ──────────────────────────────────────────────────────

_CLEANING_MODE_OPTIONS: dict[int, str] = {
    0: "Vacuum",
    1: "Vacuum and Mop",
    2: "Mop",
    3: "Vacuum then Mop",
}
_CLEANING_STRATEGY_OPTIONS: dict[int, str] = {
    0: "Auto",
    1: "Boost",
    2: "Quiet",
    3: "Quick",
}
# [5] uses a non-standard encoding — 99 means "Very Low" (not 3)
_WATER_LEVEL_OPTIONS: dict[int, str] = {
    99: "Very Low",
    0:  "Low",
    1:  "Medium",
    2:  "High",
}
_MOP_REPETITIONS_OPTIONS: dict[int, str] = {
    0: "One Pass",
    1: "Two Passes",
}

# Inverse maps (display label → raw int)
_INV_CLEANING_MODE     = {v: k for k, v in _CLEANING_MODE_OPTIONS.items()}
_INV_CLEANING_STRATEGY = {v: k for k, v in _CLEANING_STRATEGY_OPTIONS.items()}
_INV_WATER_LEVEL       = {v: k for k, v in _WATER_LEVEL_OPTIONS.items()}
_INV_MOP_REPETITIONS   = {v: k for k, v in _MOP_REPETITIONS_OPTIONS.items()}

# Descriptor tuples: (select_type, label, icon, array_index, options_map, inverse_map)
_ROOM_SELECT_DESCRIPTORS = [
    (
        "cleaning_mode", "Cleaning Mode", "mdi:robot-vacuum",
        3, _CLEANING_MODE_OPTIONS, _INV_CLEANING_MODE,
    ),
    (
        "cleaning_strategy", "Cleaning Strategy", "mdi:tune",
        4, _CLEANING_STRATEGY_OPTIONS, _INV_CLEANING_STRATEGY,
    ),
    (
        "water_level", "Water Level", "mdi:water",
        5, _WATER_LEVEL_OPTIONS, _INV_WATER_LEVEL,
    ),
    (
        "mop_repetitions", "Mop Repetitions", "mdi:repeat",
        6, _MOP_REPETITIONS_OPTIONS, _INV_MOP_REPETITIONS,
    ),
]


# ── Setup entry ───────────────────────────────────────────────────────────────

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: DysonCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([DysonStationSelect(coordinator, entry, key, *item)
                        for key, item in SELECTS.items()])
    known_rooms: set[str] = set()

    # Global cleaning-mode select — created immediately, no room data needed
    async_add_entities([DysonCleaningModeSelectEntity(coordinator, entry)])

    @callback
    def _check_for_new_rooms() -> None:
        """Create per-room select entities for any rooms not yet registered."""
        new_rooms = [r for r in coordinator.room_names if r not in known_rooms]
        if not new_rooms:
            return
        known_rooms.update(new_rooms)
        _LOGGER.info(
            "[%s] Creating per-room select entities for: %s",
            coordinator.serial, new_rooms,
        )
        entities: list[SelectEntity] = []
        for room_name in new_rooms:
            for select_type, label, icon, array_index, options_map, inverse_map in _ROOM_SELECT_DESCRIPTORS:
                entities.append(
                    DysonRoomSelectEntity(
                        coordinator=coordinator,
                        entry=entry,
                        room_name=room_name,
                        select_type=select_type,
                        label=label,
                        icon=icon,
                        array_index=array_index,
                        options_map=options_map,
                        inverse_map=inverse_map,
                    )
                )
        async_add_entities(entities)

    class _RoomWatcher:
        """Thin listener shim — receives coordinator notifications and checks for new rooms."""
        def async_write_ha_state(self) -> None:
            _check_for_new_rooms()

    coordinator.async_add_listener(_RoomWatcher())
    _check_for_new_rooms()


# ── Global cleaning-mode select ───────────────────────────────────────────────

class DysonCleaningModeSelectEntity(SelectEntity):
    """Drop-down for selecting the cleaning mode applied when starting all rooms."""

    _attr_has_entity_name = True
    _attr_name = "Vacuum Mode"
    _attr_icon = "mdi:robot-vacuum"
    _attr_should_poll = False
    _attr_options = CLEANING_MODES

    def __init__(self, coordinator: DysonCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.data[CONF_SERIAL]}_cleaning_mode"
        self._attr_device_info = device_info(entry)
        self._attr_current_option = coordinator.current_mode or DEFAULT_MODE

    async def async_added_to_hass(self) -> None:
        self._coordinator.async_add_listener(self)

    async def async_will_remove_from_hass(self) -> None:
        self._coordinator.async_remove_listener(self)

    @property
    def available(self) -> bool:
        return self._coordinator.mqtt is not None and self._coordinator.mqtt.connected

    @property
    def current_option(self) -> str:
        return self._coordinator.current_mode or DEFAULT_MODE

    async def async_select_option(self, option: str) -> None:
        """Update the coordinator's mode; if cleaning now, switch the robot immediately."""
        if option not in CLEANING_MODES:
            _LOGGER.warning("Unknown cleaning mode: %s", option)
            return

        self._coordinator.current_mode = option
        self._coordinator._async_notify_listeners()

        # Mirrors DysonVacuumEntity.async_set_fan_speed. start_mode() refuses
        # to start while a job is active, so this branch is currently a no-op
        # (logs "start blocked"); the new mode applies to the next clean.
        # Left as-is pending hardware investigation of mid-clean mode changes.
        if self._coordinator.mqtt and is_any_cleaning(self._coordinator.state):
            mode_int = MODE_TO_INT[option]
            await self.hass.async_add_executor_job(
                self._coordinator.mqtt.start_mode, mode_int
            )

    @callback
    def async_write_ha_state(self) -> None:
        super().async_write_ha_state()


# ── Per-room select entity ────────────────────────────────────────────────────

class DysonRoomSelectEntity(SelectEntity):
    """Select entity for a single room preference (mode, strategy, water, passes).

    Reads the current value directly from the robot's cached preference array
    and publishes the full updated array back via service.set_preference whenever
    the user changes the option — without starting a clean cycle.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        *,
        coordinator: DysonCoordinator,
        entry: ConfigEntry,
        room_name: str,
        select_type: str,
        label: str,
        icon: str,
        array_index: int,
        options_map: dict[int, str],
        inverse_map: dict[str, int],
    ) -> None:
        self._coordinator  = coordinator
        self._room_name    = room_name
        self._select_type  = select_type
        self._array_index  = array_index
        self._options_map  = options_map
        self._inverse_map  = inverse_map

        self._attr_unique_id = (
            f"{entry.data[CONF_SERIAL]}_room_{room_slug(room_name)}_{select_type}"
        )
        self._attr_name      = f"Room {room_name} {label}"
        self._attr_icon      = icon
        self._attr_options   = list(options_map.values())
        self._attr_device_info = device_info(entry)

    # ── HA lifecycle ──────────────────────────────────────────────────────────

    async def async_added_to_hass(self) -> None:
        self._coordinator.async_add_listener(self)

    async def async_will_remove_from_hass(self) -> None:
        self._coordinator.async_remove_listener(self)

    # ── Entity state ──────────────────────────────────────────────────────────

    def _find_room_prefs(self) -> list | None:
        """Return the preference array for this room, or None if not yet cached."""
        if not self._coordinator.mqtt:
            return None
        for room in self._coordinator.mqtt.room_preferences:
            if _room_display_name(room[1]).casefold() == self._room_name.casefold():
                return room
        return None

    @property
    def available(self) -> bool:
        return (
            self._coordinator.mqtt is not None
            and self._coordinator.mqtt.connected
            and self._find_room_prefs() is not None
        )

    @property
    def current_option(self) -> str | None:
        room = self._find_room_prefs()
        if room is None or len(room) <= self._array_index:
            return None
        return self._options_map.get(room[self._array_index])

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        """Expose the stable map room ID so cards survive display-name changes."""
        room = self._find_room_prefs()
        if room is None or not room:
            return None
        return {"room_id": str(room[0])}

    # ── User interaction ──────────────────────────────────────────────────────

    async def async_select_option(self, option: str) -> None:
        raw = self._inverse_map.get(option)
        if raw is None:
            _LOGGER.warning(
                "[%s] Unknown %s option: %r",
                self._coordinator.serial, self._select_type, option,
            )
            return

        room = self._find_room_prefs()
        if room is None:
            _LOGGER.warning(
                "[%s] Cannot set %s: room %r not in preference cache",
                self._coordinator.serial, self._select_type, self._room_name,
            )
            return

        mqtt = self._coordinator.mqtt
        if mqtt is None:
            raise HomeAssistantError("The Dyson vacuum is unavailable")

        # Send only this field's change. edit_room_setting() reads the latest
        # base (including any not-yet-confirmed edit), applies the change and
        # publishes as one serialised step, so overlapping edits don't undo
        # each other. The cache this entity displays is only updated by the
        # robot's own preference reply, so until it arrives the dropdown keeps
        # showing the old value, and a failed send needs no rollback.
        try:
            saved = await self.hass.async_add_executor_job(
                mqtt.edit_room_setting, room[0], self._array_index, raw)
        except Exception as exc:  # noqa: BLE001 — surface any send failure to the user
            _LOGGER.warning("[%s] Saving %s for %s raised: %s",
                            self._coordinator.serial, self._select_type, self._room_name, exc)
            saved = False
        if not saved:
            self.async_write_ha_state()
            raise HomeAssistantError(f"Could not save {self._select_type} for {self._room_name}")

        # Accepted by the MQTT client only. The robot's preference reply will
        # update current_option via the coordinator when it arrives.
        self.async_write_ha_state()

    @callback
    def async_write_ha_state(self) -> None:
        super().async_write_ha_state()


class DysonStationSelect(StationEntity, SelectEntity):
    """Select among station settings observed in the MyDyson app."""

    _attr_icon = "mdi:tune"

    def __init__(self, coordinator, entry, key, name, options):
        super().__init__(coordinator, entry, key, name)
        self._attr_options = list(options)

    @property
    def current_option(self):
        return self.setting_value

    async def async_select_option(self, option):
        if option not in self._attr_options:
            raise HomeAssistantError("Unsupported station setting")
        await self._async_set_setting(option)
