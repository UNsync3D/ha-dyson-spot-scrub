"""Robot offline (GONE-AWAY) and reconnect handling; no robot or HA required.

From FutuRazor's connectivity capture (31 Oct 2026): Dyson's broker published
{"msg": "GONE-AWAY"} on the robot's status topic while the robot was
unreachable, and HA still showed it available. Also: a deliberate disconnect
logged "will reconnect", and "Reconnected" was logged before the broker had
accepted the connection.
Run: python -m unittest discover -s tests -v
"""
import asyncio
import importlib
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


def module(name, **attributes):
    result = ModuleType(name)
    result.__dict__.update(attributes)
    return result


class OfflineTests(unittest.TestCase):
    def setUp(self):
        package = module("offline_integration")
        package.__path__ = [str(Path(__file__).resolve().parents[1] /
                                "custom_components/dyson_spot_scrub")]
        self.enterContext(patch.dict(sys.modules, {
            "offline_integration": package,
            "paho": module("paho"),
            "paho.mqtt": module("paho.mqtt"),
            "paho.mqtt.client": module("paho.mqtt.client"),
            "homeassistant": module("homeassistant"),
            "homeassistant.config_entries": module(
                "homeassistant.config_entries", ConfigEntry=object),
            "homeassistant.core": module(
                "homeassistant.core", HomeAssistant=object, callback=lambda f: f),
            "homeassistant.helpers": module("homeassistant.helpers"),
            "homeassistant.helpers.aiohttp_client": module(
                "homeassistant.helpers.aiohttp_client",
                async_get_clientsession=lambda hass: None),
            "offline_integration.dyson_api": module(
                "offline_integration.dyson_api", DysonApiError=Exception,
                DysonRateLimitError=Exception,
                get_iot_credentials=AsyncMock(), get_live_map=AsyncMock(),
                get_map_metadata=AsyncMock(return_value=[])),
        }))
        self.mqtt = importlib.import_module("offline_integration.dyson_mqtt")
        self.coordinator_module = importlib.import_module("offline_integration.coordinator")
        self.client = self.new_client()
        self.client.connected = True
        self.client.state = {"persistentMapId": "123", "state": "INACTIVE_CHARGING",
                             "dockState": "IDLE"}
        self.client._cached_preference = {"room": [[12, "Flur", 0, 3, 0, 2, 0, 0, 1, 0, 1, 0]],
                                           "uv_switch": [[12, 0]]}
        self.sent = []
        self.enterContext(patch.object(self.client, "_publish_raw",
                                       lambda topic, payload: self.sent.append(payload) or True))
        self.timers = []
        self.enterContext(patch.object(self.mqtt.threading, "Timer", self.fake_timer))

    def new_client(self):
        return self.mqtt.DysonMqttClient("serial", "RB05", "example.invalid", "token",
                                         "signature", "client-id", "authorizer")

    def fake_timer(self, delay, fn, args=()):
        timer = SimpleNamespace(delay=delay, fn=fn, args=args, daemon=False,
                                cancelled=False, start=lambda: None)
        timer.cancel = lambda: setattr(timer, "cancelled", True)
        self.timers.append(timer)
        return timer

    def message(self, suffix, payload):
        msg = SimpleNamespace(topic=f"RB05/serial/{suffix}",
                              payload=json.dumps(payload).encode())
        self.client._on_message(None, None, msg)

    def poll_timers(self):
        return [t for t in self.timers if t.fn == self.client._offline_poll and not t.cancelled]

    def go_offline(self):
        self.message("status", {"msg": "GONE-AWAY"})

    # ── GONE-AWAY ─────────────────────────────────────────────────────────────

    def test_gone_away_makes_the_robot_unavailable_but_keeps_rooms(self):
        self.assertTrue(self.client.available)
        self.go_offline()
        self.assertFalse(self.client.robot_online)
        self.assertFalse(self.client.available)
        self.assertTrue(self.client.connected, "the broker connection is still up")
        self.assertNotIn("state", self.client.state, "stale readiness is dropped")
        self.assertEqual(self.client.state["persistentMapId"], "123")
        self.assertIsNotNone(self.client._cached_preference)
        self.assertEqual(self.client._cached_preference["room"][0][1], "Flur")

    def test_commands_are_refused_while_offline(self):
        self.go_offline()
        self.assertEqual(self.mqtt.start_block_reason(self.client.state, self.client.available),
                         "disconnected")
        self.assertFalse(self.client.start_room("Flur", 0))
        self.assertFalse(self.client.empty_bin())
        self.assertEqual(self.sent, [])

    def test_offline_poll_only_asks_for_state(self):
        self.go_offline()
        [timer] = self.poll_timers()
        self.assertEqual(timer.delay, self.mqtt._OFFLINE_POLL_INTERVAL)
        timer.fn()
        self.assertEqual(self.sent, [{"msg": "REQUEST-CURRENT-STATE",
                                      "time": self.sent[0]["time"]}])
        self.assertEqual(len(self.poll_timers()), 1, "polls again a minute later")

    def test_fresh_telemetry_brings_the_robot_back(self):
        self.go_offline()
        with patch.object(self.client, "_fetch_room_preferences") as fetch:
            self.message("status", {"msg": "CURRENT-STATE", "state": "INACTIVE_CHARGING",
                                     "dockState": "IDLE", "persistentMapId": "123"})
        self.assertTrue(self.client.robot_online)
        self.assertTrue(self.client.available)
        self.assertEqual(self.poll_timers(), [], "polling stops")
        fetch.assert_called_once()        # one room-settings refresh, nothing replayed
        self.assertEqual(self.sent, [])

    def test_other_clients_requests_do_not_count_as_the_robot(self):
        """In the capture, other clients' polling continued while the robot was gone."""
        self.go_offline()
        self.message("command", {"msg": "REQUEST-CURRENT-STATE", "mode-reason": "RAPP"})
        self.message("command/jdm", {"msgId": "1", "method": "service.get_map_list",
                                     "params": {}})
        self.assertFalse(self.client.robot_online)

    def test_offline_survives_a_broker_reconnect(self):
        self.go_offline()
        client = self.new_client()
        client.robot_online = False      # carried over by the coordinator
        with patch.object(client, "_publish_raw", Mock(return_value=True)), \
                patch.object(client, "_schedule_map_probes"):
            client._on_connect(Mock(), None, {}, 0)
        self.assertTrue(client.connected)
        self.assertFalse(client.available)
        self.assertTrue(any(t.fn == client._offline_poll for t in self.timers))

    # ── Deliberate disconnects ────────────────────────────────────────────────

    def test_deliberate_disconnect_is_not_reported_as_a_failure(self):
        callback = Mock()
        self.client.on_disconnected = callback
        paho = Mock()
        paho.disconnect.side_effect = lambda: self.client._on_disconnect(paho, None, 0)
        self.client._client = paho
        with self.assertNoLogs("offline_integration.dyson_mqtt", level="WARNING"):
            self.client.disconnect()
        callback.assert_not_called()
        self.assertFalse(self.client.connected)

    def test_unexpected_disconnect_still_triggers_a_reconnect(self):
        callback = Mock()
        self.client.on_disconnected = callback
        with self.assertLogs("offline_integration.dyson_mqtt", level="WARNING"):
            self.client._on_disconnect(None, None, 16)
        callback.assert_called_once()

    # ── Coordinator: confirmed connections only ───────────────────────────────

    def coordinator(self, connects):
        async def run(fn, *args):
            return fn(*args)
        hass = SimpleNamespace(async_add_executor_job=run)
        coordinator = self.coordinator_module.DysonCoordinator(
            hass, "token", "serial", "RB05", SimpleNamespace(data={}))
        clients = []

        class Client:
            def __init__(inner, **kwargs):
                inner.connected = False
                inner.robot_online = True
                inner.disconnect = Mock()
                inner.register_callback = Mock()
                clients.append(inner)

            def connect(inner):
                inner.connected = connects

        credentials = {"endpoint": "e", "token_value": "t", "token_signature": "s",
                       "client_id": "c", "authorizer_name": "a"}
        self.enterContext(patch.object(self.coordinator_module, "DysonMqttClient", Client))
        self.enterContext(patch.object(self.coordinator_module, "get_iot_credentials",
                                       AsyncMock(return_value=credentials)))
        self.enterContext(patch.object(self.coordinator_module, "_CONNECT_CONFIRM_TIMEOUT", 0.05))
        return coordinator, clients

    def test_unconfirmed_connection_is_a_failed_attempt(self):
        coordinator, clients = self.coordinator(connects=False)
        with self.assertRaises(ConnectionError):
            asyncio.run(coordinator._async_connect_cloud())
        self.assertIsNone(coordinator.mqtt)
        clients[0].disconnect.assert_called_once()

    def test_confirmed_connection_carries_the_offline_state(self):
        coordinator, clients = self.coordinator(connects=True)
        asyncio.run(coordinator._async_connect_cloud(robot_online=False))
        self.assertIs(coordinator.mqtt, clients[0])
        self.assertFalse(clients[0].robot_online)

    def test_reconnect_logs_success_only_after_confirmation(self):
        coordinator, clients = self.coordinator(connects=False)
        old = SimpleNamespace(robot_online=False, disconnect=Mock())
        coordinator.mqtt = old
        self.enterContext(patch.object(self.coordinator_module, "_RECONNECT_DELAYS", [0]))

        async def attempts():
            task = asyncio.ensure_future(coordinator._async_reconnect())
            while len(clients) < 2:
                await asyncio.sleep(0.01)
            for client in clients:          # the broker starts accepting
                client.connected = True
            type(clients[0]).connect = lambda inner: setattr(inner, "connected", True)
            await asyncio.wait_for(task, 2)

        with self.assertLogs("offline_integration.coordinator", level="INFO") as logs:
            asyncio.run(attempts())
        text = "\n".join(logs.output)
        self.assertIn("attempt 1 failed", text)
        self.assertIn("Reconnected to Dyson's broker", text)
        self.assertIn("robot still offline", text)
        self.assertLess(text.index("attempt 1 failed"), text.index("Reconnected"))
        old.disconnect.assert_called_once()
        self.assertFalse(coordinator.mqtt.robot_online)
        self.assertEqual(coordinator._reconnect_attempt, 0)


if __name__ == "__main__":
    unittest.main()
