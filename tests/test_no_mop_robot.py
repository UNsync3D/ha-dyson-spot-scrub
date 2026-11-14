"""Robots without a mop: the Dyson R1 Nurovi Dry (#49).

Fixture: tests/fixtures/nurovi_r1_nov13_capture.json, from the R1 log shared
on #31 (13 Nov 2026). The R1 shares the R2's prefix (RB07) and product type
(914); only its CURRENT-STATE shows it has no mop: no mop settings and no mop
consumable.
"""
import asyncio
from copy import deepcopy
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_drying
import test_room_preferences
from test_drying import module
from test_nurovi_r2_entities import R2_STATE, Entity

CAPTURE = json.loads((Path(__file__).parent / "fixtures/nurovi_r1_nov13_capture.json").read_text())
R1_STATE = CAPTURE["current_state"]
SPOT_STATE = {
    "msg": "CURRENT-STATE", "state": "INACTIVE_CHARGING", "hotWaterMop": False,
    "washMopBeforeClean": True, "airDryFrequency": 3, "backWashType": "TIME",
    "consumables": [{"type": "brushBar", "usage": 0}, {"type": "mopRoller", "usage": 3},
                    {"type": "sideBrushes", "usage": 1}, {"type": "robotFilter", "usage": 0},
                    {"type": "dockFilter", "usage": 0}],
}


def entry(**extra):
    return SimpleNamespace(entry_id="one", data={
        "serial": "test", "device_name": "R1", "product_type": "914",
        "mqtt_prefix": "RB07", **extra})


class DetectionTests(unittest.TestCase):
    setUp = test_drying.DryingTests.setUp

    def test_r1_reports_no_mop(self):
        self.assertIs(self.mqtt.reports_mop(R1_STATE), False)

    def test_r2_and_spot_scrub_report_a_mop(self):
        self.assertIs(self.mqtt.reports_mop({"msg": "CURRENT-STATE", "state": "INACTIVE_CHARGING",
                                             **R2_STATE}), True)
        self.assertIs(self.mqtt.reports_mop(SPOT_STATE), True)

    def test_a_mop_consumable_alone_is_enough(self):
        state = {k: v for k, v in SPOT_STATE.items()
                 if k not in ("hotWaterMop", "washMopBeforeClean", "airDryFrequency", "backWashType")}
        self.assertIs(self.mqtt.reports_mop(state), True)

    def test_partial_messages_tell_nothing(self):
        self.assertIsNone(self.mqtt.reports_mop(CAPTURE["partial_current_state"]))
        self.assertIsNone(self.mqtt.reports_mop({"msg": "CURRENT-STATE", "state": "FULL_CLEAN_RUNNING"}))

    def test_client_learns_from_full_messages_only(self):
        self.assertIsNone(self.client.has_mop)
        self.client._handle_status(deepcopy(CAPTURE["partial_current_state"]))
        self.assertIsNone(self.client.has_mop)
        self.client._handle_status(deepcopy(R1_STATE))
        self.assertIs(self.client.has_mop, False)
        # Merged state still holds a mop field from earlier, but the next full
        # message decides on its own.
        self.client.state["hotWaterMop"] = True
        self.client._handle_status(deepcopy(R1_STATE))
        self.assertIs(self.client.has_mop, False)
        self.client._handle_status(deepcopy(SPOT_STATE))
        self.assertIs(self.client.has_mop, True)

    def test_state_changes_do_not_decide(self):
        self.client._handle_status({"msg": "STATE-CHANGE", "state": "FULL_CLEAN_RUNNING",
                                    "consumables": []})
        self.assertIsNone(self.client.has_mop)

    def test_has_mop_defaults_to_true(self):
        helpers = importlib.import_module("drying_test_integration.entity_helpers")
        self.assertTrue(helpers.has_mop(entry()))
        self.assertTrue(helpers.has_mop(entry(has_mop=True)))
        self.assertFalse(helpers.has_mop(entry(has_mop=False)))


class CoordinatorTests(unittest.TestCase):
    """The coordinator saves the answer and reloads once when it changes."""
    publish = test_room_preferences.RoomSettingsTests.publish
    reply = test_room_preferences.RoomSettingsTests.reply

    def setUp(self):
        test_room_preferences.RoomSettingsTests.setUp(self)
        self.module = importlib.import_module("room_test_integration.coordinator")

    def make(self, data, has_mop):
        updates, reloads = [], []
        entry = SimpleNamespace(entry_id="one", data=dict(data))

        def update(e, data):
            updates.append(data)
            e.data = data
        hass = SimpleNamespace(config_entries=SimpleNamespace(
            async_update_entry=update, async_schedule_reload=reloads.append))
        coordinator = self.module.DysonCoordinator(hass, "token", "serial", "RB07", entry)
        coordinator.mqtt = SimpleNamespace(has_mop=has_mop)
        return coordinator, entry, updates, reloads

    def test_r1_is_saved_and_reloaded_once(self):
        coordinator, entry, updates, reloads = self.make({"serial": "serial"}, False)
        coordinator._async_check_mop()
        coordinator._async_check_mop()
        self.assertIs(entry.data["has_mop"], False)
        self.assertEqual(len(updates), 1)
        self.assertEqual(reloads, ["one"])

    def test_a_mop_robot_is_saved_without_a_reload(self):
        coordinator, entry, updates, reloads = self.make({"serial": "serial"}, True)
        coordinator._async_check_mop()
        coordinator._async_check_mop()
        self.assertIs(entry.data["has_mop"], True)
        self.assertEqual(len(updates), 1)
        self.assertEqual(reloads, [])

    def test_nothing_happens_before_a_full_state(self):
        coordinator, entry, updates, reloads = self.make({"serial": "serial"}, None)
        coordinator._async_check_mop()
        self.assertNotIn("has_mop", entry.data)
        self.assertEqual((updates, reloads), ([], []))

    def test_a_change_back_reloads_again(self):
        coordinator, entry, updates, reloads = self.make({"serial": "serial", "has_mop": False}, True)
        coordinator._async_check_mop()
        self.assertIs(entry.data["has_mop"], True)
        self.assertEqual(reloads, ["one"])


class PlatformTests(unittest.TestCase):
    setUp = test_drying.DryingTests.setUp

    def platforms(self):
        return patch.dict("sys.modules", {
            "homeassistant.components.switch": module("switch", SwitchEntity=Entity),
            "homeassistant.components.select": module("select", SelectEntity=Entity),
            "homeassistant.components.button": module("button", ButtonEntity=Entity),
            "homeassistant.components.sensor": module(
                "sensor", SensorEntity=Entity,
                SensorDeviceClass=SimpleNamespace(BATTERY="battery", DURATION="duration", ENUM="enum"),
                SensorStateClass=SimpleNamespace(MEASUREMENT="measurement")),
            "homeassistant.const": module("const", PERCENTAGE="%",
                                          UnitOfTime=SimpleNamespace(MINUTES="min", SECONDS="s")),
        })

    def setup_platform(self, name, config_entry, rooms=()):
        self.coord.serial = "test"
        self.coord.room_names = list(rooms)
        self.coord.enabled_rooms = set()
        self.coord.current_mode = None
        self.coord.room_preferences = []
        self.coord.async_add_listener = Mock()
        entities = []
        with self.platforms():
            sys_name = f"drying_test_integration.{name}"
            import sys
            sys.modules.pop(sys_name, None)
            platform = importlib.import_module(sys_name)
            asyncio.run(platform.async_setup_entry(self.hass, config_entry, entities.extend))
        return entities

    def ids(self, entities):
        return {e._attr_unique_id for e in entities if hasattr(e, "_attr_unique_id")}

    def test_switches(self):
        self.assertEqual(self.ids(self.setup_platform("switch", entry(has_mop=False))), set())
        self.assertTrue(any(u.startswith("test_station_")
                            for u in self.ids(self.setup_platform("switch", entry()))))

    def test_buttons(self):
        r1 = self.ids(self.setup_platform("button", entry(has_mop=False)))
        r2 = self.ids(self.setup_platform("button", entry()))
        self.assertEqual(r2 - r1, {"test_wash_mop", "test_dry_mop"})

    def test_sensors(self):
        r1 = self.ids(self.setup_platform("sensor", entry(has_mop=False)))
        consumables = {"test_consumable_brush_bar_usage", "test_consumable_side_brushes_usage",
                       "test_consumable_robot_filter_usage", "test_consumable_dock_filter_usage"}
        self.assertTrue(consumables <= r1)
        self.assertFalse({"test_consumable_mop_roller_usage", "test_consumable_mop_pad_usage",
                          "test_consumable_cleaning_solution", "test_drying_remaining"} & r1)
        self.assertEqual(len(r1), len(consumables) + 1)      # plus the battery
        self.assertIn("test_drying_remaining", self.ids(self.setup_platform("sensor", entry())))

    def test_selects(self):
        r1 = self.setup_platform("select", entry(has_mop=False), rooms=["Salón"])
        self.assertFalse(any(u.startswith("test_station_") for u in self.ids(r1)))
        types = {getattr(e, "_select_type", None) for e in r1}
        self.assertNotIn("water_level", types)
        mode = next(e for e in r1 if getattr(e, "_select_type", None) == "cleaning_mode")
        self.assertEqual(mode._attr_options, ["Vacuum"])
        overall = next(e for e in r1 if e._attr_unique_id == "test_cleaning_mode")
        self.assertEqual(overall._attr_options, ["Room settings", "Vacuum"])
        r2 = self.setup_platform("select", entry(), rooms=["Salón"])
        overall = next(e for e in r2 if e._attr_unique_id == "test_cleaning_mode")
        self.assertEqual(len(overall._attr_options), 5)
        self.assertIn("water_level", {getattr(e, "_select_type", None) for e in r2})

    def test_vacuum_modes(self):
        r1 = self.vacuum.DysonVacuumEntity(self.coord, entry(has_mop=False))
        self.assertEqual(r1._attr_fan_speed_list, ["Room settings", "Vacuum"])
        r2 = self.vacuum.DysonVacuumEntity(self.coord, entry())
        self.assertEqual(len(r2._attr_fan_speed_list), 5)


class PurgeTests(unittest.TestCase):
    setUp = test_drying.DryingTests.setUp

    def test_mop_entities_are_recognised(self):
        is_mop = importlib.import_module("drying_test_integration.entity_helpers").is_mop_only_entity
        for uid in ("test_station_hot_water_mopping", "test_station_roller_drying_duration",
                    "test_consumable_mop_roller_usage", "test_consumable_mop_pad_usage",
                    "test_consumable_cleaning_solution", "test_drying_remaining",
                    "test_wash_mop", "test_dry_mop", "test_room_salon_water_level"):
            self.assertTrue(is_mop(uid, "test"), uid)
        for uid in ("test_vacuum", "test_battery", "test_consumable_brush_bar_usage",
                    "test_room_salon_cleaning_mode", "test_room_salon_mop_repetitions",
                    "test_cleaning_mode", "test_clean_whole_home", "other_wash_mop"):
            self.assertFalse(is_mop(uid, "test"), uid)


class DockingFaultTests(unittest.TestCase):
    """Fonsos's second R1 log (#31, 14 Nov 2026): it failed to dock twice."""
    setUp = test_drying.DryingTests.setUp

    STOPPED = {"state": "INACTIVE_DISCHARGING", "dockState": "IDLE", "persistentMapId": "65537",
               "activeFaults": [{"faultCode": "5005", "nextActionRequired": "USER_CONTINUE"}]}
    RETRYING = {"state": "ABORTED", "dockState": "IDLE", "persistentMapId": "65537",
                "activeFaults": []}

    def activity(self, state):
        act = SimpleNamespace(ERROR="error", PAUSED="paused", CLEANING="cleaning",
                              RETURNING="returning", DOCKED="docked", IDLE="idle")
        with patch.object(self.vacuum, "VacuumActivity", act):
            self.coord.state = state
            return self.entities[0].activity

    def test_5005_is_an_error_with_a_message(self):
        const = importlib.import_module("drying_test_integration.const")
        self.assertEqual(self.mqtt.active_fault_code(self.STOPPED), 5005)
        self.assertEqual(const.FAULT_CODE_MESSAGES[5005], "Couldn't return to the dock")
        self.assertEqual(self.activity(self.STOPPED), "error")
        # Off the dock, so it isn't the stale on-dock case: starts stay blocked.
        self.assertEqual(self.mqtt.start_block_reason(self.STOPPED), "robot_fault")

    def test_aborted_is_returning(self):
        self.assertEqual(self.activity(self.RETRYING), "returning")
        self.assertEqual(self.mqtt.start_block_reason(self.RETRYING), "returning_or_finishing")
