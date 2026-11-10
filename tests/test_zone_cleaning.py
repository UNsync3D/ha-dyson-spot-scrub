"""Zone prototype: exact wire format, targeting, guards and pixel calibration."""
import asyncio
from copy import deepcopy
import hashlib
import importlib
import io
import unittest
from unittest.mock import Mock, patch

import test_room_preferences as rooms
import test_service_targeting as targeting
import test_map_retention as retention


class ZoneCommandTests(unittest.TestCase):
    setUp = rooms.RoomSettingsTests.setUp
    publish = rooms.RoomSettingsTests.publish
    reply = rooms.RoomSettingsTests.reply

    def test_single_app_start_without_room_mutation_or_duplicate_jdm(self):
        before = deepcopy(self.client._cached_preference)
        self.assertTrue(self.client.start_zone("123", [-1, -4, 7, 1]))
        self.assertEqual(len(self.sent), 1)
        payload = self.sent[0][1]
        self.assertEqual(payload["msg"], "START")
        self.assertEqual(payload["cleaningMode"], "spotZoneConfigured")
        self.assertEqual(payload["cleaningProgramme"], {
            "persistentMapId": "123",
            "spotZones": [{"id": "1", "points": [
                {"x": -1, "y": -4}, {"x": -1, "y": 1},
                {"x": 7, "y": 1}, {"x": 7, "y": -4}]}],
            "defaultSpotZoneSettings": {"waterLevel": "low", "cleaningStrategy": "auto",
                "mopPasses": 1, "isUvScanOn": True, "cleanType": "vacuum", "dryPasses": 1},
        })
        self.assertEqual(self.client._cached_preference, before)

    def test_invalid_rectangles_and_stale_map_send_nothing(self):
        for rectangle in ([], [1, 2, 3], [0, 0, 0, 1], [2, 0, 1, 2],
                          [0, 0, float("nan"), 2], [0, 0, float("inf"), 2],
                          [False, 0, 1, 2], ["0", 0, 1, 2]):
            self.assertFalse(self.client.start_zone("123", rectangle))
        for map_id in (None, True, "", "0", "456"):
            self.assertFalse(self.client.start_zone(map_id, [0, 0, 1, 1]))
        self.assertEqual(self.sent, [])

    def test_busy_offline_and_concurrent_starts_are_blocked(self):
        for status in ("FULL_CLEAN_RUNNING", "FULL_CLEAN_PAUSED", "RETURNING_TO_BASE", "MAPPING"):
            self.client.state["state"] = status
            self.assertFalse(self.client.start_zone("123", [0, 0, 1, 1]))
        self.client.state["state"] = "INACTIVE_CHARGING"
        for dock in ("WASHING_MOP", "COLLECTING_DUST"):
            self.client.state["dockState"] = dock
            self.assertFalse(self.client.start_zone("123", [0, 0, 1, 1]))
        self.client.state["dockState"] = "IDLE"
        with self.client._start_lock:
            self.assertFalse(self.client.start_zone("123", [0, 0, 1, 1]))
        self.client.connected = False
        self.assertFalse(self.client.start_zone("123", [0, 0, 1, 1]))
        self.assertEqual(self.sent, [])

    def test_drying_abort_and_failed_transport(self):
        self.client.state["dockState"] = "DRYING_MOP"
        self.assertTrue(self.client.start_zone("123", [0, 0, 1, 1]))
        self.assertEqual([p["msg"] for _, p in self.sent], ["ABORT-DOCK-ACTION", "START"])
        with patch.object(self.client, "_publish", return_value=False) as send:
            self.assertFalse(self.client.start_zone("123", [0, 0, 1, 1]))
            self.assertEqual(send.call_count, 1)
        self.assertFalse(self.client._start_lock.locked())
        self.client.state["dockState"] = "IDLE"
        with patch.object(self.client, "_publish", return_value=False):
            self.assertFalse(self.client.start_zone("123", [0, 0, 1, 1]))


class ZoneServiceTests(unittest.TestCase):
    setUp = targeting.ServiceTargetingTests.setUp
    robot = targeting.ServiceTargetingTests.robot
    call = targeting.ServiceTargetingTests.call

    def test_explicit_target_and_map_validation(self):
        for robot in self.robots.values():
            robot.mqtt.state["persistentMapId"] = "123"
            robot.mqtt.start_zone = Mock(return_value=True)
        self.call("clean_zone", entity_id=["vacuum.robot_two"], map_id="123", rectangle=[0, 0, 1, 1])
        self.robots["two"].mqtt.start_zone.assert_called_once_with("123", [0, 0, 1, 1])
        self.robots["one"].mqtt.start_zone.assert_not_called()
        for target, map_id, rectangle in [
            (["vacuum.robot_one", "vacuum.robot_two"], "123", [0, 0, 1, 1]),
            (["vacuum.robot_one"], "456", [0, 0, 1, 1]),
            (["vacuum.robot_one"], "123", [0, 0, 0, 0]),
        ]:
            with self.assertRaises(self.HAError):
                self.call("clean_zone", entity_id=target, map_id=map_id, rectangle=rectangle)
        self.robots["one"].mqtt.start_zone.assert_not_called()


class ZoneCalibrationTests(unittest.TestCase):
    def test_round_trip_and_exact_png_dimensions(self):
        from PIL import Image
        renderer = importlib.import_module("retention_integration.map_renderer")
        for size in (8, 1000):
            calibration = {}
            data = {"id": 123, "zones": [{"id": 1, "name": "Room", "visited": [
                {"x": -1, "y": -4}, {"x": size, "y": 2}]}]}
            png = renderer.render_map(data, [], calibration=calibration, raise_errors=True)
            self.assertTrue(calibration)
            self.assertEqual(Image.open(io.BytesIO(png)).size,
                             (calibration["image_width"], calibration["image_height"]))
            self.assertEqual(hashlib.sha256(png).hexdigest(), calibration["image_sha256"])
            self.assertEqual(calibration["map_id"], "123")
            for x, y in ((-1, -4), (2, 1)):
                scale, pad = calibration["pixels_per_metre"], calibration["padding"]
                px = (x - calibration["min_x"]) * scale + pad
                py = (calibration["max_y"] - y) * scale + pad
                self.assertAlmostEqual((px-pad)/scale + calibration["min_x"], x)
                self.assertAlmostEqual(calibration["max_y"] - (py-pad)/scale, y)
            renderer.render_map(None, [], calibration=calibration)
            self.assertEqual(calibration, {})


class ZoneCameraTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = retention.RetentionTests.asyncSetUp
    state = retention.RetentionTests.state

    async def test_calibration_persists_with_fallback_and_resets_on_map_change(self):
        self.state("INACTIVE_CHARGING", map_id=123)
        self.camera._map_data = {"id": 123, "zones": [{"id": 1, "visited": [[0, 0], [4, 4]]}]}
        real_render = importlib.import_module("retention_integration.map_renderer").render_map
        with patch.object(retention.camera_module, "render_map", real_render):
            image = await self.camera.async_camera_image()
        calibration = self.camera.extra_state_attributes["map_calibration"]
        self.assertEqual(calibration["image_sha256"], hashlib.sha256(image).hexdigest())
        with patch.object(retention.camera_module, "render_map", side_effect=RuntimeError("test")):
            self.assertEqual(await self.camera.async_camera_image(), image)
        self.assertEqual(self.camera.extra_state_attributes["map_calibration"], calibration)
        self.state("INACTIVE_CHARGING", map_id=456)
        self.assertEqual(self.camera.extra_state_attributes, {})
