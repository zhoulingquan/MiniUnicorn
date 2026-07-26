"""Session-management & inbound-envelope helpers for the WebSocket channel.

Pure helpers shared by the WebSocket channel:

- ``publish_runtime_model_update``: enqueue a runtime model snapshot for
  fan-out to websocket subscribers (re-exported from the package for
  callers like ``miniunicorn.cli.commands``).
- ``_is_valid_chat_id`` / ``_parse_envelope``: validate inbound chat IDs
  and parse the new-style JSON envelopes (legacy frames fall through to
  ``_parse_inbound_payload``).
- ``_parse_inbound_payload``: parse a client frame into text content.

These have no dependency on the ``WebSocketChannel`` instance, so they
live here to keep ``channel.py`` focused on the channel class itself.
"""

from __future__ import annotations

import json
import re
from typing import Any

from loguru import logger

from miniunicorn.bus.events import OutboundMessage
from miniunicorn.bus.queue import MessageBus


def publish_runtime_model_update(
    bus: MessageBus,
    model: str,
    model_preset: str | None,
) -> None:
    """Enqueue a runtime model snapshot for websocket subscribers (fan-out in-channel)."""
    bus.outbound.put_nowait(OutboundMessage(
        channel="websocket",
        chat_id="*",
        content="",
        metadata={
            "_runtime_model_updated": True,
            "model": model,
            "model_preset": model_preset,
        },
    ))


def _parse_inbound_payload(raw: str) -> str | None:
    """Parse a client frame into text; return None for empty or unrecognized content."""
    text = raw.strip()
    if not text:
        return None
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(data, dict):
            for key in ("content", "text", "message"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value
            return None
        return None
    return text


# Accept UUIDs and short scoped keys like "unified:default". Keeps the capability
# namespace small enough to rule out path traversal / quote injection tricks.
_CHAT_ID_RE = re.compile(r"^[A-Za-z0-9_:-]{1,64}$")


def _is_valid_chat_id(value: Any) -> bool:
    return isinstance(value, str) and _CHAT_ID_RE.match(value) is not None


def _parse_envelope(raw: str) -> dict[str, Any] | None:
    """Return a typed envelope dict if the frame is a new-style JSON envelope, else None.

    A frame qualifies when it parses as a JSON object with a string ``type`` field.
    Legacy frames (plain text, or ``{"content": ...}`` without ``type``) return None;
    callers should fall back to :func:`_parse_inbound_payload` for those.
    """
    text = raw.strip()
    if not text.startswith("{"):
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    t = data.get("type")
    if not isinstance(t, str):
        return None
    return data


def _default_model_name_from_config() -> str | None:
    """Resolved model string from on-disk config (bootstrap fallback).

    Kept here (rather than in ``channel.py``) so the WebSocket channel
    module stays lean — but it is re-imported into ``channel.py`` so the
    test monkeypatches against ``miniunicorn.channels.websocket.channel``
    continue to take effect on the bootstrap resolver used there.
    """
    try:
        from miniunicorn.config.loader import load_config

        model = load_config().resolve_preset().model.strip()
        return model or None
    except Exception as e:
        logger.debug("bootstrap model_name could not load from config: {}", e)
        return None
