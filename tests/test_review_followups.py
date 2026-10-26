"""Regression tests for FutuRazor's review of test/security-review (25 Oct 2026).

Each test crosses the real module boundary the review exercised: the real
dyson_api helper behind the real config flow, the real _snippet(), and the
real select entity calling the real MQTT preference save. Only the network
transport and Home Assistant itself are stubbed.

Run: python -m unittest discover -s tests -v
"""
import asyncio
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
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


# ── Finding 1: non-JSON 401/403 must not save a rejected token ────────────────

class TokenRejectionTests(unittest.TestCase):
    def setUp(self):
        class Flow:
            def __init_subclass__(cls, **kwargs):
                pass

            def async_show_form(self, **kwargs):
                return {"type": "form", **kwargs}

            def async_create_entry(self, **kwargs):
                return {"type": "create_entry", **kwargs}

            def async_abort(self, **kwargs):
                return {"type": "abort", **kwargs}

            def _async_abort_entries_match(self, match):
                pass

        selectors = SimpleNamespace(BooleanSelector=object, ColorRGBSelector=object,
                                    NumberSelector=object, NumberSelectorConfig=dict,
                                    NumberSelectorMode=SimpleNamespace(SLIDER="slider"))
        entries = module("homeassistant.config_entries", ConfigFlow=Flow, OptionsFlow=Flow)
        self.http = module("aiohttp", ClientSession=MagicMock(), TCPConnector=MagicMock())
        self.enterContext(patch.dict(sys.modules, {
            "followup_flow": package("followup_flow"),
            "aiohttp": self.http,
            "homeassistant": module("homeassistant", config_entries=entries),
            "homeassistant.config_entries": entries,
            "homeassistant.core": module("homeassistant.core", callback=lambda f: f),
            "homeassistant.data_entry_flow": module("homeassistant.data_entry_flow",
                                                    FlowResult=dict, section=None),
            "homeassistant.helpers": module("homeassistant.helpers", selector=selectors),
        }))
        # Real dyson_api (transport mocked) behind the real config flow.
        self.flow = importlib.import_module("followup_flow.config_flow")

    def respond(self, status, *, json_body=None, text_body=""):
        response = MagicMock(status=status, headers={})
        if json_body is None:
            # What aiohttp raises for an HTML/plain/empty body with content_type=None.
            response.json = AsyncMock(side_effect=ValueError("Expecting value"))
        else:
            response.json = AsyncMock(return_value=json_body)
        response.text = AsyncMock(return_value=text_body)
        session = MagicMock()
        session.get.return_value.__aenter__.return_value = response
        self.http.ClientSession.return_value.__aenter__.return_value = session

    def run_token_step(self):
        flow = self.flow.DysonConfigFlow()
        flow.hass = SimpleNamespace(config=SimpleNamespace(country="GB"))
        return asyncio.run(flow.async_step_token(
            {"token": "tok", "serial": "7VD-AU-ABC1234A", "country": "AU"}))

    def test_rejections_show_invalid_auth_for_any_body_format(self):
        for status in (401, 403):
            for label, kwargs in [
                ("json", {"json_body": {"message": "Unauthorized"}}),
                ("html", {"text_body": "<html><h1>401 Authorization Required</h1></html>"}),
                ("plain", {"text_body": "Unauthorized"}),
                ("empty", {"text_body": ""}),
            ]:
                with self.subTest(status=status, body=label):
                    self.respond(status, **kwargs)
                    result = self.run_token_step()
                    self.assertEqual(result["type"], "form", "rejected token must not be saved")
                    self.assertEqual(result["errors"], {"base": "invalid_auth"})

    def test_outages_keep_synthetic_entry_fallback(self):
        for status, kwargs in [(503, {"text_body": "<html>Service Unavailable</html>"}),
                               (500, {"json_body": {"message": "error"}})]:
            with self.subTest(status=status):
                self.respond(status, **kwargs)
                self.assertEqual(self.run_token_step()["type"], "create_entry")

    def test_unreadable_body_still_raises_status(self):
        api = sys.modules["followup_flow.dyson_api"]
        response = MagicMock(status=401, headers={})
        response.json = AsyncMock(side_effect=ValueError("no json"))
        response.text = AsyncMock(side_effect=RuntimeError("connection reset"))
        session = MagicMock()
        session.get.return_value.__aenter__.return_value = response
        self.http.ClientSession.return_value.__aenter__.return_value = session
        with self.assertRaises(api.DysonApiError) as caught:
            asyncio.run(api.get_devices("tok"))
        self.assertEqual(caught.exception.status, 401)


# ── Finding 2: unstructured bodies must not leak credentials ──────────────────

class RedactionTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(sys.modules, {
            "followup_api": package("followup_api"),
            "aiohttp": module("aiohttp", ClientSession=MagicMock(), TCPConnector=MagicMock()),
        }))
        self.api = importlib.import_module("followup_api.dyson_api")

    def test_review_examples_are_not_echoed(self):
        # The exact inputs from the review: plain-text bodies are never logged.
        for text in ["Authorization: Bearer EXAMPLE_SECRET", "token='EXAMPLE SECRET'"]:
            with self.subTest(text=text):
                out = self.api._snippet(text)
                self.assertNotIn("EXAMPLE", out)
                self.assertNotIn("SECRET", out)
                self.assertEqual(out, f"<non-JSON body, {len(text)} chars, not logged>")

    def test_html_and_empty_bodies(self):
        html = "<input name=password value='EXAMPLE SECRET'>"
        self.assertNotIn("SECRET", self.api._snippet(html))
        self.assertEqual(self.api._snippet(""), "<empty body>")
        self.assertNotIn("SECRET", self.api._snippet(html.encode()))

    def test_json_string_values_are_scrubbed(self):
        # Review examples embedded in a JSON body, which IS logged.
        for message in ["Authorization: Bearer EXAMPLE_SECRET", "token='EXAMPLE SECRET'"]:
            with self.subTest(message=message):
                out = self.api._snippet({"message": message, "code": 401})
                self.assertNotIn("EXAMPLE", out)
                self.assertNotIn("SECRET", out)
                self.assertIn("401", out)

    def test_authorization_schemes(self):
        for header in ["Authorization: Bearer EXAMPLE_SECRET",
                       "authorization: Basic dXNlcjpwYXNz",
                       "Authorization=Bearer EXAMPLE_SECRET",
                       '"Authorization": "Bearer EXAMPLE_SECRET"',
                       "Proxy-Authorization: Bearer EXAMPLE_SECRET",
                       "rejected Bearer EXAMPLE_SECRET"]:
            with self.subTest(header=header):
                out = self.api._scrub_text(header)
                self.assertNotIn("EXAMPLE_SECRET", out)
                self.assertNotIn("dXNlcjpwYXNz", out)

    def test_quote_styles_and_neighbours_survive(self):
        out = self.api._scrub_text(
            "denied: token='EXAMPLE SECRET', password=\"two words\" user=alice")
        for secret in ("EXAMPLE", "SECRET", "two words"):
            self.assertNotIn(secret, out)
        self.assertIn("user=alice", out)
        self.assertIn("denied", out)
        out = self.api._scrub_text("x-api-key: EXAMPLE_SECRET\nnext: line")
        self.assertNotIn("EXAMPLE_SECRET", out)
        self.assertIn("next: line", out)

    def test_url_encoded_and_query_forms(self):
        out = self.api._scrub_text(
            "GET /mqtt?token=abc%20SECRET&x-amz-customauthorizer-signature=SIG%2F1")
        self.assertNotIn("SECRET", out)
        self.assertNotIn("SIG%2F1", out)

    def test_ordinary_errors_stay_readable(self):
        body = {"message": "Service Unavailable: please retry later", "code": 503}
        self.assertEqual(self.api._snippet(body), repr(body))


if __name__ == "__main__":
    unittest.main()
