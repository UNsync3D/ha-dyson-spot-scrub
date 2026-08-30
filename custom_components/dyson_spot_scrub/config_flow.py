"""Config flow for Dyson Spot+Scrub AI."""
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
    get_devices,
    DysonApiError,
)

_LOGGER = logging.getLogger(__name__)


def _token_schema(default_country: str) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required("token"):   str,
            vol.Required("serial"):  str,
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


class DysonConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Dyson Spot+Scrub AI."""

    VERSION = 1

    def __init__(self) -> None:
        self._token:   str = ""
        self._country: str = "GB"
        self._robots:  list[dict] = []

    # ── Entry point ───────────────────────────────────────────────────────────

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Go straight to token-paste setup."""
        return await self.async_step_token()

    # ── Token paste ───────────────────────────────────────────────────────────

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

            # Try to discover the device from the manifest; fall back to a
            # minimal synthetic device if the manifest endpoint is unavailable.
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
            except Exception as exc:
                _LOGGER.warning(
                    "Manifest fetch failed (%s) — using serial directly", exc
                )

            # Manifest unavailable — build a minimal entry from what we know.
            # Guess "RB05" for Spot+Scrub serials; dyson_mqtt.py will
            # auto-correct at runtime if this is still wrong.
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

        return self.async_show_form(
            step_id="token",
            data_schema=_token_schema(_ha_country(self.hass)),
            errors=errors,
        )

    # ── Device discovery (shared) ─────────────────────────────────────────────

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

        # Derive the MQTT topic prefix.  The manifest field is absent for
        # RB0S robots; fall back to the first 4 chars of the firmware Version
        # string (e.g. "RB05PR.01.109…" → "RB05").  Last resort: "NROB".
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
