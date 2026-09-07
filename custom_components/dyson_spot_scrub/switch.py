"""Switch entities — one per mapped room, auto-created when the robot reports its map.

Each switch represents a room.  Toggle the rooms you want cleaned, then press
the "Start Clean" button.  The robot will clean every enabled room in sequence.
"""
from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, CONF_SERIAL, CONF_DEVICE_NAME, CONF_PRODUCT_TYPE
from .coordinator import DysonCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: DysonCoordinator = hass.data[DOMAIN][entry.entry_id]
    known_rooms: set[str] = set()

    @callback
    def _check_for_new_rooms() -> None:
        # Prefer live MQTT room names; fall back to coordinator's persisted cache
        # so entities are created immediately on startup even if MQTT hasn't
        # connected yet (e.g. robot is drying or briefly offline).
        if coordinator.mqtt and coordinator.mqtt.room_names:
            rooms = coordinator.mqtt.room_names
        else:
            rooms = coordinator.cached_room_names

        new_rooms = [r for r in rooms if r not in known_rooms]
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
        serial = entry.data[CONF_SERIAL]

        # Unique ID based on serial + room name (slugified)
        slug = room_name.lower().replace(" ", "_")
        self._attr_unique_id = f"{serial}_room_{slug}"
        self._attr_name = f"Room {room_name}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            name=entry.data[CONF_DEVICE_NAME],
            manufacturer="Dyson",
            model=entry.data.get(CONF_PRODUCT_TYPE, "RB0S"),
            serial_number=serial,
        )

    async def async_added_to_hass(self) -> None:
        self._coordinator.async_add_listener(self)

    async def async_will_remove_from_hass(self) -> None:
        self._coordinator.async_remove_listener(self)

    @property
    def is_on(self) -> bool:
        return self._room_name in self._coordinator.enabled_rooms

    async def async_turn_on(self, **kwargs) -> None:
        self._coordinator.enabled_rooms.add(self._room_name)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self._coordinator.enabled_rooms.discard(self._room_name)
        self.async_write_ha_state()

    @callback
    def async_write_ha_state(self) -> None:
        super().async_write_ha_state()
