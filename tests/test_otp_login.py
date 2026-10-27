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
        self.enterContext(patch.object(self.flow_module, "_USER_STATUS_PAUSE", 0))
        self.enterContext(patch.dict(self.flow_module._login_failures, clear=True))
        self.flow = self.flow_module.DysonConfigFlow()
        self.flow.hass = SimpleNamespace(config=SimpleNamespace(country="AU"))

    def run_step(self, step, data=None):
        return asyncio.run(getattr(self.flow, f"async_step_{step}")(data))

    ACTIVE = {"accountStatus": "ACTIVE", "authenticationMethod": "EMAIL_PWD_2FA"}

    def login(self, reply, *, status=None, minimum="5.0.21062"):
        """Version check -> user status -> code request, as the iOS app does."""
        self.replies["get"].append(minimum if not isinstance(minimum, str)
                                   else response(200, minimum))
        self.replies["post"].append(status or response(200, self.ACTIVE))
        if reply is not None:
            self.replies["post"].append(reply)
        return self.run_step("login", {"email": "me@example.com", "password": "pw-SECRET",
                                       "country": "au"})

    def paths(self):
        return [(verb, url.split("appapi.cp.dyson.com")[1]) for verb, url, _ in self.calls]

    def test_menu_offers_login_and_token(self):
        result = self.run_step("user")
        self.assertEqual(result["type"], "menu")
        self.assertEqual(set(result["menu_options"]), {"login", "token"})

    def test_happy_path_sends_the_right_things(self):
        result = self.login(response(200, {"challengeId": "chal-1"}))
        self.assertEqual(result["step_id"], "otp")
        self.assertEqual(self.paths(), [
            ("get", "/v1/provisioningservice/application/ios/version"),
            ("post", "/v3/userregistration/email/userstatus"),
            ("post", "/v3/userregistration/email/auth"),
        ], "the iOS app's order; no legacy v1 attempt")
        _, _, version_kw = self.calls[0]
        self.assertNotIn("Authorization", version_kw["headers"])
        _, _, status_kw = self.calls[1]
        self.assertEqual(status_kw["json"], {"email": "me@example.com"})
        self.assertEqual(status_kw["params"], {"country": "AU"})
        _, _, kwargs = self.calls[2]
        self.assertEqual(kwargs["json"], {"email": "me@example.com"}, "password not sent yet")
        self.assertEqual(kwargs["params"], {"country": "AU", "culture": "en-AU"})
        for _, _, kw in self.calls:
            headers = kw["headers"]
            self.assertEqual(headers["X-Platform"], "ios")
            # One consistent iOS identity, as captured from the real app.
            self.assertTrue(headers["User-Agent"].startswith("DysonLink/237306 CFNetwork/"))
            self.assertEqual(headers["X-App-Version"], "6.4.26340")
            self.assertEqual(headers["Accept"], "application/json")
            self.assertEqual(headers["Cache-Control"], "no-cache")
            self.assertNotIn("X-Dyson-LinkApp-Version", headers)

        self.replies["post"].append(response(200, {"token": "bearer-1"}))
        self.replies["get"].append(response(200, [ROBOT]))
        result = self.run_step("otp", {"otp_code": " 123 456 "})
        verb, url, kwargs = self.calls[3]
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
                                       # Dyson asking us to slow down starts the cool-down.
                                       (429, None, "too_many_attempts"),
                                       (502, None, "cannot_connect"),
                                       (200, None, "cannot_connect"),
                                       (200, {"unexpected": True}, "cannot_connect")]:
            with self.subTest(status=status, json=body is not None):
                self.flow_module._login_failures.clear()
                self.login(response(200, {"challengeId": "chal-1"}))
                self.replies["post"].append(response(status, body, text="<html>err</html>"))
                result = self.run_step("otp", {"otp_code": "123456"})
                self.assertEqual(result["type"], "form")
                self.assertEqual(result["step_id"], "otp")
                self.assertEqual(result["errors"], {"base": expected})

    def verify(self, reply):
        self.replies["post"].append(reply)
        return self.run_step("otp", {"otp_code": "123456"})

    # verify's reply to a wrong code, a wrong password or a locked account:
    # HTTP 401 with an empty text/html body (maintainer's captures).
    LOCKED = staticmethod(lambda: response(401, None, text=""))

    @staticmethod
    def fields(result):
        return [str(key) for key in result["data_schema"].schema]

    def test_wrong_password_is_corrected_without_a_new_code(self):
        # Replays the maintainer's 1 Sep 2026 capture: verify 401 (wrong
        # password), then 15 s later the same challenge with the right
        # password -> token. No second auth request in between.
        first = self.login(response(200, {"challengeId": "chal-1"}))
        self.assertEqual(self.fields(first), ["otp_code"], "no password field at first")
        rejected = self.verify(self.LOCKED())
        self.assertEqual(rejected["errors"], {"base": "invalid_otp"})
        self.assertEqual(self.fields(rejected), ["otp_code", "password"])
        self.assertNotIn("default", repr(rejected["data_schema"].schema), "never pre-filled")
        self.replies["post"].append(response(200, {"account": "a", "token": "bearer-1",
                                                   "tokenType": "Bearer"}))
        self.replies["get"].append(response(200, [ROBOT]))
        result = self.run_step("otp", {"otp_code": "123456", "password": "pw-RIGHT"})
        self.assertEqual(result["type"], "create_entry")
        verifies = [kw["json"] for verb, url, kw in self.calls if url.endswith("/verify")]
        self.assertEqual([v["password"] for v in verifies], ["pw-SECRET", "pw-RIGHT"])
        self.assertEqual({v["challengeId"] for v in verifies}, {"chal-1"})
        self.assertEqual(self.paths().count(("post", "/v3/userregistration/email/auth")), 1,
                         "no new code requested")

    def test_blank_password_keeps_the_first_one(self):
        self.login(response(200, {"challengeId": "chal-1"}))
        self.verify(self.LOCKED())
        self.replies["post"].append(self.LOCKED())
        self.run_step("otp", {"otp_code": "123456", "password": ""})
        verifies = [kw["json"] for verb, url, kw in self.calls if url.endswith("/verify")]
        self.assertEqual([v["password"] for v in verifies], ["pw-SECRET", "pw-SECRET"])

    def test_two_rejected_codes_pause_login_without_contacting_dyson(self):
        self.login(response(200, {"challengeId": "chal-1"}))
        self.assertEqual(self.verify(self.LOCKED())["errors"], {"base": "invalid_otp"})
        self.assertEqual(self.verify(self.LOCKED())["errors"], {"base": "too_many_attempts"})
        sent = len(self.calls)
        result = self.run_step("otp", {"otp_code": "654321"})   # third try: refused locally
        self.assertEqual(result["errors"], {"base": "too_many_attempts"})
        self.assertEqual(len(self.calls), sent, "no request while paused")
        # Closing the dialog and starting again doesn't get around it.
        self.flow = self.flow_module.DysonConfigFlow()
        self.flow.hass = SimpleNamespace(config=SimpleNamespace(country="AU"))
        result = self.run_step("login", {"email": " ME@example.com ", "password": "pw",
                                         "country": "au"})
        self.assertEqual(result["errors"], {"base": "too_many_attempts"})
        self.assertEqual(len(self.calls), sent, "no status check or new code while paused")
        # Another account is unaffected.
        self.replies["get"].append(response(200, "5.0.21062"))
        self.replies["post"] += [response(200, self.ACTIVE), response(200, {"challengeId": "c2"})]
        result = self.run_step("login", {"email": "other@example.com", "password": "pw",
                                         "country": "au"})
        self.assertEqual(result["step_id"], "otp")

    def test_pause_expires(self):
        clock = [1000.0]
        self.enterContext(patch.object(self.flow_module.time, "monotonic", lambda: clock[0]))
        self.login(response(200, {"challengeId": "chal-1"}))
        self.verify(self.LOCKED())
        self.verify(self.LOCKED())
        clock[0] += 15 * 60 - 1
        self.assertEqual(self.run_step("otp", {"otp_code": "1"})["errors"],
                         {"base": "too_many_attempts"})
        clock[0] += 2
        self.replies["get"].append(response(200, [ROBOT]))
        self.assertEqual(self.verify(response(200, {"token": "bearer-1"}))["type"], "create_entry")

    def test_success_resets_and_server_errors_do_not_count(self):
        self.login(response(200, {"challengeId": "chal-1"}))
        self.assertEqual(self.verify(self.LOCKED())["errors"], {"base": "invalid_otp"})
        self.assertEqual(self.verify(response(503, None))["errors"], {"base": "cannot_connect"})
        self.assertEqual(self.verify(response(502, None))["errors"], {"base": "cannot_connect"})
        self.replies["get"].append(response(200, [ROBOT]))
        self.assertEqual(self.verify(response(200, {"token": "bearer-1"}))["type"], "create_entry")
        self.assertEqual(self.flow_module._login_failures, {}, "success clears the count")

    def test_messages_warn_against_retrying(self):
        for name in ("strings.json", "translations/en.json"):
            errors = __import__("json").loads((ROOT / name).read_text())["config"]["error"]
            self.assertIn("rejected again, stop", errors["invalid_otp"], name)
            self.assertIn("code or password", errors["invalid_otp"], name)
            self.assertIn("15 minutes", errors["too_many_attempts"], name)

    def test_account_checks_stop_before_any_code_is_requested(self):
        for status_body, expected in [
            ({"accountStatus": "LOCKED", "authenticationMethod": "EMAIL_PWD_2FA"},
             "account_not_active"),
            ({"accountStatus": "UNREGISTERED"}, "account_not_active"),
            ({"accountStatus": "ACTIVE", "authenticationMethod": "EMAIL_OTP"},
             "unsupported_auth_method"),
            ({"accountStatus": "ACTIVE"}, "unsupported_auth_method"),
        ]:
            with self.subTest(status=status_body):
                self.calls.clear()
                result = self.login(None, status=response(200, status_body))
                self.assertEqual(result["errors"], {"base": expected})
                self.assertNotIn(("post", "/v3/userregistration/email/auth"), self.paths())

    def test_user_status_failure_stops_before_code_request(self):
        for status, body, expected in [(429, None, "rate_limited"),
                                       (401, {"message": "no"}, "invalid_auth"),
                                       (503, None, "cannot_connect"),
                                       (200, {"unexpected": 1}, "cannot_connect")]:
            with self.subTest(status=status):
                self.calls.clear()
                result = self.login(None, status=response(status, body, text="<html/>"))
                self.assertEqual(result["errors"], {"base": expected})
                self.assertNotIn(("post", "/v3/userregistration/email/auth"), self.paths())

    def test_version_check(self):
        # Dyson's minimum above what we report: stop, no account calls at all.
        result = self.login(None, minimum="9.9.0")
        self.assertEqual(result["errors"], {"base": "app_version_unsupported"})
        self.assertEqual(len(self.calls), 1)
        # A failed check is informational only: login carries on.
        self.replies["post"].clear(); self.replies["get"].clear(); self.calls.clear()
        result = self.login(response(200, {"challengeId": "c"}),
                            minimum=response(503, None, text="<html/>"))
        self.assertEqual(result["step_id"], "otp")
        self.assertEqual(self.flow_module.version_tuple("6.4.26420"), (6, 4, 26420))
        self.assertGreater(self.flow_module.version_tuple("6.4.26420"),
                           self.flow_module.version_tuple("5.0.21062"))

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
