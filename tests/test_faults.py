"""Offline regression tests for Dyson fault payloads; no robot or HA required.

Run: python -m unittest discover -s tests -v
"""
import importlib
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import patch


def module(name):
    return ModuleType(name)


class FaultTests(unittest.TestCase):
    def setUp(self):
        package = module("fault_test_integration")
        package.__path__ = [str(Path(__file__).resolve().parents[1] /
                                "custom_components/dyson_spot_scrub")]
        self.enterContext(patch.dict(sys.modules, {
            "fault_test_integration": package,
            "paho": module("paho"),
            "paho.mqtt": module("paho.mqtt"),
            "paho.mqtt.client": module("paho.mqtt.client"),
        }))
        self.mqtt = importlib.import_module("fault_test_integration.dyson_mqtt")
        self.const = importlib.import_module("fault_test_integration.const")

    def test_current_fault_code_is_actionable(self):
        state = {"activeFaults": [{
            "faultCode": "583",
            "nextActionRequired": "USER_CONTINUE",
        }]}

        self.assertTrue(self.mqtt.has_fault(state))
        self.assertEqual(self.mqtt.active_fault_code(state), 583)
        self.assertEqual(
            self.const.FAULT_CODE_MESSAGES[583],
            "Dock's clean water tank not detected",
        )

    def test_legacy_fault_id_remains_supported(self):
        state = {"activeFaults": [{
            "faultId": 583,
            "nextActionRequired": "USER_CONTINUE",
        }]}

        self.assertTrue(self.mqtt.has_fault(state))
        self.assertEqual(self.mqtt.active_fault_code(state), 583)

    def test_log_only_fault_is_not_actionable(self):
        state = {"activeFaults": [{
            "faultCode": "583",
            "nextActionRequired": "LOG_ONLY",
        }]}

        self.assertFalse(self.mqtt.has_fault(state))
        self.assertIsNone(self.mqtt.active_fault_code(state))

    def test_informational_faults_do_not_set_error(self):
        for key in ("faultCode", "faultId"):
            for code in (2103, 2105):
                with self.subTest(key=key, code=code):
                    state = {"activeFaults": [{
                        key: str(code),
                        "nextActionRequired": "USER_CONTINUE",
                    }]}
                    self.assertFalse(self.mqtt.has_fault(state))
                    self.assertIsNone(self.mqtt.active_fault_code(state))

    def test_dock_maintenance_accepts_both_field_names(self):
        for key in ("faultCode", "faultId"):
            with self.subTest(key=key):
                state = {"activeFaults": [{key: "2103"}]}
                self.assertTrue(self.mqtt.dock_needs_maintenance(state))

    def test_unknown_fault_code_is_preserved_for_fallback_text(self):
        state = {"activeFaults": [{
            "faultCode": "9999",
            "nextActionRequired": "USER_CONTINUE",
        }]}

        self.assertTrue(self.mqtt.has_fault(state))
        self.assertEqual(self.mqtt.active_fault_code(state), 9999)

    def test_invalid_and_malformed_entries_do_not_raise(self):
        state = {"activeFaults": [None, "invalid", {
            "faultCode": "not-a-number",
            "nextActionRequired": "USER_CONTINUE",
        }]}

        self.assertTrue(self.mqtt.has_fault(state))
        self.assertIsNone(self.mqtt.active_fault_code(state))
        self.assertFalse(self.mqtt.dock_needs_maintenance(state))


if __name__ == "__main__":
    unittest.main()
