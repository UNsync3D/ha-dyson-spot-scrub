"""clean_room / clean_rooms act on one robot, not every connected one.

Previously both services looped over every configured Dyson robot, so with
two robots a room clean started on both. They now take an optional entity_id:
omitted is fine with one robot; with several it must say which.
Run: python -m unittest discover -s tests -v
"""
import asyncio
from enum import IntFlag
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1] / "custom_components/dyson_spot_scrub"
READY = {"state": "INACTIVE_CHARGING", "dockState": "IDLE"}


def module(name, **attributes):
    result = ModuleType(name)
    result.__dict__.update(attributes)
    return result


class ServiceTargetingTests(unittest.TestCase):
    def setUp(self):
        class HAError(Exception):
            pass

        class Coordinator:
            pass

        self.HAError, self.Coordinator = HAError, Coordinator
        package = module("targeting_pkg")
        package.__path__ = [str(ROOT)]
        features = IntFlag("F", {n: 1 << i for i, n in enumerate(
            ["START", "STOP", "PAUSE", "RETURN_HOME", "FAN_SPEED", "STATE"])})
        registry_entries = {
            "vacuum.robot_one": SimpleNamespace(platform="dyson_spot_scrub",
                                                config_entry_id="one", entity_id="vacuum.robot_one"),
            "vacuum.robot_two": SimpleNamespace(platform="dyson_spot_scrub",
                                                config_entry_id="two", entity_id="vacuum.robot_two"),
        }
        registry = SimpleNamespace(async_get=registry_entries.get)
        cv = module("cv", string=str, entity_id=str,
                    ensure_list=lambda v: v if isinstance(v, list) else [v])
        er = module("er", async_get=lambda hass: registry)
        helpers = module("homeassistant.helpers", config_validation=cv, entity_registry=er)
        self.enterContext(patch.dict(sys.modules, {
            "targeting_pkg": package,
            "targeting_pkg.coordinator": module("c", DysonCoordinator=Coordinator),
            "paho": module("paho"), "paho.mqtt": module("paho.mqtt"),
            "paho.mqtt.client": module("paho.mqtt.client"),
            "homeassistant": module("homeassistant"),
            "homeassistant.components": module("components"),
            "homeassistant.components.vacuum": module(
                "vacuum", StateVacuumEntity=object, VacuumEntityFeature=features,
                VacuumActivity=SimpleNamespace()),
            "homeassistant.config_entries": module("ce", ConfigEntry=object),
            "homeassistant.core": module("core", HomeAssistant=object, ServiceCall=object,
                                         callback=lambda f: f),
            "homeassistant.exceptions": module("exc", HomeAssistantError=HAError),
            "homeassistant.helpers": helpers,
            "homeassistant.helpers.config_validation": cv,
            "homeassistant.helpers.entity_registry": er,
            "homeassistant.helpers.entity": module("entity", DeviceInfo=dict),
            "homeassistant.helpers.entity_platform": module("p", AddEntitiesCallback=object),
        }))
        sys.modules.pop("targeting_pkg.vacuum", None)
        self.vacuum = importlib.import_module("targeting_pkg.vacuum")
        self.robots = {"one": self.robot(), "two": self.robot()}
        self.services, self.tasks = {}, []

        async def executor(fn, *args):
            return fn(*args)

        self.hass = SimpleNamespace(
            data={"dyson_spot_scrub": self.robots},
            async_add_executor_job=executor,
            async_create_task=lambda coro: self.tasks.append(coro),
            services=SimpleNamespace(
                has_service=lambda domain, name: name in self.services,
                async_register=lambda domain, name, handler, **kw: self.services.update(
                    {name: handler})))
        entry = SimpleNamespace(entry_id="one", data={"serial": "S1", "device_name": "Robot"})
        asyncio.run(self.vacuum.async_setup_entry(self.hass, entry, lambda entities: None))
        self.addCleanup(lambda: [coro.close() for coro in self.tasks])

    def robot(self, connected=True, state=READY, starts=True):
        robot = self.Coordinator()
        robot.mqtt = SimpleNamespace(connected=connected, state=dict(state),
                                     start_room=Mock(return_value=starts))
        robot.state = dict(state)
        robot.async_clean_rooms_sequential = Mock(side_effect=lambda rooms: AsyncMock()())
        return robot

    def call(self, service, **data):
        return asyncio.run(self.services[service](SimpleNamespace(data=data)))

    def started(self):
        return {name: (r.mqtt.start_room.call_count, r.async_clean_rooms_sequential.call_count)
                for name, r in self.robots.items()}

    def test_two_robots_without_target_start_nothing(self):
        for service, arg in (("clean_room", {"room": "Kitchen"}),
                             ("clean_rooms", {"rooms": ["Kitchen"]})):
            with self.subTest(service=service), self.assertRaises(self.HAError) as caught:
                self.call(service, **arg)
            self.assertIn("entity_id", str(caught.exception))
        self.assertEqual(self.started(), {"one": (0, 0), "two": (0, 0)})

    def test_target_starts_only_that_robot(self):
        self.call("clean_room", entity_id=["vacuum.robot_two"], room="Kitchen")
        self.call("clean_rooms", entity_id=["vacuum.robot_two"], rooms=["Kitchen", "Dining"])
        self.assertEqual(self.started(), {"one": (0, 0), "two": (1, 1)})
        self.robots["two"].mqtt.start_room.assert_called_once_with("Kitchen")
        self.robots["two"].async_clean_rooms_sequential.assert_called_once_with(
            ["Kitchen", "Dining"])

    def test_single_robot_needs_no_target(self):
        del self.robots["two"]
        self.call("clean_room", room="Kitchen")
        self.call("clean_rooms", rooms=["Kitchen"])
        self.assertEqual(self.started(), {"one": (1, 1)})

    def test_unavailable_busy_or_failed_robot_is_an_error(self):
        cases = [
            ("disconnected", self.robot(connected=False), "disconnected"),
            ("busy", self.robot(state={"state": "FULL_CLEAN_RUNNING", "dockState": "IDLE"}),
             "Cannot start"),
            ("rejected", self.robot(starts=False), "Could not start"),
        ]
        for name, robot, message in cases:
            with self.subTest(name):
                self.robots["two"] = robot
                with self.assertRaises(self.HAError) as caught:
                    self.call("clean_room", entity_id=["vacuum.robot_two"], room="Kitchen")
                self.assertIn(message, str(caught.exception))
                self.assertEqual(self.robots["one"].mqtt.start_room.call_count, 0)

    def test_target_must_be_one_dyson_vacuum(self):
        for target in (["vacuum.robot_one", "vacuum.robot_two"], ["vacuum.other"]):
            with self.subTest(target=target), self.assertRaises(self.HAError):
                self.call("clean_room", entity_id=target, room="Kitchen")
        self.assertEqual(self.started(), {"one": (0, 0), "two": (0, 0)})

    def test_services_yaml_offers_the_target(self):
        text = (ROOT / "services.yaml").read_text()
        for service in ("clean_room:", "clean_rooms:"):
            block = text.split(service, 1)[1].split("\n\n", 1)[0]
            self.assertIn("entity_id:", block, service)
            self.assertIn("integration: dyson_spot_scrub", block, service)


if __name__ == "__main__":
    unittest.main()
