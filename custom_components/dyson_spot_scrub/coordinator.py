"""Coordinator — bridges the paho MQTT thread to HA's asyncio event loop."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_MQTT_PREFIX,
    DEFAULT_MODE,
    CONF_CACHED_ROOMS,
    MODE_TO_INT,
)
from .dyson_api import (
    DysonApiError,
    DysonRateLimitError,
    get_iot_credentials,
    get_live_map,
    get_map_metadata,
)
from .dyson_mqtt import DysonMqttClient, is_any_cleaning, start_block_reason

_LOGGER = logging.getLogger(__name__)

# Reconnect backoff schedule (seconds): 15 s, 30 s, 60 s, then 120 s forever
_RECONNECT_DELAYS = [15, 30, 60, 120]
# How long Dyson's broker has to accept a connection before we give up on it.
_CONNECT_CONFIRM_TIMEOUT = 20.0

# Robot states that indicate a clean cycle has genuinely completed.
# Deliberately excludes FULL_CLEAN_CHARGING (mid-clean recharge) and
# WASHING_MOP / DRYING_MOP (dock cycle stages) — those are transient
# states within a clean, not signals that the room is done.
_CLEAN_COMPLETE_ROBOT_STATES: frozenset[str] = frozenset({"CHARGING", "INACTIVE_CHARGING"})

# Minimum seconds between upstream live-map API calls (shared across all viewers)
_LIVE_MAP_MIN_INTERVAL = 5.0


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

        # HA's shared aiohttp session — reused for every REST call so the
        # 5-second live-map poll doesn't open a new TLS connection each time.
        self.http_session = async_get_clientsession(hass)

        # Entities register here; coordinator calls them on every state change
        self._listeners: list[Any] = []

        # Currently selected cleaning mode (persisted between restarts via HA storage)
        self.current_mode: str = DEFAULT_MODE

        # Room names from the robot's map preference cache.
        # Loaded from the config entry on startup so room switch entities can be
        # created immediately, even before the MQTT connection delivers fresh data.
        self.cached_room_names: list[str] = list(
            config_entry.data.get(CONF_CACHED_ROOMS, [])
        )

        # Rooms whose switch entity is toggled ON (for sequential clean)
        self.enabled_rooms: set[str] = set()

        # Sequential cleaning task — retained so stop/return_to_base can cancel it
        self._cleaning_task: asyncio.Task | None = None

        # Last MQTT state snapshot — captured on the paho thread and applied on the
        # HA event loop so fast transitions (e.g. FINISHED → RUNNING) are never lost.
        self._state_snapshot: dict = {}

        # Reconnect state
        self._shutting_down: bool = False
        self._reconnect_task: asyncio.Task | None = None
        self._inventory_task: asyncio.Task | None = None
        self._reconnect_attempt: int = 0
        # Last known robot reachability, kept across reconnect attempts.
        self._robot_online: bool = True

        # Live-map fetch: shared across all camera viewers (deduplication + 429 backoff)
        self._live_map_cache: dict | None = None
        self._live_map_fetched_at: float = 0.0
        self._live_map_backoff_until: float = 0.0
        self._live_map_lock: asyncio.Lock = asyncio.Lock()
        self._live_map_generation = 0

    # ── Setup / teardown ──────────────────────────────────────────────────────

    async def async_setup(self) -> None:
        """Open the cloud MQTT connection.

        Fetches fresh IoT credentials from Dyson's API and connects to the
        AWS IoT Core broker over WebSocket (port 443).

        On failure the error is logged but NOT re-raised so the config entry
        still loads and entities are created.  They show as unavailable until
        a background reconnect succeeds.
        """
        self._shutting_down = False
        self._reconnect_attempt = 0

        try:
            await self._async_connect_cloud()
        except Exception as exc:
            _LOGGER.warning(
                "[%s] Initial MQTT connection failed (%s) — "
                "entities will load in unavailable state; "
                "reconnect will retry automatically",
                self.serial, exc,
            )
            if not self._shutting_down and (
                    self._reconnect_task is None or self._reconnect_task.done()):
                self._reconnect_task = self.hass.async_create_task(
                    self._async_reconnect()
                )

    async def _async_connect_cloud(self, robot_online: bool = True) -> None:
        """Fetch fresh IoT credentials and connect to the cloud MQTT broker."""
        _LOGGER.debug("[%s] Fetching IoT credentials from Dyson API", self.serial)
        iot = await get_iot_credentials(self._token, self.serial, session=self.http_session)

        new_client = DysonMqttClient(
            serial=self.serial,
            mqtt_prefix=self._prefix,
            endpoint=iot["endpoint"],
            token_value=iot["token_value"],
            token_signature=iot["token_signature"],
            client_id=iot["client_id"],
            authorizer_name=iot["authorizer_name"],
            verbose=self._verbose,
        )
        new_client.on_connected      = self._on_mqtt_connected
        new_client.on_disconnected   = self._on_mqtt_disconnected
        new_client.on_prefix_changed = self._on_mqtt_prefix_changed
        new_client.on_map_changed    = self._on_mqtt_map_changed
        new_client.register_callback(self._on_state_change)
        # Carry a GONE-AWAY over a reconnect: a new broker connection doesn't
        # mean the robot came back.
        new_client.robot_online = robot_online

        # P1 #5: Assign before connecting so async_shutdown can disconnect
        # the client if it is called while connect() is blocking in the executor.
        self.mqtt = new_client
        try:
            await self.hass.async_add_executor_job(new_client.connect)
            # connect() only sends the request; wait for the broker to accept it.
            loop = asyncio.get_running_loop()
            deadline = loop.time() + _CONNECT_CONFIRM_TIMEOUT
            while not new_client.connected:
                if self._shutting_down:
                    raise ConnectionError("shutting down")
                if loop.time() >= deadline:
                    raise ConnectionError(
                        f"Dyson's broker did not accept the connection within "
                        f"{_CONNECT_CONFIRM_TIMEOUT:.0f} s")
                await asyncio.sleep(0.25)
        except Exception:
            if self.mqtt is new_client:
                self.mqtt = None
            try:
                await self.hass.async_add_executor_job(new_client.disconnect)
            except Exception:
                pass
            raise

    async def async_shutdown(self) -> None:
        self._shutting_down = True
        await self._async_cancel_cleaning_task()
        if self._inventory_task and not self._inventory_task.done():
            self._inventory_task.cancel()
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
        """Reconnect loop — runs until connected or shut down.

        P1 #4: self-contained loop (not recursive) so a disconnect that fires
        while we are sleeping in back-off does not orphan the retry chain.
        Fetches fresh IoT credentials on each attempt because the tokens expire.
        """
        while not self._shutting_down:
            delay = _RECONNECT_DELAYS[
                min(self._reconnect_attempt, len(_RECONNECT_DELAYS) - 1)
            ]
            self._reconnect_attempt += 1
            _LOGGER.info(
                "[%s] Reconnect attempt %d — waiting %d s before retrying",
                self.serial, self._reconnect_attempt, delay,
            )
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return

            if self._shutting_down:
                return

            # Tear down the old client cleanly (stop its loop thread). This
            # is a deliberate close, so it doesn't log as a disconnect.
            old_mqtt = self.mqtt
            self.mqtt = None
            if old_mqtt:
                self._robot_online = old_mqtt.robot_online
            robot_online = self._robot_online
            if old_mqtt:
                try:
                    await self.hass.async_add_executor_job(old_mqtt.disconnect)
                except Exception:
                    pass

            _LOGGER.debug("[%s] Reconnect attempt %d: connecting",
                          self.serial, self._reconnect_attempt)
            try:
                await self._async_connect_cloud(robot_online)
                attempts, self._reconnect_attempt = self._reconnect_attempt, 0
                _LOGGER.info("[%s] Reconnected to Dyson's broker (attempt %d)%s",
                             self.serial, attempts,
                             "" if robot_online else "; robot still offline")
                return  # Done — exit the loop
            except Exception as exc:
                if self._shutting_down:
                    return
                _LOGGER.warning(
                    "[%s] Reconnect attempt %d failed: %s: %s — retrying",
                    self.serial, self._reconnect_attempt, type(exc).__name__, exc,
                )
                # Loop continues to next iteration with updated backoff

    # ── Entity registration ───────────────────────────────────────────────────

    def async_add_listener(self, listener: Any) -> None:
        """Register an entity to be notified of state changes."""
        self._listeners.append(listener)

    def async_remove_listener(self, listener: Any) -> None:
        self._listeners = [l for l in self._listeners if l is not listener]

    # ── Live-map fetch (shared across camera viewers) ─────────────────────────

    @callback
    def async_clear_live_map_cache(self) -> None:
        """Discard an old session's map without cancelling a rate-limit backoff."""
        self._live_map_generation += 1
        self._live_map_cache = None

    async def async_get_live_map(self) -> dict | None:
        """Fetch the live cleaning map, shared across all viewers.

        Returns cached data if it is still fresh (within _LIVE_MAP_MIN_INTERVAL).
        Returns None — without touching the cache — when inside a 429 back-off
        window or when the upstream request fails.  Callers should keep their
        previous good frame when None is returned during an active cleaning run.

        At most one coroutine fetches at a time; others wait and share the result.
        """
        now = time.monotonic()

        # Inside 429 back-off — don't attempt a new request
        if now < self._live_map_backoff_until:
            _LOGGER.debug(
                "[%s] Live-map: 429 back-off %.0f s remaining",
                self.serial,
                self._live_map_backoff_until - now,
            )
            return None

        # Return cached data if it is fresh enough (skip the lock — fast path)
        if (
            self._live_map_cache is not None
            and (now - self._live_map_fetched_at) < _LIVE_MAP_MIN_INTERVAL
        ):
            return self._live_map_cache

        # One coroutine fetches at a time; the rest wait and share the result
        async with self._live_map_lock:
            # Re-check after acquiring — a previous waiter may have already fetched
            now = time.monotonic()
            if now < self._live_map_backoff_until:
                return None
            if (
                self._live_map_cache is not None
                and (now - self._live_map_fetched_at) < _LIVE_MAP_MIN_INTERVAL
            ):
                return self._live_map_cache

            try:
                generation = self._live_map_generation
                data = await get_live_map(self._token, self.serial, session=self.http_session)
                if generation != self._live_map_generation:
                    return None
                self._live_map_cache = data
                self._live_map_fetched_at = time.monotonic()
                return data
            except DysonApiError as exc:
                if isinstance(exc, DysonRateLimitError):
                    retry_after = max(5.0, exc.retry_after)
                    self._live_map_backoff_until = time.monotonic() + retry_after
                    _LOGGER.warning(
                        "[%s] Live-map HTTP 429 — backing off %.0f s",
                        self.serial,
                        retry_after,
                    )
                else:
                    _LOGGER.debug("[%s] Live-map fetch failed: %s", self.serial, exc)
                # Return None so callers keep their last-good frame
                return None
            except Exception as exc:  # noqa: BLE001
                # Transport-layer failures (aiohttp.ClientError, asyncio.TimeoutError,
                # SSL errors, DNS failures, …) are not DysonApiError subclasses.
                # Without this handler they would propagate through the lock, bypass
                # the camera's "return last-good frame on None" fallback, and surface
                # as an unhandled exception in the camera entity.
                _LOGGER.warning(
                    "[%s] Live-map fetch failed (transport error): %s: %s",
                    self.serial, type(exc).__name__, exc,
                )
                return None

    # ── Internal callbacks (called from paho thread) ──────────────────────────

    def _on_state_change(self, state: dict) -> None:
        """paho thread → copy snapshot then schedule application on HA event loop."""
        snapshot = dict(state)  # copy here; mqtt.state may change before the loop runs
        self.hass.loop.call_soon_threadsafe(self._async_apply_state, snapshot)

    def _on_mqtt_connected(self) -> None:
        self.hass.loop.call_soon_threadsafe(self._async_update_rooms_and_notify)

    def _on_mqtt_map_changed(self, map_id: str) -> None:
        """paho thread → refresh the room inventory for the (new) current map."""
        self.hass.loop.call_soon_threadsafe(self._async_schedule_inventory_refresh)

    @callback
    def _async_schedule_inventory_refresh(self) -> None:
        if self._shutting_down:
            return
        task = self._inventory_task
        if task is None or task.done():
            self._inventory_task = self.hass.async_create_task(
                self.async_refresh_map_inventory())

    async def async_refresh_map_inventory(self) -> None:
        """Feed the REST map metadata (authoritative room list) to the MQTT client.

        get_preference replies can be partial, so they never remove rooms; this
        is what does. Called on map change (including the first report after
        connecting); the camera also passes on metadata it fetches. A failure
        only means removed rooms linger until the next successful refresh.
        """
        mqtt = self.mqtt
        if mqtt is None:
            return
        try:
            maps = await get_map_metadata(self._token, self.serial, session=self.http_session)
        except Exception as exc:  # noqa: BLE001 — best effort
            _LOGGER.debug("[%s] Map metadata refresh failed: %s", self.serial, exc)
            return
        self.apply_map_inventory(maps)

    def apply_map_inventory(self, maps) -> None:
        """Pass a get_map_metadata() result to the MQTT client's room store."""
        mqtt = self.mqtt
        if not isinstance(maps, list) or mqtt is None or not hasattr(mqtt, "set_map_inventory"):
            return
        for entry in maps:
            if isinstance(entry, dict) and entry.get("id") is not None:
                mqtt.set_map_inventory(entry["id"], entry.get("zones") or [])

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
            if self._reconnect_task is None or self._reconnect_task.done():
                self._reconnect_task = self.hass.async_create_task(
                    self._async_reconnect()
                )

    @callback
    def _async_apply_state(self, snapshot: dict) -> None:
        """Store the state snapshot then notify listeners (runs on HA event loop)."""
        self._state_snapshot = snapshot
        self._async_update_rooms_and_notify()

    @callback
    def _async_update_rooms_and_notify(self) -> None:
        """Check for new room names from MQTT, persist if changed, then notify."""
        if self.mqtt:
            new_rooms = self.mqtt.room_names
            if new_rooms and set(new_rooms) != set(self.cached_room_names):
                self.cached_room_names = list(new_rooms)
                self.hass.config_entries.async_update_entry(
                    self._config_entry,
                    data={**self._config_entry.data, CONF_CACHED_ROOMS: new_rooms},
                )
                _LOGGER.debug(
                    "[%s] Room names cached: %s", self.serial, new_rooms
                )
        self._async_notify_listeners()

    @property
    def state(self) -> dict:
        """Last MQTT state snapshot, set on the HA event loop."""
        return self._state_snapshot

    @callback
    def _async_notify_listeners(self) -> None:
        for listener in list(self._listeners):
            try:
                listener.async_write_ha_state()
            except Exception:
                _LOGGER.exception("[%s] Error notifying listener", self.serial)

    # ── Sequential multi-room cleaning ────────────────────────────────────────

    async def _async_cancel_cleaning_task(self) -> None:
        """Cancel any in-flight sequential room-cleaning task."""
        task = self._cleaning_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._cleaning_task = None

    async def async_stop(self) -> None:
        """Stop cleaning and cancel any sequential room task (P1 #2)."""
        await self._async_cancel_cleaning_task()
        if self.mqtt and self.mqtt.available:
            await self.hass.async_add_executor_job(self.mqtt.stop)

    async def async_return_to_base(self) -> None:
        """Return to base and cancel any sequential room task (P1 #2)."""
        await self._async_cancel_cleaning_task()
        if self.mqtt and self.mqtt.available:
            await self.hass.async_add_executor_job(self.mqtt.return_to_base)

    @property
    def room_names(self) -> list[str]:
        """Live room names from MQTT, falling back to the names persisted at last run.

        The fallback lets per-room entities be created at startup before MQTT
        connects (e.g. while the robot is drying or briefly offline).
        """
        if self.mqtt and self.mqtt.room_names:
            return self.mqtt.room_names
        return self.cached_room_names

    async def async_clean_rooms_sequential(self, room_names: list[str]) -> None:
        """Clean a list of rooms one at a time, waiting for docking between each."""
        if not room_names:
            return
        reason = start_block_reason(self.mqtt.state if self.mqtt else {}, bool(self.mqtt and self.mqtt.available))
        if reason:
            _LOGGER.warning("Sequential clean blocked: %s", reason)
            return
        # P1 #2: record this task so stop/return_to_base can cancel it
        self._cleaning_task = asyncio.current_task()
        try:
            _LOGGER.info("[%s] Sequential clean starting — rooms: %s", self.serial, room_names)
            for room in room_names:
                if not self.mqtt or not self.mqtt.available:
                    _LOGGER.warning("[%s] robot not reachable — aborting sequential clean", self.serial)
                    return
                _LOGGER.info("[%s] Sequential clean → '%s'", self.serial, room)
                mode_int = MODE_TO_INT.get(self.current_mode, 0)
                started = await self.hass.async_add_executor_job(
                    self.mqtt.start_room, room, mode_int,
                )
                if not started:
                    _LOGGER.warning(
                        "[%s] Sequential clean stopped: could not start %r", self.serial, room,
                    )
                    return
                completed = await self._async_wait_for_clean_complete()
                if not completed:
                    _LOGGER.warning(
                        "[%s] Sequential clean aborted: timed out waiting for %r to dock",
                        self.serial, room,
                    )
                    return
            _LOGGER.info("[%s] Sequential clean finished all rooms", self.serial)
        finally:
            self._cleaning_task = None

    async def _async_wait_for_clean_complete(self, timeout: float = 3600.0) -> bool:
        """Wait until the robot returns to dock after cleaning.

        Require both a completion state and new-job readiness. Charging alone
        can precede the end of station work. Drying after a completed job may
        be interrupted by the next room; mid-job charging remains blocked.
        """
        done_event: asyncio.Event = asyncio.Event()
        cleaning_started = False

        def _check() -> None:
            nonlocal cleaning_started
            if not self.mqtt:
                # Client torn down (shutdown/reconnect). Returning "complete"
                # lets the loop's own connected-check abort the sequence.
                done_event.set()
                return
            state = self.state  # snapshot — already on HA event loop
            if is_any_cleaning(state):
                cleaning_started = True
            elif (cleaning_started and state.get("state") in _CLEAN_COMPLETE_ROBOT_STATES
                  and start_block_reason(state, self.mqtt.available) is None):
                done_event.set()

        class _Watcher:
            # Listeners are always notified on the HA event loop, so _check
            # can run directly — no call_soon_threadsafe hop needed.
            def async_write_ha_state(inner_self) -> None:
                _check()

        watcher = _Watcher()
        self.async_add_listener(watcher)
        try:
            await asyncio.wait_for(done_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            _LOGGER.warning("[%s] Timed out waiting for robot to dock", self.serial)
            return False
        finally:
            self.async_remove_listener(watcher)
