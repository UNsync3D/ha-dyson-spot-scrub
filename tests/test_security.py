"""Offline regression tests for the security-hardening changes.

Run: python -m unittest discover -s tests -v
HA, paho and HTTP are stubbed; the integration's own modules run for real.
"""
import asyncio
import base64
import importlib
import io
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


class FakePahoClient:
    """Records what DysonMqttClient.connect() configures; never opens a socket."""

    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        FakePahoClient.instances.append(self)

    def ws_set_options(self, path):
        self.ws_path = path

    def tls_set_context(self, ctx):
        pass

    def connect(self, *args, **kwargs):
        pass

    def loop_start(self):
        pass


class MqttHardeningTests(unittest.TestCase):
    def setUp(self):
        paho_client = module("paho.mqtt.client", Client=FakePahoClient,
                             CallbackAPIVersion=SimpleNamespace(VERSION1=1), MQTTv311=4)
        self.enterContext(patch.dict(sys.modules, {
            "sec_mqtt": package("sec_mqtt"),
            "paho": module("paho"), "paho.mqtt": module("paho.mqtt"),
            "paho.mqtt.client": paho_client,
        }))
        self.mqtt = importlib.import_module("sec_mqtt.dyson_mqtt")

    def client(self, **overrides):
        args = dict(serial="SER-1", mqtt_prefix="RB05", endpoint="example.invalid",
                    token_value="tok", token_signature="sig", client_id="cid",
                    authorizer_name="auth")
        args.update(overrides)
        return self.mqtt.DysonMqttClient(**args)

    def test_websocket_query_values_are_encoded(self):
        client = self.client(token_value="a&b=c#d", authorizer_name="x&y",
                             token_signature="s/+=")
        client.connect()
        path = FakePahoClient.instances[-1].ws_path
        self.assertIn("token=a%26b%3Dc%23d", path)
        self.assertIn("x-amz-customauthorizer-name=x%26y", path)
        self.assertIn("signature=s%2F%2B%3D", path)
        self.assertEqual(path.count("&"), 2, "only the two real separators remain")

    def test_prefix_autodetect_rejects_wildcards(self):
        client = self.client()
        changed = []
        client.on_prefix_changed = changed.append
        with patch.object(client, "request_current_state"), \
             patch.object(client, "_schedule_map_probes"):
            for bad in ["#", "+", "a b", "x" * 40]:
                msg = SimpleNamespace(topic=f"{bad}/SER-1/status", payload=b"{}")
                client._on_message(None, None, msg)
            self.assertEqual(changed, [])
            self.assertEqual(client._prefix, "RB05")
            client._on_message(None, None, SimpleNamespace(topic="NROB/SER-1/status", payload=b"{}"))
        self.assertEqual(changed, ["NROB"])

    def test_dock_actions_share_one_guarded_path(self):
        client = self.client()
        client.connected = True
        sent = []
        with patch.object(client, "_publish_raw", lambda topic, p: sent.append((topic, p)) or True):
            client.state = {"dockState": "IDLE"}
            self.assertTrue(client.empty_bin())
            self.assertTrue(client.wash_mop())
            client.state = {"dockState": "DRYING_MOP"}
            self.assertFalse(client.empty_bin())
            self.assertFalse(client.wash_mop())
        legacy = [p["action"] for t, p in sent if t.endswith("/command")]
        jdm = [p["params"]["station_act"] for t, p in sent if t.endswith("/command/jdm")]
        self.assertEqual(legacy, ["COLLECT_DUST", "WASH_MOP"])
        self.assertEqual(jdm, [3, 1])

    def test_fault_helpers_agree(self):
        state = {"activeFaults": [
            {"faultCode": 2105, "status": "WARN"},          # informational
            {"faultCode": 513, "status": "LOG_ONLY"},       # log only
            {"status": "STOP"},                             # actionable, no code
            {"faultCode": 567, "nextActionRequired": "STOP"},
        ]}
        self.assertTrue(self.mqtt.has_fault(state))
        self.assertEqual(self.mqtt.active_fault_code(state), 567)
        self.assertFalse(self.mqtt.has_fault({"activeFaults": [{"faultCode": 2103, "status": "X"}]}))


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

    async def test_shared_session_is_used_without_new_session(self):
        shared = self.respond(200, {"cleanPath": []})
        self.http.ClientSession.reset_mock()
        await self.api.get_live_map("t", "S", session=shared)
        self.http.ClientSession.assert_not_called()
        shared.get.assert_called_once()

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
        # Non-JSON bodies are summarised, never echoed.
        text = self.api._snippet('denied token=tok-SECRET&x=1 "password": "pw SECRET" otp=1')
        self.assertNotIn("SECRET", text)
        self.assertIn("not logged", text)
        # Redaction runs before truncation, so a secret can't straddle the cut.
        long = self.api._snippet({"message": "x" * 190 + " token=SECRET-VALUE-THAT-IS-LONG"})
        self.assertNotIn("SECRET", long)

    def test_local_credentials_padding_is_validated(self):
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        def encrypt(plain):
            enc = Cipher(algorithms.AES(self.api._LC_KEY), modes.CBC(self.api._LC_IV)).encryptor()
            return base64.b64encode(enc.update(plain) + enc.finalize()).decode()

        good = encrypt(b"password" + bytes([8]) * 8)
        self.assertEqual(self.api.decrypt_local_credentials(good), ("", "password"))
        for bad in [encrypt(b"password" + bytes([0]) * 8),      # pad 0
                    encrypt(b"password1234567" + bytes([40])),  # pad > 16
                    encrypt(b"password" + bytes([1]) * 7 + bytes([8]))]:  # inconsistent
            with self.assertRaises(self.api.DysonApiError):
                self.api.decrypt_local_credentials(bad)
        with self.assertRaises(self.api.DysonApiError):
            self.api.decrypt_local_credentials(base64.b64encode(b"short").decode())


class RendererHardeningTests(unittest.TestCase):
    def test_canvas_is_capped_for_bogus_coordinates(self):
        from PIL import Image
        sys.modules.setdefault("sec_render", package("sec_render"))
        renderer = importlib.import_module("sec_render.map_renderer")
        tri = lambda x, y: [{"x": x, "y": y}, {"x": x + 1, "y": y}, {"x": x, "y": y + 1}]
        data = {"zones": [], "dockLocation": {"x": 0, "y": 0},
                "furniture": [{"points": tri(0, 0)}, {"points": tri(1e6, 1e6)}]}
        options = {"map_show_grid": True}
        image = Image.open(io.BytesIO(renderer.render_map(data, [], None, options, raise_errors=True)))
        self.assertLessEqual(max(image.size), renderer.MAX_CANVAS)


class ApiError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class ConfigFlowValidationTests(unittest.TestCase):
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
        self.enterContext(patch.dict(sys.modules, {
            "sec_flow": package("sec_flow"),
            "homeassistant": module("homeassistant", config_entries=entries),
            "homeassistant.config_entries": entries,
            "homeassistant.core": module("homeassistant.core", callback=lambda f: f),
            "homeassistant.data_entry_flow": module("homeassistant.data_entry_flow",
                                                    FlowResult=dict, section=None),
            "homeassistant.helpers": module("homeassistant.helpers", selector=selectors),
            "sec_flow.dyson_api": module("sec_flow.dyson_api",
                authenticate_v1=None, initiate_v3_auth=None, verify_v3_auth=None,
                get_devices=self.get_devices, DysonAuthError=Exception, DysonApiError=ApiError),
        }))
        self.flow = importlib.import_module("sec_flow.config_flow")
        self.devices_error = None

    async def get_devices(self, token):
        if self.devices_error:
            raise self.devices_error
        return [{"Serial": "SER-1", "ProductType": "804", "Name": "Robot"}]

    def step(self, serial="SER-1", country="GB"):
        flow = self.flow.DysonConfigFlow()
        flow.hass = SimpleNamespace(config=SimpleNamespace(country="GB"))
        return asyncio.run(flow.async_step_token(
            {"token": "tok", "serial": serial, "country": country}))

    def test_token_step(self):
        self.assertEqual(self.step()["type"], "create_entry")
        self.assertEqual(self.step(serial="SER/#")["errors"], {"base": "invalid_serial"})
        self.assertEqual(self.step(country="GBR")["errors"], {"base": "invalid_country"})
        self.devices_error = ApiError("rejected", status=401)
        self.assertEqual(self.step()["errors"], {"base": "invalid_auth"},
                         "a rejected token must not be saved")
        self.devices_error = ApiError("outage", status=503)
        self.assertEqual(self.step()["type"], "create_entry", "outage keeps the fallback")
        self.devices_error = OSError("dns")
        self.assertEqual(self.step()["type"], "create_entry")

    def test_serial_validation(self):
        for good in ["7VD-UK-ABC1234A", "NK6EU12345", "ABC"]:
            self.assertTrue(self.flow._valid_serial(good), good)
        for bad in ["", "AB", "SER/../x", "SER#", "SER+", "ser lower", "A" * 40, "../etc"]:
            self.assertFalse(self.flow._valid_serial(bad), bad)

    def test_country_validation(self):
        self.assertTrue(self.flow._valid_country("GB"))
        for bad in ["", "G", "GBR", "g1", "G&"]:
            self.assertFalse(self.flow._valid_country(bad), bad)


if __name__ == "__main__":
    unittest.main()
