"""Config flow for Dyson Spot+Scrub AI — v1.2."""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult, section
from homeassistant.helpers import selector

from .const import (
    DOMAIN,
    CONF_AUTH_TOKEN,
    CONF_SERIAL,
    CONF_DEVICE_NAME,
    CONF_PRODUCT_TYPE,
    CONF_MQTT_PREFIX,
    CONF_COUNTRY,
    CONF_CACHED_ROOMS,
    COUNTRY_PATTERN,
    MQTT_PREFIX_PATTERN,
    ROBOT_PRODUCT_PREFIXES,
    SERIAL_PATTERN,
    MAP_RETENTION_DEFAULTS,
    MAP_APPEARANCE_DEFAULTS,
    MAP_VISIBILITY_DEFAULTS,
    MAP_NUMBER_RANGES,
)
from .map_options import map_options
from .dyson_api import (
    MYDYSON_APP_VERSION,
    get_minimum_app_version,
    get_user_status,
    initiate_v3_auth,
    version_tuple,
    verify_v3_auth,
    get_devices,
    DysonAuthError,
    DysonApiError,
)

_LOGGER = logging.getLogger(__name__)

# The iOS app requests the code ~1.5-2 s after its user-status check.
_USER_STATUS_PAUSE = 1.5


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


def _valid_serial(serial: str) -> bool:
    return re.fullmatch(SERIAL_PATTERN, serial) is not None


def _valid_country(country: str) -> bool:
    return re.fullmatch(COUNTRY_PATTERN, country) is not None


def _device_name(device: dict) -> str:
    raw = device.get("Name") or device.get("name") or "Dyson Robot"
    return re.sub(r"[^\w\s'.,!?-]", "", raw).strip()


# ── Config flow ────────────────────────────────────────────────────────────────

class DysonConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Dyson Spot+Scrub AI."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Home Assistant supplies the entry through OptionsFlow.config_entry."""
        return DysonOptionsFlow()

    def __init__(self) -> None:
        self._token:        str = ""
        self._country:      str = "GB"
        self._email:        str = ""
        self._password:     str = ""
        self._challenge_id: str = ""
        self._robots:       list[dict] = []
        self._selected_device: dict = {}

    # ── Entry point — choose auth method ──────────────────────────────────────

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Choose between email + OTP login and pasting a bearer token.

        TEST BRANCH: email login re-enabled for a single supervised attempt.
        It has not yet been confirmed end to end. Repeated failed attempts can
        get a Dyson account rate-limited, so the flow makes one request per
        step and reports 429 explicitly.
        """
        return self.async_show_menu(
            step_id="user",
            menu_options={
                "login": "Log in with Dyson account (email + OTP)",
                "token": "Enter a bearer token directly",
            },
        )

    # ── Step 1a: email + password login ───────────────────────────────────────

    @staticmethod
    def _auth_error(exc: Exception, rejected: str) -> str:
        """Map a login failure to a form error using its HTTP status."""
        status = getattr(exc, "status", None)
        if status in (400, 401, 403):
            return rejected
        if status == 429:
            return "rate_limited"
        return "cannot_connect"

    async def async_step_login(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Accept email, password and country, then request an emailed OTP.

        Mirrors the MyDyson iOS app's captured sequence (Aug 2026):
          1. GET  provisioningservice/application/ios/version (minimum version)
          2. POST userregistration/email/userstatus  {"email"}
          3. POST userregistration/email/auth        {"email"}  -> challengeId
          4. POST userregistration/email/verify      (async_step_otp)
        Steps 1-2 send no password and trigger no email. If step 2 shows the
        account isn't ACTIVE or doesn't use EMAIL_PWD_2FA, we stop BEFORE
        requesting a code. The legacy v1 password login is not attempted.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            email    = user_input["email"].strip()
            password = user_input["password"]
            country  = user_input["country"].strip().upper()

            self._email    = email
            self._password = password
            self._country  = country

            if not _valid_country(country):
                errors["base"] = "invalid_country"
            else:
                errors = await self._pre_login_checks(email, country)
            if not errors:
                try:
                    self._challenge_id = await initiate_v3_auth(email, country)
                    _LOGGER.debug("OTP requested; waiting for the emailed code")
                    return await self.async_step_otp()
                except (DysonAuthError, DysonApiError) as exc:
                    errors["base"] = self._auth_error(exc, "invalid_auth")
                    _LOGGER.warning("OTP request failed (HTTP %s)",
                                    getattr(exc, "status", None))
                except Exception as exc:  # noqa: BLE001 — network etc.
                    _LOGGER.warning("OTP request failed: %s", type(exc).__name__)
                    errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="login",
            data_schema=_login_schema(_ha_country(self.hass)),
            errors=errors,
        )

    async def _pre_login_checks(self, email: str, country: str) -> dict[str, str]:
        """The app's version and user-status checks; errors stop the login.

        A failed version check is not fatal (the app treats it as a startup
        check). A failed or unexpected user status IS: requesting a code for
        an account Dyson doesn't report as ACTIVE + EMAIL_PWD_2FA risks the
        same kind of block the maintainer's account suffered.
        """
        try:
            minimum = await get_minimum_app_version()
            _LOGGER.debug("Dyson minimum app version %s (we report %s)",
                          minimum, MYDYSON_APP_VERSION)
            if version_tuple(minimum) > version_tuple(MYDYSON_APP_VERSION):
                _LOGGER.warning("Dyson now requires app version %s; integration "
                                "reports %s - not requesting a code", minimum,
                                MYDYSON_APP_VERSION)
                return {"base": "app_version_unsupported"}
        except Exception as exc:  # noqa: BLE001 — informational check only
            _LOGGER.debug("App version check failed (HTTP %s); continuing",
                          getattr(exc, "status", type(exc).__name__))

        try:
            status = await get_user_status(email, country)
        except (DysonAuthError, DysonApiError) as exc:
            _LOGGER.warning("User status check failed (HTTP %s)",
                            getattr(exc, "status", None))
            return {"base": self._auth_error(exc, "invalid_auth")}
        except Exception as exc:  # noqa: BLE001 — network etc.
            _LOGGER.warning("User status check failed: %s", type(exc).__name__)
            return {"base": "cannot_connect"}

        account, method = status.get("accountStatus"), status.get("authenticationMethod")
        _LOGGER.debug("Dyson account status %s, method %s", account, method)
        if account != "ACTIVE":
            _LOGGER.warning("Dyson reports account status %r - not requesting a code", account)
            return {"base": "account_not_active"}
        if method != "EMAIL_PWD_2FA":
            _LOGGER.warning("Dyson reports sign-in method %r - not requesting a code", method)
            return {"base": "unsupported_auth_method"}

        await asyncio.sleep(_USER_STATUS_PAUSE)
        return {}

    # ── Step 1b: OTP verification ─────────────────────────────────────────────

    async def async_step_otp(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Accept the 6-digit OTP code and verify it."""
        errors: dict[str, str] = {}

        if user_input is not None:
            otp_code = re.sub(r"\s+", "", user_input["otp_code"])
            try:
                # Same country as the OTP request (it previously defaulted to GB).
                token = await verify_v3_auth(
                    self._email,
                    self._password,
                    self._challenge_id,
                    otp_code,
                    country=self._country,
                )
                self._token = token
                self._password = ""   # no longer needed; don't keep it in memory
                return await self._fetch_robots()
            except (DysonAuthError, DysonApiError) as exc:
                errors["base"] = self._auth_error(exc, "invalid_otp")
                _LOGGER.warning("OTP verification failed (HTTP %s)",
                                getattr(exc, "status", None))
            except Exception as exc:  # noqa: BLE001 — network etc.
                _LOGGER.warning("OTP verification failed: %s", type(exc).__name__)
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

            # Validate before the serial reaches URLs, MQTT topics or storage.
            if not _valid_serial(serial):
                errors["base"] = "invalid_serial"
            elif not _valid_country(country):
                errors["base"] = "invalid_country"
            if errors:
                return self.async_show_form(
                    step_id="token",
                    data_schema=_token_schema(_ha_country(self.hass)),
                    errors=errors,
                )

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
                    self._selected_device = match
                    return self._create_entry(self._selected_device)
                errors["base"] = "serial_not_found"
            except DysonApiError as exc:
                if getattr(exc, "status", None) not in (401, 403):
                    # Treat other API errors like an outage (below).
                    return self._create_synthetic_entry(serial, exc)
                # The token itself was rejected. Previously this fell through to
                # the synthetic entry and saved a dead token, leaving the
                # integration permanently unavailable.
                _LOGGER.warning("Bearer token rejected by Dyson (HTTP %s)", exc.status)
                errors["base"] = "invalid_auth"
            except Exception as exc:
                return self._create_synthetic_entry(serial, exc)

        return self.async_show_form(
            step_id="token",
            data_schema=_token_schema(_ha_country(self.hass)),
            errors=errors,
        )

    def _create_synthetic_entry(self, serial: str, exc: Exception) -> FlowResult:
        """Create an entry without the manifest (Dyson API temporarily down).

        The MQTT prefix is a guess; dyson_mqtt auto-corrects and persists it
        from the first inbound message.
        """
        _LOGGER.warning(
            "Manifest fetch failed (%s) — continuing with synthetic entry", exc,
        )
        guessed_prefix = "RB05" if serial.startswith("7VD") else "NROB"
        self._selected_device = {
            "Serial":            serial,
            "Name":              f"Dyson Robot ({serial})",
            "ProductType":       "RB0S",
            "mqttRootTopicLevel": guessed_prefix,
        }
        return self._create_entry(self._selected_device)

    # ── Device selection (shared by login/OTP paths) ──────────────────────────

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
            self._selected_device = self._robots[0]
            return self._create_entry(self._selected_device)
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
                self._selected_device = device
                return self._create_entry(self._selected_device)

        options = {
            (d.get("Serial") or d.get("serial")): _device_name(d)
            for d in self._robots
        }
        schema = vol.Schema({vol.Required("serial"): vol.In(options)})
        return self.async_show_form(step_id="select_robot", data_schema=schema)

    def _create_entry(self, device: dict) -> FlowResult:
        """Store the bearer token and device identity in the config entry.

        The token is kept in entry.data, which HA stores unencrypted in
        .storage/core.config_entries — standard for HA integrations, but it
        means backups of the config directory contain the Dyson token.
        """
        serial  = device.get("Serial") or device.get("serial", "")
        name    = _device_name(device)
        pt      = device.get("ProductType") or device.get("productType", "RB0S")

        version = device.get("Version") or device.get("version") or ""
        prefix  = (
            device.get("mqttRootTopicLevel")
            or (version[:4].upper() if version.upper().startswith("RB") else None)
            or "NROB"
        )
        # Manifest values are remote data: never let them inject MQTT wildcards.
        if not re.fullmatch(MQTT_PREFIX_PATTERN, str(prefix)):
            prefix = "NROB"
        if not _valid_serial(str(serial).upper()):
            return self.async_abort(reason="invalid_serial")

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
                CONF_CACHED_ROOMS: [],
            },
        )


class DysonOptionsFlow(config_entries.OptionsFlow):
    """Single map-settings form, with flat storage for existing camera options."""

    async def async_step_init(self, user_input=None) -> FlowResult:
        values = map_options(self.config_entry.options)
        top_defaults = {
            **MAP_RETENTION_DEFAULTS,
            "map_transparent_background": False,
        }
        groups = {
            "line_settings_section": MAP_APPEARANCE_DEFAULTS,
            "visibility_settings_section": {
                key: value for key, value in MAP_VISIBILITY_DEFAULTS.items()
                if key != "map_transparent_background"
            },
        }
        errors = {}
        invalid_groups = set()
        if user_input is not None:
            submitted = {key: user_input[key] for key in top_defaults if key in user_input}
            for name, defaults in groups.items():
                group_input = user_input.get(name, {})
                if not isinstance(group_input, dict):
                    errors["base"] = "invalid_map_option"
                    invalid_groups.add(name)
                    continue
                submitted.update({key: group_input[key] for key in defaults if key in group_input})
            validated = map_options({**values, **submitted})
            for key, value in submitted.items():
                if (validated[key] != value
                        or isinstance(values[key], bool) and not isinstance(value, bool)
                        or key in MAP_NUMBER_RANGES and isinstance(value, bool)):
                    errors["base"] = "invalid_map_option"
                    invalid_groups.update(name for name, defaults in groups.items() if key in defaults)
            if not errors:
                # Section nesting is only a UI concern. Keep existing option keys
                # flat so the camera and older saved settings remain compatible.
                options = dict(self.config_entry.options)
                options.update({key: validated[key] for key in submitted})
                return self.async_create_entry(title="", data=options)
            values.update(submitted)

        fields = self._fields(top_defaults, values)
        for name, defaults in groups.items():
            # A section default takes precedence over its children's defaults
            # in the frontend. An empty dict leaves every child uninitialized.
            fields[vol.Optional(name, default={key: values[key] for key in defaults})] = section(
                vol.Schema(self._fields(defaults, values)),
                {"collapsed": name not in invalid_groups},
            )
        return self.async_show_form(
            step_id="init", data_schema=vol.Schema(fields), errors=errors,
        )

    @staticmethod
    def _fields(defaults, values):
        fields = {}
        for key, default in defaults.items():
            if isinstance(default, bool):
                field = selector.BooleanSelector()
            elif key in MAP_NUMBER_RANGES:
                minimum, maximum = MAP_NUMBER_RANGES[key]
                field = selector.NumberSelector(selector.NumberSelectorConfig(
                    min=minimum, max=maximum, step=1,
                    mode=selector.NumberSelectorMode.SLIDER,
                    unit_of_measurement="min" if key == "map_retention_minutes" else "px",
                ))
            else:
                field = selector.ColorRGBSelector()
            marker = vol.Optional if isinstance(default, bool) else vol.Required
            fields[marker(key, default=values[key])] = field
        return fields
