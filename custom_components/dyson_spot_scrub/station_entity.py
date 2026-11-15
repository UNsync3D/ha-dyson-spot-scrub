"""Shared lifecycle and explicit-write handling for station controls."""
import time

from homeassistant.exceptions import HomeAssistantError

from .const import CONF_SERIAL
from .entity_helpers import device_info
from .station_settings import current_setting

# How long a sent value is shown after a change (#50). A status reply can
# still carry the old value for a moment, even after the robot has confirmed
# the new one, which made controls jump back before settling.
PENDING_SECONDS = 5.0


class StationEntity:
    """Mixin: incoming state updates only render; they never send commands."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    # Unique ID group: "station" for mop station settings, "setting" for the
    # robot's own settings (#50), so removing a no-mop robot's station
    # entities (#49) never touches them.
    _id_group = "station"

    def __init__(self, coordinator, entry, key, name):
        self._coordinator = coordinator
        self._setting_key = key
        self._attr_unique_id = f"{entry.data[CONF_SERIAL]}_{self._id_group}_{key}"
        self._attr_name = name
        self._attr_device_info = device_info(entry)
        self._pending = None        # (value, monotonic deadline) after a change
        self._pending_timer = None

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self._coordinator.async_add_listener(self)

    async def async_will_remove_from_hass(self):
        self._cancel_pending_timer()
        self._coordinator.async_remove_listener(self)
        await super().async_will_remove_from_hass()

    @property
    def available(self):
        client = self._coordinator.mqtt
        return bool(client and client.available and self.setting_value is not None)

    @property
    def setting_value(self):
        reported = current_setting(self._coordinator.state, self._setting_key)
        if self._pending is not None:
            value, deadline = self._pending
            # Held for the whole window, even after the robot confirms: an
            # older status reply can still arrive just after the confirmation
            # (FutuRazor's log, #50). Afterwards only the robot's value counts.
            if reported is not None and time.monotonic() < deadline:
                return value
            self._pending = None
        return reported

    def _cancel_pending_timer(self):
        if self._pending_timer is not None:
            self._pending_timer.cancel()
            self._pending_timer = None

    def _pending_expired(self):
        self._pending_timer = None
        if getattr(self, "hass", None) is not None:
            self.async_write_ha_state()         # show the robot's value again

    async def _async_set_setting(self, value):
        if not self.available:
            raise HomeAssistantError("Station setting is not available")
        accepted = await self.hass.async_add_executor_job(
            self._coordinator.mqtt.set_station_setting, self._setting_key, value)
        if not accepted:
            raise HomeAssistantError("Could not send station setting; check the device state")
        # Show the sent value until the robot confirms it, for at most
        # PENDING_SECONDS; after that only the robot's reported value counts.
        self._pending = (value, time.monotonic() + PENDING_SECONDS)
        self._cancel_pending_timer()
        loop = getattr(self.hass, "loop", None)
        if loop is not None:
            self._pending_timer = loop.call_later(PENDING_SECONDS + 0.1, self._pending_expired)
        if hasattr(self, "async_write_ha_state"):
            self.async_write_ha_state()
