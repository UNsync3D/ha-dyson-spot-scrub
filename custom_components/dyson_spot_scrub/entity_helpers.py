"""Small helpers shared by every entity platform."""
from __future__ import annotations

from homeassistant.helpers.entity import DeviceInfo

from .const import (DOMAIN, CONF_SERIAL, CONF_DEVICE_NAME, CONF_PRODUCT_TYPE, CONF_MQTT_PREFIX,
                    CONF_HAS_MOP)


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


def is_nurovi_r2(entry) -> bool:
    """Dyson R2 Nurovi Wash+Dry: product type 914, MQTT prefix RB07."""
    data = getattr(entry, "data", {}) or {}
    return (str(data.get(CONF_PRODUCT_TYPE, "")) == "914"
            or str(data.get(CONF_MQTT_PREFIX, "")).upper() == "RB07")


def supports_whole_home(entry) -> bool:
    """Whole-home clean: confirmed on the Spot+Scrub AI (804 / RB05) only."""
    data = getattr(entry, "data", {}) or {}
    return (str(data.get(CONF_PRODUCT_TYPE, "")) == "804"
            or str(data.get(CONF_MQTT_PREFIX, "")).upper() == "RB05")


def has_mop(entry) -> bool:
    """False once the robot has reported it has no mop (#49); True until then."""
    data = getattr(entry, "data", {}) or {}
    return data.get(CONF_HAS_MOP, True) is not False


_MOP_ONLY_IDS = frozenset({"consumable_mop_roller_usage", "consumable_mop_pad_usage",
                           "consumable_cleaning_solution", "drying_remaining",
                           "wash_mop", "dry_mop"})


def is_mop_only_entity(unique_id: str, serial: str) -> bool:
    """Unique IDs of the entities only a robot with a mop gets (#49)."""
    if not unique_id or not unique_id.startswith(f"{serial}_"):
        return False
    rest = unique_id[len(serial) + 1:]
    return (rest.startswith("station_") or rest in _MOP_ONLY_IDS
            or (rest.startswith("room_") and rest.endswith("_water_level")))


def room_slug(room_name: str) -> str:
    """unique_id fragment for a room.

    Must stay byte-for-byte stable: changing it would orphan every existing
    per-room entity in users' entity registries.
    """
    return room_name.lower().replace(" ", "_")
