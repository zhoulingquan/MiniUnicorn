"""Adapter between :class:`AgentLoop` and :class:`AgentRunner`.

``AgentRunAdapter`` is the single thick adaptation layer that constructs the
:class:`AgentRunSpec` from loop-level state, invokes the runner, and copies
the runner's results into a :class:`AgentLoopRunResult`. The loop delegates
``_run_agent_loop`` here so the loop body can shrink without changing the
runner's contract.

The adapter sees the host through the narrow :class:`AgentRunHost` protocol
and never imports ``AgentLoop`` at runtime.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Protocol

from loguru import logger

from miniunicorn.agent.hook import AgentHook, CompositeHook
from miniunicorn.agent.progress_hook import AgentProgressHook
from miniunicorn.agent.runner import _MAX_INJECTIONS_PER_TURN, AgentRunSpec
from miniunicorn.agent.tools.context import (
    RequestContext,
    bind_request_context,
    reset_request_context,
)
from miniunicorn.agent.tools.file_state import bind_file_states, reset_file_states
from miniunicorn.agent.turn_runtime import (
    AgentLoopRunResult,
    current_turn_runtime,
)
from miniunicorn.security.workspace_access import (
    bind_workspace_scope,
    reset_workspace_scope,
)
from miniunicorn.session.goal_state import (
    goal_state_runtime_lines,
    runner_wall_llm_timeout_s,
    sustained_goal_active,
)
from miniunicorn.utils.runtime import SUSTAINED_GOAL_CONTINUE_PROMPT

if TYPE_CHECKING:
    from miniunicorn.agent.runner import AgentRunner
    from miniunicorn.agent.subagent_registry import SubagentDefinition
    from miniunicorn.agent.tools.registry import ToolRegistry
    from miniunicorn.session.manager import Session


def _durable_runtime_port(attr: str) -> Any:
    """Read a durable runtime port from the bound TurnRuntime (design §11.1).

    Returns None for legacy (non-durable) turns where no TurnRuntime is
    bound or the port was not populated by the Worker Adapter.
    """
    runtime = current_turn_runtime()
    if runtime is None:
        return None
    return getattr(runtime, attr, None)


class DirectToolExecutionPort:
    """Legacy/test fallback that executes tools directly via the registry.

    Used when no durable ToolGateway is bound (legacy/test mode). Mirrors
    the former in-process execution path: resolve the tool via the
    registry, call ``tool.execute(**params)`` for real ToolRegistry
    instances, or fall back to ``tools.execute(name, params)`` for mocks.
    Exceptions are re-raised so the Runner's gateway exception handler
    can apply violation classification and soft-error semantics.
    """

    def __init__(self, tools: Any) -> None:
        self._tools = tools

    async def execute(self, request: Any) -> Any:
        from miniunicorn.agent.ports import ToolExecutionResult
        from miniunicorn.agent.tools.registry import ToolRegistry

        if isinstance(self._tools, ToolRegistry):
            tool = self._tools.get(request.tool_name)
            if tool is not None:
                result = await tool.execute(**request.normalized_arguments)
            else:
                result = await self._tools.execute(
                    request.tool_name, request.normalized_arguments
                )
        else:
            result = await self._tools.execute(
                request.tool_name, request.normalized_arguments
            )
        return ToolExecutionResult(state="SUCCEEDED", content=result)


class AgentRunHost(Protocol):
    """Host capabilities required by :class:`AgentRunAdapter`."""

    # Provider/model/runtime settings ------------------------------------
    provider: Any
    model: str
    max_iterations: int
    max_tool_result_chars: int
    tool_hint_max_length: int | None
    context_window_tokens: int
    context_block_limit: int | None
    provider_retry_mode: str
    use_planner: bool
    planner_model: str | None
    planner_max_replans: int
    enable_reflection: bool
    reflection_interval: int
    runner: "AgentRunner"
    sessions: Any
    subagents: Any
    tools: "ToolRegistry"
    workspace_scopes: Any
    context: Any  # ContextBuilder; only _build_user_content is used here
    _extra_hooks: list[AgentHook]
    _file_state_store: Any

    # Mutable host state -------------------------------------------------
    def _sync_subagent_runtime_limits(self) -> None: ...

    def _filter_tools_for_override(self, whitelist: list[str]) -> "ToolRegistry": ...

    def _set_tool_context(
        self,
        channel: str,
        chat_id: str,
        message_id: str | None = None,
        metadata: dict | None = None,
        session_key: str | None = None,
    ) -> None: ...

    def _prepare_message_media(self, content: str, media: list[str]) -> tuple[str, list[str]]: ...

    def _build_turn_budget(self) -> Any: ...

    @staticmethod
    def _record_turn_iteration(iteration: int) -> None: ...


class AgentRunAdapter:
    """Single thick adapter that invokes the runner for one turn."""

    def __init__(self, host: AgentRunHost) -> None:
        self._host = host

    @property
    def host(self) -> AgentRunHost:
        """Read-only diagnostic accessor for the bound host."""
        return self._host

    async def run(
        self,
        initial_messages: list[dict],
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        on_retry_wait: Callable[[str], Awaitable[None]] | None = None,
        *,
        session: Session | None = None,
        channel: str = "cli",
        chat_id: str = "direct",
        message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        session_key: str | None = None,
        pending_queue: asyncio.Queue | None = None,
        agent_override: SubagentDefinition | None = None,
        turn_hooks: list[AgentHook] | None = None,
    ) -> AgentLoopRunResult:
        """Run the agent iteration loop.

        *on_stream*: called with each content delta during streaming.
        *on_stream_end(resuming)*: called when a streaming session finishes.
        ``resuming=True`` means tool calls follow (spinner should restart);
        ``resuming=False`` means this is the final response.

        *turn_hooks*: per-dispatch hooks bound to this single turn (e.g. SDK
        capture hook). Combined with the loop-level ``_extra_hooks`` so the
        SDK no longer mutates shared state for concurrent runs.

        Returns an :class:`AgentLoopRunResult`.
        """
        host = self._host
        host._sync_subagent_runtime_limits()

        loop_hook = AgentProgressHook(
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            channel=channel,
            chat_id=chat_id,
            message_id=message_id,
            metadata=metadata,
            session_key=session_key,
            tool_hint_max_length=host.tool_hint_max_length,
            set_tool_context=host._set_tool_context,
            on_iteration=host._record_turn_iteration,
        )
        # Per-turn hooks take precedence over loop-level _extra_hooks so the
        # SDK can pass distinct hooks for concurrent runs without serializing
        # through shared mutable state.
        extra = list(host._extra_hooks) + list(turn_hooks or [])
        hook: AgentHook = CompositeHook([loop_hook] + extra) if extra else loop_hook

        async def _drain_pending(*, limit: int = _MAX_INJECTIONS_PER_TURN) -> list[dict[str, Any]]:
            """Drain follow-up messages from the pending queue.

            When no messages are immediately available but sub-agents
            spawned in this dispatch are still running, blocks until at
            least one result arrives (or timeout).  This keeps the runner
            loop alive so subsequent sub-agent completions are consumed
            in-order rather than dispatched separately.
            """
            if pending_queue is None:
                return []

            def _to_user_message(pending_msg: Any) -> dict[str, Any]:
                content = pending_msg.content
                media = pending_msg.media if pending_msg.media else None
                if media:
                    content, media = host._prepare_message_media(content, media)
                    media = media or None
                user_content = host.context._build_user_content(content, media)
                return {"role": "user", "content": user_content}

            items: list[dict[str, Any]] = []
            while len(items) < limit:
                try:
                    items.append(_to_user_message(pending_queue.get_nowait()))
                except asyncio.QueueEmpty:
                    break

            # Block if nothing drained but sub-agents spawned in this dispatch
            # are still running.  Keeps the runner loop alive so subsequent
            # completions are injected in-order rather than dispatched separately.
            if (
                not items
                and session is not None
                and host.subagents.get_running_count_by_session(session.key) > 0
            ):
                try:
                    msg = await asyncio.wait_for(pending_queue.get(), timeout=300)
                except asyncio.TimeoutError:
                    logger.warning(
                        "Timeout waiting for sub-agent completion in session {}",
                        session.key,
                    )
                    return items
                items.append(_to_user_message(msg))
                while len(items) < limit:
                    try:
                        items.append(_to_user_message(pending_queue.get_nowait()))
                    except asyncio.QueueEmpty:
                        break

            return items

        active_session_key = session.key if session else session_key
        effective_scope = host.workspace_scopes.for_turn(
            channel=channel,
            message_metadata=metadata,
            session_metadata=session.metadata if session is not None else None,
        )
        request_ctx = RequestContext(
            channel=channel,
            chat_id=chat_id,
            message_id=message_id,
            session_key=active_session_key,
            metadata=dict(metadata or {}),
        )
        file_state_token = bind_file_states(host._file_state_store.for_session(active_session_key))
        request_token = bind_request_context(request_ctx)
        workspace_token = bind_workspace_scope(effective_scope)
        # Apply subagent takeover overrides: filter tools to the subagent's
        # whitelist (if any) and select its model (falling back to host.model).
        if agent_override is not None:
            if agent_override.tools is not None:
                tools = host._filter_tools_for_override(agent_override.tools)
            else:
                tools = host.tools
            run_model = agent_override.model or host.model
        else:
            tools = host.tools
            run_model = host.model
        # Build continuation message that embeds the active goal objective so
        # the LLM can see it even if earlier Runtime Context was truncated.
        _goal_lines = goal_state_runtime_lines(session.metadata if session is not None else None)
        _goal_continue = (
            (
                "You have an active sustained goal:\n\n"
                + "\n".join(_goal_lines)
                + "\n\nPlease continue working toward the objective using your tools, "
                "or call complete_goal if the work is truly finished."
            )
            if _goal_lines
            else SUSTAINED_GOAL_CONTINUE_PROMPT
        )
        try:
            result = await host.runner.run(
                AgentRunSpec(
                    initial_messages=initial_messages,
                    tools=tools,
                    model=run_model,
                    max_iterations=host.max_iterations,
                    max_tool_result_chars=host.max_tool_result_chars,
                    hook=hook,
                    error_message="Sorry, I encountered an error calling the AI model.",
                    concurrent_tools=True,
                    workspace=effective_scope.project_path,
                    session_key=session.key if session else None,
                    context_window_tokens=host.context_window_tokens,
                    context_block_limit=host.context_block_limit,
                    provider_retry_mode=host.provider_retry_mode,
                    progress_callback=on_progress,
                    stream_progress_deltas=on_stream is not None,
                    retry_wait_callback=on_retry_wait,
                    injection_callback=_drain_pending,
                    # Sustained goals may legitimately exceed MINIUNICORN_LLM_TIMEOUT_S; idle stall
                    # is still capped by MINIUNICORN_STREAM_IDLE_TIMEOUT_S in streaming providers.
                    llm_timeout_s=runner_wall_llm_timeout_s(
                        host.sessions,
                        session.key if session is not None else session_key,
                        metadata=(session.metadata if session is not None else None),
                    ),
                    goal_active_predicate=lambda: (
                        sustained_goal_active(session.metadata) if session is not None else False
                    ),
                    goal_continue_message=_goal_continue,
                    # Plan-and-Execute / Reflection / TurnBudget (opt-in via config).
                    use_planner=host.use_planner,
                    planner_model=host.planner_model,
                    planner_max_replans=host.planner_max_replans,
                    enable_reflection=host.enable_reflection,
                    reflection_interval=host.reflection_interval,
                    turn_budget=host._build_turn_budget(),
                    # Durable runtime ports (design §11.1, §19, §20). Picked
                    # up from the bound TurnRuntime when running under the
                    # durable runtime; fall back to DirectToolExecutionPort
                    # for legacy/test turns where no TurnRuntime is bound.
                    tool_execution_port=(
                        _durable_runtime_port("tool_execution_port")
                        or DirectToolExecutionPort(tools)
                    ),
                    turn_journal=_durable_runtime_port("turn_journal"),
                )
            )
        finally:
            reset_workspace_scope(workspace_token)
            reset_request_context(request_token)
            reset_file_states(file_state_token)
        # Copy final usage into the bound TurnRuntime so self-inspection
        # and turn-end reads see the finalized values. The runner also
        # updates the runtime mid-turn; this covers the final exit path.
        runtime = current_turn_runtime()
        if runtime is not None:
            runtime.usage = dict(result.usage)
            runtime.last_call_usage = dict(result.last_call_usage)
        if result.stop_reason == "max_iterations":
            logger.warning("Max iterations ({}) reached", host.max_iterations)
            # Push final content through stream so streaming channels (e.g. Feishu)
            # update the card instead of leaving it empty.
            if on_stream and on_stream_end:
                await on_stream(result.final_content or "")
                await on_stream_end(resuming=False)
        elif result.stop_reason == "error":
            logger.error("LLM returned error: {}", (result.final_content or "")[:200])
        return AgentLoopRunResult(
            final_content=result.final_content,
            tools_used=result.tools_used,
            messages=result.messages,
            stop_reason=result.stop_reason,
            had_injections=result.had_injections,
            usage=dict(result.usage),
            last_call_usage=dict(result.last_call_usage),
        )
