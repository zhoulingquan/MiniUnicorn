"""Shared helpers for decoding ``data:...;base64,...`` URLs to disk.

Historically lived in ``erza.api.server``; now shared by the WebSocket
channel so the ``api`` + ``websocket`` ingress paths apply the same parsing,
size guard, and filesystem layout.
"""

from __future__ import annotations

import base64
import mimetypes
import re
import uuid
from pathlib import Path

from erza.utils.helpers import safe_filename

DEFAULT_MAX_BYTES = 10 * 1024 * 1024
MAX_FILE_SIZE = DEFAULT_MAX_BYTES

_DATA_URL_RE = re.compile(r"^data:([^;]+);base64,(.+)$", re.DOTALL)


class FileSizeExceededError(Exception):
    """Raised when a decoded payload exceeds the caller's size limit."""


def save_base64_data_url(
    data_url: str,
    media_dir: Path,
    *,
    max_bytes: int | None = None,
    filename_hint: str | None = None,
) -> str | None:
    """Decode a ``data:<mime>;base64,<payload>`` URL and persist it.

    Returns the absolute path on success, ``None`` when the URL shape or the
    base64 payload itself is malformed. Raises :class:`FileSizeExceededError`
    when the decoded payload is larger than ``max_bytes`` (default 10 MB).

    When ``filename_hint`` is provided and the MIME-derived extension is
    ``.bin`` (e.g. ``application/octet-stream`` for .log/.toml/.ini/.cfg),
    the original filename's extension is used instead so downstream
    ``extract_documents()`` can recognize the document type.
    """
    m = _DATA_URL_RE.match(data_url)
    if not m:
        return None
    mime_type, b64_payload = m.group(1), m.group(2)
    try:
        raw = base64.b64decode(b64_payload)
    except Exception:
        return None
    limit = DEFAULT_MAX_BYTES if max_bytes is None else max_bytes
    if len(raw) > limit:
        raise FileSizeExceededError(f"File exceeds {limit // (1024 * 1024)}MB limit")
    ext = mimetypes.guess_extension(mime_type) or ".bin"
    # 当 MIME 推断出 .bin 时(如 application/octet-stream),回退到原始文件名扩展名,
    # 确保文档解析器能按扩展名识别文件类型
    if ext == ".bin" and filename_hint:
        hint_ext = Path(filename_hint).suffix
        if hint_ext:
            ext = hint_ext.lower()
    filename = f"{uuid.uuid4().hex[:12]}{ext}"
    dest = media_dir / safe_filename(filename)
    dest.write_bytes(raw)
    return str(dest)
