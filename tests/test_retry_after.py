"""Offline tests: real API/coordinator code, mocked HTTP and HA dependencies.

Run from the repository root: python -m unittest discover -s tests -v
No Home Assistant installation or robot connection is required.
"""
import asyncio
from datetime import datetime, timezone
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


def module(name, **attributes):
    result = ModuleType(name)
    result.__dict__.update(attributes)
    return result


class RetryAfterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        package = module("retry_test_integration")
        package.__path__ = [str(Path(__file__).resolve().parents[1] /
                                "custom_components/dyson_spot_scrub")]
        self.http = module("aiohttp", ClientSession=MagicMock(),
                           TCPConnector=MagicMock())
        self.enterContext(patch.dict(sys.modules, {
            "retry_test_integration": package,
            "retry_test_integration.dyson_mqtt": module(
                "retry_test_integration.dyson_mqtt", DysonMqttClient=object,
                is_any_cleaning=None, start_block_reason=None),
            "aiohttp": self.http,
            "homeassistant": module("homeassistant"),
            "homeassistant.config_entries": module(
                "homeassistant.config_entries", ConfigEntry=object),
            "homeassistant.core": module(
                "homeassistant.core", HomeAssistant=object, callback=lambda f: f),
            # None → dyson_api falls back to a per-call session (mocked above).
            "homeassistant.helpers": module("homeassistant.helpers"),
            "homeassistant.helpers.aiohttp_client": module(
                "homeassistant.helpers.aiohttp_client",
                async_get_clientsession=lambda hass: None),
        }))
        self.api = importlib.import_module("retry_test_integration.dyson_api")
        self.coordinator_module = importlib.import_module(
            "retry_test_integration.coordinator")
        self.now = 1000.0
        self.enterContext(patch.object(self.coordinator_module, "time",
                         SimpleNamespace(monotonic=lambda: self.now)))
        self.coordinator = self.coordinator_module.DysonCoordinator(
            SimpleNamespace(), "test-token", "test-serial", "RB05",
            SimpleNamespace(data={}))

    def response(self, status, header=None, body=None):
        response = MagicMock()
        response.status = status
        response.headers = {} if header is None else {"Retry-After": header}
        response.json = AsyncMock(return_value=body)
        session = MagicMock()
        session.get.return_value.__aenter__.return_value = response
        self.http.ClientSession.return_value.__aenter__.return_value = session
        return response

    def test_seconds_and_invalid_headers(self):
        for value, expected in [("10", 10), (" 120 ", 120), ("0", 0),
                                ("429", 429), (None, 30), ("", 30),
                                ("invalid", 30), ("-1", 30), ("NaN", 30),
                                ("inf", 30), ("1.5", 30), ("9" * 400, 30)]:
            with self.subTest(value=value):
                self.assertEqual(self.api._parse_retry_after(value), expected)

    def test_http_dates(self):
        with patch.object(self.api, "datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
            self.assertEqual(self.api._parse_retry_after(
                "Tue, 06 Oct 2026 12:01:00 GMT"), 60)
            self.assertEqual(self.api._parse_retry_after(
                "Tue, 06 Oct 2026 11:59:00 GMT"), 0)
            self.assertEqual(self.api._parse_retry_after(
                "Tue, 06 Oct 2026 12:01:00"), 30)

    async def test_real_api_exception_reaches_coordinator_and_expires(self):
        self.response(429, "10")
        with self.assertRaises(self.api.DysonRateLimitError) as caught:
            await self.api.get_live_map("test-token", "test-serial")
        self.assertIn("HTTP 429", str(caught.exception))
        self.assertEqual(caught.exception.retry_after, 10)
        self.http.ClientSession.reset_mock()
        results = await asyncio.gather(*[
            self.coordinator.async_get_live_map() for _ in range(3)])
        self.assertEqual(results, [None, None, None])
        self.assertEqual(self.coordinator._live_map_backoff_until, 1010)
        self.assertEqual(self.http.ClientSession.call_count, 1)
        self.now = 1009
        self.assertIsNone(await self.coordinator.async_get_live_map())
        self.assertEqual(self.http.ClientSession.call_count, 1)
        self.response(200, body={"cleanPath": [[1, 2], [3, 4]]})
        self.now = 1010
        result = await self.coordinator.async_get_live_map()
        self.assertIn("cleanPath", result)
        self.assertEqual(self.http.ClientSession.call_count, 2)
        self.assertEqual(await self.coordinator.async_get_live_map(), result)
        self.assertEqual(self.http.ClientSession.call_count, 2)

    async def test_fallback_and_minimum_delay_through_http(self):
        for header, expected in [(None, 30), ("broken", 30), ("0", 5), ("2", 5)]:
            with self.subTest(header=header):
                self.coordinator._live_map_backoff_until = 0
                self.response(429, header)
                self.assertIsNone(await self.coordinator.async_get_live_map())
                self.assertEqual(self.coordinator._live_map_backoff_until,
                                 self.now + expected)

    async def test_unrelated_error_containing_429_does_not_back_off(self):
        self.response(500, body={"message": "request 429 failed"})
        self.assertIsNone(await self.coordinator.async_get_live_map())
        self.assertEqual(self.coordinator._live_map_backoff_until, 0)


if __name__ == "__main__":
    unittest.main()
