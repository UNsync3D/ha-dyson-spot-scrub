"""Dyson Spot+Scrub AI — Home Assistant integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

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

    try:
        await coordinator.async_setup()
    except Exception as exc:
        _LOGGER.error("[%s] Failed to set up MQTT connection: %s", serial, exc)
        raise ConfigEntryNotReady(f"Could not connect to {serial}: {exc}") from exc

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: DysonCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()
    return unload_ok
