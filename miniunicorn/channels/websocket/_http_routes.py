"""HTTP route helpers for the WebSocket channel.

Pure helpers shared by the HTTP dispatch surface that runs beside the
WebSocket endpoint: response builders, request/path/query parsers,
bearer-token extraction, chunked-header reassembly, and the MCP-preset
action path map. None of these depend on the ``WebSocketChannel`` instance
state, so they live here to keep ``channel.py`` focused on the channel
class.
"""

from __future__ import annotations

import email.utils
import hmac
import http
import json
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from websockets.datastructures import Headers
from websockets.http11 import Request as WsRequest
from websockets.http11 import Response

from miniunicorn.channels.websocket._chunked_header import (  # noqa: F401 — re-exported for channel.py
    _collect_chunked_header,
    collect_chunked_header,
)
from miniunicorn.webui.settings_api import WebUISettingsError

# Path → action mapping for the MCP presets HTTP surface. Used by both
# the dispatcher and the per-action handler in ``channel.py``.
_MCP_PRESET_ACTIONS_BY_PATH = {
    "/api/settings/mcp-presets/enable": "enable",
    "/api/settings/mcp-presets/remove": "remove",
    "/api/settings/mcp-presets/test": "test",
    "/api/settings/mcp-presets/custom": "custom",
    "/api/settings/mcp-presets/import": "import",
    "/api/settings/mcp-presets/import-cursor": "import-cursor",
    "/api/settings/mcp-presets/tools": "tools",
}
_MCP_VALUES_HEADER = "x-miniunicorn-MCP-Values"
_MCP_VALUES_HEADER_MAX_BYTES = 64 * 1024

# 通用敏感字段 header:前端把 api_key / config / backends_json 等秘密放进这个
# JSON 对象 header,而不是 URL query,避免秘密进入日志/Referer/浏览器历史。
# 与 MCP 专用 header 平行,但面向所有需要携带秘密的路由(provider/web-search/
# channel/model-config 等)。支持分块({base}/{base}-1/...),总字节上限 256KB。
_SENSITIVE_VALUES_HEADER = "x-miniunicorn-Values"
_SENSITIVE_VALUES_HEADER_MAX_BYTES = 256 * 1024

# Skill ZIP 分块上传的 header 数量与总字节上限。前端在超过限制时提前报错,
# 不再产生服务端必然拒绝的请求(避免触发协议 header 数量上限)。
SKILL_ZIP_MAX_HEADERS = 200
SKILL_ZIP_MAX_TOTAL_BYTES = 32 * 1024 * 1024  # 32 MiB


class ChunkedHeaderLimitError(ValueError):
    """分块 header 超过数量或总字节上限时抛出。"""


def collect_chunked_header_limited(
    headers: Any,
    base_name: str,
    *,
    max_count: int,
    max_total_bytes: int,
) -> str:
    """与 :func:`collect_chunked_header` 相同,但强制数量与字节上限。

    超过上限抛出 :class:`ChunkedHeaderLimitError`,调用方应返回 413 给客户端。
    """
    parts: dict[int, str] = {}
    first = headers.get(base_name)
    if first:
        parts[0] = first
    for key, value in headers.raw_items():
        lower = key.lower()
        prefix = f"{base_name.lower()}-"
        if lower.startswith(prefix):
            suffix = key[len(base_name) + 1 :]
            try:
                idx = int(suffix)
            except ValueError:
                continue
            parts[idx] = value
    if not parts:
        return ""
    if len(parts) > max_count:
        raise ChunkedHeaderLimitError(
            f"too many {base_name} header chunks ({len(parts)} > {max_count})"
        )
    total = sum(len(v.encode("utf-8")) for v in parts.values())
    if total > max_total_bytes:
        raise ChunkedHeaderLimitError(
            f"{base_name} header payload too large ({total} > {max_total_bytes} bytes)"
        )
    return "".join(parts[i] for i in sorted(parts))


def _merge_sensitive_values_header(
    request: WsRequest, query: dict[str, list[str]]
) -> dict[str, list[str]]:
    """把 ``x-miniunicorn-Values`` chunked JSON header 合并进 *query*。

    - header 缺失或为空 → 原样返回 *query*(不复制,调用方不应修改)。
    - header 存在 → JSON 解码为对象,字符串值覆盖 query 中同名 key,非字符串值
      JSON 序列化后写入。超限或格式错误时忽略(不阻断请求,handler 仍可从 query
      读取非敏感字段)。
    """
    raw = collect_chunked_header_limited(
        request.headers,
        _SENSITIVE_VALUES_HEADER,
        max_count=256,
        max_total_bytes=_SENSITIVE_VALUES_HEADER_MAX_BYTES,
    )
    if not raw:
        return query
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return query
    if not isinstance(payload, dict):
        return query
    merged = {key: list(values) for key, values in query.items()}
    for key, value in payload.items():
        if not isinstance(key, str) or not key:
            continue
        if value is None:
            continue
        if isinstance(value, str):
            text = value
        else:
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        merged[key] = [text]
    return merged


def _human_readable_size(num_bytes: int) -> str:
    """把字节数格式化为人类可读的字符串(1024 进制)。"""
    if num_bytes < 0:
        return ""
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(num_bytes)
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024
        idx += 1
    if idx == 0:
        return f"{int(size)} {units[idx]}"
    return f"{size:.1f} {units[idx]}"


def _http_json_response(data: dict[str, Any], *, status: int = 200) -> Response:
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    headers = Headers(
        [
            ("Date", email.utils.formatdate(usegmt=True)),
            ("Connection", "close"),
            ("Content-Length", str(len(body))),
            ("Content-Type", "application/json; charset=utf-8"),
        ]
    )
    reason = http.HTTPStatus(status).phrase
    return Response(status, reason, headers, body)


def _http_response(
    body: bytes,
    *,
    status: int = 200,
    content_type: str = "text/plain; charset=utf-8",
    extra_headers: list[tuple[str, str]] | None = None,
) -> Response:
    headers = [
        ("Date", email.utils.formatdate(usegmt=True)),
        ("Connection", "close"),
        ("Content-Length", str(len(body))),
        ("Content-Type", content_type),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    reason = http.HTTPStatus(status).phrase
    return Response(status, reason, Headers(headers), body)


def _http_error(status: int, message: str | None = None) -> Response:
    body = (message or http.HTTPStatus(status).phrase).encode("utf-8")
    return _http_response(body, status=status)


def _bearer_token(headers: Any) -> str | None:
    """Pull a Bearer token out of standard or query-style headers."""
    auth = headers.get("Authorization") or headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return None


def _issue_route_secret_matches(headers: Any, configured_secret: str) -> bool:
    """Return True if the token-issue HTTP request carries credentials matching ``token_issue_secret``."""
    if not configured_secret:
        return True
    authorization = headers.get("Authorization") or headers.get("authorization")
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
        return hmac.compare_digest(supplied, configured_secret)
    header_token = headers.get("x-miniunicorn-Auth") or headers.get("x-miniunicorn-auth")
    if not header_token:
        return False
    return hmac.compare_digest(header_token.strip(), configured_secret)


def _parse_request_path(path_with_query: str) -> tuple[str, dict[str, list[str]]]:
    """Parse normalized path and query parameters in one pass."""
    parsed = urlparse("ws://x" + path_with_query)
    # Reuse the trailing-slash normalizer from the WS upgrade module so the
    # behavior stays identical for HTTP and WS path matching.
    from ._ws_upgrade import _strip_trailing_slash

    path = _strip_trailing_slash(parsed.path or "/")
    return path, parse_qs(parsed.query, keep_blank_values=True)


def _normalize_http_path(path_with_query: str) -> str:
    """Return the path component (no query string), with trailing slash normalized (root stays ``/``)."""
    return _parse_request_path(path_with_query)[0]


def _parse_query(path_with_query: str) -> dict[str, list[str]]:
    return _parse_request_path(path_with_query)[1]


def _parse_mcp_settings_query(request: WsRequest) -> dict[str, list[str]]:
    query = _parse_query(request.path)
    raw = request.headers.get(_MCP_VALUES_HEADER)
    if not raw:
        return query
    if len(raw.encode("utf-8")) > _MCP_VALUES_HEADER_MAX_BYTES:
        raise WebUISettingsError("MCP settings payload is too large")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WebUISettingsError("invalid MCP settings payload") from exc
    if not isinstance(payload, dict):
        raise WebUISettingsError("MCP settings payload must be a JSON object")
    merged = {key: list(values) for key, values in query.items()}
    for key, value in payload.items():
        if not isinstance(key, str) or not key:
            raise WebUISettingsError("MCP settings payload contains an invalid key")
        if value is None:
            continue
        if isinstance(value, str):
            text = value.strip()
        else:
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if text:
            merged[key] = [text]
    return merged


def _query_first(query: dict[str, list[str]], key: str) -> str | None:
    """Return the first value for *key*, or None."""
    values = query.get(key)
    return values[0] if values else None


# Matches the legacy chat-id pattern but allows file-system-safe stems too,
# so the API can address sessions whose keys came from non-WebSocket channels.
_API_KEY_RE = re.compile(r"^[A-Za-z0-9_:.-]{1,128}$")


def _decode_api_key(raw_key: str) -> str | None:
    """Decode a percent-encoded API path segment, then validate the result."""
    from urllib.parse import unquote

    key = unquote(raw_key)
    if _API_KEY_RE.match(key) is None:
        return None
    return key
