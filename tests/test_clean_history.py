"""Cleaning history (issue #26): API call, parsing, coordinator refresh, sensors.

The sample records are trimmed from a real MyDyson capture of
GET /v2/{serial}/clean-maps; download links are replaced with fakes.
Run: python -m unittest discover -s tests -v
"""
import asyncio
from datetime import datetime, timezone
import importlib
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1] / "custom_components/dyson_spot_scrub"
SERIAL = "7VD-AU-ABC1234A"
SECRET = "X-Amz-Security-Token=FAKE-TEMPORARY-AWS-CREDENTIAL"


def module(name, **attributes):
    result = ModuleType(name)
    result.__dict__.update(attributes)
    return result


def package(name):
    result = module(name)
    result.__path__ = [str(ROOT)]
    return result


def zone(zone_id, name, status, settings=True, **extra):
    return {
        "id": zone_id, "name": name, "isSelected": status != "CLEAN_NOT_REQUESTED",
        "settings": {"cleaningStrategy": "auto", "cleanType": "vacuumAndMop",
                     "waterLevel": "low", "mopPasses": 1, "dryPasses": 1} if settings else {},
        "nameLocation": {"x": 1.0, "y": 2.0}, "cleanStatus": status, **extra,
    }


def record(clean_id, start, end, duration, area, battery, zones, method=0):
    return {
        "persistentMapId": "1787955355", "cleanId": clean_id, "isSpotClean": False,
        "orientation": 0, "startTime": start, "endTime": end, "cleanDuration": duration,
        "areaCleaned": area, "downloadUrl": f"https://example.invalid/{clean_id}?{SECRET}",
        "zones": zones, "spotZones": [], "dockLocation": None,
        "startBattery": battery[0], "endBattery": battery[1], "startMethod": method,
        "faults": [], "firmwareVersion": None,
    }


FOUR_ROOMS = ["Kitchen1", "Living room", "Dining", "Toilet"]
# Deliberately not newest-first, to prove parse_history sorts.
CAPTURE = {"data": [
    record("80e51e0f", 1792720763, 1792724322, 60, 23.46, (100.0, 57.0),
           [zone(str(10 + i), n, "CANT_CLEAN" if n == "Toilet" else "CLEAN_COMPLETE")
            for i, n in enumerate(FOUR_ROOMS)], method=1),
    record("a6a7eb71", 1792912598, 1792914531, 33, 25.56, (93.0, 64.0),
           [zone("10", "Room", "CLEAN_COMPLETE")]),
    record("459df767", 1792912059, 1792912512, 8, 7.2, (98.0, 93.0),
           [zone("10", "Room", "CLEAN_IN_PROGRESS")]),
    record("1292f815", 1792303076, 1792304010, 16, 13.15, (98.0, 85.0),
           [zone("10", "Kitchen1", "CLEAN_IN_PROGRESS", type="kitchen")]
           + [zone(str(11 + i), n, "CLEAN_NOT_REQUESTED", settings=False)
              for i, n in enumerate(FOUR_ROOMS[1:])]),
]}


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(sys.modules, {"history_pkg": package("history_pkg")}))
        self.h = importlib.import_module("history_pkg.clean_history")

    def test_newest_first_with_units_and_times(self):
        records = self.h.parse_history(CAPTURE)
        self.assertEqual([r["clean_id"] for r in records],
                         ["a6a7eb71", "459df767", "80e51e0f", "1292f815"])
        latest = records[0]
        self.assertEqual(latest["start"], datetime(2026, 10, 25, 7, 16, 38, tzinfo=timezone.utc))
        self.assertEqual(latest["end"], datetime.fromtimestamp(1792914531, timezone.utc))
        self.assertEqual(latest["duration_minutes"], 33)
        self.assertEqual(latest["area_m2"], 25.56)
        self.assertEqual(latest["battery_used"], 29.0)
        self.assertEqual(latest["map_id"], "1787955355")
        # The raw list (what get_clean_history returns) parses the same way.
        self.assertEqual(self.h.parse_history(CAPTURE["data"]), records)

    def test_results_from_room_statuses(self):
        by_id = {r["clean_id"]: r for r in self.h.parse_history(CAPTURE)}
        self.assertEqual(by_id["a6a7eb71"]["result"], "complete")
        self.assertEqual(by_id["80e51e0f"]["result"], "partial")      # Toilet: CANT_CLEAN
        self.assertEqual(by_id["459df767"]["result"], "stopped")      # stopped after 8 min
        self.assertEqual(by_id["1292f815"]["result"], "stopped")      # unrequested rooms ignored
        self.assertEqual(self.h.requested_rooms(by_id["1292f815"]), ["Kitchen1"])
        self.assertEqual(self.h.clean_result([{"status": "CANT_CLEAN"}]), "cant_clean")
        self.assertIsNone(self.h.clean_result([{"status": "CLEAN_NOT_REQUESTED"}]))
        self.assertIsNone(self.h.clean_result([]))

    def test_download_links_never_survive(self):
        records = self.h.parse_history(CAPTURE)
        exposed = json.dumps([self.h.record_attributes(r) for r in records]
                             + self.h.history_summary(records))
        self.assertNotIn("Amz", exposed)
        self.assertNotIn("example.invalid", exposed)
        self.assertNotIn("downloadUrl", exposed)
        self.assertNotIn("download_url", records[0])

    def test_malformed_records_are_skipped_or_unknown(self):
        good = CAPTURE["data"][1]
        bad = [None, "x", {}, {**good, "cleanId": ""}, {**good, "cleanId": "b", "startTime": True},
               {**good, "cleanId": "c", "startTime": float("nan")},
               {**good, "cleanId": "d", "startTime": -5}]
        odd = {**good, "cleanId": "e", "endTime": 1, "cleanDuration": "33",
               "areaCleaned": float("inf"), "startBattery": 20, "endBattery": 90,
               "zones": "nope", "faults": None, "persistentMapId": ""}
        records = self.h.parse_history({"data": bad + [odd, good, dict(good)]})
        self.assertEqual([r["clean_id"] for r in records], ["e", "a6a7eb71"], "dupes dropped")
        e = records[0]
        for key in ("end", "duration_minutes", "area_m2", "battery_used", "map_id", "result"):
            self.assertIsNone(e[key], key)
        self.assertEqual((e["rooms"], e["fault_count"]), ([], 0))
        self.assertEqual(self.h.parse_history({"data": "x"}), [])
        self.assertEqual(self.h.parse_history(None), [])

    def test_history_attribute_is_compact_and_capped(self):
        many = {"data": [record(f"id{i}", 1792000000 + i * 3600, None, 10, 5.0, (90, 80),
                                [zone("10", "Room", "CLEAN_COMPLETE")]) for i in range(15)]}
        summary = self.h.history_summary(self.h.parse_history(many))
        self.assertEqual(len(summary), 10)
        newest = datetime.fromtimestamp(1792000000 + 14 * 3600, timezone.utc).isoformat()
        self.assertEqual(summary[0], {"start": newest, "duration_minutes": 10,
                                      "area_m2": 5.0, "battery_used": 10, "result": "complete",
                                      "rooms": ["Room"]})


class ApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.http = module("aiohttp", ClientSession=MagicMock(), TCPConnector=MagicMock())
        self.enterContext(patch.dict(sys.modules, {
            "history_api": package("history_api"), "aiohttp": self.http}))
        self.api = importlib.import_module("history_api.dyson_api")

    def respond(self, status, body):
        response = MagicMock(status=status, headers={"Retry-After": "30"})
        response.json = AsyncMock(return_value=body)
        session = MagicMock()
        session.get.return_value.__aenter__.return_value = response
        return session

    async def test_get_uses_the_app_path_and_token(self):
        session = self.respond(200, CAPTURE)
        self.assertEqual(await self.api.get_clean_history("tok", SERIAL, session=session),
                         CAPTURE["data"])
        url = session.get.call_args.args[0]
        self.assertEqual(url, f"https://appapi.cp.dyson.com/v2/{SERIAL}/clean-maps")
        self.assertEqual(session.get.call_args.kwargs["headers"]["Authorization"], "Bearer tok")

    async def test_bad_bodies_and_rate_limits_raise(self):
        for body in ({"data": {"x": 1}}, {"items": []}, []):
            with self.subTest(body=body), self.assertRaises(self.api.DysonApiError):
                await self.api.get_clean_history("t", SERIAL, session=self.respond(200, body))
        with self.assertRaises(self.api.DysonRateLimitError):
            await self.api.get_clean_history("t", SERIAL, session=self.respond(429, {}))


class FakeLoop:
    """call_later recorder; timers fire only when the test says so."""

    def __init__(self):
        self.timers = []

    class Handle:
        def __init__(self, delay, fn):
            self.delay, self.fn, self.cancelled = delay, fn, False

        def cancel(self):
            self.cancelled = True

    def call_later(self, delay, fn):
        handle = self.Handle(delay, fn)
        self.timers.append(handle)
        return handle

    def live(self):
        return sorted(t.delay for t in self.timers if not t.cancelled)


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.fetch = AsyncMock(side_effect=lambda *a, **k: CAPTURE["data"])
        cleaning = lambda state: state.get("state") == "FULL_CLEAN_RUNNING"
        self.enterContext(patch.dict(sys.modules, {
            "history_coord": package("history_coord"),
            "history_coord.dyson_mqtt": module(
                "m", DysonMqttClient=object, is_any_cleaning=cleaning, start_block_reason=None),
            "history_coord.dyson_api": module(
                "a", DysonApiError=Exception, DysonRateLimitError=Exception,
                get_iot_credentials=AsyncMock(), get_live_map=AsyncMock(),
                get_map_metadata=AsyncMock(), get_clean_history=self.fetch),
            "aiohttp": module("aiohttp", ClientSession=MagicMock(), TCPConnector=MagicMock()),
            "homeassistant": module("homeassistant"),
            "homeassistant.config_entries": module("ce", ConfigEntry=object),
            "homeassistant.core": module("core", HomeAssistant=object, callback=lambda f: f),
            "homeassistant.helpers": module("homeassistant.helpers"),
            "homeassistant.helpers.aiohttp_client": module(
                "ac", async_get_clientsession=lambda hass: None),
        }))
        coordinator = importlib.import_module("history_coord.coordinator")
        self.loop = FakeLoop()
        self.tasks = []
        hass = SimpleNamespace(loop=self.loop,
                               async_create_task=lambda coro: self.tasks.append(coro) or MagicMock(
                                   done=lambda: False))
        self.c = coordinator.DysonCoordinator(hass, "token", SERIAL, "RB05",
                                              SimpleNamespace(data={}))
        self.notified = []
        self.c._async_notify_listeners = lambda: self.notified.append(1)

    def run_tasks(self):
        tasks, self.tasks = self.tasks, []
        for coro in tasks:
            asyncio.run(coro)
        # Each refresh task is deduplicated only while running; mark it done.
        self.c._history_task = None

    def test_refresh_parses_notifies_once_and_keeps_one_hourly_timer(self):
        self.assertIsNone(self.c.clean_history)
        asyncio.run(self.c.async_refresh_clean_history())
        self.assertEqual(self.c.last_clean["clean_id"], "a6a7eb71")
        self.assertEqual(len(self.notified), 1)
        asyncio.run(self.c.async_refresh_clean_history())   # unchanged: no notify
        self.assertEqual(len(self.notified), 1)
        self.assertEqual(self.loop.live(), [3600.0], "hourly timers never stack")
        self.fetch.assert_awaited_with("token", SERIAL, session=None)

    def test_failure_keeps_previous_history(self):
        asyncio.run(self.c.async_refresh_clean_history())
        before = self.c.clean_history
        self.fetch.side_effect = RuntimeError("HTTP 503")
        asyncio.run(self.c.async_refresh_clean_history())
        self.assertIs(self.c.clean_history, before)
        self.assertEqual(self.loop.live(), [3600.0], "still retried hourly")

    def test_clean_ending_schedules_two_refreshes(self):
        self.c._async_check_clean_finished({}, {"state": "INACTIVE_CHARGED"})
        self.c._async_check_clean_finished({"state": "INACTIVE_CHARGED"}, {"state": "FULL_CLEAN_RUNNING"})
        self.assertEqual(self.loop.live(), [], "startup and clean start don't trigger")
        self.c._async_check_clean_finished({"state": "FULL_CLEAN_RUNNING"}, {"state": "CHARGING"})
        self.assertEqual(self.loop.live(), [90.0, 360.0])
        self.loop.timers[0].fn()
        self.assertEqual(len(self.tasks), 1)
        self.run_tasks()
        self.assertEqual(self.c.last_clean["clean_id"], "a6a7eb71")

    def test_apply_state_compares_with_previous_snapshot(self):
        self.c._async_update_rooms_and_notify = lambda: None
        self.c._async_apply_state({"state": "FULL_CLEAN_RUNNING"})
        self.c._async_apply_state({"state": "CHARGING"})
        self.assertEqual(self.loop.live(), [90.0, 360.0])

    def test_shutdown_cancels_history_timers(self):
        self.c._async_check_clean_finished({"state": "FULL_CLEAN_RUNNING"}, {"state": "CHARGING"})
        asyncio.run(self.c.async_refresh_clean_history())
        self.c._shutting_down = True
        self.c.hass.async_add_executor_job = AsyncMock()
        asyncio.run(self.c.async_shutdown())
        self.assertEqual(self.loop.live(), [])
        self.c._async_schedule_history_refresh(0)
        self.assertEqual(self.tasks, [], "nothing starts after shutdown")


class SensorTests(unittest.TestCase):
    def setUp(self):
        ns = SimpleNamespace
        self.enterContext(patch.dict(sys.modules, {
            "history_sensor": package("history_sensor"),
            "history_sensor.coordinator": module("c", DysonCoordinator=object),
            "paho": module("paho"), "paho.mqtt": module("paho.mqtt"),
            "paho.mqtt.client": module("paho.mqtt.client"),
            "homeassistant": module("homeassistant"),
            "homeassistant.components": module("components"),
            "homeassistant.components.sensor": module(
                "sensor", SensorEntity=object,
                SensorDeviceClass=ns(BATTERY="battery", ENUM="enum", DURATION="duration",
                                     TIMESTAMP="timestamp"),
                SensorStateClass=ns(MEASUREMENT="measurement")),
            "homeassistant.config_entries": module("ce", ConfigEntry=object),
            "homeassistant.const": module("const", PERCENTAGE="%",
                                          UnitOfTime=ns(SECONDS="s", MINUTES="min")),
            "homeassistant.core": module("core", HomeAssistant=object, callback=lambda f: f),
            "homeassistant.helpers": module("helpers"),
            "homeassistant.helpers.entity": module("entity", DeviceInfo=dict),
            "homeassistant.helpers.entity_platform": module("p", AddEntitiesCallback=object),
        }))
        history = importlib.import_module("history_sensor.clean_history")
        sensor = importlib.import_module("history_sensor.sensor")
        self.records = history.parse_history(CAPTURE)
        self.coord = ns(mqtt=None, state={}, clean_history=None, last_clean=None,
                        async_add_listener=lambda e: None)
        entry = ns(entry_id="e", data={"serial": SERIAL, "device_name": "Robot"})
        entities = []
        asyncio.run(sensor.async_setup_entry(
            ns(data={"dyson_spot_scrub": {"e": self.coord}}), entry, entities.extend))
        self.by_id = {e._attr_unique_id.removeprefix(SERIAL + "_"): e for e in entities}

    def load(self, records):
        self.coord.clean_history = records
        self.coord.last_clean = records[0] if records else None

    def history_sensors(self):
        return [self.by_id[k] for k in ("last_clean_start", "last_clean_duration",
                                        "last_clean_area", "last_clean_battery_used",
                                        "last_clean_result")]

    def test_unavailable_until_loaded_even_without_mqtt(self):
        for entity in self.history_sensors():
            self.assertFalse(entity.available)
            self.assertIsNone(entity.native_value)
            self.assertEqual(entity.extra_state_attributes, {})
        self.load([])                                     # loaded, no cleans yet
        for entity in self.history_sensors():
            self.assertTrue(entity.available, "MQTT is down; history is REST")
            self.assertIsNone(entity.native_value)

    def test_values_units_and_attributes(self):
        self.load(self.records)
        start, duration, area, battery, result = self.history_sensors()
        self.assertEqual(start.native_value, self.records[0]["start"])
        self.assertEqual(start._attr_device_class, "timestamp")
        self.assertEqual((duration.native_value, duration._attr_native_unit_of_measurement), (33, "min"))
        self.assertEqual((area.native_value, area._attr_native_unit_of_measurement), (25.56, "m²"))
        self.assertEqual((battery.native_value, battery._attr_native_unit_of_measurement), (29.0, "%"))
        self.assertEqual(result.native_value, "complete")
        self.assertEqual(result._attr_options, ["complete", "partial", "stopped", "cant_clean"])
        self.assertEqual(result.extra_state_attributes["rooms"], {"Room": "CLEAN_COMPLETE"})
        attrs = start.extra_state_attributes
        self.assertEqual(len(attrs["history"]), 4)
        self.assertEqual(attrs["rooms"][0]["name"], "Room")
        self.assertNotIn("Amz", json.dumps(attrs), "attributes are JSON-safe and link-free")

    def test_translations_cover_every_result(self):
        for name in ("strings.json", "translations/en.json"):
            states = json.loads((ROOT / name).read_text())["entity"]["sensor"]["last_clean_result"]["state"]
            self.assertEqual(set(states), {"complete", "partial", "stopped", "cant_clean"}, name)


if __name__ == "__main__":
    unittest.main()
