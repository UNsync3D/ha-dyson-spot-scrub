"""Dyson R2 Nurovi Wash+Dry (RB07): checks against nordlead3's capture of 8 Nov 2026.

Fixture: tests/fixtures/nurovi_r2_nov8_capture.json. The owner changed one app
setting at a time on one room, with the dock's clean-water tank removed, so
every mopping start was refused with fault 22011.
"""
from copy import deepcopy
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_drying
import test_room_preferences

CAPTURE = json.loads((Path(__file__).parent / "fixtures/nurovi_r2_nov8_capture.json").read_text())


class _RoomHarness(unittest.TestCase):
    """test_room_preferences' offline MQTT client, without its tests."""
    publish = test_room_preferences.RoomSettingsTests.publish
    settings = test_room_preferences.RoomSettingsTests.settings
    reply = test_room_preferences.RoomSettingsTests.reply

    def setUp(self):
        test_room_preferences.RoomSettingsTests.setUp(self)


class RepetitionSlotTests(_RoomHarness):
    """The app's one "1x/2x" choice lives in [6], [7] or both, by clean type."""

    def test_each_app_row_reads_as_the_app_showed_it(self):
        for sample in CAPTURE["app_rows"]:
            with self.subTest(sample["app"]):
                self.assertEqual(self.mqtt.room_repetitions(sample["row"], True), sample["passes"])

    def test_writing_repetitions_matches_the_apps_rows(self):
        by_mode = {}
        for sample in CAPTURE["app_rows"]:
            by_mode.setdefault(sample["mode"], {})[sample["passes"]] = sample["row"]
        for mode, rows in by_mode.items():
            if 0 not in rows or 1 not in rows:
                continue
            for want, other in ((1, 0), (0, 1)):
                with self.subTest(mode=mode, passes=want):
                    row = deepcopy(rows[other])
                    self.mqtt.set_room_repetitions(row, want, True)
                    # Only the repetition slots may differ between the app's rows.
                    expected = deepcopy(rows[other])
                    expected[6], expected[7] = rows[want][6], rows[want][7]
                    self.assertEqual(row, expected)
                    self.assertEqual(self.mqtt.room_repetitions(row, True), want)

    def test_spot_scrub_keeps_one_slot(self):
        row = [12, "Flur", 0, 0, 0, 2, 0, 0, 1, 0, 1, 0]
        self.mqtt.set_room_repetitions(row, 1, False)
        self.assertEqual(row[6:8], [1, 0])
        self.assertEqual(self.mqtt.room_repetitions(row, False), 1)

    def test_changing_clean_type_keeps_the_shown_repetitions(self):
        vacuum_2x = next(s["row"] for s in CAPTURE["app_rows"] if s["app"] == "Vacuum, Auto, 2x")
        row = deepcopy(vacuum_2x)
        self.mqtt.set_room_mode(row, 2, True)          # Wash
        self.assertEqual(row[3], 2)
        self.assertEqual(self.mqtt.room_repetitions(row, True), 1)
        self.assertEqual(row[6], 1)

    def test_robot_row_reads_vacuum_passes_for_a_vacuum_room(self):
        row = CAPTURE["get_preference_row"]           # Vacuum, [6]=1 left over, [7]=1
        self.assertEqual(self.mqtt.room_repetitions(row, True), row[7])


class R2ClientTests(_RoomHarness):
    """The MQTT client on an RB07 robot."""

    def setUp(self):
        super().setUp()
        self.client._prefix = "RB07"
        self.preferences["room"] = [
            [12, "Play Room", 0, 0, 0, 1, 0, 1, 0, 0, 2, 0],
            [15, "Office", 0, 0, 0, 1, 0, 0, 1, 0, 1, 0],
        ]
        self.client._cached_preference = deepcopy(self.preferences)
        self.on_request = self.reply_rb07

    def reply_rb07(self, request, preferences=None, code=0, msg_id=None):
        data = {"msgId": request["msgId"] if msg_id is None else msg_id,
                "method": "service.get_preference", "code": code,
                "data": deepcopy(self.preferences if preferences is None else preferences)}
        self.client._on_message(None, None, SimpleNamespace(
            topic="RB07/test-serial/status/jdm", payload=json.dumps(data).encode()))

    def test_rb07_splits_repetitions(self):
        self.assertTrue(self.client.splits_repetitions)

    def test_select_edit_on_a_vacuum_room_writes_vacuum_passes(self):
        self.assertTrue(self.client.edit_room_setting(15, 6, 1))
        office = next(r for r in self.settings()["room_preference"] if r[0] == 15)
        self.assertEqual(office[6:8], [0, 1])

    def test_card_draft_applies_clean_type_before_repetitions(self):
        # Wash 2x in one draft, listed repetitions-first: [6] must carry it.
        self.assertTrue(self.client.save_room_settings(
            {"15": {"mop_repetitions": "Two Passes", "cleaning_mode": "Mop"}}))
        office = next(r for r in self.settings()["room_preference"] if r[0] == 15)
        self.assertEqual(office[3], 2)
        self.assertEqual(office[6], 1)

    def test_rows_are_sent_with_eleven_values_like_the_app(self):
        self.assertTrue(self.client.edit_room_setting(15, 4, 3))
        self.assertTrue(all(len(r) == 11 for r in self.settings()["room_preference"]))
        self.sent.clear()
        self.assertTrue(self.client.start_room("Office"))
        self.assertTrue(all(len(r) == 11 for r in self.settings()["room_preference"]))

    def test_spot_scrub_rows_keep_their_length(self):
        self.client._prefix = "RB05"
        self.on_request = self.reply
        self.assertTrue(self.client.edit_room_setting(15, 4, 3))
        self.assertTrue(all(len(r) == 12 for r in self.settings()["room_preference"]))


class StaleFaultTests(_RoomHarness):
    def test_refused_mop_start_leaves_22011_behind(self):
        state = CAPTURE["refused_mop_start"]["current_state_after"]
        self.assertEqual(state["activeFaults"],
                         [{"faultCode": "22011", "nextActionRequired": "USER_CONTINUE"}])
        self.assertTrue(self.mqtt.has_fault(state))          # still shown as an error
        self.assertIsNone(self.mqtt.start_block_reason(state))  # but a new start is allowed

    def test_the_apps_next_start_cleared_it(self):
        state = CAPTURE["refused_mop_start"]["next_vacuum_start_first_state"]
        self.assertEqual(state["state"], "FULL_CLEAN_RUNNING")

    def test_other_faults_and_busy_dock_still_block(self):
        base = CAPTURE["refused_mop_start"]["current_state_after"]
        for change in ({"activeFaults": [{"faultCode": "586", "nextActionRequired": "USER_RECOVERABLE"}]},
                       {"dockState": "WASHING_MOP"},
                       {"state": "FULL_CLEAN_PAUSED"}):
            with self.subTest(change=change):
                state = {**base, **change}
                self.assertIsNotNone(self.mqtt.start_block_reason(state))

    def test_22011_has_a_message(self):
        const = importlib.import_module("room_test_integration.const")
        self.assertEqual(const.FAULT_CODE_MESSAGES[22011], "Dock's clean water tank not detected")


class AfterHomeTests(unittest.TestCase):
    setUp = test_drying.DryingTests.setUp

    def test_full_clean_aborted_is_returning(self):
        activity = SimpleNamespace(ERROR="error", PAUSED="paused", CLEANING="cleaning",
                                   RETURNING="returning", DOCKED="docked", IDLE="idle")
        vacuum = self.entities[0]
        with patch.object(self.vacuum, "VacuumActivity", activity):
            self.coord.state = CAPTURE["after_home"]["current_state"]
            self.assertEqual(vacuum.activity, "returning")
            self.coord.state = CAPTURE["after_home"]["docked_state"]
            self.assertEqual(vacuum.activity, "docked")


if __name__ == "__main__":
    unittest.main()
