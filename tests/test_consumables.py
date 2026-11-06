"""Offline sensor contract tests with minimal HA API stubs (no robot required)."""
import asyncio
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


class ConsumableTests(unittest.TestCase):
    def setUp(self):
        def mod(name, **attrs):
            result = ModuleType(name)
            result.__dict__.update(attrs)
            return result
        package = mod("consumable_test_integration")
        package.__path__ = [str(Path(__file__).resolve().parents[1] / "custom_components/dyson_spot_scrub")]
        modules = {
            "consumable_test_integration": package,
            "consumable_test_integration.coordinator": mod("coordinator", DysonCoordinator=object),
            "consumable_test_integration.dyson_mqtt": mod("mqtt", battery_level=lambda state: state.get("battery")),
            "homeassistant": mod("homeassistant"),
            "homeassistant.components": mod("components"),
            "homeassistant.components.sensor": mod("sensor", SensorEntity=object, SensorDeviceClass=SimpleNamespace(BATTERY="battery", ENUM="enum", DURATION="duration"), SensorStateClass=SimpleNamespace(MEASUREMENT="measurement")),
            "homeassistant.config_entries": mod("config_entries", ConfigEntry=object),
            "homeassistant.const": mod("const", PERCENTAGE="%", UnitOfTime=SimpleNamespace(SECONDS="s")),
            "homeassistant.core": mod("core", HomeAssistant=object, callback=lambda f: f),
            "homeassistant.helpers": mod("helpers"),
            "homeassistant.helpers.entity": mod("entity", DeviceInfo=dict),
            "homeassistant.helpers.entity_platform": mod("entity_platform", AddEntitiesCallback=object),
        }
        self.enterContext(patch.dict(sys.modules, modules))
        self.sensor = importlib.import_module("consumable_test_integration.sensor")
        self.entry = SimpleNamespace(entry_id="entry", data={"serial": "TEST", "device_name": "Dyson"})
        self.listeners = []
        self.coord = SimpleNamespace(mqtt=SimpleNamespace(connected=True, available=True), state={}, async_add_listener=self.listeners.append, async_remove_listener=self.listeners.remove)
        hass = SimpleNamespace(data={"dyson_spot_scrub": {"entry": self.coord}})
        self.entities = []
        asyncio.run(self.sensor.async_setup_entry(hass, self.entry, self.entities.extend))
        self.usage = self.entities[1:6]
        self.solution = self.entities[6]
        self.drying = self.entities[7]

    def test_registration_and_discovery_contract(self):
        self.assertEqual(len(self.entities), 8)
        self.assertEqual(len({e._attr_unique_id for e in self.entities}), 8)
        self.assertEqual(self.drying._attr_unique_id, "TEST_drying_remaining")
        self.assertEqual(self.entities[0]._attr_unique_id, "TEST_battery")
        for sensor in self.usage:
            self.assertEqual(sensor._attr_native_unit_of_measurement, "%")
            self.assertIsNone(sensor._attr_device_class)
            self.assertEqual(sensor._attr_state_class, "measurement")
            self.assertEqual(sensor._attr_extra_state_attributes["value_kind"], "usage_percent")
            self.assertEqual(sensor._attr_device_info["identifiers"], {("dyson_spot_scrub", "TEST")})
        self.assertNotIn("ioniserCartridge", [e._attr_extra_state_attributes["consumable_type"] for e in self.entities[1:7]])

    def test_sample_and_zero_are_consumption_not_remaining(self):
        kinds = [row[0] for row in self.sensor.CONSUMABLES]
        self.coord.state = {"consumables": [dict(type=k, usage=v) for k, v in zip(kinds, [4, 6, 9, 9, 0])]}
        self.assertEqual([s.native_value for s in self.usage], [4, 6, 9, 9, 0])
        self.coord.state["consumables"][0]["usage"] = 100
        self.assertEqual(self.usage[0].native_value, 100)
        self.coord.state["consumables"][0]["usage"] = 0
        self.assertEqual(self.usage[0].native_value, 0, "replacement resets are accepted")

    def test_unknown_and_invalid_never_look_new(self):
        for value in [None, -1, 101, True, "4", float("nan"), float("inf")]:
            self.coord.state = {"consumables": [{"type": "brushBar", "usage": value}]}
            self.assertIsNone(self.usage[0].native_value)
        for records in [None, {}, [], [None], [{"type": "brushBar", "usage": 4}] * 2]:
            self.coord.state = {"consumables": records}
            self.assertIsNone(self.usage[0].native_value)

    def test_solution_boolean_and_disconnection(self):
        for value, expected in [(False, "ok"), (True, "refill_required"), (None, None), (0, None), ("false", None)]:
            self.coord.state = {"consumables": [{"type": "cleaningSolution", "needsRefill": value}]}
            self.assertEqual(self.solution.native_value, expected)
        self.assertIsNone(self.solution._attr_native_unit_of_measurement)
        self.assertEqual(self.solution._attr_options, ["ok", "refill_required"])
        self.coord.mqtt.connected = self.coord.mqtt.available = False
        for sensor in self.entities[1:]:
            self.assertFalse(sensor.available)
            self.assertIsNone(sensor.native_value)

    def test_drying_countdown_only_while_drying(self):
        self.coord.state = {"dockState": "DRYING_MOP", "drying_remaining_seconds": 11280,
                            "drying_total_seconds": 14400, "drying_updated_at": "t"}
        self.assertTrue(self.drying.available)
        self.assertEqual(self.drying.native_value, 11280)
        self.assertEqual(self.drying.extra_state_attributes, {"total_seconds": 14400, "updated_at": "t"})
        self.coord.state = {**self.coord.state, "dockState": "IDLE"}
        self.assertFalse(self.drying.available)
        for value in [None, -1, float("nan"), "5"]:
            self.coord.state = {"dockState": "DRYING_MOP", "drying_remaining_seconds": value}
            self.assertIsNone(self.drying.native_value)

    def test_push_listener_lifecycle(self):
        for sensor in self.entities[1:]:
            asyncio.run(sensor.async_added_to_hass())
            self.assertIn(sensor, self.listeners)
            asyncio.run(sensor.async_will_remove_from_hass())
            self.assertNotIn(sensor, self.listeners)


if __name__ == "__main__":
    unittest.main()


class R2ConsumableTests(ConsumableTests):
    """Dyson R2 Nurovi Wash+Dry: mop pad instead of mop roller, no cleaning solution."""

    def setUp(self):
        super().setUp()
        self.entry = SimpleNamespace(entry_id="entry", data={"serial": "TEST", "device_name": "R2",
                                                             "product_type": "914"})
        hass = SimpleNamespace(data={"dyson_spot_scrub": {"entry": self.coord}})
        self.entities = []
        asyncio.run(self.sensor.async_setup_entry(hass, self.entry, self.entities.extend))

    def test_registration_and_discovery_contract(self):
        kinds = [getattr(e, "_attr_extra_state_attributes", {}).get("consumable_type")
                 for e in self.entities]
        self.assertIn("mopPad", kinds)
        self.assertNotIn("mopRoller", kinds)
        self.assertNotIn("cleaningSolution", kinds)
        self.assertEqual(len(self.entities), 7)
        pad = next(e for e in self.entities
                   if getattr(e, "_attr_extra_state_attributes", {}).get("consumable_type") == "mopPad")
        self.assertEqual(pad._attr_unique_id, "TEST_consumable_mop_pad_usage")
        self.assertEqual(pad._attr_name, "Mop pad usage")
        self.coord.state = {"consumables": [{"type": "mopPad", "usage": 1}]}
        self.assertEqual(pad.native_value, 1)

    # The Spot+Scrub-specific checks below do not apply to the R2's entity list.
    test_sample_and_zero_are_consumption_not_remaining = None
    test_unknown_and_invalid_never_look_new = None
    test_solution_boolean_and_disconnection = None
    test_drying_countdown_only_while_drying = None
    test_push_listener_lifecycle = None
