"""Room inventory vs. cleaning preferences - replays of real captures.

get_preference is not a complete room inventory:
  * FutuRazor, 26 Oct 2026: after a scheduled start (created in the Dyson app
    for one room), replies contain ONLY the scheduled room, on the same map,
    and stay that way after another room is cleaned.
  * UNsync3D, 25 Oct 2026: after building a new map, the app requests
    preferences for both the old map ("Home", 3 rooms) and the new one
    ("Map1", 0 rooms); the Home reply must not become Map1's room list.

Messages below are taken from the sanitized logs (IDs/arrays preserved) and
fed through the real DysonMqttClient._on_message. Only paho is stubbed.
Run: python -m unittest discover -s tests -v
"""
import asyncio
import importlib
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1] / "custom_components/dyson_spot_scrub"
MAP = 1000000001
SERIAL = "ROBOT_SERIAL"

# FutuRazor capture 1, 23:56:15 - full reply before the schedule runs.
FULL_ROOMS = [[13, "Room 13", 0, 0, 0, 0, 0, 0, 1, 0, 1, 0],
              [12, "Room 12", 0, 0, 0, 2, 0, 0, 0, 0, 2, 0],
              [10, "Room 10", 0, 0, 0, 0, 0, 0, 0, 0, 3, 0],
              [11, "Room 11", 0, 0, 0, 0, 0, 0, 0, 0, 4, 0],
              [14, "Room 14", 0, 0, 0, 0, 0, 0, 0, 0, 5, 0]]
FULL_UV = [[13, 1], [12, 1], [10, 1], [11, 1], [14, 1]]
# 23:57:08 - after the scheduled start: only Room 12.
PARTIAL_12 = [[12, "Room 12", 0, 0, 0, 2, 0, 0, 1, 0, 0, 0]]
# Second test: only Kitchen (room 13) after its scheduled start.
PARTIAL_13 = [[13, "Room 13", 0, 0, 0, 0, 0, 0, 1, 0, 0, 0]]


def module(name, **attributes):
    result = ModuleType(name)
    result.__dict__.update(attributes)
    return result


class InventoryFixture(unittest.TestCase):
    def setUp(self):
        package = module("inventory_integration")
        package.__path__ = [str(ROOT)]
        self.enterContext(patch.dict(sys.modules, {
            "inventory_integration": package,
            "paho": module("paho"), "paho.mqtt": module("paho.mqtt"),
            "paho.mqtt.client": module("paho.mqtt.client"),
        }))
        self.mqtt = importlib.import_module("inventory_integration.dyson_mqtt")
        self.enterContext(patch.object(self.mqtt, "_PREFERENCE_TIMEOUT", 0.5))
        self.client = self.mqtt.DysonMqttClient(SERIAL, "RB05", "e", "t", "s", "c", "a")
        self.client.connected = True
        self.client.state = {"persistentMapId": str(MAP), "state": "INACTIVE_CHARGING",
                             "dockState": "IDLE"}
        self.sent = []
        self.robot_answers = None  # callable(request) -> rooms, for our own requests

        def publish(topic, payload):
            self.sent.append(payload)
            if payload.get("method") == "service.get_preference":
                # As the real _publish_raw does: remember which map we asked about.
                self.client._track_preference_request(
                    payload.get("msgId"), payload["params"].get("map_id"))
            if (payload.get("method") == "service.get_preference"
                    and self.robot_answers is not None):
                self.client._handle_jdm({
                    "msgId": payload["msgId"], "code": 0, "method": "service.get_preference",
                    "data": {"room": self.robot_answers(payload), "material": [],
                             "uv_switch": [], "prefer_on": 1}})
            return True
        self.enterContext(patch.object(self.client, "_publish_raw", side_effect=publish))

    # Raw MQTT messages, as the app and robot send them -------------------------

    def message(self, topic_tail, data):
        self.client._on_message(None, None, SimpleNamespace(
            topic=f"RB05/{SERIAL}/{topic_tail}", payload=json.dumps(data).encode()))

    def app_requests(self, msg_id, map_id=MAP):
        self.message("command/jdm", {"msgId": msg_id, "version": "1.0.1",
                                     "method": "service.get_preference",
                                     "params": {"map_id": map_id}})

    def robot_replies(self, msg_id, rooms, uv=None):
        self.message("status/jdm", {"msgId": msg_id, "code": 0,
                                    "method": "service.get_preference",
                                    "data": {"room": rooms, "material": [],
                                             "uv_switch": uv or [], "prefer_on": 1}})

    def names(self):
        return self.client.room_names

    def sets(self):
        return [p["params"] for p in self.sent if p.get("method") == "service.set_preference"]


class ScheduledCleaningTests(InventoryFixture):
    def capture_one(self):
        self.app_requests("569902156")
        self.robot_replies("569902156", FULL_ROOMS, FULL_UV)     # 23:56:15
        self.app_requests("260125533")
        self.robot_replies("260125533", PARTIAL_12, [[12, 1]])   # 23:57:08

    def test_partial_reply_keeps_unselected_rooms(self):
        self.capture_one()
        self.assertEqual(self.names(),
                         ["Room 13", "Room 12", "Room 10", "Room 11", "Room 14"])
        by_id = {str(r[0]): r for r in self.client.room_preferences}
        self.assertEqual(by_id["12"], PARTIAL_12[0], "included room is updated")
        self.assertEqual(by_id["10"], FULL_ROOMS[2], "omitted rooms are untouched")
        self.app_requests("435075278")
        self.robot_replies("435075278", PARTIAL_12, [[12, 1]])   # 23:58:08
        self.assertEqual(len(self.names()), 5)

    def test_hidden_room_can_be_started_with_full_list(self):
        # Second test: Kitchen (13) scheduled, then a Hallway (12) start.
        self.app_requests("1")
        self.robot_replies("1", FULL_ROOMS, FULL_UV)
        self.app_requests("2")
        self.robot_replies("2", PARTIAL_13)
        self.robot_answers = lambda request: PARTIAL_13       # robot stays partial
        self.assertTrue(self.client.start_rooms_with_settings(["12"]))
        sent = self.sets()[-1]
        self.assertEqual(sorted(r[0] for r in sent["room_preference"]), [10, 11, 12, 13, 14],
                         "outgoing set_preference carries every room, like the app")
        selected = [r[0] for r in sent["room_preference"] if r[8] == 1]
        self.assertEqual(selected, [12])
        self.assertEqual(len(sent["uv_switch"]), 5)

    def test_edit_during_partial_state_sends_full_list(self):
        self.capture_one()
        self.assertTrue(self.client.edit_room_setting(12, 3, 2))
        self.assertEqual(len(self.sets()[-1]["room_preference"]), 5)

    def test_malformed_rows_are_not_stored(self):
        self.capture_one()
        self.robot_replies("x", [[10, "Room 10"], "junk", [[11], "R", 0, 0, 0, 0, 0, 0, 0, 0, 1]])
        self.assertEqual(self.client.room_preferences[2], FULL_ROOMS[2])


class MapCorrelationTests(InventoryFixture):
    HOME, MAP1 = 1787955355, 1792911875
    HOME_ROOMS = [[12, '{"type":"dining","name":"Dining"}', 3, 2, 1, 2, 1, 0, 1, 0, 2, 0],
                  [10, "Living room", 3, 3, 1, 2, 1, 0, 1, 0, 3, 0],
                  [13, '{"type":"toilet","name":"Toilet"}', 3, 1, 1, 2, 1, 0, 1, 0, 4, 0]]

    def test_new_map_does_not_show_previous_maps_rooms(self):
        # 25 Oct: the robot switches to Map1; the app asks about both maps.
        self.client.state["persistentMapId"] = str(self.MAP1)
        self.app_requests("653808619", self.HOME)
        self.app_requests("895521948", self.MAP1)
        self.robot_replies("895521948", [])
        self.robot_replies("653808619", self.HOME_ROOMS)
        self.assertEqual(self.names(), [], "Map1 has no rooms yet")
        self.client.state["persistentMapId"] = str(self.HOME)
        self.assertEqual(self.names(), ["Dining", "Living Room", "Toilet"])

    def test_map_change_triggers_fetch_for_new_map(self):
        self.message("status", {"msg": "CURRENT-STATE", "persistentMapId": "0"})
        changed = []
        self.client.on_map_changed = changed.append
        self.message("status", {"msg": "CURRENT-STATE", "persistentMapId": str(self.MAP1)})
        requested = [p["params"]["map_id"] for p in self.sent
                     if p.get("method") == "service.get_preference"]
        self.assertIn(self.MAP1, requested)
        self.assertEqual(changed, [str(self.MAP1)])
        # Same map again: no repeat fetch.
        before = len(self.sent)
        self.message("status", {"msg": "CURRENT-STATE", "persistentMapId": str(self.MAP1)})
        self.assertEqual(len(self.sent), before)

    # Replies to requests we never saw (sent before we subscribed, or missed).
    # The reply doesn't name its map and room IDs repeat across maps, so these
    # are only trusted when every room matches the current map (FutuRazor, #24).

    def test_unseen_request_with_nothing_known_is_ignored(self):
        self.client.state["persistentMapId"] = str(self.MAP1)
        self.robot_replies("never-seen", self.HOME_ROOMS)     # e.g. the old map's
        self.assertEqual(self.names(), [], "old map's names not shown")

    def test_unseen_request_matching_current_rooms_updates_settings(self):
        self.app_requests("seen", MAP)
        self.robot_replies("seen", FULL_ROOMS)
        updated = [list(PARTIAL_12[0])]
        updated[0][5] = 1                                       # water level changed
        self.robot_replies("never-seen", updated)
        row = next(r for r in self.client._cached_preference["room"] if r[0] == 12)
        self.assertEqual(row[5], 1)
        self.assertEqual(len(self.names()), 5)

    def test_unseen_request_from_another_map_is_ignored(self):
        self.app_requests("seen", MAP)
        self.robot_replies("seen", FULL_ROOMS)
        other_map = [[10, "Hallway", 0, 0, 0, 0, 0, 0, 0, 0, 1, 0]]  # same ID, other name
        self.robot_replies("never-seen", other_map)
        self.assertIn("Room 10", self.names())
        self.assertNotIn("Hallway", self.names())
        extra = [[15, "Garage", 0, 0, 0, 0, 0, 0, 0, 0, 6, 0]]       # ID unknown here
        self.robot_replies("never-seen-2", extra)
        self.assertNotIn("Garage", self.names())

    def test_unseen_request_checked_against_map_metadata(self):
        self.client.set_map_inventory(MAP, [{"id": "10", "name": "Room10"},
                                            {"id": "12", "name": "Room 12"}])
        self.robot_replies("never-seen", [FULL_ROOMS[1], FULL_ROOMS[2]])  # 12 and 10
        self.assertEqual(sorted(self.names()), ["Room 10", "Room 12"],
                         "names compared after normalising (Room10 = Room 10)")
        self.robot_replies("never-seen-2", [[11, "Room 11", 0, 0, 0, 0, 0, 0, 0, 0, 4, 0]])
        self.assertNotIn("Room 11", self.names(), "not in this map's metadata")


class InventoryRemovalTests(InventoryFixture):
    def zones(self, *ids):
        return [{"id": str(i), "name": f"Room {i}"} for i in ids]

    def setUp(self):
        super().setUp()
        self.app_requests("a")
        self.robot_replies("a", FULL_ROOMS, FULL_UV)

    def test_room_removed_only_by_map_metadata(self):
        self.client.set_map_inventory(MAP, self.zones(10, 11, 12, 13))
        self.assertNotIn("Room 14", self.names())
        self.assertEqual(len(self.client._cached_preference["uv_switch"]), 4)
        self.robot_replies("b", PARTIAL_12)
        self.assertEqual(len(self.names()), 4)

    def test_mismatched_metadata_removes_nothing(self):
        self.client.set_map_inventory(MAP, self.zones(901, 902))
        self.assertEqual(len(self.names()), 5)

    def test_empty_or_other_map_metadata_removes_nothing(self):
        self.client.set_map_inventory(MAP, [])
        self.client.set_map_inventory(42, self.zones(10))
        self.assertEqual(len(self.names()), 5)


class CoordinatorInventoryTests(unittest.TestCase):
    def setUp(self):
        package = module("inventory_coord")
        package.__path__ = [str(ROOT)]
        self.maps = [{"id": str(MAP), "zones": [{"id": "10", "name": "Room 10"}]}]
        self.get_meta = AsyncMock(side_effect=lambda *a, **k: self.maps)
        self.enterContext(patch.dict(sys.modules, {
            "inventory_coord": package,
            "inventory_coord.dyson_mqtt": module(
                "m", DysonMqttClient=object, is_any_cleaning=None, start_block_reason=None),
            "inventory_coord.dyson_api": module(
                "a", DysonApiError=Exception, DysonRateLimitError=Exception,
                get_iot_credentials=AsyncMock(), get_live_map=AsyncMock(),
                get_map_metadata=self.get_meta),
            "aiohttp": module("aiohttp", ClientSession=MagicMock(), TCPConnector=MagicMock()),
            "homeassistant": module("homeassistant"),
            "homeassistant.config_entries": module("ce", ConfigEntry=object),
            "homeassistant.core": module("core", HomeAssistant=object, callback=lambda f: f),
            "homeassistant.helpers": module("homeassistant.helpers"),
            "homeassistant.helpers.aiohttp_client": module(
                "ac", async_get_clientsession=lambda hass: None),
        }))
        coordinator_module = importlib.import_module("inventory_coord.coordinator")
        self.coordinator = coordinator_module.DysonCoordinator(
            SimpleNamespace(), "token", SERIAL, "RB05", SimpleNamespace(data={}))
        self.inventory = []
        self.coordinator.mqtt = SimpleNamespace(
            set_map_inventory=lambda map_id, zones: self.inventory.append((map_id, zones)))

    def test_refresh_passes_metadata_to_client(self):
        asyncio.run(self.coordinator.async_refresh_map_inventory())
        self.assertEqual(self.inventory, [(str(MAP), [{"id": "10", "name": "Room 10"}])])

    def test_refresh_failure_changes_nothing(self):
        self.get_meta.side_effect = RuntimeError("HTTP 503")
        asyncio.run(self.coordinator.async_refresh_map_inventory())
        self.assertEqual(self.inventory, [])


if __name__ == "__main__":
    unittest.main()
