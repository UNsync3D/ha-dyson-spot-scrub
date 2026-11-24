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
    CONF_HAS_MOP,
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
from .room_locator import dock_room, latest_position, locate, room_outlines
from .room_types import translate_room
from .schedules import (
    ADD_ORDER, DEL_ORDER, ScheduleBook, ScheduleError, days_to_mask, describe_order, new_order_id,
    ordered_params, parse_time, room_plan, time_conflict, toggled, uv_plan, validate_order,
)

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
# Room outlines for the current room (#53): at most one map fetch this often.
_OUTLINE_RETRY_SECONDS = 600.0
# Schedules (#67): ask for the robot's summary this often, and after a change.
_SCHEDULE_POLL_SECONDS = 1800.0
_SCHEDULE_STORAGE_VERSION = 1


def _docked(state: dict) -> bool:
    """On the dock (dyson_mqtt.is_docked, imported late for the test stubs)."""
    from .dyson_mqtt import is_docked
    return is_docked(state)


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

        # Sequential cleaning task — retained so stop/return_to_base can cancel it.
        # Only one sequence runs at a time (async_start_sequential), and only
        # the task that owns this slot may clear it (#42).
        self._cleaning_task: asyncio.Task | None = None
        # Bumped by Stop, Home and shutdown. A sequence started before the
        # bump never starts another room, even if it was somehow not cancelled.
        self._stop_generation: int = 0

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
        # Current room (#53): room outlines of one map, fetched when needed.
        self._room_outlines: list | None = None
        self._room_outlines_map: str | None = None
        self._outline_fetch_at = -_OUTLINE_RETRY_SECONDS
        self._outline_task = None
        self.current_room_id: str | None = None
        # Standard room types in the user's language, key -> name (#57).
        self.room_type_names: dict[str, str] = {}
        # Weekday names in the user's language, "mon" -> "Mon" (#67).
        self.weekday_names: dict[str, str] = {}
        # Cleaning schedules on the robot, as far as Home Assistant has seen (#67).
        self.schedules = ScheduleBook()
        self._schedule_store = None
        self._schedule_poll_unsub = None
        self._schedule_lock = asyncio.Lock()
        self._dock_room_id: str | None = None
        # Leaving the dock (FutuRazor's idea, #53): until the robot reports a
        # new position, it is in the dock's room. The last position from the
        # previous clean is kept so it isn't mistaken for a new one.
        self._seen_docked = False
        self._position_at_dock = None
        self._fresh_position = False

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
        new_client.on_schedule_message = self._on_schedule_message
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
        if self._schedule_poll_unsub:
            self._schedule_poll_unsub()
            self._schedule_poll_unsub = None
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
        self.hass.loop.call_soon_threadsafe(self._async_request_schedule_summary)

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
    def _async_check_mop(self) -> None:
        """Save whether the robot has a mop; reload once when that changes (#49).

        Entities are created from the saved value, so a reload is what adds or
        removes the mop-only ones. Nothing happens until a full CURRENT-STATE
        says, and only when the answer differs from what's saved.
        """
        reported = getattr(self.mqtt, "has_mop", None) if self.mqtt else None
        if reported is None:
            return
        saved = self._config_entry.data.get(CONF_HAS_MOP, True) is not False
        if reported == saved and CONF_HAS_MOP in self._config_entry.data:
            return
        self.hass.config_entries.async_update_entry(
            self._config_entry, data={**self._config_entry.data, CONF_HAS_MOP: reported})
        if reported != saved:
            _LOGGER.info("[%s] Robot reports %s; reloading to update its controls",
                         self.serial, "a mop" if reported else "no mop")
            self.hass.config_entries.async_schedule_reload(self._config_entry.entry_id)

    def room_label(self, room_name: str) -> str:
        """A room's name for display: standard types in the user's language (#57)."""
        return translate_room(room_name, self.room_type_names)

    # ── Current room (#53) ────────────────────────────────────────────────────

    @property
    def current_room(self) -> str | None:
        """Name of the room the robot is in; None if docked or unknown."""
        if self.current_room_id is None or self.mqtt is None:
            return None
        from .dyson_mqtt import _room_display_name
        for row in self.mqtt.room_preferences or []:
            if isinstance(row, (list, tuple)) and len(row) > 1 and str(row[0]) == self.current_room_id:
                return _room_display_name(row[1])
        return None

    @property
    def current_room_state(self) -> str | None:
        """For the Current room sensor: the room, "docked", or None if unknown.

        "docked" is a state key, shown translated ("Docked", "Angedockt"); room
        names have no translation and are shown as they are.
        """
        if _docked(self.state):
            return "docked"
        return self.current_room

    def set_room_outlines(self, map_id, map_data) -> None:
        """Room outlines from a persistent map (the camera passes on what it fetches)."""
        outlines = room_outlines(map_data)
        if map_id is not None and outlines:
            self._room_outlines, self._room_outlines_map = outlines, str(map_id)
            self._dock_room_id = dock_room(map_data, outlines)

    @callback
    def _async_update_current_room(self) -> None:
        state = self.state
        position = latest_position(state)
        if _docked(state):
            self.current_room_id = None
            self._seen_docked = True
            self._position_at_dock = position
            self._fresh_position = False
            return
        map_id = state.get("persistentMapId")
        if map_id is None:
            return
        map_id = str(map_id)
        if self._room_outlines_map != map_id:
            self._async_fetch_room_outlines(map_id)
            return
        if position is not None and (self._fresh_position or position != self._position_at_dock):
            self._fresh_position = True
            self.current_room_id = locate(self._room_outlines, *position, self.current_room_id)
        elif not self._fresh_position and self._seen_docked:
            # Just left the dock; the robot is still finding its position.
            self.current_room_id = self._dock_room_id

    @callback
    def _async_fetch_room_outlines(self, map_id: str) -> None:
        now = time.monotonic()
        if (self._shutting_down or now - self._outline_fetch_at < _OUTLINE_RETRY_SECONDS
                or (self._outline_task is not None and not self._outline_task.done())):
            return
        self._outline_fetch_at = now

        async def fetch() -> None:
            try:
                from .dyson_api import get_map
                data = await get_map(self._token, self.serial, map_id, session=self.http_session)
            except Exception as exc:  # noqa: BLE001 - best effort; retried later
                _LOGGER.debug("[%s] Room outlines fetch failed: %s", self.serial, exc)
                return
            self.set_room_outlines(map_id, data)
            if self._room_outlines_map == map_id:
                self._async_update_current_room()
                self._async_notify_listeners()
        self._outline_task = self.hass.async_create_task(fetch())

    @callback
    def _async_update_rooms_and_notify(self) -> None:
        """Check for new room names from MQTT, persist if changed, then notify."""
        self._async_check_mop()
        self._async_update_current_room()
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

    # ── Cleaning schedules (#67) ──────────────────────────────────────────────

    async def async_setup_schedules(self) -> None:
        """Load Home Assistant's copy of the robot's schedules and start the summary poll."""
        from datetime import timedelta
        from homeassistant.helpers.event import async_track_time_interval
        from homeassistant.helpers.storage import Store
        from .const import DOMAIN

        self._schedule_store = Store(
            self.hass, _SCHEDULE_STORAGE_VERSION, f"{DOMAIN}.schedules.{self.serial}")
        try:
            data = await self._schedule_store.async_load()
        except Exception as exc:  # noqa: BLE001 - start empty rather than fail setup
            _LOGGER.warning("[%s] Couldn't load saved schedules: %s", self.serial, exc)
            data = None
        self.schedules = ScheduleBook.from_dict(data)

        async def _poll(_now) -> None:
            self._async_request_schedule_summary()

        self._schedule_poll_unsub = async_track_time_interval(
            self.hass, _poll, timedelta(seconds=_SCHEDULE_POLL_SECONDS))

    def _on_schedule_message(self, kind: str, data: dict) -> None:
        """paho thread → apply schedule traffic on the HA event loop."""
        self.hass.loop.call_soon_threadsafe(self._async_schedule_message, kind, data)

    @callback
    def _async_schedule_message(self, kind: str, data: dict) -> None:
        book = self.schedules
        if kind == "command":
            book.command_seen(data)
            return
        was_in_sync = book.in_sync
        changed = book.reply_seen(data) if kind == "reply" else book.total_seen(data)
        if not changed:
            return
        if was_in_sync is not False and book.in_sync is False:
            _LOGGER.warning(
                "[%s] The robot has %s schedule(s); Home Assistant knows %d. Open and "
                "save any missing schedule once in the MyDyson app so Home Assistant "
                "can learn it", self.serial, book.robot_total, len(book.orders))
        self._async_schedules_changed()

    @callback
    def _async_schedules_changed(self) -> None:
        if self._schedule_store is not None:
            self._schedule_store.async_delay_save(self.schedules.to_dict, 1)
        self._async_notify_listeners()

    @callback
    def _async_request_schedule_summary(self) -> None:
        mqtt = self.mqtt
        if mqtt is None or self._shutting_down or not hasattr(mqtt, "request_order_summary"):
            return
        self.hass.async_add_executor_job(mqtt.request_order_summary)

    def describe_schedule(self, order: dict) -> dict:
        """A schedule for attributes and action responses, room names as on the room entities."""
        from .dyson_mqtt import _room_display_name
        return describe_order(order, _room_display_name)

    def _schedule_writer(self):
        mqtt = self.mqtt
        if mqtt is None or not mqtt.available:
            raise ScheduleError("The Dyson vacuum is disconnected")
        if not getattr(mqtt, "supports_schedule_writes", False):
            raise ScheduleError("Changing schedules isn't confirmed on this robot model yet")
        return mqtt

    async def _async_send_order(self, method: str, params: dict) -> None:
        mqtt = self._schedule_writer()
        code = await self.hass.async_add_executor_job(mqtt.send_order_command, method, params)
        if code != 0:
            raise ScheduleError(
                "The robot didn't confirm the schedule change" if code is None
                else f"The robot refused the schedule change (code {code})")
        if method == ADD_ORDER:
            self.schedules.apply_add(params)
        else:
            self.schedules.apply_delete(params["id"])
        self._async_schedules_changed()
        self._async_request_schedule_summary()

    async def async_set_schedule_enabled(self, schedule_id: int, enabled: bool) -> dict:
        """Switch a known schedule on or off, as the app does."""
        async with self._schedule_lock:
            order = self.schedules.orders.get(schedule_id)
            if order is None:
                raise ScheduleError(f"Unknown schedule: {schedule_id}")
            if enabled and time_conflict(self.schedules.orders.values(), order["day"],
                                         order["hour"], order["minute"], schedule_id):
                raise ScheduleError("Another schedule that is on starts at the same time on one of its days")
            params = toggled(order, enabled)
            await self._async_send_order(ADD_ORDER, params)
            return self.describe_schedule(params)

    async def async_set_schedule(
        self,
        schedule_id: int | None = None,
        days: list[str] | None = None,
        start_time: str | None = None,
        enabled: bool | None = None,
        room_ids: list | None = None,
        room_settings: dict | None = None,
    ) -> dict:
        """Create a schedule (no ID) or change one; omitted fields keep their value."""
        from homeassistant.util import dt as dt_util

        async with self._schedule_lock:
            mqtt = self._schedule_writer()
            existing = None
            if schedule_id is not None:
                existing = self.schedules.orders.get(schedule_id)
                if existing is None:
                    raise ScheduleError(f"Unknown schedule: {schedule_id}")
            elif not days or start_time is None or not room_ids:
                raise ScheduleError("A new schedule needs days, a time and rooms")
            try:
                day = days_to_mask(days) if days is not None else existing["day"]
                hour, minute = (parse_time(start_time) if start_time is not None
                                else (existing["hour"], existing["minute"]))
                if existing is None:
                    map_id, rows, uv_rows = mqtt.schedule_base()
                    if map_id is None or not rows:
                        raise ScheduleError("The robot's rooms aren't known yet")
                    chosen = room_ids
                else:
                    map_id = existing.get("map_id")
                    rows, uv_rows = existing["room_preference"], existing.get("uv_switch")
                    chosen = room_ids if room_ids is not None else [
                        row[0] for row in rows if row[8] == 1]
                plan = room_plan(rows, chosen, mqtt.apply_schedule_room_settings, room_settings)
            except ValueError as exc:
                raise ScheduleError(str(exc)) from exc
            enable = (1 if enabled else 0) if enabled is not None else (
                existing["enable"] if existing else 1)
            order_id = existing["id"] if existing else new_order_id(self.schedules.orders)
            if enable and time_conflict(self.schedules.orders.values(), day, hour, minute, order_id):
                raise ScheduleError("Another schedule that is on starts at the same time on one of these days")
            offset = dt_util.now().utcoffset()
            order = dict(existing or {})
            order.update({
                "id": order_id, "enable": enable, "day": day, "hour": hour, "minute": minute,
                "repeat": order.get("repeat", 1), "map_id": map_id, "room_count": len(plan),
                "time_zone": int(offset.total_seconds()) if offset else 0,
                "prefer_type": order.get("prefer_type", 1), "is_global": order.get("is_global", 0),
                "areas": order.get("areas", []), "room_preference": plan,
                "uv_switch": uv_plan(plan, uv_rows),
            })
            if not enable:
                order = toggled(order, False)
            params = ordered_params(order)
            try:
                validate_order(params)
            except ValueError as exc:
                raise ScheduleError(str(exc)) from exc
            await self._async_send_order(ADD_ORDER, params)
            return self.describe_schedule(params)

    async def async_delete_schedule(self, schedule_id: int) -> None:
        async with self._schedule_lock:
            if schedule_id not in self.schedules.orders:
                raise ScheduleError(f"Unknown schedule: {schedule_id}")
            await self._async_send_order(DEL_ORDER, {"id": schedule_id})

    # ── Sequential multi-room cleaning ────────────────────────────────────────

    async def _async_cancel_cleaning_task(self) -> None:
        """Cancel any in-flight sequential room-cleaning task."""
        self._stop_generation += 1
        task = self._cleaning_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if self._cleaning_task is task:
            self._cleaning_task = None

    @property
    def sequence_running(self) -> bool:
        """True while a sequential room clean is scheduled or running."""
        return self._cleaning_task is not None and not self._cleaning_task.done()

    @callback
    def async_start_sequential(self, room_names: list[str]) -> str | None:
        """Schedule a sequential room clean; return why not, or None when scheduled.

        The only entry point for the Start button and the clean_rooms action.
        Runs on the event loop with no await, so the check, the generation
        snapshot and claiming the slot happen together: a second request can't
        slip in, and a Stop pressed before the task first runs still applies.
        """
        if not room_names:
            return "no_rooms"
        if self.sequence_running:
            return "sequence_running"
        task = self.hass.async_create_task(
            self.async_clean_rooms_sequential(list(room_names), self._stop_generation))
        self._cleaning_task = task
        return None

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

    async def async_clean_whole_home(self) -> None:
        """Start a whole-home clean; raise with the reason when it can't start."""
        from homeassistant.exceptions import HomeAssistantError
        if self.sequence_running:
            raise HomeAssistantError("Cannot start a new clean: sequence running")
        reason = start_block_reason(self.mqtt.state if self.mqtt else {},
                                    bool(self.mqtt and self.mqtt.available))
        if reason:
            raise HomeAssistantError(f"Cannot start a new clean: {reason.replace('_', ' ')}")
        if not self.mqtt.supports_whole_home:
            raise HomeAssistantError(
                "Whole-home cleaning isn't confirmed on this robot model yet")
        started = await self.hass.async_add_executor_job(self.mqtt.start_whole_home)
        if not started:
            raise HomeAssistantError("Could not start the whole-home clean")

    @property
    def room_names(self) -> list[str]:
        """Live room names from MQTT, falling back to the names persisted at last run.

        The fallback lets per-room entities be created at startup before MQTT
        connects (e.g. while the robot is drying or briefly offline).
        """
        if self.mqtt and self.mqtt.room_names:
            return self.mqtt.room_names
        return self.cached_room_names

    async def async_clean_rooms_sequential(
        self, room_names: list[str], generation: int | None = None,
    ) -> None:
        """Clean a list of rooms one at a time, waiting for docking between each.

        Schedule it with async_start_sequential(), which claims the task slot.
        """
        if not room_names:
            return
        current = asyncio.current_task()
        if self._cleaning_task not in (None, current) and not self._cleaning_task.done():
            _LOGGER.warning("[%s] Sequential clean not started: another sequence is running",
                            self.serial)
            return
        if generation is None:
            generation = self._stop_generation
        reason = start_block_reason(self.mqtt.state if self.mqtt else {}, bool(self.mqtt and self.mqtt.available))
        if reason:
            _LOGGER.warning("Sequential clean blocked: %s", reason)
            if self._cleaning_task is current:
                self._cleaning_task = None
            return
        # P1 #2: record this task so stop/return_to_base can cancel it
        self._cleaning_task = current
        try:
            _LOGGER.info("[%s] Sequential clean starting — rooms: %s", self.serial, room_names)
            for room in room_names:
                if generation != self._stop_generation:
                    _LOGGER.info("[%s] Sequential clean stopped by Stop/Home before %r",
                                 self.serial, room)
                    return
                if not self.mqtt or not self.mqtt.available:
                    _LOGGER.warning("[%s] robot not reachable — aborting sequential clean", self.serial)
                    return
                _LOGGER.info("[%s] Sequential clean → '%s'", self.serial, room)
                mode_int = MODE_TO_INT.get(self.current_mode)
                started = await self.hass.async_add_executor_job(
                    self.mqtt.start_room, room, mode_int,
                )
                if not started:
                    _LOGGER.warning(
                        "[%s] Sequential clean stopped: could not start %r", self.serial, room,
                    )
                    return
                completed = await self._async_wait_for_clean_complete()
                if completed == "aborted":
                    _LOGGER.info(
                        "[%s] Sequential clean ended: %r was stopped outside Home "
                        "Assistant (app or robot)", self.serial, room,
                    )
                    return
                if not completed:
                    _LOGGER.warning(
                        "[%s] Sequential clean aborted: timed out waiting for %r to dock",
                        self.serial, room,
                    )
                    return
            _LOGGER.info("[%s] Sequential clean finished all rooms", self.serial)
        finally:
            if self._cleaning_task is current:
                self._cleaning_task = None

    async def _async_wait_for_clean_complete(self, timeout: float = 3600.0) -> bool | str:
        """Wait until the robot returns to dock after cleaning.

        Require both a completion state and new-job readiness. Charging alone
        can precede the end of station work. Drying after a completed job may
        be interrupted by the next room; mid-job charging remains blocked.

        Returns True when the room finished, "aborted" (truthy) when the clean
        was stopped - FULL_CLEAN_ABORTED, seen when the app or robot stops it
        (R2 capture, 8 Nov 2026) - and False on timeout.
        """
        done_event: asyncio.Event = asyncio.Event()
        cleaning_started = False
        aborted = False

        def _check() -> None:
            nonlocal cleaning_started, aborted
            if not self.mqtt:
                # Client torn down (shutdown/reconnect). Returning "complete"
                # lets the loop's own connected-check abort the sequence.
                done_event.set()
                return
            state = self.state  # snapshot — already on HA event loop
            if is_any_cleaning(state):
                cleaning_started = True
            elif state.get("state") == "FULL_CLEAN_ABORTED":
                # Stopped, not finished: end the sequence, don't start the next room.
                aborted = True
                done_event.set()
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
            return "aborted" if aborted else True
        except asyncio.TimeoutError:
            _LOGGER.warning("[%s] Timed out waiting for robot to dock", self.serial)
            return False
        finally:
            self.async_remove_listener(watcher)
