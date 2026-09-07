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

import json
import logging
import ssl
from typing import Any
from urllib.parse import urlencode

import aiohttp

_LOGGER = logging.getLogger(__name__)

API_HOST = "appapi.cp.dyson.com"

_HEADERS = {
    "Content-Type":            "application/json",
    "User-Agent":              "Dalvik/2.1.0 (Linux; U; Android 11; Build/RQ3A.210905.001)",
    "Accept":                  "application/json, text/plain, */*",
    "Accept-Language":         "en-AU,en;q=0.9",
    "X-App-Version":           "6.4.26341",
    "X-Platform":              "ios",
    "X-Dyson-LinkApp-Version": "6.4.26340",
}

# SSL context that skips verification (Dyson's cert chain fails on some platforms)
_SSL_CONTEXT = ssl.create_default_context()
_SSL_CONTEXT.check_hostname = False
_SSL_CONTEXT.verify_mode = ssl.CERT_NONE


# ── Exceptions ────────────────────────────────────────────────────────────────


class DysonAuthError(Exception):
    """Raised when authentication with Dyson's API fails."""


class DysonApiError(Exception):
    """Raised when a Dyson API call fails."""


# ── Helpers ───────────────────────────────────────────────────────────────────


def _connector() -> aiohttp.TCPConnector:
    return aiohttp.TCPConnector(ssl=_SSL_CONTEXT)


async def _json(resp: aiohttp.ClientResponse) -> Any:
    """Parse JSON from a response, raising DysonAuthError on empty/invalid body."""
    try:
        return await resp.json(content_type=None)
    except (json.JSONDecodeError, aiohttp.ContentTypeError) as exc:
        body = await resp.text()
        raise DysonAuthError(
            f"Non-JSON response (HTTP {resp.status}): {body!r}"
        ) from exc


# ── Auth ──────────────────────────────────────────────────────────────────────


async def authenticate_v1(email: str, password: str, country: str = "GB") -> str:
    """Try legacy v1 auth (no OTP). Returns a Basic token string or raises."""
    url = f"https://{API_HOST}/v1/userregistration/authenticate?country={country}"
    async with aiohttp.ClientSession(connector=_connector()) as session:
        async with session.post(url, json={"Email": email, "Password": password},
                                headers=_HEADERS) as resp:
            data = await _json(resp)
            if resp.status == 200 and data.get("Account") and data.get("Password"):
                import base64
                token = base64.b64encode(
                    f"{data['Account']}:{data['Password']}".encode()
                ).decode()
                return token
            raise DysonAuthError(
                f"v1 auth failed (HTTP {resp.status}): {data}"
            )


async def initiate_v3_auth(email: str, country: str = "GB") -> str:
    """Start v3 OTP flow. Returns challengeId (OTP email sent to user).

    NOTE: password is NOT sent at this step — only email goes in the body.
    Password is sent in verify_v3_auth. Sending password here causes 401s.
    """
    culture = f"en-{country}"
    url = (
        f"https://{API_HOST}/v3/userregistration/email/auth"
        f"?country={country}&culture={culture}"
    )
    async with aiohttp.ClientSession(connector=_connector()) as session:
        async with session.post(
            url,
            json={"email": email},
            headers=_HEADERS,
        ) as resp:
            data = await _json(resp)
            if resp.status == 200 and data.get("challengeId"):
                return data["challengeId"]
            raise DysonAuthError(
                f"v3 auth initiation failed (HTTP {resp.status}): {data}"
            )


async def verify_v3_auth(
    email: str, password: str, challenge_id: str, otp_code: str,
    country: str = "GB",
) -> str:
    """Verify OTP. Returns bearer token string."""
    url = f"https://{API_HOST}/v3/userregistration/email/verify?country={country}"
    async with aiohttp.ClientSession(connector=_connector()) as session:
        async with session.post(
            url,
            json={
                "email": email,
                "password": password,
                "challengeId": challenge_id,
                "otpCode": otp_code,
            },
            headers=_HEADERS,
        ) as resp:
            data = await _json(resp)
            if resp.status == 200:
                if data.get("token"):
                    return data["token"]
                # Older API: build Basic token from account + password
                if data.get("account") and data.get("password"):
                    import base64
                    return base64.b64encode(
                        f"{data['account']}:{data['password']}".encode()
                    ).decode()
            raise DysonAuthError(
                f"v3 OTP verification failed (HTTP {resp.status}): {data}"
            )


# ── Manifest / devices ────────────────────────────────────────────────────────


async def get_devices(token: str) -> list[dict[str, Any]]:
    """Return the full device manifest list."""
    url = f"https://{API_HOST}/v2/provisioningservice/manifest"
    headers = {**_HEADERS, "Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession(connector=_connector()) as session:
        async with session.get(url, headers=headers) as resp:
            data = await _json(resp)
            if resp.status == 200 and isinstance(data, list):
                return data
            raise DysonApiError(
                f"Manifest fetch failed (HTTP {resp.status}): {data}"
            )


async def get_iot_credentials(token: str, serial: str) -> dict[str, str]:
    """Return IoT connection credentials for one device."""
    url = f"https://{API_HOST}/v2/authorize/iot-credentials"
    headers = {**_HEADERS, "Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession(connector=_connector()) as session:
        async with session.post(
            url, json={"Serial": serial}, headers=headers
        ) as resp:
            data = await _json(resp)
            if resp.status != 200:
                raise DysonApiError(
                    f"IoT credentials fetch failed (HTTP {resp.status}): {data}"
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
    """Return True if the token is still accepted by Dyson's API."""
    try:
        await get_devices(token)
        return True
    except Exception:
        return False


# ── Map endpoints ─────────────────────────────────────────────────────────────


async def get_live_map(token: str, serial: str) -> dict[str, Any]:
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

    Raises DysonApiError if the robot is not currently cleaning (the endpoint
    may return 404 or an error body when no session is active).
    """
    url = f"https://{API_HOST}/v1/app/{serial}/live-maps/cleaning"
    headers = {**_HEADERS, "Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession(connector=_connector()) as session:
        async with session.get(url, headers=headers) as resp:
            data = await _json(resp)
            if resp.status == 200 and isinstance(data, dict):
                return data
            raise DysonApiError(
                f"Live map fetch failed (HTTP {resp.status}): {data}"
            )


async def get_map_metadata(token: str, serial: str) -> list[dict[str, Any]]:
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
    url = f"https://{API_HOST}/v2/app/{serial}/persistent-map-metadata"
    headers = {**_HEADERS, "Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession(connector=_connector()) as session:
        async with session.get(url, headers=headers) as resp:
            data = await _json(resp)
            if resp.status == 200 and isinstance(data, list):
                return data
            raise DysonApiError(
                f"Map metadata fetch failed (HTTP {resp.status}): {data}"
            )


async def get_map(token: str, serial: str, map_id: str) -> dict[str, Any]:
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
    url = f"https://{API_HOST}/v2/app/{serial}/persistent-maps/{map_id}"
    headers = {**_HEADERS, "Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession(connector=_connector()) as session:
        async with session.get(url, headers=headers) as resp:
            data = await _json(resp)
            if resp.status == 200 and isinstance(data, dict):
                return data
            raise DysonApiError(
                f"Map fetch failed (HTTP {resp.status}): {data}"
            )


async def update_map_zone_selection(
    token: str, serial: str, map_id: str,
    zones: list[dict[str, Any]],
) -> None:
    """Set zone selection/settings and arm the robot for the next clean.

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

    Usage pattern for starting a clean from HA:
      1. zones = await get_map_metadata(token, serial)  # fetch current list
         zones = zones[0]["zones"]                       # first (current) map
      2. Adjust isSelected / order / settings on each zone entry
      3. await update_map_zone_selection(token, serial, map_id, zones)
      4. Publish MQTT start command  ← this triggers actual robot movement

    Endpoint: PUT /v2/app/{serial}/persistent-map-metadata/{mapId}
    Note: URL uses persistent-map-metadata (not persistent-maps).
    """
    url = (
        f"https://{API_HOST}/v2/app/{serial}"
        f"/persistent-map-metadata/{map_id}"
    )
    headers = {**_HEADERS, "Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession(connector=_connector()) as session:
        async with session.put(url, json=zones, headers=headers) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise DysonApiError(
                    f"Zone selection update failed (HTTP {resp.status}): {body}"
                )


async def get_ota_status(token: str, serial: str) -> str:
    """Return the firmware OTA status for a device.

    Returns the otaStatus string, one of:
      "Completed"   — most recent update installed successfully
      "Available"   — update ready to download
      "Downloading" — update being pulled to the robot
      "Installing"  — firmware being applied
      "Failed"      — update attempt failed

    Useful for a binary_sensor.dyson_update_available entity (fires when
    otaStatus == "Available") or a diagnostic sensor showing current status.
    """
    url = f"https://{API_HOST}/v1/assets/devices/{serial}/ota"
    headers = {**_HEADERS, "Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession(connector=_connector()) as session:
        async with session.get(url, headers=headers) as resp:
            data = await _json(resp)
            if resp.status == 200 and "otaStatus" in data:
                return data["otaStatus"]
            raise DysonApiError(
                f"OTA status fetch failed (HTTP {resp.status}): {data}"
            )


async def get_clean_estimation(
    token: str, serial: str, map_id: str,
    zones: list[dict[str, Any]],
) -> dict[str, Any]:
    """Estimate duration and recharge stops for a planned clean.

    `zones` is a list of zone dicts, each containing:
      { "id": str, "area": float, "settings": { cleanType, waterLevel,
        mopPasses, dryPasses, cleaningStrategy, isUvScanOn } }

    Send ALL zones (even unselected ones) — the server expects the full array.
    Returns: { "duration": int (minutes), "charges": int (dock recharge stops) }

    Example:
      est = await get_clean_estimation(token, serial, map_id, zones)
      # → {"duration": 22, "charges": 0}
    """
    url = (
        f"https://{API_HOST}/v2/app/{serial}"
        f"/persistent-maps/{map_id}/clean-estimation"
    )
    headers = {**_HEADERS, "Authorization": f"Bearer {token}"}
    payload = {"zones": zones}
    async with aiohttp.ClientSession(connector=_connector()) as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            data = await _json(resp)
            if resp.status == 200 and "duration" in data:
                return data
            raise DysonApiError(
                f"Clean estimation failed (HTTP {resp.status}): {data}"
            )


async def get_current_map(token: str, serial: str) -> tuple[str, dict[str, Any]]:
    """Convenience: fetch the active map, returning (map_id, map_data).

    Calls get_map_metadata to find the map with isCurrentMap=True, then
    fetches the full map. Raises DysonApiError if no current map is found.
    """
    maps = await get_map_metadata(token, serial)
    current = next((m for m in maps if m.get("isCurrentMap")), None)
    if current is None:
        if maps:
            current = maps[0]
        else:
            raise DysonApiError("No maps found for device")
    map_id = str(current["id"])
    map_data = await get_map(token, serial, map_id)
    return map_id, map_data
