"""Service-level tests for the extracted Weixin API client (Task 18 Step 2).

These tests exercise ``WeixinApiClient`` directly with ``httpx.MockTransport``
so they run without a real WeChat backend. They assert:

* exact URL, headers, query/body for GET and POST
* auth vs no-auth (``Authorization`` header presence/absence)
* ``base_info`` auto-injection on POST and preservation when already present
* ``get_with_base`` uses the custom base URL (used for QR redirect polling)
* ``SKRouteTag`` header injection when ``route_tag`` is configured
* HTTP error propagation via ``raise_for_status``
* ``X-WECHAT-UIN`` is base64 of a decimal uint32 string
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from miniunicorn.channels.weixin.api_client import (
    BASE_INFO,
    ILINK_APP_ID,
    WeixinApiClient,
)


def _make_client(
    *,
    transport: httpx.MockTransport,
    token: str = "test-token",
    base_url: str = "https://ilinkai.weixin.qq.com",
    route_tag: str | int | None = None,
) -> WeixinApiClient:
    http_client = httpx.AsyncClient(transport=transport)
    return WeixinApiClient(
        client=http_client,
        base_url=base_url,
        token_getter=lambda: token,
        route_tag=route_tag,
    )


def _make_mock(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# GET
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_builds_correct_url_and_params() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"ret": 0})

    api = _make_client(transport=_make_mock(handler))
    try:
        await api.get("ilink/bot/get_bot_qrcode", params={"bot_type": "3"}, auth=False)
    finally:
        await api._client.aclose()

    assert captured["url"] == "https://ilinkai.weixin.qq.com/ilink/bot/get_bot_qrcode?bot_type=3"
    assert captured["params"]["bot_type"] == "3"


@pytest.mark.asyncio
async def test_get_with_auth_includes_authorization_header() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={"ret": 0})

    api = _make_client(transport=_make_mock(handler), token="my-token")
    try:
        await api.get("ilink/bot/getupdates")
    finally:
        await api._client.aclose()

    assert captured["headers"]["authorization"] == "Bearer my-token"


@pytest.mark.asyncio
async def test_get_without_auth_omits_authorization_header() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={"ret": 0})

    api = _make_client(transport=_make_mock(handler), token="my-token")
    try:
        await api.get("ilink/bot/get_bot_qrcode", auth=False)
    finally:
        await api._client.aclose()

    assert "authorization" not in captured["headers"]


@pytest.mark.asyncio
async def test_get_includes_required_headers() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={"ret": 0})

    api = _make_client(transport=_make_mock(handler))
    try:
        await api.get("ilink/bot/test")
    finally:
        await api._client.aclose()

    assert captured["headers"]["content-type"] == "application/json"
    assert captured["headers"]["authorizationtype"] == "ilink_bot_token"
    assert captured["headers"]["ilink-app-id"] == ILINK_APP_ID
    assert "ilink-app-clientversion" in captured["headers"]
    assert "x-wechat-uin" in captured["headers"]


@pytest.mark.asyncio
async def test_get_with_route_tag_includes_sk_route_tag_header() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={"ret": 0})

    api = _make_client(transport=_make_mock(handler), route_tag="cn-east")
    try:
        await api.get("ilink/bot/test")
    finally:
        await api._client.aclose()

    assert captured["headers"]["skroutetag"] == "cn-east"


@pytest.mark.asyncio
async def test_get_with_extra_headers_overrides_defaults() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={"ret": 0})

    api = _make_client(transport=_make_mock(handler))
    try:
        await api.get("ilink/bot/test", extra_headers={"X-Custom": "val"})
    finally:
        await api._client.aclose()

    assert captured["headers"]["x-custom"] == "val"


@pytest.mark.asyncio
async def test_get_raises_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    api = _make_client(transport=_make_mock(handler))
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await api.get("ilink/bot/test")
    finally:
        await api._client.aclose()


# ---------------------------------------------------------------------------
# get_with_base
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_with_base_uses_custom_base_url() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"status": "wait"})

    api = _make_client(transport=_make_mock(handler))
    try:
        await api.get_with_base(
            base_url="https://redirect.example.test",
            endpoint="ilink/bot/get_qrcode_status",
            params={"qrcode": "qr-123"},
            auth=False,
        )
    finally:
        await api._client.aclose()

    assert captured["url"].startswith("https://redirect.example.test/ilink/bot/get_qrcode_status")
    assert "qr-123" in captured["url"]


@pytest.mark.asyncio
async def test_get_with_base_strips_trailing_slash() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"ret": 0})

    api = _make_client(transport=_make_mock(handler))
    try:
        await api.get_with_base(
            base_url="https://example.test/",
            endpoint="ilink/bot/test",
            auth=False,
        )
    finally:
        await api._client.aclose()

    assert captured["url"] == "https://example.test/ilink/bot/test"


# ---------------------------------------------------------------------------
# POST
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_injects_base_info_when_missing() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ret": 0})

    api = _make_client(transport=_make_mock(handler))
    try:
        await api.post("ilink/bot/sendmessage", body={"msg": {"to_user_id": "u1"}})
    finally:
        await api._client.aclose()

    assert captured["body"]["base_info"] == BASE_INFO
    assert captured["body"]["msg"]["to_user_id"] == "u1"


@pytest.mark.asyncio
async def test_post_preserves_existing_base_info() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ret": 0})

    custom = {"channel_version": "custom-1.0.0"}
    api = _make_client(transport=_make_mock(handler))
    try:
        await api.post("ilink/bot/test", body={"base_info": custom})
    finally:
        await api._client.aclose()

    assert captured["body"]["base_info"] == custom


@pytest.mark.asyncio
async def test_post_with_empty_body_injects_base_info() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ret": 0})

    api = _make_client(transport=_make_mock(handler))
    try:
        await api.post("ilink/bot/test")
    finally:
        await api._client.aclose()

    assert captured["body"] == {"base_info": BASE_INFO}


@pytest.mark.asyncio
async def test_post_raises_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    api = _make_client(transport=_make_mock(handler))
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await api.post("ilink/bot/test")
    finally:
        await api._client.aclose()


# ---------------------------------------------------------------------------
# Header helper
# ---------------------------------------------------------------------------


def test_random_wechat_uin_is_base64_of_decimal_uint32() -> None:
    uin = WeixinApiClient._random_wechat_uin()
    decoded = base64.b64decode(uin).decode("ascii")
    assert decoded.isdigit()
    val = int(decoded)
    assert 0 <= val <= 0xFFFFFFFF
