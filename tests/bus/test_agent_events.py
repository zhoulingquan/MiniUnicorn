"""Tests for the typed agent event protocol (Pydantic models).

The protocol is the source of truth for every WebSocket server event. These
tests pin the wire shape, validation rules, and discriminated-union behavior
so the frontend can rely on a stable contract.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from miniunicorn.bus.agent_events import (
    AGENT_EVENT_ADAPTER,
    PROTOCOL_VERSION,
    ContextUsagePayload,
    DeltaEvent,
    ErrorEvent,
    FileEditEvent,
    FileEditPayload,
    GoalStateEvent,
    GoalStatePayload,
    GoalStatusEvent,
    MessageEvent,
    ReadyEvent,
    ReasoningDeltaEvent,
    ReasoningEndEvent,
    RuntimeModelUpdatedEvent,
    SessionUpdatedEvent,
    StreamEndEvent,
    SubagentActivityEvent,
    ToolProgressEvent,
    TurnEndEvent,
    serialize_agent_event,
)


def test_protocol_version_is_one() -> None:
    assert PROTOCOL_VERSION == 1


def test_message_event_serializes_existing_wire_shape() -> None:
    event = MessageEvent(chat_id="chat-1", text="hello", kind="progress")
    assert serialize_agent_event(event) == {
        "protocol_version": 1,
        "event": "message",
        "chat_id": "chat-1",
        "text": "hello",
        "kind": "progress",
    }


def test_message_event_includes_optional_fields_when_present() -> None:
    event = MessageEvent(
        chat_id="chat-1",
        text="hi",
        reply_to="m-1",
        media=["a.png"],
        media_urls=[{"url": "/api/media/x"}],
        tool_events=[ToolProgressEvent(phase="start", call_id="c1", name="exec")],
        kind="tool_hint",
        latency_ms=42,
        agent_ui={"kind": "card"},
    )
    payload = serialize_agent_event(event)
    assert payload["reply_to"] == "m-1"
    assert payload["media"] == ["a.png"]
    assert payload["media_urls"] == [{"url": "/api/media/x"}]
    assert payload["tool_events"][0]["call_id"] == "c1"
    assert payload["kind"] == "tool_hint"
    assert payload["latency_ms"] == 42
    assert payload["agent_ui"] == {"kind": "card"}


def test_message_event_excludes_none_optionals() -> None:
    event = MessageEvent(chat_id="chat-1", text="hi")
    payload = serialize_agent_event(event)
    assert "reply_to" not in payload
    assert "media" not in payload
    assert "media_urls" not in payload
    assert "tool_events" not in payload
    assert "kind" not in payload
    assert "latency_ms" not in payload
    assert "agent_ui" not in payload


def test_message_event_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        MessageEvent(chat_id="chat-1", text="hi", kind="bogus")


def test_turn_end_rejects_negative_latency() -> None:
    with pytest.raises(ValidationError):
        TurnEndEvent(chat_id="chat-1", latency_ms=-1)


def test_turn_end_serializes_with_context_usage() -> None:
    event = TurnEndEvent(
        chat_id="chat-1",
        latency_ms=120,
        context_usage=ContextUsagePayload(
            prompt_tokens=12,
            completion_tokens=3,
            total_tokens=15,
            cached_tokens=0,
        ),
    )
    payload = serialize_agent_event(event)
    assert payload == {
        "protocol_version": 1,
        "event": "turn_end",
        "chat_id": "chat-1",
        "latency_ms": 120,
        "context_usage": {
            "prompt_tokens": 12,
            "completion_tokens": 3,
            "total_tokens": 15,
            "cached_tokens": 0,
        },
    }


def test_context_usage_rejects_negative_tokens() -> None:
    with pytest.raises(ValidationError):
        ContextUsagePayload(prompt_tokens=-1)


def test_discriminated_union_rejects_unknown_event() -> None:
    with pytest.raises(ValidationError):
        AGENT_EVENT_ADAPTER.validate_python({"protocol_version": 1, "event": "unknown"})


def test_discriminated_union_accepts_default_protocol_version() -> None:
    """``protocol_version`` defaults to 1, so omitting it is valid."""
    validated = AGENT_EVENT_ADAPTER.validate_python(
        {"event": "message", "chat_id": "c", "text": "t"}
    )
    assert isinstance(validated, MessageEvent)
    assert validated.protocol_version == 1


def test_discriminated_union_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        MessageEvent(chat_id="c", text="t", bogus=True)  # type: ignore[call-arg]


def test_tool_progress_event_preserves_version_one_payload() -> None:
    payload = ToolProgressEvent(
        phase="start",
        call_id="call-1",
        name="exec",
        arguments={"command": "pwd"},
    )
    assert payload.version == 1
    assert payload.files == []
    assert payload.embeds == []


def test_tool_progress_event_rejects_unknown_phase() -> None:
    with pytest.raises(ValidationError):
        ToolProgressEvent(phase="bogus", call_id="call-1", name="exec")


def test_ready_event_serializes() -> None:
    event = ReadyEvent(chat_id="chat-1", client_id="client-a")
    assert serialize_agent_event(event) == {
        "protocol_version": 1,
        "event": "ready",
        "chat_id": "chat-1",
        "client_id": "client-a",
    }


def test_delta_and_stream_end_round_trip() -> None:
    delta = DeltaEvent(chat_id="c", text="chunk", stream_id="s1")
    end = StreamEndEvent(chat_id="c", stream_id="s1", text="full")
    assert serialize_agent_event(delta)["event"] == "delta"
    assert serialize_agent_event(end)["event"] == "stream_end"


def test_reasoning_events_serialize() -> None:
    d = ReasoningDeltaEvent(chat_id="c", text="think", stream_id="s1")
    e = ReasoningEndEvent(chat_id="c", stream_id="s1")
    assert serialize_agent_event(d)["event"] == "reasoning_delta"
    assert serialize_agent_event(e)["event"] == "reasoning_end"


def test_runtime_model_updated_serializes() -> None:
    event = RuntimeModelUpdatedEvent(model_name="gpt-x", model_preset="default")
    payload = serialize_agent_event(event)
    assert payload["event"] == "runtime_model_updated"
    assert payload["model_name"] == "gpt-x"
    assert payload["model_preset"] == "default"


def test_goal_state_and_status_events_serialize() -> None:
    gs = GoalStateEvent(
        chat_id="c",
        goal_state=GoalStatePayload(active=True, ui_summary="running"),
    )
    status = GoalStatusEvent(chat_id="c", status="running", started_at=1.5)
    assert serialize_agent_event(gs)["event"] == "goal_state"
    assert serialize_agent_event(status)["status"] == "running"


def test_session_updated_event_serializes() -> None:
    event = SessionUpdatedEvent(chat_id="c", scope="metadata")
    payload = serialize_agent_event(event)
    assert payload["scope"] == "metadata"


def test_subagent_activity_event_serializes() -> None:
    event = SubagentActivityEvent(chat_id="c", label="sub-a", task_id="t1", content="working")
    payload = serialize_agent_event(event)
    assert payload["event"] == "subagent_activity"
    assert payload["label"] == "sub-a"
    assert payload["task_id"] == "t1"


def test_error_event_serializes_with_optional_fields() -> None:
    event = ErrorEvent(chat_id="c", detail="bad", reason="malformed")
    payload = serialize_agent_event(event)
    assert payload["event"] == "error"
    assert payload["detail"] == "bad"
    assert payload["reason"] == "malformed"


def test_file_edit_event_serializes() -> None:
    event = FileEditEvent(
        chat_id="c",
        edits=[
            FileEditPayload(
                call_id="call-1",
                tool="edit",
                path="foo.py",
                status="done",
                added=3,
                deleted=1,
            )
        ],
    )
    payload = serialize_agent_event(event)
    assert payload["event"] == "file_edit"
    assert payload["edits"][0]["path"] == "foo.py"
    assert payload["edits"][0]["added"] == 3


def test_file_edit_payload_rejects_negative_added() -> None:
    with pytest.raises(ValidationError):
        FileEditPayload(call_id="c", tool="t", path="p", status="done", added=-1)


def test_file_edit_payload_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        FileEditPayload(call_id="c", tool="t", path="p", status="bogus")


def test_serialize_excludes_none_but_keeps_zero() -> None:
    event = TurnEndEvent(
        chat_id="c",
        context_usage=ContextUsagePayload(prompt_tokens=0, total_tokens=0),
    )
    payload = serialize_agent_event(event)
    assert payload["context_usage"]["prompt_tokens"] == 0
    assert payload["context_usage"]["total_tokens"] == 0
    # latency_ms is None — must be excluded.
    assert "latency_ms" not in payload
    assert "goal_state" not in payload
