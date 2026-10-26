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


# ── Finding 3: a failed room-setting send must not change the shown value ─────

class RoomSelectRollbackTests(unittest.TestCase):
    def setUp(self):
        class HAError(Exception):
            pass

        self.HAError = HAError
        self.enterContext(patch.dict(sys.modules, {
            "followup_select": package("followup_select"),
            "aiohttp": module("aiohttp", ClientSession=MagicMock(), TCPConnector=MagicMock()),
            "paho": module("paho"), "paho.mqtt": module("paho.mqtt"),
            "paho.mqtt.client": module("paho.mqtt.client"),
            "homeassistant": module("homeassistant"),
            "homeassistant.components": module("homeassistant.components"),
            "homeassistant.components.select": module("select", SelectEntity=object),
            "homeassistant.config_entries": module("config_entries", ConfigEntry=object),
            "homeassistant.core": module("core", HomeAssistant=object, callback=lambda f: f),
            "homeassistant.exceptions": module("exceptions", HomeAssistantError=HAError),
            "homeassistant.helpers": module("homeassistant.helpers"),
            "homeassistant.helpers.entity": module("entity", DeviceInfo=dict),
            "homeassistant.helpers.entity_platform": module("platform", AddEntitiesCallback=object),
            "homeassistant.helpers.aiohttp_client": module(
                "aiohttp_client", async_get_clientsession=lambda hass: None),
        }))
        self.mqtt = importlib.import_module("followup_select.dyson_mqtt")
        select = importlib.import_module("followup_select.select")

        # Real MQTT client; only the paho transport (_publish_raw) is replaced.
        self.client = self.mqtt.DysonMqttClient("SER-1", "RB05", "e", "t", "s", "c", "a")
        self.client.connected = True
        self.client.state = {"persistentMapId": "123"}
        self.client._cached_preference = {
            "room": [[10, "Living room", 0, 0, 1, 2, 1, 0, 1, 0, 3, 0]],
            "uv_switch": [[10, 1]],
        }
        self.select_module = select
        self.entity = self.make_entity("cleaning_mode")

    def make_entity(self, select_type):
        coordinator = SimpleNamespace(mqtt=self.client, serial="SER-1")
        entry = SimpleNamespace(data={"serial": "SER-1", "device_name": "Robot"})
        descriptor = next(d for d in self.select_module._ROOM_SELECT_DESCRIPTORS
                          if d[0] == select_type)
        entity = self.select_module.DysonRoomSelectEntity(
            coordinator=coordinator, entry=entry, room_name="Living Room",
            select_type=descriptor[0], label=descriptor[1], icon=descriptor[2],
            array_index=descriptor[3], options_map=descriptor[4], inverse_map=descriptor[5])

        async def executor(fn, *args):
            return fn(*args)
        entity.hass = SimpleNamespace(async_add_executor_job=executor)
        entity.async_write_ha_state = lambda: None
        return entity

    def robot_reply(self, msg_id, mode=0, water=2):
        """A service.get_preference reply as the robot would publish it."""
        self.client._handle_jdm({
            "msgId": msg_id, "code": 0, "method": "service.get_preference",
            "data": {"room": [[10, "Living room", 0, mode, 1, water, 1, 0, 1, 0, 3, 0]],
                     "uv_switch": [[10, 1]]}})

    def capture(self):
        sent = []
        return sent, patch.object(self.client, "_publish_raw",
                                  side_effect=lambda topic, payload: sent.append(payload) or True)

    def cached_mode(self):
        return self.client._cached_preference["room"][0][3]

    def select(self, option="Mop"):
        return asyncio.run(self.entity.async_select_option(option))

    def test_publish_rejected_keeps_old_value_and_raises(self):
        # Review case: _publish_raw() returns False → previously "success", mode 2.
        with patch.object(self.client, "_publish_raw", return_value=False):
            with self.assertRaises(self.HAError):
                self.select()
        self.assertEqual(self.cached_mode(), 0)
        self.assertEqual(self.entity.current_option, "Vacuum")

    def test_publish_exception_keeps_old_value_and_raises(self):
        # Review case: _publish_raw() raises → previously propagated with mode 2.
        with patch.object(self.client, "_publish_raw", side_effect=OSError("socket closed")):
            with self.assertRaises(self.HAError):
                self.select()
        self.assertEqual(self.cached_mode(), 0)

    # ── Option B (follow-up review, 26 Oct): only the robot's reply updates ──

    def test_accepted_send_shows_value_only_after_robot_reply(self):
        sent, transport = self.capture()
        with transport:
            self.select()
        methods = [p["method"] for p in sent]
        self.assertEqual(methods, ["service.set_preference", "service.get_preference"])
        self.assertEqual(sent[0]["params"]["room_preference"][0][3], 2)
        self.assertEqual(self.cached_mode(), 0, "not shown until the robot reports it")
        self.robot_reply(sent[1]["msgId"], mode=2)
        self.assertEqual(self.cached_mode(), 2)
        self.assertEqual(self.entity.current_option, "Mop")

    def test_accepted_send_does_not_overwrite_intervening_telemetry(self):
        # The follow-up review's reproduction, with the transport ACCEPTING the
        # send: mode 3 arrives mid-publish, then the publish returns True.
        def accepting_publish(topic, payload):
            if payload["method"] == "service.set_preference":
                self.robot_reply("app-1234", mode=3)
            return True

        with patch.object(self.client, "_publish_raw", side_effect=accepting_publish):
            self.select("Mop")
        self.assertEqual(self.cached_mode(), 3, "newer telemetry kept after an accepted send")

    def test_quick_second_edit_builds_on_first(self):
        water = self.make_entity("water_level")
        sent, transport = self.capture()
        with transport:
            self.select("Mop")                                   # mode -> 2
            # An unrelated/older reply arrives first; it must not drop the
            # pending edit, or the next send would revert mode to 0.
            self.robot_reply("app-older", mode=0)
            asyncio.run(water.async_select_option("Low"))        # water -> 0
        second = [p for p in sent if p["method"] == "service.set_preference"][1]
        self.assertEqual(second["params"]["room_preference"][0][3], 2, "first edit kept")
        self.assertEqual(second["params"]["room_preference"][0][5], 0, "second edit applied")

    def test_edits_rebase_on_robot_data_after_confirmation_or_timeout(self):
        sent, transport = self.capture()
        with transport:
            self.select("Mop")
            self.robot_reply(sent[1]["msgId"], mode=1)   # robot reports something else
            self.assertEqual(self.client.room_preferences_for_edit()[0][3], 1)

            self.select("Mop")
            self.assertEqual(self.client.room_preferences_for_edit()[0][3], 2, "pending")
            later = self.mqtt.time.monotonic() + self.mqtt._PREFERENCE_TIMEOUT + 1
            with patch.object(self.mqtt.time, "monotonic", return_value=later):
                self.assertEqual(self.client.room_preferences_for_edit()[0][3], 1,
                                 "unconfirmed edit expires; robot data wins")

    def test_cache_untouched_before_send_and_newer_data_not_overwritten(self):
        seen_during_send = []

        def rejecting_publish(topic, payload):
            seen_during_send.append(self.cached_mode())
            # A fresh get_preference reply lands while the send is in flight.
            self.client._cached_preference = {
                "room": [[10, "Living room", 0, 3, 1, 2, 1, 0, 1, 0, 3, 0]], "uv_switch": []}
            return False

        with patch.object(self.client, "_publish_raw", side_effect=rejecting_publish):
            with self.assertRaises(self.HAError):
                self.select()
        self.assertEqual(seen_during_send, [0], "confirmed cache is not edited before sending")
        self.assertEqual(self.cached_mode(), 3, "newer telemetry is kept, not rolled back")

    def test_save_room_settings_reports_publish_failure(self):
        with patch.object(self.client, "_publish_raw", return_value=False):
            self.assertFalse(self.client.save_room_settings({"10": {"cleaning_mode": "Mop"}}))
        self.assertEqual(self.cached_mode(), 0)

if __name__ == "__main__":
    unittest.main()
