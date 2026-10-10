"""Dyson cloud MQTT client — connection, commands, state parsing.

Connects to Dyson's AWS IoT Core broker over WebSocket (port 443) using a
custom authorizer.  No certificates required — authentication is via signed
token values from the IoT credentials endpoint.

WebSocket URL:
  wss://{endpoint}/mqtt
    ?x-amz-customauthorizer-name={authorizer_name}
    &token={token_value}
    &x-amz-customauthorizer-signature={url-encoded token_signature}

Four cleaning modes (via start_mode):
  0 = Vacuum only
  1 = Vacuum + Mop (simultaneous)
  2 = Mop only
  3 = Vacuum then Mop (sequential)
"""
from __future__ import annotations

import json
import logging
import math
import random
import re
import ssl
import threading
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import quote

import paho.mqtt.client as mqtt

_LOGGER = logging.getLogger(__name__)

_PREFERENCE_TIMEOUT = 10.0

_ROOM_SETTING_OPTIONS = {
    "cleaning_mode": (3, {
        "Vacuum": 0, "Vacuum and Mop": 1, "Mop": 2, "Vacuum then Mop": 3,
    }),
    "cleaning_strategy": (4, {"Auto": 0, "Boost": 1, "Quiet": 2, "Quick": 3}),
    "water_level": (5, {"Very Low": 99, "Low": 0, "Medium": 1, "High": 2}),
    "mop_repetitions": (6, {"One Pass": 0, "Two Passes": 1}),
}

# ── State classification ──────────────────────────────────────────────────────

RUNNING_STATES = {
    "FULL_CLEAN_RUNNING",
    "FULL_CLEAN_PAUSED",
    "FULL_CLEAN_DISCOVERING",
    "MAPPING_N_CLEANING",
    "ZONE_CLEANING_RUNNING",
    "SPOT_CLEANING_RUNNING",
}

CHARGING_STATES = {
    "CHARGING",
    "FULL_CLEAN_CHARGING",
    "INACTIVE_CHARGING",
}


def is_running(state: dict) -> bool:
    return state.get("state") in RUNNING_STATES


def is_vacuum_then_mop(state: dict) -> bool:
    """Vacuum-then-mop sequential.

    sweep_type 7 appears across all run types (confirmed Oct 2026, FutuRazor),
    so it is not a reliable discriminator.  This function currently returns False;
    both phases of a V-then-M run are covered by is_vacuuming_only / is_mopping.
    A better signal will be wired in once MQTT captures confirm it.
    """
    return False


def is_vacuuming_only(state: dict) -> bool:
    return is_running(state) and state.get("fullCleanAction") == "VACUUMING"


def is_vacuuming_and_mopping(state: dict) -> bool:
    return is_running(state) and state.get("fullCleanAction") == "VACUUMING_AND_MOPPING"


def is_mopping(state: dict) -> bool:
    return is_running(state) and state.get("fullCleanAction") == "MOPPING"


def is_any_cleaning(state: dict) -> bool:
    return (
        is_vacuuming_only(state)
        or is_vacuuming_and_mopping(state)
        or is_mopping(state)
        or is_vacuum_then_mop(state)
    )


def is_docked(state: dict) -> bool:
    return (
        state.get("state") in CHARGING_STATES
        or state.get("dockState") in {"DOCKED", "DRYING_MOP", "WASHING_MOP"}
    )


def is_charging(state: dict) -> bool:
    return state.get("state") in CHARGING_STATES and is_docked(state)


def battery_level(state: dict) -> int | None:
    b = state.get("batteryChargeLevel")
    if isinstance(b, (int, float)):
        return max(0, min(100, int(b)))
    return None


def has_fault(state: dict) -> bool:
    faults = state.get("activeFaults")
    if not isinstance(faults, list) or not faults:
        return False
    for f in faults:
        # The field name differs by message type:
        #   CURRENT-STATE uses "nextActionRequired"
        #   prop.post / older firmware use "status"
        severity = f.get("nextActionRequired") or f.get("status")
        if severity is not None and severity != "LOG_ONLY":
            return True
    return False


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rand_msg_id() -> str:
    return str(random.randint(0, 4294967295))


# ── Room name normalisation ───────────────────────────────────────────────────

def _room_display_name(raw_name: Any) -> str:
    """Normalise a room name from the robot's preference cache.

    The robot stores room names in three formats:
    - Plain string with wrong case:       "Living room"  → "Living Room"
    - JSON-encoded object:                '{"type":"dining","name":"Dining"}' → "Dining"
    - Plain string with trailing digit:   "Kitchen1"     → "Kitchen 1"

    Trailing digits are preserved with a space separator — they are the robot's
    internal disambiguation for rooms the user named identically (e.g. two rooms
    both called "Kitchen"). Stripping them would produce duplicate entity IDs.
    """
    s = str(raw_name) if not isinstance(raw_name, str) else raw_name
    # Try to parse JSON (handles '{"type":"dining","name":"Dining"}')
    try:
        parsed = json.loads(s)
        if isinstance(parsed, dict):
            s = parsed.get("name") or parsed.get("type") or s
    except (json.JSONDecodeError, TypeError):
        pass
    # Separate a trailing digit with a space ("Kitchen1" → "Kitchen 1")
    # so duplicate room names remain unique without being stripped entirely.
    s = re.sub(r"([A-Za-z])(\d+)$", r"\1 \2", s).strip()
    # Title-case ("Living room" → "Living Room")
    return s.title()


# ── MQTT client ───────────────────────────────────────────────────────────────


class DysonMqttClient:
    """Thread-safe paho-mqtt wrapper for the Dyson robot.

    Connects to Dyson's AWS IoT Core broker over WebSocket (port 443).
    Callbacks (on_state_change, on_connected, on_disconnected) are called
    from the paho network thread. Bridge to asyncio with
    hass.loop.call_soon_threadsafe() in the coordinator.
    """

    def __init__(
        self,
        serial: str,
        mqtt_prefix: str,
        endpoint: str,
        token_value: str,
        token_signature: str,
        client_id: str,
        authorizer_name: str,
        verbose: bool = False,
    ) -> None:
        self.serial            = serial
        self._prefix           = mqtt_prefix
        self._endpoint         = endpoint
        self._token_value      = token_value
        self._token_signature  = token_signature
        self._client_id        = client_id
        self._authorizer_name  = authorizer_name
        self._verbose          = verbose

        self._client:    mqtt.Client | None = None
        self._lock       = threading.Lock()
        self.connected   = False
        self.state: dict = {}

        self._cached_preference:    dict | None = None
        self._preferences_fetched              = False

        # Guard: only fire on_disconnected once per client instance.
        # paho's loop_start() may call _on_disconnect multiple times if it
        # tries to auto-reconnect (each failed attempt gets another callback).
        self._disconnected_handled: bool = False

        # Callbacks
        self.on_prefix_changed: Callable[[str], None] | None = None

        # Start calls run in HA's executor; MQTT replies arrive on paho's thread.
        self._start_lock = threading.Lock()
        self._preference_lock = threading.RLock()
        self._preference_ready = threading.Event()
        self._preference_request_id: str | None = None
        self._preference_response: dict | None = None
        self._start_cancelled = False

        # Registered callbacks — called on every state change
        self._state_callbacks: list[Callable[[dict], None]] = []

        # on_connected / on_disconnected hooks (called once each transition)
        self.on_connected:    Callable[[], None] | None = None
        self.on_disconnected: Callable[[], None] | None = None

    # ── Topics ────────────────────────────────────────────────────────────────

    @property
    def _command_topic(self) -> str:
        return f"{self._prefix}/{self.serial}/command"

    @property
    def _jdm_command_topic(self) -> str:
        return f"{self._prefix}/{self.serial}/command/jdm"

    @property
    def _wildcard_topic(self) -> str:
        return f"{self._prefix}/{self.serial}/#"

    # ── Public API ────────────────────────────────────────────────────────────

    def register_callback(self, cb: Callable[[dict], None]) -> None:
        self._state_callbacks.append(cb)

    def unregister_callback(self, cb: Callable[[dict], None]) -> None:
        self._state_callbacks = [c for c in self._state_callbacks if c is not cb]

    def connect(self) -> None:
        """Connect to Dyson's AWS IoT Core broker over WebSocket (blocking).

        Uses paho's websockets transport on port 443 with TLS.  Authentication
        is handled by AWS's custom authorizer — no client certificates needed.

        reconnect_on_failure=False: the coordinator manages all reconnects.
        paho's built-in retry would reuse the same ClientId and cause the broker
        to kick us off (rc=7), creating a rapid-fire disconnect cascade.
        """
        ws_path = (
            f"/mqtt"
            f"?x-amz-customauthorizer-name={self._authorizer_name}"
            f"&token={self._token_value}"
            f"&x-amz-customauthorizer-signature={quote(self._token_signature, safe='')}"
        )

        try:
            client = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
                client_id=self._client_id,
                transport="websockets",
                protocol=mqtt.MQTTv311,
                reconnect_on_failure=False,
            )
        except AttributeError:
            # paho-mqtt 1.x — no CallbackAPIVersion or reconnect_on_failure
            client = mqtt.Client(
                client_id=self._client_id,
                transport="websockets",
                protocol=mqtt.MQTTv311,
            )

        client.ws_set_options(path=ws_path)
        client.tls_set_context(ssl.create_default_context())
        client.on_connect    = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message    = self._on_message

        _LOGGER.debug(
            "[%s] Connecting to Dyson cloud MQTT at %s:443",
            self.serial, self._endpoint,
        )
        client.connect(self._endpoint, port=443, keepalive=30)

        with self._lock:
            self._client = client

        client.loop_start()

    def disconnect(self) -> None:
        self._cancel_pending_start()
        with self._lock:
            c = self._client
            self._client = None
        if c:
            c.loop_stop()
            c.disconnect()
        self.connected = False

    def request_current_state(self) -> None:
        self._publish({"msg": "REQUEST-CURRENT-STATE", "time": _now_iso()})

    def start_mode(self, mode: int) -> bool:
        """Start all rooms in mode 0-3, preserving their other settings."""
        return self._start_cleaning(mode)

    def _cancel_pending_start(self) -> None:
        """Wake a pending preference read without allowing a delayed start."""
        with self._preference_lock:
            self._start_cancelled = True
            self._preference_ready.set()

    def _abort_dock_action(self) -> None:
        """Send ABORT-DOCK-ACTION before every clean start.

        The Dyson app sends this command (with action=DRY_MOP) before starting
        any clean cycle.  It kills any active drying cycle so the robot does
        not try to do two things at once.  Confirmed from MQTT captures Oct 2026.
        """
        self._publish({
            "msg":         "ABORT-DOCK-ACTION",
            "action":      "DRY_MOP",
            "mode-reason": "RAPP",
            "time":        _now_iso(),
        })

    def stop(self) -> None:
        with self._preference_lock:
            self._cancel_pending_start()
            _LOGGER.info("[%s] → STOP", self.serial)
            self._publish({"msg": "STOP", "mode-reason": "RAPP", "time": _now_iso()})

    def return_to_base(self) -> None:
        with self._preference_lock:
            self._cancel_pending_start()
            _LOGGER.info("[%s] → RETURN_TO_BASE", self.serial)
            self._publish({"msg": "ABORT", "mode-reason": "RAPP", "time": _now_iso()})
            self._publish_jdm("service.start_recharge", {})

    # ── Room listing ──────────────────────────────────────────────────────────

    @property
    def room_names(self) -> list[str]:
        """Normalised display names of every room in the preference cache."""
        if not (self._cached_preference and self._cached_preference.get("room")):
            return []
        return [_room_display_name(r[1]) for r in self._cached_preference["room"]]

    @property
    def room_preferences(self) -> list[list]:
        """Live list of room-preference arrays from the robot's map cache.

        Each entry is a list with confirmed indices (FutuRazor MQTT captures, Oct 2026):
          [0]  roomId            int
          [1]  roomName          str (may be JSON-encoded; use _room_display_name)
          [2]  unknown           (preserved)
          [3]  cleaningMode      0=Vacuum  1=Vacuum+Mop  2=Mop  3=Vacuum then Mop
          [4]  cleaningStrategy  0=Auto  1=Boost  2=Quiet  3=Quick
          [5]  waterLevel        99=Very Low  0=Low  1=Medium  2=High
          [6]  mopRepetitions    0=One pass  1=Two passes
          [7]  unknown           (preserved)
          [8]  selected          0=No  1=Yes  (managed by switch.py)
          [9]  unknown           (preserved)
          [10] roomOrder         int
          [11] additional field  (preserved, newer firmware)

        Returns a direct reference into the cache — callers may modify a room
        entry in-place and then call set_room_preferences() to persist the change.
        """
        if self._cached_preference and isinstance(self._cached_preference.get("room"), list):
            return self._cached_preference["room"]
        return []

    def set_room_preferences(self, rooms: list[list]) -> bool:
        """Publish an updated room-preferences array to the robot.

        Used by per-room select entities to persist a single-setting change
        (cleaning mode, strategy, water level, or mop repetitions) without
        starting a clean cycle.  Safe to call from any thread; does not block.
        """
        try:
            map_id = int(self.state.get("persistentMapId", ""))
        except (TypeError, ValueError):
            _LOGGER.warning("[%s] set_room_preferences: no map ID available", self.serial)
            return False
        if not self.connected:
            _LOGGER.warning("[%s] set_room_preferences: not connected", self.serial)
            return False
        uv_switch: list = []
        if self._cached_preference:
            uv_switch = deepcopy(self._cached_preference.get("uv_switch", []))
        _LOGGER.debug(
            "[%s] Persisting room preferences (%d room(s))", self.serial, len(rooms)
        )
        self._publish_jdm("service.set_preference", {
            "map_id":          map_id,
            "prefer_type":     1,
            "room_preference": rooms,
            "uv_switch":       uv_switch,
        })
        if self._cached_preference is not None:
            self._cached_preference["room"] = deepcopy(rooms)
        return True

    def save_room_settings(self, room_settings: dict[str, dict[str, str]]) -> bool:
        """Apply a room-ID keyed draft and publish preferences once, without cleaning."""
        if not isinstance(room_settings, dict) or not room_settings:
            _LOGGER.warning("[%s] No room settings were supplied", self.serial)
            return False
        rooms = deepcopy(self.room_preferences)
        if not rooms:
            _LOGGER.warning("[%s] Cannot save room settings: preferences unavailable", self.serial)
            return False
        by_id = {str(room[0]): room for room in rooms if isinstance(room, list) and room}
        for room_id, settings in room_settings.items():
            room = by_id.get(str(room_id))
            if room is None or len(room) < 7 or not isinstance(settings, dict) or not settings:
                _LOGGER.warning("[%s] Invalid room settings for room ID %r", self.serial, room_id)
                return False
            for key, option in settings.items():
                field = _ROOM_SETTING_OPTIONS.get(key)
                if field is None or not isinstance(option, str):
                    _LOGGER.warning("[%s] Invalid room setting %r=%r", self.serial, key, option)
                    return False
                index, option_values = field
                if option not in option_values:
                    _LOGGER.warning("[%s] Unsupported value for %s: %r", self.serial, key, option)
                    return False
                room[index] = option_values[option]
        return self.set_room_preferences(rooms)

    # ── Single-room clean ─────────────────────────────────────────────────────

    def start_room(self, room_name: str, mode: int = 0) -> bool:
        """Start one room, matching its normalised display name."""
        return self._start_cleaning(mode, room_name)

    def start_rooms_with_settings(
        self,
        room_ids: list[str | int],
        room_settings: dict[str, dict[str, str]] | None = None,
    ) -> bool:
        """Apply a card draft and pass the selected room IDs to the robot in order."""
        if not room_ids:
            _LOGGER.warning("[%s] Cannot start: no room IDs supplied", self.serial)
            return False
        return self._start_cleaning(
            None, room_ids=room_ids, room_settings=room_settings or {}
        )

    def _start_cleaning(
        self,
        mode: int | None,
        room_name: str | None = None,
        *,
        room_ids: list[str | int] | None = None,
        room_settings: dict[str, dict[str, str]] | None = None,
    ) -> bool:
        """Read current preferences before editing mode and room selection.

        Called from an executor, never the MQTT callback or HA event-loop thread.
        Keep the existing four-command start sequence; only its room payload is
        corrected here. A failed refresh must not fall back to stale preferences.
        """
        if mode is not None and mode not in (0, 1, 2, 3):
            _LOGGER.warning("[%s] Cannot start: invalid cleaning mode %r", self.serial, mode)
            return False
        if room_ids is not None and len({str(room_id) for room_id in room_ids}) != len(room_ids):
            _LOGGER.warning("[%s] Cannot start: duplicate room IDs", self.serial)
            return False
        if room_settings is not None and not isinstance(room_settings, dict):
            _LOGGER.warning("[%s] Cannot start: room settings must be an object", self.serial)
            return False
        if not self._start_lock.acquire(blocking=False):
            _LOGGER.warning("[%s] Cannot start: another start is pending", self.serial)
            return False
        try:
            with self._preference_lock:
                self._start_cancelled = False
                if not self.connected:
                    _LOGGER.warning("[%s] Cannot start: MQTT disconnected", self.serial)
                    return False
                try:
                    map_id = int(self.state["persistentMapId"])
                except (KeyError, TypeError, ValueError):
                    _LOGGER.warning("[%s] Cannot start: current map unavailable", self.serial)
                    return False
                request_id = _rand_msg_id()
                self._preference_request_id = request_id
                self._preference_response = None
                self._preference_ready.clear()
                self._publish_raw(self._jdm_command_topic, {
                    "msgId": request_id,
                    "version": "1.0.1",
                    "method": "service.get_preference",
                    "params": {"map_id": map_id},
                    "time": _now_iso(),
                })

            received = self._preference_ready.wait(_PREFERENCE_TIMEOUT)
            with self._preference_lock:
                if self._start_cancelled or not self.connected:
                    _LOGGER.info("[%s] Pending cleaning start cancelled", self.serial)
                    return False
                if str(self.state.get("persistentMapId")) != str(map_id):
                    _LOGGER.warning("[%s] Cannot start: current map changed", self.serial)
                    return False
                pref = self._preference_response
                if not received or pref is None:
                    _LOGGER.warning(
                        "[%s] Cannot start: no successful room-preference response "
                        "within %.0f s; existing settings were not overwritten",
                        self.serial, _PREFERENCE_TIMEOUT,
                    )
                    return False
                rooms = pref.get("room")
                if not isinstance(rooms, list) or not rooms or any(
                    not isinstance(room, list) or len(room) < 11
                    or not isinstance(room[0], (int, str)) for room in rooms
                ):
                    _LOGGER.warning("[%s] Cannot start: incomplete room preferences", self.serial)
                    return False
                if room_ids is not None:
                    by_id = {str(room[0]): room for room in rooms}
                    requested = [str(room_id) for room_id in room_ids]
                    if any(room_id not in by_id for room_id in requested):
                        _LOGGER.warning("[%s] Cannot start: a requested room ID is missing", self.serial)
                        return False
                    selected = [by_id[room_id] for room_id in requested]
                elif room_name is None:
                    selected = rooms
                else:
                    selected = [room for room in rooms if
                                _room_display_name(room[1]).casefold() == room_name.strip().casefold()]
                    if len(selected) != 1:
                        _LOGGER.warning(
                            "[%s] Cannot start: room %r is missing or ambiguous",
                            self.serial, room_name,
                        )
                        return False
                selected_room_ids = [room[0] for room in selected]
                if len({str(room[0]) for room in rooms}) != len(rooms):
                    _LOGGER.warning("[%s] Cannot start: duplicate room IDs", self.serial)
                    return False

                # App captures (Oct 2026): [3] mode, [4] strategy, [5] water,
                # [6] mop passes, [8] selected. Preserve names, order, unknown
                # fields and any trailing fields returned by get_preference.
                updated_rooms = deepcopy(rooms)
                settings_by_id = room_settings or {}
                for room in updated_rooms:
                    enabled = str(room[0]) in {str(room_id) for room_id in selected_room_ids}
                    room[8] = int(enabled)
                    if enabled and mode is not None:
                        room[3] = mode
                    settings = settings_by_id.get(str(room[0]), {})
                    if not isinstance(settings, dict):
                        _LOGGER.warning("[%s] Invalid room settings for room ID %r", self.serial, room[0])
                        return False
                    for key, option in settings.items():
                        field = _ROOM_SETTING_OPTIONS.get(key)
                        if field is None or not isinstance(option, str):
                            _LOGGER.warning("[%s] Invalid room setting %r=%r", self.serial, key, option)
                            return False
                        index, option_values = field
                        if option not in option_values:
                            _LOGGER.warning("[%s] Unsupported value for %s: %r", self.serial, key, option)
                            return False
                        room[index] = option_values[option]
                if set(settings_by_id) - {str(room[0]) for room in updated_rooms}:
                    _LOGGER.warning("[%s] Room settings refer to a missing room ID", self.serial)
                    return False

                ordered_room_ids = selected_room_ids

                _LOGGER.info(
                    "[%s] Starting cleaning in rooms %s with refreshed preferences",
                    self.serial, ordered_room_ids,
                )
                if room_ids is not None and self._cached_preference is not None:
                    self._cached_preference["room"] = deepcopy(updated_rooms)
                self._publish_jdm("service.set_preference", {
                    "map_id": map_id,
                    "prefer_type": 1,
                    "room_preference": updated_rooms,
                    "uv_switch": deepcopy(pref.get("uv_switch", [])),
                })
                self._abort_dock_action()
                self._publish({
                    "msg": "START",
                    "mode-reason": "RAPP",
                    "cleaningMode": "zoneConfigured",
                    "cleaningProgramme": {
                        "persistentMapId": str(map_id),
                        "unorderedZones": [str(room_id) for room_id in ordered_room_ids],
                    },
                    "time": _now_iso(),
                })
                self._publish_jdm("service.set_cur_map", {"map_id": map_id})
                self._publish_jdm("service.set_room_clean", {
                    "ctrl_value": 1, "clean_type": 0, "room_ids": ordered_room_ids,
                })
                return True
        finally:
            with self._preference_lock:
                self._preference_request_id = None
                self._preference_response = None
            self._start_lock.release()

    # ── paho callbacks ────────────────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, rc) -> None:
        if rc != 0:
            _LOGGER.error("[%s] MQTT connect failed, rc=%s", self.serial, rc)
            return
        _LOGGER.info(
            "[%s] MQTT connected — endpoint=%s prefix=%s",
            self.serial, self._endpoint, self._prefix,
        )
        self.connected = True
        self._preferences_fetched = False  # Allow re-fetch on reconnect
        # Single wildcard subscription covers every prefix (RB05, NROB, …).
        client.subscribe(f"+/{self.serial}/#", qos=0)
        self.request_current_state()
        # Probe map IDs 0-3 at staggered intervals so rooms populate even
        # when the robot omits persistentMapId from its idle CURRENT-STATE.
        for _mid in range(4):
            delay = _mid * 2.5  # 0 s, 2.5 s, 5 s, 7.5 s
            if delay == 0:
                self._probe_map_id(0)
            else:
                threading.Timer(delay, self._probe_map_id, args=(_mid,)).start()
        if self.on_connected:
            self.on_connected()

    def _on_disconnect(self, client, userdata, rc) -> None:
        if self._disconnected_handled:
            _LOGGER.debug(
                "[%s] Ignoring repeated disconnect callback (rc=%s)", self.serial, rc
            )
            return
        self._disconnected_handled = True
        _LOGGER.warning("[%s] MQTT disconnected (rc=%s)", self.serial, rc)
        self.connected = False
        self._cancel_pending_start()
        if self.on_disconnected:
            self.on_disconnected()

    def _on_message(self, client, userdata, msg) -> None:
        try:
            data = json.loads(msg.payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        if not isinstance(data, dict):
            _LOGGER.debug(
                "[%s] Ignoring non-object MQTT payload on %s: %r",
                self.serial, msg.topic, type(data).__name__,
            )
            return

        topic = msg.topic

        # Auto-detect the real MQTT prefix from inbound robot messages.
        parts = topic.split("/")
        if (
            len(parts) >= 2
            and parts[1] == self.serial
            and parts[0] != self._prefix
        ):
            _LOGGER.warning(
                "[%s] Real MQTT prefix is '%s' (was '%s') — switching now",
                self.serial, parts[0], self._prefix,
            )
            self._prefix = parts[0]
            if self.on_prefix_changed:
                self.on_prefix_changed(parts[0])
            self.request_current_state()
            self._preferences_fetched = False
            for _mid in range(4):
                delay = _mid * 2.5
                if delay == 0:
                    self._probe_map_id(0)
                else:
                    threading.Timer(delay, self._probe_map_id, args=(_mid,)).start()

        _LOGGER.debug("[%s] ← %s  %s", self.serial, topic, json.dumps(data))

        if topic.endswith("/status/jdm"):
            self._handle_jdm(data)
        elif topic.endswith("/status") or topic.endswith("/status/current"):
            self._handle_status(data)

    # ── Message handlers ──────────────────────────────────────────────────────

    def _handle_jdm(self, data: dict) -> None:
        method = data.get("method")

        if method == "prop.post" and data.get("params"):
            self._merge_jdm_props(data["params"])
        elif method == "prop.get" and data.get("data"):
            self._merge_jdm_props(data["data"])
        elif method == "service.get_preference":
            pref = data.get("data")
            success = data.get("code") == 0 and isinstance(pref, dict)
            with self._preference_lock:
                if (self._preference_request_id is not None
                        and str(data.get("msgId")) == self._preference_request_id):
                    self._preference_response = deepcopy(pref) if success else None
                    self._preference_ready.set()
            if not success or not isinstance(pref.get("room"), list):
                return
            n = len(pref.get("room", []))
            if n == 0:
                _LOGGER.debug("[%s] get_preference: 0 rooms (map probe)", self.serial)
                return
            first_cache = self._cached_preference is None
            self._cached_preference = pref
            if first_cache:
                _LOGGER.info("[%s] Room preferences cached (%d room(s))", self.serial, n)
                self._notify_state_change()
            else:
                _LOGGER.debug("[%s] Room preferences refreshed (%d room(s))", self.serial, n)

    def _handle_status(self, data: dict) -> None:
        msg_type = data.get("msg") or data.get("method")
        if msg_type in {"CURRENT-STATE", "STATE-CHANGE", "PRODUCT_INFO", "INITIAL_STATE"}:
            self.state = {**self.state, **data}
            self._notify_state_change()

            if not self._preferences_fetched and self.state.get("persistentMapId"):
                self._preferences_fetched = True
                self._fetch_room_preferences()

        elif "faultId" in data:
            _LOGGER.warning(
                "[%s] Fault %s — status: %s", self.serial,
                data.get("faultId"), data.get("status")
            )

    def _merge_jdm_props(self, props: dict) -> None:
        updates: dict = {}
        if "sweep_type" in props:
            updates["sweepType"] = props["sweep_type"]
        if "work_mode" in props:
            updates["workMode"] = props["work_mode"]
        if "status" in props:
            updates["jdmStatus"] = props["status"]
        if "batteryChargeLevel" in props:
            updates["batteryChargeLevel"] = props["batteryChargeLevel"]
        if updates:
            self.state = {**self.state, **updates}
            self._notify_state_change()

    def _notify_state_change(self) -> None:
        for cb in list(self._state_callbacks):
            try:
                cb(self.state)
            except Exception:
                _LOGGER.exception("[%s] Error in state callback", self.serial)

    def _probe_map_id(self, map_id: int) -> None:
        """Send service.get_preference for one map_id — skip if already have rooms."""
        if self._cached_preference and self._cached_preference.get("room"):
            return
        _LOGGER.debug("[%s] Probing map_id=%d for room preferences", self.serial, map_id)
        self._publish_raw(self._jdm_command_topic, {
            "msgId":   _rand_msg_id(),
            "version": "1.0.1",
            "method":  "service.get_preference",
            "params":  {"map_id": map_id},
            "time":    _now_iso(),
        })

    def _fetch_room_preferences(self) -> None:
        map_id = self.state.get("persistentMapId")
        try:
            map_id_int = int(map_id)
        except (TypeError, ValueError):
            return
        _LOGGER.debug("[%s] Fetching room preferences for map %s", self.serial, map_id_int)
        self._probe_map_id(map_id_int)

    # ── Internal publish ──────────────────────────────────────────────────────

    def _publish_jdm(self, method: str, params: dict) -> None:
        self._publish_raw(self._jdm_command_topic, {
            "msgId":   _rand_msg_id(),
            "version": "1.0.1",
            "method":  method,
            "params":  params,
            "time":    _now_iso(),
        })

    def _publish(self, payload: dict) -> None:
        self._publish_raw(self._command_topic, payload)

    def _publish_raw(self, topic: str, payload: dict) -> None:
        with self._lock:
            client = self._client
        if not client or not self.connected:
            _LOGGER.warning("[%s] Cannot publish — not connected", self.serial)
            return
        body = json.dumps(payload)
        if self._verbose:
            _LOGGER.debug("[MQTT out] %s: %s", topic, body)
        client.publish(topic, body, qos=0)
