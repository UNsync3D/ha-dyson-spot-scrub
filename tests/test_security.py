"""Offline regression tests for the security-hardening changes.

Run: python -m unittest discover -s tests -v
HA, paho and HTTP are stubbed; the integration's own modules run for real.
"""
import importlib
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


ROOT = Path(__file__).resolve().parents[1] / "custom_components/dyson_spot_scrub"


def module(name, **attributes):
    result = ModuleType(name)
    result.__dict__.update(attributes)
    return result


def package(name):
    pkg = module(name)
    pkg.__path__ = [str(ROOT)]
    return pkg


class ApiHardeningTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.http = module("aiohttp", ClientSession=MagicMock(), TCPConnector=MagicMock())
        self.enterContext(patch.dict(sys.modules, {
            "sec_api": package("sec_api"), "aiohttp": self.http,
        }))
        self.api = importlib.import_module("sec_api.dyson_api")

    def respond(self, status, body):
        response = MagicMock(status=status, headers={})
        response.json = AsyncMock(return_value=body)
        session = MagicMock()
        for verb in ("get", "post", "put"):
            getattr(session, verb).return_value.__aenter__.return_value = response
        self.http.ClientSession.return_value.__aenter__.return_value = session
        return session

    async def test_path_segments_are_encoded(self):
        session = self.respond(200, {"zones": []})
        await self.api.get_map("t", "../../v1/x", "1/2")
        url = session.get.call_args.args[0]
        self.assertEqual(
            url, "https://appapi.cp.dyson.com/v2/app/..%2F..%2Fv1%2Fx/persistent-maps/1%2F2")

    async def test_error_bodies_are_truncated_and_carry_status(self):
        self.respond(401, {"secret": "x" * 5000})
        with self.assertRaises(self.api.DysonApiError) as caught:
            await self.api.get_devices("t")
        self.assertEqual(caught.exception.status, 401)
        self.assertLess(len(str(caught.exception)), 300)

    async def test_sensitive_error_content_is_redacted_not_just_truncated(self):
        self.respond(500, {"IoTCredentials": {"TokenValue": "tv-SECRET", "TokenSignature": "sig-SECRET"},
                           "token": "bearer-SECRET", "message": "server error"})
        with self.assertRaises(self.api.DysonApiError) as caught:
            await self.api.get_iot_credentials("t", "S")
        text = str(caught.exception)
        self.assertNotIn("SECRET", text)
        self.assertIn("server error", text)

    def test_redaction_of_non_json_text(self):
        text = self.api._snippet('denied token=tok-SECRET&x=1 "password": "pw SECRET" otp=1')
        self.assertNotIn("SECRET", text)
        self.assertIn("x=1", text)
        # Redaction runs before truncation, so a secret can't straddle the cut.
        long = self.api._snippet("x" * 190 + " token=SECRET-VALUE-THAT-IS-LONG")
        self.assertNotIn("SECRET", long)


if __name__ == "__main__":
    unittest.main()
