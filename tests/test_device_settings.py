"""Robot settings: voice prompts, child lock, volume and Do Not Disturb (#50).

Payloads are FutuRazor's Spot+Scrub AI capture of 13 Nov 2026 on #50. The
reported values also match Fonsos's R1 logs (#31): volume 70 then 50, and
Do Not Disturb 22:00-9:00 reported as quiet_begin_time 1320 / quiet_end_time 540.
"""
from datetime import time
import importlib
import unittest
from unittest.mock import patch

import test_drying

DND = {"isOn": True, "startTime": "22:00", "endTime": "8:00"}


class PayloadTests(unittest.TestCase):
    setUp = test_drying.DryingTests.setUp

    def p(self):
        return importlib.import_module("drying_test_integration.station_settings")

    def test_switch_payloads_match_the_app(self):
        p = self.p()
        self.assertEqual(p.setting_payloads("voice_prompts", False), ({"alarm": False}, {"alarm": 0}))
        self.assertEqual(p.setting_payloads("voice_prompts", True), ({"alarm": True}, {"alarm": 1}))
        self.assertEqual(p.setting_payloads("child_lock", True), ({"childLock": True}, {"child_lock": 1}))
        self.assertEqual(p.setting_payloads("child_lock", False), ({"childLock": False}, {"child_lock": 0}))

    def test_volume_payload_matches_the_app(self):
        p = self.p()
        self.assertEqual(p.setting_payloads("volume", 60), ({"volume": 60}, {"volume": 60}))
        for bad in (-1, 101, 60.0, True, "60"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                p.setting_payloads("volume", bad)

    def test_do_not_disturb_payloads_match_the_app(self):
        p = self.p()
        off = {"doNotDisturbMode": {"isOn": False, "startTime": "22:00", "endTime": "9:00"}}
        self.assertEqual(p.setting_payloads("do_not_disturb", True, off),
                         ({"doNotDisturbMode": {"isOn": True, "startTime": "22:00", "endTime": "9:00"}},
                          {"is_open": 1, "quiet_begin_time": 1320, "quiet_end_time": 540}))
        on = {"doNotDisturbMode": DND}
        self.assertEqual(p.setting_payloads("do_not_disturb_end", time(9, 0), on),
                         ({"doNotDisturbMode": {"isOn": True, "startTime": "22:00", "endTime": "9:00"}},
                          {"is_open": 1, "quiet_begin_time": 1320, "quiet_end_time": 540}))
        self.assertEqual(p.setting_payloads("do_not_disturb", False,
                                            {"doNotDisturbMode": {**DND, "endTime": "9:00"}})[1],
                         {"is_open": 0, "quiet_begin_time": 1320, "quiet_end_time": 540})
        self.assertEqual(p.setting_payloads("do_not_disturb_start", time(7, 5), on)[0],
                         {"doNotDisturbMode": {"isOn": True, "startTime": "7:05", "endTime": "8:00"}})

    def test_do_not_disturb_needs_known_state(self):
        p = self.p()
        for state in ({}, {"doNotDisturbMode": {"isOn": True}},
                      {"doNotDisturbMode": {**DND, "endTime": "25:00"}}):
            with self.subTest(state=state), self.assertRaises(ValueError):
                p.setting_payloads("do_not_disturb", False, state)
        with self.assertRaises(ValueError):
            p.setting_payloads("do_not_disturb_start", "22:00", {"doNotDisturbMode": DND})

    def test_current_settings(self):
        p = self.p()
        state = {"alarm": True, "childLock": False, "volume": 50, "doNotDisturbMode": DND}
        self.assertIs(p.current_setting(state, "voice_prompts"), True)
        self.assertIs(p.current_setting(state, "child_lock"), False)
        self.assertEqual(p.current_setting(state, "volume"), 50)
        self.assertIs(p.current_setting(state, "do_not_disturb"), True)
        self.assertEqual(p.current_setting(state, "do_not_disturb_start"), time(22, 0))
        self.assertEqual(p.current_setting(state, "do_not_disturb_end"), time(8, 0))
        for key in ("voice_prompts", "child_lock", "volume", "do_not_disturb", "do_not_disturb_end"):
            self.assertIsNone(p.current_setting({}, key))
        self.assertIsNone(p.current_setting({"volume": True}, "volume"))

    def test_reported_properties(self):
        p = self.p()
        # prop.get (R1, 14 Nov 2026)
        self.assertEqual(p.reported_properties({"alarm": 1, "child_lock": 0, "volume": 50,
                                                "quiet_is_open": 1, "quiet_begin_time": 1320,
                                                "quiet_end_time": 540}),
                         {"alarm": True, "childLock": False, "volume": 50,
                          "doNotDisturbMode": {"isOn": True, "startTime": "22:00", "endTime": "9:00"}})
        # prop.post after a change
        self.assertEqual(p.reported_properties({"quiet_status": {"quieting": 0, "begin_time": 1320,
                                                                 "end_time": 480}}),
                         {"doNotDisturbMode": {"isOn": False, "startTime": "22:00", "endTime": "8:00"}})
        # Partial or invalid quiet times are ignored.
        self.assertEqual(p.reported_properties({"quiet_is_open": 1, "quiet_begin_time": 1320}), {})
        self.assertEqual(p.reported_properties({"quiet_status": {"quieting": 1, "begin_time": 9999,
                                                                 "end_time": 480}}), {})
        self.assertEqual(p.reported_properties({"volume": 150}), {})

    def test_new_fields_are_cleared_on_reconnect(self):
        p = self.p()
        for field in ("alarm", "childLock", "volume", "doNotDisturbMode"):
            self.assertIn(field, p.STATE_FIELDS)

    def test_mop_station_keys_exclude_robot_settings(self):
        p = self.p()
        self.assertFalse({"voice_prompts", "child_lock", "volume", *p.DND_KEYS} & p.MOP_SETTING_KEYS)


class ClientTests(unittest.TestCase):
    setUp = test_drying.DryingTests.setUp

    def test_do_not_disturb_uses_set_quiet_time(self):
        self.client.state["doNotDisturbMode"] = dict(DND)
        with patch.object(self.client, "_publish_raw", return_value=True) as raw:
            self.assertTrue(self.client.set_station_setting("do_not_disturb_end", time(9, 0)))
        legacy = self.sent.call_args_list[0].args[0]
        self.assertEqual(legacy["msg"], "STATE-SET")
        self.assertEqual(legacy["doNotDisturbMode"],
                         {"isOn": True, "startTime": "22:00", "endTime": "9:00"})
        jdm = raw.call_args.args[1]
        self.assertEqual(jdm["method"], "service.set_quiet_time")
        self.assertEqual(jdm["params"], {"is_open": 1, "quiet_begin_time": 1320, "quiet_end_time": 540})
        # Not shown until the robot confirms it.
        self.assertEqual(self.client.state["doNotDisturbMode"], DND)

    def test_switches_and_volume_use_prop_set(self):
        self.client.state.update({"alarm": True, "volume": 50})
        with patch.object(self.client, "_publish_raw", return_value=True) as raw:
            self.assertTrue(self.client.set_station_setting("voice_prompts", False))
            self.assertEqual(raw.call_args.args[1]["method"], "prop.set")
            self.assertEqual(raw.call_args.args[1]["params"], {"alarm": 0})
            self.assertTrue(self.client.set_station_setting("volume", 60))
            self.assertEqual(raw.call_args.args[1]["params"], {"volume": 60})

    def test_unknown_state_sends_nothing(self):
        with patch.object(self.client, "_publish_raw") as raw:
            self.assertFalse(self.client.set_station_setting("do_not_disturb", True))
            self.assertFalse(self.client.set_station_setting("volume", 60))
            raw.assert_not_called()
        self.sent.assert_not_called()

    def test_robot_reply_updates_state(self):
        self.client._handle_jdm({"method": "prop.post",
                                 "params": {"quiet_status": {"quieting": 1, "begin_time": 1320,
                                                             "end_time": 540}}})
        self.assertEqual(self.client.state["doNotDisturbMode"],
                         {"isOn": True, "startTime": "22:00", "endTime": "9:00"})


class QuickChangeTests(unittest.TestCase):
    """FutuRazor's test of v2.1.0b2 (#50): controls bounced back for a second."""
    setUp = test_drying.DryingTests.setUp

    def test_second_dnd_change_builds_on_the_first(self):
        self.client.state["doNotDisturbMode"] = dict(DND)
        with patch.object(self.client, "_publish_raw", return_value=True) as raw:
            self.client.set_station_setting("do_not_disturb_start", time(23, 0))
            self.client.set_station_setting("do_not_disturb_end", time(9, 0))   # before confirmation
        self.assertEqual(raw.call_args.args[1]["params"],
                         {"is_open": 1, "quiet_begin_time": 1380, "quiet_end_time": 540})

    def test_quick_toggle_back_is_still_sent(self):
        self.client.state["alarm"] = True
        with patch.object(self.client, "_publish_raw", return_value=True) as raw:
            self.client.set_station_setting("voice_prompts", False)
            self.client.set_station_setting("voice_prompts", True)   # robot still reports True
        self.assertEqual([c.args[1]["params"] for c in raw.call_args_list],
                         [{"alarm": 0}, {"alarm": 1}])

    def test_repeat_of_the_last_sent_value_is_not_resent(self):
        self.client.state["volume"] = 50
        with patch.object(self.client, "_publish_raw", return_value=True) as raw:
            self.client.set_station_setting("volume", 60)
            self.client.set_station_setting("volume", 60)
        self.assertEqual(raw.call_count, 1)

    def test_slider_shows_the_sent_value_until_confirmed(self):
        class Entity:
            async def async_added_to_hass(self): pass
            async def async_will_remove_from_hass(self): pass
        import asyncio
        from types import SimpleNamespace
        from test_drying import module
        with patch.dict("sys.modules", {
            "homeassistant.components.number": module("number", NumberEntity=Entity,
                                                      NumberMode=SimpleNamespace(SLIDER="slider")),
            "homeassistant.const": module("const", PERCENTAGE="%")}):
            number = importlib.import_module("drying_test_integration.number")
            station_entity = importlib.import_module("drying_test_integration.station_entity")
            volume = number.DysonVolumeNumber(self.coord, self.entry, "volume", "Volume")
            volume.hass = self.hass
            self.client.state["volume"] = 50
            with patch.object(self.client, "set_station_setting", return_value=True):
                asyncio.run(volume.async_set_native_value(60))
            self.assertEqual(volume.native_value, 60)
            self.client.state["volume"] = 50          # an old status reply: no bounce
            self.assertEqual(volume.native_value, 60)
            self.client.state["volume"] = 60          # confirmed
            self.assertEqual(volume.native_value, 60)
            self.assertIsNone(volume._pending)
            # Not confirmed within the window: the robot's value is shown.
            with patch.object(self.client, "set_station_setting", return_value=True):
                asyncio.run(volume.async_set_native_value(80))
            later = station_entity.time.monotonic() + station_entity.PENDING_SECONDS + 1
            with patch.object(station_entity.time, "monotonic", return_value=later):
                self.assertEqual(volume.native_value, 60)


if __name__ == "__main__":
    unittest.main()
