"""Tests for ``WebSocketOutboundEmitter`` — the outbound emission service
extracted from ``WebSocketChannel`` (Task 15).

These tests pin the exact wire JSON for every event type and verify that one
broken connection does not prevent fan-out to the remaining subscribers.
The emitter is constructed with explicit callable dependencies (no channel
state, no lifecycle, no HTTP routing) so it can be tested in isolation.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from miniunicorn.bus.agent_events import ReadyEvent
from miniunicorn.channels.websocket.outbound import WebSocketOutboundEmitter


class FakeConnection:
    """Minimal WebSocket connection stand-in that records every ``send()``."""

    def __init__(self, name: str = "conn") -> None:
        self.name = name
        self.sent: list[str] = []
        self.broken = False

    async def send(self, raw: str) -> None:
        if self.broken:
            raise ConnectionError(f"{self.name} is broken")
        self.sent.append(raw)


def _make_emitter(
    *,
    connections: list[FakeConnection] | None = None,
    rewrite: Any = None,
    log_calls: list[tuple[str, str]] | None = None,
) -> tuple[WebSocketOutboundEmitter, list[dict[str, Any]]]:
    """Build an emitter with fake dependencies.

    Returns ``(emitter, transcript)`` where *transcript* is the list of
    payloads appended via the ``append_transcript`` callback.
    """
    transcript: list[dict[str, Any]] = []
    log = log_calls if log_calls is not None else []
    conns = list(connections) if connections is not None else []

    async def safe_send(connection: Any, raw: str) -> None:
        try:
            await connection.send(raw)
        except Exception as exc:  # noqa: BLE001 — test helper logs every failure
            log.append((getattr(connection, "name", "?"), str(exc)))

    emitter = WebSocketOutboundEmitter(
        connections_for_chat=lambda cid: list(conns),
        safe_send=safe_send,
        rewrite_markdown_images=rewrite or (lambda text: text),
        append_transcript=lambda cid, payload: transcript.append(
            {"chat_id": cid, "payload": payload}
        ),
    )
    return emitter, transcript


def _parse(raw: str) -> dict[str, Any]:
    return json.loads(raw)


# -- send_message (answer) -----------------------------------------------


@pytest.mark.asyncio
async def test_send_message_emits_exact_json() -> None:
    conn = FakeConnection()
    emitter, transcript = _make_emitter(connections=[conn])

    await emitter.send_message(
        "chat-1",
        "hello world",
        reply_to="m1",
        media=["/tmp/a.png"],
        media_urls=[{"url": "/api/media/sig", "name": "a.png"}],
        kind="tool_hint",
        latency_ms=42,
    )

    assert len(conn.sent) == 1
    assert _parse(conn.sent[0]) == {
        "protocol_version": 1,
        "event": "message",
        "chat_id": "chat-1",
        "text": "hello world",
        "reply_to": "m1",
        "media": ["/tmp/a.png"],
        "media_urls": [{"url": "/api/media/sig", "name": "a.png"}],
        "kind": "tool_hint",
        "latency_ms": 42,
    }
    # Transcript persists the original (pre-rewrite) text.
    assert len(transcript) == 1
    assert transcript[0]["payload"]["event"] == "message"
    assert transcript[0]["payload"]["text"] == "hello world"


@pytest.mark.asyncio
async def test_send_message_rewrites_markdown_images() -> None:
    conn = FakeConnection()
    emitter, transcript = _make_emitter(
        connections=[conn],
        rewrite=lambda text: text.replace("diagram.png", "/api/media/signed"),
    )

    await emitter.send_message("chat-1", "![img](diagram.png)")

    payload = _parse(conn.sent[0])
    assert payload["text"] == "![img](/api/media/signed)"
    # Transcript keeps the original text.
    assert transcript[0]["payload"]["text"] == "![img](diagram.png)"


# -- send_reasoning_delta / send_reasoning_end ---------------------------


@pytest.mark.asyncio
async def test_send_reasoning_delta_emits_exact_json() -> None:
    conn = FakeConnection()
    emitter, _ = _make_emitter(connections=[conn])

    await emitter.send_reasoning_delta("chat-1", "thinking...", {"_stream_id": "r1"})

    assert _parse(conn.sent[0]) == {
        "protocol_version": 1,
        "event": "reasoning_delta",
        "chat_id": "chat-1",
        "text": "thinking...",
        "stream_id": "r1",
    }


@pytest.mark.asyncio
async def test_send_reasoning_delta_drops_empty_delta() -> None:
    conn = FakeConnection()
    emitter, _ = _make_emitter(connections=[conn])

    await emitter.send_reasoning_delta("chat-1", "", {"_stream_id": "r1"})

    assert conn.sent == []


@pytest.mark.asyncio
async def test_send_reasoning_end_emits_exact_json() -> None:
    conn = FakeConnection()
    emitter, _ = _make_emitter(connections=[conn])

    await emitter.send_reasoning_end("chat-1", {"_stream_id": "r1"})

    assert _parse(conn.sent[0]) == {
        "protocol_version": 1,
        "event": "reasoning_end",
        "chat_id": "chat-1",
        "stream_id": "r1",
    }


# -- send_delta (streaming) ----------------------------------------------


@pytest.mark.asyncio
async def test_send_delta_emits_delta_and_stream_end() -> None:
    conn = FakeConnection()
    emitter, _ = _make_emitter(connections=[conn])

    await emitter.send_delta("chat-1", "part", {"_stream_id": "sid"})
    await emitter.send_delta("chat-1", "", {"_stream_end": True, "_stream_id": "sid"})

    assert len(conn.sent) == 2
    first = _parse(conn.sent[0])
    second = _parse(conn.sent[1])
    assert first["event"] == "delta"
    assert first["text"] == "part"
    assert first["stream_id"] == "sid"
    assert second["event"] == "stream_end"
    assert second["stream_id"] == "sid"


@pytest.mark.asyncio
async def test_send_delta_stream_end_rewrites_buffered_text() -> None:
    conn = FakeConnection()
    emitter, _ = _make_emitter(
        connections=[conn],
        rewrite=lambda text: text.replace("img.png", "/api/media/r"),
    )

    await emitter.send_delta("chat-1", "![i](", {"_stream_id": "s"})
    await emitter.send_delta("chat-1", "img.png)", {"_stream_id": "s"})
    await emitter.send_delta("chat-1", "", {"_stream_end": True, "_stream_id": "s"})

    final = _parse(conn.sent[-1])
    assert final["event"] == "stream_end"
    assert final["text"] == "![i](/api/media/r)"


# -- send_turn_end -------------------------------------------------------


@pytest.mark.asyncio
async def test_send_turn_end_emits_exact_json() -> None:
    conn = FakeConnection()
    emitter, _ = _make_emitter(connections=[conn])

    await emitter.send_turn_end("chat-1", latency_ms=100)

    assert _parse(conn.sent[0]) == {
        "protocol_version": 1,
        "event": "turn_end",
        "chat_id": "chat-1",
        "latency_ms": 100,
    }


@pytest.mark.asyncio
async def test_send_turn_end_minimal() -> None:
    conn = FakeConnection()
    emitter, _ = _make_emitter(connections=[conn])

    await emitter.send_turn_end("chat-1")

    assert _parse(conn.sent[0]) == {
        "protocol_version": 1,
        "event": "turn_end",
        "chat_id": "chat-1",
    }


# -- goal state / status -------------------------------------------------


@pytest.mark.asyncio
async def test_send_goal_state_emits_exact_json() -> None:
    conn = FakeConnection()
    emitter, _ = _make_emitter(connections=[conn])

    await emitter.send_goal_state("chat-1", {"active": True, "ui_summary": "Working"})

    assert _parse(conn.sent[0]) == {
        "protocol_version": 1,
        "event": "goal_state",
        "chat_id": "chat-1",
        "goal_state": {"active": True, "ui_summary": "Working"},
    }


@pytest.mark.asyncio
async def test_send_goal_status_running_emits_exact_json() -> None:
    conn = FakeConnection()
    emitter, _ = _make_emitter(connections=[conn])

    await emitter.send_goal_status("chat-1", "running", started_at=1_700_000_000.5)

    assert _parse(conn.sent[0]) == {
        "protocol_version": 1,
        "event": "goal_status",
        "chat_id": "chat-1",
        "status": "running",
        "started_at": 1_700_000_000.5,
    }


@pytest.mark.asyncio
async def test_send_goal_status_idle_omits_started_at() -> None:
    conn = FakeConnection()
    emitter, _ = _make_emitter(connections=[conn])

    await emitter.send_goal_status("chat-1", "idle")

    assert _parse(conn.sent[0]) == {
        "protocol_version": 1,
        "event": "goal_status",
        "chat_id": "chat-1",
        "status": "idle",
    }


# -- session update ------------------------------------------------------


@pytest.mark.asyncio
async def test_send_session_updated_emits_exact_json() -> None:
    conn = FakeConnection()
    emitter, _ = _make_emitter(connections=[conn])

    await emitter.send_session_updated("chat-1", scope="metadata")

    assert _parse(conn.sent[0]) == {
        "protocol_version": 1,
        "event": "session_updated",
        "chat_id": "chat-1",
        "scope": "metadata",
    }


# -- runtime model update ------------------------------------------------


@pytest.mark.asyncio
async def test_send_runtime_model_updated_emits_exact_json() -> None:
    conn = FakeConnection()
    emitter, _ = _make_emitter(connections=[conn])

    await emitter.send_runtime_model_updated(model_name="gpt-4", model_preset="fast")

    assert _parse(conn.sent[0]) == {
        "protocol_version": 1,
        "event": "runtime_model_updated",
        "model_name": "gpt-4",
        "model_preset": "fast",
    }


@pytest.mark.asyncio
async def test_send_runtime_model_updated_skips_empty_name() -> None:
    conn = FakeConnection()
    emitter, _ = _make_emitter(connections=[conn])

    await emitter.send_runtime_model_updated(model_name="  ")

    assert conn.sent == []


# -- send_agent_event (generic) ------------------------------------------


@pytest.mark.asyncio
async def test_send_agent_event_fans_out_and_persists() -> None:
    conn = FakeConnection()
    emitter, transcript = _make_emitter(connections=[conn])

    event = ReadyEvent(chat_id="chat-1", client_id="c1")
    await emitter.send_agent_event(event, persist=True)

    payload = _parse(conn.sent[0])
    assert payload["event"] == "ready"
    assert payload["chat_id"] == "chat-1"
    assert payload["client_id"] == "c1"
    assert len(transcript) == 1
    assert transcript[0]["payload"]["event"] == "ready"


@pytest.mark.asyncio
async def test_send_agent_event_uses_connections_callback_when_none() -> None:
    conn = FakeConnection()
    emitter, _ = _make_emitter(connections=[conn])

    event = ReadyEvent(chat_id="chat-1", client_id="c1")
    await emitter.send_agent_event(event)  # connections=None → resolve via callback

    assert len(conn.sent) == 1


# -- broken connection does not prevent delivery -------------------------


@pytest.mark.asyncio
async def test_broken_connection_does_not_prevent_delivery() -> None:
    """One broken connection must not prevent delivery to the other.

    The ``safe_send`` callback catches the exception and logs it through the
    supplied logger callback, allowing the fan-out loop to continue.
    """
    log_calls: list[tuple[str, str]] = []
    conn_good = FakeConnection("good")
    conn_broken = FakeConnection("broken")
    conn_broken.broken = True

    emitter, _ = _make_emitter(
        connections=[conn_broken, conn_good],
        log_calls=log_calls,
    )

    await emitter.send_turn_end("chat-1")

    # Good connection still received the event.
    assert len(conn_good.sent) == 1
    assert _parse(conn_good.sent[0])["event"] == "turn_end"
    # Broken connection failure was logged.
    assert len(log_calls) == 1
    assert log_calls[0][0] == "broken"


@pytest.mark.asyncio
async def test_broken_connection_logged_for_send_message() -> None:
    log_calls: list[tuple[str, str]] = []
    conn_a = FakeConnection("a")
    conn_b = FakeConnection("b")
    conn_b.broken = True
    conn_c = FakeConnection("c")

    emitter, _ = _make_emitter(
        connections=[conn_a, conn_b, conn_c],
        log_calls=log_calls,
    )

    await emitter.send_message("chat-1", "hi")

    assert len(conn_a.sent) == 1
    assert len(conn_c.sent) == 1
    assert len(log_calls) == 1
    assert log_calls[0][0] == "b"
