"""Deterministic inbound and internal envelope construction (design Task 4).

This module owns the conversion from normalized ``RuntimeInboundRequest``
objects to durable ``InboundTaskEnvelope`` / ``InternalTaskEnvelope``
records. It also provides ``local_request_scope`` for deriving a stable
``RequestScope`` from the root ``Config``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from miniunicorn.runtime.models import (
    InboundTaskEnvelope,
    InternalTaskEnvelope,
    RequestScope,
)

try:
    from miniunicorn.agent.ports import TaskKind
except ImportError:  # pragma: no cover
    TaskKind = Any  # type: ignore[assignment,misc]


def build_inbound_envelope(
    request: Any,
    *,
    now_ms: int,
) -> InboundTaskEnvelope:
    """Convert a ``RuntimeInboundRequest`` to a durable ``InboundTaskEnvelope``.

    The payload is serialised as canonical JSON (sorted keys, no extra
    whitespace) so the hash is deterministic. The dedup key is
    ``{channel}:{session_key}:{channel_message_id}`` when a channel
    message id is present, otherwise ``None`` (CLI/API sessions that
    should not deduplicate).
    """
    payload = {
        "content": request.content,
        "media": list(request.media),
        "metadata": request.metadata,
    }
    payload_bytes = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    payload_hash = hashlib.sha256(payload_bytes).hexdigest()
    dedup_key = (
        f"{request.channel}:{request.session_key}:{request.channel_message_id}"
        if request.channel_message_id
        else None
    )
    return InboundTaskEnvelope(
        protocol_version=1,
        task_kind="USER_TURN",
        priority=100,
        scope=request.scope,
        session_key=request.session_key,
        channel=request.channel,
        channel_account=request.channel_account,
        channel_message_id=request.channel_message_id,
        dedup_key=dedup_key,
        normalized_payload_ref=f"inline:{payload_hash[:16]}",
        payload_hash=payload_hash,
        media_refs=(),
        received_at_ms=now_ms,
        turn_id=None,
        payload_content=payload_bytes,
    )


def build_internal_envelope(
    *,
    kind: Any,
    scope: RequestScope,
    session_key: str,
    dedup_key: str,
    payload: dict[str, Any],
    priority: int,
    now_ms: int,
) -> InternalTaskEnvelope:
    """Build a durable ``InternalTaskEnvelope`` for background work.

    Used by Dream, Cron, and maintenance triggers (design §13.1, Task 10).
    The payload is serialised as canonical JSON and embedded inline so
    ``submit_internal`` can write it as a blob without an external ref.
    """
    payload_bytes = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    payload_hash = hashlib.sha256(payload_bytes).hexdigest()
    return InternalTaskEnvelope(
        protocol_version=1,
        task_kind=kind,
        priority=priority,
        scope=scope,
        session_key=session_key,
        dedup_key=dedup_key,
        normalized_payload_ref=f"inline:{payload_hash[:16]}",
        payload_hash=payload_hash,
        received_at_ms=now_ms,
        payload_content=payload_bytes,
    )


def local_request_scope(
    config: Any,
    principal_id: str = "local-user",
) -> RequestScope:
    """Derive a stable ``RequestScope`` from the root ``Config``.

    The ``workspace_id`` is the first 16 hex chars of the SHA-256 of the
    resolved workspace path, so the same workspace always maps to the
    same scope across restarts.
    """
    workspace = str(config.workspace_path.resolve())
    workspace_id = hashlib.sha256(workspace.encode("utf-8")).hexdigest()[:16]
    return RequestScope(
        tenant_id="local",
        principal_id=principal_id,
        agent_id="default",
        workspace_id=workspace_id,
    )


__all__ = [
    "build_inbound_envelope",
    "build_internal_envelope",
    "local_request_scope",
]
