"""Switch entities — one per mapped room, auto-created when the robot reports its map.

Each switch represents a room.  Toggle the rooms you want cleaned, then press
the "Start Clean" button.  The robot will clean every enabled room in sequence.
"""
from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, CONF_SERIAL
from .coordinator import DysonCoordinator
from .station_entity import StationEntity
from .entity_helpers import device_info, room_slug
from .station_settings import SWITCHES

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: DysonCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([DysonStationSwitch(coordinator, entry, key, item[0])
                        for key, item in SWITCHES.items()])
    known_rooms: set[str] = set()

    @callback
    def _check_for_new_rooms() -> None:
        new_rooms = [r for r in coordinator.room_names if r not in known_rooms]
        if not new_rooms:
            return
        known_rooms.update(new_rooms)
        _LOGGER.info("[%s] Creating switch entities for rooms: %s", coordinator.serial, new_rooms)
        async_add_entities(
            [DysonRoomSwitchEntity(coordinator, entry, room) for room in new_rooms]
        )

    # Tiny listener shim — coordinator calls async_write_ha_state() on listeners;
    # we only need the notification hook so we can check for new room entities.
    class _RoomWatcher:
        def async_write_ha_state(self) -> None:
            _check_for_new_rooms()

    watcher = _RoomWatcher()
    coordinator.async_add_listener(watcher)

    # Check immediately in case room preferences are already cached
    _check_for_new_rooms()


class DysonRoomSwitchEntity(SwitchEntity):
    """Toggle switch for a single mapped room.

    When ON, the room is queued for the next "Start Clean" button press.
    Multiple rooms can be toggled at once; they are cleaned sequentially.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_icon = "mdi:map-marker-check"

    def __init__(
        self,
        coordinator: DysonCoordinator,
        entry: ConfigEntry,
        room_name: str,
    ) -> None:
        self._coordinator = coordinator
        self._room_name = room_name
        # Unique ID based on serial + room name (slugified)
        self._attr_unique_id = f"{entry.data[CONF_SERIAL]}_room_{room_slug(room_name)}"
        self._attr_name = f"Room {room_name}"
        self._attr_device_info = device_info(entry)

    async def async_added_to_hass(self) -> None:
        self._coordinator.async_add_listener(self)

    async def async_will_remove_from_hass(self) -> None:
        self._coordinator.async_remove_listener(self)

    @property
    def is_on(self) -> bool:
        return self._room_name in self._coordinator.enabled_rooms

    # Room selection lives only in memory (coordinator.enabled_rooms) and is
    # consumed by the "Vacuum Start" button; nothing is sent to the robot here.
    async def async_turn_on(self, **kwargs) -> None:
        self._coordinator.enabled_rooms.add(self._room_name)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self._coordinator.enabled_rooms.discard(self._room_name)
        self.async_write_ha_state()

    @callback
    def async_write_ha_state(self) -> None:
        super().async_write_ha_state()


class DysonStationSwitch(StationEntity, SwitchEntity):
    """A confirmed station preference, independent of room selection."""

    _attr_icon = "mdi:tune"

    @property
    def is_on(self):
        return self.setting_value

    async def async_turn_on(self, **kwargs):
        await self._async_set_setting(True)

    async def async_turn_off(self, **kwargs):
        await self._async_set_setting(False)
