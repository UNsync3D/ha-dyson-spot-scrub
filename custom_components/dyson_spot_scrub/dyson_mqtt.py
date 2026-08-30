"""Dyson local MQTT client — connection, commands, state parsing.

Connects to Dyson's AWS IoT Core broker over WebSocket (port 443) using a
custom authorizer. No certificates required — authentication is via signed
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
  3 = Vacuum then Mop (sequential, detected by sweep_type == 7)
"""
from __future__ import annotations

import json
import logging
import math
import random
import threading
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import quote

import paho.mqtt.client as mqtt

_LOGGER = logging.getLogger(__name__)

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
    """Vacuum-then-mop sequential — detected via sweep_type == 7."""
    return is_running(state) and state.get("sweepType") == 7


def is_vacuuming_only(state: dict) -> bool:
    return (
        is_running(state)
        and state.get("fullCleanAction") == "VACUUMING"
        and state.get("sweepType") != 7
    )


def is_vacuuming_and_mopping(state: dict) -> bool:
    return is_running(state) and state.get("fullCleanAction") == "VACUUMING_AND_MOPPING"


def is_mopping(state: dict) -> bool:
    return (
        is_running(state)
        and state.get("fullCleanAction") == "MOPPING"
        and state.get("sweepType") != 7
    )


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
    return any(f.get("status") != "LOG_ONLY" for f in faults)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rand_msg_id() -> str:
    return str(random.randint(0, 4294967295))


# ── MQTT client ───────────────────────────────────────────────────────────────


class DysonMqttClient:
    """Thread-safe paho-mqtt wrapper for the Dyson robot.

    Callbacks (on_state_change, on_connected, on_disconnected) are called
    from the paho network thread. Bridge to asyncio with
    hass.loop.call_soon_threadsafe() in the coordinator.
    """

    def __init__(
        self,
        serial: str,
        mqtt_prefix: str,
        iot_creds: dict[str, str],
        verbose: bool = False,
    ) -> None:
        self.serial     = serial
        self._prefix    = mqtt_prefix
        self._iot_creds = iot_creds
        self._verbose   = verbose

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

        # Single retry timer for start_mode — prevents runaway thread spawning
        self._start_retry_timer: threading.Timer | None = None
        self._start_retry_mode:  int | None = None
        self._start_retry_count: int = 0

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
        """Connect (blocking until first connect or error). Call from executor."""
        creds = self._iot_creds

        # paho-mqtt 2.x requires an explicit callback API version.
        # VERSION1 keeps the old 4-argument on_connect / 3-argument on_disconnect
        # signatures so the rest of the code is unchanged.
        # reconnect_on_failure=False: we manage all reconnects in the coordinator.
        # paho's built-in retry reuses a stale WebSocket URL+token and — because
        # it shares the same ClientId — causes the broker to kick our new client
        # off (rc=7), creating a rapid-fire disconnect cascade.
        try:
            client = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
                client_id=creds["client_id"],
                transport="websockets",
                protocol=mqtt.MQTTv311,
                reconnect_on_failure=False,
            )
        except AttributeError:
            # paho-mqtt 1.x — no CallbackAPIVersion or reconnect_on_failure
            client = mqtt.Client(
                client_id=creds["client_id"],
                transport="websockets",
                protocol=mqtt.MQTTv311,
            )

        # Include custom-authorizer credentials in the WebSocket upgrade path.
        # paho uses this string as the full HTTP URI (including query params).
        ws_path = (
            "/mqtt"
            f"?x-amz-customauthorizer-name={quote(creds['authorizer_name'])}"
            f"&token={quote(creds['token_value'])}"
            f"&x-amz-customauthorizer-signature={quote(creds['token_signature'])}"
        )
        client.ws_set_options(path=ws_path)
        client.tls_set_context()  # use system CAs; Dyson's endpoint is valid AWS IoT
        client.tls_insecure_set(True)

        client.on_connect    = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message    = self._on_message

        _LOGGER.debug("[%s] Connecting to %s:443 (WSS)", self.serial, creds["endpoint"])
        client.connect(creds["endpoint"], port=443, keepalive=30)

        with self._lock:
            self._client = client

        client.loop_start()

    def disconnect(self) -> None:
        with self._lock:
            c = self._client
            self._client = None
        if c:
            c.loop_stop()
            c.disconnect()
        self.connected = False

    def request_current_state(self) -> None:
        self._publish({"msg": "REQUEST-CURRENT-STATE", "time": _now_iso()})

    def start_mode(self, mode: int) -> None:
        """Start cleaning in one of four modes (0-3)."""
        labels = ["vacuum only", "vacuum + mop", "mop only", "vacuum then mop"]
        _LOGGER.info("[%s] → START (%s)", self.serial, labels[mode] if mode < 4 else mode)

        # Cancel any pending retry for a different mode
        if self._start_retry_mode != mode:
            self._cancel_start_retry()
            self._start_retry_count = 0
            self._start_retry_mode = mode

        # Mop-only: global clean in mop mode — no room preference needed
        if mode == 2:
            self._cancel_start_retry()
            self._publish({
                "msg":          "START",
                "mode-reason":  "RAPP",
                "cleaningMode": "global",
                "time":         _now_iso(),
            })
            return

        # All other modes need room preference cache
        if not (self._cached_preference and self._cached_preference.get("room")):
            self._start_retry_count += 1

            # After 10 retries (~20 s), fall back to a basic global start
            if self._start_retry_count > 10:
                _LOGGER.warning(
                    "[%s] Room preferences unavailable after %d retries — "
                    "sending basic global start",
                    self.serial, self._start_retry_count,
                )
                self._cancel_start_retry()
                # Basic global start — no room_ids so the robot cleans everything.
                # Do NOT send service.set_room_clean with room_ids=[] here; an
                # empty room list is rejected by the robot and prevents cleaning.
                self._publish({
                    "msg":          "START",
                    "mode-reason":  "RAPP",
                    "cleaningMode": "global",
                    "time":         _now_iso(),
                })
                return

            # Only schedule ONE retry at a time
            if self._start_retry_timer is None:
                _LOGGER.warning(
                    "[%s] Room preferences not yet cached — retrying in 2 s (attempt %d/10)",
                    self.serial, self._start_retry_count,
                )
                self._start_retry_timer = threading.Timer(
                    2.0, self._do_start_retry
                )
                self._start_retry_timer.start()
            return

        pref    = self._cached_preference
        map_id  = int(self.state.get("persistentMapId", 0))
        room_ids = [r[0] for r in pref["room"]]
        zone_ids = [str(r) for r in room_ids]

        # Set preference mode (index 3) for every room
        updated_rooms = []
        for room in pref["room"]:
            r = list(room)[:11]
            while len(r) < 11:
                r.append(0)
            r[3] = mode
            updated_rooms.append(r)

        self._publish_jdm("service.set_preference", {
            "map_id":          map_id,
            "prefer_type":     1,
            "room_preference": updated_rooms,
            "uv_switch":       pref.get("uv_switch", []),
        })

        self._publish({
            "msg":           "START",
            "mode-reason":   "RAPP",
            "cleaningMode":  "zoneConfigured",
            "cleaningProgramme": {
                "persistentMapId": str(map_id),
                "unorderedZones":  zone_ids,
            },
            "time": _now_iso(),
        })

        self._publish_jdm("service.set_cur_map", {"map_id": map_id})
        self._publish_jdm("service.set_room_clean",
                           {"ctrl_value": 1, "clean_type": 0, "room_ids": room_ids})

    def _cancel_start_retry(self) -> None:
        """Cancel the pending start-mode retry timer (if any)."""
        t = self._start_retry_timer
        self._start_retry_timer = None
        if t is not None:
            t.cancel()

    def _do_start_retry(self) -> None:
        """Called by the retry timer — clears the timer ref then retries."""
        self._start_retry_timer = None
        mode = self._start_retry_mode
        if mode is not None:
            self.start_mode(mode)

    def stop(self) -> None:
        self._cancel_start_retry()
        _LOGGER.info("[%s] → STOP", self.serial)
        self._publish({"msg": "STOP", "mode-reason": "RAPP", "time": _now_iso()})

    def return_to_base(self) -> None:
        self._cancel_start_retry()
        _LOGGER.info("[%s] → RETURN_TO_BASE", self.serial)
        self._publish({"msg": "ABORT", "mode-reason": "RAPP", "time": _now_iso()})
        self._publish_jdm("service.start_recharge", {})

    # ── paho callbacks ────────────────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, rc) -> None:
        if rc != 0:
            _LOGGER.error("[%s] MQTT connect failed, rc=%s", self.serial, rc)
            return
        _LOGGER.info(
            "[%s] MQTT connected — endpoint=%s prefix=%s",
            self.serial, self._iot_creds.get("endpoint"), self._prefix,
        )
        self.connected = True
        self._preferences_fetched = False  # Allow re-fetch on reconnect
        client.subscribe(self._wildcard_topic, qos=0)
        # Also subscribe with a wildcard prefix as a safety net.  If the
        # stored prefix ever diverges from what the robot uses (e.g. after
        # a firmware update), messages still arrive and _on_message auto-
        # corrects self._prefix for the current session.
        client.subscribe(f"+/{self.serial}/#", qos=0)
        self.request_current_state()
        # Probe for preferences immediately with map_id=0 — catches robots
        # that don't include persistentMapId in their idle CURRENT-STATE
        self._publish_raw(self._jdm_command_topic, {
            "msgId":   _rand_msg_id(),
            "version": "1.0.1",
            "method":  "service.get_preference",
            "params":  {"map_id": 0},
            "time":    _now_iso(),
        })
        if self.on_connected:
            self.on_connected()

    def _on_disconnect(self, client, userdata, rc) -> None:
        # Guard: only propagate the first disconnect event per client instance.
        # With paho's auto-reconnect, _on_disconnect may fire multiple times
        # (once per failed retry attempt), each with a different rc. Without this
        # guard, each firing would trigger a new coordinator reconnect task,
        # creating more clients and amplifying the cascade.
        if self._disconnected_handled:
            _LOGGER.debug(
                "[%s] Ignoring repeated disconnect callback (rc=%s)", self.serial, rc
            )
            return
        self._disconnected_handled = True
        _LOGGER.warning("[%s] MQTT disconnected (rc=%s)", self.serial, rc)
        self.connected = False
        if self.on_disconnected:
            self.on_disconnected()

    def _on_message(self, client, userdata, msg) -> None:
        try:
            data = json.loads(msg.payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        topic = msg.topic

        # Auto-detect the real MQTT prefix from inbound robot messages.
        # The stored prefix may differ from what the robot actually uses
        # (e.g. config stored "NROB" but robot publishes on "RB05").
        # Adopt the real prefix immediately so all subsequent command
        # publishes reach the robot on the correct topic.
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
            # The initial requests (current-state, get_preference) were sent
            # to the wrong prefix and were silently dropped.  Re-send them
            # now on the correct prefix so the room preferences are cached
            # before the user presses Start.
            self.request_current_state()
            self._preferences_fetched = False
            self._publish_raw(self._jdm_command_topic, {
                "msgId":   _rand_msg_id(),
                "version": "1.0.1",
                "method":  "service.get_preference",
                "params":  {"map_id": 0},
                "time":    _now_iso(),
            })

        _LOGGER.debug("[%s] ← %s  %s", self.serial, topic, str(data)[:300])

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
        elif method == "service.get_preference" and data.get("code") == 0 and data.get("data"):
            pref = data["data"]
            n = len(pref.get("room", []))
            if n == 0:
                # map_id=0 probe returned nothing — ignore so we don't
                # blank out a previously valid cache.
                _LOGGER.debug("[%s] get_preference: 0 rooms (map probe)", self.serial)
                return
            first_cache = self._cached_preference is None
            self._cached_preference = pref
            if first_cache:
                _LOGGER.info("[%s] Room preferences cached (%d room(s))", self.serial, n)
            else:
                _LOGGER.debug("[%s] Room preferences refreshed (%d room(s))", self.serial, n)

    def _handle_status(self, data: dict) -> None:
        msg_type = data.get("msg") or data.get("method")
        if msg_type in {"CURRENT-STATE", "STATE-CHANGE", "PRODUCT_INFO", "INITIAL_STATE"}:
            self.state = {**self.state, **data}
            self._notify_state_change()

            # Fetch room preferences once we have the map ID
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

    def _fetch_room_preferences(self) -> None:
        map_id = self.state.get("persistentMapId")
        try:
            map_id_int = int(map_id)
        except (TypeError, ValueError):
            return
        _LOGGER.debug("[%s] Fetching room preferences for map %s", self.serial, map_id_int)
        self._publish_raw(self._jdm_command_topic, {
            "msgId":   _rand_msg_id(),
            "version": "1.0.1",
            "method":  "service.get_preference",
            "params":  {"map_id": map_id_int},
            "time":    _now_iso(),
        })

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
