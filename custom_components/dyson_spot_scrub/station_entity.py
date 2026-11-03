"""Shared lifecycle and explicit-write handling for station controls."""
from homeassistant.exceptions import HomeAssistantError

from .const import CONF_SERIAL
from .entity_helpers import device_info
from .station_settings import current_setting


class StationEntity:
    """Mixin: incoming state updates only render; they never send commands."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator, entry, key, name):
        self._coordinator = coordinator
        self._setting_key = key
        self._attr_unique_id = f"{entry.data[CONF_SERIAL]}_station_{key}"
        self._attr_name = name
        self._attr_device_info = device_info(entry)

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self._coordinator.async_add_listener(self)

    async def async_will_remove_from_hass(self):
        self._coordinator.async_remove_listener(self)
        await super().async_will_remove_from_hass()

    @property
    def available(self):
        client = self._coordinator.mqtt
        return bool(client and client.available and self.setting_value is not None)

    @property
    def setting_value(self):
        return current_setting(self._coordinator.state, self._setting_key)

    async def _async_set_setting(self, value):
        if not self.available:
            raise HomeAssistantError("Station setting is not available")
        accepted = await self.hass.async_add_executor_job(
            self._coordinator.mqtt.set_station_setting, self._setting_key, value)
        if not accepted:
            raise HomeAssistantError("Could not send station setting; check the device state")
        # Do not optimistically change state: the robot must report the value.
