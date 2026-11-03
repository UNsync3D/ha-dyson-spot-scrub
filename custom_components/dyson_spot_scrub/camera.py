"""Camera entity — renders the Dyson floor map as a live PNG image.

The entity polls the Dyson REST API for map data and produces a PNG
using map_renderer.py (Pillow).  Poll rate adapts to robot state:
  • Cleaning  → every 5 seconds  (live robot position + clean path)
  • Otherwise → every 60 seconds (static map refresh)

The persistent map data (zone boundaries, furniture, etc.) is cached in
memory and refreshed every STATIC_CACHE_TTL seconds so the API isn't
hammered on every fast-poll tick.

Zone presentation data (perimeter segments) is persisted to HA storage
so room outlines survive restarts without needing a new cleaning run.

The last cleaning image is kept in RAM during pauses and mode transitions,
and for five minutes after completion or cancellation. A new cleaning clears it.
"""
from __future__ import annotations

import asyncio
import logging
import time
from copy import deepcopy
from functools import partial
from typing import Any

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.storage import Store

from .const import DOMAIN, CONF_SERIAL, CONF_AUTH_TOKEN
from .coordinator import DysonCoordinator
from .dyson_api import get_current_map, get_map_metadata, DysonApiError
from .dyson_mqtt import RUNNING_STATES, is_any_cleaning
from .entity_helpers import device_info
from .map_renderer import render_map
from .map_options import map_options
from .map_path import PathHistory

_LOGGER = logging.getLogger(__name__)

# Frame interval in seconds
_CLEANING_INTERVAL = 5.0    # fast poll while the robot is active
_IDLE_INTERVAL     = 60.0   # slow poll when docked / idle / offline

# How long to keep the persistent map data cached before re-fetching (seconds)
_STATIC_CACHE_TTL  = 3600   # 1 hour

# After a static-map fetch fails, wait at least this long before retrying.
# Prevents a burst of camera polls (or multiple concurrent viewers) from
# each making their own API call when the server is returning 503/429.
_STATIC_CACHE_ERROR_BACKOFF = 30.0  # seconds

# HA storage for zone presentation persistence
_STORE_VERSION = 1

class _CleaningStateListener:
    """Observe coordinator notifications even when nobody is viewing the camera."""

    def __init__(self, camera: DysonMapCamera) -> None:
        self._camera = camera

    def async_write_ha_state(self) -> None:
        self._camera._observe_cleaning_state()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Dyson floor-map camera entity."""
    coordinator: DysonCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([DysonMapCamera(coordinator, entry)])


class DysonMapCamera(Camera):
    """Camera entity that renders the Dyson floor map as a PNG image.

    The image shows:
      • Room boundary polygons (coloured by clean status when cleaning)
      • Zone labels with area
      • Furniture outlines (user-taught vs auto-detected)
      • Keep-out zones
      • Dock location
      • Robot position + heading arrow (only while cleaning)
      • Live clean path  (only while cleaning)
      • Historical visited paths (from live-map zones)
    """

    _attr_has_entity_name = True
    _attr_name            = "Floor Map"
    _attr_icon            = "mdi:map"
    _attr_is_streaming    = False

    def __init__(self, coordinator: DysonCoordinator, entry: ConfigEntry) -> None:
        super().__init__()
        self._coordinator = coordinator
        self._config_entry = entry
        self._render_options = map_options(entry.options)
        self._token       = entry.data[CONF_AUTH_TOKEN]
        serial            = entry.data[CONF_SERIAL]
        self._serial      = serial

        self._attr_unique_id   = f"{serial}_floor_map"
        self._attr_device_info = device_info(entry)

        # Persistent map cache
        self._map_data:    dict[str, Any] | None = None
        self._metadata:    list[dict[str, Any]] | None = None
        self._cache_ts:    float = 0.0          # monotonic time of last successful fetch
        self._cache_error_ts: float = 0.0       # monotonic time of last FAILED fetch
        self._cache_lock:  asyncio.Lock = asyncio.Lock()

        # Zone presentation (perimeter segment) cache.
        # Dyson's persistent-map REST API returns zones with empty
        # presentation[] when the robot is docked — the segments are only
        # present in live_data during an active cleaning run.  We cache
        # the last known non-empty presentation per zone and persist it to
        # HA storage so room outlines survive restarts.
        self._presentation_cache: dict[str, list] = {}
        self._store: Store | None = None          # set in async_added_to_hass
        self._store_key = f"{DOMAIN}.presentations.{serial}"

        # Last rendered image (returned on error / while fetching)
        self._last_image:  bytes | None = None
        self._last_cleaning_image: bytes | None = None
        self._cleaning_active = False
        self._cleaning_finished_at: float | None = None
        self._cleaning_generation = 0
        self._map_id: str | None = None
        self._image_lock = asyncio.Lock()
        self._cleaning_listener = _CleaningStateListener(self)
        self._last_source: tuple | None = None
        self._last_image_options: dict | None = None
        self._path_history = PathHistory()
        self._observed_action: str | None = None
        self._action_changed_at = 0.0

    @property
    def _retention_seconds(self) -> float:
        """Keep-last overrides the timed policy; zero disables timed retention."""
        if self._render_options["map_keep_last"]:
            return float("inf")
        return self._render_options["map_retention_minutes"] * 60

    def _reset_cleaning_image(self, reason: str) -> None:
        self._cleaning_generation += 1
        self._last_cleaning_image = None
        self._last_image = None
        self._last_source = None
        self._last_image_options = None
        self._path_history = PathHistory()
        self._cleaning_finished_at = None
        _LOGGER.debug("[%s] Cleaning image reset: %s", self._serial, reason)

    def _observe_cleaning_state(self) -> None:
        mqtt = self._coordinator.mqtt
        if mqtt is None or not mqtt.connected:
            return  # A disconnect is not the end of a cleaning.
        state = dict(mqtt.state)
        action = state.get("fullCleanAction")
        if action != self._observed_action:
            self._observed_action = action
            self._action_changed_at = time.monotonic()
        map_id = state.get("persistentMapId")
        if map_id is not None:
            map_id = str(map_id)
            if self._map_id is not None and map_id != self._map_id:
                self._reset_cleaning_image("map changed")
                self._cleaning_active = False
                self._map_data = None
                self._metadata = None
                self._presentation_cache = {}
                self._coordinator.async_clear_live_map_cache()
            self._map_id = map_id

        status = state.get("state")
        if status in ("FULL_CLEAN_FINISHED", "ABORTED"):
            if self._cleaning_active:
                self._cleaning_active = False
                self._cleaning_finished_at = time.monotonic()
                _LOGGER.debug(
                    "[%s] Cleaning image retention started: %s, duration=%s s",
                    self._serial, status, self._retention_seconds,
                )
        elif status in RUNNING_STATES and status != "FULL_CLEAN_PAUSED":
            if not self._cleaning_active:
                self._reset_cleaning_image("new cleaning")
                self._coordinator.async_clear_live_map_cache()
                self._cleaning_active = True

    def _fetch_live_map(self) -> bool:
        mqtt = self._coordinator.mqtt
        if mqtt is None or not mqtt.connected:
            return False
        state = dict(mqtt.state)
        return (
            is_any_cleaning(state)
            and state.get("state") != "FULL_CLEAN_PAUSED"
            and state.get("fullCleanAction") != "NONE"
        )

    def _retained_image(self) -> bytes | None:
        if self._cleaning_finished_at is not None:
            if time.monotonic() - self._cleaning_finished_at >= self._retention_seconds:
                self._reset_cleaning_image("retention expired")
                return None
            return self._last_cleaning_image
        if self._cleaning_active and not self._fetch_live_map():
            # Pausing, mop washing, mode transitions and disconnects do not
            # complete a job. Do not start the retention timer in these states.
            return self._last_cleaning_image
        return None

    # ── HA lifecycle ──────────────────────────────────────────────────────────

    async def async_added_to_hass(self) -> None:
        """Called when the entity is added — load persisted presentation cache."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._config_entry.add_update_listener(self._async_options_updated)
        )
        self._coordinator.async_add_listener(self._cleaning_listener)
        self.async_on_remove(
            lambda: self._coordinator.async_remove_listener(self._cleaning_listener)
        )
        self._observe_cleaning_state()
        self._store = Store(self.hass, _STORE_VERSION, self._store_key)
        try:
            data = await self._store.async_load()
        except Exception:  # noqa: BLE001  # e.g. UnsupportedStorageVersionError
            _LOGGER.warning(
                "[%s] Presentation cache storage is incompatible (version mismatch) "
                "— starting with an empty cache; delete "
                "/config/.storage/%s to suppress this warning. "
                "The cache will be rebuilt on the next cleaning run.",
                self._serial, self._store_key,
            )
            data = None
        if data and isinstance(data, dict):
            self._presentation_cache = data
            _LOGGER.debug(
                "[%s] Loaded presentation cache from storage: %d zone(s)",
                self._serial, len(data),
            )

    async def async_will_remove_from_hass(self) -> None:
        self._reset_cleaning_image("entity unloaded")
        self._cleaning_active = False
        await super().async_will_remove_from_hass()

    async def _async_options_updated(self, hass, entry) -> None:
        """Apply options without reloading MQTT or losing the retained source."""
        self._render_options = map_options(entry.options)
        self.async_write_ha_state()

    async def _render_source(self, source: tuple) -> tuple[bytes, dict]:
        """Render a stable option snapshot; changes during rendering retry locally."""
        while True:
            options = self._render_options
            image = await self.hass.async_add_executor_job(
                partial(render_map, raise_errors=True), *source, options
            )
            if options == self._render_options:
                return image, options

    async def _repaint_cached_image(self) -> None:
        """Repaint a retained/error-fallback frame without a cloud request.

        Called under the image lock. Session generation prevents a late repaint
        from restoring an image after a new cleaning, map change or expiry.
        """
        source = self._last_source
        if source is None or self._last_image_options == self._render_options:
            return
        generation = self._cleaning_generation
        cleaning_image = self._last_cleaning_image
        try:
            image, options = await self._render_source(source)
        except Exception as exc:
            _LOGGER.warning("[%s] Map options repaint failed: %s", self._serial, exc)
            return
        self._observe_cleaning_state()
        self._retained_image()  # Expire before accepting an in-flight repaint.
        if generation != self._cleaning_generation:
            return
        self._last_image, self._last_image_options = image, options
        if cleaning_image is not None:
            self._last_cleaning_image = image

    @property
    def available(self) -> bool:
        """Camera is always available; it shows the last known map on errors."""
        return True

    @property
    def frame_interval(self) -> float:
        """Seconds between automatic image refreshes."""
        if self._cleaning_active or self._cleaning_finished_at is not None:
            return _CLEANING_INTERVAL
        if (
            self._coordinator.mqtt is not None
            and self._coordinator.mqtt.connected
            and is_any_cleaning(self._coordinator.mqtt.state)
        ):
            return _CLEANING_INTERVAL
        return _IDLE_INTERVAL

    # ── Image generation ──────────────────────────────────────────────────────

    async def async_camera_image(
        self,
        width:  int | None = None,
        height: int | None = None,
    ) -> bytes | None:
        """Fetch map data and render a PNG; return cached image on errors."""
        async with self._image_lock:
            return await self._async_camera_image()

    async def _async_camera_image(self) -> bytes | None:
        self._observe_cleaning_state()
        self._retained_image()
        await self._repaint_cached_image()
        if (retained := self._retained_image()) is not None:
            return retained
        generation = self._cleaning_generation
        # ── Refresh persistent map if cache is stale ──────────────────────────
        async with self._cache_lock:
            age = time.monotonic() - self._cache_ts
            needs_fetch = (
                self._map_data is None
                or self._metadata is None
                or age > _STATIC_CACHE_TTL
            )
            # After a failed fetch, skip retries within the backoff window so
            # a burst of concurrent camera polls doesn't each fire an API call.
            backoff_active = (
                (self._map_data is None or self._metadata is None)
                and time.monotonic() - self._cache_error_ts < _STATIC_CACHE_ERROR_BACKOFF
            )
            if needs_fetch and not backoff_active:
                await self._async_refresh_static_map()

        self._observe_cleaning_state()
        if generation != self._cleaning_generation:
            return self._retained_image() or self._last_image

        if self._map_data is None:
            # First fetch failed — return last image or None
            return self._last_image

        # ── Fetch live data if cleaning ───────────────────────────────────────
        live_data: dict[str, Any] | None = None
        cleaning = self._fetch_live_map()
        action = self._observed_action
        if cleaning:
            live_data = await self._coordinator.async_get_live_map()

        self._observe_cleaning_state()
        retained = self._retained_image()
        if generation != self._cleaning_generation or retained is not None:
            return retained or self._last_image

        # A temporary empty live snapshot must not erase an already drawn path,
        # particularly while switching from vacuuming to mopping.
        has_path = bool(live_data and len(live_data.get("cleanPath") or []) >= 2)
        if cleaning and not has_path and self._last_cleaning_image is not None:
            return self._last_cleaning_image

        # If cleaning but the fetch failed (rate-limited or transient error),
        # return the last known good frame rather than rendering a pathless image.
        if cleaning and live_data is None and self._last_image is not None:
            return self._last_image

        if has_path:
            # A cached snapshot predating the action change, or a response
            # spanning a change, must not be assigned the new action blindly.
            if (action != self._observed_action
                    or self._coordinator._live_map_fetched_at < self._action_changed_at):
                action = None
            live_data = self._path_history.annotate(live_data, action)

        # ── Update presentation cache from fresh zone data ────────────────────
        # Prefer live_data (most up-to-date), fall back to static map.
        cache_updated = False
        for src in (live_data, self._map_data):
            if src:
                for z in src.get("zones", []):
                    zid = str(z.get("id", ""))
                    if zid and z.get("presentation"):
                        if self._presentation_cache.get(zid) != z["presentation"]:
                            self._presentation_cache[zid] = z["presentation"]
                            cache_updated = True

        # Persist whenever something new was learned
        if cache_updated and self._store is not None:
            await self._store.async_save(self._presentation_cache)
            _LOGGER.debug(
                "[%s] Saved presentation cache: %d zone(s)",
                self._serial, len(self._presentation_cache),
            )

        # ── Inject cached presentations into map_data for the renderer ────────
        static_zone_ids = [str(z.get("id", "")) for z in self._map_data.get("zones", [])]
        _LOGGER.debug(
            "[%s] Inject check — cache keys: %s | static zone ids: %s",
            self._serial,
            sorted(self._presentation_cache.keys()),
            static_zone_ids,
        )
        patched_map = self._map_data
        if self._presentation_cache:
            patched_zones = []
            for z in self._map_data.get("zones", []):
                zid = str(z.get("id", ""))
                has_presentation = bool(z.get("presentation"))
                in_cache = zid in self._presentation_cache
                if not has_presentation and in_cache:
                    z = dict(z)
                    z["presentation"] = self._presentation_cache[zid]
                    _LOGGER.debug("[%s] Injected presentation for zone %s", self._serial, zid)
                elif not has_presentation and not in_cache:
                    _LOGGER.debug("[%s] Zone %s has no presentation and not in cache", self._serial, zid)
                patched_zones.append(z)
            if patched_zones:
                patched_map = dict(self._map_data)
                patched_map["zones"] = patched_zones

        # ── Render in executor (CPU-bound) ────────────────────────────────────
        self._observe_cleaning_state()
        retained = self._retained_image()
        if generation != self._cleaning_generation or retained is not None:
            return retained or self._last_image
        try:
            source = deepcopy((patched_map, self._metadata, live_data))
            image_bytes, options = await self._render_source(source)
            self._observe_cleaning_state()
            retained = self._retained_image()
            if generation != self._cleaning_generation or retained is not None:
                return retained or self._last_image
            self._last_image = image_bytes
            self._last_source = source
            self._last_image_options = options
            if has_path and image_bytes:
                self._last_cleaning_image = image_bytes
            return image_bytes
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.error("[%s] Map render failed: %s", self._serial, exc)
            return self._last_image

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _async_refresh_static_map(self) -> None:
        """Fetch (or re-fetch) the persistent map and metadata from Dyson's API.

        Must be called while ``_cache_lock`` is held.
        Logs warnings and leaves cached data unchanged on errors.
        """
        generation = self._cleaning_generation
        try:
            _LOGGER.debug("[%s] Refreshing persistent map data", self._serial)
            session = self._coordinator.http_session
            _, map_data = await get_current_map(self._token, self._serial, session=session)
            metadata    = await get_map_metadata(self._token, self._serial, session=session)
            self._observe_cleaning_state()
            if generation != self._cleaning_generation:
                return
            self._map_data  = map_data
            self._metadata  = metadata
            self._cache_ts  = time.monotonic()
            # Same metadata is the authoritative room list for the MQTT client.
            apply_inventory = getattr(self._coordinator, "apply_map_inventory", None)
            if apply_inventory:
                apply_inventory(metadata)

            _LOGGER.debug(
                "[%s] map_data top-level keys: %s",
                self._serial, list(map_data.keys()),
            )
            zones = map_data.get("zones") or []
            if zones:
                _LOGGER.debug(
                    "[%s] map_data zones[0] keys: %s; presentation length: %d",
                    self._serial,
                    list(zones[0].keys()),
                    len(zones[0].get("presentation") or []),
                )
            _LOGGER.debug("[%s] Persistent map cached successfully", self._serial)
        except DysonApiError as exc:
            self._cache_error_ts = time.monotonic()
            _LOGGER.warning(
                "[%s] Failed to fetch persistent map: %s — "
                "will back off %.0f s before retrying",
                self._serial, exc, _STATIC_CACHE_ERROR_BACKOFF,
            )
        except Exception as exc:  # pylint: disable=broad-except
            self._cache_error_ts = time.monotonic()
            _LOGGER.error(
                "[%s] Unexpected error fetching map: %s",
                self._serial, exc,
            )

