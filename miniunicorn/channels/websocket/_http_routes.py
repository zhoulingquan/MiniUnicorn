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
