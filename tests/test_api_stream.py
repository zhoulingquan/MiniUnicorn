"""Tests for SSE streaming support in /v1/chat/completions."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from miniunicorn.api.server import (
    _SSE_DONE,
    _sse_chunk,
    create_app,
)
from miniunicorn.runtime.application import RuntimeTurnResult
from miniunicorn.runtime.models import DurableReply, RequestScope, TaskSnapshot

try:
    from aiohttp.test_utils import TestClient, TestServer

    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

pytest_plugins = ("pytest_asyncio",)


# ---------------------------------------------------------------------------
# Mock RuntimeApplication helpers
# ---------------------------------------------------------------------------


def _make_scope() -> RequestScope:
    return RequestScope(
        tenant_id="local",
        principal_id="local-user",
        agent_id="default",
        workspace_id="test",
    )


def _make_snapshot(state: str = "COMPLETED") -> TaskSnapshot:
    error = None
    if state == "FAILED":
        from miniunicorn.agent.ports import SafeError

        error = SafeError(
            error_code="INTERNAL_ERROR",
            error_summary="backend blew up",
        )
    return TaskSnapshot(
        task_id="test-task",
        state=state,
        checkpoint_phase="done",
        run_segment=0,
        root_attempt_count=1,
        max_root_attempts=3,
        recovery_pending=0,
        error=error,
    )


def _make_reply(content: str = "mock response") -> DurableReply:
    return DurableReply(content=content, outbox_id=1, metadata={})


def _make_runtime(
    *,
    reply_text: str = "mock response",
    delta_tokens: list[str] | None = None,
    state: str = "COMPLETED",
) -> MagicMock:
    """Create a mock RuntimeApplication.

    For non-streaming: ``submit_and_wait`` returns a completed result.
    For streaming: ``submit`` returns a handle, ``subscribe`` yields delta
    events, ``wait`` returns a terminal snapshot, ``read_reply`` returns
    the final reply.
    """
    runtime = MagicMock()
    _scope = _make_scope()
    snapshot = _make_snapshot(state)
    reply = _make_reply(reply_text)

    async def _submit(request):
        handle = MagicMock()
        handle.task_id = "test-task"
        return handle

    async def _wait(scope, task_id, timeout_s):
        return snapshot

    async def _submit_and_wait(request, timeout_s=None):
        return RuntimeTurnResult(snapshot=snapshot, reply=reply)

    def _read_reply(scope, task_id):
        return reply

    @asynccontextmanager
    async def _subscribe(task_id):
        queue: asyncio.Queue = asyncio.Queue()
        if delta_tokens:
            for token in delta_tokens:
                await queue.put({"event": "delta", "text": token})
            await queue.put({"event": "stream_end", "stream_id": "s0"})
        yield queue

    runtime.submit = AsyncMock(side_effect=_submit)
    runtime.wait = AsyncMock(side_effect=_wait)
    runtime.submit_and_wait = AsyncMock(side_effect=_submit_and_wait)
    runtime.read_reply = MagicMock(side_effect=_read_reply)
    runtime.subscribe = _subscribe
    return runtime


# ---------------------------------------------------------------------------
# Unit tests for SSE helpers
# ---------------------------------------------------------------------------


def test_sse_chunk_with_delta() -> None:
    raw = _sse_chunk("hello", "test-model", "chatcmpl-abc123")
    line = raw.decode()
    assert line.startswith("data: ")
    payload = json.loads(line[len("data: ") :])
    assert payload["id"] == "chatcmpl-abc123"
    assert payload["object"] == "chat.completion.chunk"
    assert payload["model"] == "test-model"
    assert payload["choices"][0]["delta"]["content"] == "hello"
    assert payload["choices"][0]["finish_reason"] is None


def test_sse_chunk_finish_reason() -> None:
    raw = _sse_chunk("", "m", "id1", finish_reason="stop")
    payload = json.loads(raw.decode().split("data: ", 1)[1])
    assert payload["choices"][0]["delta"] == {}
    assert payload["choices"][0]["finish_reason"] == "stop"


def test_sse_done_format() -> None:
    assert _SSE_DONE == b"data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# Integration tests with aiohttp TestClient
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def aiohttp_client():
    clients: list[TestClient] = []

    async def _make_client(app):
        client = TestClient(TestServer(app))
        await client.start_server()
        clients.append(client)
        return client

    try:
        yield _make_client
    finally:
        for client in clients:
            await client.close()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_stream_true_returns_sse(aiohttp_client) -> None:
    """stream=true should return text/event-stream with SSE chunks."""
    runtime = _make_runtime(delta_tokens=["Hello", " world"])
    app = create_app(runtime, model_name="test-model")
    client = await aiohttp_client(app)

    resp = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
    )
    assert resp.status == 200
    assert resp.content_type == "text/event-stream"

    body = await resp.text()
    lines = [line for line in body.split("\n") if line.startswith("data: ")]

    # Should have: 2 token chunks + 1 finish chunk + [DONE]
    data_lines = [line[len("data: ") :] for line in lines]
    assert data_lines[-1] == "[DONE]"

    chunks = [json.loads(line) for line in data_lines[:-1]]
    assert chunks[0]["choices"][0]["delta"]["content"] == "Hello"
    assert chunks[1]["choices"][0]["delta"]["content"] == " world"
    # Last chunk before [DONE] should have finish_reason=stop
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["choices"][0]["delta"] == {}


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_stream_false_returns_json(aiohttp_client) -> None:
    """stream=false should still return regular JSON response."""
    runtime = _make_runtime(reply_text="normal reply")
    app = create_app(runtime, model_name="m")
    client = await aiohttp_client(app)

    resp = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}], "stream": False},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "normal reply"


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_stream_default_is_false(aiohttp_client) -> None:
    """Omitting stream should behave like stream=false."""
    runtime = _make_runtime(reply_text="default reply")
    app = create_app(runtime, model_name="m")
    client = await aiohttp_client(app)

    resp = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["object"] == "chat.completion"


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_stream_sse_chunk_ids_are_consistent(aiohttp_client) -> None:
    """All SSE chunks in a single stream should share the same id."""
    runtime = _make_runtime(delta_tokens=["A", "B", "C"])
    app = create_app(runtime, model_name="m")
    client = await aiohttp_client(app)

    resp = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "go"}], "stream": True},
    )
    body = await resp.text()
    data_lines = [
        line[len("data: ") :]
        for line in body.split("\n")
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    chunks = [json.loads(line) for line in data_lines]

    chunk_ids = {c["id"] for c in chunks}
    assert len(chunk_ids) == 1, f"Expected single chunk id, got {chunk_ids}"
    assert chunk_ids.pop().startswith("chatcmpl-")


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_stream_uses_final_response_when_no_deltas(aiohttp_client) -> None:
    """stream=true should emit the durable reply when no deltas were received."""
    runtime = _make_runtime(reply_text="plain final", delta_tokens=[])
    app = create_app(runtime, model_name="m")
    client = await aiohttp_client(app)

    resp = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
    )

    assert resp.status == 200
    body = await resp.text()
    data_lines = [line[len("data: ") :] for line in body.split("\n") if line.startswith("data: ")]
    chunks = [json.loads(line) for line in data_lines[:-1]]
    deltas = [c["choices"][0]["delta"].get("content", "") for c in chunks]

    assert "plain final" in deltas
    assert data_lines[-1] == "[DONE]"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_stream_with_session_id(aiohttp_client) -> None:
    """Streaming should respect session_id for session key routing."""
    captured_request: list = []

    async def _submit(request):
        captured_request.append(request)
        handle = MagicMock()
        handle.task_id = "test-task"
        return handle

    runtime = _make_runtime(delta_tokens=["ok"])
    runtime.submit = AsyncMock(side_effect=_submit)
    app = create_app(runtime, model_name="m")
    client = await aiohttp_client(app)

    resp = await client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
            "session_id": "my-session",
        },
    )
    assert resp.status == 200
    assert captured_request[0].session_key == "api:my-session"


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_streaming_backend_failure_does_not_emit_success_terminator(aiohttp_client) -> None:
    """Failed tasks should not surface as a normal stop+[DONE] stream."""
    runtime = _make_runtime(state="FAILED", delta_tokens=[])
    app = create_app(runtime, model_name="m")
    client = await aiohttp_client(app)

    resp = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
    )

    assert resp.status == 200
    body = await resp.text()
    assert '"finish_reason": "stop"' not in body
    assert "[DONE]" not in body
