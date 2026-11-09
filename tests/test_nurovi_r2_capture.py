"""Dyson R2 Nurovi Wash+Dry (RB07): checks against a real app capture.

Fixture: tests/fixtures/nurovi_r2_capture.json, from a Matterbridge debug log
(18 Oct 2026) of a one-room clean started in the MyDyson app.
"""
from copy import deepcopy
import importlib
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1] / "custom_components/dyson_spot_scrub"
CAPTURE = json.loads((Path(__file__).parent / "fixtures/nurovi_r2_capture.json").read_text())
MAP_ID = CAPTURE["app_start"]["cleaningProgramme"]["persistentMapId"]


def module(name, **attributes):
    result = ModuleType(name)
    result.__dict__.update(attributes)
    return result


def path_points(messages):
    """cur_path: [first point index, (x, y, heading, update) * n, unix time]."""
    points = []
    for message in messages:
        body = message[1:-1]
        for i in range(0, len(body) - 3, 4):
            x, y, _heading, update = body[i:i + 4]
            points.append({"x": x, "y": y, "update": update})
    return points


class R2StartTests(unittest.TestCase):
    def setUp(self):
        package = module("r2_test_integration")
        package.__path__ = [str(ROOT)]
        self.enterContext(patch.dict(sys.modules, {
            "r2_test_integration": package,
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
            "r2_test_integration.dyson_api": module(
                "r2_test_integration.dyson_api", DysonApiError=Exception,
                DysonRateLimitError=Exception,
                get_iot_credentials=AsyncMock(), get_live_map=AsyncMock(),
                get_map_metadata=AsyncMock(return_value=[])),
        }))
        self.mqtt = importlib.import_module("r2_test_integration.dyson_mqtt")
        self.client = self.mqtt.DysonMqttClient(
            "test-serial", "RB07", "example.invalid", "token", "signature",
            "client-id", "authorizer")
        self.client.connected = True
        self.client.state = {"persistentMapId": MAP_ID, "state": "INACTIVE_CHARGING",
                             "dockState": "IDLE"}
        self.sent = []
        self.enterContext(patch.object(self.client, "_publish_raw", self.publish))
        self.enterContext(patch.object(self.mqtt, "_PREFERENCE_TIMEOUT", 0.05))
        self.enterContext(patch.object(self.mqtt, "_SETTINGS_CONFIRM_TIMEOUT", 0.2))

    def publish(self, topic, payload):
        self.sent.append((topic, deepcopy(payload)))
        method = payload.get("method")
        if method == "service.get_preference":
            self.client._track_preference_request(
                payload.get("msgId"), payload["params"].get("map_id"))
            reply = {"msgId": payload["msgId"], "method": method, "code": 0,
                     "data": deepcopy(CAPTURE["get_preference_reply"])}
            self.client._on_message(None, None, SimpleNamespace(
                topic="RB07/test-serial/status/jdm", payload=json.dumps(reply).encode()))
        elif method == "service.set_preference" and payload.get("msgId") in self.client._reply_waiters:
            self.client._handle_jdm({"msgId": payload["msgId"], "code": 0, "method": method,
                                     "data": {"result": 0}})
        return True

    def test_room_start_follows_the_app(self):
        app_rows = CAPTURE["app_set_preference"]["room_preference"]
        # The robot calls it "room1"; Home Assistant shows "Room 1".
        self.assertTrue(self.client.start_room("Room 1", app_rows[0][3]))
        commands = [p.get("method", p.get("msg")) for _, p in self.sent]
        # Like the app: save settings, then START. The cloud adds
        # set_cur_map and set_room_clean (both seen in the capture).
        self.assertEqual(commands, ["service.get_preference", "service.set_preference", "START"])
        self.assertEqual([c["method"] for c in CAPTURE["cloud_jdm"]],
                         ["service.set_cur_map", "service.set_room_clean"])

        rows = next(p for _, p in self.sent
                    if p.get("method") == "service.set_preference")["params"]["room_preference"]
        reply_rows = CAPTURE["get_preference_reply"]["room"]
        # The robot reports 12 values per room; the app sends 11 (also 8 Nov 2026).
        self.assertEqual([len(r) for r in reply_rows], [12] * len(reply_rows))
        self.assertEqual([len(r) for r in rows], [len(r) for r in app_rows])
        self.assertEqual([r[8] for r in rows], [r[8] for r in app_rows])        # only room1 selected
        self.assertEqual(rows[0][:4], app_rows[0][:4])                          # room1 vacuum+mop

        start, app_start = self.sent[-1][1], CAPTURE["app_start"]
        self.assertEqual(start["cleaningMode"], app_start["cleaningMode"])
        self.assertEqual(start["cleaningProgramme"]["persistentMapId"], MAP_ID)
        self.assertEqual(start["cleaningProgramme"]["unorderedZones"],
                         app_start["cleaningProgramme"]["unorderedZones"])
        self.assertLessEqual(set(start) - {"time"}, set(app_start))


class R2PathTests(unittest.TestCase):
    def setUp(self):
        package = module("r2_path_pkg")
        package.__path__ = [str(ROOT)]
        sys.modules.setdefault("r2_path_pkg", package)
        spec = importlib.util.spec_from_file_location("r2_path_pkg.map_renderer", ROOT / "map_renderer.py")
        self.renderer = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.renderer
        spec.loader.exec_module(self.renderer)

    def test_travel_is_hidden_and_vacuum_and_mop_is_one_colour(self):
        cp = path_points(CAPTURE["cur_path"])
        codes = {p["update"] for p in cp}
        self.assertEqual(codes, {0, 3})            # travel and vacuum+mop only
        pts = [(p["x"] * 20, p["y"] * 20) for p in cp]
        runs = self.renderer._path_runs(cp, pts, [None] * (len(pts) - 1))
        self.assertEqual({mode for mode, _ in runs}, {"VACUUMING_AND_MOPPING"})
        # Each drawn stretch starts from the point before it, then every
        # vacuum+mop point; no travel point is drawn otherwise.
        drawn = sum(len(run) for _, run in runs)
        self.assertEqual(drawn - len(runs), sum(1 for p in cp if p["update"] == 3))
        stretches = sum(1 for a, b in zip([None] + cp, cp) if b["update"] == 3
                        and (a is None or a["update"] != 3))
        self.assertEqual(len(runs), stretches)


if __name__ == "__main__":
    unittest.main()
