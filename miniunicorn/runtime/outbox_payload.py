"""Versioned Outbox payload codec (Task 7 Step 5).

Both ``FINAL_REPLY`` and ``MESSAGE_TOOL`` Outbox rows store their
content as a protected ``runtime_blobs`` row whose ``inline_content``
is the bytes produced by :func:`encode_outbox_payload`. The codec
uses a versioned JSON envelope so future revisions can add fields
without breaking decode of existing rows:

.. code-block:: json

   {"version":1,"content":"final answer","media":[],"metadata":{}}

For legacy ``FINAL_REPLY`` rows that were written before this codec
existed (raw UTF-8 bytes), :func:`decode_outbox_payload` falls back to
treating the bytes as the literal reply content so historical replies
remain readable. New rows must always use the codec.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(slots=True, frozen=True)
class DecodedOutboxPayload:
    """Decoded Outbox payload content (Task 7 Step 5)."""

    content: str
    media: tuple[str, ...]
    metadata: dict[str, Any]


def encode_outbox_payload(
    *,
    content: str,
    media: tuple[str, ...] | list[str] = (),
    metadata: Mapping[str, Any] | None = None,
) -> bytes:
    """Encode Outbox payload as a versioned JSON envelope (Task 7 Step 5).

    The envelope is canonical JSON (sorted keys, no extra whitespace) so
    the stored bytes are deterministic and the payload hash is stable.
    """
    payload = {
        "version": 1,
        "content": content,
        "media": list(media),
        "metadata": dict(metadata or {}),
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def decode_outbox_payload(message_kind: str, raw: bytes) -> DecodedOutboxPayload:
    """Decode Outbox payload bytes (Task 7 Step 5).

    ``message_kind`` is the Outbox row kind (``FINAL_REPLY`` or
    ``MESSAGE_TOOL``). For ``FINAL_REPLY`` rows whose bytes are not a
    valid versioned envelope (legacy raw-text blobs), the raw UTF-8
    text is returned as ``content`` with empty media/metadata so
    historical replies remain readable. For other kinds, a malformed
    payload raises :class:`ValueError` rather than silently degrading.
    """
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        if message_kind == "FINAL_REPLY":
            return DecodedOutboxPayload(
                content=raw.decode("utf-8", errors="replace"),
                media=(),
                metadata={},
            )
        raise ValueError(f"invalid {message_kind} outbox payload")
    if not isinstance(value, dict) or value.get("version") != 1:
        if message_kind == "FINAL_REPLY":
            return DecodedOutboxPayload(
                content=raw.decode("utf-8", errors="replace"),
                media=(),
                metadata={},
            )
        raise ValueError(f"unsupported {message_kind} outbox payload")
    return DecodedOutboxPayload(
        content=str(value.get("content", "")),
        media=tuple(str(item) for item in value.get("media", [])),
        metadata=dict(value.get("metadata") or {}),
    )


__all__ = [
    "DecodedOutboxPayload",
    "encode_outbox_payload",
    "decode_outbox_payload",
]
