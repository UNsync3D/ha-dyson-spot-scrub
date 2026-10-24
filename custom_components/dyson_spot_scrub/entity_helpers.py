"""Small helpers shared by every entity platform."""
from __future__ import annotations

from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN, CONF_SERIAL, CONF_DEVICE_NAME, CONF_PRODUCT_TYPE


def device_info(entry) -> DeviceInfo:
    """DeviceInfo that groups all of a robot's entities under one HA device."""
    serial = entry.data[CONF_SERIAL]
    return DeviceInfo(
        identifiers={(DOMAIN, serial)},
        name=entry.data[CONF_DEVICE_NAME],
        manufacturer="Dyson",
        model=entry.data.get(CONF_PRODUCT_TYPE, "RB0S"),
        serial_number=serial,
    )


def room_slug(room_name: str) -> str:
    """unique_id fragment for a room.

    Must stay byte-for-byte stable: changing it would orphan every existing
    per-room entity in users' entity registries.
    """
    return room_name.lower().replace(" ", "_")
