"""Subagent manager for background task execution."""

import asyncio
import contextvars
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from miniunicorn.agent.hook import AgentHook, AgentHookContext
from miniunicorn.agent.runner import AgentRunner, AgentRunSpec
from miniunicorn.agent.tools.context import ToolContext
from miniunicorn.agent.tools.file_state import FileStates
from miniunicorn.agent.tools.loader import ToolLoader
from miniunicorn.agent.tools.registry import ToolRegistry
from miniunicorn.bus.events import InboundMessage, OutboundMessage, make_session_key
from miniunicorn.bus.queue import MessageBus
from miniunicorn.config.schema import AgentDefaults, ToolsConfig
from miniunicorn.providers.base import LLMProvider
from miniunicorn.security.workspace_access import (
    WorkspaceScope,
    bind_workspace_scope,
    reset_workspace_scope,
    workspace_sandbox_status,
)
from miniunicorn.utils.prompt_templates import render_template

# 子代理递归深度跟踪：主代理 depth=0，子代理 depth=1，孙代理 depth=2。
# delegate tool 在运行时读取此 ContextVar 决定是否允许再 delegate。
# 默认值 0 表示主代理上下文（未进入子代理 run 时）。
_current_depth: contextvars.ContextVar[int] = contextvars.ContextVar(
    "_subagent_current_depth", default=0
)


def get_current_subagent_depth() -> int:
    """返回当前上下文的子代理递归深度。

    主代理运行时返回 0；子代理运行时返回 1；孙代理返回 2，依此类推。
    delegate tool 用此值与 ``max_subagent_recursion_depth`` 比较来决定是否允许再 delegate。
    """
    return _current_depth.get()


@dataclass(slots=True)
class SubagentStatus:
    """Real-time status of a running subagent."""

    task_id: str
    label: str
    task_description: str
    started_at: float  # time.monotonic()
    # initializing | awaiting_tools | tool_started | tool_blocked |
    # tool_completed | tools_completed | final_response | done | error
    phase: str = "initializing"
    iteration: int = 0
    tool_events: list = field(default_factory=list)  # [{name, status, detail}, ...]
    usage: dict = field(default_factory=dict)  # token usage
    stop_reason: str | None = None
    error: str | None = None


class _SubagentHook(AgentHook):
    """Hook for subagent execution — logs tool calls and updates status.

    When ``bus`` and origin routing (``origin_channel`` / ``origin_chat_id``)
    are provided, the hook also forwards tool/reasoning activity to the message
    bus as ``_subagent_activity`` outbound frames so connected clients can
    stream subagent progress in real time (upgrade: subagent activity stream).
    """

    def __init__(
        self,
        task_id: str,
        status: SubagentStatus | None = None,
        *,
        bus: MessageBus | None = None,
        origin_channel: str | None = None,
        origin_chat_id: str | None = None,
    ) -> None:
        super().__init__()
        self._task_id = task_id
        self._status = status
        self._bus = bus
        self._origin_channel = origin_channel
        self._origin_chat_id = origin_chat_id
        # Cached label/task_id for activity metadata; falls back to task_id when
        # status is unavailable (hook constructed without status).
        if status is not None:
            self._label = status.label
        else:
            self._label = task_id

    def _activity_enabled(self) -> bool:
        return (
            self._bus is not None
            and self._origin_channel is not None
            and self._origin_chat_id is not None
        )

    def _activity_metadata(self) -> dict[str, Any]:
        return {
            "_subagent_activity": True,
            "_subagent_label": self._label,
            "_subagent_task_id": self._task_id,
            "_progress": True,
        }

    async def _publish_activity(self, content: str) -> None:
        """Forward one activity breadcrumb to the bus (best-effort)."""
        if not self._activity_enabled():
            return
        try:
            await self._bus.publish_outbound(
                OutboundMessage(
                    channel=self._origin_channel,
                    chat_id=self._origin_chat_id,
                    content=content,
                    metadata=self._activity_metadata(),
                )
            )
        except Exception:
            logger.exception("Subagent [{}] failed to publish activity", self._task_id)

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        for tool_call in context.tool_calls:
            args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
            logger.debug(
                "Subagent [{}] executing: {} with arguments: {}",
                self._task_id,
                tool_call.name,
                args_str,
            )
            # Forward "tool start" activity to subscribed clients.
            await self._publish_activity(f"[{self._label}] calling {tool_call.name}")

    async def after_iteration(self, context: AgentHookContext) -> None:
        if self._status is not None:
            self._status.iteration = context.iteration
            self._status.tool_events = list(context.tool_events)
            self._status.usage = dict(context.usage)
            if context.error:
                self._status.error = str(context.error)
        # Forward tool completion / failure events. tool_events accumulate per
        # iteration; emit one breadcrumb per event so the client can render a
        # fine-grained trace. Only the most recent iteration's events are
        # forwarded to avoid duplicates (the runner resets context.tool_events
        # each iteration before populating with the new batch).
        for event in context.tool_events:
            name = event.get("name", "tool")
            status_str = event.get("status", "ok")
            detail = event.get("detail", "")
            suffix = f": {detail}" if detail else ""
            await self._publish_activity(f"[{self._label}] {name} {status_str}{suffix}")

    async def emit_reasoning(self, reasoning_content: str | None) -> None:
        # Forward reasoning chunks (one-shot and streaming) to clients so the
        # subagent's thought process is visible alongside tool activity.
        if reasoning_content:
            await self._publish_activity(f"[{self._label}] thinking: {reasoning_content}")


class SubagentManager:
    """Manages background subagent execution."""

    @staticmethod
    def _auto_detect_concurrency(provider: LLMProvider) -> int:
        """根据 provider.is_local 自动选择并发上限。

        本地 provider(Ollama/vLLM)→ 1(保护 KV Cache)
        云端 provider(OpenAI/Anthropic)→ 4(可放心并行)
        """
        if getattr(provider, "is_local", False):
            return 1
        return 4

    def __init__(
        self,
        provider: LLMProvider,
        workspace: Path,
        bus: MessageBus,
        max_tool_result_chars: int,
        model: str | None = None,
        tools_config: ToolsConfig | None = None,
        restrict_to_workspace: bool = False,
        disabled_skills: list[str] | None = None,
        max_iterations: int | None = None,
        max_concurrent_subagents: int | None = None,
        llm_wall_timeout_for_session: Callable[[str | None], float | None] | None = None,
        max_subagent_recursion_depth: int | None = None,
        turn_budget_factory: Callable[[], Any] | None = None,
        # 主循环的高风险工具策略必须传播到子代理，否则 deny 可被 spawn 绕过。
        # 注意：approval_callback 不向子代理传播（loop 级 Manager vs 每次运行的
        # spec 级回调，架构上需要更大改动），此处仅同步静态策略。
        high_risk_policy: str = "allow",
    ):
        defaults = AgentDefaults()
        self.provider = provider
        self.workspace = workspace
        self.bus = bus
        self.model = model or provider.get_default_model()
        self.tools_config = tools_config or ToolsConfig()
        self.max_tool_result_chars = max_tool_result_chars
        self.restrict_to_workspace = restrict_to_workspace
        self.disabled_skills = set(disabled_skills or [])
        self.max_iterations = (
            max_iterations if max_iterations is not None else defaults.max_tool_iterations
        )
        self.max_concurrent_subagents = (
            max_concurrent_subagents
            if max_concurrent_subagents is not None
            else self._auto_detect_concurrency(provider)
        )
        # 并发上限信号量，强制限制在跑子代理数量
        # 当 max_concurrent_subagents 为 None（未配置自适应）时退化为 1，避免阻塞
        self._spawn_semaphore = asyncio.Semaphore(self.max_concurrent_subagents or 1)
        # 递归深度限制:None 时回退到 AgentDefaults 的默认值(1)。
        # delegate tool 在运行时读取此属性决定是否允许子代理再 delegate。
        self.max_subagent_recursion_depth = (
            max_subagent_recursion_depth
            if max_subagent_recursion_depth is not None
            else defaults.max_subagent_recursion_depth
        )
        self.runner = AgentRunner(provider)
        self._llm_wall_timeout_for_session = llm_wall_timeout_for_session
        # 每个子代理 run 调用一次该工厂，获得独立的新鲜 TurnBudget，
        # 使主轮次的 token/cost 预算对派生的 LLM 调用同样生效。
        self._turn_budget_factory = turn_budget_factory
        self._high_risk_policy = high_risk_policy
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._task_statuses: dict[str, SubagentStatus] = {}
        self._session_tasks: dict[str, set[str]] = {}  # session_key -> {task_id, ...}

    def _subagent_tools_config(self) -> ToolsConfig:
        """Build a ToolsConfig scoped for subagent use."""
        return ToolsConfig(
            exec=self.tools_config.exec,
            web=self.tools_config.web,
            restrict_to_workspace=self.restrict_to_workspace,
        )

    def _build_tools(
        self,
        workspace: Path | None = None,
        tools_config: ToolsConfig | None = None,
    ) -> ToolRegistry:
        """Build an isolated subagent tool registry via ToolLoader."""
        root = self.workspace if workspace is None else workspace
        registry = ToolRegistry()
        cfg = tools_config if tools_config is not None else self._subagent_tools_config()
        ctx = ToolContext(
            config=cfg,
            workspace=str(root.resolve()),
            file_state_store=FileStates(),
            workspace_sandbox=workspace_sandbox_status(
                restrict_to_workspace=cfg.restrict_to_workspace,
                workspace=root,
            ),
        )
        ToolLoader().load(ctx, registry, scope="subagent")
        return registry

    def _filter_tools(self, registry: ToolRegistry, whitelist: list[str]) -> ToolRegistry:
        """Return a filtered copy of the registry containing only whitelisted tools."""
        filtered = ToolRegistry()
        for name in whitelist:
            tool = registry.get(name)
            if tool is not None:
                filtered.register(tool)
        return filtered

    def set_provider(self, provider: LLMProvider, model: str) -> None:
        self.provider = provider
        self.model = model
        self.runner.provider = provider

    async def spawn(
        self,
        task: str,
        label: str | None = None,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
        session_key: str | None = None,
        origin_message_id: str | None = None,
        temperature: float | None = None,
        workspace_scope: WorkspaceScope | None = None,
    ) -> str:
        """Spawn a subagent to execute a task in the background."""
        task_id = str(uuid.uuid4())[:8]
        display_label = label or task[:30] + ("..." if len(task) > 30 else "")
        origin = {"channel": origin_channel, "chat_id": origin_chat_id, "session_key": session_key}

        status = SubagentStatus(
            task_id=task_id,
            label=display_label,
            task_description=task,
            started_at=time.monotonic(),
        )
        self._task_statuses[task_id] = status

        bg_task = asyncio.create_task(
            self._run_subagent(
                task_id,
                task,
                display_label,
                origin,
                status,
                origin_message_id,
                temperature,
                workspace_scope,
            )
        )
        self._running_tasks[task_id] = bg_task
        if session_key:
            self._session_tasks.setdefault(session_key, set()).add(task_id)

        def _cleanup(_: asyncio.Task) -> None:
            self._running_tasks.pop(task_id, None)
            self._task_statuses.pop(task_id, None)
            if session_key and (ids := self._session_tasks.get(session_key)):
                ids.discard(task_id)
                if not ids:
                    del self._session_tasks[session_key]

        bg_task.add_done_callback(_cleanup)

        logger.info("Spawned subagent [{}]: {}", task_id, display_label)
        return f"Subagent [{display_label}] started (id: {task_id}). I'll notify you when it completes."

    async def spawn_and_wait(
        self,
        task: str,
        label: str | None = None,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
        session_key: str | None = None,
        temperature: float | None = None,
        workspace_scope: WorkspaceScope | None = None,
        system_prompt_override: str | None = None,
        model_override: str | None = None,
        tools_whitelist: list[str] | None = None,
    ) -> tuple[str, str]:
        """Spawn a subagent and await completion. Returns (status, final_content).

        Unlike spawn(), this does NOT announce via the message bus — the caller
        (e.g. execute_plan tool) handles the result directly.

        Optional overrides (used by the declarative `delegate` tool):
          * system_prompt_override — replace the default subagent system prompt.
          * model_override         — run the subagent with a different model.
          * tools_whitelist        — restrict the subagent to a subset of tools.
        """
        task_id = str(uuid.uuid4())[:8]
        display_label = label or task[:30] + ("..." if len(task) > 30 else "")
        origin = {"channel": origin_channel, "chat_id": origin_chat_id, "session_key": session_key}
        status = SubagentStatus(
            task_id=task_id,
            label=display_label,
            task_description=task,
            started_at=time.monotonic(),
        )
        self._task_statuses[task_id] = status
        if session_key:
            self._session_tasks.setdefault(session_key, set()).add(task_id)
        try:
            result_status, result_content = await self._run_subagent_direct(
                task_id,
                task,
                display_label,
                origin,
                status,
                temperature=temperature,
                workspace_scope=workspace_scope,
                system_prompt_override=system_prompt_override,
                model_override=model_override,
                tools_whitelist=tools_whitelist,
            )
            return result_status, result_content
        finally:
            self._task_statuses.pop(task_id, None)
            if session_key and (ids := self._session_tasks.get(session_key)):
                ids.discard(task_id)
                if not ids:
                    del self._session_tasks[session_key]

    async def _run_subagent(
        self,
        task_id: str,
        task: str,
        label: str,
        origin: dict[str, str],
        status: SubagentStatus,
        origin_message_id: str | None = None,
        temperature: float | None = None,
        workspace_scope: WorkspaceScope | None = None,
    ) -> None:
        """Execute the subagent task and announce the result."""
        logger.info("Subagent [{}] starting task: {}", task_id, label)

        async def _on_checkpoint(payload: dict) -> None:
            status.phase = payload.get("phase", status.phase)
            status.iteration = payload.get("iteration", status.iteration)

        # 通过信号量强制限制同时运行的子代理数量
        async with self._spawn_semaphore:
            try:
                root = (
                    workspace_scope.project_path if workspace_scope is not None else self.workspace
                )
                cfg = None
                if workspace_scope is not None:
                    cfg = self._subagent_tools_config()
                    cfg.restrict_to_workspace = workspace_scope.restrict_to_workspace
                tools = self._build_tools(workspace=root, tools_config=cfg)
                system_prompt = self._build_subagent_prompt(workspace=root)
                messages: list[dict[str, Any]] = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": task},
                ]

                sess_key = origin.get("session_key")
                llm_timeout = (
                    self._llm_wall_timeout_for_session(sess_key)
                    if self._llm_wall_timeout_for_session
                    else None
                )
                token = (
                    bind_workspace_scope(workspace_scope) if workspace_scope is not None else None
                )
                # 子代理运行时 depth+1，使 delegate tool 能检测递归深度。
                # ContextVar.set 返回 token，用于 finally 中 reset。
                depth_token = _current_depth.set(_current_depth.get() + 1)
                try:
                    result = await self.runner.run(
                        AgentRunSpec(
                            initial_messages=messages,
                            tools=tools,
                            model=self.model,
                            temperature=temperature,
                            max_iterations=self.max_iterations,
                            max_tool_result_chars=self.max_tool_result_chars,
                            hook=_SubagentHook(task_id, status),
                            max_iterations_message="Task completed but no final response was generated.",
                            error_message=None,
                            fail_on_tool_error=True,
                            checkpoint_callback=_on_checkpoint,
                            session_key=sess_key,
                            workspace=root,
                            llm_timeout_s=llm_timeout,
                            turn_budget=(
                                self._turn_budget_factory()
                                if self._turn_budget_factory is not None
                                else None
                            ),
                            high_risk_policy=self._high_risk_policy,
                        )
                    )
                finally:
                    _current_depth.reset(depth_token)
                    if token is not None:
                        reset_workspace_scope(token)
                status.phase = "done"
                status.stop_reason = result.stop_reason

                if result.stop_reason == "tool_error":
                    status.tool_events = list(result.tool_events)
                    await self._announce_result(
                        task_id,
                        label,
                        task,
                        self._format_partial_progress(result),
                        origin,
                        "error",
                        origin_message_id,
                    )
                elif result.stop_reason == "error":
                    await self._announce_result(
                        task_id,
                        label,
                        task,
                        result.error or "Error: subagent execution failed.",
                        origin,
                        "error",
                        origin_message_id,
                    )
                else:
                    final_result = (
                        result.final_content
                        or "Task completed but no final response was generated."
                    )
                    logger.info("Subagent [{}] completed successfully", task_id)
                    await self._announce_result(
                        task_id, label, task, final_result, origin, "ok", origin_message_id
                    )

            except Exception as e:
                status.phase = "error"
                status.error = str(e)
                logger.exception("Subagent [{}] failed", task_id)
                # 包裹 _announce_result 调用，避免通知失败掩盖原始错误
                try:
                    await self._announce_result(
                        task_id, label, task, f"Error: {e}", origin, "error", origin_message_id
                    )
                except Exception:
                    logger.exception("Failed to announce subagent failure result")

    async def _run_subagent_direct(
        self,
        task_id: str,
        task: str,
        label: str,
        origin: dict[str, str],
        status: SubagentStatus,
        temperature: float | None = None,
        workspace_scope: WorkspaceScope | None = None,
        system_prompt_override: str | None = None,
        model_override: str | None = None,
        tools_whitelist: list[str] | None = None,
    ) -> tuple[str, str]:
        """Run subagent and return (status, content). No bus announcement.

        Two upgrades vs. ``_run_subagent``:
          * Activity streaming — the hook is constructed with ``bus`` and the
            parent origin so tool/reasoning events are forwarded to clients
            as ``_subagent_activity`` outbound frames.
          * Session isolation — the runner gets a namespaced session key
            (``{channel}:{chat_id}#sub:{task_id}``) so the subagent's
            consolidation history stays independent of the parent session.
            The parent session key is still used for the LLM wall-timeout
            lookup so per-session limits continue to apply.
        """
        logger.info("Subagent [{}] starting (direct): {}", task_id, label)

        async def _on_checkpoint(payload: dict) -> None:
            status.phase = payload.get("phase", status.phase)
            status.iteration = payload.get("iteration", status.iteration)

        try:
            root = workspace_scope.project_path if workspace_scope is not None else self.workspace
            cfg = None
            if workspace_scope is not None:
                cfg = self._subagent_tools_config()
                cfg.restrict_to_workspace = workspace_scope.restrict_to_workspace
            tools = self._build_tools(workspace=root, tools_config=cfg)
            if tools_whitelist is not None:
                tools = self._filter_tools(tools, tools_whitelist)
            if system_prompt_override:
                system_prompt = system_prompt_override
            else:
                system_prompt = self._build_subagent_prompt(workspace=root)
            use_model = model_override or self.model
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task},
            ]
            # Parent session key drives per-session concerns (LLM wall timeout,
            # cancel_by_session tracking). The subagent's own consolidation
            # history uses a namespaced key so it cannot collide with — or
            # mutate — the parent's session cursor.
            parent_session_key = origin.get("session_key")
            llm_timeout = (
                self._llm_wall_timeout_for_session(parent_session_key)
                if self._llm_wall_timeout_for_session
                else None
            )
            sub_session_key = make_session_key(
                origin["channel"],
                origin["chat_id"],
                f"sub:{task_id}",
            )
            origin_channel = origin.get("channel")
            origin_chat_id = origin.get("chat_id")
            hook = _SubagentHook(
                task_id,
                status,
                bus=self.bus,
                origin_channel=origin_channel,
                origin_chat_id=origin_chat_id,
            )
            token = bind_workspace_scope(workspace_scope) if workspace_scope is not None else None
            # 子代理运行时 depth+1，使 delegate tool 能检测递归深度。
            depth_token = _current_depth.set(_current_depth.get() + 1)
            try:
                result = await self.runner.run(
                    AgentRunSpec(
                        initial_messages=messages,
                        tools=tools,
                        model=use_model,
                        temperature=temperature,
                        max_iterations=self.max_iterations,
                        max_tool_result_chars=self.max_tool_result_chars,
                        hook=hook,
                        max_iterations_message="Task completed but no final response was generated.",
                        error_message=None,
                        fail_on_tool_error=True,
                        checkpoint_callback=_on_checkpoint,
                        session_key=sub_session_key,
                        workspace=root,
                        llm_timeout_s=llm_timeout,
                        turn_budget=(
                            self._turn_budget_factory()
                            if self._turn_budget_factory is not None
                            else None
                        ),
                        high_risk_policy=self._high_risk_policy,
                    )
                )
            finally:
                _current_depth.reset(depth_token)
                if token is not None:
                    reset_workspace_scope(token)
            status.phase = "done"
            status.stop_reason = result.stop_reason

            if result.stop_reason == "tool_error":
                return "error", self._format_partial_progress(result)
            elif result.stop_reason == "error":
                return "error", result.error or "Error: subagent execution failed."
            else:
                final = (
                    result.final_content or "Task completed but no final response was generated."
                )
                logger.info("Subagent [{}] completed (direct)", task_id)
                return "ok", final

        except Exception as e:
            status.phase = "error"
            status.error = str(e)
            logger.exception("Subagent [{}] failed (direct)", task_id)
            return "error", f"Error: {e}"

    async def _announce_result(
        self,
        task_id: str,
        label: str,
        task: str,
        result: str,
        origin: dict[str, str],
        status: str,
        origin_message_id: str | None = None,
    ) -> None:
        """Announce the subagent result to the main agent via the message bus."""
        status_text = "completed successfully" if status == "ok" else "failed"

        announce_content = render_template(
            "agent/subagent_announce.md",
            label=label,
            status_text=status_text,
            task=task,
            result=result,
        )

        # Inject as system message to trigger main agent.
        # Use session_key_override to align with the main agent's effective
        # session key (which accounts for unified sessions) so the result is
        # routed to the correct pending queue (mid-turn injection) instead of
        # being dispatched as a competing independent task.
        override = origin.get("session_key") or f"{origin['channel']}:{origin['chat_id']}"
        metadata: dict[str, Any] = {
            "injected_event": "subagent_result",
            "subagent_task_id": task_id,
        }
        if origin_message_id:
            metadata["origin_message_id"] = origin_message_id
        msg = InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id=f"{origin['channel']}:{origin['chat_id']}",
            content=announce_content,
            session_key_override=override,
            metadata=metadata,
        )

        await self.bus.publish_inbound(msg)
        logger.debug(
            "Subagent [{}] announced result to {}:{}", task_id, origin["channel"], origin["chat_id"]
        )

    @staticmethod
    def _format_partial_progress(result) -> str:
        completed = [e for e in result.tool_events if e["status"] == "ok"]
        failure = next((e for e in reversed(result.tool_events) if e["status"] == "error"), None)
        lines: list[str] = []
        if completed:
            lines.append("Completed steps:")
            for event in completed[-3:]:
                lines.append(f"- {event['name']}: {event['detail']}")
        if failure:
            if lines:
                lines.append("")
            lines.append("Failure:")
            lines.append(f"- {failure['name']}: {failure['detail']}")
        if result.error and not failure:
            if lines:
                lines.append("")
            lines.append("Failure:")
            lines.append(f"- {result.error}")
        return "\n".join(lines) or (result.error or "Error: subagent execution failed.")

    def _build_subagent_prompt(self, workspace: Path | None = None) -> str:
        """Build a focused system prompt for the subagent."""
        from miniunicorn.agent.context import ContextBuilder
        from miniunicorn.agent.skills import SkillsLoader

        time_ctx = ContextBuilder._build_runtime_context(None, None)
        root = workspace or self.workspace
        skills_summary = SkillsLoader(
            root,
            disabled_skills=self.disabled_skills,
        ).build_skills_summary()
        return render_template(
            "agent/subagent_system.md",
            time_ctx=time_ctx,
            workspace=str(root),
            skills_summary=skills_summary or "",
        )

    async def cancel_by_session(self, session_key: str) -> int:
        """Cancel all subagents for the given session. Returns count cancelled."""
        tasks = [
            self._running_tasks[tid]
            for tid in self._session_tasks.get(session_key, [])
            if tid in self._running_tasks and not self._running_tasks[tid].done()
        ]
        for t in tasks:
            t.cancel()
        # gather 加超时，避免某个子代理清理卡住导致 /stop 长时间无响应
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=10.0)
        return len(tasks)

    def get_running_count(self) -> int:
        """Return the number of currently running subagents."""
        return len(self._running_tasks)

    def get_running_count_by_session(self, session_key: str) -> int:
        """Return the number of currently running subagents for a session."""
        tids = self._session_tasks.get(session_key, set())
        return sum(
            1 for tid in tids if tid in self._running_tasks and not self._running_tasks[tid].done()
        )
