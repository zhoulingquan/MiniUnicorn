"""Tests for AgentRunner's supervised periodic reflection tasks.

Validates that:
* The runner owns a ``TaskSupervisor`` for fire-and-forget reflections.
* A failing periodic reflection is logged exactly once via the supervisor's
  done-callback (no silent swallow).
* :meth:`AgentRunner.aclose` drains pending reflection tasks.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from loguru import logger as loguru_logger

from miniunicorn.agent.runner import AgentRunner, AgentRunSpec
from miniunicorn.config.schema import AgentDefaults
from miniunicorn.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from miniunicorn.utils.task_supervisor import TaskSupervisor

from tests.agent.conftest import FakeToolExecutionPort

_MAX_TOOL_RESULT_CHARS = AgentDefaults().max_tool_result_chars


def _patch_reflection(monkeypatch: pytest.MonkeyPatch, reflect_coro) -> None:
    """Patch ``miniunicorn.agent.reflection.Reflection`` with a fake whose
    ``reflect()`` delegates to ``reflect_coro``.

    The runner imports ``Reflection`` lazily inside ``run()``, so patching the
    source module's attribute is sufficient. ``monkeypatch`` restores it on
    teardown, preventing cross-test pollution.
    """

    class _FakeReflection:
        def __init__(self, *args, **kwargs):
            pass

        async def reflect(self, **kwargs):
            return await reflect_coro(**kwargs)

    import miniunicorn.agent.reflection as reflection_mod

    monkeypatch.setattr(reflection_mod, "Reflection", _FakeReflection)


def _make_runner_for_reflection(tmp_path) -> tuple[AgentRunner, MagicMock]:
    """Build a runner whose run() performs one tool call then finalizes.

    With ``reflection_interval=1`` the post-iteration-0 hook fires a periodic
    reflection as a supervised background task.
    """

    provider = MagicMock(spec=LLMProvider)
    call_count = {"n": 0}

    async def chat_with_retry(*, messages, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return LLMResponse(
                content="thinking",
                tool_calls=[
                    ToolCallRequest(id="call_1", name="list_dir", arguments={"path": "."})
                ],
                usage={"prompt_tokens": 5, "completion_tokens": 3},
            )
        return LLMResponse(content="done", tool_calls=[], usage={})

    provider.chat_with_retry = chat_with_retry

    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(return_value="tool result")

    runner = AgentRunner(provider)
    # Use a no-op governor whose govern() returns messages unchanged. The
    # real governor.govern() is synchronous, so we use a plain MagicMock
    # (not AsyncMock) to avoid unawaited-coroutine warnings.
    runner._default_governor = MagicMock()
    runner._default_governor.govern = MagicMock(side_effect=lambda msgs, _ctx: msgs)
    return runner, tools


@pytest.mark.asyncio
async def test_runner_owns_reflection_supervisor():
    """The runner exposes a TaskSupervisor for reflection tasks."""
    runner = AgentRunner(MagicMock(spec=LLMProvider))
    assert isinstance(runner._reflection_supervisor, TaskSupervisor)
    assert runner._reflection_supervisor.pending_count == 0


@pytest.mark.asyncio
async def test_failing_periodic_reflection_is_logged(tmp_path, monkeypatch, caplog):
    """A periodic reflection that raises must be logged via the supervisor."""

    async def _fail(**_kwargs):
        raise RuntimeError("reflection exploded")

    _patch_reflection(monkeypatch, _fail)
    runner, tools = _make_runner_for_reflection(tmp_path)

    handler_id = loguru_logger.add(caplog.handler, format="{message}", level="ERROR")
    try:
        with caplog.at_level(logging.ERROR):
            await runner.run(
                AgentRunSpec(
                    initial_messages=[
                        {"role": "system", "content": "system"},
                        {"role": "user", "content": "do task"},
                    ],
                    tools=tools,
                    tool_execution_port=FakeToolExecutionPort(tools),
                    model="test-model",
                    max_iterations=3,
                    max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
                    enable_reflection=True,
                    reflection_interval=1,
                    workspace=tmp_path,
                    session_key="ws:test",
                )
            )
            # Drain the background reflection so the done-callback fires.
            await runner.aclose()
    finally:
        loguru_logger.remove(handler_id)

    assert "reflection exploded" in caplog.text
    assert "reflection:ws:test:0" in caplog.text


@pytest.mark.asyncio
async def test_aclose_drains_pending_reflection(tmp_path, monkeypatch):
    """aclose() waits for in-flight reflection tasks to complete."""
    completed = asyncio.Event()

    async def _slow(**_kwargs):
        await asyncio.sleep(0.05)
        completed.set()
        return "lesson"

    _patch_reflection(monkeypatch, _slow)
    runner, tools = _make_runner_for_reflection(tmp_path)

    await runner.run(
        AgentRunSpec(
            initial_messages=[
                {"role": "system", "content": "system"},
                {"role": "user", "content": "do task"},
            ],
            tools=tools,
            tool_execution_port=FakeToolExecutionPort(tools),
            model="test-model",
            max_iterations=3,
            max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
            enable_reflection=True,
            reflection_interval=1,
            workspace=tmp_path,
            session_key="ws:test",
        )
    )
    # The reflection task is fire-and-forget; aclose() must drain it.
    assert not completed.is_set()
    await runner.aclose()
    assert completed.is_set()
    assert runner._reflection_supervisor.pending_count == 0


@pytest.mark.asyncio
async def test_aclose_with_no_pending_tasks_is_noop():
    """aclose() on a fresh runner with no reflection tasks is a no-op."""
    runner = AgentRunner(MagicMock(spec=LLMProvider))
    await runner.aclose()
    assert runner._reflection_supervisor.pending_count == 0
