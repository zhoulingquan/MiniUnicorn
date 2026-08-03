"""WebSocket server channel: Miniunicorn acts as a WebSocket server and serves connected clients.

Sibling ``_``-prefixed modules hold the path/HTTP/MIME/session/config helpers;
they are re-imported here so the public surface and test monkeypatch targets
keep working unchanged.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import mimetypes
import secrets
import ssl
import time
import uuid
from collections.abc import Callable, Iterable
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger
from websockets.asyncio.server import ServerConnection, serve, unix_serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request as WsRequest
from websockets.http11 import Response

from miniunicorn import __version__
from miniunicorn.agent.tools.mcp import request_mcp_reload
from miniunicorn.bus.agent_events import (
    AgentEvent,
    ReadyEvent,
)
from miniunicorn.bus.events import (
    OutboundMessage,
)
from miniunicorn.bus.queue import MessageBus
from miniunicorn.channels.base import BaseChannel
from miniunicorn.config.paths import get_media_dir, get_workspace_path
from miniunicorn.security.workspace_access import (
    WORKSPACE_SCOPE_METADATA_KEY,
    WorkspaceScopeError,
)
from miniunicorn.session.goal_state import goal_state_ws_blob
from miniunicorn.session.webui_turns import websocket_turn_wall_started_at
from miniunicorn.utils.media_decode import (
    FileSizeExceededError,
    save_base64_data_url,
)
from miniunicorn.webui.cli_apps_api import (
    cli_apps_action,  # noqa: F401 — re-exported as test monkeypatch target
    cli_apps_payload,  # noqa: F401 — re-exported as test monkeypatch target
    normalize_cli_app_mentions,
)
from miniunicorn.webui.mcp_presets_api import (
    normalize_mcp_preset_mentions,
)
from miniunicorn.webui.media_api import (
    sign_media_path,
    sign_or_stage_media_path,
)
from miniunicorn.webui.settings_api import (
    decorate_settings_payload,
    runtime_capabilities,
)
from miniunicorn.webui.transcript import (
    append_transcript_object,
    rewrite_local_markdown_images,
)
from miniunicorn.webui.workspaces import (
    WebUIWorkspaceController,
)

# Importing handlers triggers ``@router.route(...)`` registration for all
# migrated WebUI HTTP endpoints. Must come after ``_http_router``.
from . import handlers  # noqa: F401 — side effect: registers declarative routes

# Re-export helpers from sibling submodules so test monkeypatch targets keep working.
from ._http_router import RouteDeps, router  # noqa: F401 — router used by _dispatch_http
from ._http_routes import (  # noqa: F401
    _bearer_token,
    _http_error,
    _http_json_response,
    _http_response,
    _issue_route_secret_matches,
    _normalize_http_path,
    _parse_query,
    _parse_request_path,
    _query_first,
)
from ._media_sign import (  # noqa: F401
    _DOCUMENT_MIME_ALLOWED,
    _IMAGE_MIME_ALLOWED,
    _MAX_DOCUMENT_BYTES,
    _MAX_IMAGE_BYTES,
    _MAX_IMAGES_PER_MESSAGE,
    _MAX_VIDEO_BYTES,
    _MAX_VIDEOS_PER_MESSAGE,
    _UPLOAD_MIME_ALLOWED,
    _VIDEO_MIME_ALLOWED,
    _extract_data_url_mime,
)
from ._session import (  # noqa: F401
    _default_model_name_from_config,
    _is_valid_chat_id,
    _parse_envelope,
    _parse_inbound_payload,
    publish_runtime_model_update,
)
from ._ws_upgrade import (  # noqa: F401
    _LOCALHOSTS,
    WebSocketConfig,
    _case_insensitive_header,
    _host_for_url,
    _is_localhost,
    _is_websocket_upgrade,
    _normalize_config_path,
    _RateLimiter,
    _safe_host_header,
)
from .outbound import WebSocketOutboundEmitter

if TYPE_CHECKING:
    from miniunicorn.session.manager import SessionManager


def _resolve_bootstrap_model_name(
    runtime_name: Callable[[], str | None] | None,
) -> str | None:
    """Prefer an in-process resolver (e.g. AgentLoop); else config-derived default."""
    if runtime_name is not None:
        try:
            raw = runtime_name()
        except Exception as e:
            logger.debug("bootstrap runtime model resolver failed: {}", e)
        else:
            if isinstance(raw, str):
                stripped = raw.strip()
                if stripped:
                    return stripped
    return _default_model_name_from_config()


class WebSocketChannel(BaseChannel):
    """Run a local WebSocket server; forward text/JSON messages to the message bus."""

    name = "websocket"
    display_name = "WebSocket"

    def __init__(
        self,
        config: Any,
        bus: MessageBus,
        *,
        session_manager: "SessionManager | None" = None,
        static_dist_path: Path | None = None,
        workspace_path: Path | None = None,
        restrict_to_workspace: bool = False,
        runtime_model_name: Callable[[], str | None] | None = None,
        runtime_surface: str = "browser",
        runtime_capabilities_overrides: dict[str, Any] | None = None,
        provider_loader: Callable[[], Any] | None = None,
        cron_reloader: Callable[[], None] | None = None,
        agent_model_refresher: Callable[[], None] | None = None,
        cron_service: Any = None,
        tool_registry: Any = None,
        submit_control: Any = None,
    ):
        if isinstance(config, dict):
            config = WebSocketConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: WebSocketConfig = config
        # chat_id -> connections subscribed to it (fan-out target).
        self._subs: dict[str, set[Any]] = {}
        # connection -> chat_ids it is subscribed to (O(1) cleanup on disconnect).
        self._conn_chats: dict[Any, set[str]] = {}
        # connection -> default chat_id for legacy frames that omit routing.
        self._conn_default: dict[Any, str] = {}
        # Single-use tokens consumed at WebSocket handshake.
        self._issued_tokens: dict[str, float] = {}
        # Multi-use tokens for HTTP routes served beside WS; checked but not consumed.
        self._api_tokens: dict[str, float] = {}
        self._stop_event: asyncio.Event | None = None
        self._server_task: asyncio.Task[None] | None = None
        self._session_manager = session_manager
        self._static_dist_path: Path | None = (
            static_dist_path.resolve() if static_dist_path is not None else None
        )
        self._workspace_path = (
            Path(workspace_path).expanduser()
            if workspace_path is not None
            else get_workspace_path()
        ).resolve(strict=False)
        self._default_restrict_to_workspace = restrict_to_workspace
        self._webui_workspaces = WebUIWorkspaceController(
            session_manager=self._session_manager,
            default_workspace=self._workspace_path,
            default_restrict_to_workspace=self._default_restrict_to_workspace,
        )
        self._runtime_model_name = runtime_model_name
        # Lazy provider accessor used by HTTP routes that need to call the
        # LLM (e.g. /api/agents/generate). Returns an LLMProvider or None.
        self._provider_loader = provider_loader
        self._cron_reloader = cron_reloader
        self._agent_model_refresher = agent_model_refresher
        self._cron_service = cron_service
        self._tool_registry = tool_registry
        self._submit_control = submit_control
        self._runtime_surface = "native" if runtime_surface in {"native", "desktop"} else "browser"
        self._runtime_capabilities = runtime_capabilities(
            self._runtime_surface,
            runtime_capabilities_overrides,
        )
        self._settings_restart_sections: set[str] = set()
        self._periodic_cleanup_task: asyncio.Task | None = None
        # Process-local secret used to HMAC-sign media URLs. The signed URL is
        # the capability — anyone who holds a valid URL can fetch that one
        # file, nothing else. The secret regenerates on restart so links
        # become self-expiring (callers just refresh the session list).
        self._media_secret: bytes = secrets.token_bytes(32)
        # IP-level rate limiters (sliding window, per client IP).
        self._conn_rate_limiter = _RateLimiter(max_count=60, window_s=60.0)
        self._token_rate_limiter = _RateLimiter(max_count=60, window_s=60.0)
        self._media_rate_limiter = _RateLimiter(max_count=10, window_s=3600.0)
        self._origin_allowlist: set[str] = self._build_origin_allowlist()
        # Outbound emission service (Task 15): owns event serialization,
        # fan-out, and stream-text buffering via injected callbacks.
        self._outbound = WebSocketOutboundEmitter(
            connections_for_chat=self._connections_for_chat,
            safe_send=self._safe_send_to,
            rewrite_markdown_images=self._rewrite_local_markdown_images,
            append_transcript=self._try_append_webui_transcript,
            sign_media=self._sign_media_urls,
        )

    def _connections_for_chat(self, chat_id: str) -> Iterable[Any]:
        if chat_id == "*":
            return list(self._conn_chats)
        return list(self._subs.get(chat_id, ()))

    def _sign_media_urls(self, media: list[str] | None) -> list[dict[str, str]] | None:
        if not media:
            return None
        urls: list[dict[str, str]] = []
        for entry in media:
            signed = self._sign_or_stage_media_path(Path(entry))
            if signed is not None:
                urls.append(signed)
        return urls if urls else None

    # -- Client IP resolution and rate limiting ----------------------------

    def _get_real_client_ip(self, connection: Any) -> str:
        """Return the real client IP, resolving X-Forwarded-For only for trusted proxies."""
        addr = getattr(connection, "remote_address", None)
        if not addr:
            return ""
        peer_ip = addr[0] if isinstance(addr, tuple) else str(addr)
        # Normalize IPv6-mapped IPv4 (``::ffff:127.0.0.1`` → ``127.0.0.1``).
        if peer_ip.startswith("::ffff:"):
            peer_ip = peer_ip[7:]

        if peer_ip in self.config.trusted_proxies:
            request = getattr(connection, "request", None)
            if request is not None:
                xff = request.headers.get("X-Forwarded-For") or request.headers.get(
                    "x-forwarded-for"
                )
                if xff:
                    # Leftmost entry is the original client IP.
                    first = xff.split(",")[0].strip()
                    if first:
                        return first
        return peer_ip

    def _is_localhost_connection(self, connection: Any) -> bool:
        """Like the module-level ``_is_localhost`` but proxy-aware."""
        ip = self._get_real_client_ip(connection)
        return ip in _LOCALHOSTS

    def _check_rate_limit(self, limiter: _RateLimiter, connection: Any, label: str) -> bool:
        """Return True if the request passes the rate limit, else log and return False."""
        ip = self._get_real_client_ip(connection)
        if not ip:
            return True  # Unknown peer — don't block (other auth guards apply).
        if not limiter.check(ip):
            self.logger.warning("rate limit exceeded for {} ({})", ip, label)
            return False
        return True

    # -- 浏览器 Origin 校验 ------------------------------------------------

    def _build_origin_allowlist(self) -> set[str]:
        """Build default Origin allowlist: localhost any port + gateway + admin config."""
        hosts = ("127.0.0.1", "localhost")
        schemes = ("http", "https")
        allowlist: set[str] = set()
        for scheme in schemes:
            for host in hosts:
                allowlist.add(f"{scheme}://{host}")
                port = int(getattr(self.config, "port", 0) or 0)
                if port > 0:
                    allowlist.add(f"{scheme}://{host}:{port}")
        for origin in getattr(self.config, "allow_origin", None) or []:
            origin_norm = origin.strip()
            if origin_norm:
                allowlist.add(origin_norm)
        return allowlist

    def _is_origin_allowed(self, request: Any) -> bool:
        """Return True if the browser Origin header is allowlisted."""
        headers = getattr(request, "headers", None) if request is not None else None
        if headers is None:
            return True
        origin = headers.get("Origin") or headers.get("origin")
        if not origin:
            return True  # Non-browser request (no Origin): other auth layers apply.
        origin = origin.strip()
        if not origin:
            return True
        if origin in self._origin_allowlist:
            return True
        # Allow any localhost port (covers dev servers on dynamic ports).
        try:
            scheme, _, host_port = origin.partition("://")
            if scheme and host_port:
                host = host_port.split(":", 1)[0]
                if host in ("127.0.0.1", "localhost"):
                    return True
        except Exception:
            pass
        return False

    # -- Subscription bookkeeping -------------------------------------------

    def _attach(self, connection: Any, chat_id: str) -> None:
        """Idempotently subscribe *connection* to *chat_id*."""
        self._subs.setdefault(chat_id, set()).add(connection)
        self._conn_chats.setdefault(connection, set()).add(chat_id)

    def _cleanup_connection(self, connection: Any) -> None:
        """Remove *connection* from every subscription set; safe to call multiple times."""
        chat_ids = self._conn_chats.pop(connection, set())
        for cid in chat_ids:
            subs = self._subs.get(cid)
            if subs is None:
                continue
            subs.discard(connection)
            if not subs:
                self._subs.pop(cid, None)
        self._conn_default.pop(connection, None)

    async def _maybe_push_active_goal_state(self, chat_id: str) -> None:
        """Replay an active sustained goal from session metadata after *chat_id* is subscribed."""
        if self._session_manager is None:
            return
        row = self._session_manager.read_session_file(f"websocket:{chat_id}")
        meta = row.get("metadata", {}) if isinstance(row, dict) else {}
        if not isinstance(meta, dict):
            meta = {}
        blob = goal_state_ws_blob(meta)
        if not blob.get("active"):
            return
        await self.send_goal_state(chat_id, blob)

    async def _maybe_push_turn_run_wall_clock(self, chat_id: str) -> None:
        """Replay ``goal_status: running`` when a turn is still active (same-process refresh)."""
        t0 = websocket_turn_wall_started_at(chat_id)
        if t0 is None:
            return
        await self.send_goal_status(chat_id, "running", started_at=t0)

    async def _hydrate_after_subscribe(self, chat_id: str) -> None:
        """Replay goal/run strip state after subscribe (same-process refresh)."""
        await self._maybe_push_active_goal_state(chat_id)
        await self._maybe_push_turn_run_wall_clock(chat_id)

    async def _send_event(self, connection: Any, event: str, **fields: Any) -> None:
        """Send a control event (attached, error, session_updated) to a single connection."""
        try:
            await self._outbound.send_control_event(connection, event, fields)
        except Exception as e:
            self.logger.warning("failed to send {} event: {}", event, e)

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return WebSocketConfig().model_dump(by_alias=True)

    def _expected_path(self) -> str:
        return _normalize_config_path(self.config.path)

    def _build_ssl_context(self) -> ssl.SSLContext | None:
        cert = self.config.ssl_certfile.strip()
        key = self.config.ssl_keyfile.strip()
        if not cert and not key:
            return None
        if not cert or not key:
            raise ValueError(
                "ssl_certfile and ssl_keyfile must both be set for WSS, or both left empty"
            )
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(certfile=cert, keyfile=key)
        return ctx

    _MAX_ISSUED_TOKENS = 10_000
    _STREAM_TEXT_BUF_CLEANUP_INTERVAL = 300

    def _purge_expired_issued_tokens(self) -> None:
        now = time.monotonic()
        for token_key, expiry in list(self._issued_tokens.items()):
            if now > expiry:
                self._issued_tokens.pop(token_key, None)

    def _cleanup_stale_stream_text_buffers(self) -> None:
        """Evict stream-text buffers past their TTL (delegated to emitter)."""
        evicted = self._outbound.cleanup_stale_buffers()
        if evicted:
            self.logger.warning("清理了 {} 个陈旧的流文本缓冲", evicted)

    def _cleanup_rate_limiters(self) -> None:
        """Purge expired keys from all three RateLimiters."""
        self._conn_rate_limiter.cleanup()
        self._token_rate_limiter.cleanup()
        self._media_rate_limiter.cleanup()

    async def _periodic_cleanup_loop(self) -> None:
        """Background cleanup loop: stream buffers + rate limiters."""
        while self._running:
            try:
                await asyncio.sleep(self._STREAM_TEXT_BUF_CLEANUP_INTERVAL)
                self._cleanup_stale_stream_text_buffers()
                self._cleanup_rate_limiters()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.logger.warning("周期清理任务异常: {}", exc)

    def _take_issued_token_if_valid(self, token_value: str | None) -> bool:
        """Validate and consume one issued token (single use per connection attempt)."""
        if not token_value:
            return False
        self._purge_expired_issued_tokens()
        expiry = self._issued_tokens.pop(token_value, None)
        if expiry is None:
            return False
        if time.monotonic() > expiry:
            return False
        return True

    def _handle_token_issue_http(self, connection: Any, request: Any) -> Any:
        secret = self.config.token_issue_secret.strip()
        if secret:
            if not _issue_route_secret_matches(request.headers, secret):
                return connection.respond(401, "Unauthorized")
        else:
            self.logger.warning(
                "token_issue_path is set but token_issue_secret is empty; "
                "any client can obtain connection tokens — set token_issue_secret for production."
            )
        # Per-IP token issuance rate limit (5/min).
        if not self._check_rate_limit(self._token_rate_limiter, connection, "token_issue"):
            return _http_json_response({"error": "rate limited"}, status=429)
        self._purge_expired_issued_tokens()
        if len(self._issued_tokens) >= self._MAX_ISSUED_TOKENS:
            self.logger.error(
                "too many outstanding issued tokens ({}), rejecting issuance",
                len(self._issued_tokens),
            )
            return _http_json_response({"error": "too many outstanding tokens"}, status=429)
        token_value = f"nbwt_{secrets.token_urlsafe(32)}"
        self._issued_tokens[token_value] = time.monotonic() + float(self.config.token_ttl_s)

        return _http_json_response({"token": token_value, "expires_in": self.config.token_ttl_s})

    # -- HTTP dispatch ------------------------------------------------------

    async def _dispatch_http(self, connection: Any, request: WsRequest) -> Any:
        """Route an inbound HTTP request to a handler or to the WS upgrade path."""
        got, query = _parse_request_path(request.path)

        # 1. Custom token issuance endpoint.
        if self.config.token_issue_path:
            issue_expected = _normalize_config_path(self.config.token_issue_path)
            if got == issue_expected:
                return self._handle_token_issue_http(connection, request)

        # 1b. /health — public liveness probe (no token, no state).
        if got == "/health":
            return _http_json_response({"status": "ok"})

        # 2. /webui/bootstrap — channel-side stateful bootstrap endpoint.
        if got == "/webui/bootstrap":
            return self._handle_bootstrap(connection, request)

        # 3. Declarative router: handlers receive dependencies via RouteDeps.
        deps = self._build_route_deps()
        api_response = await router.dispatch(deps, connection, request)
        if api_response is not None:
            return api_response

        # 4. WebSocket upgrade.
        ws_matched, ws_response = self._dispatch_websocket_upgrade(connection, request, got, query)
        if ws_matched:
            return ws_response

        # API clients must not receive the SPA shell for unknown routes.
        if got.startswith("/api/"):
            return _http_error(404, "API route not found")

        # 5. Static files.
        if self._static_dist_path is not None:
            response = self._serve_static(got)
            if response is not None:
                return response

        return connection.respond(404, "Not Found")

    def _build_route_deps(self) -> RouteDeps:
        """Build a dependency snapshot for one request, injecting channel state into handlers."""
        return RouteDeps(
            workspace_path=self._workspace_path,
            webui_workspaces=self._webui_workspaces,
            session_manager=self._session_manager,
            cron_service=self._cron_service,
            tool_registry=self._tool_registry,
            provider_loader=self._provider_loader,
            runtime_model_name=self._runtime_model_name,
            runtime_surface=self._runtime_surface,
            runtime_capabilities=self._runtime_capabilities,
            media_secret=self._media_secret,
            bus=self.bus,
            logger=self.logger,
            check_api_token=self._check_api_token,
            is_localhost_connection=self._is_localhost_connection,
            is_origin_allowed=self._is_origin_allowed,
            with_restart_state=self._with_settings_restart_state,
            refresh_agent_model=self._maybe_refresh_agent_model,
            reload_cron=self._reload_cron_safe,
            reload_mcp=self._reload_mcp_safe,
            notify_session_updated=self._notify_session_updated_safe,
            invalidate_bootstrap_cache=self._invalidate_bootstrap_cache,
            sign_media_path=self._sign_media_path,
            sign_or_stage_media_path=self._sign_or_stage_media_path,
            get_media_dir=get_media_dir,
        )

    def _reload_cron_safe(self) -> None:
        """Re-register heartbeat/dream cron tasks so new intervals take effect (best-effort)."""
        if self._cron_reloader is not None:
            try:
                self._cron_reloader()
            except Exception:
                logger.exception("Cron reloader failed after runtime settings update")

    def _reload_mcp_safe(self) -> None:
        """Trigger MCP service hot-reload (best-effort)."""
        try:
            request_mcp_reload(self.bus)
        except Exception:
            logger.exception("MCP reload failed after preset change")

    def _notify_session_updated_safe(self, chat_id: str) -> None:
        """Fire-and-forget: notify connected WS clients to refresh session view."""
        try:
            asyncio.create_task(self.send_session_updated(chat_id))
        except RuntimeError:
            # No running loop — the client will refresh on next poll/reconnect.
            pass

    def _invalidate_bootstrap_cache(self, name: str) -> None:
        """Best-effort invalidation of ContextBuilder's bootstrap file cache."""
        try:
            agent = getattr(self, "_agent_loop", None) or getattr(self, "agent_loop", None)
            ctx = getattr(agent, "context", None) if agent else None
            cache = getattr(ctx, "_bootstrap_cache", None) if ctx else None
            if cache is None:
                return
            target = self._workspace_path / name
            for key in list(cache):
                try:
                    if str(key) == str(target):
                        cache.pop(key, None)
                except Exception:
                    continue
        except Exception:
            # Cache invalidation is best-effort; mtime check covers it anyway.
            pass

    def _dispatch_websocket_upgrade(
        self,
        connection: Any,
        request: WsRequest,
        got: str,
        query: dict[str, list[str]],
    ) -> tuple[bool, Any | None]:
        """Authorize only real WS upgrade requests for the configured path."""
        expected_ws = self._expected_path()
        if got != expected_ws or not _is_websocket_upgrade(request):
            return False, None
        client_id = _query_first(query, "client_id") or ""
        if len(client_id) > 128:
            client_id = client_id[:128]
        if not self.is_allowed(client_id):
            return True, connection.respond(403, "Forbidden")
        return True, self._authorize_websocket_handshake(connection, request, query)

    # -- HTTP route handlers ------------------------------------------------

    def _check_api_token(self, request: WsRequest) -> bool:
        """Validate a request against the API token pool (multi-use, TTL-bound)."""
        self._purge_expired_api_tokens()
        # Prefer Authorization header; fall back to ?token= query param (deprecated).
        token = _bearer_token(request.headers)
        if not token:
            token = _query_first(_parse_query(request.path), "token")
            if token:
                self.logger.warning(
                    "使用 ?token= 查询参数鉴权已废弃,请改用 Authorization 头;token 可能泄漏到日志/Referer/浏览器历史"
                )
        if not token:
            return False
        expiry = self._api_tokens.get(token)
        if expiry is None or time.monotonic() > expiry:
            self._api_tokens.pop(token, None)
            return False
        return True

    def _purge_expired_api_tokens(self) -> None:
        now = time.monotonic()
        for token_key, expiry in list(self._api_tokens.items()):
            if now > expiry:
                self._api_tokens.pop(token_key, None)

    def _handle_bootstrap(self, connection: Any, request: Any) -> Response:
        # Origin check blocks cross-origin browser fetch() from stealing tokens.
        if not self._is_origin_allowed(request):
            return _http_error(403, "Forbidden")
        # When a secret is configured, validate it regardless of source IP
        # (secures deployments behind a reverse proxy where all peers are localhost).
        secret = self.config.token_issue_secret.strip() or self.config.token.strip()
        if secret:
            if not _issue_route_secret_matches(request.headers, secret):
                return _http_error(401, "Unauthorized")
        elif not self._is_localhost_connection(connection):
            return _http_error(403, "bootstrap is localhost-only")
        if not self._check_rate_limit(self._token_rate_limiter, connection, "bootstrap"):
            return _http_json_response({"error": "rate limited"}, status=429)
        self._purge_expired_issued_tokens()
        self._purge_expired_api_tokens()
        if (
            len(self._issued_tokens) >= self._MAX_ISSUED_TOKENS
            or len(self._api_tokens) >= self._MAX_ISSUED_TOKENS
        ):
            return _http_response(
                json.dumps({"error": "too many outstanding tokens"}).encode("utf-8"),
                status=429,
                content_type="application/json; charset=utf-8",
            )
        token = f"nbwt_{secrets.token_urlsafe(32)}"
        expiry = time.monotonic() + float(self.config.token_ttl_s)
        # Same string registered in both pools: WS handshake consumes one copy,
        # REST surface validates the other until TTL expiry.
        self._issued_tokens[token] = expiry
        self._api_tokens[token] = expiry
        ws_url = self._bootstrap_ws_url(request)
        return _http_json_response(
            {
                "token": token,
                "ws_path": self._expected_path(),
                "ws_url": ws_url,
                "expires_in": self.config.token_ttl_s,
                "model_name": _resolve_bootstrap_model_name(self._runtime_model_name),
                "runtime_surface": self._runtime_surface,
                "runtime_capabilities": self._runtime_capabilities,
                "version": __version__,
                # Frame-size cap echoed to the WebUI so the Composer can
                # pre-validate payload size and avoid a server-side 1009 close.
                "max_message_bytes": int(self.config.max_message_bytes),
            }
        )

    def _bootstrap_ws_url(self, request: Any) -> str:
        """Absolute WS URL clients should prefer over a dev-server proxy."""
        headers = getattr(request, "headers", {}) or {}
        host = _safe_host_header(_case_insensitive_header(headers, "Host"))
        if not host:
            host = _host_for_url(self.config.host, self.config.port)

        proto = _case_insensitive_header(headers, "X-Forwarded-Proto")
        proto = proto.split(",", 1)[0].strip().lower()
        secure = proto in {"https", "wss"} or bool(self.config.ssl_certfile.strip())
        scheme = "wss" if secure else "ws"
        return f"{scheme}://{host}{self._expected_path()}"

    def _with_settings_restart_state(
        self,
        payload: dict[str, Any],
        *,
        section: str | None = None,
    ) -> dict[str, Any]:
        """Keep restart-required state alive for this gateway process."""
        if section and payload.get("requires_restart"):
            self._settings_restart_sections.add(section)
        sections = sorted(self._settings_restart_sections)
        payload = dict(payload)
        if sections:
            payload["requires_restart"] = True
        return decorate_settings_payload(
            payload,
            surface=self._runtime_surface,
            runtime_capability_overrides=self._runtime_capabilities,
            restart_required_sections=sections,
        )

    def _maybe_refresh_agent_model(self) -> None:
        """Refresh the running agent's model after settings changes."""
        if self._agent_model_refresher is not None:
            try:
                self._agent_model_refresher()
            except Exception:
                logger.exception("Agent model refresh failed after settings update")

    def _try_append_webui_transcript(self, chat_id: str, wire: dict[str, Any]) -> None:
        sk = f"websocket:{chat_id}"
        try:
            dup = json.loads(json.dumps(wire, ensure_ascii=False))
            append_transcript_object(sk, dup)
        except (ValueError, TypeError) as e:
            self.logger.warning("webui transcript append failed: {}", e)

    async def _handle_message(
        self,
        sender_id: str,
        chat_id: str,
        content: str,
        media: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        session_key: str | None = None,
        is_dm: bool = False,
    ) -> None:
        meta = metadata or {}
        if meta.get("webui"):
            user_obj: dict[str, Any] = {
                "event": "user",
                "chat_id": chat_id,
                "text": content,
            }
            if media:
                user_obj["media_paths"] = list(media)
            cli_apps = meta.get("cli_apps")
            if isinstance(cli_apps, list) and cli_apps:
                user_obj["cli_apps"] = cli_apps
            mcp_presets = meta.get("mcp_presets")
            if isinstance(mcp_presets, list) and mcp_presets:
                user_obj["mcp_presets"] = mcp_presets
            self._try_append_webui_transcript(chat_id, user_obj)
        await super()._handle_message(
            sender_id,
            chat_id,
            content,
            media,
            metadata,
            session_key,
            is_dm,
        )

    def _sign_media_path(self, abs_path: Path) -> str | None:
        """Return a ``/api/media/<sig>/<payload>`` URL for *abs_path*, or"""
        return sign_media_path(
            abs_path,
            secret=self._media_secret,
            media_dir=lambda channel=None: get_media_dir(channel),
        )

    def _sign_or_stage_media_path(self, path: Path) -> dict[str, str] | None:
        """Return a signed media URL payload for *path*."""
        return sign_or_stage_media_path(
            path,
            secret=self._media_secret,
            media_dir=lambda channel=None: get_media_dir(channel),
            logger=self.logger,
        )

    def _rewrite_local_markdown_images(self, text: str) -> str:
        return rewrite_local_markdown_images(
            text,
            workspace_path=self._workspace_path,
            sign_path=self._sign_or_stage_media_path,
        )

    # -- Backward-compat wrappers for tests that call handlers directly -----

    def _handle_sessions_list(self, request: WsRequest) -> Response:
        """Backward-compat: delegates to the ``/api/sessions`` route handler."""
        from ._http_router import RouteContext

        deps = self._build_route_deps()
        got, query = _parse_request_path(request.path)
        entry = router._exact.get(got)
        if entry is None:
            return _http_error(404, "not found")
        ctx = RouteContext(deps=deps, connection=None, request=request, query=query, got=got)
        return entry.fn(ctx)  # type: ignore[return-value]

    def _handle_webui_thread_get(self, request: WsRequest, key: str) -> Response:
        """Backward-compat: delegates to the ``/api/sessions/<key>/webui-thread`` handler."""
        from ._http_router import RouteContext

        deps = self._build_route_deps()
        # ``key`` is already URL-encoded by the caller (matches old signature).
        got = f"/api/sessions/{key}/webui-thread"
        _, query = _parse_request_path(request.path)
        for pattern, entry in router._regex:
            m = pattern.match(got)
            if m is not None:
                ctx = RouteContext(
                    deps=deps,
                    connection=None,
                    request=request,
                    query=query,
                    got=got,
                    path_vars=m.groupdict(),
                )
                return entry.fn(ctx)  # type: ignore[return-value]
        return _http_error(404, "not found")

    # -- Static files and WebSocket handshake ------------------------------

    def _serve_static(self, request_path: str) -> Response | None:
        """Resolve *request_path* against the built SPA directory; SPA fallback to index.html."""
        assert self._static_dist_path is not None
        rel = request_path.lstrip("/")
        if not rel:
            rel = "index.html"
        if ".." in rel.split("/") or rel.startswith("/"):
            return _http_error(403, "Forbidden")
        candidate = (self._static_dist_path / rel).resolve()
        try:
            candidate.relative_to(self._static_dist_path)
        except ValueError:
            return _http_error(403, "Forbidden")
        if not candidate.is_file():
            # SPA history-mode fallback: unknown routes serve index.html.
            index = self._static_dist_path / "index.html"
            if index.is_file():
                candidate = index
            else:
                return None
        try:
            body = candidate.read_bytes()
        except OSError as e:
            self.logger.warning("static: failed to read {}: {}", candidate, e)
            return _http_error(500, "Internal Server Error")
        ctype, _ = mimetypes.guess_type(candidate.name)
        if ctype is None:
            ctype = "application/octet-stream"
        if ctype.startswith("text/") or ctype in {"application/javascript", "application/json"}:
            ctype = f"{ctype}; charset=utf-8"
        # Hash-named build assets are immutable; index.html and brand assets stay fresh.
        if candidate.name == "index.html" or "/brand/" in request_path:
            cache = "no-cache"
        else:
            cache = "public, max-age=31536000, immutable"
        return _http_response(
            body,
            status=200,
            content_type=ctype,
            extra_headers=[("Cache-Control", cache)],
        )

    def _authorize_websocket_handshake(
        self, connection: Any, request: WsRequest, query: dict[str, list[str]]
    ) -> Any:
        # Origin check: empty Origin (non-browser) is allowed; non-empty must be allowlisted.
        if not self._is_origin_allowed(request):
            return connection.respond(403, "Forbidden")
        supplied = _query_first(query, "token")
        static_token = self.config.token.strip()

        if static_token:
            if supplied and hmac.compare_digest(supplied, static_token):
                return None
            if supplied and self._take_issued_token_if_valid(supplied):
                return None
            return connection.respond(401, "Unauthorized")

        if self.config.websocket_requires_token:
            if supplied and self._take_issued_token_if_valid(supplied):
                return None
            return connection.respond(401, "Unauthorized")

        if supplied:
            self._take_issued_token_if_valid(supplied)
        return None

    # -- Server lifecycle and connection ingress ---------------------------

    async def start(self) -> None:
        from miniunicorn.utils.logging_bridge import redirect_lib_logging

        redirect_lib_logging("websockets", level="WARNING")

        self._running = True
        self._stop_event = asyncio.Event()

        ssl_context = self._build_ssl_context()
        scheme = "wss" if ssl_context else "ws"

        async def process_request(connection: ServerConnection, request: WsRequest) -> Any:
            return await self._dispatch_http(connection, request)

        async def handler(connection: ServerConnection) -> None:
            await self._connection_loop(connection)

        self.logger.info(
            "WebSocket server listening on {}",
            (
                f"unix:{self.config.unix_socket_path}{self.config.path}"
                if self.config.unix_socket_path
                else f"{scheme}://{self.config.host}:{self.config.port}{self.config.path}"
            ),
        )
        if self.config.token_issue_path:
            self.logger.info(
                "WebSocket token issue route: {}",
                (
                    f"unix:{self.config.unix_socket_path}{_normalize_config_path(self.config.token_issue_path)}"
                    if self.config.unix_socket_path
                    else (
                        f"{scheme}://{self.config.host}:{self.config.port}"
                        f"{_normalize_config_path(self.config.token_issue_path)}"
                    )
                ),
            )

        async def runner() -> None:
            socket_path = self.config.unix_socket_path
            if socket_path:
                path_obj = Path(socket_path)
                path_obj.parent.mkdir(parents=True, exist_ok=True)
                with suppress(FileNotFoundError):
                    path_obj.unlink()
                server = await unix_serve(
                    handler,
                    socket_path,
                    process_request=process_request,
                    max_size=self.config.max_message_bytes,
                    ping_interval=self.config.ping_interval_s,
                    ping_timeout=self.config.ping_timeout_s,
                    # open_timeout=None: process_request also serves slow HTTP API routes.
                    open_timeout=None,
                )
                with suppress(OSError):
                    path_obj.chmod(0o600)
            else:
                server = await serve(
                    handler,
                    self.config.host,
                    self.config.port,
                    process_request=process_request,
                    max_size=self.config.max_message_bytes,
                    ping_interval=self.config.ping_interval_s,
                    ping_timeout=self.config.ping_timeout_s,
                    ssl=ssl_context,
                    open_timeout=None,  # See above: slow HTTP API routes.
                )
            try:
                assert self._stop_event is not None
                await self._stop_event.wait()
            finally:
                server.close()
                await server.wait_closed()
                if socket_path:
                    with suppress(FileNotFoundError):
                        Path(socket_path).unlink()

        self._server_task = asyncio.create_task(runner())
        self._periodic_cleanup_task = asyncio.create_task(self._periodic_cleanup_loop())
        await self._server_task

    async def _connection_loop(self, connection: Any) -> None:
        request = connection.request
        path_part = request.path if request else "/"
        _, query = _parse_request_path(path_part)
        client_id_raw = _query_first(query, "client_id")
        client_id = client_id_raw.strip() if client_id_raw else ""
        if not client_id:
            client_id = f"anon-{uuid.uuid4().hex[:12]}"
        elif len(client_id) > 128:
            self.logger.warning("client_id too long ({} chars), truncating", len(client_id))
            client_id = client_id[:128]

        # Per-IP connection rate limit (10/min).
        if not self._check_rate_limit(self._conn_rate_limiter, connection, "connection"):
            with suppress(Exception):
                await connection.close(code=1013, reason="rate limited")
            return

        default_chat_id = str(uuid.uuid4())

        try:
            ready_event = ReadyEvent(chat_id=default_chat_id, client_id=client_id)
            await self._send_agent_event(ready_event, connections=[connection], label=" ready ")
            # Register only after ready is sent to avoid out-of-order delivery.
            self._conn_default[connection] = default_chat_id
            self._attach(connection, default_chat_id)
            await self._hydrate_after_subscribe(default_chat_id)

            async for raw in connection:
                if isinstance(raw, bytes):
                    try:
                        raw = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        self.logger.warning("ignoring non-utf8 binary frame")
                        continue

                envelope = _parse_envelope(raw)
                if envelope is not None:
                    await self._dispatch_envelope(connection, client_id, envelope)
                    continue

                content = _parse_inbound_payload(raw)
                if content is None:
                    continue
                # WS authenticates at handshake; treat as non-DM (no pairing codes).
                await self._handle_message(
                    sender_id=client_id,
                    chat_id=default_chat_id,
                    content=content,
                    metadata={"remote": getattr(connection, "remote_address", None)},
                    is_dm=False,
                )
        except Exception as e:
            self.logger.debug("connection ended: {}", e)
        finally:
            self._cleanup_connection(connection)

    # -- Inbound WebSocket envelopes ---------------------------------------

    def _save_envelope_media(self, media: list[Any]) -> tuple[list[str], str | None]:
        """Decode and persist ``media`` items from a ``message`` envelope."""
        image_count = 0
        video_count = 0
        for item in media:
            mime = (
                _extract_data_url_mime(item.get("data_url", "")) if isinstance(item, dict) else None
            )
            if mime in _VIDEO_MIME_ALLOWED:
                video_count += 1
            elif mime in _IMAGE_MIME_ALLOWED or mime in _DOCUMENT_MIME_ALLOWED:
                # Documents share the image attachment pool.
                image_count += 1
        if image_count > _MAX_IMAGES_PER_MESSAGE:
            return [], "too_many_images"
        if video_count > _MAX_VIDEOS_PER_MESSAGE:
            return [], "too_many_videos"

        media_dir = get_media_dir("websocket")
        paths: list[str] = []

        def _abort(reason: str) -> tuple[list[str], str]:
            for p in paths:
                try:
                    Path(p).unlink(missing_ok=True)
                except OSError as exc:
                    self.logger.warning("failed to unlink partial media {}: {}", p, exc)
            return [], reason

        for item in media:
            if not isinstance(item, dict):
                return _abort("malformed")
            data_url = item.get("data_url")
            if not isinstance(data_url, str) or not data_url:
                return _abort("malformed")
            mime = _extract_data_url_mime(data_url)
            if mime is None:
                return _abort("decode")
            if mime not in _UPLOAD_MIME_ALLOWED:
                return _abort("mime")
            is_video = mime in _VIDEO_MIME_ALLOWED
            is_document = mime in _DOCUMENT_MIME_ALLOWED
            if is_video:
                max_bytes = _MAX_VIDEO_BYTES
            elif is_document:
                max_bytes = _MAX_DOCUMENT_BYTES
            else:
                max_bytes = _MAX_IMAGE_BYTES
            # Preserve filename so save_base64_data_url can fall back to its
            # extension for application/octet-stream (browsers use this for
            # .log/.toml/.ini/.cfg text formats parsed by extension).
            name_hint = item.get("name") if isinstance(item.get("name"), str) else None
            try:
                saved = save_base64_data_url(
                    data_url,
                    media_dir,
                    max_bytes=max_bytes,
                    filename_hint=name_hint,
                )
            except FileSizeExceededError:
                return _abort("size")
            except Exception as exc:
                self.logger.warning("media decode failed: {}", exc)
                return _abort("decode")
            if saved is None:
                return _abort("decode")
            paths.append(saved)
        return paths, None

    async def _dispatch_envelope(
        self, connection: Any, client_id: str, envelope: dict[str, Any]
    ) -> None:
        """Route one typed inbound envelope (``new_chat`` / ``attach`` / ``message``)."""
        t = envelope.get("type")
        if t == "new_chat":
            new_id = str(uuid.uuid4())
            # Echo request_id so the WebUI can correlate newChat() with the attached response.
            req_id = envelope.get("request_id")
            if not isinstance(req_id, str) or not req_id:
                req_id = None
            scope = await self._workspace_scope_or_error(
                connection,
                lambda: self._webui_workspaces.scope_for_new_chat(
                    envelope,
                    controls_available=self._is_localhost_connection(connection),
                ),
            )
            if scope is None:
                return
            self._webui_workspaces.persist_scope(new_id, scope)
            self._attach(connection, new_id)
            attached_fields: dict[str, Any] = {"chat_id": new_id}
            if req_id is not None:
                attached_fields["request_id"] = req_id
            await self._send_event(connection, "attached", **attached_fields)
            await self._send_event(
                connection,
                "session_updated",
                chat_id=new_id,
                scope="metadata",
                workspace_scope=scope.payload(),
            )
            await self._hydrate_after_subscribe(new_id)
            return
        if t == "attach":
            cid = envelope.get("chat_id")
            if not _is_valid_chat_id(cid):
                await self._send_event(connection, "error", detail="invalid chat_id")
                return
            self._attach(connection, cid)
            await self._send_event(connection, "attached", chat_id=cid)
            await self._hydrate_after_subscribe(cid)
            return
        if t == "set_workspace_scope":
            cid = envelope.get("chat_id")
            if not _is_valid_chat_id(cid):
                await self._send_event(connection, "error", detail="invalid chat_id")
                return
            scope = await self._workspace_scope_or_error(
                connection,
                lambda: self._webui_workspaces.scope_for_set_request(
                    envelope,
                    chat_id=cid,
                    chat_running=websocket_turn_wall_started_at(cid) is not None,
                    controls_available=self._is_localhost_connection(connection),
                ),
                chat_id=cid,
            )
            if scope is None:
                return
            self._webui_workspaces.persist_scope(cid, scope)
            await self._send_event(
                connection,
                "session_updated",
                chat_id=cid,
                scope="metadata",
                workspace_scope=scope.payload(),
            )
            return
        if t == "message":
            cid = envelope.get("chat_id")
            content = envelope.get("content")
            if not _is_valid_chat_id(cid):
                await self._send_event(connection, "error", detail="invalid chat_id")
                return
            if not isinstance(content, str):
                await self._send_event(connection, "error", detail="missing content")
                return

            raw_media = envelope.get("media")
            media_paths: list[str] = []
            if raw_media is not None:
                if not isinstance(raw_media, list):
                    await self._send_event(
                        connection,
                        "error",
                        detail="image_rejected",
                        reason="malformed",
                    )
                    return
                # Per-IP media upload rate limit (10/hour).
                if not self._check_rate_limit(self._media_rate_limiter, connection, "media_upload"):
                    await self._send_event(
                        connection,
                        "error",
                        detail="image_rejected",
                        reason="rate_limited",
                    )
                    return
                media_paths, reason = self._save_envelope_media(raw_media)
                if reason is not None:
                    await self._send_event(
                        connection,
                        "error",
                        detail="image_rejected",
                        reason=reason,
                    )
                    return

            # Allow image-only turns (content may be empty when media is attached).
            if not content.strip() and not media_paths:
                await self._send_event(connection, "error", detail="missing content")
                return
            scope = await self._workspace_scope_or_error(
                connection,
                lambda: self._webui_workspaces.scope_for_message(
                    envelope,
                    chat_id=cid,
                    chat_running=websocket_turn_wall_started_at(cid) is not None,
                    controls_available=self._is_localhost_connection(connection),
                ),
                chat_id=cid,
            )
            if scope is None:
                return

            # Auto-attach on first use so clients can one-shot without a separate attach.
            self._attach(connection, cid)
            await self._hydrate_after_subscribe(cid)
            metadata: dict[str, Any] = {"remote": getattr(connection, "remote_address", None)}
            if envelope.get("webui") is True:
                metadata["webui"] = True
            cli_apps = normalize_cli_app_mentions(envelope.get("cli_apps"))
            if cli_apps:
                metadata["cli_apps"] = cli_apps
            mcp_presets = normalize_mcp_preset_mentions(envelope.get("mcp_presets"))
            if mcp_presets:
                metadata["mcp_presets"] = mcp_presets
            metadata[WORKSPACE_SCOPE_METADATA_KEY] = scope.metadata()
            self._webui_workspaces.persist_scope(cid, scope)
            await self._handle_message(
                sender_id=client_id,
                chat_id=cid,
                content=content,
                media=media_paths or None,
                metadata=metadata,
                is_dm=False,
            )
            return
        if t == "task_control":
            await self._handle_task_control(connection, client_id, envelope)
            return
        await self._send_event(connection, "error", detail=f"unknown type: {t!r}")

    async def _handle_task_control(
        self,
        connection: Any,
        client_id: str,
        envelope: dict[str, Any],
    ) -> None:
        """Handle a task control request (cancel/steer/continue/approve/reject)."""
        if self._submit_control is None:
            await self._send_event(connection, "error", detail="task_control not available")
            return

        task_id = envelope.get("task_id")
        action = envelope.get("action")
        if not isinstance(task_id, str) or not task_id:
            await self._send_event(connection, "error", detail="invalid task_id")
            return
        if not isinstance(action, str) or not action:
            await self._send_event(connection, "error", detail="missing action")
            return

        action_map = {
            "cancel": "CANCEL",
            "steer": "STEER",
            "continue": "CONTINUE",
            "approve_tool": "APPROVE_TOOL",
            "reject_tool": "REJECT_TOOL",
        }
        kind = action_map.get(action)
        if kind is None:
            await self._send_event(connection, "error", detail=f"unknown action: {action!r}")
            return

        payload = envelope.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}

        try:
            result = await self._submit_control(
                task_id=task_id,
                kind=kind,
                payload=payload,
                requested_by=client_id,
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("task_control failed: {}", exc)
            await self._send_event(connection, "error", detail="task_control_failed")
            return

        status = getattr(result, "status", "ERROR")
        await self._send_event(
            connection,
            "error" if status == "TASK_NOT_FOUND" else "session_updated",
            chat_id=envelope.get("chat_id"),
        )

    async def _workspace_scope_or_error(
        self, connection: Any, resolver: Callable[[], Any], *, chat_id: str | None = None
    ) -> Any | None:
        try:
            return resolver()
        except WorkspaceScopeError as exc:
            await self._send_event(
                connection,
                "error",
                detail="workspace_scope_rejected",
                reason=exc.message,
                **({"chat_id": chat_id} if chat_id else {}),
            )
            return None

    # -- Outbound WebSocket events -----------------------------------------

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._stop_event:
            self._stop_event.set()
        if self._periodic_cleanup_task is not None:
            self._periodic_cleanup_task.cancel()
            with suppress(Exception):
                await self._periodic_cleanup_task
            self._periodic_cleanup_task = None
        if self._server_task:
            try:
                await self._server_task
            except Exception as e:
                self.logger.warning("server task error during shutdown: {}", e)
            self._server_task = None
        self._subs.clear()
        self._conn_chats.clear()
        self._conn_default.clear()
        self._issued_tokens.clear()
        self._api_tokens.clear()
        self._outbound.clear_buffers()

    async def _safe_send_to(self, connection: Any, raw: str, *, label: str = "") -> None:
        """Send a raw frame to one connection, cleaning up on ConnectionClosed."""
        try:
            await connection.send(raw)
        except ConnectionClosed:
            self._cleanup_connection(connection)
            self.logger.warning("connection gone{}", label)
        except Exception:
            self.logger.exception("send failed{}", label)
            raise

    async def _send_agent_event(
        self,
        event: AgentEvent,
        *,
        connections: list[Any] | None = None,
        label: str = "",
        persist: bool = False,
    ) -> None:
        await self._outbound.send_agent_event(
            event, connections=connections, label=label, persist=persist
        )

    async def send(self, msg: OutboundMessage) -> None:
        await self._outbound.dispatch_outbound(msg)

    async def send_reasoning_delta(
        self,
        chat_id: str,
        delta: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await self._outbound.send_reasoning_delta(chat_id, delta, metadata)

    async def send_reasoning_end(
        self,
        chat_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await self._outbound.send_reasoning_end(chat_id, metadata)

    async def send_delta(
        self,
        chat_id: str,
        delta: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await self._outbound.send_delta(chat_id, delta, metadata)

    async def send_turn_end(
        self,
        chat_id: str,
        latency_ms: int | None = None,
        *,
        goal_state: dict[str, Any] | None = None,
        context_usage: dict[str, Any] | None = None,
    ) -> None:
        await self._outbound.send_turn_end(
            chat_id, latency_ms, goal_state=goal_state, context_usage=context_usage
        )

    async def send_goal_state(self, chat_id: str, blob: dict[str, Any]) -> None:
        await self._outbound.send_goal_state(chat_id, blob)

    async def send_goal_status(
        self,
        chat_id: str,
        status: str,
        *,
        started_at: float | None = None,
    ) -> None:
        await self._outbound.send_goal_status(chat_id, status, started_at=started_at)

    async def send_session_updated(self, chat_id: str, *, scope: str | None = None) -> None:
        await self._outbound.send_session_updated(chat_id, scope=scope)

    async def send_runtime_model_updated(
        self,
        *,
        model_name: Any,
        model_preset: Any = None,
    ) -> None:
        await self._outbound.send_runtime_model_updated(
            model_name=model_name, model_preset=model_preset
        )
