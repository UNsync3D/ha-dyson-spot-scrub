"""Debug logs never carry secrets or the serial hidden in clean IDs.

A clean ID's second half is the robot's serial in hexadecimal (seen in R1,
R2 and Spot+Scrub AI logs shared publicly, Nov 2026). Values below are made up.
"""
import json
import logging
from types import SimpleNamespace
import unittest

import test_room_preferences

CLEAN_ID = "1ebb0400-3438-4145-5556-464130353530"   # made-up serial in the second half


class LogRedactionTests(unittest.TestCase):
    publish = test_room_preferences.RoomSettingsTests.publish
    reply = test_room_preferences.RoomSettingsTests.reply

    def setUp(self):
        test_room_preferences.RoomSettingsTests.setUp(self)

    def test_clean_ids_keep_only_their_first_block(self):
        redact = self.mqtt._redact
        self.assertEqual(redact({"params": {"clean_id": CLEAN_ID, "fault": 0}}),
                         {"params": {"clean_id": "1ebb0400-***", "fault": 0}})
        self.assertEqual(redact({"msg": "MAP-UPLOAD-STATUS", "cleanId": CLEAN_ID}),
                         {"msg": "MAP-UPLOAD-STATUS", "cleanId": "1ebb0400-***"})
        for odd in ("nodashes", "", None, 12):
            self.assertEqual(redact({"cleanId": odd}), {"cleanId": "***"})

    def test_secrets_are_still_masked(self):
        self.assertEqual(self.mqtt._redact({"localpwd": "x", "nested": [{"token": "y"}]}),
                         {"localpwd": "***", "nested": [{"token": "***"}]})

    def test_the_debug_log_line_hides_the_clean_id(self):
        message = {"msgId": "1", "method": "prop.post",
                   "params": {"clean_id": CLEAN_ID, "fault": 0}}
        with self.assertLogs(self.mqtt._LOGGER, level=logging.DEBUG) as logs:
            self.client._on_message(None, None, SimpleNamespace(
                topic="RB05/test-serial/status/jdm", payload=json.dumps(message).encode()))
        text = "\n".join(logs.output)
        self.assertIn("1ebb0400-***", text)
        self.assertNotIn("3438-4145-5556-464130353530", text)


if __name__ == "__main__":
    unittest.main()
