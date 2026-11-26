"""Offline map selection using the 25/26 November RB05 command shapes."""
import asyncio
from copy import deepcopy
import importlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import test_room_preferences as room_tests
module = room_tests.module

ROOT = Path(__file__).resolve().parents[1] / "custom_components/dyson_spot_scrub"
spec = importlib.util.spec_from_file_location("map_values", ROOT / "maps.py")
values = importlib.util.module_from_spec(spec)
spec.loader.exec_module(values)
MAPS = [{"id": 123, "name": "Wohnung", "cur": True},
        {"id": 456, "name": "Dachboden", "cur": False}]
CAPTURE = json.loads((Path(__file__).parent / "fixtures/map-switching-nov26.json").read_text(encoding="utf-8"))


class CatalogueTests(unittest.TestCase):
    def test_duplicate_names_and_colliding_labels_keep_distinct_ids(self):
        rows = [{"id": 1, "name": "Home", "cur": True},
                {"id": 2, "name": "Home", "cur": False},
                {"id": 3, "name": "Home [1]", "cur": False}]
        parsed = values.map_catalogue(rows)
        self.assertEqual(len({item["option"] for item in parsed}), 3)
        self.assertEqual([item["map_id"] for item in parsed], ["1", "2", "3"])

    def test_invalid_or_ambiguous_full_lists_are_rejected(self):
        for raw in [None, {}, [None], [{"id": True, "name": "X", "cur": True}],
                    [{"id": 0, "name": "X", "cur": False}], MAPS + [MAPS[0]],
                    [{**item, "cur": True} for item in MAPS],
                    [{**MAPS[0], "name": " "}], [{**MAPS[0], "id": 1.2}],
                    [{**MAPS[0], "id": "9007199254740992"}]]:
            with self.subTest(raw=raw):
                self.assertIsNone(values.map_catalogue(raw))
        self.assertEqual(values.map_catalogue([]), [])


class MapSwitchTests(unittest.TestCase):
    def setUp(self):
        room_tests.RoomSettingsTests.setUp(self)
        self.client.maps_enabled = True
        self.ack = 0
        self.result = 0
        self.confirm = True
        self.clean_race = None
        self.telemetry_hook = None
        self.feed_list(MAPS)
        self.sent.clear()

    def publish(self, topic, payload):
        if payload.get("method") == "service.set_cur_map":
            self.sent.append((topic, deepcopy(payload)))
            self.assertTrue(self.client.map_switching)
            self.clean_race = self.client.start_whole_home()
            if self.ack is not None:
                self.client._handle_jdm({"method": "service.set_cur_map",
                    "msgId": payload["msgId"], "code": self.ack,
                    "data": {"result": self.result}})
            if self.telemetry_hook:
                self.telemetry_hook(payload)
            if self.confirm:
                self.client._handle_status({"msg": "CURRENT-STATE",
                    "persistentMapId": str(payload["params"]["map_id"])})
            return True
        return room_tests.RoomSettingsTests.publish(self, topic, payload)

    def reply(self, *args, **kwargs):
        return room_tests.RoomSettingsTests.reply(self, *args, **kwargs)

    def feed_list(self, maps, code=0):
        self.client.request_maps()
        request = self.sent[-1][1]
        self.client._handle_jdm({"method": "service.get_map_list",
            "msgId": request["msgId"], "code": code, "data": {"map_list": maps}})

    def test_select_sends_one_map_command_and_waits_for_telemetry(self):
        self.assertTrue(self.client.select_map("456", timeout=0.01))
        writes = [p for _, p in self.sent if p.get("method") == "service.set_cur_map"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]["params"], {"map_id": 456})
        self.assertFalse(self.clean_race)
        self.assertFalse(self.client.map_switching)
        self.assertEqual(self.client.state["persistentMapId"], "456")
        self.assertFalse(any(p.get("msg") == "START" or p.get("method") in {
            "service.set_preference", "service.set_room_clean", "service.del_map"}
            for _, p in self.sent))

    def test_acknowledgement_alone_does_not_invent_state_or_retry(self):
        self.confirm = False
        self.assertFalse(self.client.select_map("456", timeout=0.001))
        self.assertEqual(self.client.state["persistentMapId"], "123")
        self.assertFalse(self.client.map_switching)
        self.client._handle_status({"msg": "CURRENT-STATE", "persistentMapId": "456"})
        self.assertEqual(self.client.state["persistentMapId"], "456")
        self.assertEqual(sum(p.get("method") == "service.set_cur_map" for _, p in self.sent), 1)

    def test_live_capture_confirms_from_jdm_before_delayed_legacy_state(self):
        messages = {item["line"]: item["message"] for item in CAPTURE["switch"]}
        self.client.state = {**self.client.state, **messages[125]}
        self.feed_list([{"id":1784483068,"name":"Wohnung","cur":False},
                        {"id":1795654677,"name":"Karte1","cur":True}])
        changed=[]
        self.client.on_map_changed=changed.append
        self.confirm=False
        def report(_payload):
            # The first reply to REQUEST-CURRENT-STATE still contains the old map.
            self.client._handle_jdm(messages[124])
            self.client._handle_status(messages[125])
            # This real property report arrives ~0.75 s after the request.
            self.client._handle_jdm(messages[126])
        self.telemetry_hook=report
        self.assertTrue(self.client.select_map("1784483068",timeout=0.01))
        self.assertEqual(self.client.state["persistentMapId"],"1784483068")
        self.assertEqual(changed[-1],"1784483068")
        count=changed.count("1784483068")
        self.client._handle_status(messages[148])
        self.assertEqual(changed.count("1784483068"),count)

    def test_map_property_validation_and_zero_do_not_choose_a_saved_map(self):
        for invalid in [True,False,-1,1.5,"456",None,9007199254740992]:
            self.client._handle_jdm({"method":"prop.post","params":{"current_map_id":invalid}})
            self.assertEqual(self.client.state["persistentMapId"],"123")
        zero=next(item["message"] for item in CAPTURE["return"] if item["line"]==611)
        self.client._handle_jdm(zero)
        self.assertEqual(self.client.state["persistentMapId"],"0")
        self.assertNotIn("0",[item["map_id"] for item in self.client.maps])

    def test_partial_property_get_updates_map_but_failed_get_does_not(self):
        self.client._handle_jdm({"method":"prop.get","code":2,"data":{"current_map_id":456}})
        self.assertEqual(self.client.state["persistentMapId"],"123")
        self.client._handle_jdm({"method":"prop.get","code":1,"data":{"current_map_id":456}})
        self.assertEqual(self.client.state["persistentMapId"],"456")
        self.assertEqual(self.client._prefs_map_id,"456")

    def test_reply_rejection_missing_reply_and_failure_result(self):
        self.confirm = False
        for ack, result in [(1, 0), (None, 0), (0, 1), (False, 0), (0, None)]:
            self.ack, self.result = ack, result
            self.assertFalse(self.client.select_map("456", timeout=0.001))
            self.assertFalse(self.client.map_switching)
            self.assertFalse(self.client._reply_waiters)

    def test_busy_offline_unsupported_or_unknown_maps_send_nothing(self):
        for robot in ["FULL_CLEAN_RUNNING", "FULL_CLEAN_PAUSED", "ABORTED", "MAPPING_RUNNING", ""]:
            self.client.state["state"] = robot
            self.assertFalse(self.client.select_map("456", timeout=0.001))
        self.client.state["state"] = "INACTIVE_CHARGING"
        for identity in ["999", "0", None, True]:
            self.assertFalse(self.client.select_map(identity, timeout=0.001))
        self.client.robot_online = False
        self.assertFalse(self.client.select_map("456", timeout=0.001))
        self.client.robot_online = True
        self.client._prefix = "RB07"
        self.assertFalse(self.client.select_map("456", timeout=0.001))
        self.assertEqual(self.sent, [])

    def test_deleted_map_removed_rename_keeps_id_and_errors_keep_last_list(self):
        self.feed_list([{**MAPS[0], "name": "Renamed"}])
        self.assertEqual(self.client.maps, [{"map_id": "123", "name": "Renamed", "option": "Renamed"}])
        self.feed_list(None)
        self.feed_list([], code=1)
        self.assertEqual(len(self.client.maps), 1)
        self.assertFalse(self.client.select_map("456", timeout=0.001))
        self.feed_list([])
        self.assertEqual(self.client.maps, [])
        self.assertTrue(self.client.maps_known)

    def test_schedule_writes_recheck_map_and_share_the_map_switch_lock(self):
        observed = []
        def write(method, params, timeout):
            observed.append((method, params))
            self.assertTrue(self.client._start_lock.locked())
            self.assertFalse(self.client.select_map("456", timeout=0.001))
            return 0
        with patch.object(self.client, "_send_order_command", write):
            self.assertEqual(self.client.send_order_command("service.add_order", {"map_id": 123}), 0)
            self.assertIsNone(self.client.send_order_command("service.add_order", {"map_id": 456}))
            self.assertIsNone(self.client.send_order_command("service.del_order", {"id": 1}, expected_map_id="456"))
            self.assertEqual(self.client.send_order_command("service.del_order", {"id": 1}, expected_map_id="123"), 0)
            self.client.state["persistentMapId"] = "0"
            self.assertIsNone(self.client.send_order_command("service.add_order", {"map_id": 123}))
        self.assertEqual(len(observed), 2)
        self.assertEqual(self.sent, [])
        self.assertFalse(self.client._start_lock.locked())

    def test_older_list_reply_cannot_resurrect_deleted_map(self):
        self.client.request_maps()
        old = self.sent[-1][1]
        self.feed_list([MAPS[0]])
        self.client._handle_jdm({"method": "service.get_map_list", "code": 0,
            "msgId": old["msgId"], "data": {"map_list": MAPS}})
        self.assertEqual(len(self.client.maps), 1)

    def test_app_list_requests_and_mutations_refresh_the_same_catalogue(self):
        self.client._track_map_request("app-list")
        self.client._handle_jdm({"method": "service.get_map_list", "code": 0,
            "msgId": "app-list", "data": {"map_list": [MAPS[0]]}})
        self.assertEqual(len(self.client.maps), 1)
        for method in ["service.del_map", "service.rename_map", "event.BuildMapFinish.post"]:
            self.client._handle_jdm({"method": method, "code": 0, "data": {"result": 0}})
        self.assertEqual(sum(p.get("method") == "service.get_map_list" for _, p in self.sent), 3)

    def test_poll_registration_reconnect_and_shutdown(self):
        import sys
        import types
        unsub = unittest.mock.Mock()
        track = unittest.mock.Mock(return_value=unsub)
        with patch.dict(sys.modules, {"homeassistant.helpers.event": module(
                "homeassistant.helpers.event", async_track_time_interval=track)}):
            coordinator = importlib.import_module("room_test_integration.coordinator")
            hass = SimpleNamespace(loop=None)
            c = coordinator.DysonCoordinator(hass, "token", "test", "RB05", SimpleNamespace(data={}))
            c.mqtt = self.client
            async def run():
                await c.async_setup_maps()
                await c.async_setup_maps()
                await track.call_args.args[1](None)
            asyncio.run(run())
            self.assertEqual(track.call_count, 1)
            self.assertEqual(track.call_args.args[2].total_seconds(), 60)
            self.assertEqual(sum(p.get("method") == "service.get_map_list" for _, p in self.sent), 2)
            replacement = SimpleNamespace(request_maps=unittest.mock.Mock(), disconnect=unittest.mock.Mock())
            c.mqtt = replacement
            c._async_request_maps()
            self.assertTrue(replacement.maps_enabled)
            replacement.request_maps.assert_called_once()
            c._async_cancel_cleaning_task = AsyncMock()
            async def executor(function):
                return function()
            hass.async_add_executor_job = executor
            asyncio.run(c.async_shutdown())
            unsub.assert_called_once()
            replacement.disconnect.assert_called_once()

    def test_select_entity_reports_maps_and_confirms_current_telemetry(self):
        import test_entity_compat as compat
        seen = {}
        display = compat.display_name
        def capture(platform, entity):
            if type(entity).__name__ == "DysonMapSelectEntity":
                seen["class"] = type(entity)
            return display(platform, entity)
        with patch.object(compat, "display_name", capture):
            compat.collect(str(ROOT))
        coordinator = SimpleNamespace(mqtt=self.client,state=self.client.state,
            sequence_running=False,async_select_map=AsyncMock())
        entry = SimpleNamespace(data={"serial":"test-serial","device_name":"Robot","product_type":"804"})
        entity = seen["class"](coordinator,entry)
        self.assertEqual(entity._attr_unique_id,"test-serial_active_map")
        self.assertEqual(entity.current_option,"Wohnung")
        self.assertEqual(entity.options,["Wohnung","Dachboden"])
        self.assertTrue(entity.extra_state_attributes["can_change"])
        asyncio.run(entity.async_select_option("Dachboden"))
        coordinator.async_select_map.assert_awaited_once_with("456")
        self.assertEqual(entity.current_option,"Wohnung")
        coordinator.state={**coordinator.state,"persistentMapId":"456"}
        self.assertEqual(entity.current_option,"Dachboden")
        self.client.robot_online=False
        self.assertFalse(entity.available)
        self.assertFalse(entity.extra_state_attributes["can_change"])
        with self.assertRaises(RuntimeError):
            asyncio.run(entity.async_select_option("Wohnung"))


if __name__ == "__main__":
    unittest.main()
