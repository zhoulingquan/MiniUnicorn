"""End-to-end tests for the embedded webui's HTTP routes on the WebSocket channel."""

import asyncio
import functools
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlencode

import httpx
import pytest

from miniunicorn.channels.websocket import WebSocketChannel
from miniunicorn.session.manager import Session, SessionManager

_PORT = 29900


def _ch(
    bus: Any,
    *,
    session_manager: SessionManager | None = None,
    static_dist_path: Path | None = None,
    port: int = _PORT,
    runtime_model_name: Any | None = None,
    **extra: Any,
) -> WebSocketChannel:
    cfg: dict[str, Any] = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": port,
        "path": "/",
        "websocketRequiresToken": False,
    }
    cfg.update(extra)
    ws_kwargs: dict[str, Any] = {
        "session_manager": session_manager,
        "static_dist_path": static_dist_path,
    }
    if runtime_model_name is not None:
        ws_kwargs["runtime_model_name"] = runtime_model_name
    return WebSocketChannel(
        cfg,
        bus,
        **ws_kwargs,
    )


@pytest.fixture()
def bus() -> MagicMock:
    b = MagicMock()
    b.publish_inbound = AsyncMock()
    return b


async def _http_get(url: str, headers: dict[str, str] | None = None) -> httpx.Response:
    return await asyncio.to_thread(
        functools.partial(httpx.get, url, headers=headers or {}, timeout=5.0)
    )


def _seed_session(workspace: Path, key: str = "websocket:test") -> SessionManager:
    sm = SessionManager(workspace)
    s = Session(key=key)
    s.add_message("user", "hi")
    s.add_message("assistant", "hello back")
    sm.save(s)
    return sm


def _seed_many(workspace: Path, keys: list[str]) -> SessionManager:
    sm = SessionManager(workspace)
    for k in keys:
        s = Session(key=k)
        s.add_message("user", f"hi from {k}")
        sm.save(s)
    return sm


@pytest.mark.asyncio
async def test_bootstrap_returns_token_for_localhost(bus: MagicMock, tmp_path: Path) -> None:
    sm = _seed_session(tmp_path)
    # 传入 runtime_model_name 确保 bootstrap 返回的 model_name 是字符串而非 None
    channel = _ch(bus, session_manager=sm, port=29901, runtime_model_name=lambda: "test-model")
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        resp = await _http_get("http://127.0.0.1:29901/webui/bootstrap")
        assert resp.status_code == 200
        body = resp.json()
        assert body["token"].startswith("nbwt_")
        assert body["ws_path"] == "/"
        assert body["ws_url"] == "ws://127.0.0.1:29901/"
        assert body["expires_in"] > 0
        assert isinstance(body.get("model_name"), str)
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_sessions_routes_require_bearer_token(bus: MagicMock, tmp_path: Path) -> None:
    sm = _seed_session(tmp_path, key="websocket:abc")
    channel = _ch(bus, session_manager=sm, port=29902)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        # Unauthenticated → 401.
        deny = await _http_get("http://127.0.0.1:29902/api/sessions")
        assert deny.status_code == 401

        # Mint a token via bootstrap, then call the API with it.
        boot = await _http_get("http://127.0.0.1:29902/webui/bootstrap")
        token = boot.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        listing = await _http_get("http://127.0.0.1:29902/api/sessions", headers=auth)
        assert listing.status_code == 200
        keys = [s["key"] for s in listing.json()["sessions"]]
        assert "websocket:abc" in keys
        # Server stays an opaque source: filesystem paths must not leak to the wire.
        assert all("path" not in s for s in listing.json()["sessions"])

        msgs = await _http_get(
            "http://127.0.0.1:29902/api/sessions/websocket:abc/messages",
            headers=auth,
        )
        assert msgs.status_code == 200
        body = msgs.json()
        assert body["key"] == "websocket:abc"
        assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_mcp_presets_routes_require_token_and_return_payload(
    bus: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "miniunicorn.webui.mcp_presets_api.mcp_presets_payload",
        lambda: {
            "presets": [
                {
                    "name": "playwright",
                    "display_name": "Playwright",
                    "category": "browser",
                    "description": "Cloud browser automation",
                    "docs_url": "https://docs.playwright.com/integrations/mcp/configuration",
                    "transport": "streamableHttp",
                    "requires": "Playwright API key",
                    "note": "",
                    "install_supported": True,
                    "installed": False,
                    "configured": False,
                    "available": False,
                    "status": "not_installed",
                    "logo_url": None,
                    "brand_color": "#111827",
                    "required_fields": [],
                    "connection_summary": "",
                }
            ],
            "installed_count": 0,
        },
    )
    preset_queries: list[tuple[str, dict[str, list[str]]]] = []
    custom_queries: list[tuple[str, dict[str, list[str]]]] = []

    def _mcp_preset_action(action: str, query: dict[str, list[str]]) -> dict[str, Any]:
        preset_queries.append((action, query))
        return {
            "presets": [],
            "installed_count": 1,
            "requires_restart": action != "test",
            "last_action": {"ok": True, "message": f"{action}:{query['name'][0]}"},
        }

    def _custom_action(action: str, query: dict[str, list[str]]) -> dict[str, Any]:
        custom_queries.append((action, query))
        return {
            "presets": [],
            "installed_count": 1,
            "requires_restart": True,
            "last_action": {
                "ok": True,
                "message": f"{action}:{query.get('name', ['config'])[0]}",
            },
        }

    monkeypatch.setattr(
        "miniunicorn.webui.mcp_presets_api.mcp_presets_action",
        _mcp_preset_action,
    )
    monkeypatch.setattr(
        "miniunicorn.webui.mcp_presets_api.custom_mcp_action",
        _custom_action,
    )

    async def _hot_reload(_bus):
        return {"ok": True, "message": "MCP config reloaded.", "requires_restart": False}

    monkeypatch.setattr(
        "miniunicorn.channels.websocket.channel.request_mcp_reload",
        _hot_reload,
    )
    channel = _ch(bus, session_manager=_seed_session(tmp_path), port=29913)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        deny = await _http_get("http://127.0.0.1:29913/api/settings/mcp-presets")
        assert deny.status_code == 401

        boot = await _http_get("http://127.0.0.1:29913/webui/bootstrap")
        token = boot.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        catalog = await _http_get(
            "http://127.0.0.1:29913/api/settings/mcp-presets",
            headers=auth,
        )
        assert catalog.status_code == 200
        assert catalog.json()["presets"][0]["name"] == "playwright"

        enabled = await _http_get(
            "http://127.0.0.1:29913/api/settings/mcp-presets/enable?name=playwright",
            headers={
                **auth,
                "x-miniunicorn-MCP-Values": json.dumps({"playwright_api_key": "bb_live_secret"}),
            },
        )
        assert enabled.status_code == 200
        assert preset_queries[-1][1]["playwright_api_key"] == ["bb_live_secret"]
        body = enabled.json()
        assert "bb_live_secret" not in enabled.text
        # hot_reload runs in background; message no longer carries the reload suffix.
        assert body["last_action"]["message"] == "enable:playwright"
        assert body["restart_required_sections"] == ["runtime"]

        bad_header = await _http_get(
            "http://127.0.0.1:29913/api/settings/mcp-presets/enable?name=playwright",
            headers={**auth, "x-miniunicorn-MCP-Values": "[]"},
        )
        assert bad_header.status_code == 400

        custom = await _http_get(
            "http://127.0.0.1:29913/api/settings/mcp-presets/custom",
            headers={
                **auth,
                "x-miniunicorn-MCP-Values": json.dumps({"name": "docs", "command": "npx"}),
            },
        )
        assert custom.status_code == 200
        assert custom_queries[-1][1]["command"] == ["npx"]
        assert custom.json()["last_action"]["message"] == "custom:docs"

        imported = await _http_get(
            "http://127.0.0.1:29913/api/settings/mcp-presets/import",
            headers={**auth, "x-miniunicorn-MCP-Values": json.dumps({"config": "{}"})},
        )
        assert imported.status_code == 200
        assert imported.json()["last_action"]["message"] == "import:config"

        tools = await _http_get(
            "http://127.0.0.1:29913/api/settings/mcp-presets/tools",
            headers={
                **auth,
                "x-miniunicorn-MCP-Values": json.dumps({"name": "docs", "enabled_tools": []}),
            },
        )
        assert tools.status_code == 200
        assert tools.json()["last_action"]["message"] == "tools:docs"
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_sessions_list_only_returns_websocket_sessions_by_default(
    bus: MagicMock, tmp_path: Path
) -> None:
    # Seed a realistic multi-channel disk state: CLI, Slack, Lark and
    # websocket sessions all live in the same ``sessions/`` directory.
    sm = _seed_many(
        tmp_path,
        [
            "cli:direct",
            "slack:C123",
            "lark:oc_abc",
            "websocket:alpha",
            "websocket:beta",
        ],
    )
    channel = _ch(bus, session_manager=sm, port=29906)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        boot = await _http_get("http://127.0.0.1:29906/webui/bootstrap")
        token = boot.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        listing = await _http_get("http://127.0.0.1:29906/api/sessions", headers=auth)
        assert listing.status_code == 200
        keys = {s["key"] for s in listing.json()["sessions"]}
        # Only websocket-channel sessions are part of the webui surface; CLI /
        # Slack / Lark rows would be non-resumable from the browser.
        assert keys == {"websocket:alpha", "websocket:beta"}
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_webui_sidebar_state_routes_are_config_dir_scoped(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("miniunicorn.config.paths.get_data_dir", lambda: tmp_path)
    sm = _seed_session(tmp_path, key="websocket:sidebar")
    channel = _ch(bus, session_manager=sm, port=29911)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        boot = await _http_get("http://127.0.0.1:29911/webui/bootstrap")
        token = boot.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        initial = await _http_get(
            "http://127.0.0.1:29911/api/webui/sidebar-state",
            headers=auth,
        )
        assert initial.status_code == 200
        assert initial.json()["schema_version"] == 1
        assert initial.json()["pinned_keys"] == []

        payload = {
            "pinned_keys": ["websocket:sidebar"],
            "archived_keys": ["websocket:old"],
            "title_overrides": {"websocket:sidebar": "Pinned work"},
            "view": {"density": "compact", "show_archived": True},
        }
        query = urlencode({"state": json.dumps(payload)})
        updated = await _http_get(
            f"http://127.0.0.1:29911/api/webui/sidebar-state/update?{query}",
            headers=auth,
        )
        assert updated.status_code == 200
        body = updated.json()
        assert body["pinned_keys"] == ["websocket:sidebar"]
        assert body["title_overrides"] == {"websocket:sidebar": "Pinned work"}
        assert body["view"]["density"] == "compact"

        state_path = tmp_path / "webui" / "sidebar-state.json"
        assert state_path.is_file()
        assert json.loads(state_path.read_text(encoding="utf-8"))["pinned_keys"] == [
            "websocket:sidebar"
        ]
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_session_delete_removes_file(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("miniunicorn.config.paths.get_data_dir", lambda: tmp_path)
    sm = _seed_session(tmp_path, key="websocket:doomed")
    from miniunicorn.webui.transcript import append_transcript_object

    append_transcript_object(
        "websocket:doomed", {"event": "user", "chat_id": "doomed", "text": "x"}
    )
    channel = _ch(bus, session_manager=sm, port=29903)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        boot = await _http_get("http://127.0.0.1:29903/webui/bootstrap")
        token = boot.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        path = sm._get_session_path("websocket:doomed")
        assert path.exists()
        webui_path = tmp_path / "webui" / f"{SessionManager.safe_key('websocket:doomed')}.jsonl"
        assert webui_path.is_file()
        resp = await _http_get(
            "http://127.0.0.1:29903/api/sessions/websocket:doomed/delete",
            headers=auth,
        )
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True
        assert not path.exists()
        assert not webui_path.exists()
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_session_routes_accept_percent_encoded_websocket_keys(
    bus: MagicMock, tmp_path: Path
) -> None:
    sm = _seed_session(tmp_path, key="websocket:encoded-key")
    channel = _ch(bus, session_manager=sm, port=29910)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        boot = await _http_get("http://127.0.0.1:29910/webui/bootstrap")
        token = boot.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        msgs = await _http_get(
            "http://127.0.0.1:29910/api/sessions/websocket%3Aencoded-key/messages",
            headers=auth,
        )
        assert msgs.status_code == 200
        assert msgs.json()["key"] == "websocket:encoded-key"

        path = sm._get_session_path("websocket:encoded-key")
        assert path.exists()
        deleted = await _http_get(
            "http://127.0.0.1:29910/api/sessions/websocket%3Aencoded-key/delete",
            headers=auth,
        )
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True
        assert not path.exists()
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_session_routes_reject_non_websocket_keys(bus: MagicMock, tmp_path: Path) -> None:
    sm = _seed_many(
        tmp_path,
        [
            "websocket:kept",
            "cli:direct",
            "slack:C123",
        ],
    )
    channel = _ch(bus, session_manager=sm, port=29909)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        boot = await _http_get("http://127.0.0.1:29909/webui/bootstrap")
        token = boot.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        # The webui list already hides non-websocket sessions; handcrafted URLs
        # should hit the same boundary rather than exposing or deleting them.
        msgs = await _http_get(
            "http://127.0.0.1:29909/api/sessions/cli:direct/messages",
            headers=auth,
        )
        assert msgs.status_code == 404

        doomed = sm._get_session_path("slack:C123")
        assert doomed.exists()
        deny_delete = await _http_get(
            "http://127.0.0.1:29909/api/sessions/slack:C123/delete",
            headers=auth,
        )
        assert deny_delete.status_code == 404
        assert doomed.exists()
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_session_routes_reject_invalid_key(bus: MagicMock, tmp_path: Path) -> None:
    sm = _seed_session(tmp_path)
    channel = _ch(bus, session_manager=sm, port=29904)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        boot = await _http_get("http://127.0.0.1:29904/webui/bootstrap")
        token = boot.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        # Invalid characters in the key -> regex match fails -> 404
        # (route doesn't match, falls through to channel 404).
        resp = await _http_get(
            "http://127.0.0.1:29904/api/sessions/bad%20key/messages",
            headers=auth,
        )
        assert resp.status_code in {400, 404}
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_static_serves_index_when_dist_present(bus: MagicMock, tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>nbweb</title>")
    (dist / "favicon.svg").write_text("<svg/>")
    sm = _seed_session(tmp_path / "ws_state")
    channel = _ch(bus, session_manager=sm, static_dist_path=dist, port=29905)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        # Bare ``GET /`` is a browser opening the app: it must return the SPA
        # index.html, not the WS-upgrade handler's 401/426.
        root = await _http_get("http://127.0.0.1:29905/")
        assert root.status_code == 200
        assert "nbweb" in root.text
        asset = await _http_get("http://127.0.0.1:29905/favicon.svg")
        assert asset.status_code == 200
        assert "<svg" in asset.text
        # Unknown SPA route falls back to index.html.
        spa = await _http_get("http://127.0.0.1:29905/sessions/abc")
        assert spa.status_code == 200
        assert "nbweb" in spa.text
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_static_rejects_path_traversal(bus: MagicMock, tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("ok")
    secret = tmp_path / "secret.txt"
    secret.write_text("classified")
    channel = _ch(bus, static_dist_path=dist, port=29906)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        resp = await _http_get("http://127.0.0.1:29906/../secret.txt")
        # Normalized by httpx into /secret.txt → falls back to index.html, not 'classified'.
        assert "classified" not in resp.text
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_unknown_route_returns_404(bus: MagicMock) -> None:
    channel = _ch(bus, port=29907)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        resp = await _http_get("http://127.0.0.1:29907/api/unknown")
        assert resp.status_code == 404
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_api_token_pool_purges_expired(bus: MagicMock, tmp_path: Path) -> None:
    sm = _seed_session(tmp_path)
    channel = _ch(bus, session_manager=sm, port=29908)
    # Don't start a server — directly inject and validate.
    import time as _time

    channel._api_tokens["expired"] = _time.monotonic() - 1
    channel._api_tokens["live"] = _time.monotonic() + 60

    class _FakeReq:
        path = "/api/sessions"
        headers = {"Authorization": "Bearer expired"}

    assert channel._check_api_token(_FakeReq()) is False

    class _LiveReq:
        path = "/api/sessions"
        headers = {"Authorization": "Bearer live"}

    assert channel._check_api_token(_LiveReq()) is True


class _FakeConn:
    """Minimal connection stub with a configurable remote_address."""

    def __init__(self, remote_address: tuple[str, int]):
        self.remote_address = remote_address

    def respond(self, status: int, body: str) -> Any:
        from websockets.http11 import Response

        return Response(status=status, body=body.encode())


class _FakeReq:
    """Minimal request stub with configurable headers."""

    def __init__(self, headers: dict[str, str] | None = None):
        self.headers = headers or {}


_REMOTE = _FakeConn(("192.168.1.5", 12345))
_LOCAL = _FakeConn(("127.0.0.1", 12345))
_NO_HEADERS = _FakeReq()


def test_wildcard_host_without_auth_raises_on_startup(bus: MagicMock) -> None:
    import pytest
    from pydantic_core import ValidationError

    with pytest.raises(ValidationError, match="token"):
        _ch(bus, host="0.0.0.0")


def test_wildcard_host_with_token_is_valid(bus: MagicMock) -> None:
    channel = _ch(bus, host="0.0.0.0", allowFrom=["caller"], token="my-token")
    assert channel.config.host == "0.0.0.0"


def test_wildcard_host_with_secret_is_valid(bus: MagicMock) -> None:
    channel = _ch(bus, host="0.0.0.0", allowFrom=["caller"], tokenIssueSecret="s3cret")
    assert channel.config.host == "0.0.0.0"


def test_wildcard_ipv6_without_auth_raises(bus: MagicMock) -> None:
    import pytest
    from pydantic_core import ValidationError

    with pytest.raises(ValidationError, match="token"):
        _ch(bus, host="::")


def test_wildcard_ipv6_with_secret_is_valid(bus: MagicMock) -> None:
    channel = _ch(bus, host="::", allowFrom=["caller"], tokenIssueSecret="s3cret")
    resp = channel._handle_bootstrap(_REMOTE, _FakeReq({"x-miniunicorn-Auth": "s3cret"}))
    assert resp.status_code == 200


def test_bootstrap_accepts_static_token_as_secret(bus: MagicMock) -> None:
    """When only token (not token_issue_secret) is set, bootstrap accepts it."""
    channel = _ch(bus, host="0.0.0.0", allowFrom=["caller"], token="static-tok")
    resp = channel._handle_bootstrap(_REMOTE, _FakeReq({"Authorization": "Bearer static-tok"}))
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["token"].startswith("nbwt_")


def test_bootstrap_ws_url_uses_forwarded_https_host(bus: MagicMock) -> None:
    channel = _ch(bus, host="127.0.0.1", port=29931)
    resp = channel._handle_bootstrap(
        _LOCAL,
        _FakeReq({"Host": "miniunicorn.example", "X-Forwarded-Proto": "https"}),
    )
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["ws_url"] == "wss://miniunicorn.example/"


def test_localhost_without_auth_is_valid(bus: MagicMock) -> None:
    channel = _ch(bus, host="127.0.0.1")
    resp = channel._handle_bootstrap(_LOCAL, _NO_HEADERS)
    assert resp.status_code == 200


def test_bootstrap_prefers_runtime_model_name(
    bus: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "miniunicorn.channels.websocket.channel._default_model_name_from_config",
        lambda: "from-disk",
    )
    channel = _ch(bus, host="127.0.0.1", runtime_model_name=lambda: "  live/model  ")
    resp = channel._handle_bootstrap(_LOCAL, _NO_HEADERS)
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["model_name"] == "live/model"


def test_bootstrap_falls_back_when_runtime_returns_empty(
    bus: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "miniunicorn.channels.websocket.channel._default_model_name_from_config",
        lambda: "from-disk",
    )
    channel = _ch(bus, host="127.0.0.1", runtime_model_name=lambda: "   ")
    resp = channel._handle_bootstrap(_LOCAL, _NO_HEADERS)
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["model_name"] == "from-disk"


def test_bootstrap_falls_back_when_runtime_raises(
    bus: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "miniunicorn.channels.websocket.channel._default_model_name_from_config",
        lambda: "from-disk",
    )

    def boom():
        raise RuntimeError("resolver failed")

    channel = _ch(bus, host="127.0.0.1", runtime_model_name=boom)
    resp = channel._handle_bootstrap(_LOCAL, _NO_HEADERS)
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["model_name"] == "from-disk"


def test_bootstrap_rejects_wrong_secret(bus: MagicMock) -> None:
    channel = _ch(bus, host="0.0.0.0", allowFrom=["caller"], tokenIssueSecret="correct")
    resp = channel._handle_bootstrap(_REMOTE, _FakeReq({"Authorization": "Bearer wrong"}))
    assert resp.status_code == 401


def test_bootstrap_accepts_remote_with_valid_secret(bus: MagicMock) -> None:
    channel = _ch(bus, host="0.0.0.0", allowFrom=["caller"], tokenIssueSecret="s3cret")
    resp = channel._handle_bootstrap(_REMOTE, _FakeReq({"Authorization": "Bearer s3cret"}))
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["token"].startswith("nbwt_")


def test_bootstrap_accepts_x_miniunicorn_auth_header(bus: MagicMock) -> None:
    channel = _ch(bus, host="0.0.0.0", allowFrom=["caller"], tokenIssueSecret="s3cret")
    resp = channel._handle_bootstrap(_REMOTE, _FakeReq({"x-miniunicorn-Auth": "s3cret"}))
    assert resp.status_code == 200


def test_bootstrap_secret_also_enforced_on_localhost(bus: MagicMock) -> None:
    """When secret is set, even localhost must provide it (reverse-proxy safety)."""
    channel = _ch(bus, host="0.0.0.0", allowFrom=["caller"], tokenIssueSecret="s3cret")
    resp = channel._handle_bootstrap(_LOCAL, _NO_HEADERS)
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 4.1 regression: method / Origin / sensitive-header / ZIP-limit enforcement
# ---------------------------------------------------------------------------


async def _http_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """HTTP request helper that supports arbitrary methods (POST/PUT/...)."""
    return await asyncio.to_thread(
        functools.partial(httpx.request, method, url, headers=headers or {}, timeout=5.0)
    )


@pytest.mark.asyncio
async def test_get_only_route_rejects_post_with_405(bus: MagicMock, tmp_path: Path) -> None:
    """Routes declared ``methods={"GET"}`` must return 405 for POST.

    The websockets HTTP layer rejects POST at the transport level, so we test
    the dispatch logic directly with a fake POST request.
    """
    from websockets.datastructures import Headers
    from websockets.http11 import Request

    from miniunicorn.channels.websocket._http_router import router

    sm = _seed_session(tmp_path, key="websocket:m")
    channel = _ch(bus, session_manager=sm, port=29930)
    # Build a fake POST request to /api/sessions (a GET-only route).
    req = Request("/api/sessions", Headers([("Host", "127.0.0.1")]))
    # Attach method attribute — websockets Request doesn't store it, but
    # dispatch reads it via getattr(request, "method", "GET").
    req.method = "POST"  # type: ignore[attr-defined]
    deps = channel._build_route_deps()
    resp = await router.dispatch(deps, None, req)
    assert resp is not None
    assert resp.status_code == 405


@pytest.mark.asyncio
async def test_state_change_route_rejects_bad_origin_with_403(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """State-changing routes (accept POST) must 403 on non-allowlisted Origin.

    Since the websockets HTTP layer only supports GET, state changes arrive as
    GET. A cross-origin browser GET can still trigger the state change, so
    Origin enforcement must cover these routes regardless of method.
    """
    sm = _seed_session(tmp_path, key="websocket:origin")
    channel = _ch(bus, session_manager=sm, port=29931)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        boot = await _http_get("http://127.0.0.1:29931/webui/bootstrap")
        token = boot.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}
        # GET on a state-changing route (/api/skills/toggle accepts POST)
        # with a cross-origin Origin header — must be rejected.
        resp = await _http_get(
            "http://127.0.0.1:29931/api/skills/toggle?name=x&disabled=true",
            headers={**auth, "Origin": "http://evil.example.com"},
        )
        assert resp.status_code == 403
        # Empty Origin (non-browser client) must still be allowed.
        ok = await _http_get(
            "http://127.0.0.1:29931/api/skills/toggle?name=x&disabled=true",
            headers=auth,
        )
        # 200 or 400 (invalid name) is fine; the point is it's NOT 403.
        assert ok.status_code != 403
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_sensitive_values_header_merges_into_query(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``x-miniunicorn-Values`` JSON header must merge into ``ctx.query``."""
    captured: list[dict[str, list[str]]] = []

    def _capture(query: dict[str, list[str]]) -> dict:
        captured.append({k: list(v) for k, v in query.items()})
        return {}

    # Patch the handler module's imported reference, not the source module.
    monkeypatch.setattr(
        "miniunicorn.channels.websocket.handlers.settings.update_provider_settings",
        _capture,
    )
    sm = _seed_session(tmp_path, key="websocket:sens")
    channel = _ch(bus, session_manager=sm, port=29932)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        boot = await _http_get("http://127.0.0.1:29932/webui/bootstrap")
        token = boot.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}
        # Non-sensitive field in query, sensitive field in header.
        resp = await _http_get(
            "http://127.0.0.1:29932/api/settings/provider/update?provider=deepseek",
            headers={
                **auth,
                "x-miniunicorn-Values": json.dumps(
                    {"api_key": "sk-secret-123", "api_base": "https://api.deepseek.com"}
                ),
            },
        )
        assert resp.status_code == 200
        assert captured, "handler must have been invoked"
        merged = captured[-1]
        # Non-sensitive field from query preserved.
        assert merged["provider"] == ["deepseek"]
        # Sensitive field from header merged in.
        assert merged["api_key"] == ["sk-secret-123"]
        assert merged["api_base"] == ["https://api.deepseek.com"]
        # Sensitive value must NOT appear in the URL (only in the header).
        assert "sk-secret-123" not in str(resp.url)
    finally:
        await channel.stop()
        await server_task


def test_skill_zip_too_many_headers_returns_413() -> None:
    """Skill ZIP upload exceeding the header-count limit must return 413.

    Tested at the helper level because the websockets HTTP layer has its own
    header-line size limit that rejects oversized headers before dispatch.
    """
    from websockets.datastructures import Headers

    from miniunicorn.channels.websocket._http_routes import (
        SKILL_ZIP_MAX_HEADERS,
        ChunkedHeaderLimitError,
        collect_chunked_header_limited,
    )

    # Build headers with more chunks than allowed.
    pairs: list[tuple[str, str]] = [("x-miniunicorn-Skill-Zip", "AAAA")]
    for i in range(1, SKILL_ZIP_MAX_HEADERS + 5):
        pairs.append((f"x-miniunicorn-Skill-Zip-{i}", "AAAA"))
    headers = Headers(pairs)
    with pytest.raises(ChunkedHeaderLimitError):
        collect_chunked_header_limited(
            headers,
            "x-miniunicorn-Skill-Zip",
            max_count=SKILL_ZIP_MAX_HEADERS,
            max_total_bytes=32 * 1024 * 1024,
        )


def test_skill_zip_payload_too_large_returns_413() -> None:
    """Skill ZIP upload exceeding the total-byte limit must return 413."""
    from websockets.datastructures import Headers

    from miniunicorn.channels.websocket._http_routes import (
        SKILL_ZIP_MAX_TOTAL_BYTES,
        ChunkedHeaderLimitError,
        collect_chunked_header_limited,
    )

    oversize = "A" * (SKILL_ZIP_MAX_TOTAL_BYTES + 1024)
    headers = Headers([("x-miniunicorn-Skill-Zip", oversize)])
    with pytest.raises(ChunkedHeaderLimitError):
        collect_chunked_header_limited(
            headers,
            "x-miniunicorn-Skill-Zip",
            max_count=200,
            max_total_bytes=SKILL_ZIP_MAX_TOTAL_BYTES,
        )


@pytest.mark.asyncio
async def test_state_change_with_allowed_origin_succeeds(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET on a state-changing route from an allowlisted Origin must pass."""
    # Patch the handler module's imported reference.
    monkeypatch.setattr(
        "miniunicorn.channels.websocket.handlers.settings.update_runtime_settings",
        lambda q: {},
    )
    monkeypatch.setattr(
        "miniunicorn.channels.websocket.channel.WebSocketChannel._reload_cron_safe",
        lambda self: None,
    )
    sm = _seed_session(tmp_path, key="websocket:ok")
    channel = _ch(bus, session_manager=sm, port=29935)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        boot = await _http_get("http://127.0.0.1:29935/webui/bootstrap")
        token = boot.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}
        # localhost Origin is on the default allowlist; GET must succeed.
        resp = await _http_get(
            "http://127.0.0.1:29935/api/settings/runtime/update?heartbeat_interval_s=30",
            headers={**auth, "Origin": "http://127.0.0.1:5173"},
        )
        assert resp.status_code == 200
    finally:
        await channel.stop()
        await server_task
