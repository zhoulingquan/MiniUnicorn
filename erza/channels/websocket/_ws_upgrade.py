"""WebSocket upgrade & connection-management helpers.

Holds:
- ``WebSocketConfig``: pydantic config for the WS server channel.
- Path/headers helpers used during the WS handshake.
- ``_RateLimiter``: sliding-window per-IP limiter used by the connection loop.
- localhost detection used by the bootstrap/upgrade paths.

These are pure helpers / data classes with no dependency on the
``WebSocketChannel`` instance, so they live here to keep ``channel.py``
focused on the channel class itself.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Self

from pydantic import Field, field_validator, model_validator

from erza.config.schema import Base


def _strip_trailing_slash(path: str) -> str:
    if len(path) > 1 and path.endswith("/"):
        return path.rstrip("/")
    return path or "/"


def _normalize_config_path(path: str) -> str:
    return _strip_trailing_slash(path)


def _case_insensitive_header(headers: Any, key: str) -> str:
    """Read a header from websockets/http test stubs without assuming casing."""
    try:
        value = headers.get(key)
    except Exception:
        value = None
    if value is None:
        try:
            value = headers.get(key.lower())
        except Exception:
            value = None
    return str(value or "").strip()


def _safe_host_header(value: str) -> str:
    """Return a safe Host header value, or empty when it should not be echoed."""
    value = value.strip()
    if not value:
        return ""
    if re.fullmatch(r"\[[0-9A-Fa-f:.]+\](?::\d{1,5})?", value):
        return value
    if re.fullmatch(r"[A-Za-z0-9.-]+(?::\d{1,5})?", value):
        return value
    return ""


def _host_for_url(host: str, port: int) -> str:
    host = host.strip()
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{host}:{port}"


class _RateLimiter:
    """Simple sliding-window rate limiter keyed by client identifier.

    Tracks request timestamps per key in a dict and rejects when the count
    inside the rolling window exceeds ``max_count``. Designed for the
    single-threaded asyncio model — no locking required.
    """

    def __init__(self, max_count: int, window_s: float):
        self._max_count = max_count
        self._window_s = window_s
        self._hits: dict[str, list[float]] = {}

    def check(self, key: str) -> bool:
        """Return True if the request is allowed, False if rate-limited."""
        now = time.monotonic()
        cutoff = now - self._window_s
        hits = [t for t in self._hits.get(key, []) if t > cutoff]
        if len(hits) >= self._max_count:
            self._hits[key] = hits
            return False
        hits.append(now)
        self._hits[key] = hits
        return True

    def cleanup(self) -> None:
        """Remove stale keys to prevent unbounded memory growth."""
        now = time.monotonic()
        cutoff = now - self._window_s
        for key in list(self._hits):
            self._hits[key] = [t for t in self._hits[key] if t > cutoff]
            if not self._hits[key]:
                del self._hits[key]


class WebSocketConfig(Base):
    """WebSocket server channel configuration.

    Clients connect with URLs like ``ws://{host}:{port}{path}?client_id=...&token=...``.
    - ``client_id``: Used for ``allow_from`` authorization; if omitted, a value is generated and logged.
    - ``token``: If non-empty, the ``token`` query param may match this static secret; short-lived tokens
      from ``token_issue_path`` are also accepted.
    - ``token_issue_path``: If non-empty, **GET** (HTTP/1.1) to this path returns JSON
      ``{"token": "...", "expires_in": <seconds>}``; use ``?token=...`` when opening the WebSocket.
      Must differ from ``path`` (the WS upgrade path). If the client runs in the **same process** as
      Erza and shares the asyncio loop, use a thread or async HTTP client for GET—do not call
      blocking ``urllib`` or synchronous ``httpx`` from inside a coroutine.
    - ``token_issue_secret``: If non-empty, token requests must send ``Authorization: Bearer <secret>`` or
      ``x-erza-Auth: <secret>``.
    - ``websocket_requires_token``: If True, the handshake must include a valid token (static or issued and not expired).
    - ``allow_origin``: 可选,允许通过 Origin 校验的额外来源列表(例如 ``https://app.example.com``)。
      默认放行 ``http(s)://127.0.0.1:<port>`` 与 ``http(s)://localhost:<port>``(本地 WebUI 同源场景)。
      非浏览器客户端(无 Origin 头,如 curl)始终放行以保持向后兼容。配置后会与默认列表合并生效。
      **公网/局域网部署警告**:默认白名单只覆盖本地回环,且无 Origin 的非浏览器客户端一律放行——
      暴露到非回环地址时必须同时配置 ``token_issue_secret``(或静态 ``token``),仅靠 Origin 校验
      不足以防护非浏览器客户端。
    - ``allow_query_token``: 是否接受 ``?token=`` 查询参数鉴权(向后兼容,默认开)。
      token 出现在 URL 中会被访问日志/Referer/浏览器历史泄漏,生产环境建议设为 ``False``,
      强制客户端改用 ``Authorization: Bearer <token>`` 头。
    - Each connection has its own session: a unique ``chat_id`` maps to the agent session internally.
    - ``media`` field in outbound messages contains local filesystem paths; remote clients need a
      shared filesystem or an HTTP file server to access these files.
    """

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8765
    unix_socket_path: str = ""
    path: str = "/"
    token: str = ""
    token_issue_path: str = ""
    token_issue_secret: str = ""
    token_ttl_s: int = Field(default=300, ge=30, le=86_400)
    websocket_requires_token: bool = True
    allow_from: list[str] = Field(default_factory=list)
    trusted_proxies: list[str] = Field(default_factory=list)
    # 允许的浏览器 Origin 列表(扩展默认的 localhost 同源放行)。
    allow_origin: list[str] = Field(default_factory=list)
    # 是否接受 ?token= 查询参数鉴权(向后兼容)。False 时强制 Authorization 头,
    # 避免 token 进入 URL 被日志/Referer/浏览器历史泄漏。
    allow_query_token: bool = True
    streaming: bool = True
    # Default 36 MB, upper 40 MB: supports up to 4 images at ~6 MB each after
    # client-side Worker normalization (see webui Composer). 4 × 6 MB × 1.37
    # (base64 overhead) + envelope framing stays under 36 MB; the 40 MB ceiling
    # leaves a small margin for sender slop without opening a DoS avenue.
    max_message_bytes: int = Field(default=37_748_736, ge=1024, le=41_943_040)
    ping_interval_s: float = Field(default=20.0, ge=5.0, le=300.0)
    ping_timeout_s: float = Field(default=20.0, ge=5.0, le=300.0)
    ssl_certfile: str = ""
    ssl_keyfile: str = ""

    @field_validator("unix_socket_path")
    @classmethod
    def unix_socket_path_format(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        if "\x00" in value:
            raise ValueError("unix_socket_path must not contain NUL bytes")
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError("unix_socket_path must be an absolute path")
        return str(path)

    @field_validator("path")
    @classmethod
    def path_must_start_with_slash(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError('path must start with "/"')
        return _normalize_config_path(value)

    @field_validator("token_issue_path")
    @classmethod
    def token_issue_path_format(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        if not value.startswith("/"):
            raise ValueError('token_issue_path must start with "/"')
        return _normalize_config_path(value)

    @model_validator(mode="after")
    def token_issue_path_differs_from_ws_path(self) -> Self:
        if not self.token_issue_path:
            return self
        if _normalize_config_path(self.token_issue_path) == _normalize_config_path(self.path):
            raise ValueError("token_issue_path must differ from path (the WebSocket upgrade path)")
        return self

    @model_validator(mode="after")
    def non_loopback_host_requires_auth(self) -> Self:
        host = self.host.strip().lower()
        if self.unix_socket_path:
            # Unix socket 由文件系统权限保护, 不经 TCP 暴露, host 不参与监听。
            return self
        if host in ("127.0.0.1", "::1", "localhost"):
            return self
        if self.token.strip() or self.token_issue_secret.strip():
            return self
        raise ValueError(
            f"host is {self.host} (non-loopback bind) but neither token nor "
            "token_issue_secret is set — set one to prevent unauthenticated access"
        )

    @model_validator(mode="after")
    def wildcard_host_forbids_star_allow_from(self) -> Self:
        if self.host in ("0.0.0.0", "::") and "*" in self.allow_from:
            raise ValueError(
                "host is 0.0.0.0 (all interfaces) but allow_from contains '*' — "
                "this would allow anyone to connect. Remove '*' and specify "
                "explicit client IDs."
            )
        return self


def _is_websocket_upgrade(request: Any) -> bool:
    """Detect an actual WS upgrade; plain HTTP GETs to the same path should fall through."""
    upgrade = request.headers.get("Upgrade") or request.headers.get("upgrade")
    connection = request.headers.get("Connection") or request.headers.get("connection")
    if not upgrade or "websocket" not in upgrade.lower():
        return False
    if not connection or "upgrade" not in connection.lower():
        return False
    return True


_LOCALHOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _is_localhost(connection: Any) -> bool:
    """Return True if *connection* originated from the loopback interface."""
    addr = getattr(connection, "remote_address", None)
    if not addr:
        return False
    host = addr[0] if isinstance(addr, tuple) else addr
    if not isinstance(host, str):
        return False
    # ``::ffff:127.0.0.1`` is loopback in IPv6-mapped form.
    if host.startswith("::ffff:"):
        host = host[7:]
    return host in _LOCALHOSTS
