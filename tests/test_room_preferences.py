"""Offline regression tests for room settings; no robot or HA required.

Run: python -m unittest discover -s tests -v
MQTT transport and HA are mocked; the actual MQTT/coordinator modules run.
Room arrays follow the get_preference capture from 2026-10-05 and the
controlled app setting changes from 2026-10-06.
"""
import asyncio
from copy import deepcopy
import importlib
import json
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


def module(name, **attributes):
    result = ModuleType(name)
    result.__dict__.update(attributes)
    return result


class RoomSettingsTests(unittest.TestCase):
    def setUp(self):
        package = module("room_test_integration")
        package.__path__ = [str(Path(__file__).resolve().parents[1] /
                                "custom_components/dyson_spot_scrub")]
        self.enterContext(patch.dict(sys.modules, {
            "room_test_integration": package,
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
            "room_test_integration.dyson_api": module(
                "room_test_integration.dyson_api", DysonApiError=Exception,
                DysonRateLimitError=Exception,
                get_iot_credentials=AsyncMock(), get_live_map=AsyncMock(),
                get_map_metadata=AsyncMock(return_value=[])),
        }))
        self.mqtt = importlib.import_module("room_test_integration.dyson_mqtt")
        self.client = self.mqtt.DysonMqttClient(
            "test-serial", "RB05", "example.invalid", "token", "signature",
            "client-id", "authorizer")
        self.client.connected = True
        self.client.state = {"persistentMapId": "123", "state": "INACTIVE_CHARGING", "dockState": "IDLE"}
        self.preferences = {
            "room": [
                [12, "Flur", 0, 3, 0, 2, 0, 0, 1, 0, 1, 0],
                [10, "Schlafzimmer", 0, 0, 3, 2, 0, 0, 0, 0, 2, 0],
            ],
            "material": [], "uv_switch": [[12, 0], [10, 0]], "prefer_on": 1,
        }
        self.client._cached_preference = deepcopy(self.preferences)
        self.sent = []
        self.enterContext(patch.object(self.client, "_publish_raw", self.publish))
        self.enterContext(patch.object(self.mqtt, "_PREFERENCE_TIMEOUT", 0.02))
        self.on_request = lambda payload: self.reply(payload)

    def publish(self, topic, payload):
        self.sent.append((topic, deepcopy(payload)))
        if payload.get("method") == "service.get_preference":
            # As the real _publish_raw does: remember which map we asked about.
            self.client._track_preference_request(
                payload.get("msgId"), payload["params"].get("map_id"))
            self.on_request(payload)
        return True  # Match the transport's successful-publication contract.

    def reply(self, request, preferences=None, code=0, msg_id=None):
        data = {
            "msgId": request["msgId"] if msg_id is None else msg_id,
            "method": "service.get_preference", "code": code,
            "data": deepcopy(self.preferences if preferences is None else preferences),
        }
        self.client._on_message(None, None, SimpleNamespace(
            topic="RB05/test-serial/status/jdm", payload=json.dumps(data).encode()))

    def settings(self):
        return next(p["params"] for _, p in self.sent
                    if p.get("method") == "service.set_preference")

    def assert_no_start(self):
        self.assertFalse(any(p.get("msg") == "START" or p.get("method") in {
            "service.set_preference", "service.set_room_clean", "service.set_cur_map",
        } for _, p in self.sent))

    def test_all_four_modes_preserve_other_fields_and_array_lengths(self):
        for length in (11, 12, 13):
            for mode in range(4):
                with self.subTest(length=length, mode=mode):
                    self.sent.clear()
                    rooms = self.preferences["room"]
                    rooms[0] = [12, "Flur", 17, 0, 1, 99, 1, 23, 0, 42, 2, 7, 8][:length]
                    rooms[1] = [10, "Schlafzimmer", 19, 2, 2, 1, 1, 24, 1, 43, 1, 9, 10][:length]
                    original = deepcopy(self.preferences)
                    self.assertTrue(self.client.start_room("flur", mode))
                    expected = deepcopy(rooms)
                    expected[0][3], expected[0][8], expected[1][8] = mode, 1, 0
                    self.assertEqual(self.settings()["room_preference"], expected)
                    self.assertEqual(self.settings()["uv_switch"], original["uv_switch"])
                    self.assertEqual(self.preferences, original)
                    self.assertEqual(self.client._cached_preference, original)
                    commands = [p.get("method", p.get("msg")) for _, p in self.sent]
                    self.assertEqual(commands, ["service.get_preference", "service.set_preference",
                                               "ABORT-DOCK-ACTION", "START",
                                               "service.set_cur_map", "service.set_room_clean"])
                    self.assertEqual(self.sent[-1][1]["params"]["room_ids"], [12])

    def test_vacuum_then_mop_does_not_turn_auto_into_quick(self):
        self.assertTrue(self.client.start_room("Flur", 3))
        self.assertEqual(self.settings()["room_preference"][0][3:7], [3, 0, 2, 0])

    def test_global_start_including_mop_updates_only_mode_and_selection(self):
        for mode in range(4):
            with self.subTest(mode=mode):
                self.sent.clear()
                self.assertTrue(self.client.start_mode(mode))
                expected = deepcopy(self.preferences["room"])
                for room in expected:
                    room[3], room[8] = mode, 1
                self.assertEqual(self.settings()["room_preference"], expected)
                self.assertEqual(self.sent[-1][1]["params"]["room_ids"], [12, 10])

    def test_new_response_overrides_stale_cache(self):
        self.preferences["room"][0][4:7] = [2, 99, 1]
        self.assertTrue(self.client.start_room("Flur", 1))
        self.assertEqual(self.settings()["room_preference"][0][3:7], [1, 2, 99, 1])

    def test_room_names_are_preserved_while_matching_display_name(self):
        self.preferences["room"][0][1] = '{"type":"hallway","name":"Flur"}'
        self.assertTrue(self.client.start_room("FLUR", 0))
        self.assertEqual(self.settings()["room_preference"][0][1],
                         self.preferences["room"][0][1])

    def test_timeout_never_uses_existing_cache_or_starts_later(self):
        requests = []
        self.on_request = requests.append
        self.assertFalse(self.client.start_room("Flur", 3))
        self.reply(requests[0])
        self.assert_no_start()

    def test_matching_response_is_required(self):
        self.on_request = lambda p: self.reply(p, msg_id="unrelated")
        self.assertFalse(self.client.start_mode(2))
        self.assert_no_start()

    def test_late_previous_response_cannot_satisfy_next_start(self):
        requests = []
        self.on_request = requests.append
        self.assertFalse(self.client.start_mode(0))
        self.on_request = lambda p: self.reply(requests[0])
        self.assertFalse(self.client.start_mode(1))
        self.assert_no_start()

    def test_error_response_aborts(self):
        self.on_request = lambda p: self.reply(p, code=1)
        self.assertFalse(self.client.start_mode(0))
        self.assert_no_start()

    def test_incomplete_rooms_abort_without_padding_defaults(self):
        for rooms in ([], [[12, "Flur"]], "invalid",
                      [[[12], "Flur", 0, 0, 0, 0, 0, 0, 1, 0, 1]]):
            with self.subTest(rooms=rooms):
                self.on_request = lambda p: self.reply(p, {"room": rooms})
                self.assertFalse(self.client.start_room("Flur", 0))
                self.assert_no_start()

    def test_missing_or_ambiguous_room_aborts(self):
        self.assertFalse(self.client.start_room("missing", 0))
        self.preferences["room"][1][1] = "Flur"
        self.assertFalse(self.client.start_room("Flur", 0))
        self.assert_no_start()

    def test_missing_map_or_connection_aborts(self):
        self.client.state = {}
        self.assertFalse(self.client.start_mode(0))
        self.client.state = {"persistentMapId": "123"}
        self.client.connected = False
        self.assertFalse(self.client.start_mode(0))
        self.assertEqual(self.sent, [])

    def test_map_change_during_read_aborts(self):
        def change_map(request):
            self.client.state["persistentMapId"] = "456"
            self.reply(request)
        self.on_request = change_map
        self.assertFalse(self.client.start_room("Flur", 0))
        self.assert_no_start()

    def test_stop_return_and_disconnect_cancel_pending_start(self):
        for action in (self.client.stop, self.client.return_to_base,
                       self.client.disconnect):
            with self.subTest(action=action.__name__):
                self.client.connected = True
                def cancel(request):
                    action()
                    self.reply(request)
                self.on_request = cancel
                self.assertFalse(self.client.start_mode(2))
                self.assert_no_start()

    def test_mqtt_disconnect_cancels_pending_start(self):
        def disconnect(request):
            self.client._on_disconnect(None, None, 1)
        self.on_request = disconnect
        self.assertFalse(self.client.start_room("Flur", 0))
        self.assert_no_start()

    def test_concurrent_start_is_rejected(self):
        def concurrent(request):
            self.assertFalse(self.client.start_room("Schlafzimmer", 0))
            self.reply(request)
        self.on_request = concurrent
        self.assertTrue(self.client.start_room("Flur", 2))
        self.assertEqual(sum(p.get("msg") == "START" for _, p in self.sent), 1)

    def test_reply_from_mqtt_thread_unblocks_executor(self):
        requested = threading.Event()
        requests, results = [], []
        def capture(request):
            requests.append(request)
            requested.set()
        self.on_request = capture
        with patch.object(self.mqtt, "_PREFERENCE_TIMEOUT", 1):
            worker = threading.Thread(target=lambda: results.append(
                self.client.start_room("Flur", 3)), daemon=True)
            worker.start()
            self.assertTrue(requested.wait(1))
            self.reply(requests[0])
            worker.join(2)
            self.assertFalse(worker.is_alive())
        self.assertEqual(results, [True])

    def test_failed_room_start_does_not_wait_or_advance_sequence(self):
        coordinator_module = importlib.import_module("room_test_integration.coordinator")
        hass = SimpleNamespace(async_add_executor_job=AsyncMock(return_value=False))
        coordinator = coordinator_module.DysonCoordinator(
            hass, "token", "serial", "RB05", SimpleNamespace(data={}))
        coordinator.mqtt = SimpleNamespace(connected=True, start_room=Mock(), state=dict(self.client.state))
        with patch.object(coordinator, "_async_wait_for_clean_complete", AsyncMock()) as wait:
            asyncio.run(coordinator.async_clean_rooms_sequential(["Flur", "Schlafzimmer"]))
            wait.assert_not_awaited()
        hass.async_add_executor_job.assert_awaited_once()

    def test_readiness_change_during_preference_read_sends_no_writes(self):
        def changed(request):
            self.client.state.update(state="FULL_CLEAN_FINISHED", workMode=38)
            self.reply(request)
        self.on_request = changed
        self.assertFalse(self.client.start_room("Flur", 0))
        self.assertEqual([p.get("method") for _, p in self.sent], ["service.get_preference"])

    def test_start_during_confirmed_drying_keeps_existing_start_sequence(self):
        self.client.state.update(dockState="DRYING_MOP", workMode=0)
        self.assertTrue(self.client.start_room("Flur", 0))
        commands = [p.get("method", p.get("msg")) for _, p in self.sent]
        self.assertIn("ABORT-DOCK-ACTION", commands)
        self.assertIn("START", commands)
        self.assertLess(commands.index("ABORT-DOCK-ACTION"), commands.index("START"))

    def test_sequence_waits_for_station_work_after_charging(self):
        coordinator_module = importlib.import_module("room_test_integration.coordinator")
        async def run():
            coordinator = coordinator_module.DysonCoordinator(
                SimpleNamespace(), "token", "serial", "RB05", SimpleNamespace(data={}))
            coordinator.mqtt = self.client
            watchers = []
            coordinator.async_add_listener = watchers.append
            coordinator.async_remove_listener = watchers.remove
            wait = asyncio.create_task(coordinator._async_wait_for_clean_complete(timeout=1))
            await asyncio.sleep(0)
            for snapshot in [
                {"state":"FULL_CLEAN_RUNNING", "fullCleanAction":"VACUUMING", "dockState":"IDLE"},
                {"state":"FULL_CLEAN_FINISHED", "dockState":"IDLE", "workMode":38},
                {"state":"INACTIVE_CHARGING", "dockState":"COLLECTING_DUST", "workMode":0},
            ]:
                coordinator._state_snapshot = snapshot
                watchers[0].async_write_ha_state()
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                self.assertFalse(wait.done())
            coordinator._state_snapshot = {"state":"INACTIVE_CHARGING", "dockState":"IDLE", "workMode":0}
            watchers[0].async_write_ha_state()
            self.assertTrue(await wait)
            self.assertEqual(watchers, [])
        asyncio.run(run())


    # The card/room start sends a full room list, like a settings save, so it
    # must not revert a setting change that is in flight (Oct 2026 follow-up).

    def last_set(self):
        return [p for _, p in self.sent if p.get("method") == "service.set_preference"][-1]

    def test_start_keeps_a_just_sent_setting_change(self):
        self.enterContext(patch.object(self.mqtt, "_PREFERENCE_TIMEOUT", 5))
        self.on_request = lambda payload: None      # robot hasn't confirmed the edit yet
        self.assertTrue(self.client.edit_room_setting(12, 5, 1))   # Flur: water level 1
        self.on_request = lambda payload: self.reply(payload)      # still reports the OLD value
        self.assertTrue(self.client.start_room("flur", 0))
        flur = next(r for r in self.last_set()["params"]["room_preference"] if r[0] == 12)
        self.assertEqual(flur[5], 1, "the start must not send the old water level back")
        self.assertEqual(flur[8], 1)

    def test_edit_lock_held_while_preparing_and_sending_not_while_waiting(self):
        lock_free = {}

        def probe():
            result = []
            def attempt():
                got = self.client._room_edit_lock.acquire(blocking=False)
                result.append(got)
                if got:
                    self.client._room_edit_lock.release()
            worker = threading.Thread(target=attempt)
            worker.start()
            worker.join(2)
            return result[0]

        original = self.publish
        def publish(topic, payload):
            name = payload.get("method") or payload.get("msg")
            lock_free.setdefault(name, probe())
            return original(topic, payload)
        self.enterContext(patch.object(self.client, "_publish_raw", publish))
        self.assertTrue(self.client.start_room("flur", 0))
        self.assertTrue(lock_free["service.get_preference"], "free while waiting for the robot")
        for name in ("service.set_preference", "START", "service.set_room_clean"):
            self.assertFalse(lock_free[name], f"{name} sent while an edit could interleave")

    def test_edit_that_read_before_the_start_cannot_revert_it(self):
        # The race: an edit reads its base (robot's old selection: Flur), the
        # start then selects Schlafzimmer and publishes, then the edit publishes
        # its stale full list. The edit lock makes the start wait for the edit
        # and build on it instead, so the LAST list sent has both.
        self.enterContext(patch.object(self.mqtt, "_PREFERENCE_TIMEOUT", 5))
        read_base, start_sent = threading.Event(), threading.Event()
        original = self.client.room_preferences_for_edit
        def edit_base():
            rooms = original()
            if threading.current_thread().name == "edit":
                read_base.set()
                start_sent.wait(0.5)        # give the start its chance to interleave
            return rooms
        self.enterContext(patch.object(self.client, "room_preferences_for_edit", edit_base))
        sent_before = self.publish
        def publish(topic, payload):
            if payload.get("msg") == "START":
                start_sent.set()
            return sent_before(topic, payload)
        self.enterContext(patch.object(self.client, "_publish_raw", publish))
        self.on_request = lambda payload: (self.reply(payload)
                                           if threading.current_thread().name != "edit" else None)
        edit = threading.Thread(name="edit",
                                target=lambda: self.client.edit_room_setting(12, 5, 1))
        edit.start()
        self.assertTrue(read_base.wait(2))
        self.assertTrue(self.client.start_room("schlafzimmer", 0))
        edit.join(2)
        rooms = {r[0]: r for r in self.last_set()["params"]["room_preference"]}
        self.assertEqual((rooms[10][8], rooms[12][8]), (1, 0), "start's selection survives")
        self.assertEqual(rooms[12][5], 1, "and so does the edit")


if __name__ == "__main__":
    unittest.main()
