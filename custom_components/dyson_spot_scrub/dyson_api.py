"""Dyson cloud REST API client — auth, device manifest, IoT credentials.

Auth flow (confirmed working Aug 2026):
  POST appapi.cp.dyson.com/v3/userregistration/email/auth?country={CC}
    → challengeId (OTP email sent)
  POST appapi.cp.dyson.com/v3/userregistration/email/verify
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
    "Content-Type":   "application/json",
    "User-Agent":     "Dalvik/2.1.0 (Linux; U; Android 11; Build/RQ3A.210905.001)",
    "Accept":         "application/json, text/plain, */*",
    "Accept-Language": "en-AU,en;q=0.9",
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


async def initiate_v3_auth(email: str, password: str, country: str = "GB") -> str:
    """Start v3 OTP flow. Returns challengeId (OTP email sent to user)."""
    url = (
        f"https://{API_HOST}/v3/userregistration/email/auth"
        f"?country={country}"
    )
    async with aiohttp.ClientSession(connector=_connector()) as session:
        async with session.post(
            url,
            json={"email": email, "password": password, "language": "EN"},
            headers=_HEADERS,
        ) as resp:
            data = await _json(resp)
            if resp.status == 200 and data.get("challengeId"):
                return data["challengeId"]
            raise DysonAuthError(
                f"v3 auth initiation failed (HTTP {resp.status}): {data}"
            )


async def verify_v3_auth(
    email: str, password: str, challenge_id: str, otp_code: str
) -> str:
    """Verify OTP. Returns bearer token string."""
    url = f"https://{API_HOST}/v3/userregistration/email/verify"
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
