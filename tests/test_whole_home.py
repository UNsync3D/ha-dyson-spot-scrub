"""Whole-home clean: the legacy global START (Spot+Scrub AI hardware test, 12 Nov 2026).

The MyDyson app has no whole-home button; the robot's own button reports
currentCleaningMode "global" and work_mode 36. The global START gives the
same, and Dyson's cloud turns it into set_room_clean with no room IDs.
"""
import asyncio
import importlib
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_room_preferences
import test_service_targeting

CAPTURE = json.loads((Path(__file__).parent / "fixtures/whole_home_capture.json").read_text())


class HAError(Exception):
    pass


class WholeHomeClientTests(unittest.TestCase):
    publish = test_room_preferences.RoomSettingsTests.publish
    reply = test_room_preferences.RoomSettingsTests.reply

    def setUp(self):
        test_room_preferences.RoomSettingsTests.setUp(self)

    def test_sends_exactly_the_tested_start_and_nothing_else(self):
        before = dict(self.client._cached_preference)
        self.assertTrue(self.client.start_whole_home())
        self.assertEqual(len(self.sent), 1)
        topic, payload = self.sent[0]
        self.assertTrue(topic.endswith("/command"))
        sent = dict(payload, time=CAPTURE["sent"]["time"])
        self.assertEqual(sent, CAPTURE["sent"])
        self.assertEqual(self.client._cached_preference, before)   # room settings untouched

    def test_the_cloud_start_has_no_rooms(self):
        # What the cloud sent the robot: a room clean with an empty room list.
        self.assertEqual(CAPTURE["cloud_jdm"]["params"]["room_ids"], [])
        self.assertEqual(CAPTURE["current_state"]["currentCleaningMode"], "global")
        self.assertEqual(CAPTURE["robot_props"]["work_mode"], 36)

    def test_busy_robot_r2_and_concurrent_start_are_refused(self):
        self.client.state["state"] = "FULL_CLEAN_RUNNING"
        self.assertFalse(self.client.start_whole_home())
        self.client.state["state"] = "INACTIVE_CHARGING"
        self.client.state["dockState"] = "WASHING_MOP"
        self.assertFalse(self.client.start_whole_home())
        self.client.state["dockState"] = "IDLE"
        with self.client._start_lock:
            self.assertFalse(self.client.start_whole_home())
        self.client._prefix = "RB07"                      # not confirmed on the R2
        self.assertFalse(self.client.supports_whole_home)
        self.assertFalse(self.client.start_whole_home())
        self.assertEqual(self.sent, [])

    def test_drying_is_stopped_first(self):
        self.client.state["dockState"] = "DRYING_MOP"
        self.assertTrue(self.client.start_whole_home())
        self.assertEqual([p["msg"] for _, p in self.sent], ["ABORT-DOCK-ACTION", "START"])


class WholeHomeCoordinatorTests(unittest.TestCase):
    publish = test_room_preferences.RoomSettingsTests.publish
    reply = test_room_preferences.RoomSettingsTests.reply

    def setUp(self):
        test_room_preferences.RoomSettingsTests.setUp(self)
        self.enterContext(patch.dict(sys.modules, {
            "homeassistant.exceptions": SimpleNamespace(HomeAssistantError=HAError)}))
        module = importlib.import_module("room_test_integration.coordinator")
        self.hass = SimpleNamespace(async_add_executor_job=AsyncMock(return_value=True))
        self.coordinator = module.DysonCoordinator(
            self.hass, "token", "serial", "RB05", SimpleNamespace(data={}))
        self.robot = SimpleNamespace(
            available=True, supports_whole_home=True, start_whole_home=Mock(),
            state={"state": "INACTIVE_CHARGING", "dockState": "IDLE"})
        self.coordinator.mqtt = self.robot

    def test_starts_when_ready(self):
        asyncio.run(self.coordinator.async_clean_whole_home())
        self.hass.async_add_executor_job.assert_awaited_once_with(self.robot.start_whole_home)

    def test_refusals_say_why(self):
        cases = [
            ({"state": "FULL_CLEAN_RUNNING", "dockState": "IDLE"}, "cleaning job active"),
            ({"state": "INACTIVE_CHARGING", "dockState": "COLLECTING_DUST"}, "emptying bin"),
        ]
        for state, words in cases:
            with self.subTest(words):
                self.robot.state = state
                with self.assertRaises(HAError) as caught:
                    asyncio.run(self.coordinator.async_clean_whole_home())
                self.assertIn(words, str(caught.exception))
        self.robot.state = {"state": "INACTIVE_CHARGING", "dockState": "IDLE"}
        self.robot.supports_whole_home = False
        with self.assertRaises(HAError):
            asyncio.run(self.coordinator.async_clean_whole_home())
        self.hass.async_add_executor_job.assert_not_awaited()

    def test_a_running_room_sequence_blocks_it(self):
        async def run():
            self.coordinator._cleaning_task = asyncio.get_running_loop().create_future()
            with self.assertRaises(HAError) as caught:
                await self.coordinator.async_clean_whole_home()
            self.assertIn("sequence running", str(caught.exception))
        asyncio.run(run())

    def test_failed_publish_is_an_error(self):
        self.hass.async_add_executor_job.return_value = False
        with self.assertRaises(HAError):
            asyncio.run(self.coordinator.async_clean_whole_home())


class WholeHomeEntityTests(unittest.TestCase):
    publish = test_room_preferences.RoomSettingsTests.publish
    reply = test_room_preferences.RoomSettingsTests.reply

    def test_only_the_spot_scrub_gets_the_button(self):
        test_room_preferences.RoomSettingsTests.setUp(self)
        helpers = importlib.import_module("room_test_integration.entity_helpers")
        spot = SimpleNamespace(data={"product_type": "804", "mqtt_prefix": "RB05"})
        r2 = SimpleNamespace(data={"product_type": "914", "mqtt_prefix": "RB07"})
        self.assertTrue(helpers.supports_whole_home(spot))
        self.assertFalse(helpers.supports_whole_home(r2))


class WholeHomeServiceTests(unittest.TestCase):
    setUp = test_service_targeting.ServiceTargetingTests.setUp
    robot = test_service_targeting.ServiceTargetingTests.robot
    call = test_service_targeting.ServiceTargetingTests.call

    def test_targets_one_robot(self):
        for robot in self.robots.values():
            robot.async_clean_whole_home = AsyncMock()
        self.call("clean_whole_home", entity_id=["vacuum.robot_two"])
        self.robots["two"].async_clean_whole_home.assert_awaited_once()
        self.robots["one"].async_clean_whole_home.assert_not_awaited()
        with self.assertRaises(self.HAError):
            self.call("clean_whole_home", entity_id=["vacuum.robot_one", "vacuum.robot_two"])


if __name__ == "__main__":
    unittest.main()
