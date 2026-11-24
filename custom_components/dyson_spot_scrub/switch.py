"""Switch entities — one per mapped room, auto-created when the robot reports its map.

Each switch represents a room.  Toggle the rooms you want cleaned, then press
the "Start Clean" button.  The robot will clean every enabled room in sequence.
"""
from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, CONF_SERIAL
from .coordinator import DysonCoordinator
from .station_entity import StationEntity
from .entity_helpers import device_info, has_mop, is_nurovi_r2, room_slug
from .station_settings import DEVICE_SWITCHES, R2_MISSING_SWITCHES, SWITCHES

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: DysonCoordinator = hass.data[DOMAIN][entry.entry_id]
    skip = R2_MISSING_SWITCHES if is_nurovi_r2(entry) else set()
    if not has_mop(entry):          # every station switch is a mop setting (#49)
        skip = set(SWITCHES)
    async_add_entities([DysonStationSwitch(coordinator, entry, key, item[0])
                        for key, item in SWITCHES.items() if key not in skip])
    # Robot settings every model has (#50).
    async_add_entities([
        *(DysonDeviceSettingSwitch(coordinator, entry, key, item[0])
          for key, item in DEVICE_SWITCHES.items()),
        DysonDeviceSettingSwitch(coordinator, entry, "do_not_disturb", "Do not disturb"),
    ])
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

    # One switch per schedule Home Assistant knows (#67), added and removed
    # as schedules are learnt and deleted.
    schedule_switches: dict[int, DysonScheduleSwitch] = {}

    @callback
    def _sync_schedule_switches() -> None:
        book = getattr(coordinator, "schedules", None)
        if book is None:
            return
        new = [DysonScheduleSwitch(coordinator, entry, order_id)
               for order_id in book.orders if order_id not in schedule_switches]
        for switch in new:
            schedule_switches[switch.schedule_id] = switch
        if new:
            async_add_entities(new)
        for order_id in [i for i in schedule_switches if i not in book.orders]:
            switch = schedule_switches.pop(order_id)
            if switch.hass is None or switch.entity_id is None:
                continue
            from homeassistant.helpers import entity_registry as er
            registry = er.async_get(switch.hass)
            if registry.async_get(switch.entity_id):
                registry.async_remove(switch.entity_id)
            else:
                switch.hass.async_create_task(switch.async_remove(force_remove=True))

    class _ScheduleWatcher:
        def async_write_ha_state(self) -> None:
            _sync_schedule_switches()

    coordinator.async_add_listener(_ScheduleWatcher())
    _sync_schedule_switches()


def _room_label(coordinator, room_name: str) -> str:
    """Standard room types in the user's language; the English name everywhere else."""
    label = getattr(coordinator, "room_label", None)
    return label(room_name) if callable(label) else room_name


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
        # Translated name, e.g. "Room Kitchen" (#57).
        self._attr_translation_key = "room"
        self._attr_translation_placeholders = {"room": _room_label(coordinator, room_name)}
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


class DysonDeviceSettingSwitch(DysonStationSwitch):
    """Voice prompts, child lock and Do Not Disturb (#50)."""

    _id_group = "setting"
    _ICONS = {"voice_prompts": "mdi:account-voice", "child_lock": "mdi:lock",
              "do_not_disturb": "mdi:sleep"}

    def __init__(self, coordinator, entry, key, name):
        super().__init__(coordinator, entry, key, name)
        self._attr_icon = self._ICONS.get(key, "mdi:tune")


class DysonScheduleSwitch(SwitchEntity):
    """A cleaning schedule on the robot, on or off (#67), e.g. "Schedule 10:00 Tue, Thu"."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_icon = "mdi:calendar-clock"
    _attr_translation_key = "schedule"

    def __init__(self, coordinator: DysonCoordinator, entry: ConfigEntry, schedule_id: int) -> None:
        from homeassistant.const import EntityCategory

        self._coordinator = coordinator
        self.schedule_id = schedule_id
        self._attr_unique_id = f"{entry.data[CONF_SERIAL]}_schedule_{schedule_id}"
        self._attr_entity_category = EntityCategory.CONFIG
        self._attr_device_info = device_info(entry)
        self._attr_translation_placeholders = self._placeholders()

    def _order(self) -> dict | None:
        return self._coordinator.schedules.orders.get(self.schedule_id)

    def _placeholders(self) -> dict[str, str]:
        order = self._order()
        if order is None:
            return {"time": "", "days": ""}
        info = self._coordinator.describe_schedule(order)
        names = getattr(self._coordinator, "weekday_names", {}) or {}
        days = ", ".join(names.get(day, day.title()) for day in info["days"])
        return {"time": info["time"], "days": days}

    async def async_added_to_hass(self) -> None:
        self._coordinator.async_add_listener(self)

    async def async_will_remove_from_hass(self) -> None:
        self._coordinator.async_remove_listener(self)

    @property
    def available(self) -> bool:
        mqtt = self._coordinator.mqtt
        return self._order() is not None and bool(mqtt and mqtt.available)

    @property
    def is_on(self) -> bool | None:
        order = self._order()
        return None if order is None else order["enable"] == 1

    @property
    def extra_state_attributes(self) -> dict:
        order = self._order()
        if order is None:
            return {}
        info = self._coordinator.describe_schedule(order)
        return {key: info[key] for key in ("schedule_id", "days", "time", "room_ids", "rooms")}

    async def _async_set(self, enabled: bool) -> None:
        from .schedules import ScheduleError
        try:
            await self._coordinator.async_set_schedule_enabled(self.schedule_id, enabled)
        except ScheduleError as exc:
            raise HomeAssistantError(str(exc)) from exc

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_set(False)

    @callback
    def async_write_ha_state(self) -> None:
        if self.hass is None or self._order() is None:
            return
        placeholders = self._placeholders()
        if placeholders != self._attr_translation_placeholders:
            self._attr_translation_placeholders = placeholders
        super().async_write_ha_state()
