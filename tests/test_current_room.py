"""Current room (#53): the robot's position on the map's room outlines.

Fixtures: map_downstairs_4rooms.json (Spot+Scrub AI map) and
positions_dining_nov3.json, the robot's globalPosition reports from a clean of
Dining (room 12) on that map, 3 Nov 2026.
"""
import asyncio
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import test_room_preferences

HERE = Path(__file__).parent
MAP = json.loads((HERE / "fixtures/map_downstairs_4rooms.json").read_text())
POSITIONS = json.loads((HERE / "fixtures/positions_dining_nov3.json").read_text())["positions"]

_spec = importlib.util.spec_from_file_location(
    "room_locator", HERE.parent / "custom_components/dyson_spot_scrub/room_locator.py")
locator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(locator)


class LocatorTests(unittest.TestCase):
    def setUp(self):
        self.outlines = locator.room_outlines(MAP)

    def test_every_room_has_an_outline(self):
        self.assertEqual({zone_id for zone_id, _ in self.outlines}, {"10", "11", "12", "13"})

    def test_a_dining_clean_stays_in_dining(self):
        room, seen = None, []
        for x, y in POSITIONS:
            room = locator.locate(self.outlines, x, y, room)
            seen.append(room)
        self.assertEqual(set(seen), {"12"})

    def test_without_history_every_position_is_in_a_room(self):
        rooms = {locator.locate(self.outlines, x, y) for x, y in POSITIONS}
        self.assertNotIn(None, rooms)
        self.assertLessEqual(rooms, {"12", "13"})      # Dining, and just over the Kitchen line

    def test_room_centres(self):
        for zone in MAP["zones"]:
            with self.subTest(zone["name"]):
                p = zone["nameLocation"]
                self.assertEqual(locator.locate(self.outlines, p["x"], p["y"]), str(zone["id"]))

    def test_moving_well_into_another_room_switches(self):
        kitchen = next(z for z in MAP["zones"] if str(z["id"]) == "13")["nameLocation"]
        self.assertEqual(locator.locate(self.outlines, kitchen["x"], kitchen["y"], "12"), "13")

    def test_doorway_keeps_the_previous_room(self):
        self.assertEqual(locator.locate(self.outlines, 50, 50, "13"), "13")    # in no outline
        self.assertIsNone(locator.locate(self.outlines, 50, 50))

    def test_presentation_fallback(self):
        zone = deepcopy(MAP["zones"][0])
        zone["visited"] = []
        outlines = locator.room_outlines({"zones": [zone]})
        p = zone["nameLocation"]
        self.assertEqual(locator.locate(outlines, p["x"], p["y"]), str(zone["id"]))

    def test_latest_position(self):
        self.assertEqual(locator.latest_position({"globalPosition": [
            {"id": 1, "x": 0.1, "y": 0.2}, {"id": 2, "x": 0.3, "y": -0.4}]}), (0.3, -0.4))
        self.assertIsNone(locator.latest_position({}))
        self.assertIsNone(locator.latest_position({"globalPosition": [{"x": True, "y": 1}]}))

    def test_bad_map_data(self):
        self.assertEqual(locator.room_outlines(None), [])
        self.assertEqual(locator.room_outlines({"zones": [{"id": 1, "visited": [{"x": 0}]}]}), [])


class CoordinatorTests(unittest.TestCase):
    publish = test_room_preferences.RoomSettingsTests.publish
    reply = test_room_preferences.RoomSettingsTests.reply

    def setUp(self):
        test_room_preferences.RoomSettingsTests.setUp(self)
        self.module = importlib.import_module("room_test_integration.coordinator")
        self.get_map = AsyncMock(return_value=MAP)
        api = importlib.import_module("room_test_integration.dyson_api")
        api.get_map = self.get_map

    def make(self, loop):
        hass = SimpleNamespace(async_create_task=loop.create_task)
        coordinator = self.module.DysonCoordinator(hass, "token", "serial", "RB05",
                                                   SimpleNamespace(data={}))
        coordinator.mqtt = SimpleNamespace(room_preferences=[
            [12, '{"type":"dining","name":"Dining"}', 0], [13, "kitchen", 0]], available=True)
        coordinator._listeners = []
        return coordinator

    def moving(self, x, y):
        return {"state": "FULL_CLEAN_RUNNING", "dockState": "IDLE", "persistentMapId": "1792911875",
                "globalPosition": [{"id": 1, "x": x, "y": y, "angle": 0, "update": 0}]}

    def test_room_from_position_after_one_map_fetch(self):
        async def run():
            c = self.make(asyncio.get_running_loop())
            c._state_snapshot = self.moving(*POSITIONS[10])
            c._async_update_current_room()
            self.assertIsNone(c.current_room)            # outlines not fetched yet
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertEqual(c.current_room, "Dining")   # same name as the room entities
            self.assertEqual(c.current_room_state, "Dining")
            c._state_snapshot = self.moving(1.5, 1.0)    # Kitchen
            c._async_update_current_room()
            self.assertEqual(c.current_room, "Kitchen")
            self.get_map.assert_awaited_once()
            self.assertEqual(self.get_map.await_args.args[2], "1792911875")
        asyncio.run(run())

    def test_docked(self):
        async def run():
            c = self.make(asyncio.get_running_loop())
            c.set_room_outlines("1792911875", MAP)
            c._state_snapshot = self.moving(1.5, 1.0)
            c._async_update_current_room()
            self.assertEqual(c.current_room, "Kitchen")
            c._state_snapshot = {**self.moving(1.5, 1.0), "state": "INACTIVE_CHARGING"}
            c._async_update_current_room()
            self.assertIsNone(c.current_room)
            self.assertEqual(c.current_room_state, "Docked")
            self.get_map.assert_not_awaited()           # the camera's map was used
        asyncio.run(run())

    def test_failed_fetch_is_not_retried_straight_away(self):
        async def run():
            self.get_map.side_effect = RuntimeError("offline")
            c = self.make(asyncio.get_running_loop())
            c._state_snapshot = self.moving(1.5, 1.0)
            for _ in range(3):
                c._async_update_current_room()
                await asyncio.sleep(0)
            self.assertEqual(self.get_map.await_count, 1)
            self.assertIsNone(c.current_room_state)
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
