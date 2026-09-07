"""Dyson Spot+Scrub AI — Home Assistant integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import (
    DOMAIN,
    PLATFORMS,
    CONF_AUTH_TOKEN,
    CONF_SERIAL,
    CONF_MQTT_PREFIX,
)
from .coordinator import DysonCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Dyson Spot+Scrub AI from a config entry."""
    token       = entry.data[CONF_AUTH_TOKEN]
    serial      = entry.data[CONF_SERIAL]
    mqtt_prefix = entry.data.get(CONF_MQTT_PREFIX, "NROB")
    verbose     = entry.options.get("log_mqtt", False)

    coordinator = DysonCoordinator(
        hass=hass,
        token=token,
        serial=serial,
        mqtt_prefix=mqtt_prefix,
        config_entry=entry,
        verbose=verbose,
    )

    # async_setup is non-fatal — it logs and retries in the background if the
    # initial MQTT connection fails.  Entities are always created; they show
    # as unavailable until MQTT connects.
    await coordinator.async_setup()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # ── Remove orphaned entities from older versions ───────────────────────────
    # The "Clean Room" select entity (unique_id: <serial>_room_select) was
    # removed in v1.2.0 — clean it from the registry so it doesn't show as
    # "unavailable" on users upgrading from an earlier release.
    _purge_orphan_entities(hass, entry, serial)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _purge_orphan_entities(hass: HomeAssistant, entry: ConfigEntry, serial: str) -> None:
    """Remove entity registry entries left behind by older versions."""
    obsolete_unique_ids = [
        ("select", f"{serial}_room_select"),   # removed in v1.2.0
    ]
    reg = er.async_get(hass)
    for platform, uid in obsolete_unique_ids:
        entity_id = reg.async_get_entity_id(platform, DOMAIN, uid)
        if entity_id:
            _LOGGER.info("Removing obsolete entity %s", entity_id)
            reg.async_remove(entity_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: DysonCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()
    return unload_ok
