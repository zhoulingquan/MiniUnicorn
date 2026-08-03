"""Model-client tests for AgentRunner extraction (Task 10).

These tests pin the behavior of the ``ModelRequester`` collaborator that
Task 10 extracts from ``AgentRunner``. They cover:

- Provider swap: changing ``runner.provider`` between runs routes the
  second run through the new Provider (the getter resolves live).
- Request kwargs parity: exact kwargs captured by the fake Provider.
- Streaming callbacks: ``chat_stream_with_retry`` is used with the
  ``on_content_delta`` / ``on_thinking_delta`` callbacks.
- Retry: ``retry_mode`` and ``on_retry_wait`` are forwarded.
- Cancellation: ``asyncio.CancelledError`` propagates.
- Provider exception: a raised exception propagates.

The file imports ``ModelRequester`` from ``miniunicorn.agent.runner_model``
so Step 2 fails until that module exists.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from miniunicorn.agent.hook import AgentHook, AgentHookContext
from miniunicorn.agent.runner import AgentRunner, AgentRunSpec
from miniunicorn.agent.runner_model import ModelRequester
from miniunicorn.config.schema import AgentDefaults
from miniunicorn.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from tests.agent.conftest import FakeToolExecutionPort

_MAX_TOOL_RESULT_CHARS = AgentDefaults().max_tool_result_chars


def _spec(
    messages: list[dict[str, Any]],
    *,
    tools: Any = None,
    tool_execution_port: Any = None,
    max_iterations: int = 3,
    **kwargs: Any,
) -> AgentRunSpec:
    if tools is None:
        tools = MagicMock()
        tools.get_definitions.return_value = []
    return AgentRunSpec(
        initial_messages=messages,
        tools=tools,
        tool_execution_port=tool_execution_port,
        model="test-model",
        max_iterations=max_iterations,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        **kwargs,
    )


def _no_tools() -> Any:
    tools = MagicMock()
    tools.get_definitions.return_value = []
    return tools


@pytest.mark.asyncio
async def test_provider_swap_routes_second_run_through_new_provider() -> None:
    """Changing ``runner.provider`` between runs routes the second run through
    the new Provider.

    ``ModelRequester`` must resolve the Provider through its getter for every
    call, so swapping ``runner.provider`` is observed by the next run.
    """
    provider_a = MagicMock(spec=LLMProvider)
    provider_a.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content="from-a", tool_calls=[], usage={})
    )
    provider_b = MagicMock(spec=LLMProvider)
    provider_b.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content="from-b", tool_calls=[], usage={})
    )

    tools = _no_tools()
    runner = AgentRunner(provider_a)
    result_a = await runner.run(
        _spec(
            [{"role": "user", "content": "hi"}],
            tools=tools,
            tool_execution_port=FakeToolExecutionPort(tools),
        )
    )
    assert result_a.final_content == "from-a"
    assert provider_a.chat_with_retry.await_count == 1
    assert provider_b.chat_with_retry.await_count == 0

    runner.provider = provider_b
    result_b = await runner.run(
        _spec(
            [{"role": "user", "content": "hi"}],
            tools=tools,
            tool_execution_port=FakeToolExecutionPort(tools),
        )
    )
    assert result_b.final_content == "from-b"
    assert provider_a.chat_with_retry.await_count == 1
    assert provider_b.chat_with_retry.await_count == 1


@pytest.mark.asyncio
async def test_model_requester_resolves_provider_through_getter() -> None:
    """``ModelRequester`` calls the getter on every ``request`` invocation."""
    provider_a = MagicMock(spec=LLMProvider)
    provider_a.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content="a", tool_calls=[], usage={})
    )
    provider_b = MagicMock(spec=LLMProvider)
    provider_b.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content="b", tool_calls=[], usage={})
    )

    current = provider_a
    requester = ModelRequester(lambda: current)

    assert requester.provider is provider_a

    tools = _no_tools()
    spec = _spec(
        [{"role": "user", "content": "hi"}],
        tools=tools,
        tool_execution_port=FakeToolExecutionPort(tools),
    )
    hook = AgentHook()
    context = AgentHookContext(iteration=0, messages=spec.initial_messages)
    response_a = await requester.request(spec, spec.initial_messages, hook, context)
    assert response_a.content == "a"
    assert provider_a.chat_with_retry.await_count == 1

    current = provider_b
    response_b = await requester.request(spec, spec.initial_messages, hook, context)
    assert response_b.content == "b"
    assert provider_b.chat_with_retry.await_count == 1
    assert provider_a.chat_with_retry.await_count == 1


@pytest.mark.asyncio
async def test_request_kwargs_parity() -> None:
    """Exact kwargs forwarded to ``chat_with_retry`` match the spec fields."""
    provider = MagicMock(spec=LLMProvider)
    captured: dict[str, Any] = {}

    async def chat_with_retry(**kwargs: Any) -> LLMResponse:
        # Copy messages so later mutations don't affect the captured snapshot.
        captured.update(kwargs)
        captured["messages"] = list(kwargs["messages"])
        return LLMResponse(content="ok", tool_calls=[], usage={})

    provider.chat_with_retry = chat_with_retry

    tools = MagicMock()
    tools.get_definitions.return_value = [{"function": {"name": "t"}}]

    def on_retry_wait(_: float) -> None:
        pass

    spec = AgentRunSpec(
        initial_messages=[{"role": "user", "content": "hi"}],
        tools=tools,
        tool_execution_port=FakeToolExecutionPort(tools),
        model="kw-model",
        max_iterations=1,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        temperature=0.7,
        max_tokens=128,
        reasoning_effort="high",
        provider_retry_mode="aggressive",
        retry_wait_callback=on_retry_wait,
    )
    runner = AgentRunner(provider)
    await runner.run(spec)

    assert captured["model"] == "kw-model"
    assert captured["temperature"] == 0.7
    assert captured["max_tokens"] == 128
    assert captured["reasoning_effort"] == "high"
    assert captured["retry_mode"] == "aggressive"
    assert captured["on_retry_wait"] is on_retry_wait
    assert captured["messages"] == spec.initial_messages
    assert captured["tools"] == [{"function": {"name": "t"}}]


@pytest.mark.asyncio
async def test_request_finalization_forwards_tools_none() -> None:
    """``request_finalization`` passes ``tools=None`` to the Provider."""
    provider = MagicMock(spec=LLMProvider)
    captured: dict[str, Any] = {}

    async def chat_with_retry(**kwargs: Any) -> LLMResponse:
        captured.update(kwargs)
        return LLMResponse(content="final", tool_calls=[], usage={})

    provider.chat_with_retry = chat_with_retry

    tools = _no_tools()
    spec = _spec(
        [{"role": "user", "content": "hi"}],
        tools=tools,
        tool_execution_port=FakeToolExecutionPort(tools),
    )
    requester = ModelRequester(lambda: provider)
    response = await requester.request_finalization(spec, list(spec.initial_messages))
    assert response.content == "final"
    assert captured["tools"] is None
    # The finalization retry message must be appended.
    assert captured["messages"][-1]["role"] == "user"


@pytest.mark.asyncio
async def test_streaming_callbacks_route_through_chat_stream() -> None:
    """A hook that wants streaming routes through ``chat_stream_with_retry``."""
    provider = MagicMock(spec=LLMProvider)
    deltas: list[str] = []
    thinking_deltas: list[str] = []

    async def chat_stream_with_retry(
        *,
        on_content_delta: Any = None,
        on_thinking_delta: Any = None,
        **kwargs: Any,
    ) -> LLMResponse:
        await on_content_delta("hel")
        await on_content_delta("lo")
        await on_thinking_delta("step")
        return LLMResponse(content="hello", tool_calls=[], usage={})

    provider.chat_stream_with_retry = chat_stream_with_retry
    provider.chat_with_retry = AsyncMock()

    class StreamingHook(AgentHook):
        def wants_streaming(self) -> bool:
            return True

        async def on_stream(self, context: AgentHookContext, delta: str) -> None:
            deltas.append(delta)

        async def emit_reasoning(self, reasoning_content: str | None) -> None:
            if reasoning_content:
                thinking_deltas.append(reasoning_content)

    tools = _no_tools()
    runner = AgentRunner(provider)
    result = await runner.run(
        _spec(
            [{"role": "user", "content": "hi"}],
            tools=tools,
            tool_execution_port=FakeToolExecutionPort(tools),
            hook=StreamingHook(),
        )
    )
    assert result.final_content == "hello"
    assert deltas == ["hel", "lo"]
    assert thinking_deltas == ["step"]
    provider.chat_with_retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_mode_and_wait_callback_passed_through() -> None:
    """``retry_mode`` and ``on_retry_wait`` reach the streaming path too."""
    provider = MagicMock(spec=LLMProvider)
    captured: dict[str, Any] = {}

    async def chat_stream_with_retry(**kwargs: Any) -> LLMResponse:
        captured.update(kwargs)
        return LLMResponse(content="ok", tool_calls=[], usage={})

    provider.chat_stream_with_retry = chat_stream_with_retry

    class StreamingHook(AgentHook):
        def wants_streaming(self) -> bool:
            return True

    def on_retry_wait(seconds: float) -> None:
        pass

    tools = _no_tools()
    spec = AgentRunSpec(
        initial_messages=[{"role": "user", "content": "hi"}],
        tools=tools,
        tool_execution_port=FakeToolExecutionPort(tools),
        model="retry-model",
        max_iterations=1,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        provider_retry_mode="aggressive",
        retry_wait_callback=on_retry_wait,
        hook=StreamingHook(),
    )
    runner = AgentRunner(provider)
    await runner.run(spec)
    assert captured["retry_mode"] == "aggressive"
    assert captured["on_retry_wait"] is on_retry_wait


@pytest.mark.asyncio
async def test_provider_exception_propagates() -> None:
    """A Provider exception propagates from ``ModelRequester.request``."""
    provider = MagicMock(spec=LLMProvider)

    async def chat_with_retry(**kwargs: Any) -> LLMResponse:
        raise RuntimeError("provider boom")

    provider.chat_with_retry = chat_with_retry

    tools = _no_tools()
    spec = _spec(
        [{"role": "user", "content": "hi"}],
        tools=tools,
        tool_execution_port=FakeToolExecutionPort(tools),
    )
    requester = ModelRequester(lambda: provider)
    hook = AgentHook()
    context = AgentHookContext(iteration=0, messages=spec.initial_messages)
    with pytest.raises(RuntimeError, match="provider boom"):
        await requester.request(spec, spec.initial_messages, hook, context)


@pytest.mark.asyncio
async def test_cancellation_propagates() -> None:
    """``asyncio.CancelledError`` propagates from ``ModelRequester.request``."""
    provider = MagicMock(spec=LLMProvider)

    async def chat_with_retry(**kwargs: Any) -> LLMResponse:
        await asyncio.sleep(3600)
        return LLMResponse(content="never", tool_calls=[], usage={})

    provider.chat_with_retry = chat_with_retry

    tools = _no_tools()
    spec = _spec(
        [{"role": "user", "content": "hi"}],
        tools=tools,
        tool_execution_port=FakeToolExecutionPort(tools),
    )
    requester = ModelRequester(lambda: provider)
    hook = AgentHook()
    context = AgentHookContext(iteration=0, messages=spec.initial_messages)

    task = asyncio.create_task(
        requester.request(spec, spec.initial_messages, hook, context)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_request_model_facade_delegates_to_model_requester() -> None:
    """``AgentRunner._request_model`` delegates to ``self._model_requester``."""
    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content="ok", tool_calls=[], usage={})
    )
    tools = _no_tools()
    runner = AgentRunner(provider)
    assert hasattr(runner, "_model_requester")
    assert isinstance(runner._model_requester, ModelRequester)
    spec = _spec(
        [{"role": "user", "content": "hi"}],
        tools=tools,
        tool_execution_port=FakeToolExecutionPort(tools),
    )
    hook = AgentHook()
    context = AgentHookContext(iteration=0, messages=spec.initial_messages)
    response = await runner._request_model(spec, spec.initial_messages, hook, context)
    assert response.content == "ok"


@pytest.mark.asyncio
async def test_request_finalization_retry_facade_delegates() -> None:
    """``AgentRunner._request_finalization_retry`` delegates to the requester."""
    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content="final", tool_calls=[], usage={})
    )
    tools = _no_tools()
    runner = AgentRunner(provider)
    spec = _spec(
        [{"role": "user", "content": "hi"}],
        tools=tools,
        tool_execution_port=FakeToolExecutionPort(tools),
    )
    response = await runner._request_finalization_retry(spec, list(spec.initial_messages))
    assert response.content == "final"


def test_runner_types_re_export_dataclasses() -> None:
    """``runner_types`` exposes ``AgentRunSpec`` and ``AgentRunResult``."""
    from miniunicorn.agent.runner_types import AgentRunResult, AgentRunSpec

    assert AgentRunSpec.__name__ == "AgentRunSpec"
    assert AgentRunResult.__name__ == "AgentRunResult"
