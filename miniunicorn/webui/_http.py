"""Dependency-neutral HTTP response and header helpers for the WebUI surface.

Provides the canonical implementations of the small HTTP response builders,
error response helper, and case-insensitive header lookup used by both the
WebSocket channel HTTP routes (``channels/websocket/_http_routes.py``) and
the signed media API (``webui/media_api.py``).

This module depends only on the ``websockets`` library and the standard
library; it does not import from any media or WebSocket module.
"""

from __future__ import annotations

import email.utils
import http
import json
from typing import Any

from websockets.datastructures import Headers
from websockets.http11 import Response


def http_json_response(data: dict[str, Any], *, status: int = 200) -> Response:
    """Build a JSON HTTP response with the standard header set."""
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


def http_response(
    body: bytes,
    *,
    status: int = 200,
    content_type: str = "text/plain; charset=utf-8",
    extra_headers: list[tuple[str, str]] | None = None,
) -> Response:
    """Build a plain HTTP response with the standard header set.

    Header order is: ``Date``, ``Connection``, ``Content-Length``,
    ``Content-Type``, then any ``extra_headers``.
    """
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


def http_error(status: int, message: str | None = None) -> Response:
    """Build a plain-text error response."""
    body = (message or http.HTTPStatus(status).phrase).encode("utf-8")
    return http_response(body, status=status)


def case_insensitive_header(headers: Any, key: str) -> str:
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


# Backward-compatible aliases — historical call sites used the leading-underscore
# names. Both names refer to the same implementations.
_http_json_response = http_json_response
_http_response = http_response
_http_error = http_error
_case_insensitive_header = case_insensitive_header
