"""Number platform: the robot's voice and sound volume (#50)."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .station_entity import StationEntity
from .station_settings import VOLUME_RANGE, VOLUME_STEP


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([DysonVolumeNumber(coordinator, entry, "volume", "Volume")])


class DysonVolumeNumber(StationEntity, NumberEntity):
    """Volume of voice prompts and sounds, as the MyDyson app sets it."""

    _id_group = "setting"
    _attr_icon = "mdi:volume-high"
    _attr_native_min_value = VOLUME_RANGE[0]
    _attr_native_max_value = VOLUME_RANGE[1]
    _attr_native_step = VOLUME_STEP
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.SLIDER

    @property
    def native_value(self):
        return self.setting_value

    async def async_set_native_value(self, value: float) -> None:
        await self._async_set_setting(int(round(value)))
