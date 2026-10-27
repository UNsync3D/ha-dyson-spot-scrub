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
import random
import re
import ssl
import threading
import time
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import quote

import paho.mqtt.client as mqtt

from .const import DOCK_BUSY_STATES, FAULT_CODE_INFORMATIONAL, MQTT_PREFIX_PATTERN
from .station_settings import STATE_FIELDS, current_setting, setting_payloads, reported_properties

_LOGGER = logging.getLogger(__name__)

# Seconds to wait for the robot's service.get_preference reply before a start.
_PREFERENCE_TIMEOUT = 10.0
# Map IDs probed on connect (and their stagger) before persistentMapId is known.
_PROBE_MAP_IDS = range(4)
_PROBE_INTERVAL = 2.5
# How many outstanding get_preference msgId -> map_id mappings to remember.
_MAX_TRACKED_REQUESTS = 64
# A room-preference row must reach index [10] (roomOrder) to be usable.
_MIN_ROOM_ROW_LEN = 11


def _valid_room_row(row) -> bool:
    return (isinstance(row, list) and len(row) >= _MIN_ROOM_ROW_LEN
            and isinstance(row[0], (int, str)) and not isinstance(row[0], bool))
_DRYING_TIME_KEYS = ("drying_total_seconds", "drying_remaining_seconds", "drying_updated_at")

# Per-room setting name → (index in the room-preference array, label → raw value).
# Index meanings are documented on DysonMqttClient.room_preferences.
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


def start_block_reason(state: dict, connected: bool = True) -> str | None:
    """Classify new-job readiness without changing activity or robot state.

    October 18/21 captures show FINISHED/workMode=38 throughout the return
    and dock preparation, including the gap before station work starts.
    Inactive charging is ready only after blocking station work has ended.
    This does not govern resume, pause, Home or saving preferences.
    """
    if not connected:
        return "disconnected"
    if has_fault(state):
        return "robot_fault"
    robot = state.get("state", "")
    dock = state.get("dockState")
    if not isinstance(robot, str):
        return "waiting_for_ready_state"
    if dock == "WASHING_MOP":
        return "washing_mop"
    if dock == "COLLECTING_DUST":
        return "emptying_bin"
    if robot == "MAPPING" or robot.startswith("MAPPING_"):
        return "mapping_or_navigation"
    if robot in {"RETURNING_TO_BASE", "ABORTED", "FULL_CLEAN_ABORTED"}:
        return "returning_or_finishing"
    if robot == "FULL_CLEAN_FINISHED" or state.get("workMode") == 38:
        return "returning_or_finishing"
    if robot in RUNNING_STATES or robot.startswith("FULL_CLEAN_"):
        return "cleaning_job_active"
    # Do not require a previous station cycle: not every job performs one.
    if robot in {"INACTIVE_CHARGING", "INACTIVE_CHARGED", "INACTIVE_DISCHARGING", "CHARGING"}:
        if dock in {"IDLE", "DOCKED", "DRYING_MOP"}:
            return None
    return "waiting_for_ready_state"


def is_paused(state: dict) -> bool:
    """True when the robot is mid-clean but paused (FULL_CLEAN_PAUSED).

    FULL_CLEAN_PAUSED is included in RUNNING_STATES so that is_any_cleaning()
    continues to return True during a pause (the job is still in progress).
    This helper lets callers that need to distinguish paused from actively
    running do so without expanding the RUNNING_STATES check.
    """
    return state.get("state") == "FULL_CLEAN_PAUSED"


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


def has_freeze_fault(state: dict) -> bool:
    """Keep the legacy freeze heuristic, excluding confirmed pause signals.

    The 2026-10-13 capture reports work_mode=37, status=2 during a normal
    FULL_CLEAN_PAUSED session with fault=0 and activeFaults=[]. JDM status
    arrives before CURRENT-STATE, so either pause signal must suppress this
    heuristic. Explicit actionable faults are still checked by has_fault().
    """
    return (
        state.get("workMode") == 37
        and state.get("jdmStatus") != 2
        and not is_paused(state)
    )


def _fault_code(fault: dict) -> int | None:
    """Return a fault code from current or legacy Dyson payloads."""
    if not isinstance(fault, dict):
        return None
    raw_code = fault.get("faultCode")
    if raw_code is None:
        raw_code = fault.get("faultId")
    try:
        return int(raw_code) if raw_code is not None else None
    except (TypeError, ValueError):
        return None


def _actionable_faults(state: dict):
    """Yield the fault code (or None if it has none) of each actionable fault.

    Shared by has_fault() and active_fault_code() so both apply identical
    rules: skip LOG_ONLY faults and informational codes (dock baseline 2105,
    maintenance log 2103 — see FAULT_CODE_INFORMATIONAL).
    """
    faults = state.get("activeFaults")
    if not isinstance(faults, list):
        return
    for f in faults:
        if not isinstance(f, dict):
            continue
        # The severity field name differs by message type:
        #   CURRENT-STATE uses "nextActionRequired"
        #   prop.post / older firmware use "status"
        severity = f.get("nextActionRequired") or f.get("status")
        if severity is None or severity == "LOG_ONLY":
            continue
        fid = _fault_code(f)
        if fid is not None and fid in FAULT_CODE_INFORMATIONAL:
            continue
        yield fid


def has_fault(state: dict) -> bool:
    """True when there is an actionable (non-informational) fault.

    A fault with no parseable code still counts — it is actionable but unknown.
    """
    return has_freeze_fault(state) or any(True for _ in _actionable_faults(state))


def active_fault_code(state: dict) -> int | None:
    """Return the first actionable fault code from activeFaults, or None.

    Look the result up in FAULT_CODE_MESSAGES for a user-readable string.
    """
    return next((fid for fid in _actionable_faults(state) if fid is not None), None)


def dock_needs_maintenance(state: dict) -> bool:
    """True when fault code 2103 is present (dock self-clean finished, needs emptying)."""
    faults = state.get("activeFaults")
    if not isinstance(faults, list):
        return False
    return any(_fault_code(f) == 2103 for f in faults)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rand_msg_id() -> str:
    # Only a correlation ID for matching replies — not security sensitive.
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

        # ── Room preference store (see _cached_preference) ─────────────────
        # get_preference is NOT a complete room inventory: after a scheduled
        # start it can contain only the scheduled room (FutuRazor captures,
        # 26 Oct 2026), and a reply can be for a different map than the
        # current one (new-map capture, 25 Oct). So replies are MERGED per
        # map and room ID into this store and never remove rooms. Rooms are
        # removed only when the authoritative REST map metadata for that map
        # no longer lists them (set_map_inventory). All guarded by
        # _preference_lock (the paho thread writes, executor/loop read).
        self._room_store: dict[str, dict[str, list]] = {}    # map -> room id -> row
        self._uv_store: dict[str, dict[str, list]] = {}      # map -> room id -> [id, on]
        self._pref_extras: dict[str, dict] = {}              # map -> other reply keys
        self._map_inventory: dict[str, dict[str, str]] = {}  # map -> room id -> name
        # get_preference msgId -> map_id, for our requests and the app's
        # (seen on command/jdm), so each reply is filed under the right map.
        self._pref_request_maps: OrderedDict[str, str] = OrderedDict()
        self._prefs_map_id: str | None = None   # map whose prefs were last fetched

        # Room settings HA has sent but the robot hasn't yet reported back.
        # The cache above only ever holds what the robot reported; these let a
        # second quick edit build on the first instead of reverting it. See
        # room_preferences_for_edit().
        self._pending_rooms: list | None = None
        # Serialises each complete read -> merge -> publish of room settings,
        # so overlapping edits (two selects, a select and a card batch save,
        # automations) each build on the previous one instead of on the same
        # stale snapshot. Held only while preparing and publishing - never
        # while waiting for the robot's reply. Re-entrant because the edit
        # helpers call set_room_preferences() while holding it.
        self._room_edit_lock = threading.RLock()
        self._pending_refresh_id: str | None = None
        self._pending_since = 0.0

        # Guard: only fire on_disconnected once per client instance.
        # paho's loop_start() may call _on_disconnect multiple times if it
        # tries to auto-reconnect (each failed attempt gets another callback).
        self._disconnected_handled: bool = False

        # Callbacks
        self.on_prefix_changed: Callable[[str], None] | None = None

        # Start calls run in HA's executor; MQTT replies arrive on paho's thread.
        self._start_lock = threading.Lock()
        self._preference_lock = threading.RLock()
        # Called (paho thread) when the robot reports a different current map.
        self.on_map_changed: Callable[[str], None] | None = None
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
        # Every query value is URL-encoded: a token or authorizer name that
        # contains "&", "=" or "#" would otherwise corrupt the query string.
        # This path carries live credentials — never log it.
        ws_path = (
            f"/mqtt"
            f"?x-amz-customauthorizer-name={quote(self._authorizer_name, safe='')}"
            f"&token={quote(self._token_value, safe='')}"
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

    def set_station_setting(self, key: str, value) -> bool:
        """Send the captured app command pair; await telemetry before showing it.

        Publish success only means transport acceptance, not device confirmation.
        No automatic retries: a partial send may already have changed the robot.
        """
        state_fields, props = setting_payloads(key, value)
        if not self.connected or current_setting(self.state, key) is None:
            return False
        if current_setting(self.state, key) == value:
            return True
        if not self._publish({"msg": "STATE-SET", "mode-reason": "RAPP",
                              "time": _now_iso(), **state_fields}):
            return False
        accepted = self._publish_raw(self._jdm_command_topic, {
            "msgId": _rand_msg_id(), "version": "1.0.1",
            "method": "prop.set", "params": props,
        })
        self.request_current_state()
        return accepted

    def start_mode(self, mode: int) -> bool:
        """Start all rooms in mode 0-3, preserving their other settings."""
        return self._start_cleaning(mode)

    def _can_start_new_clean(self) -> bool:
        reason = start_block_reason(self.state, self.connected)
        if reason:
            _LOGGER.warning("[%s] New cleaning start blocked: %s", self.serial, reason)
        return reason is None

    def _cancel_pending_start(self) -> None:
        """Wake a pending preference read without allowing a delayed start."""
        with self._preference_lock:
            self._start_cancelled = True
            self._preference_ready.set()

    def pause(self) -> None:
        """Pause an active clean.

        Mirrors the Dyson app sequence (captured Oct 2026):
        1. PAUSE on the command channel
        2. service.set_room_clean with ctrl_value: 2 (pause signal) on JDM
        Any in-flight start is cancelled first so the preference lock is
        released and the PAUSE messages are sent immediately.
        """
        with self._preference_lock:
            self._cancel_pending_start()
            _LOGGER.info("[%s] → PAUSE", self.serial)
            self._publish({"msg": "PAUSE", "mode-reason": "RAPP", "time": _now_iso()})
            self._publish_jdm("service.set_room_clean", {
                "ctrl_value": 2, "clean_type": 0, "room_ids": [],
            })

    def resume(self) -> None:
        """Resume a paused clean.

        The Dyson app does NOT send a general START on resume — it sends
        only service.set_room_clean with ctrl_value: 1 and empty room_ids.
        Sending START would start a new job from scratch; this resumes the
        existing one.
        """
        _LOGGER.info("[%s] → RESUME", self.serial)
        self._publish_jdm("service.set_room_clean", {
            "ctrl_value": 1, "clean_type": 0, "room_ids": [],
        })

    def _abort_dock_action(self) -> bool:
        """Send ABORT-DOCK-ACTION before every clean start.

        The Dyson app sends this command (with action=DRY_MOP) before starting
        any clean cycle.  It kills any active drying cycle so the robot does
        not try to do two things at once.  Confirmed from MQTT captures Oct 2026.
        """
        return self._publish({
            "msg":         "ABORT-DOCK-ACTION",
            "action":      "DRY_MOP",
            "mode-reason": "RAPP",
            "time":        _now_iso(),
        })

    def cancel_drying(self) -> bool:
        """Cancel only roller drying; telemetry confirms completion."""
        if not self.connected or self.state.get("dockState") != "DRYING_MOP":
            return False
        return self._abort_dock_action()

    def empty_bin(self) -> bool:
        """Trigger the dock to collect dust from the robot's bin.

        legacy: START-DOCK-ACTION / action=COLLECT_DUST (confirmed, app captures)
        JDM:    service.start_station_act / station_act=3
        Returns False if not connected or a dock action is already running.
        """
        return self._start_dock_action("COLLECT_DUST", 3)

    def wash_mop(self) -> bool:
        """Trigger the dock to wash then air-dry the mop roller.

        legacy: START-DOCK-ACTION / action=WASH_MOP (confirmed, app captures)
        JDM:    service.start_station_act / station_act=1
        The dock auto-transitions to DRYING_MOP after washing (station_act=2)
        — no separate dry command is needed.
        Returns False if not connected or a dock action is already running.
        """
        return self._start_dock_action("WASH_MOP", 1)

    def _start_dock_action(self, action: str, station_act: int) -> bool:
        """Send one station action on both command channels, if the dock is free.

        OPEN QUESTION (FutuRazor testing, Oct 2026): HA-triggered actions show
        TWO service.start_station_act requests with different msgIds, while
        app-triggered actions show one. This method publishes the JDM call
        exactly once, so the extra request is probably generated upstream
        (robot/cloud translating the legacy START-DOCK-ACTION into JDM). Under
        investigation: compare a legacy-only publish against this pair before
        changing anything.
        """
        dock = self.state.get("dockState")
        if not self.connected:
            _LOGGER.warning("[%s] %s: not connected", self.serial, action)
            return False
        if dock in DOCK_BUSY_STATES:
            _LOGGER.warning("[%s] %s: dock already busy (%s)", self.serial, action, dock)
            return False
        _LOGGER.info("[%s] → %s (station_act=%d)", self.serial, action, station_act)
        self._publish({
            "msg":         "START-DOCK-ACTION",
            "action":      action,
            "mode-reason": "RAPP",
            "time":        _now_iso(),
        })
        self._publish_jdm("service.start_station_act", {
            "station_act": station_act,
            "ctrl_value":  1,
        })
        return True

    def _clear_drying_time(self) -> None:
        """Never carry a previous cycle's times into a new dock cycle."""
        self.state = {key: value for key, value in self.state.items()
                      if key not in _DRYING_TIME_KEYS}

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

    # ── Room preference store ─────────────────────────────────────────────────

    def _map_key(self, map_id=None) -> str:
        """Store key for a map ID; the current map if none is given."""
        if map_id is None:
            map_id = self.state.get("persistentMapId")
        return "?" if map_id in (None, "") else str(map_id)

    def _merge_preference(self, map_key: str, pref: dict) -> int:
        """Merge one get_preference payload into the store; returns rooms merged.

        Rooms absent from the payload are left untouched: a partial reply
        updates only the rooms it contains.
        """
        merged = 0
        with self._preference_lock:
            rooms = self._room_store.setdefault(map_key, {})
            uv = self._uv_store.setdefault(map_key, {})
            room_rows = pref.get("room")
            for row in room_rows if isinstance(room_rows, list) else []:
                # Malformed rows are never stored: they would later be sent
                # back to the robot as part of a full preference list.
                if _valid_room_row(row):
                    rooms[str(row[0])] = deepcopy(row)
                    merged += 1
            for pair in pref.get("uv_switch") or []:
                if isinstance(pair, list) and len(pair) >= 2:
                    uv[str(pair[0])] = list(pair)
            extras = {k: deepcopy(v) for k, v in pref.items() if k not in ("room", "uv_switch")}
            if extras:
                self._pref_extras.setdefault(map_key, {}).update(extras)
        return merged

    def _preference_view(self, map_key: str) -> dict | None:
        """The merged preferences for one map, shaped like a get_preference reply."""
        with self._preference_lock:
            rooms = self._room_store.get(map_key)
            if not rooms:
                return None
            room_rows = [deepcopy(row) for row in rooms.values()]
            uv = self._uv_store.get(map_key, {})
            uv_rows = [list(uv[rid]) for rid in rooms if rid in uv]
            return {**deepcopy(self._pref_extras.get(map_key, {})),
                    "room": room_rows, "uv_switch": uv_rows}

    @property
    def _cached_preference(self) -> dict | None:
        """Merged preferences for the CURRENT map (a fresh copy), or None.

        Kept under its historical name: callers and tests read it like the
        single cached get_preference reply it used to be.
        """
        return self._preference_view(self._map_key())

    @_cached_preference.setter
    def _cached_preference(self, value: dict | None) -> None:
        """Replace the current map's stored preferences wholesale (tests/reset)."""
        key = self._map_key()
        with self._preference_lock:
            self._room_store.pop(key, None)
            self._uv_store.pop(key, None)
            self._pref_extras.pop(key, None)
        if value:
            self._merge_preference(key, value)

    def set_map_inventory(self, map_id, zones: list) -> None:
        """Record a map's authoritative room list (REST persistent-map metadata).

        Rooms the store holds for that map but the metadata no longer lists are
        removed - this is the only way a room disappears. As a safety net, if
        none of the metadata's zone IDs match any stored room ID the data is
        assumed inconsistent and nothing is removed.
        """
        if not isinstance(zones, list):
            return
        inventory = {str(z.get("id")): str(z.get("name") or "")
                     for z in zones if isinstance(z, dict) and z.get("id") is not None}
        key = self._map_key(map_id)
        removed: list[str] = []
        with self._preference_lock:
            self._map_inventory[key] = inventory
            stored = self._room_store.get(key, {})
            if inventory and stored and set(stored) & set(inventory):
                removed = [rid for rid in stored if rid not in inventory]
                for rid in removed:
                    stored.pop(rid, None)
                    self._uv_store.get(key, {}).pop(rid, None)
            elif inventory and stored:
                _LOGGER.warning("[%s] Map %s metadata shares no room IDs with stored "
                                "preferences; not removing any rooms", self.serial, key)
        if removed:
            _LOGGER.info("[%s] Rooms %s removed from map %s (no longer in map data)",
                         self.serial, removed, key)
            self._notify_state_change()

    def _track_preference_request(self, msg_id, map_id) -> None:
        """Remember which map a get_preference request (ours or the app's) asked for."""
        if msg_id is None or map_id is None:
            return
        with self._preference_lock:
            self._pref_request_maps[str(msg_id)] = self._map_key(map_id)
            while len(self._pref_request_maps) > _MAX_TRACKED_REQUESTS:
                self._pref_request_maps.popitem(last=False)

    # ── Room listing ──────────────────────────────────────────────────────────

    @property
    def room_names(self) -> list[str]:
        """Normalised display names of every known room on the current map."""
        pref = self._cached_preference
        if not (pref and pref.get("room")):
            return []
        return [_room_display_name(r[1]) for r in pref["room"]]

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

        Returns a COPY of the merged rows for the current map: every room seen
        in any preference reply for it, minus rooms the map metadata removed.
        Change settings with edit_room_setting() / save_room_settings().
        """
        pref = self._cached_preference
        if pref and isinstance(pref.get("room"), list):
            return pref["room"]
        return []

    def room_preferences_for_edit(self) -> list[list]:
        """A private copy of the room array to base a new settings edit on.

        Normally the robot-reported cache. While a just-sent change is awaiting
        the robot's confirming reply (at most _PREFERENCE_TIMEOUT seconds), it
        is that sent array instead, so a second quick edit doesn't send the
        pre-edit values back and undo the first.
        """
        with self._preference_lock:
            if (self._pending_rooms is not None
                    and time.monotonic() - self._pending_since < _PREFERENCE_TIMEOUT):
                return deepcopy(self._pending_rooms)
            self._pending_rooms = None
            return deepcopy(self.room_preferences)

    def edit_room_setting(self, room_id, index: int, value) -> bool:
        """Change one raw field of one room and publish, as one serialised step.

        Used by the per-room selects. The base array is read inside the lock,
        so an overlapping edit always sees this one's result.
        """
        def change(rooms: list[list]) -> bool:
            room = next((r for r in rooms if r and str(r[0]) == str(room_id)), None)
            if room is None or len(room) <= index:
                _LOGGER.warning("[%s] Room ID %r is no longer in the map", self.serial, room_id)
                return False
            room[index] = value
            return True
        return self._edit_rooms(change)

    def _edit_rooms(self, change: Callable[[list[list]], bool]) -> bool:
        """Read the edit base, apply ``change`` to a copy, publish - atomically."""
        with self._room_edit_lock:
            rooms = self.room_preferences_for_edit()
            if not rooms:
                _LOGGER.warning("[%s] Cannot save room settings: preferences unavailable",
                                self.serial)
                return False
            if not change(rooms):
                return False
            return self.set_room_preferences(rooms)

    def set_room_preferences(self, rooms: list[list]) -> bool:
        """Publish an updated room-preferences array to the robot.

        Prefer edit_room_setting()/save_room_settings(), which read their base
        array under the same lock; a caller passing a full array it prepared
        earlier can still race with other edits.

        Used by per-room select entities to persist a single-setting change
        (cleaning mode, strategy, water level, or mop repetitions) without
        starting a clean cycle.  Safe to call from any thread; does not block.

        Returns True only if the MQTT client accepted the publish. That is
        local transport acceptance, NOT confirmation that the robot applied it.

        The cache is never written from here, even on success: HA asks the
        robot for its preferences straight after, and only the robot's reply
        updates what entities show. So a failed send leaves the old value, and
        a successful one can't overwrite telemetry that arrived mid-send.
        """
        with self._room_edit_lock:
            return self._send_room_preferences(rooms)

    def _send_room_preferences(self, rooms: list[list]) -> bool:
        """Body of set_room_preferences(); caller holds _room_edit_lock."""
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
        try:
            accepted = self._publish_jdm("service.set_preference", {
                "map_id":          map_id,
                "prefer_type":     1,
                "room_preference": rooms,
                "uv_switch":       uv_switch,
            })
        except Exception:  # noqa: BLE001 — any transport failure means "not sent"
            _LOGGER.exception("[%s] set_room_preferences: publish raised", self.serial)
            return False
        if not accepted:
            _LOGGER.warning("[%s] set_room_preferences: publish rejected", self.serial)
            return False

        # Remember what we sent until the robot answers THIS refresh (matched
        # by msgId - an older or app-originated reply doesn't count).
        refresh_id = _rand_msg_id()
        with self._preference_lock:
            self._pending_rooms = deepcopy(rooms)
            self._pending_refresh_id = refresh_id
            self._pending_since = time.monotonic()
        try:
            requested = self._publish_raw(self._jdm_command_topic, {
                "msgId": refresh_id, "version": "1.0.1",
                "method": "service.get_preference",
                "params": {"map_id": map_id}, "time": _now_iso(),
            })
        except Exception:  # noqa: BLE001 — the save itself was accepted
            requested = False
        if not requested:
            # The setting was still sent; HA will show it after the next
            # preference refresh (reconnect, map probe or app activity).
            _LOGGER.warning("[%s] Room settings sent but the confirmation request "
                            "failed; HA may show old values until the next refresh",
                            self.serial)
        return True

    def save_room_settings(self, room_settings: dict[str, dict[str, str]]) -> bool:
        """Apply a room-ID keyed draft and publish preferences once, without cleaning."""
        if not isinstance(room_settings, dict) or not room_settings:
            _LOGGER.warning("[%s] No room settings were supplied", self.serial)
            return False

        def change(rooms: list[list]) -> bool:
            by_id = {str(room[0]): room for room in rooms if isinstance(room, list) and room}
            for room_id, settings in room_settings.items():
                room = by_id.get(str(room_id))
                if room is None or len(room) < 7 or not isinstance(settings, dict) or not settings:
                    _LOGGER.warning("[%s] Invalid room settings for room ID %r",
                                    self.serial, room_id)
                    return False
                if not self._apply_room_settings(room, settings):
                    return False
            return True
        return self._edit_rooms(change)

    def _apply_room_settings(self, room: list, settings: dict) -> bool:
        """Write label-valued settings into one room array; False if any is invalid.

        Only keys/labels in _ROOM_SETTING_OPTIONS are accepted, so service-call
        input can never write arbitrary values or indices into the array.
        May partially modify ``room`` before failing — callers work on a copy.
        """
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
        return True

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
                if not self._can_start_new_clean():
                    return False
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
                # A Dyson-app command may have changed readiness while we waited.
                if not self._can_start_new_clean():
                    return False
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
                # The fresh reply itself must be well-formed - a malformed or
                # empty one still aborts, and nothing is padded with defaults.
                fresh = pref.get("room")
                if not isinstance(fresh, list) or not fresh or not all(
                        _valid_room_row(room) for room in fresh):
                    _LOGGER.warning("[%s] Cannot start: incomplete room preferences", self.serial)
                    return False
                # It may still be PARTIAL (e.g. only the scheduled room). It has
                # already been merged into the store, so start from the merged
                # view: every robot-reported room stays addressable and the
                # outgoing set_preference carries the full list, as the app does.
                merged = self._preference_view(self._map_key(map_id)) or {}
                rooms = merged.get("room") or []
                if not rooms:
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
                selected_set = {str(room_id) for room_id in selected_room_ids}
                for room in updated_rooms:
                    enabled = str(room[0]) in selected_set
                    room[8] = int(enabled)
                    if enabled and mode is not None:
                        room[3] = mode
                    settings = settings_by_id.get(str(room[0]), {})
                    if not isinstance(settings, dict):
                        _LOGGER.warning("[%s] Invalid room settings for room ID %r", self.serial, room[0])
                        return False
                    if not self._apply_room_settings(room, settings):
                        return False
                if set(settings_by_id) - {str(room[0]) for room in updated_rooms}:
                    _LOGGER.warning("[%s] Room settings refer to a missing room ID", self.serial)
                    return False

                ordered_room_ids = selected_room_ids

                _LOGGER.info(
                    "[%s] Starting cleaning in rooms %s with refreshed preferences",
                    self.serial, ordered_room_ids,
                )
                if room_ids is not None:
                    # Pre-existing optimistic update of the card start path.
                    self._merge_preference(self._map_key(map_id), {"room": updated_rooms})
                self._publish_jdm("service.set_preference", {
                    "map_id": map_id,
                    "prefer_type": 1,
                    "room_preference": updated_rooms,
                    "uv_switch": deepcopy(merged.get("uv_switch", [])),
                })
                self._abort_dock_action()
                published = self._publish({
                    "msg": "START",
                    "mode-reason": "RAPP",
                    "cleaningMode": "zoneConfigured",
                    "cleaningProgramme": {
                        "persistentMapId": str(map_id),
                        "unorderedZones": [str(room_id) for room_id in ordered_room_ids],
                    },
                    "time": _now_iso(),
                })
                if not published:
                    _LOGGER.warning(
                        "[%s] Cannot start: START publish rejected by paho (rc != 0)",
                        self.serial,
                    )
                    return False
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
        # Retain map/preferences, but never authorize a new job from a previous
        # connection's readiness snapshot. Wait for fresh robot/dock telemetry.
        for key in ("state", "dockState", "workMode", "jdmStatus", *STATE_FIELDS):
            self.state.pop(key, None)
        self._notify_state_change()
        self._prefs_map_id = None  # Re-fetch the current map's rooms on reconnect
        # Single wildcard subscription covers every prefix (RB05, NROB, …).
        client.subscribe(f"+/{self.serial}/#", qos=0)
        self.request_current_state()
        self._schedule_map_probes()
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
        self._clear_drying_time()
        self._notify_state_change()
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
        # The new prefix is persisted to the config entry and used in every
        # outbound topic, so only accept plain identifiers (no wildcards).
        parts = topic.split("/")
        if (
            len(parts) >= 2
            and parts[1] == self.serial
            and parts[0] != self._prefix
            and re.fullmatch(MQTT_PREFIX_PATTERN, parts[0])
        ):
            _LOGGER.warning(
                "[%s] Real MQTT prefix is '%s' (was '%s') — switching now",
                self.serial, parts[0], self._prefix,
            )
            self._prefix = parts[0]
            if self.on_prefix_changed:
                self.on_prefix_changed(parts[0])
            self.request_current_state()
            self._prefs_map_id = None
            self._schedule_map_probes()

        # json.dumps of large map/state payloads is costly; skip it unless
        # debug logging is actually enabled.
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("[%s] ← %s  %s", self.serial, topic, json.dumps(data))

        if topic.endswith("/command/jdm"):
            # Our own and the Dyson app's requests. Only used to learn which
            # map each get_preference reply belongs to.
            if data.get("method") == "service.get_preference":
                params = data.get("params") if isinstance(data.get("params"), dict) else {}
                self._track_preference_request(data.get("msgId"), params.get("map_id"))
        elif topic.endswith("/status/jdm"):
            self._handle_jdm(data)
        elif topic.endswith("/status") or topic.endswith("/status/current"):
            self._handle_status(data)

    # ── Message handlers ──────────────────────────────────────────────────────

    def _handle_jdm(self, data: dict) -> None:
        method = data.get("method")

        if method == "prop.post" and data.get("params"):
            self._merge_jdm_props(data["params"])
        elif method == "prop.get" and data.get("code") in (0, 1) and isinstance(data.get("data"), dict):
            # Dyson returns code 1 for partially supported property requests.
            updates = reported_properties(data["data"])
            if updates:
                self.state = {**self.state, **updates}
                self._notify_state_change()
        elif method == "prop.set" and data.get("code") != 0:
            _LOGGER.warning("[%s] Station property write rejected (code=%s)",
                            self.serial, data.get("code"))
        elif method == "service.get_preference":
            pref = data.get("data")
            success = data.get("code") == 0 and isinstance(pref, dict)
            with self._preference_lock:
                # File the reply under the map it was requested for. Unknown
                # msgIds (request not seen) are assumed to be the current map;
                # merging can only add or update rooms, never remove them.
                map_key = self._pref_request_maps.pop(str(data.get("msgId")), None)
                if map_key is None:
                    map_key = self._map_key()
                merged = 0
                if success and isinstance(pref.get("room"), list):
                    # Merge BEFORE waking a pending start, which reads the store.
                    merged = self._merge_preference(map_key, pref)
                if (self._preference_request_id is not None
                        and str(data.get("msgId")) == self._preference_request_id):
                    self._preference_response = deepcopy(pref) if success else None
                    self._preference_ready.set()
                if (self._pending_refresh_id is not None
                        and str(data.get("msgId")) == self._pending_refresh_id):
                    # The robot has answered our post-save refresh: from now on
                    # edits build on what it reports, not on what we sent.
                    self._pending_rooms = None
                    self._pending_refresh_id = None
            if not merged:
                _LOGGER.debug("[%s] get_preference for map %s: 0 rooms", self.serial, map_key)
                return
            known = len(self._room_store.get(map_key, {}))
            _LOGGER.debug("[%s] Room preferences for map %s: %d in reply, %d known",
                          self.serial, map_key, merged, known)
            if map_key == self._map_key():
                self._notify_state_change()

    def _handle_status(self, data: dict) -> None:
        msg_type = data.get("msg") or data.get("method")
        if msg_type in {"CURRENT-STATE", "STATE-CHANGE", "PRODUCT_INFO", "INITIAL_STATE"}:
            self.state = {**self.state, **data}
            if "dockState" in data and data["dockState"] != "DRYING_MOP":
                self._clear_drying_time()
            self._notify_state_change()

            # Fetch rooms whenever the current map changes (and after
            # reconnect), not just once per connection: a new map must not
            # keep showing the previous map's rooms.
            map_id = self.state.get("persistentMapId")
            if map_id not in (None, "") and str(map_id) != self._prefs_map_id:
                self._prefs_map_id = str(map_id)
                self._fetch_room_preferences()
                if self.on_map_changed:
                    self.on_map_changed(str(map_id))

        elif "faultCode" in data or "faultId" in data:
            _LOGGER.warning(
                "[%s] Fault %s — status: %s", self.serial,
                _fault_code(data), data.get("status")
            )

    def _merge_jdm_props(self, props: dict) -> None:
        updates: dict = reported_properties(props)
        work_time = props.get("work_time")
        if isinstance(work_time, dict) and self.state.get("dockState") == "DRYING_MOP":
            total, remaining = work_time.get("total"), work_time.get("surplus")
            # Type 3 is confirmed for drying. Reject booleans, incomplete and
            # invalid samples without resetting the last valid measurement.
            if (type(work_time.get("type")) is int and work_time["type"] == 3
                    and type(total) is int and type(remaining) is int
                    and total > 0 and 0 <= remaining <= total):
                updates.update({
                    "drying_total_seconds": total,
                    "drying_remaining_seconds": remaining,
                    "drying_updated_at": _now_iso(),
                })
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

    def _schedule_map_probes(self) -> None:
        """Probe map IDs 0-3 at staggered intervals (0, 2.5, 5, 7.5 s).

        Rooms then populate even when the robot omits persistentMapId from its
        idle CURRENT-STATE. Timers are daemon threads so a pending probe never
        delays Home Assistant shutdown.
        """
        for map_id in _PROBE_MAP_IDS:
            delay = map_id * _PROBE_INTERVAL
            if delay == 0:
                self._probe_map_id(map_id)
                continue
            timer = threading.Timer(delay, self._probe_map_id, args=(map_id,))
            timer.daemon = True
            timer.start()

    def _probe_map_id(self, map_id: int) -> None:
        """Send service.get_preference for one map_id.

        Probes for low integer IDs (0–3) fire speculatively at connection time
        before the robot has reported persistentMapId. Replies are filed under
        the map they were requested for, so a probe can no longer put another
        map's rooms on the current map. Skipped if rooms for that map are known.
        """
        with self._preference_lock:
            if self._room_store.get(self._map_key(map_id)):
                return
        _LOGGER.debug("[%s] Probing map_id=%d for room preferences", self.serial, map_id)
        self._publish_jdm("service.get_preference", {"map_id": map_id})

    def _fetch_room_preferences(self) -> None:
        map_id = self.state.get("persistentMapId")
        try:
            map_id_int = int(map_id)
        except (TypeError, ValueError):
            return
        _LOGGER.debug("[%s] Fetching room preferences for map %s", self.serial, map_id_int)
        # Always ask (unlike the probes): refreshes known rooms after reconnect.
        self._publish_jdm("service.get_preference", {"map_id": map_id_int})

    # ── Internal publish ──────────────────────────────────────────────────────

    def _publish_jdm(self, method: str, params: dict) -> bool:
        """Publish a JDM RPC call; True if paho accepted it (not robot confirmation)."""
        return self._publish_raw(self._jdm_command_topic, {
            "msgId":   _rand_msg_id(),
            "version": "1.0.1",
            "method":  method,
            "params":  params,
            "time":    _now_iso(),
        })

    def _publish(self, payload: dict) -> bool:
        """Publish to the robot's command topic; returns True on success."""
        return self._publish_raw(self._command_topic, payload)

    def _publish_raw(self, topic: str, payload: dict) -> bool:
        """Publish a JSON payload; returns True if paho accepted the message."""
        if payload.get("method") == "service.get_preference":
            params = payload.get("params") or {}
            self._track_preference_request(payload.get("msgId"), params.get("map_id"))
        with self._lock:
            client = self._client
        if not client or not self.connected:
            _LOGGER.warning("[%s] Cannot publish — not connected", self.serial)
            return False
        body = json.dumps(payload)
        if self._verbose:
            _LOGGER.debug("[MQTT out] %s: %s", topic, body)
        try:
            result = client.publish(topic, body, qos=0)
        except Exception:  # noqa: BLE001 — paho can raise on a closing socket
            _LOGGER.exception("[%s] Publish raised, topic=%s", self.serial, topic)
            return False
        if result.rc != 0:
            _LOGGER.warning(
                "[%s] Publish failed (rc=%d) topic=%s", self.serial, result.rc, topic,
            )
            return False
        return True
