"""Coordinator — bridges the paho MQTT thread to HA's asyncio event loop."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .const import CONF_SERIAL, CONF_MQTT_PREFIX, DEFAULT_MODE
from .dyson_api import get_iot_credentials
from .dyson_mqtt import DysonMqttClient

_LOGGER = logging.getLogger(__name__)

# Reconnect backoff schedule (seconds): 15 s, 30 s, 60 s, then 120 s forever
_RECONNECT_DELAYS = [15, 30, 60, 120]


class DysonCoordinator:
    """Owns the MQTT client and distributes state to registered HA entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        token: str,
        serial: str,
        mqtt_prefix: str,
        config_entry: ConfigEntry,
        verbose: bool = False,
    ) -> None:
        self.hass          = hass
        self._token        = token
        self.serial        = serial
        self._prefix       = mqtt_prefix
        self._config_entry = config_entry
        self._verbose      = verbose

        self.mqtt: DysonMqttClient | None = None

        # Entities register here; coordinator calls them on every state change
        self._listeners: list[Any] = []

        # Currently selected cleaning mode (persisted between restarts via HA storage)
        self.current_mode: str = DEFAULT_MODE

        # Reconnect state
        self._shutting_down: bool = False
        self._reconnect_task: asyncio.Task | None = None
        self._reconnect_attempt: int = 0

    # ── Setup / teardown ──────────────────────────────────────────────────────

    async def async_setup(self) -> None:
        """Fetch IoT credentials and open the MQTT connection."""
        _LOGGER.debug("[%s] Fetching IoT credentials", self.serial)
        iot_creds = await get_iot_credentials(self._token, self.serial)
        self._shutting_down = False
        self._reconnect_attempt = 0
        await self._async_connect_with_creds(iot_creds)

    async def _async_connect_with_creds(self, iot_creds: dict) -> None:
        """Build a fresh DysonMqttClient from credentials and connect."""
        new_client = DysonMqttClient(
            serial=self.serial,
            mqtt_prefix=self._prefix,
            iot_creds=iot_creds,
            verbose=self._verbose,
        )
        new_client.on_connected     = self._on_mqtt_connected
        new_client.on_disconnected  = self._on_mqtt_disconnected
        new_client.on_prefix_changed = self._on_mqtt_prefix_changed
        new_client.register_callback(self._on_state_change)

        await self.hass.async_add_executor_job(new_client.connect)
        self.mqtt = new_client

    async def async_shutdown(self) -> None:
        self._shutting_down = True
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass
        if self.mqtt:
            await self.hass.async_add_executor_job(self.mqtt.disconnect)

    # ── Reconnect logic ───────────────────────────────────────────────────────

    async def _async_reconnect(self) -> None:
        """Fetch fresh IoT credentials and reconnect, with backoff."""
        delay = _RECONNECT_DELAYS[
            min(self._reconnect_attempt, len(_RECONNECT_DELAYS) - 1)
        ]
        self._reconnect_attempt += 1
        _LOGGER.info(
            "[%s] Reconnect attempt %d — waiting %d s",
            self.serial, self._reconnect_attempt, delay,
        )
        await asyncio.sleep(delay)

        if self._shutting_down:
            return

        # Tear down the old client cleanly (stop its loop thread)
        old_mqtt = self.mqtt
        self.mqtt = None
        if old_mqtt:
            try:
                await self.hass.async_add_executor_job(old_mqtt.disconnect)
            except Exception:
                pass

        try:
            _LOGGER.info("[%s] Fetching fresh IoT credentials for reconnect", self.serial)
            iot_creds = await get_iot_credentials(self._token, self.serial)
        except Exception:
            _LOGGER.exception(
                "[%s] Failed to fetch IoT credentials — scheduling retry", self.serial
            )
            if not self._shutting_down:
                self._reconnect_task = self.hass.async_create_task(
                    self._async_reconnect()
                )
            return

        try:
            await self._async_connect_with_creds(iot_creds)
            self._reconnect_attempt = 0  # Reset backoff on success
            _LOGGER.info("[%s] Reconnected successfully", self.serial)
        except Exception:
            _LOGGER.exception(
                "[%s] Reconnect connect() failed — scheduling retry", self.serial
            )
            if not self._shutting_down:
                self._reconnect_task = self.hass.async_create_task(
                    self._async_reconnect()
                )

    # ── Entity registration ───────────────────────────────────────────────────

    def async_add_listener(self, listener: Any) -> None:
        """Register an entity to be notified of state changes."""
        self._listeners.append(listener)

    def async_remove_listener(self, listener: Any) -> None:
        self._listeners = [l for l in self._listeners if l is not listener]

    # ── Internal callbacks (called from paho thread) ──────────────────────────

    def _on_state_change(self, state: dict) -> None:
        """paho thread → schedule update on HA event loop."""
        self.hass.loop.call_soon_threadsafe(self._async_notify_listeners)

    def _on_mqtt_connected(self) -> None:
        self.hass.loop.call_soon_threadsafe(self._async_notify_listeners)

    def _on_mqtt_disconnected(self) -> None:
        _LOGGER.warning("[%s] MQTT disconnected — will reconnect", self.serial)
        self.hass.loop.call_soon_threadsafe(self._async_on_disconnected)

    def _on_mqtt_prefix_changed(self, new_prefix: str) -> None:
        """paho thread → schedule prefix persistence on HA event loop."""
        self.hass.loop.call_soon_threadsafe(
            self._async_on_prefix_changed, new_prefix
        )

    @callback
    def _async_on_prefix_changed(self, new_prefix: str) -> None:
        """Persist the corrected MQTT prefix to the config entry."""
        self._prefix = new_prefix  # use corrected prefix on next reconnect
        self.hass.config_entries.async_update_entry(
            self._config_entry,
            data={**self._config_entry.data, CONF_MQTT_PREFIX: new_prefix},
        )
        _LOGGER.info(
            "[%s] MQTT prefix updated to '%s' in config entry",
            self.serial, new_prefix,
        )

    @callback
    def _async_on_disconnected(self) -> None:
        """Runs on the HA event loop after an unexpected disconnect."""
        self._async_notify_listeners()
        if not self._shutting_down:
            # Only schedule a new reconnect task if one isn't already pending
            if self._reconnect_task is None or self._reconnect_task.done():
                self._reconnect_task = self.hass.async_create_task(
                    self._async_reconnect()
                )

    @callback
    def _async_notify_listeners(self) -> None:
        for listener in list(self._listeners):
            try:
                listener.async_write_ha_state()
            except Exception:
                _LOGGER.exception("[%s] Error notifying listener", self.serial)
