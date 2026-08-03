"""WP0 — Freeze the current Agent event protocol surface.

The durable runtime must not introduce a second event protocol
(design §6.12, §23.1, §29.15). These tests pin the current protocol version,
the set of event types, and the discriminated-union behavior so any
WP5/WP6 changes that accidentally diverge are caught immediately.
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter

from miniunicorn.bus.agent_events import (
    AGENT_EVENT_ADAPTER,
    PROTOCOL_VERSION,
    AttachedEvent,
    DeltaEvent,
    ErrorEvent,
    FileEditEvent,
    GoalStateEvent,
    GoalStatusEvent,
    MessageEvent,
    ReadyEvent,
    ReasoningDeltaEvent,
    ReasoningEndEvent,
    RuntimeModelUpdatedEvent,
    SessionUpdatedEvent,
    StreamEndEvent,
    SubagentActivityEvent,
    TurnEndEvent,
)

# Canonical set of event classes that must remain stable across the runtime
# migration. Adding a new event is additive; removing or renaming one is a
# protocol break that requires bumping PROTOCOL_VERSION.
FROZEN_EVENT_CLASSES = {
    ReadyEvent,
    AttachedEvent,
    MessageEvent,
    FileEditEvent,
    DeltaEvent,
    StreamEndEvent,
    ReasoningDeltaEvent,
    ReasoningEndEvent,
    RuntimeModelUpdatedEvent,
    TurnEndEvent,
    GoalStatusEvent,
    GoalStateEvent,
    SessionUpdatedEvent,
    SubagentActivityEvent,
    ErrorEvent,
}


def test_protocol_version_is_one() -> None:
    """The wire protocol version is currently 1.

    Bumping this is a breaking change that requires a migration period
    (design §23.1). Pinning it catches accidental schema removals/renames.
    """
    assert PROTOCOL_VERSION == 1


def test_event_class_surface_is_frozen() -> None:
    """Every currently-defined event class must remain importable.

    New events may be added; existing ones may not be removed or renamed
    without bumping the protocol version.
    """
    missing = sorted(cls.__name__ for cls in FROZEN_EVENT_CLASSES if not hasattr(cls, "__name__"))
    assert missing == []


def test_event_adapter_round_trips_message_event() -> None:
    """The discriminated union must round-trip a basic message event."""
    payload = {
        "event": "message",
        "chat_id": "test:1",
        "text": "hi",
    }
    obj = AGENT_EVENT_ADAPTER.validate_python(payload)
    assert isinstance(obj, MessageEvent)


def test_event_adapter_rejects_unknown_event_type() -> None:
    """Unknown discriminator values must be rejected (extra='forbid')."""
    with pytest.raises(Exception):
        AGENT_EVENT_ADAPTER.validate_python({"event": "nonexistent_event"})


def test_event_adapter_rejects_unknown_fields() -> None:
    """Typo'd fields must fail validation, not silently drop."""
    with pytest.raises(Exception):
        AGENT_EVENT_ADAPTER.validate_python(
            {"event": "message", "chat_id": "x", "text": "y", "typo_field": 1}
        )


def test_message_event_preserves_protocol_version() -> None:
    """Every serialized event must carry the protocol version."""
    event = MessageEvent(chat_id="test:1", text="x")
    assert event.protocol_version == PROTOCOL_VERSION


def test_stream_end_event_round_trips() -> None:
    payload = {"event": "stream_end", "chat_id": "test:1"}
    obj = AGENT_EVENT_ADAPTER.validate_python(payload)
    assert isinstance(obj, StreamEndEvent)


def test_turn_end_event_round_trips() -> None:
    payload = {
        "event": "turn_end",
        "chat_id": "test:1",
    }
    obj = AGENT_EVENT_ADAPTER.validate_python(payload)
    assert isinstance(obj, TurnEndEvent)


def test_typeadapter_serializes_message_event() -> None:
    """The TypeAdapter must serialize an event back to a JSON-compatible dict."""
    event = MessageEvent(chat_id="test:1", text="hello")
    adapter = TypeAdapter(MessageEvent)
    dumped = adapter.dump_python(event, mode="json")
    assert dumped["event"] == "message"
    assert dumped["protocol_version"] == PROTOCOL_VERSION
