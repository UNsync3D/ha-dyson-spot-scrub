"""Sequential room cleans: one at a time, and Stop/Home always end them (#42).

Reported by FutuRazor: two sequence requests close together let the second
task clear the first task's cancellation slot, so Stop/Home could no longer
cancel the first, which then started the next room once the robot docked.
"""
import asyncio
import importlib
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_room_preferences

READY = {"state": "INACTIVE_CHARGING", "dockState": "IDLE", "persistentMapId": "123"}


class SequenceTests(unittest.TestCase):
    publish = test_room_preferences.RoomSettingsTests.publish
    reply = test_room_preferences.RoomSettingsTests.reply

    def setUp(self):
        test_room_preferences.RoomSettingsTests.setUp(self)
        self.module = importlib.import_module("room_test_integration.coordinator")

    def make(self, loop, gate):
        """A coordinator whose room starts block on ``gate`` and whose rooms never finish."""
        self.starts = []

        async def executor(fn, *args):
            if fn is self.robot.start_room:
                self.starts.append(args[0])
                await gate.wait()
                return True
            return fn(*args)

        hass = SimpleNamespace(async_add_executor_job=executor, async_create_task=loop.create_task)
        coordinator = self.module.DysonCoordinator(
            hass, "token", "serial", "RB05", SimpleNamespace(data={}))
        self.robot = SimpleNamespace(connected=True, available=True, state=dict(READY),
                                     start_room=Mock(), stop=Mock(), return_to_base=Mock())
        coordinator.mqtt = self.robot
        return coordinator

    @staticmethod
    def run_limited(run):
        """A regression must fail, not hang CI: an orphaned task never ends."""
        async def limited():
            await asyncio.wait_for(run(), 5)
        asyncio.run(limited())

    @staticmethod
    async def settle():
        for _ in range(5):
            await asyncio.sleep(0)

    def test_second_request_is_refused_and_stop_still_cancels_the_first(self):
        async def run():
            gate = asyncio.Event()
            coordinator = self.make(asyncio.get_running_loop(), gate)
            forever = asyncio.Event()

            async def room_runs_forever(*_args, **_kwargs):
                await forever.wait()
            with patch.object(coordinator, "_async_wait_for_clean_complete", room_runs_forever):
                self.assertIsNone(coordinator.async_start_sequential(["Kitchen", "Hall"]))
                first = coordinator._cleaning_task
                # The robot hasn't reported anything yet, so it still looks ready.
                self.assertEqual(coordinator.async_start_sequential(["Office"]), "sequence_running")
                await self.settle()
                self.assertIs(coordinator._cleaning_task, first)
                gate.set()                           # Kitchen starts and runs
                await self.settle()
                self.assertIs(coordinator._cleaning_task, first)
                await coordinator.async_stop()
                self.assertTrue(first.cancelled())
                self.assertIsNone(coordinator._cleaning_task)
                self.robot.stop.assert_called_once()
                forever.set()                        # even if the room "finished" now...
                await self.settle()
            self.assertEqual(self.starts, ["Kitchen"])   # ...Hall never starts
        self.run_limited(run)

    def test_a_task_that_loses_the_race_leaves_the_owners_slot_alone(self):
        async def run():
            gate = asyncio.Event()
            coordinator = self.make(asyncio.get_running_loop(), gate)
            owner = asyncio.get_running_loop().create_task(
                coordinator.async_clean_rooms_sequential(["Kitchen"]))
            await self.settle()
            self.assertIs(coordinator._cleaning_task, owner)
            await coordinator.async_clean_rooms_sequential(["Office"])   # the old double start
            self.assertIs(coordinator._cleaning_task, owner)
            self.assertEqual(self.starts, ["Kitchen"])
            await coordinator.async_return_to_base()
            self.assertTrue(owner.cancelled())
            self.robot.return_to_base.assert_called_once()
        self.run_limited(run)

    def test_stop_before_the_sequence_first_runs(self):
        async def run():
            gate = asyncio.Event()
            gate.set()
            coordinator = self.make(asyncio.get_running_loop(), gate)
            self.assertIsNone(coordinator.async_start_sequential(["Kitchen"]))
            await coordinator.async_stop()           # before the task has run at all
            await self.settle()
            self.assertEqual(self.starts, [])
            self.assertFalse(coordinator.sequence_running)
        self.run_limited(run)

    def test_an_orphaned_sequence_never_starts_another_room_after_stop(self):
        async def run():
            gate = asyncio.Event()
            gate.set()
            coordinator = self.make(asyncio.get_running_loop(), gate)
            generation = coordinator._stop_generation
            await coordinator.async_stop()           # Stop/Home bump the generation
            await coordinator.async_clean_rooms_sequential(["Kitchen"], generation)
            self.assertEqual(self.starts, [])
        self.run_limited(run)

    def test_a_stop_from_the_app_ends_the_sequence(self):
        async def run():
            gate = asyncio.Event()
            gate.set()
            coordinator = self.make(asyncio.get_running_loop(), gate)

            async def stopped_in_app(*_args, **_kwargs):
                return "aborted"
            with patch.object(coordinator, "_async_wait_for_clean_complete", stopped_in_app):
                await coordinator.async_clean_rooms_sequential(["Kitchen", "Hall"])
            self.assertEqual(self.starts, ["Kitchen"])
            self.assertIsNone(coordinator._cleaning_task)
        self.run_limited(run)

    def test_wait_reports_full_clean_aborted(self):
        async def run():
            coordinator = self.module.DysonCoordinator(
                SimpleNamespace(), "token", "serial", "RB05", SimpleNamespace(data={}))
            coordinator.mqtt = self.client
            watchers = []
            coordinator.async_add_listener = watchers.append
            coordinator.async_remove_listener = watchers.remove
            wait = asyncio.create_task(coordinator._async_wait_for_clean_complete(timeout=1))
            await asyncio.sleep(0)
            # R2 capture, 8 Nov 2026: stopped mid-room, then driving back.
            for snapshot in ({"state": "FULL_CLEAN_RUNNING", "fullCleanAction": "VACUUMING"},
                             {"state": "FULL_CLEAN_PAUSED", "fullCleanAction": "NONE"},
                             {"state": "FULL_CLEAN_ABORTED", "fullCleanAction": "NONE"}):
                coordinator._state_snapshot = snapshot
                watchers[0].async_write_ha_state()
                await asyncio.sleep(0)
            self.assertEqual(await wait, "aborted")
        self.run_limited(run)


if __name__ == "__main__":
    unittest.main()
