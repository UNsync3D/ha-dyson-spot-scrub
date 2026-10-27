"""Email + OTP login: real config flow over the real dyson_api, HTTP mocked.

Checks exactly what is sent to Dyson at each step (and what isn't), and that
every failure becomes a form error instead of a crash or a saved entry.
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
ROBOT = {"Serial": "7VD-AU-ABC1234A", "ProductType": "804", "Name": "Robot",
         "mqttRootTopicLevel": "RB05"}


def module(name, **attributes):
    result = ModuleType(name)
    result.__dict__.update(attributes)
    return result


def response(status, json_body=None, text=""):
    resp = MagicMock(status=status, headers={"Content-Type": "application/json"})
    if json_body is None:
        resp.json = AsyncMock(side_effect=ValueError("Expecting value"))
        resp.headers = {"Content-Type": "text/html"}
    else:
        resp.json = AsyncMock(return_value=json_body)
    resp.text = AsyncMock(return_value=text)
    return resp


class OtpLoginTests(unittest.TestCase):
    def setUp(self):
        class Flow:
            def __init_subclass__(cls, **kwargs):
                pass

            def async_show_menu(self, **kwargs):
                return {"type": "menu", **kwargs}

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
        self.http = module("aiohttp", ClientSession=MagicMock(), TCPConnector=MagicMock(),
                           ContentTypeError=type("ContentTypeError", (Exception,), {}))
        pkg = module("otp_flow")
        pkg.__path__ = [str(ROOT)]
        self.enterContext(patch.dict(sys.modules, {
            "otp_flow": pkg, "aiohttp": self.http,
            "homeassistant": module("homeassistant", config_entries=entries),
            "homeassistant.config_entries": entries,
            "homeassistant.core": module("homeassistant.core", callback=lambda f: f),
            "homeassistant.data_entry_flow": module("homeassistant.data_entry_flow",
                                                    FlowResult=dict, section=None),
            "homeassistant.helpers": module("homeassistant.helpers", selector=selectors),
        }))
        self.flow_module = importlib.import_module("otp_flow.config_flow")
        self.calls = []                       # (verb, url, kwargs)
        self.replies = {"post": [], "get": []}
        session = MagicMock()
        for verb in ("post", "get"):
            def make(verb):
                def call(url, **kwargs):
                    self.calls.append((verb, url, kwargs))
                    ctx = MagicMock()
                    ctx.__aenter__ = AsyncMock(return_value=self.replies[verb].pop(0))
                    ctx.__aexit__ = AsyncMock(return_value=False)
                    return ctx
                return call
            setattr(session, verb, make(verb))
        self.http.ClientSession.return_value.__aenter__.return_value = session
        self.flow = self.flow_module.DysonConfigFlow()
        self.flow.hass = SimpleNamespace(config=SimpleNamespace(country="AU"))

    def run_step(self, step, data=None):
        return asyncio.run(getattr(self.flow, f"async_step_{step}")(data))

    def login(self, reply):
        self.replies["post"].append(reply)
        return self.run_step("login", {"email": "me@example.com", "password": "pw-SECRET",
                                       "country": "au"})

    def test_menu_offers_login_and_token(self):
        result = self.run_step("user")
        self.assertEqual(result["type"], "menu")
        self.assertEqual(set(result["menu_options"]), {"login", "token"})

    def test_happy_path_sends_the_right_things(self):
        result = self.login(response(200, {"challengeId": "chal-1"}))
        self.assertEqual(result["step_id"], "otp")
        verb, url, kwargs = self.calls[0]
        self.assertEqual(len(self.calls), 1, "no legacy v1 attempt before the OTP request")
        self.assertTrue(url.endswith("/v3/userregistration/email/auth"))
        self.assertEqual(kwargs["json"], {"email": "me@example.com"}, "password not sent yet")
        self.assertEqual(kwargs["params"], {"country": "AU", "culture": "en-AU"})

        self.replies["post"].append(response(200, {"token": "bearer-1"}))
        self.replies["get"].append(response(200, [ROBOT]))
        result = self.run_step("otp", {"otp_code": " 123 456 "})
        verb, url, kwargs = self.calls[1]
        self.assertTrue(url.endswith("/v3/userregistration/email/verify"))
        self.assertEqual(kwargs["params"], {"country": "AU"}, "same country as the request")
        self.assertEqual(kwargs["json"], {"email": "me@example.com", "password": "pw-SECRET",
                                          "challengeId": "chal-1", "otpCode": "123456"})
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"]["auth_token"], "bearer-1")
        self.assertEqual(result["data"]["country"], "AU")
        self.assertEqual(self.flow._password, "", "password dropped after use")

    def test_otp_request_failures_are_form_errors(self):
        for status, body, expected in [(401, {"message": "no"}, "invalid_auth"),
                                       (403, None, "invalid_auth"),
                                       (429, {"message": "slow down"}, "rate_limited"),
                                       (500, None, "cannot_connect"),
                                       (200, None, "cannot_connect")]:
            with self.subTest(status=status, json=body is not None):
                self.calls.clear()
                result = self.login(response(status, body, text="<html>err</html>"))
                self.assertEqual(result["type"], "form")
                self.assertEqual(result["errors"], {"base": expected})

    def test_verify_failures_are_form_errors_not_crashes(self):
        # The dyson_local report: a non-JSON verify reply crashed with HTTP 500.
        for status, body, expected in [(400, {"message": "bad code"}, "invalid_otp"),
                                       (401, None, "invalid_otp"),
                                       (429, None, "rate_limited"),
                                       (502, None, "cannot_connect"),
                                       (200, None, "cannot_connect"),
                                       (200, {"unexpected": True}, "cannot_connect")]:
            with self.subTest(status=status, json=body is not None):
                self.login(response(200, {"challengeId": "chal-1"}))
                self.replies["post"].append(response(status, body, text="<html>err</html>"))
                result = self.run_step("otp", {"otp_code": "123456"})
                self.assertEqual(result["type"], "form")
                self.assertEqual(result["step_id"], "otp")
                self.assertEqual(result["errors"], {"base": expected})

    def test_bad_country_makes_no_request(self):
        result = self.run_step("login", {"email": "me@example.com", "password": "pw",
                                         "country": "Australia"})
        self.assertEqual(result["errors"], {"base": "invalid_country"})
        self.assertEqual(self.calls, [])

    def test_logs_never_contain_password_or_otp(self):
        with self.assertLogs("otp_flow", level="DEBUG") as logs:
            self.login(response(200, {"challengeId": "chal-1"}))
            self.replies["post"].append(response(401, {"message": "pw-SECRET 654321"}))
            self.run_step("otp", {"otp_code": "654321"})
        text = "\n".join(logs.output)
        self.assertNotIn("pw-SECRET", text)
        self.assertNotIn("654321", text)
        self.assertIn("HTTP 401", text)


if __name__ == "__main__":
    unittest.main()
