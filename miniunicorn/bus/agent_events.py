"""Typed protocol for WebSocket server events.

This module is the single source of truth for every event the backend emits
to WebSocket clients (and, via the JSON Schema export, to the frontend's
TypeScript types). Every outbound frame is constructed as one of the
``AgentEvent`` models and serialized through :func:`serialize_agent_event`,
which guarantees:

* ``protocol_version`` is always present (currently ``1``);
* unknown fields are rejected (``extra="forbid"``);
* the ``event`` discriminator selects the concrete model;
* optional fields that are ``None`` are dropped from the wire payload.

Compatibility note: legacy ``OutboundMessage.metadata`` flags (``_progress``,
``_turn_end``, ``_goal_state``, …) remain supported for one release so
non-WebUI channels and older tests keep working. New code should construct
the Pydantic models directly and attach them via
``OUTBOUND_META_AGENT_EVENT``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

PROTOCOL_VERSION: Literal[1] = 1
"""Current wire protocol version. Bumped only on breaking schema changes.

Additive optional fields do not increment the version; renames, removals, or
semantic changes to existing fields do, and require a migration period.
"""


class EventBase(BaseModel):
    """Shared base for every server event.

    ``extra="forbid"`` keeps the wire contract tight: a typo in a field name
    fails validation instead of silently being dropped.
    """

    model_config = ConfigDict(extra="forbid")
    protocol_version: Literal[1] = PROTOCOL_VERSION


# ---------------------------------------------------------------------------
# Shared payloads (referenced by multiple events)
# ---------------------------------------------------------------------------


class ContextUsagePayload(BaseModel):
    """Token-usage snapshot for a completed turn."""

    model_config = ConfigDict(extra="forbid")
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    cached_tokens: int = Field(default=0, ge=0)


class GoalStatePayload(BaseModel):
    """Persisted sustained-goal snapshot replayed on reconnect."""

    model_config = ConfigDict(extra="forbid")
    active: bool
    ui_summary: str | None = None
    objective: str | None = None


class FileEditPayload(BaseModel):
    """One file-edit event emitted by editing tools."""

    model_config = ConfigDict(extra="forbid")
    version: int | None = None
    call_id: str
    tool: str
    path: str
    absolute_path: str | None = None
    phase: str | None = None
    added: int = Field(default=0, ge=0)
    deleted: int = Field(default=0, ge=0)
    approximate: bool | None = None
    status: Literal["editing", "done", "error"]
    operation: str | None = None
    binary: bool | None = None
    error: str | None = None
    pending: bool | None = None


class SandboxStatusPayload(BaseModel):
    """Sandbox enforcement snapshot attached to workspace scope events."""

    model_config = ConfigDict(extra="forbid")
    restrict_to_workspace: bool
    workspace_root: str
    level: str
    enforced: bool
    provider: str
    provider_label: str
    summary: str


class WorkspaceScopePayload(BaseModel):
    """Workspace scope attached to ``session_updated`` and ``attached`` events."""

    model_config = ConfigDict(extra="forbid")
    project_path: str
    project_name: str | None = None
    access_mode: Literal["restricted", "full"]
    restrict_to_workspace: bool | None = None
    sandbox_status: SandboxStatusPayload | None = None


class ToolProgressEvent(BaseModel):
    """One tool-call lifecycle breadcrumb.

    This is both a standalone event payload (embedded in ``MessageEvent``)
    and the shape persisted in the WebUI transcript for tool-call traces.
    ``version`` is fixed at ``1`` for this protocol generation.
    """

    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    phase: Literal["start", "end", "error"]
    call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: Any | None = None
    error: str | None = None
    files: list[Any] = Field(default_factory=list)
    embeds: list[Any] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Concrete events (discriminated on ``event``)
# ---------------------------------------------------------------------------


class ReadyEvent(EventBase):
    """Initial frame sent right after the WebSocket handshake completes."""

    event: Literal["ready"] = "ready"
    chat_id: str
    client_id: str


class AttachedEvent(EventBase):
    """Ack for ``new_chat`` / ``attach`` envelopes."""

    event: Literal["attached"] = "attached"
    chat_id: str
    request_id: str | None = None


class MessageEvent(EventBase):
    """Conversational assistant message (final or intermediate breadcrumb).

    ``kind`` disambiguates intermediate breadcrumbs (``tool_hint`` /
    ``progress`` / ``reasoning``) from final replies, which omit the field.
    """

    event: Literal["message"] = "message"
    chat_id: str
    text: str
    reply_to: str | None = None
    media: list[str] | None = None
    media_urls: list[dict[str, str]] | None = None
    tool_events: list[ToolProgressEvent] | None = None
    kind: Literal["tool_hint", "progress", "reasoning"] | None = None
    latency_ms: int | None = Field(default=None, ge=0)
    agent_ui: dict[str, Any] | None = None


class FileEditEvent(EventBase):
    """Batched file-edit notification (one or more edits)."""

    event: Literal["file_edit"] = "file_edit"
    chat_id: str
    edits: list[FileEditPayload]


class DeltaEvent(EventBase):
    """One streaming text chunk for the active assistant bubble."""

    event: Literal["delta"] = "delta"
    chat_id: str
    text: str
    stream_id: str | None = None


class StreamEndEvent(EventBase):
    """Close of a streaming text segment (final rewritten text optional)."""

    event: Literal["stream_end"] = "stream_end"
    chat_id: str
    stream_id: str | None = None
    text: str | None = None


class ReasoningDeltaEvent(EventBase):
    """One streaming chunk of model reasoning."""

    event: Literal["reasoning_delta"] = "reasoning_delta"
    chat_id: str
    text: str
    stream_id: str | None = None


class ReasoningEndEvent(EventBase):
    """Close of the current reasoning stream segment."""

    event: Literal["reasoning_end"] = "reasoning_end"
    chat_id: str
    stream_id: str | None = None


class RuntimeModelUpdatedEvent(EventBase):
    """Broadcast that the active model/preset changed at runtime."""

    event: Literal["runtime_model_updated"] = "runtime_model_updated"
    model_name: str
    model_preset: str | None = None


class TurnEndEvent(EventBase):
    """Signal that the agent fully finished processing the current turn."""

    event: Literal["turn_end"] = "turn_end"
    chat_id: str
    latency_ms: int | None = Field(default=None, ge=0)
    goal_state: GoalStatePayload | None = None
    context_usage: ContextUsagePayload | None = None


class GoalStatusEvent(EventBase):
    """Wall-clock hint that a turn started or finished."""

    event: Literal["goal_status"] = "goal_status"
    chat_id: str
    status: Literal["running", "idle"]
    started_at: float | None = None


class GoalStateEvent(EventBase):
    """Persisted goal-state snapshot pushed to freshly subscribed clients."""

    event: Literal["goal_state"] = "goal_state"
    chat_id: str
    goal_state: GoalStatePayload


class SessionUpdatedEvent(EventBase):
    """Notify clients that session metadata changed outside the main turn."""

    event: Literal["session_updated"] = "session_updated"
    chat_id: str
    scope: str | None = None
    workspace_scope: WorkspaceScopePayload | None = None


class SubagentActivityEvent(EventBase):
    """Subagent breadcrumb (tool call, reasoning, completion)."""

    event: Literal["subagent_activity"] = "subagent_activity"
    chat_id: str
    label: str | None = None
    task_id: str | None = None
    content: str


class ErrorEvent(EventBase):
    """Channel-level error frame (invalid envelope, media rejection, …)."""

    event: Literal["error"] = "error"
    chat_id: str | None = None
    detail: str | None = None
    reason: str | None = None


AgentEvent = Annotated[
    ReadyEvent
    | AttachedEvent
    | MessageEvent
    | FileEditEvent
    | DeltaEvent
    | StreamEndEvent
    | ReasoningDeltaEvent
    | ReasoningEndEvent
    | RuntimeModelUpdatedEvent
    | TurnEndEvent
    | GoalStatusEvent
    | GoalStateEvent
    | SessionUpdatedEvent
    | SubagentActivityEvent
    | ErrorEvent,
    Field(discriminator="event"),
]
"""Discriminated union of every server event, keyed on the ``event`` field.

Use ``AGENT_EVENT_ADAPTER.validate_python(...)`` to parse an unknown event
dictionary, and :func:`serialize_agent_event` to produce the wire payload.
"""

AGENT_EVENT_ADAPTER: TypeAdapter[AgentEvent] = TypeAdapter(AgentEvent)
"""Type adapter for parsing/serializing any ``AgentEvent`` variant."""


def serialize_agent_event(event: AgentEvent) -> dict[str, Any]:
    """Serialize an ``AgentEvent`` to a JSON-safe dictionary.

    ``None`` fields are excluded so the wire payload stays compact; this
    matches the historical shape produced by hand-built dictionaries and
    keeps ``protocol_version`` first. ``mode="json"`` ensures every value
    is JSON-safe (enums → strings, datetimes → ISO strings, etc.).
    """
    return AGENT_EVENT_ADAPTER.dump_python(event, mode="json", exclude_none=True)
