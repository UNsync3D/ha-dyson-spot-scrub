"""Time platform: Do Not Disturb start and end (#50)."""
from __future__ import annotations

from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .station_entity import StationEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        DysonDoNotDisturbTime(coordinator, entry, "do_not_disturb_start", "Do not disturb start"),
        DysonDoNotDisturbTime(coordinator, entry, "do_not_disturb_end", "Do not disturb end"),
    ])


class DysonDoNotDisturbTime(StationEntity, TimeEntity):
    """Changing one time keeps the other and the on/off state, like the app."""

    _id_group = "setting"
    _attr_icon = "mdi:clock-outline"

    @property
    def native_value(self) -> time | None:
        return self.setting_value

    async def async_set_value(self, value: time) -> None:
        await self._async_set_setting(value.replace(second=0, microsecond=0))
