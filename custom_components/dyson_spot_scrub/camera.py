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
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.storage import Store

from .const import DOMAIN, CONF_SERIAL, CONF_DEVICE_NAME, CONF_PRODUCT_TYPE, CONF_AUTH_TOKEN
from .coordinator import DysonCoordinator
from .dyson_api import get_current_map, get_map_metadata, get_live_map, DysonApiError
from .dyson_mqtt import is_any_cleaning
from .map_renderer import render_map

_LOGGER = logging.getLogger(__name__)

# Frame interval in seconds
_CLEANING_INTERVAL = 5.0    # fast poll while the robot is active
_IDLE_INTERVAL     = 60.0   # slow poll when docked / idle / offline

# How long to keep the persistent map data cached before re-fetching (seconds)
_STATIC_CACHE_TTL  = 3600   # 1 hour

# HA storage for zone presentation persistence
_STORE_VERSION = 1


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
        self._token       = entry.data[CONF_AUTH_TOKEN]
        serial            = entry.data[CONF_SERIAL]
        self._serial      = serial

        self._attr_unique_id   = f"{serial}_floor_map"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            name=entry.data[CONF_DEVICE_NAME],
            manufacturer="Dyson",
            model=entry.data.get(CONF_PRODUCT_TYPE, "RB0S"),
            serial_number=serial,
        )

        # Persistent map cache
        self._map_data:    dict[str, Any] | None = None
        self._metadata:    list[dict[str, Any]] | None = None
        self._cache_ts:    float = 0.0          # epoch seconds of last fetch
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

    # ── HA lifecycle ──────────────────────────────────────────────────────────

    async def async_added_to_hass(self) -> None:
        """Called when the entity is added — load persisted presentation cache."""
        await super().async_added_to_hass()
        self._store = Store(self.hass, _STORE_VERSION, self._store_key)
        data = await self._store.async_load()
        if data and isinstance(data, dict):
            self._presentation_cache = data
            _LOGGER.debug(
                "[%s] Loaded presentation cache from storage: %d zone(s)",
                self._serial, len(data),
            )

    @property
    def available(self) -> bool:
        """Camera is always available; it shows the last known map on errors."""
        return True

    @property
    def frame_interval(self) -> float:
        """Seconds between automatic image refreshes."""
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
        # ── Refresh persistent map if cache is stale ──────────────────────────
        async with self._cache_lock:
            age = time.monotonic() - self._cache_ts
            if self._map_data is None or self._metadata is None or age > _STATIC_CACHE_TTL:
                await self._async_refresh_static_map()

        if self._map_data is None:
            # First fetch failed — return last image or None
            return self._last_image

        # ── Fetch live data if cleaning ───────────────────────────────────────
        live_data: dict[str, Any] | None = None
        if (
            self._coordinator.mqtt is not None
            and self._coordinator.mqtt.connected
            and is_any_cleaning(self._coordinator.mqtt.state)
        ):
            live_data = await self._async_fetch_live()

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
        try:
            image_bytes: bytes = await self.hass.async_add_executor_job(
                render_map,
                patched_map,
                self._metadata,
                live_data,
            )
            self._last_image = image_bytes
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
        try:
            _LOGGER.debug("[%s] Refreshing persistent map data", self._serial)
            _, map_data = await get_current_map(self._token, self._serial)
            metadata    = await get_map_metadata(self._token, self._serial)
            self._map_data  = map_data
            self._metadata  = metadata
            self._cache_ts  = time.monotonic()

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
            _LOGGER.warning(
                "[%s] Failed to fetch persistent map: %s — "
                "will retry on next camera poll",
                self._serial, exc,
            )
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.error(
                "[%s] Unexpected error fetching map: %s",
                self._serial, exc,
            )

    async def _async_fetch_live(self) -> dict[str, Any] | None:
        """Fetch the live cleaning map; returns None if unavailable/error."""
        try:
            return await get_live_map(self._token, self._serial)
        except DysonApiError:
            # Robot may have just finished cleaning; not an error
            return None
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.debug("[%s] Live map fetch failed: %s", self._serial, exc)
            return None
