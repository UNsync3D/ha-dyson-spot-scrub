"""Config flow for Dyson Spot+Scrub AI — v1.1."""
from __future__ import annotations

import logging
import re
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import (
    DOMAIN,
    CONF_AUTH_TOKEN,
    CONF_SERIAL,
    CONF_DEVICE_NAME,
    CONF_PRODUCT_TYPE,
    CONF_MQTT_PREFIX,
    CONF_COUNTRY,
    ROBOT_PRODUCT_PREFIXES,
)
from .dyson_api import (
    authenticate_v1,
    initiate_v3_auth,
    verify_v3_auth,
    get_devices,
    DysonAuthError,
    DysonApiError,
)

_LOGGER = logging.getLogger(__name__)


# ── Schema helpers ─────────────────────────────────────────────────────────────

def _login_schema(default_country: str) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required("email"):                           str,
            vol.Required("password"):                        str,
            vol.Required("country", default=default_country): str,
        }
    )


def _otp_schema() -> vol.Schema:
    return vol.Schema({vol.Required("otp_code"): str})


def _token_schema(default_country: str) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required("token"):                           str,
            vol.Required("serial"):                          str,
            vol.Required("country", default=default_country): str,
        }
    )


def _ha_country(hass) -> str:
    """Return the HA-configured country code, falling back to 'GB'."""
    return (getattr(hass.config, "country", None) or "GB").upper()


def _is_robot(device: dict) -> bool:
    pt = (device.get("ProductType") or device.get("productType") or "").upper()
    return any(pt.startswith(p) for p in ROBOT_PRODUCT_PREFIXES)


def _device_name(device: dict) -> str:
    raw = device.get("Name") or device.get("name") or "Dyson Robot"
    return re.sub(r"[^\w\s'.,!?-]", "", raw).strip()


# ── Config flow ────────────────────────────────────────────────────────────────

class DysonConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Dyson Spot+Scrub AI."""

    VERSION = 1

    def __init__(self) -> None:
        self._token:        str = ""
        self._country:      str = "GB"
        self._email:        str = ""
        self._password:     str = ""
        self._challenge_id: str = ""
        self._robots:       list[dict] = []

    # ── Entry point — choose auth method ──────────────────────────────────────

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show menu: log in with account or paste a bearer token."""
        return self.async_show_menu(
            step_id="user",
            menu_options=["login", "token"],
        )

    # ── Step 1a: email + password login ───────────────────────────────────────

    async def async_step_login(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Accept email and password, try v1 then v3 OTP."""
        errors: dict[str, str] = {}

        if user_input is not None:
            email    = user_input["email"].strip()
            password = user_input["password"]
            country  = user_input["country"].strip().upper()

            self._email    = email
            self._password = password
            self._country  = country

            # ── Try legacy v1 (no OTP) ────────────────────────────────────────
            try:
                token = await authenticate_v1(email, password, country)
                self._token = token
                return await self._fetch_robots()
            except DysonAuthError:
                pass  # expected — fall through to v3
            except Exception as exc:
                _LOGGER.warning("v1 auth error: %s", exc)

            # ── Try v3 OTP ────────────────────────────────────────────────────
            try:
                challenge_id = await initiate_v3_auth(email, password, country)
                self._challenge_id = challenge_id
                return await self.async_step_otp()
            except DysonAuthError as exc:
                _LOGGER.warning("v3 auth initiation failed: %s", exc)
                msg = str(exc).lower()
                if "401" in msg or "unauthori" in msg:
                    errors["base"] = "invalid_auth"
                else:
                    errors["base"] = "cannot_connect"
            except Exception as exc:
                _LOGGER.exception("Unexpected login error: %s", exc)
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="login",
            data_schema=_login_schema(_ha_country(self.hass)),
            errors=errors,
        )

    # ── Step 1b: OTP verification ─────────────────────────────────────────────

    async def async_step_otp(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Accept the 6-digit OTP code and verify it."""
        errors: dict[str, str] = {}

        if user_input is not None:
            otp_code = user_input["otp_code"].strip()
            try:
                token = await verify_v3_auth(
                    self._email,
                    self._password,
                    self._challenge_id,
                    otp_code,
                )
                self._token = token
                return await self._fetch_robots()
            except DysonAuthError as exc:
                _LOGGER.warning("OTP verification failed: %s", exc)
                msg = str(exc).lower()
                if "401" in msg or "unauthori" in msg:
                    errors["base"] = "invalid_otp"
                else:
                    errors["base"] = "cannot_connect"
            except Exception as exc:
                _LOGGER.exception("Unexpected OTP error: %s", exc)
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="otp",
            data_schema=_otp_schema(),
            errors=errors,
            description_placeholders={"email": self._email},
        )

    # ── Step 1c: paste bearer token directly ──────────────────────────────────

    async def async_step_token(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Accept a bearer token + serial number directly."""
        errors: dict[str, str] = {}

        if user_input is not None:
            token   = user_input["token"].strip()
            serial  = user_input["serial"].strip().upper()
            country = user_input["country"].strip().upper()

            self._token   = token
            self._country = country

            # Try the manifest; fall back to a minimal synthetic entry.
            try:
                devices = await get_devices(token)
                robots  = [d for d in devices if _is_robot(d)]
                match   = next(
                    (d for d in robots
                     if (d.get("Serial") or d.get("serial", "")).upper() == serial),
                    None,
                )
                if match:
                    return self._create_entry(match)
                errors["base"] = "serial_not_found"
            except DysonApiError as exc:
                _LOGGER.warning("Manifest fetch failed (%s) — using serial directly", exc)
                # Token may be valid but manifest unreachable; build minimal entry.
                guessed_prefix = "RB05" if serial.startswith("7VD") else "NROB"
                self._async_abort_entries_match({CONF_SERIAL: serial})
                return self.async_create_entry(
                    title=f"Dyson Robot ({serial})",
                    data={
                        CONF_AUTH_TOKEN:   token,
                        CONF_SERIAL:       serial,
                        CONF_DEVICE_NAME:  f"Dyson Robot ({serial})",
                        CONF_PRODUCT_TYPE: "RB0S",
                        CONF_MQTT_PREFIX:  guessed_prefix,
                        CONF_COUNTRY:      country,
                    },
                )
            except Exception as exc:
                _LOGGER.exception("Token validation error: %s", exc)
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="token",
            data_schema=_token_schema(_ha_country(self.hass)),
            errors=errors,
        )

    # ── Device selection (shared by all auth paths) ───────────────────────────

    async def _fetch_robots(self) -> FlowResult:
        """Fetch the manifest and route to device selection or direct entry."""
        try:
            devices = await get_devices(self._token)
        except Exception as exc:
            _LOGGER.error("Could not fetch device manifest: %s", exc)
            return self.async_abort(reason="cannot_connect")

        self._robots = [d for d in devices if _is_robot(d)]

        if not self._robots:
            return self.async_abort(reason="no_robots_found")
        if len(self._robots) == 1:
            return self._create_entry(self._robots[0])
        return await self.async_step_select_robot()

    async def async_step_select_robot(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Choose which robot to add (multi-robot accounts only)."""
        if user_input is not None:
            serial = user_input["serial"]
            device = next(
                (d for d in self._robots
                 if (d.get("Serial") or d.get("serial")) == serial),
                None,
            )
            if device:
                return self._create_entry(device)

        options = {
            (d.get("Serial") or d.get("serial")): _device_name(d)
            for d in self._robots
        }
        schema = vol.Schema({vol.Required("serial"): vol.In(options)})
        return self.async_show_form(step_id="select_robot", data_schema=schema)

    def _create_entry(self, device: dict) -> FlowResult:
        serial  = device.get("Serial") or device.get("serial", "")
        name    = _device_name(device)
        pt      = device.get("ProductType") or device.get("productType", "RB0S")

        version = device.get("Version") or device.get("version") or ""
        prefix  = (
            device.get("mqttRootTopicLevel")
            or (version[:4].upper() if version.upper().startswith("RB") else None)
            or "NROB"
        )

        self._async_abort_entries_match({CONF_SERIAL: serial})

        return self.async_create_entry(
            title=name,
            data={
                CONF_AUTH_TOKEN:   self._token,
                CONF_SERIAL:       serial,
                CONF_DEVICE_NAME:  name,
                CONF_PRODUCT_TYPE: pt,
                CONF_MQTT_PREFIX:  prefix,
                CONF_COUNTRY:      self._country,
            },
        )
