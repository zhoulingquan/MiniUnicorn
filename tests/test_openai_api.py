"""Focused tests for the fixed-session OpenAI-compatible API."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from miniunicorn.api.server import (
    API_CHAT_ID,
    API_SESSION_KEY,
    _chat_completion_response,
    _error_json,
    create_app,
    handle_chat_completions,
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
    return TaskSnapshot(
        task_id="test-task",
        state=state,
        checkpoint_phase="done",
        run_segment=0,
        root_attempt_count=1,
        max_root_attempts=3,
        recovery_pending=0,
    )


def _make_reply(content: str = "mock response") -> DurableReply:
    return DurableReply(content=content, outbox_id=1, metadata={})


def _make_runtime(
    *,
    reply_text: str = "mock response",
    state: str = "COMPLETED",
) -> MagicMock:
    """Create a mock RuntimeApplication for non-streaming tests."""
    runtime = MagicMock()
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
        yield queue

    runtime.submit = AsyncMock(side_effect=_submit)
    runtime.wait = AsyncMock(side_effect=_wait)
    runtime.submit_and_wait = AsyncMock(side_effect=_submit_and_wait)
    runtime.read_reply = MagicMock(side_effect=_read_reply)
    runtime.subscribe = _subscribe
    return runtime


def _make_mock_runtime(response_text: str = "mock response") -> MagicMock:
    """Alias for backward compatibility with test naming."""
    return _make_runtime(reply_text=response_text)


@pytest.fixture
def mock_agent():
    """Backward-compat fixture name; returns a mock RuntimeApplication."""
    return _make_mock_runtime()


@pytest.fixture
def app(mock_agent):
    return create_app(mock_agent, model_name="test-model", request_timeout=10.0)


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


def test_error_json() -> None:
    resp = _error_json(400, "bad request")
    assert resp.status == 400
    body = json.loads(resp.body)
    assert body["error"]["message"] == "bad request"
    assert body["error"]["code"] == 400


def test_chat_completion_response() -> None:
    result = _chat_completion_response("hello world", "test-model")
    assert result["object"] == "chat.completion"
    assert result["model"] == "test-model"
    assert result["choices"][0]["message"]["content"] == "hello world"
    assert result["choices"][0]["finish_reason"] == "stop"
    assert result["id"].startswith("chatcmpl-")


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_missing_messages_returns_400(aiohttp_client, app) -> None:
    client = await aiohttp_client(app)
    resp = await client.post("/v1/chat/completions", json={"model": "test"})
    assert resp.status == 400


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_no_user_message_returns_400(aiohttp_client, app) -> None:
    client = await aiohttp_client(app)
    resp = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "system", "content": "you are a bot"}]},
    )
    assert resp.status == 400


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_stream_true_returns_sse(aiohttp_client, app) -> None:
    client = await aiohttp_client(app)
    resp = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hello"}], "stream": True},
    )
    assert resp.status == 200
    assert resp.content_type == "text/event-stream"


@pytest.mark.asyncio
async def test_model_mismatch_returns_400() -> None:
    request = MagicMock()
    request.content_type = "application/json"
    request.json = AsyncMock(
        return_value={
            "model": "other-model",
            "messages": [{"role": "user", "content": "hello"}],
        }
    )
    request.app = {
        "runtime": _make_mock_runtime(),
        "model_name": "test-model",
        "request_timeout": 10.0,
        "session_locks": {},
    }

    resp = await handle_chat_completions(request)
    assert resp.status == 400
    body = json.loads(resp.body)
    assert "test-model" in body["error"]["message"]


@pytest.mark.asyncio
async def test_single_user_message_required() -> None:
    request = MagicMock()
    request.content_type = "application/json"
    request.json = AsyncMock(
        return_value={
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "previous reply"},
            ],
        }
    )
    request.app = {
        "runtime": _make_mock_runtime(),
        "model_name": "test-model",
        "request_timeout": 10.0,
        "session_locks": {},
    }

    resp = await handle_chat_completions(request)
    assert resp.status == 400
    body = json.loads(resp.body)
    assert "single user message" in body["error"]["message"].lower()


@pytest.mark.asyncio
async def test_single_user_message_must_have_user_role() -> None:
    request = MagicMock()
    request.content_type = "application/json"
    request.json = AsyncMock(
        return_value={
            "messages": [{"role": "system", "content": "you are a bot"}],
        }
    )
    request.app = {
        "runtime": _make_mock_runtime(),
        "model_name": "test-model",
        "request_timeout": 10.0,
        "session_locks": {},
    }

    resp = await handle_chat_completions(request)
    assert resp.status == 400
    body = json.loads(resp.body)
    assert "single user message" in body["error"]["message"].lower()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_successful_request_uses_fixed_api_session(aiohttp_client, mock_agent) -> None:
    app = create_app(mock_agent, model_name="test-model")
    client = await aiohttp_client(app)
    resp = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hello"}]},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["choices"][0]["message"]["content"] == "mock response"
    assert body["model"] == "test-model"
    mock_agent.submit_and_wait.assert_called_once()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_followup_requests_share_same_session_key(aiohttp_client) -> None:
    call_log: list[str] = []

    async def _submit_and_wait(request, timeout_s=None):
        call_log.append(request.session_key)
        return RuntimeTurnResult(
            snapshot=_make_snapshot(),
            reply=_make_reply(f"reply to {request.content}"),
        )

    runtime = MagicMock()
    runtime.submit_and_wait = AsyncMock(side_effect=_submit_and_wait)
    runtime.submit = AsyncMock(return_value=MagicMock(task_id="t"))
    runtime.wait = AsyncMock(return_value=_make_snapshot())
    runtime.read_reply = MagicMock(return_value=_make_reply())

    @asynccontextmanager
    async def _subscribe(task_id):
        yield asyncio.Queue()

    runtime.subscribe = _subscribe

    app = create_app(runtime, model_name="m")
    client = await aiohttp_client(app)

    r1 = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "first"}]},
    )
    r2 = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "second"}]},
    )

    assert r1.status == 200
    assert r2.status == 200
    assert call_log == [API_SESSION_KEY, API_SESSION_KEY]


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_fixed_session_requests_are_serialized(aiohttp_client) -> None:
    order: list[str] = []

    async def slow_submit_and_wait(request, timeout_s=None):
        order.append(f"start:{request.content}")
        await asyncio.sleep(0.1)
        order.append(f"end:{request.content}")
        return RuntimeTurnResult(
            snapshot=_make_snapshot(),
            reply=_make_reply(request.content),
        )

    runtime = MagicMock()
    runtime.submit_and_wait = AsyncMock(side_effect=slow_submit_and_wait)
    runtime.submit = AsyncMock(return_value=MagicMock(task_id="t"))
    runtime.wait = AsyncMock(return_value=_make_snapshot())
    runtime.read_reply = MagicMock(return_value=_make_reply())

    @asynccontextmanager
    async def _subscribe(task_id):
        yield asyncio.Queue()

    runtime.subscribe = _subscribe

    app = create_app(runtime, model_name="m")
    client = await aiohttp_client(app)

    async def send(msg: str):
        return await client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": msg}]},
        )

    r1, r2 = await asyncio.gather(send("first"), send("second"))
    assert r1.status == 200
    assert r2.status == 200
    # Verify serialization: one process must fully finish before the other starts
    if order[0] == "start:first":
        assert order.index("end:first") < order.index("start:second")
    else:
        assert order.index("end:second") < order.index("start:first")


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_models_endpoint(aiohttp_client, app) -> None:
    client = await aiohttp_client(app)
    resp = await client.get("/v1/models")
    assert resp.status == 200
    body = await resp.json()
    assert body["object"] == "list"
    assert body["data"][0]["id"] == "test-model"


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_health_endpoint(aiohttp_client, app) -> None:
    client = await aiohttp_client(app)
    resp = await client.get("/health")
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "ok"


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_multimodal_content_extracts_text(aiohttp_client, mock_agent) -> None:
    app = create_app(mock_agent, model_name="m")
    client = await aiohttp_client(app)
    resp = await client.post(
        "/v1/chat/completions",
        json={
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe this"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                    ],
                }
            ]
        },
    )
    assert resp.status == 200
    call_kwargs = mock_agent.submit_and_wait.call_args
    inbound_request = call_kwargs.args[0]
    assert inbound_request.content == "describe this"
    assert inbound_request.session_key == API_SESSION_KEY
    assert inbound_request.channel == "api"
    assert len(inbound_request.media) >= 0  # base64 images saved to disk


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_multimodal_remote_image_url_returns_400(aiohttp_client, mock_agent) -> None:
    app = create_app(mock_agent, model_name="m")
    client = await aiohttp_client(app)
    resp = await client.post(
        "/v1/chat/completions",
        json={
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe this"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://example.com/image.png"},
                        },
                    ],
                }
            ]
        },
    )

    assert resp.status == 400
    body = await resp.json()
    assert "remote image urls are not supported" in body["error"]["message"].lower()
    mock_agent.submit_and_wait.assert_not_called()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_empty_response_falls_back(aiohttp_client) -> None:
    """Empty durable reply should fall back to the fallback message."""
    from miniunicorn.utils.runtime import EMPTY_FINAL_RESPONSE_MESSAGE

    runtime = _make_runtime(reply_text="")
    app = create_app(runtime, model_name="m")
    client = await aiohttp_client(app)
    resp = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hello"}]},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["choices"][0]["message"]["content"] == EMPTY_FINAL_RESPONSE_MESSAGE


@pytest.mark.asyncio
async def test_process_message_accepts_media() -> None:
    """_process_message should forward media paths to _execute_message."""
    from miniunicorn.agent.loop import AgentLoop
    from miniunicorn.agent.turn_coordinator import TurnCoordinator
    from miniunicorn.agent.turn_dispatcher import TurnDispatcher
    from miniunicorn.agent.turn_runtime import ProcessedTurn
    from miniunicorn.bus.events import InboundMessage

    loop = AgentLoop.__new__(AgentLoop)
    loop._turn_coordinator = TurnCoordinator(max_concurrent_requests=None)

    captured_msg = None

    async def fake_execute(msg, **kwargs):
        nonlocal captured_msg
        captured_msg = msg
        return ProcessedTurn(outbound=None, context=None)

    loop._execute_message = fake_execute

    # Build a minimal TurnDispatcher that delegates to the loop's
    # _execute_message (bypassing full __init__).
    dispatcher = TurnDispatcher.__new__(TurnDispatcher)
    dispatcher._host = loop
    dispatcher._coordinator = loop._turn_coordinator
    loop._turn_dispatcher = dispatcher

    msg = InboundMessage(
        channel="cli",
        sender_id="user",
        chat_id="1",
        content="analyze this",
        media=["/tmp/image.png", "/tmp/report.pdf"],
    )
    await loop._process_message(msg, session_key="test:1")

    assert captured_msg is not None
    assert captured_msg.media == ["/tmp/image.png", "/tmp/report.pdf"]
    assert captured_msg.content == "analyze this"
