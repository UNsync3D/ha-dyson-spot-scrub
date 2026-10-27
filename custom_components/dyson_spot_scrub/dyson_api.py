"""Dyson cloud REST API client — auth, device manifest, IoT credentials.

Auth flow (confirmed working Sep 2026, from Proxyman iOS captures):
  POST appapi.cp.dyson.com/v3/userregistration/email/auth?country={CC}&culture=en-{CC}
    Body: { "email": "{email}" }   ← password NOT sent here
    → challengeId (OTP email sent)
  POST appapi.cp.dyson.com/v3/userregistration/email/verify?country={CC}
    Body: { "email": ..., "password": ..., "challengeId": ..., "otpCode": ... }
    → { token, account }

NOTE: Use appapi.cp.dyson.com for ALL calls — api.cp.dyson.com is behind
Cloudflare mTLS (as of late Aug 2026) and returns 403 without a client cert.

All subsequent REST calls use: Authorization: Bearer {token}

IoT credentials (per device):
  POST appapi.cp.dyson.com/v2/authorize/iot-credentials
    Body: { "Serial": "{serial}" }
    → { IoTCredentials: { ClientId, TokenKey, TokenValue, TokenSignature,
                          CustomAuthorizerName }, Endpoint }

MQTT connection uses a custom AWS authorizer via WebSocket:
  wss://{Endpoint}/mqtt
    ?x-amz-customauthorizer-name={CustomAuthorizerName}
    &token={TokenValue}
    &x-amz-customauthorizer-signature={URL-encoded TokenSignature}
"""
from __future__ import annotations

import base64
import json
import logging
import math
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, AsyncIterator
from urllib.parse import quote

import aiohttp

_LOGGER = logging.getLogger(__name__)

API_HOST = "appapi.cp.dyson.com"

# MyDyson app version to report. Same format as the App Store version
# (a Proxyman capture of the iOS app sent X-App-Version 6.4.26340 when that
# was current). Update here after app releases; both version headers use it.
# 6.4.26420: App Store, released 24 Oct 2026.
MYDYSON_APP_VERSION = "6.4.26420"

# Mimic the MyDyson mobile app; Dyson's API rejects unknown clients.
# NOTE: the User-Agent is still an Android string while X-Platform says
# "ios". The real iOS app sends "DysonLink/<build> CFNetwork/<v> Darwin/<v>";
# switching needs a fresh capture so build/iOS numbers match this version.
_HEADERS = {
    "Content-Type":            "application/json",
    "User-Agent":              "Dalvik/2.1.0 (Linux; U; Android 11; Build/RQ3A.210905.001)",
    "Accept":                  "application/json, text/plain, */*",
    "Accept-Language":         "en-AU,en;q=0.9",
    "X-App-Version":           MYDYSON_APP_VERSION,
    "X-Platform":              "ios",
    "X-Dyson-LinkApp-Version": MYDYSON_APP_VERSION,
}

# Response bodies are echoed into exception messages (and therefore HA logs).
#
# Policy (tightened after review, 26 Oct 2026):
#   * JSON bodies are logged with sensitive keys replaced by *** and every
#     string value scrubbed with the text rules below.
#   * Non-JSON bodies (HTML error pages, plain text, empty) are NEVER echoed;
#     only their length is logged. Pattern-matching arbitrary text can't
#     guarantee that every credential format is caught, so we don't rely on it
#     for unstructured content.
#   * The result is then length-capped so an endpoint can't flood the log.
_MAX_BODY_IN_ERROR = 200

# Any JSON key containing one of these (case-insensitive) has its value
# replaced before logging: bearer/IoT tokens, signatures, passwords, OTP
# challenge IDs, Basic-auth account secrets, API keys and local MQTT
# credentials.
_SENSITIVE_KEY_PARTS = (
    "token", "password", "passwd", "secret", "signature", "credential",
    "authorization", "challenge", "otp", "account", "apikey", "api_key",
    "api-key", "access_key", "access-key", "session", "cookie",
)
_REDACTED = "***"

_KEY_ALTERNATION = "|".join(map(re.escape, _SENSITIVE_KEY_PARTS))
_AUTH_SCHEMES = r"(?:bearer|basic|digest|negotiate|token)"
# Scrubbing rules for text found INSIDE JSON string values, applied in order:
#  1. key/value pairs: an optionally quoted key containing a sensitive part,
#     then ":" or "=", then an optional auth scheme word and the whole value -
#     single-quoted, double-quoted, or up to the next delimiter.
#  2. a bare "Bearer xyz" / "Basic xyz" credential with no key in front.
_SENSITIVE_PAIR = re.compile(
    r"(?i)([\"']?[\w.-]*(?:" + _KEY_ALTERNATION + r")[\w.-]*[\"']?\s*[:=]\s*)"
    r"(?:" + _AUTH_SCHEMES + r"\s+)?"
    r"(?:'[^']*'|\"[^\"]*\"|[^\s,&;}<>\"']+)"
)
_BARE_CREDENTIAL = re.compile(r"(?i)\b(bearer|basic|digest|negotiate)\s+[A-Za-z0-9._~+/=%-]+")


def _scrub_text(text: str) -> str:
    """Redact credential-looking pairs inside a short JSON string value."""
    text = _SENSITIVE_PAIR.sub(lambda m: m.group(1) + _REDACTED, text)
    return _BARE_CREDENTIAL.sub(lambda m: m.group(1) + " " + _REDACTED, text)


# ── Exceptions ────────────────────────────────────────────────────────────────


class DysonAuthError(Exception):
    """Raised when authentication with Dyson's API fails.

    ``status`` is the HTTP status (None for non-HTTP failures such as an
    unparseable 200 reply), so the login flow can tell a wrong password or
    OTP (401/403) from rate limiting (429) or an outage.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class DysonApiError(Exception):
    """Raised when a Dyson API call fails.

    ``status`` carries the HTTP status code (None for non-HTTP failures) so
    callers can tell an expired token (401/403) from a transient outage
    without parsing the message text.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class DysonRateLimitError(DysonApiError):
    """Raised for HTTP 429, with a retry delay independent of the message."""

    def __init__(self, message: str, retry_after: float) -> None:
        super().__init__(message, status=429)
        self.retry_after = retry_after


# ── Helpers ───────────────────────────────────────────────────────────────────


def _parse_retry_after(value: str | None) -> float:
    """Parse Retry-After seconds or an HTTP date; default to 30 seconds."""
    if value is not None:
        value = value.strip()
        try:
            if value.isascii() and value.isdecimal():
                seconds = float(value)
                return seconds if math.isfinite(seconds) else 30.0
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is not None:
                return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            pass
    return 30.0


def _is_sensitive_key(key: Any) -> bool:
    lowered = str(key).lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def _redact(data: Any) -> Any:
    """Return a copy of a JSON value with sensitive fields replaced by ***."""
    if isinstance(data, dict):
        return {k: _REDACTED if _is_sensitive_key(k) else _redact(v)
                for k, v in data.items()}
    if isinstance(data, list):
        return [_redact(v) for v in data]
    if isinstance(data, str):
        return _scrub_text(data)
    return data


def _snippet(data: Any) -> str:
    """Return a redacted, length-capped description of a body for errors.

    Strings are parsed as JSON first. A body that isn't JSON is summarised
    by length only - its content is never included (see policy above).
    Redaction happens before truncation, so a secret can't survive by
    straddling the cut-off point.
    """
    if isinstance(data, (str, bytes)):
        try:
            data = json.loads(data)
        except ValueError:
            return f"<non-JSON body, {len(data)} chars, not logged>" if data else "<empty body>"
    text = repr(_redact(data))
    if len(text) > _MAX_BODY_IN_ERROR:
        return text[:_MAX_BODY_IN_ERROR] + "…"
    return text


def _seg(value: Any) -> str:
    """URL-encode one path segment.

    Serials and map IDs come from user input / API data. Encoding them stops
    a value such as "../../v1/..." from redirecting the request to a
    different endpoint on the same host.
    """
    return quote(str(value), safe="")


def _basic_token(account: str, password: str) -> str:
    """Build the legacy Basic auth token used by older Dyson API versions."""
    return base64.b64encode(f"{account}:{password}".encode()).decode()


def _connector() -> aiohttp.TCPConnector:
    return aiohttp.TCPConnector(ssl=True)


@asynccontextmanager
async def _session(session: aiohttp.ClientSession | None) -> AsyncIterator[Any]:
    """Yield the caller's shared session, or a short-lived one.

    Passing Home Assistant's shared session avoids a fresh TCP + TLS
    handshake on every call — significant for the live map, which is
    polled every 5 s while cleaning. The temporary session is kept for
    callers (config flow, tests) that don't have one.
    """
    if session is not None:
        yield session
        return
    async with aiohttp.ClientSession(connector=_connector()) as temp:
        yield temp


async def _json(resp: aiohttp.ClientResponse) -> Any:
    """Parse JSON from a response, raising DysonAuthError on empty/invalid body."""
    try:
        return await resp.json(content_type=None)
    except (ValueError, aiohttp.ContentTypeError) as exc:  # JSONDecodeError is a ValueError
        body = await resp.text()
        raise DysonAuthError(
            f"Non-JSON response (HTTP {resp.status}): {_snippet(body)}",
            status=resp.status,
        ) from exc


async def _error_body(resp: aiohttp.ClientResponse) -> Any:
    """Best-effort body of a failed response, for the error message only.

    Never raises: a rejection's HTTP status must survive whatever the body
    looks like (HTML error page, plain text, empty). Callers rely on that
    status — e.g. the config flow must see 401/403 as "token rejected", not
    as a parsing failure that looks like an outage.
    """
    try:
        return await resp.json(content_type=None)
    except Exception:  # noqa: BLE001 — not JSON; fall back to text
        pass
    try:
        return await resp.text()
    except Exception:  # noqa: BLE001 — body unreadable; status is what matters
        return ""


def _auth_headers(token: str) -> dict[str, str]:
    return {**_HEADERS, "Authorization": f"Bearer {token}"}


async def _request(
    method: str,
    path: str,
    *,
    token: str | None = None,
    params: dict[str, str] | None = None,
    json_body: Any = None,
    session: aiohttp.ClientSession | None = None,
    label: str,
    expect: type | tuple[type, ...] | None = dict,
    required_key: str | None = None,
    rate_limited: bool = False,
    error: type[Exception] = DysonApiError,
) -> tuple[int, Any]:
    """Perform one API call and return (status, parsed JSON).

    Shared by every endpoint so URL building, headers, rate-limit handling
    and error formatting live in one place.

    expect:       JSON type a 200 response must have; None skips JSON parsing
                  entirely (used by the PUT that returns an empty body).
    required_key: key a 200 dict response must contain.
    rate_limited: raise DysonRateLimitError on HTTP 429.
    error:        exception class raised on failure.
    """
    url = f"https://{API_HOST}{path}"
    headers = _auth_headers(token) if token else _HEADERS
    kwargs: dict[str, Any] = {"headers": headers}
    if params:
        kwargs["params"] = params
    if json_body is not None:
        kwargs["json"] = json_body
    async with _session(session) as http:
        # getattr keeps the call shape (session.get / .post / .put) that the
        # offline tests mock.
        async with getattr(http, method.lower())(url, **kwargs) as resp:
            # Status and content type only - never the body, query or headers,
            # which can carry credentials. Enough to diagnose login problems.
            _LOGGER.debug("%s: HTTP %s (%s)", label, resp.status,
                          resp.headers.get("Content-Type", "no content type"))
            if rate_limited and resp.status == 429:
                retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                raise DysonRateLimitError(
                    f"{label} failed (HTTP 429 Retry-After {retry_after:g})",
                    retry_after=retry_after,
                )
            if expect is None:
                if resp.status == 200:
                    return resp.status, None
                body = await resp.text()
                data: Any = body
            elif resp.status == 200:
                data = await _json(resp)
                if isinstance(data, expect) and (required_key is None or required_key in data):
                    return resp.status, data
            else:
                # Non-200: build the message from whatever the body is, so the
                # status-carrying error below is always the one raised.
                data = await _error_body(resp)
            message = f"{label} failed (HTTP {resp.status}): {_snippet(data)}"
            raise error(message, status=resp.status)


# ── Auth ──────────────────────────────────────────────────────────────────────


async def authenticate_v1(email: str, password: str, country: str = "GB") -> str:
    """Try legacy v1 auth (no OTP). Returns a Basic token string or raises."""
    _, data = await _request(
        "POST", "/v1/userregistration/authenticate",
        params={"country": country},
        json_body={"Email": email, "Password": password},
        label="v1 auth", required_key="Account", error=DysonAuthError,
    )
    if not data.get("Password"):
        raise DysonAuthError("v1 auth failed: response had no password field")
    return _basic_token(data["Account"], data["Password"])


async def initiate_v3_auth(email: str, country: str = "GB") -> str:
    """Start v3 OTP flow. Returns challengeId (OTP email sent to user).

    NOTE: password is NOT sent at this step — only email goes in the body.
    Password is sent in verify_v3_auth. Sending password here causes 401s.
    """
    _, data = await _request(
        "POST", "/v3/userregistration/email/auth",
        params={"country": country, "culture": f"en-{country}"},
        json_body={"email": email},
        label="v3 auth initiation", required_key="challengeId",
        error=DysonAuthError,
    )
    return data["challengeId"]


async def verify_v3_auth(
    email: str, password: str, challenge_id: str, otp_code: str,
    country: str = "GB",
) -> str:
    """Verify OTP. Returns bearer token string."""
    _, data = await _request(
        "POST", "/v3/userregistration/email/verify",
        params={"country": country},
        json_body={
            "email": email,
            "password": password,
            "challengeId": challenge_id,
            "otpCode": otp_code,
        },
        label="v3 OTP verification", error=DysonAuthError,
    )
    if data.get("token"):
        return data["token"]
    # Older API: build Basic token from account + password
    if data.get("account") and data.get("password"):
        return _basic_token(data["account"], data["password"])
    # Deliberately no body here: a 200 reply may contain account secrets.
    raise DysonAuthError("v3 OTP verification failed: no token in response")


# ── Manifest / devices ────────────────────────────────────────────────────────


async def get_devices(
    token: str, *, session: aiohttp.ClientSession | None = None,
) -> list[dict[str, Any]]:
    """Return the full device manifest list."""
    _, data = await _request(
        "GET", "/v2/provisioningservice/manifest", token=token,
        session=session, label="Manifest fetch", expect=list,
    )
    return data


async def get_iot_credentials(
    token: str, serial: str, *, session: aiohttp.ClientSession | None = None,
) -> dict[str, str]:
    """Return IoT connection credentials for one device."""
    _, data = await _request(
        "POST", "/v2/authorize/iot-credentials", token=token,
        json_body={"Serial": serial}, session=session,
        label="IoT credentials fetch",
    )
    iot = data.get("IoTCredentials") or data
    return {
        "endpoint":        data.get("Endpoint") or data.get("endpoint", ""),
        "token_value":     iot.get("TokenValue") or iot.get("tokenValue", ""),
        "token_signature": iot.get("TokenSignature") or iot.get("tokenSignature", ""),
        "client_id":       iot.get("ClientId") or iot.get("clientId", ""),
        "authorizer_name": (
            iot.get("CustomAuthorizerName")
            or iot.get("authorizerName")
            or "cld-iot-credentials-lambda-authorizer"
        ),
    }


async def validate_token(token: str) -> bool:
    """Return True if the token is still accepted by Dyson's API.

    Currently unused; kept for a future re-authentication flow.
    """
    try:
        await get_devices(token)
        return True
    except Exception:  # noqa: BLE001 — any failure means "not usable"
        return False


# ── Map endpoints ─────────────────────────────────────────────────────────────


async def get_live_map(
    token: str, serial: str, *, session: aiohttp.ClientSession | None = None,
) -> dict[str, Any]:
    """Return the live cleaning map for an active cleaning session.

    Endpoint: GET /v1/app/{serial}/live-maps/cleaning
    (Note: v1, not v2 — different API version from the map storage endpoints)

    Call this while the robot is cleaning to get real-time state. Poll every
    2-5 seconds for a live robot position tracker.

    Key fields in the response:
      robotLocation: { x, y, angle, update } — current robot position
      cleanPath: [{ x, y, update }]          — path traced so far this session
      zones[]: each zone includes:
        cleanStatus: "CLEAN_NOT_REQUESTED" | "CLEAN_PENDING" | "CLEANING"
                     | "CLEAN_COMPLETE" | "CLEAN_FAILED"
        visited: [{ x, y }]  — path history for that zone (cumulative)
        presentation: [{ start:{x,y}, end:{x,y}, type:int }]
                      — planned cleaning trajectory (0=perimeter, 1=sweep, 2=turn)
      restrictions: [{ id, points:[{x,y}], behavior:"keepOut" }]
      furniture: [{ id, type, userDefined, points:[{x,y}] }]
      dockLocation: { x, y, angle }
      obstacles: []    — detected temporary obstacles
      dirt: []         — detected dirt concentrations (future capability)
      hazardZones: []  — detected hazardous areas (future capability)

    Raises DysonRateLimitError on HTTP 429, DysonApiError if the robot is not
    currently cleaning (the endpoint may return 404 or an error body when no
    session is active).
    """
    _, data = await _request(
        "GET", f"/v1/app/{_seg(serial)}/live-maps/cleaning", token=token,
        session=session, label="Live map fetch", rate_limited=True,
    )
    return data


async def get_map_metadata(
    token: str, serial: str, *, session: aiohttp.ClientSession | None = None,
) -> list[dict[str, Any]]:
    """Return the list of persistent maps for a device.

    Each map entry includes:
      id, name, isCurrentMap, imageUrl, zones[]
    Zone entries include: id, name, type, area, nameLocation {x, y},
    isSelected, order, settings {cleaningStrategy, cleanType, waterLevel,
    mopPasses, dryPasses, isUvScanOn}.

    Example response:
      [{
        "id": "1787955355",
        "name": "Downstairs",
        "isCurrentMap": true,
        "zones": [{"id": "10", "name": "Living room", "type": "livingRoom",
                   "area": 17.6, ...}],
        "imageUrl": ""
      }]
    """
    _, data = await _request(
        "GET", f"/v2/app/{_seg(serial)}/persistent-map-metadata", token=token,
        session=session, label="Map metadata fetch", expect=list,
    )
    return data


async def get_map(
    token: str, serial: str, map_id: str,
    *, session: aiohttp.ClientSession | None = None,
) -> dict[str, Any]:
    """Return the full persistent map for a device.

    The returned dict contains:
      zones[]: room boundary polygons, zone labels, furniture, restrictions
      visitedPaths[]: cleaning path trace (list of {x, y} waypoints)
      dockLocation: {x, y, angle} of the charging dock
      resolution: map cell size in metres (typically 0.05)

    Each zone in zones[] includes:
      boundary[]: list of {x, y} coordinates forming the room polygon
      furnitureItems[]: list of {id, type, boundary[{x,y}]} detected furniture
      restrictions[]: keep-out zones as {id, boundary[{x,y}]}

    Coordinates are in metres relative to the dock (or robot origin).
    The Y-axis is inverted relative to screen coordinates — negate Y when
    rendering to SVG/canvas (positive Y is behind the robot, i.e. up on screen
    only after the flip).
    """
    _, data = await _request(
        "GET", f"/v2/app/{_seg(serial)}/persistent-maps/{_seg(map_id)}",
        token=token, session=session, label="Map fetch",
    )
    return data


async def update_map_zone_selection(
    token: str, serial: str, map_id: str,
    zones: list[dict[str, Any]],
) -> None:
    """Set zone selection/settings and arm the robot for the next clean.

    Currently unused by the integration (starts go via MQTT
    service.set_preference); kept as documented reference for the REST path.

    This is the REST half of the "Start" button in the Dyson app. The app
    calls this PUT immediately before sending the MQTT start command. It
    persists which zones to clean and in what order, so the MQTT command
    alone is sufficient to kick off the run.

    Sends ALL zones (selected and unselected) with their current settings.
    The server returns an empty 200 body on success.

    Each zone dict must include:
      { "id":         str,    # zone ID, e.g. "11"
        "name":       str,    # display name, e.g. "Dining"
        "type":       str,    # room type, e.g. "dining", "toilet", "kitchen"
        "area":       float,  # room area in m²
        "order":      int,    # cleaning sequence position (1 = first); only
                              # matters for isSelected=True zones
        "isSelected": bool,   # True = clean this zone on next run
        "settings": {
          "cleanType":       str,   # "vacuum" | "mop" | "vacuumAndMop"
          "waterLevel":      str,   # "low" | "medium" | "high"
          "mopPasses":       int,   # typically 1
          "dryPasses":       int,   # typically 1
          "cleaningStrategy":str,   # "auto" (only observed value so far)
          "isUvScanOn":      bool,  # UV sterilisation pass
        } }

    Endpoint: PUT /v2/app/{serial}/persistent-map-metadata/{mapId}
    Note: URL uses persistent-map-metadata (not persistent-maps).
    """
    await _request(
        "PUT", f"/v2/app/{_seg(serial)}/persistent-map-metadata/{_seg(map_id)}",
        token=token, json_body=zones, label="Zone selection update",
        expect=None,
    )


async def get_ota_status(token: str, serial: str) -> str:
    """Return the firmware OTA status for a device (currently unused).

    Returns the otaStatus string, one of:
      "Completed"   — most recent update installed successfully
      "Available"   — update ready to download
      "Downloading" — update being pulled to the robot
      "Installing"  — firmware being applied
      "Failed"      — update attempt failed
    """
    _, data = await _request(
        "GET", f"/v1/assets/devices/{_seg(serial)}/ota", token=token,
        label="OTA status fetch", required_key="otaStatus",
    )
    return data["otaStatus"]


async def get_clean_estimation(
    token: str, serial: str, map_id: str,
    zones: list[dict[str, Any]],
) -> dict[str, Any]:
    """Estimate duration and recharge stops for a planned clean (currently unused).

    `zones` is a list of zone dicts, each containing:
      { "id": str, "area": float, "settings": { cleanType, waterLevel,
        mopPasses, dryPasses, cleaningStrategy, isUvScanOn } }

    Send ALL zones (even unselected ones) — the server expects the full array.
    Returns: { "duration": int (minutes), "charges": int (dock recharge stops) }
    """
    _, data = await _request(
        "POST",
        f"/v2/app/{_seg(serial)}/persistent-maps/{_seg(map_id)}/clean-estimation",
        token=token, json_body={"zones": zones},
        label="Clean estimation", required_key="duration",
    )
    return data


async def get_current_map(
    token: str, serial: str, *, session: aiohttp.ClientSession | None = None,
) -> tuple[str, dict[str, Any]]:
    """Convenience: fetch the active map, returning (map_id, map_data).

    Calls get_map_metadata to find the map with isCurrentMap=True, then
    fetches the full map. Raises DysonApiError if no current map is found.
    """
    maps = await get_map_metadata(token, serial, session=session)
    current = next((m for m in maps if m.get("isCurrentMap")), None)
    if current is None:
        if maps:
            current = maps[0]
        else:
            raise DysonApiError("No maps found for device")
    map_id = str(current["id"])
    map_data = await get_map(token, serial, map_id, session=session)
    return map_id, map_data


# ── Local MQTT credential decryption ─────────────────────────────────────────
#
# LocalCredentials in the manifest are AES-256-CBC encrypted with:
#   key = bytes(range(1, 33))   # 0x01 … 0x20  (publicly known, hardcoded)
#   iv  = bytes(16)             # 16 zero bytes
#
# Newer firmware: decrypted JSON → {"serial": "...", "apPasswordHash": "..."}
# Older firmware: decrypted plaintext is the raw MQTT password.
#
# NOTE: The integration currently connects via the cloud broker only; these
# helpers are unused and kept for a possible local-MQTT mode. The key is
# Dyson's well-known static key, so this is obfuscation, not secrecy — treat
# the decrypted password as a secret anyway and never log it.

_LC_KEY = bytes(range(1, 33))
_LC_IV  = bytes(16)


def decrypt_local_credentials(encrypted_b64: str) -> tuple[str, str]:
    """Decrypt a base64-encoded LocalCredentials blob.

    Returns (mqtt_username, mqtt_password).  If the JSON contains no serial,
    the caller should use the device serial as the username instead.
    """
    try:
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:
        raise DysonApiError(
            "The 'cryptography' package is required for local credential decryption. "
            "Add it to manifest.json requirements or install it manually."
        ) from exc

    try:
        data = base64.b64decode(encrypted_b64, validate=True)
    except Exception as exc:
        raise DysonApiError(f"LocalCredentials is not valid base64: {exc}") from exc

    if not data or len(data) % 16:
        raise DysonApiError("LocalCredentials has an invalid length")

    cipher = Cipher(algorithms.AES(_LC_KEY), modes.CBC(_LC_IV), backend=default_backend())
    decryptor = cipher.decryptor()
    decrypted = decryptor.update(data) + decryptor.finalize()

    # Strip PKCS#7 padding — validate it rather than trusting the last byte,
    # otherwise a corrupt blob silently yields a truncated/garbage password.
    pad_len = decrypted[-1]
    if not 1 <= pad_len <= 16 or decrypted[-pad_len:] != bytes([pad_len]) * pad_len:
        raise DysonApiError("LocalCredentials has invalid padding")
    plaintext = decrypted[:-pad_len].decode("utf-8", errors="replace")

    try:
        obj = json.loads(plaintext)
        if isinstance(obj, dict):
            return obj.get("serial", ""), obj.get("apPasswordHash", "")
    except (json.JSONDecodeError, TypeError):
        pass

    # Older firmware — plaintext is the raw password
    return "", plaintext


async def get_local_credentials(token: str, serial: str) -> tuple[str, str]:
    """Return (mqtt_username, mqtt_password) by decrypting LocalCredentials.

    Fetches the manifest, finds the matching device (case-insensitive serial
    match), decrypts its LocalCredentials blob.  Falls back to using the
    device serial as username when the blob contains no serial field.
    """
    devices = await get_devices(token)
    device = next(
        (d for d in devices
         if (d.get("Serial") or d.get("serial", "")).upper() == serial.upper()),
        None,
    )
    if device is None:
        raise DysonApiError(f"Device {serial} not found in manifest")

    local_creds_b64 = device.get("LocalCredentials") or device.get("localCredentials")
    if not local_creds_b64:
        raise DysonApiError(f"No LocalCredentials found for device {serial}")

    username, password = decrypt_local_credentials(local_creds_b64)
    if not username:
        username = serial
    return username, password
