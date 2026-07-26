"""Chunked HTTP header reassembly helper.

The websockets HTTP layer caps each header line at 8KB. The frontend splits
large payloads across ``{base_name}`` (first chunk) and ``{base_name}-1``,
``{base_name}-2``, ... headers. This helper reassembles them in order.

Previously this logic was duplicated in ``channels/websocket.py`` (now dead
code, shadowed by the ``channels/websocket/`` package) and
``channels/websocket/_http_routes.py``. It now lives here as the single
authoritative implementation.
"""

from __future__ import annotations

from typing import Any


def collect_chunked_header(headers: Any, base_name: str) -> str:
    """Concatenate a chunked header transmitted as repeated headers.

    The websockets HTTP layer caps each header line at 8KB. The frontend
    splits large payloads across ``{base_name}`` (first chunk) and
    ``{base_name}-1``, ``{base_name}-2``, ... headers. This helper reassembles
    them in order.
    """
    parts: dict[int, str] = {}
    first = headers.get(base_name)
    if first:
        parts[0] = first
    for key, value in headers.raw_items():
        lower = key.lower()
        prefix = f"{base_name.lower()}-"
        if lower.startswith(prefix):
            suffix = key[len(base_name) + 1:]
            try:
                idx = int(suffix)
            except ValueError:
                continue
            parts[idx] = value
    if not parts:
        return ""
    return "".join(parts[i] for i in sorted(parts))


# Backward-compatible alias — historical call sites used the leading-underscore
# name. Both names refer to the same implementation.
_collect_chunked_header = collect_chunked_header
