"""Cleaning schedules kept on the robot (#67).

Fixture: schedules_nov23.json, FutuRazor's MyDyson capture on a Spot+Scrub AI
(23 Nov 2026, anonymised): schedule 1 created for Tue/Thu/Sat/Sun 10:00,
schedule 2 for Mon/Wed/Fri 00:00, schedule 1 off and on, then both off.
"""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import test_room_preferences

HERE = Path(__file__).parent
CAPTURE = json.loads((HERE / "fixtures/schedules_nov23.json").read_text())["messages"]

_spec = importlib.util.spec_from_file_location(
    "schedules", HERE.parent / "custom_components/dyson_spot_scrub/schedules.py")
sch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sch)


def adds():
    """The app's add_order params, in order."""
    return [m["message"]["params"] for m in CAPTURE
            if m["topic"] == "command/jdm" and m["message"]["method"] == sch.ADD_ORDER]


def feed(book, messages):
    """Feed messages as the robot's topics deliver them; True if anything changed."""
    changed = False
    for m in messages:
        data = m["message"]
        if m["topic"] == "command/jdm":
            book.command_seen(data)
        elif data.get("method") == "prop.post":
            changed |= book.total_seen(data["params"]["order_total"])
        else:
            changed |= book.reply_seen(data)
    return changed


class DayAndTimeTests(unittest.TestCase):
    def test_captured_day_masks(self):
        self.assertEqual(sch.days_to_mask(["tue", "thu", "sat", "sun"]), 106)
        self.assertEqual(sch.days_to_mask(["Monday", "Wednesday"]), 5)
        self.assertEqual(sch.days_to_mask(["mon", "wed", "fri"]), 21)
        self.assertEqual(sch.mask_to_days(106), ["tue", "thu", "sat", "sun"])
        self.assertEqual(sch.mask_to_days(64), ["sun"])
        for bad in ([], ["funday"]):
            with self.subTest(bad), self.assertRaises(ValueError):
                sch.days_to_mask(bad)

    def test_times(self):
        self.assertEqual(sch.parse_time("10:00"), (10, 0))
        self.assertEqual(sch.parse_time("7:05"), (7, 5))
        self.assertEqual(sch.parse_time("23:59:00"), (23, 59))
        for bad in ("24:00", "10:60", "10", "10:00:30", ""):
            with self.subTest(bad), self.assertRaises(ValueError):
                sch.parse_time(bad)


class BookTests(unittest.TestCase):
    def test_learns_the_apps_schedules(self):
        book = sch.ScheduleBook()
        self.assertTrue(feed(book, CAPTURE))
        self.assertEqual(set(book.orders), {1, 2})
        self.assertEqual(book.orders[1]["day"], 106)
        self.assertEqual([o["enable"] for o in (book.orders[1], book.orders[2])], [0, 0])
        self.assertEqual((book.robot_total, book.robot_enabled, book.in_sync), (2, 0, True))
        self.assertEqual(book.unknown_count, 0)

    def test_description(self):
        book = sch.ScheduleBook()
        feed(book, CAPTURE)
        self.assertEqual(sch.describe_order(book.orders[2]), {
            "schedule_id": 2, "enabled": False, "days": ["mon", "wed", "fri"], "time": "00:00",
            "repeat": True, "map_id": "1", "room_ids": ["13", "11"], "rooms": ["Room 2", "Room 4"]})
        self.assertEqual(sch.describe_order(book.orders[1])["room_ids"], ["12"])

    def test_unconfirmed_or_refused_commands_are_not_learnt(self):
        book = sch.ScheduleBook()
        command = next(m for m in CAPTURE if m["topic"] == "command/jdm"
                       and m["message"]["method"] == sch.ADD_ORDER)["message"]
        book.command_seen(command)
        self.assertFalse(book.reply_seen({"msgId": command["msgId"], "code": 1,
                                          "method": sch.ADD_ORDER, "data": {"result": 0}}))
        book.command_seen(command)
        self.assertFalse(book.reply_seen({"msgId": "other", "code": 0,
                                          "method": sch.ADD_ORDER, "data": {"result": 0}}))
        self.assertEqual(book.orders, {})

    def test_old_commands_are_forgotten(self):
        now = [0.0]
        book = sch.ScheduleBook(clock=lambda: now[0])
        command = adds()[0]
        book.command_seen({"msgId": "a", "method": sch.ADD_ORDER, "params": command})
        now[0] = sch.PENDING_SECONDS + 1
        book.command_seen({"msgId": "b", "method": sch.DEL_ORDER, "params": {"id": 9}})
        self.assertFalse(book.reply_seen({"msgId": "a", "code": 0, "method": sch.ADD_ORDER,
                                          "data": {"result": 0}}))

    def test_a_schedule_home_assistant_missed_is_out_of_sync(self):
        book = sch.ScheduleBook()
        book.summary_seen({"total": 0, "enable": 0, "md5": "empty"})
        self.assertTrue(book.in_sync)
        book.summary_seen({"total": 1, "enable": 1, "md5": "one"})     # made while not watching
        self.assertFalse(book.in_sync)
        self.assertEqual(book.unknown_count, 1)
        book.apply_add(adds()[0])                                         # saved again in the app
        book.summary_seen({"total": 1, "enable": 1, "md5": "one"})
        self.assertTrue(book.in_sync)

    def test_a_change_home_assistant_missed_is_out_of_sync(self):
        book = sch.ScheduleBook()
        book.apply_add(adds()[0])
        book.summary_seen({"total": 1, "enable": 1, "md5": "a"})
        book.summary_seen({"total": 1, "enable": 1, "md5": "b"})           # edited while not watching
        self.assertFalse(book.in_sync)

    def test_counts_from_prop_post(self):
        book = sch.ScheduleBook()
        book.apply_add(adds()[0])
        book.summary_seen({"total": 1, "enable": 1, "md5": "a"})
        self.assertFalse(book.total_seen({"total": 1, "enable": 1}))
        self.assertTrue(book.total_seen({"total": 2, "enable": 2}))
        self.assertFalse(book.in_sync)

    def test_delete(self):
        book = sch.ScheduleBook()
        feed(book, CAPTURE)
        book.command_seen({"msgId": "d", "method": sch.DEL_ORDER, "params": {"id": 2}})
        self.assertTrue(book.reply_seen({"msgId": "d", "code": 0, "method": sch.DEL_ORDER,
                                         "data": {"result": 0}}))
        self.assertEqual(set(book.orders), {1})

    def test_saved_and_loaded(self):
        book = sch.ScheduleBook()
        feed(book, CAPTURE)
        again = sch.ScheduleBook.from_dict(json.loads(json.dumps(book.to_dict())))
        self.assertEqual(again.orders, book.orders)
        self.assertEqual((again.robot_total, again.robot_enabled, again.in_sync), (2, 0, True))
        # Both were switched off after the last checksum, so the next one is
        # the new baseline, even after a restart.
        again.summary_seen({"total": 2, "enable": 0, "md5": "after"})
        self.assertTrue(again.in_sync)
        again.summary_seen({"total": 2, "enable": 0, "md5": "changed elsewhere"})
        self.assertFalse(again.in_sync)
        self.assertEqual(sch.ScheduleBook.from_dict({"orders": [{"id": "x"}], "in_sync": 3}).orders, {})
        self.assertEqual(sch.ScheduleBook.from_dict(None).orders, {})


class MapBookTests(unittest.TestCase):
    def make(self):
        book = sch.ScheduleBook()
        for index in range(5):
            book.apply_add(dict(adds()[0], id=index + 1, map_id=123, enable=0))
        book.select_map("123")
        book.summary_seen({"total": 5, "enable": 0, "md5": "floor-a"})
        return book

    def query(self, book, key):
        book.command_seen({"method": sch.GET_ORDER, "msgId": key, "params": {}})

    def reply(self, book, key, total=0, enabled=0, md5="floor-b"):
        return book.reply_seen({"method": sch.GET_ORDER, "msgId": key, "code": 0,
                                "data": {"order_data_lite": {"total": total, "enable": enabled, "md5": md5}}})

    def test_five_a_schedules_and_empty_b_do_not_create_a_sync_warning(self):
        book = self.make()
        book.select_map("456")
        book.total_seen({"total": 0, "enable": 0})  # Nov 26, 02:41:04.832, after B selection.
        self.assertIsNone(book.in_sync)
        self.query(book, "b")
        self.reply(book, "b")
        self.assertEqual((book.robot_total, len(book.active_orders), book.in_sync), (0, 0, True))
        self.assertEqual(book.sorted_orders(), [])
        self.assertEqual(len(book.orders), 5)
        book.select_map("123")
        self.assertEqual((book.robot_total, len(book.active_orders), book.in_sync), (5, 5, True))

    def test_equal_counts_keep_independent_checksums_and_real_missed_changes(self):
        book = sch.ScheduleBook()
        book.apply_add(dict(adds()[0], id=1, map_id=123))
        book.apply_add(dict(adds()[0], id=2, map_id=456))
        for identity, checksum in (("123", "a"), ("456", "b"), ("123", "a")):
            book.select_map(identity)
            book.summary_seen({"total": 1, "enable": 1, "md5": checksum})
            self.assertTrue(book.in_sync)
        book.summary_seen({"total": 1, "enable": 1, "md5": "a-edited-offline"})
        self.assertFalse(book.in_sync)
        book.select_map("456")
        self.assertTrue(book.in_sync)
        book.total_seen({"total": 2, "enable": 1})
        self.assertFalse(book.in_sync)
        self.assertEqual(book.unknown_count, 1)

    def test_late_summary_cannot_cross_a_map_switch_or_a_round_trip(self):
        book = self.make()
        self.query(book, "a-old")
        book.select_map("456")
        self.assertFalse(self.reply(book, "a-old", total=5, md5="a"))
        self.query(book, "b-old")
        book.select_map("123")
        self.assertFalse(self.reply(book, "b-old"))
        self.query(book, "a-before-trip")
        book.select_map("456")
        book.select_map("123")
        self.assertFalse(self.reply(book, "a-before-trip", total=5, md5="bad"))
        self.assertTrue(book.in_sync)

    def test_untracked_expired_and_reordered_summaries_are_ignored(self):
        now = [0]
        book = sch.ScheduleBook(clock=lambda: now[0])
        book.select_map("456")
        self.assertFalse(self.reply(book, "untracked", total=2))
        self.query(book, "old")
        now[0] = sch.PENDING_SECONDS + 1
        self.assertFalse(self.reply(book, "old"))
        self.query(book, "first")
        self.query(book, "second")
        self.assertTrue(self.reply(book, "second"))
        self.assertFalse(self.reply(book, "first", total=2))
        self.assertTrue(book.in_sync)

    def test_a_late_broker_echo_cannot_rebind_an_already_completed_query(self):
        book = self.make()
        self.query(book, "own-a")
        self.reply(book, "own-a", total=5, md5="floor-a")
        book.select_map("456")
        self.query(book, "own-a")  # Broker echo after the original reply/map switch.
        self.assertFalse(self.reply(book, "own-a", total=5, md5="floor-a"))
        self.assertIsNone(book.robot_total)

    def test_unknown_map_cannot_inherit_a_summary_or_any_orders(self):
        book = self.make()
        book.select_map("0")
        self.query(book, "unknown")
        self.assertFalse(self.reply(book, "unknown"))
        self.assertFalse(book.total_seen({"total": 5, "enable": 0}))
        self.assertIsNone(book.robot_total)
        self.assertIsNone(book.in_sync)
        self.assertEqual(book.active_orders, {})

    def test_legacy_storage_keeps_orders_without_assigning_its_global_checksum_to_a_map(self):
        book = self.make()
        data = {"orders": list(book.orders.values()), "robot_total": 0,
                "robot_enabled": 0, "in_sync": False, "synced_md5": "old-global"}
        restored = sch.ScheduleBook.from_dict(data)
        restored.select_map("123")
        self.assertIsNone(restored.in_sync)
        self.assertEqual(len(restored.active_orders), 5)
        restored.summary_seen({"total": 5, "enable": 0, "md5": "floor-a"})
        self.assertTrue(restored.in_sync)

    def test_map_summaries_and_changes_on_an_inactive_map_survive_restart(self):
        book = self.make()
        book.select_map("456")
        book.summary_seen({"total": 0, "enable": 0, "md5": "b"})
        book.apply_add(dict(book.orders[1], minute=30))  # A reply may arrive after switching to B.
        restored = sch.ScheduleBook.from_dict(json.loads(json.dumps(book.to_dict())))
        self.assertEqual(restored.map_id, "456")
        self.assertTrue(restored.in_sync)
        restored.select_map("123")
        restored.summary_seen({"total": 5, "enable": 0, "md5": "a-after-seen-edit"})
        self.assertTrue(restored.in_sync)
        self.assertEqual(restored.orders[1]["minute"], 30)

    def test_confirmed_app_delete_is_applied_to_its_original_map(self):
        book = self.make()
        book.command_seen({"method": sch.DEL_ORDER, "msgId": "delete-a", "params": {"id": 1}})
        book.select_map("456")
        self.assertTrue(book.reply_seen({"method": sch.DEL_ORDER, "msgId": "delete-a", "code": 0, "data": {"result": 0}}))
        self.assertNotIn(1, book.orders)
        self.assertIsNone(book.in_sync)

    def test_a_pre_edit_query_cannot_replace_the_post_edit_checksum(self):
        book = self.make()
        self.query(book, "before-edit")
        book.apply_add(dict(book.orders[1], minute=30))
        self.assertFalse(self.reply(book, "before-edit", total=5, md5="floor-a"))
        # Counts and visible in_sync stay the same, but the new baseline still
        # needs saving so a subsequent unseen edit is detected after restart.
        self.query(book, "after-edit")
        self.assertTrue(self.reply(book, "after-edit", total=5, md5="edited-a"))
        restored = sch.ScheduleBook.from_dict(json.loads(json.dumps(book.to_dict())))
        restored.summary_seen({"total": 5, "enable": 0, "md5": "edited-without-ha"})
        self.assertFalse(restored.in_sync)


class PayloadTests(unittest.TestCase):
    def test_switching_off_and_on_matches_the_app(self):
        created, _, off, on, _, _ = adds()
        self.assertEqual(sch.toggled(created, False), off)
        self.assertEqual(list(sch.toggled(created, False)), list(off))      # same key order
        self.assertEqual(sch.toggled(off, True), on)

    def test_room_plan_puts_the_chosen_rooms_first(self):
        base = deepcopy(adds()[0]["room_preference"])
        applied = []

        def apply(row, settings):
            applied.append((row[0], settings))
            row[3] = 2
            return True

        plan = sch.room_plan(base, ["14", 10], apply, {"14": {"cleaning_mode": "Mop"}})
        self.assertEqual([row[0] for row in plan], [14, 10, 12, 13, 11])
        self.assertEqual([row[8] for row in plan], [1, 1, 0, 0, 0])
        self.assertEqual([row[10] for row in plan], [1, 2, 3, 4, 5])
        self.assertEqual(plan[0][3], 2)
        self.assertEqual(applied, [(14, {"cleaning_mode": "Mop"})])
        self.assertEqual(base, adds()[0]["room_preference"])                # not changed

    def test_room_plan_refuses_bad_input(self):
        base = adds()[0]["room_preference"]
        ok = lambda row, settings: True
        for room_ids, settings in (([], None), (["99"], None), (["12", "12"], None),
                                   (["12"], {"99": {}}), (["12"], {"12": "x"})):
            with self.subTest(room_ids=room_ids, settings=settings), self.assertRaises(ValueError):
                sch.room_plan(base, room_ids, ok, settings)
        with self.assertRaises(ValueError):
            sch.room_plan(base, ["12"], lambda row, s: False, {"12": {"cleaning_mode": "x"}})

    def test_uv_follows_the_plan(self):
        plan = [[14], [12]]
        self.assertEqual(sch.uv_plan(plan, [[12, 1], [14, 0]]), [[14, 0], [12, 1]])
        self.assertEqual(sch.uv_plan(plan, None), [[14, 0], [12, 0]])

    def test_new_ids_and_conflicts(self):
        order_id = sch.new_order_id({1, 2})
        self.assertTrue(100_000_000 <= order_id < 2**31)
        orders = [dict(adds()[0], enable=1)]
        self.assertIsNotNone(sch.time_conflict(orders, sch.days_to_mask(["sun"]), 10, 0))
        self.assertIsNone(sch.time_conflict(orders, sch.days_to_mask(["mon"]), 10, 0))
        self.assertIsNone(sch.time_conflict(orders, 106, 10, 0, ignore_id=1))
        self.assertIsNone(sch.time_conflict([dict(orders[0], enable=0)], 106, 10, 0))

    def test_validation(self):
        for change in ({"day": 0}, {"day": 128}, {"hour": 24}, {"enable": True},
                       {"id": 0}, {"room_preference": [[1, "x"]]}):
            with self.subTest(change), self.assertRaises(ValueError):
                sch.validate_order({**adds()[0], **change})


class CalendarTests(unittest.TestCase):
    def test_weekly_occurrences_keep_local_time_across_the_clock_change(self):
        berlin = ZoneInfo("Europe/Berlin")
        order = dict(adds()[0], enable=1)                                   # Tue/Thu/Sat/Sun 10:00
        start = datetime(2027, 3, 27, 0, 0, tzinfo=berlin)                 # Saturday; clocks change Sunday
        found = sch.occurrences(order, start, start + timedelta(days=3))
        self.assertEqual([(d.date().isoformat(), d.hour) for d in found],
                         [("2027-03-27", 10), ("2027-03-28", 10)])
        self.assertEqual([d.utcoffset() for d in found], [timedelta(hours=1), timedelta(hours=2)])

    def test_off_schedules_and_window_edges(self):
        utc = timezone.utc
        order = dict(adds()[0], enable=1)
        self.assertEqual(sch.occurrences(dict(order, enable=0), datetime(2027, 1, 1, tzinfo=utc),
                                         datetime(2027, 2, 1, tzinfo=utc)), [])
        # Tuesday 2 Feb 2027, 10:30: the 10:00 clean is still on.
        now = datetime(2027, 2, 2, 10, 30, tzinfo=utc)
        self.assertEqual(sch.occurrences(order, now, now + timedelta(hours=1))[0].hour, 10)


class CoordinatorTests(unittest.TestCase):
    """The actions build what the app sends."""

    publish = test_room_preferences.RoomSettingsTests.publish
    reply = test_room_preferences.RoomSettingsTests.reply

    def setUp(self):
        test_room_preferences.RoomSettingsTests.setUp(self)
        self.module = importlib.import_module("room_test_integration.coordinator")
        self.Error = importlib.import_module("room_test_integration.schedules").ScheduleError
        dt = ModuleType("homeassistant.util.dt")
        dt.now = lambda: datetime(2026, 11, 23, 23, 49, tzinfo=timezone(timedelta(hours=1)))
        util = ModuleType("homeassistant.util")
        util.dt = dt
        self._dt = patch.dict(sys.modules, {"homeassistant.util": util, "homeassistant.util.dt": dt})
        self._dt.start()
        self.addCleanup(self._dt.stop)

    def make(self, base_rows, sent_code=0):
        def executor(func, *args):
            future = asyncio.get_running_loop().create_future()
            future.set_result(func(*args))
            return future
        hass = SimpleNamespace(async_add_executor_job=executor, async_create_task=lambda c: c.close())
        c = self.module.DysonCoordinator(hass, "token", "serial", "RB05", SimpleNamespace(data={}))
        c._listeners = []
        self.sent = []

        def send(method, params, **options):
            self.sent.append((method, deepcopy(params)))
            return sent_code
        c.mqtt = SimpleNamespace(
            available=True, supports_schedule_writes=True, send_order_command=send,
            request_order_summary=lambda: True,
            schedule_base=lambda: (1, deepcopy(base_rows), [[row[0], 1] for row in base_rows]),
            apply_schedule_room_settings=lambda row, settings: True)
        return c

    def test_new_schedule_like_the_apps(self):
        captured = adds()[0]
        robot_rows = [dict(enumerate(row)) for row in captured["room_preference"]]
        base = [[row[i] for i in range(12)] for row in robot_rows]
        for row in base:
            row[8] = 0
        c = self.make(base)
        info = asyncio.run(c.async_set_schedule(days=["tue", "thu", "sat", "sun"],
                                                start_time="10:00", room_ids=["12"]))
        method, params = self.sent[0]
        self.assertEqual(method, sch.ADD_ORDER)
        self.assertEqual(list(params), list(captured))                      # the app's key order
        self.assertEqual({k: v for k, v in params.items() if k != "id"},
                         {k: v for k, v in captured.items() if k != "id"})
        self.assertEqual(info["room_ids"], ["12"])
        self.assertIn(params["id"], c.schedules.orders)

    def test_switch_off_and_on_sends_the_apps_payloads(self):
        created, _, off, on, _, _ = adds()
        c = self.make([])
        c.schedules.apply_add(created)
        asyncio.run(c.async_set_schedule_enabled(1, False))
        asyncio.run(c.async_set_schedule_enabled(1, True))
        self.assertEqual([p for _, p in self.sent], [off, on])
        self.assertEqual(c.schedules.orders[1]["enable"], 1)

    def test_change_keeps_the_schedules_own_room_settings(self):
        created = adds()[0]
        c = self.make([])
        c.schedules.apply_add(created)
        asyncio.run(c.async_set_schedule(1, start_time="11:30"))
        params = self.sent[0][1]
        self.assertEqual((params["hour"], params["minute"]), (11, 30))
        self.assertEqual(params["room_preference"], created["room_preference"])
        self.assertEqual(params["id"], 1)

    def test_delete_and_refusals(self):
        c = self.make([], sent_code=None)
        c.schedules.apply_add(adds()[0])
        with self.assertRaises(self.Error):
            asyncio.run(c.async_delete_schedule(1))                         # no reply
        self.assertIn(1, c.schedules.orders)
        c = self.make([])
        c.schedules.apply_add(adds()[0])
        asyncio.run(c.async_delete_schedule(1))
        self.assertEqual(self.sent[-1], (sch.DEL_ORDER, {"id": 1}))
        self.assertEqual(c.schedules.orders, {})
        for call in (lambda: c.async_delete_schedule(1),
                     lambda: c.async_set_schedule(days=["mon"], start_time="10:00"),
                     lambda: c.async_set_schedule_enabled(5, True)):
            with self.assertRaises(self.Error):
                asyncio.run(call())

    def test_same_time_as_another_schedule_is_refused(self):
        c = self.make([[12, "Room 1", 0, 0, 0, 0, 0, 0, 0, 0, 1, 0]])
        c.schedules.apply_add(dict(adds()[0], enable=1))
        with self.assertRaises(self.Error):
            asyncio.run(c.async_set_schedule(days=["sun"], start_time="10:00", room_ids=["12"]))
        self.assertEqual(self.sent, [])

    def test_each_room_s_saved_settings_are_shown(self):
        """FutuRazor (#67): the card's schedule editor shows each room's settings."""
        c = self.make([])
        c.mqtt.splits_repetitions = False
        schedule = dict(adds()[1])                                            # rooms 13 then 11
        schedule["room_preference"] = deepcopy(schedule["room_preference"])
        schedule["room_preference"][1][5] = 99                               # room 11: Very Low
        schedule["room_preference"][1][6] = 1                                # two passes
        info = c.describe_schedule(schedule)
        self.assertEqual(list(info["room_settings"]), ["13", "11"])
        self.assertEqual(info["room_settings"]["13"], {
            "cleaning_mode": "Vacuum and Mop", "cleaning_strategy": "Auto",
            "water_level": "Low", "mop_repetitions": "One Pass"})
        self.assertEqual(info["room_settings"]["11"], {
            "cleaning_mode": "Vacuum", "cleaning_strategy": "Auto",
            "water_level": "Very Low", "mop_repetitions": "Two Passes"})
        schedule["room_preference"][0][4] = 7                                # unknown value
        self.assertNotIn("cleaning_strategy", c.describe_schedule(schedule)["room_settings"]["13"])

    def test_other_models_cant_change_schedules(self):
        c = self.make([])
        c.mqtt.supports_schedule_writes = False
        c.schedules.apply_add(adds()[0])
        with self.assertRaises(self.Error):
            asyncio.run(c.async_set_schedule_enabled(1, True))
        self.assertEqual(self.sent, [])

    def test_foreign_unknown_and_switching_maps_refuse_all_schedule_writes(self):
        for identity, switching in (("456", False), ("0", False), ("1", True)):
            c = self.make([])
            c.mqtt.maps_enabled = True
            c.mqtt.map_switching = switching
            c.mqtt.state = {"persistentMapId": identity}
            c.schedules.apply_add(adds()[0])
            for action in (lambda: c.async_set_schedule_enabled(1, False),
                           lambda: c.async_set_schedule(1, start_time="11:30"),
                           lambda: c.async_delete_schedule(1)):
                with self.subTest(identity=identity, switching=switching), self.assertRaises(self.Error):
                    asyncio.run(action())
            self.assertEqual(self.sent, [])
            self.assertEqual(c.schedules.orders[1], adds()[0])

    def test_current_map_writes_work_and_other_maps_do_not_create_time_conflicts(self):
        c = self.make(adds()[0]["room_preference"])
        c.mqtt.maps_enabled = True
        c.mqtt.map_switching = False
        c.mqtt.state = {"persistentMapId": "1"}
        c.schedules.apply_add(dict(adds()[0], id=10, map_id=456))
        c.schedules.select_map("1")
        # The same weekday/time on another floor must not block this floor.
        asyncio.run(c.async_set_schedule(days=["sun"], start_time="10:00", room_ids=["12"]))
        created = self.sent[-1][1]
        self.assertEqual(created["map_id"], 1)
        asyncio.run(c.async_set_schedule_enabled(created["id"], False))
        asyncio.run(c.async_delete_schedule(created["id"]))
        self.assertEqual(list(c.schedules.orders), [10])

    def test_schedule_switches_and_sensor_follow_active_map_without_changing_enabled_state(self):
        import test_entity_compat as compat
        c = self.make([])
        c.mqtt.maps_enabled = True
        c.mqtt.map_switching = False
        c.mqtt.state = {"persistentMapId": "1"}
        c.schedules.apply_add(adds()[0])
        seen = {}
        def inspect(platform, entity):
            if entity._attr_unique_id.endswith(("_schedule_1", "_schedules")):
                entity._coordinator = c
                seen[platform] = entity
        compat.collect(str(compat.ROOT), schedules=c.schedules, inspect=inspect)
        switch, sensor = seen["switch"], seen["sensor"]
        for identity, expected in (("1", True), ("456", False), ("0", False), ("1", True)):
            c.mqtt.state["persistentMapId"] = identity
            c.schedules.select_map(identity)
            self.assertEqual(switch.available, expected)
            self.assertTrue(switch.is_on)
            self.assertEqual(switch.extra_state_attributes["map_id"], "1")
            self.assertEqual(sensor.extra_state_attributes["known"], int(expected))
            self.assertEqual(sensor.extra_state_attributes["known_all_maps"], 1)
            self.assertEqual(sensor.extra_state_attributes["map_id"], identity)
            self.assertEqual(sensor.available, identity != "0")

    def test_combined_map_and_schedule_telemetry_updates_map_before_counts(self):
        seen = []
        self.client.register_callback(lambda state: seen.append(("map", state.get("persistentMapId"))))
        self.client.on_schedule_message = lambda kind, data: seen.append((kind, data))
        self.client._handle_jdm({"method": "prop.post", "params": {
            "current_map_id": 456, "order_total": {"total": 0, "enable": 0}}})
        self.assertLess(seen.index(("map", "456")), seen.index(("total", {"total": 0, "enable": 0})))


if __name__ == "__main__":
    unittest.main()
