"""Calendar of the cleaning schedules kept on the robot (#67).

Each schedule that is on appears as a weekly event at its time, an hour long
(the robot doesn't say how long a clean takes), named after its rooms.
Read-only: change schedules with the switches, the actions or the MyDyson app.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import CONF_SERIAL, DOMAIN
from .coordinator import DysonCoordinator
from .entity_helpers import device_info
from .schedules import EVENT_MINUTES, occurrences


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator: DysonCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([DysonScheduleCalendar(coordinator, entry)])


class DysonScheduleCalendar(CalendarEntity):
    """The robot's schedules as weekly events."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "schedules"
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator: DysonCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.data[CONF_SERIAL]}_schedule_calendar"
        self._attr_device_info = device_info(entry)

    async def async_added_to_hass(self) -> None:
        self._coordinator.async_add_listener(self)

    async def async_will_remove_from_hass(self) -> None:
        self._coordinator.async_remove_listener(self)

    def _events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        events = []
        for order in self._coordinator.schedules.active_orders.values():
            info = self._coordinator.describe_schedule(order)
            rooms = [self._coordinator.room_label(name) for name in info["rooms"]]
            summary = ", ".join(rooms) or str(order["id"])
            for at in occurrences(order, start, end):
                events.append(CalendarEvent(
                    start=at, end=at + timedelta(minutes=EVENT_MINUTES), summary=summary,
                    description=f"Dyson schedule {order['id']}",
                    uid=f"{order['id']}-{at.date().isoformat()}"))
        return sorted(events, key=lambda event: event.start)

    @property
    def event(self) -> CalendarEvent | None:
        """The clean on now, or the next one."""
        now = dt_util.now()
        upcoming = self._events(now, now + timedelta(days=8))
        return upcoming[0] if upcoming else None

    async def async_get_events(self, hass: HomeAssistant, start_date: datetime,
                               end_date: datetime) -> list[CalendarEvent]:
        local = dt_util.get_default_time_zone()
        return self._events(start_date.astimezone(local), end_date.astimezone(local))
