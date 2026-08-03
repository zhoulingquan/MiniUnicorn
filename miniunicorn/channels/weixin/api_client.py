"""Weixin iLink HTTP/API client and protocol constants.

``WeixinApiClient`` owns the HTTP request construction, authentication
headers, and ``base_info`` injection for POST bodies. It does NOT own
polling, login state transitions, typing state, or message dispatch —
those remain on ``WeixinChannel``.

This module also defines the protocol-level constants shared by the
channel façade and the media service (item types, message types, version
info) so neither ``channel.py`` nor ``media.py`` needs to import from
the other for constants.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Callable

import httpx

# ---------------------------------------------------------------------------
# Protocol constants (from openclaw-weixin types.ts)
# ---------------------------------------------------------------------------

# MessageItemType
ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5

# MessageType  (1 = inbound from user, 2 = outbound from bot)
MESSAGE_TYPE_BOT = 2

# MessageState
MESSAGE_STATE_FINISH = 2

WEIXIN_CHANNEL_VERSION = "2.1.1"
ILINK_APP_ID = "bot"


def _build_client_version(version: str) -> int:
    """Encode semantic version as 0x00MMNNPP (major/minor/patch in one uint32)."""
    parts = version.split(".")

    def _as_int(idx: int) -> int:
        try:
            return int(parts[idx])
        except Exception:
            return 0

    major = _as_int(0)
    minor = _as_int(1)
    patch = _as_int(2)
    return ((major & 0xFF) << 16) | ((minor & 0xFF) << 8) | (patch & 0xFF)


ILINK_APP_CLIENT_VERSION = _build_client_version(WEIXIN_CHANNEL_VERSION)
BASE_INFO: dict[str, str] = {"channel_version": WEIXIN_CHANNEL_VERSION}

# Media-type codes for getuploadurl  (1=image, 2=video, 3=file, 4=voice)
UPLOAD_MEDIA_IMAGE = 1
UPLOAD_MEDIA_VIDEO = 2
UPLOAD_MEDIA_FILE = 3
UPLOAD_MEDIA_VOICE = 4


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class WeixinApiClient:
    """HTTP client for the Weixin iLink API.

    Owns request construction, header building, and ``base_info`` injection.
    The channel delegates ``_api_get`` / ``_api_post`` / ``_api_get_with_base``
    to this client.

    Constructed with:

    * ``client`` — the shared ``httpx.AsyncClient`` used for all HTTP calls.
    * ``base_url`` — the iLink API base URL (may be redirected after login).
    * ``token_getter`` — a callable returning the current bot token; the
      channel retains ownership of token state.
    * ``route_tag`` — optional ``SKRouteTag`` header value.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        base_url: str,
        token_getter: Callable[[], str],
        route_tag: str | int | None = None,
    ) -> None:
        self._client = client
        self._base_url = base_url
        self._token_getter = token_getter
        self._route_tag = route_tag

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get(
        self,
        endpoint: str,
        params: dict | None = None,
        *,
        auth: bool = True,
        extra_headers: dict[str, str] | None = None,
    ) -> dict:
        """GET ``{base_url}/{endpoint}`` with standard iLink headers."""
        url = f"{self._base_url}/{endpoint}"
        hdrs = self._make_headers(auth=auth)
        if extra_headers:
            hdrs.update(extra_headers)
        resp = await self._client.get(url, params=params, headers=hdrs)
        resp.raise_for_status()
        return resp.json()

    async def get_with_base(
        self,
        *,
        base_url: str,
        endpoint: str,
        params: dict | None = None,
        auth: bool = True,
        extra_headers: dict[str, str] | None = None,
    ) -> dict:
        """GET with a custom base URL (used for QR redirect polling)."""
        url = f"{base_url.rstrip('/')}/{endpoint}"
        hdrs = self._make_headers(auth=auth)
        if extra_headers:
            hdrs.update(extra_headers)
        resp = await self._client.get(url, params=params, headers=hdrs)
        resp.raise_for_status()
        return resp.json()

    async def post(
        self,
        endpoint: str,
        body: dict | None = None,
        *,
        auth: bool = True,
    ) -> dict:
        """POST ``{base_url}/{endpoint}`` with ``base_info`` auto-injection."""
        url = f"{self._base_url}/{endpoint}"
        payload = body or {}
        if "base_info" not in payload:
            payload["base_info"] = BASE_INFO
        resp = await self._client.post(url, json=payload, headers=self._make_headers(auth=auth))
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Header helpers
    # ------------------------------------------------------------------

    def _make_headers(self, *, auth: bool = True) -> dict[str, str]:
        """Build per-request headers (new UIN each call, matching reference)."""
        headers: dict[str, str] = {
            "X-WECHAT-UIN": self._random_wechat_uin(),
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "iLink-App-Id": ILINK_APP_ID,
            "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
        }
        if auth:
            token = self._token_getter()
            if token:
                headers["Authorization"] = f"Bearer {token}"
        if self._route_tag is not None and str(self._route_tag).strip():
            headers["SKRouteTag"] = str(self._route_tag).strip()
        return headers

    @staticmethod
    def _random_wechat_uin() -> str:
        """X-WECHAT-UIN: random uint32 → decimal string → base64.

        Matches the reference plugin's ``randomWechatUin()`` in api.ts.
        Generated fresh for **every** request (same as reference).
        """
        uint32 = int.from_bytes(os.urandom(4), "big")
        return base64.b64encode(str(uint32).encode()).decode()
