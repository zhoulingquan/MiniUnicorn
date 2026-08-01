"""Event types for the message bus."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# Optional ``OutboundMessage.metadata`` key for structured, channel-agnostic UI
# payloads. Value is JSON-serializable with at least ``kind``; rich clients may
# render it and other channels may ignore unknown keys.
OUTBOUND_META_AGENT_UI = "_agent_ui"

# Optional ``OutboundMessage.metadata`` key for a fully typed, serialized
# ``AgentEvent`` payload (see ``miniunicorn.bus.agent_events``). Value is a
# dict produced by ``serialize_agent_event``; typed-aware channels (currently
# the WebSocket server) validate and forward it as-is, while non-WebUI
# channels may ignore it. Coexists with the legacy ``_turn_end`` /
# ``_progress`` / ``_goal_state`` / … flags for one compatibility release.
OUTBOUND_META_AGENT_EVENT = "_agent_event"

# Internal-only inbound metadata used by in-process channels to ask the agent
# loop to update runtime state without going through a user session.
INBOUND_META_RUNTIME_CONTROL = "_runtime_control"
RUNTIME_CONTROL_ACK = "_ack"
RUNTIME_CONTROL_MCP_RELOAD = "mcp_reload"

# Separator between the base session key (``channel:chat_id``) and an optional
# agent namespace. Subagents append ``#sub:<task_id>`` so their consolidation
# history is isolated from the parent session. The base key is recoverable via
# ``session_key.split("#", 1)[0]`` for any consumer that needs the parent scope.
SESSION_KEY_NAMESPACE_SEP = "#"


def make_session_key(
    channel: str,
    chat_id: str,
    agent_id: str | None = None,
) -> str:
    """Build a session key, optionally namespaced by ``agent_id``.

    Without ``agent_id`` this returns the legacy ``{channel}:{chat_id}`` form
    (back-compat with all existing session keys). With ``agent_id`` it returns
    ``{channel}:{chat_id}#{agent_id}`` so a subagent's consolidation history,
    pending queue, and other session-scoped state stay isolated from the
    parent while remaining derivable from the parent key
    (``session_key.split("#", 1)[0]`` yields the base).
    """
    base = f"{channel}:{chat_id}"
    if agent_id:
        return f"{base}{SESSION_KEY_NAMESPACE_SEP}{agent_id}"
    return base


def session_key_base(session_key: str) -> str:
    """Return the parent (base) portion of a possibly-namespaced session key."""
    return session_key.split(SESSION_KEY_NAMESPACE_SEP, 1)[0]


@dataclass
class InboundMessage:
    """Message received from a chat channel."""

    channel: str  # telegram, discord, slack, whatsapp
    sender_id: str  # User identifier
    chat_id: str  # Chat/channel identifier
    content: str  # Message text
    # 使用 UTC 时间戳,避免跨时区部署时时间不一致(例如容器时区与宿主机不同)。
    # 旧代码用 ``datetime.now()`` 返回 naive datetime,在序列化/比较时可能引发歧义。
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    media: list[str] = field(default_factory=list)  # Media URLs
    metadata: dict[str, Any] = field(default_factory=dict)  # Channel-specific data
    session_key_override: str | None = None  # Optional override for thread-scoped sessions

    @property
    def session_key(self) -> str:
        """Unique key for session identification."""
        return self.session_key_override or f"{self.channel}:{self.chat_id}"


@dataclass
class OutboundMessage:
    """Message to send to a chat channel.

    ``metadata`` can carry routing (``message_id``, …), trace flags (``_progress``),
    and optional ``OUTBOUND_META_AGENT_UI`` blobs for rich clients; non-WebUI
    channels may ignore unknown keys.
    """

    channel: str
    chat_id: str
    content: str
    reply_to: str | None = None
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    buttons: list[list[str]] = field(default_factory=list)
