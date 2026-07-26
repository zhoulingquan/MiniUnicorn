"""Media-signing & upload-MIME helpers for the WebSocket channel.

Pure constants and helpers used by the WebSocket channel when validating
inbound media envelopes and signing outbound media fetch URLs:

- Per-message attachment caps and byte limits.
- MIME whitelists for images / videos / documents (mirrors the Composer
  accept list and ``utils.document.SUPPORTED_EXTENSIONS``).
- ``_extract_data_url_mime``: parses the MIME prefix off a ``data:`` URL.

These have no dependency on the ``WebSocketChannel`` instance, so they
live here to keep ``channel.py`` focused on the channel class itself.
"""

from __future__ import annotations

import re

# Per-message media limits. The server-side guard is a touch looser than the
# client's ``Worker`` normalization target (6 MB) — tolerate client slop, but
# still cap total ingress at ``_MAX_IMAGES_PER_MESSAGE * _MAX_IMAGE_BYTES``
# which fits comfortably inside ``max_message_bytes``.
_MAX_IMAGES_PER_MESSAGE = 4
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_MAX_VIDEOS_PER_MESSAGE = 1
_MAX_VIDEO_BYTES = 20 * 1024 * 1024
# Documents share the 4-attachment per-message cap with images (the client
# treats all non-video attachments as one pool). 50 MB aligns with
# ``utils.document._MAX_EXTRACT_FILE_SIZE`` so anything the client accepts
# can be fully extracted server-side.
_MAX_DOCUMENT_BYTES = 50 * 1024 * 1024

# Image MIME whitelist — matches the Composer's ``accept`` list. SVG is
# explicitly excluded to avoid the XSS surface inside embedded scripts.
_IMAGE_MIME_ALLOWED: frozenset[str] = frozenset({
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
})

_VIDEO_MIME_ALLOWED: frozenset[str] = frozenset({
    "video/mp4",
    "video/webm",
    "video/quicktime",
})

# Document MIME whitelist — mirrors ``utils.document.SUPPORTED_EXTENSIONS``.
# These files are base64-encoded client-side (bypassing the image Worker's
# magic-byte check) and have their text extracted by ``extract_documents()``.
# ``application/octet-stream`` is admitted because browsers return it for
# .log/.toml/.ini/.cfg and other text formats; the original filename
# extension is preserved via ``filename_hint`` for downstream parsing.
_DOCUMENT_MIME_ALLOWED: frozenset[str] = frozenset({
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "text/plain",
    "text/markdown",
    "text/csv",
    "application/json",
    "application/xml",
    "text/xml",
    "text/html",
    "application/x-yaml",
    "text/yaml",
    "application/octet-stream",
})

_UPLOAD_MIME_ALLOWED: frozenset[str] = (
    _IMAGE_MIME_ALLOWED | _VIDEO_MIME_ALLOWED | _DOCUMENT_MIME_ALLOWED
)

_DATA_URL_MIME_RE = re.compile(r"^data:([^;]+);base64,", re.DOTALL)


def _extract_data_url_mime(url: str) -> str | None:
    """Return the MIME type of a ``data:<mime>;base64,...`` URL, else ``None``."""
    if not isinstance(url, str):
        return None
    m = _DATA_URL_MIME_RE.match(url)
    if not m:
        return None
    return m.group(1).strip().lower() or None
