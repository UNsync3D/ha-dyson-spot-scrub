"""Replay sanitized stationless faults and room discovery without robot commands."""
from copy import deepcopy
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import test_faults as fault_tests
import test_room_preferences as room_tests
import test_entity_compat as compat

CAPTURE = json.loads((Path(__file__).parent / "fixtures/stationless-nov26.json").read_text(encoding="utf-8"))


class PlacementTests(unittest.TestCase):
    def setUp(self):
        fault_tests.FaultTests.setUp(self)

    def test_both_idle_placement_faults_keep_the_warning_but_allow_another_start_request(self):
        for label, sample in CAPTURE["states"].items():
            if label == "app_restart_after_lifted":
                continue
            with self.subTest(label=label):
                state = sample["state"]
                self.assertTrue(self.mqtt.has_fault(state))
                code = self.mqtt.active_fault_code(state)
                self.assertIn(code, {2509, 2510})
                self.assertIsNone(self.mqtt.start_block_reason(state))
                self.assertIn("before continuing", self.const.FAULT_CODE_MESSAGES[code])
        restarted = CAPTURE["states"]["app_restart_after_lifted"]["state"]
        self.assertFalse(self.mqtt.has_fault(restarted))
        self.assertEqual(self.mqtt.start_block_reason(restarted), "cleaning_job_active")

    def test_active_returning_unknown_and_other_severities_stay_blocked(self):
        idle = CAPTURE["states"]["lifted_off_dock"]["state"]
        for robot in ["FULL_CLEAN_RUNNING", "FULL_CLEAN_PAUSED", "ABORTED", "MAPPING", ""]:
            self.assertIsNotNone(self.mqtt.start_block_reason({**idle, "state": robot}))
        for severity in ["USER_RECOVERABLE", "USER", "HALT"]:
            state = {**idle, "activeFaults": [{"faultCode": "2510", "nextActionRequired": severity}]}
            self.assertEqual(self.mqtt.start_block_reason(state), "robot_fault")
        for code in [583, 9999]:
            state = {**idle, "activeFaults": [{"faultCode": str(code), "nextActionRequired": "USER_CONTINUE"}]}
            self.assertEqual(self.mqtt.start_block_reason(state), "robot_fault")
        self.assertEqual(self.mqtt.start_block_reason({**idle, "workMode": 37}), "robot_fault")
        self.assertEqual(self.mqtt.start_block_reason(idle, False), "disconnected")
        mixed = deepcopy(idle)
        mixed["activeFaults"].append({"faultCode": "583", "nextActionRequired": "USER_CONTINUE"})
        self.assertEqual(self.mqtt.start_block_reason(mixed), "robot_fault")

    def test_direct_and_paused_home_share_abort_and_recharge_commands(self):
        for run in CAPTURE["runs"]:
            commands = [row["message"].get("msg") or row["message"].get("method") for row in run["commands"]]
            expected = ["START", "ABORT", "service.start_recharge"]
            if run["run"] in (3, 4):
                expected.insert(1, "PAUSE")
            self.assertEqual(commands, expected)


class DiscoveryTests(unittest.TestCase):
    publish = room_tests.RoomSettingsTests.publish
    reply = room_tests.RoomSettingsTests.reply

    def setUp(self):
        room_tests.RoomSettingsTests.setUp(self)
        self.client.maps_enabled = True
        self.coordinator = importlib.import_module("room_test_integration.coordinator")
        self.c = self.coordinator.DysonCoordinator(SimpleNamespace(), "token", "serial", "RB05", SimpleNamespace(data={}))
        self.c.mqtt = self.client
        self.c.cached_room_names = ["Room A"]
        self.client.state["persistentMapId"] = "456"
        self.c._state_snapshot = dict(self.client.state)

    def vacuum(self):
        seen = {}
        def inspect(platform, entity):
            if platform == "vacuum":
                entity._coordinator = self.c
                seen["entity"] = entity
        compat.collect(str(compat.ROOT), inspect=inspect)
        return seen["entity"]

    def test_empty_new_map_is_explicit_and_never_reuses_a_cached_room_list(self):
        self.assertEqual(self.client.room_names, [])
        self.assertEqual(self.c.room_names, [])
        self.assertEqual(self.vacuum().extra_state_attributes["rooms"], [])
        self.client.state["persistentMapId"] = "0"
        self.assertEqual(self.c.room_names, [])
        self.assertEqual(self.vacuum().extra_state_attributes["rooms"], [])

    def test_metadata_creates_discoverable_room_names_before_settings_or_a_first_clean(self):
        notify = Mock()
        self.client.register_callback(notify)
        self.client.set_map_inventory("456", [{"id": 10, "name": "Room B"}])
        self.assertEqual(self.client.room_names, ["Room B"])
        self.assertEqual(self.c.room_names, ["Room B"])
        self.assertEqual(self.vacuum().extra_state_attributes["rooms"], ["Room B"])
        self.assertEqual(self.client.room_preferences, [], "metadata must not invent cleaning settings")
        notify.assert_called_once()
        self.client.set_map_inventory("123", [{"id": 10, "name": "Other floor"}])
        self.assertEqual(self.client.room_names, ["Room B"])
        self.assertEqual(self.sent, [])

    def test_mapping_completion_refreshes_preferences_and_inventory_without_starting(self):
        self.preferences = {"room": [[10, "Room B", 0, 0, 0, 0, 0, 0, 0, 0, 1, 0]], "uv_switch": []}
        changed = Mock()
        self.client.on_map_changed = changed
        for method in ["event.BuildMapFinish.post", "event.map_change.post", "service.rename_room"]:
            self.client._handle_jdm({"method": method, "code": 0, "data": {"result": 0}})
        self.assertEqual(self.client.room_names, ["Room B"])
        self.assertEqual(changed.call_count, 3)
        self.assertTrue(all(call.args == ("456",) for call in changed.call_args_list))
        self.assertFalse(any(p.get("msg") == "START" or p.get("method") in {
            "service.set_cur_map", "service.set_preference", "service.set_room_clean"} for _, p in self.sent))

    def test_catalogue_poll_retries_missing_preferences_until_the_robot_reports_them(self):
        self.on_request = lambda request: self.reply(request, {"room": []})
        changed = Mock()
        self.client.on_map_changed = changed
        for _ in range(2):
            self.client.request_maps()
            request = self.sent[-1][1]
            self.client._handle_jdm({"method": "service.get_map_list", "msgId": request["msgId"], "code": 0,
                "data": {"map_list": [{"id": 456, "name": "Floor B", "cur": True}]}})
        self.assertEqual(changed.call_count, 2)
        self.assertEqual(self.client.room_names, [])
        self.preferences = {"room": [[10, "Room B", 0, 0, 0, 0, 0, 0, 0, 0, 1, 0]]}
        self.on_request = lambda request: self.reply(request)
        self.client._refresh_current_map_rooms()
        self.assertEqual(self.client.room_names, ["Room B"])

    def test_vacuum_exposes_fault_instructions_and_readiness_together(self):
        self.c._state_snapshot = deepcopy(CAPTURE["states"]["lifted_off_dock"]["state"])
        entity = self.vacuum()
        attrs = entity.extra_state_attributes
        self.assertTrue(attrs["ready_to_start"])
        self.assertEqual(attrs["fault_code"], 2510)
        self.assertEqual(attrs["fault"], "Robot lifted. Place it on the floor before continuing.")

    def test_idle_lifted_fault_allows_the_normal_room_start_without_clearing_the_warning_optimistically(self):
        self.client.state = deepcopy(CAPTURE["states"]["lifted_off_dock"]["state"])
        self.preferences = {"room": [[10, "Room B", 0, 0, 0, 0, 0, 0, 0, 0, 1, 0]], "uv_switch": []}
        self.assertTrue(self.client.start_rooms_with_settings(["10"]))
        self.assertTrue(any(p.get("msg") == "START" for _, p in self.sent))
        self.assertEqual(self.client.state["activeFaults"], CAPTURE["states"]["lifted_off_dock"]["state"]["activeFaults"])


if __name__ == "__main__":
    unittest.main()
