"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import dataclasses
import os
import time
from contextlib import AsyncExitStack, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from miniunicorn.agent import context as agent_context
from miniunicorn.agent import model_presets as preset_helpers
from miniunicorn.agent._mcp_lifecycle import McpLifecycleMixin
from miniunicorn.agent._provider_switching import ProviderSwitchingMixin
from miniunicorn.agent._state_machine import (
    StateMixin,
    StateTraceEntry,
    TurnContext,
    TurnState,
)
from miniunicorn.agent.agent_run_adapter import AgentRunAdapter
from miniunicorn.agent.autocompact import AutoCompact
from miniunicorn.agent.context import ContextBuilder
from miniunicorn.agent.hook import AgentHook
from miniunicorn.agent.memory import Consolidator, Dream
from miniunicorn.agent.runner import AgentRunner
from miniunicorn.agent.subagent import SubagentManager
from miniunicorn.agent.subagent_registry import SubagentDefinition, SubagentRegistry
from miniunicorn.agent.telemetry import (
    LogTelemetrySink,
    TelemetrySink,
    build_turn_telemetry,
)
from miniunicorn.agent.tools.context import RequestContext
from miniunicorn.agent.tools.file_state import FileStateStore
from miniunicorn.agent.tools.message import MessageTool
from miniunicorn.agent.tools.registry import ToolRegistry
from miniunicorn.agent.turn_coordinator import TurnCoordinator
from miniunicorn.agent.turn_executor import TURN_TRANSITIONS, TurnExecutor
from miniunicorn.agent.turn_runtime import (
    AgentLoopRunResult,
    ProcessedTurn,
    complete_turn_runtime,
    current_turn_runtime,
)
from miniunicorn.bus.events import InboundMessage, OutboundMessage, make_session_key
from miniunicorn.bus.queue import MessageBus
from miniunicorn.command import CommandContext, CommandRouter, register_builtin_commands
from miniunicorn.config.schema import AgentDefaults, ModelPresetConfig
from miniunicorn.providers.base import LLMProvider
from miniunicorn.providers.factory import ProviderSnapshot
from miniunicorn.security.workspace_access import WorkspaceScopeResolver
from miniunicorn.session.goal_state import (
    runner_wall_llm_timeout_s,
)
from miniunicorn.session.manager import Session, SessionManager
from miniunicorn.session.webui_turns import (
    WebuiTurnCoordinator,
    build_bus_progress_callback,
)
from miniunicorn.utils.document import extract_documents  # re-export for tests/extensions
from miniunicorn.utils.helpers import image_placeholder_text
from miniunicorn.utils.helpers import truncate_text as truncate_text_fn
from miniunicorn.utils.llm_runtime import LLMRuntime
from miniunicorn.utils.task_supervisor import TaskSupervisor

if TYPE_CHECKING:
    from miniunicorn.config.schema import (
        ChannelsConfig,
        ToolsConfig,
    )
    from miniunicorn.cron.service import CronService


UNIFIED_SESSION_KEY = "unified:default"


class AgentLoop(StateMixin, ProviderSwitchingMixin, McpLifecycleMixin):
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """

    @property
    def current_iteration(self) -> int:
        return self._current_iteration

    @property
    def _current_iteration(self) -> int:
        """Read-only iteration count from the bound TurnRuntime."""
        runtime = current_turn_runtime()
        return runtime.iteration if runtime is not None else 0

    @property
    def _last_usage(self) -> dict[str, int]:
        """Read-only cumulative usage from the bound TurnRuntime."""
        runtime = current_turn_runtime()
        return dict(runtime.usage) if runtime is not None else {}

    @staticmethod
    def _record_turn_iteration(iteration: int) -> None:
        """Record the current iteration on the bound TurnRuntime.

        Called by the runner's progress hook after each iteration so
        self-inspection during a running turn sees the live value.
        """
        runtime = current_turn_runtime()
        if runtime is not None:
            runtime.iteration = iteration

    @property
    def tool_names(self) -> list[str]:
        return self.tools.tool_names

    def llm_runtime(self) -> LLMRuntime:
        """Return the current provider/model pair owned by this loop."""
        self._refresh_provider_snapshot()
        return LLMRuntime(self.provider, self.model)

    def _build_turn_budget(self):
        """Construct a fresh TurnBudget for one turn, or None to disable.

        Returns None (legacy unbounded behavior) unless either
        max_input_tokens_per_turn or max_cost_per_turn_usd is set in config.
        When set, only the configured dimensions are capped; unset dimensions
        default to None (unlimited) inside TurnBudget.
        """
        if self._max_input_tokens_per_turn is None and self._max_cost_per_turn_usd is None:
            return None
        from miniunicorn.agent.turn_budget import TurnBudget

        return TurnBudget(
            max_input_tokens=self._max_input_tokens_per_turn,
            max_output_tokens=None,
            max_cost_usd=self._max_cost_per_turn_usd,
        )

    _RUNTIME_CHECKPOINT_KEY = "runtime_checkpoint"
    _PENDING_USER_TURN_KEY = "pending_user_turn"

    # Event-driven state transition table.
    # The canonical table lives in ``turn_executor.TURN_TRANSITIONS``; this
    # alias preserves the legacy ``AgentLoop._TRANSITIONS`` seam used by tests
    # and extensions. It is the same object, not a mutable copy.
    _TRANSITIONS = TURN_TRANSITIONS

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int | None = None,
        max_concurrent_subagents: int | None = None,
        max_subagent_recursion_depth: int | None = None,
        context_window_tokens: int | None = None,
        context_block_limit: int | None = None,
        max_tool_result_chars: int | None = None,
        provider_retry_mode: str = "standard",
        tool_hint_max_length: int | None = None,
        cron_service: CronService | None = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        mcp_servers: dict | None = None,
        channels_config: ChannelsConfig | None = None,
        timezone: str | None = None,
        session_ttl_minutes: int = 0,
        consolidation_ratio: float = 0.5,
        max_messages: int = 120,
        vector_recall: bool = False,
        embedding_model: str = "BAAI/bge-small-zh-v1.5",
        hooks: list[AgentHook] | None = None,
        unified_session: bool = False,
        disabled_skills: list[str] | None = None,
        tools_config: ToolsConfig | None = None,
        provider_snapshot_loader: Callable[..., ProviderSnapshot] | None = None,
        provider_signature: tuple[object, ...] | None = None,
        model_presets: dict[str, ModelPresetConfig] | None = None,
        model_preset: str | None = None,
        preset_snapshot_loader: preset_helpers.PresetSnapshotLoader | None = None,
        runtime_model_publisher: Callable[[str, str | None], None] | None = None,
        telemetry_sink: TelemetrySink | None = None,
    ):
        from miniunicorn.config.schema import ToolsConfig

        _tc = tools_config or ToolsConfig()
        defaults = AgentDefaults()
        self.bus = bus
        self.channels_config = channels_config
        self.provider = provider
        self._provider_snapshot_loader = provider_snapshot_loader
        self._preset_snapshot_loader = preset_snapshot_loader
        self._runtime_model_publisher = runtime_model_publisher
        self._provider_signature = provider_signature
        self._default_selection_signature = preset_helpers.default_selection_signature(
            provider_signature
        )
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = (
            max_iterations if max_iterations is not None else defaults.max_tool_iterations
        )
        # Runtime contract: require a resolved positive integer. No network
        # lookup, no cache, no default guess. Configuration-time resolution
        # (HF/ModelScope discovery) must have persisted a concrete value via
        # the model-save handlers before the agent starts.
        from miniunicorn.config.context_window import require_context_window

        self.context_window_tokens = require_context_window(
            self.model,
            context_window_tokens
            if context_window_tokens is not None
            else defaults.context_window_tokens,
        )
        # resolved_context_window_tokens: 后端解析值,用于 /status 等显示场景。
        # Now equal to the validated runtime value (no separate fallback path).
        self.resolved_context_window_tokens = self.context_window_tokens
        self.context_block_limit = context_block_limit
        self.max_tool_result_chars = (
            max_tool_result_chars
            if max_tool_result_chars is not None
            else defaults.max_tool_result_chars
        )
        self.provider_retry_mode = provider_retry_mode
        self.tool_hint_max_length = (
            tool_hint_max_length
            if tool_hint_max_length is not None
            else defaults.tool_hint_max_length
        )
        # Plan-and-Execute / Reflection / TurnBudget defaults (all opt-in).
        # Read from AgentDefaults so users can enable these via YAML config;
        # all default to False/None, preserving legacy behavior.
        self.use_planner = getattr(defaults, "use_planner", False)
        self.planner_model = getattr(defaults, "planner_model", None)
        self.planner_max_replans = getattr(defaults, "planner_max_replans", 3)
        self.enable_reflection = getattr(defaults, "enable_reflection", False)
        self.reflection_interval = getattr(defaults, "reflection_interval", 5)
        self._max_input_tokens_per_turn = getattr(defaults, "max_input_tokens_per_turn", None)
        self._max_cost_per_turn_usd = getattr(defaults, "max_cost_per_turn_usd", None)
        self.tools_config = _tc
        self.web_config = _tc.web
        self.exec_config = _tc.exec
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace
        self.workspace_scopes = WorkspaceScopeResolver(
            default_workspace=workspace,
            default_restrict_to_workspace=restrict_to_workspace,
        )
        self._start_time = time.time()
        self._extra_hooks: list[AgentHook] = hooks or []

        self.context = ContextBuilder(workspace, timezone=timezone, disabled_skills=disabled_skills)
        self.sessions = session_manager or SessionManager(workspace)
        self._webui_turns = WebuiTurnCoordinator(
            bus=self.bus,
            sessions=self.sessions,
            schedule_background=lambda coro: self._schedule_background(coro),
        )
        self.tools = ToolRegistry()
        # One file-read/write tracker per logical session. The tool registry is
        # shared by this loop, so tools resolve the active state via contextvars.
        self._file_state_store = FileStateStore()
        self.runner = AgentRunner(provider)
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            tools_config=_tc,
            max_tool_result_chars=self.max_tool_result_chars,
            restrict_to_workspace=restrict_to_workspace,
            disabled_skills=disabled_skills,
            max_iterations=self.max_iterations,
            max_concurrent_subagents=max_concurrent_subagents,
            llm_wall_timeout_for_session=lambda sk: runner_wall_llm_timeout_s(self.sessions, sk),
            max_subagent_recursion_depth=max_subagent_recursion_depth,
        )
        # Declarative subagent registry (TRAE-style .md definitions in agents/).
        # Loaded once at startup; empty when no agents/ dir exists.
        self.subagent_registry = SubagentRegistry(workspace)
        self.subagent_registry.load()
        self.context.subagent_registry = self.subagent_registry
        self._unified_session = unified_session
        self._max_messages = max_messages if max_messages > 0 else 120
        self._running = False
        self._mcp_servers = mcp_servers or {}
        self._mcp_stacks: dict[str, AsyncExitStack] = {}
        self._mcp_connected = False
        self._mcp_connecting = False
        self._active_tasks: dict[str, list[asyncio.Task]] = {}  # session_key -> tasks
        # Supervised fire-and-forget background jobs (archives, consolidation,
        # etc.). The supervisor owns strong references and surfaces unhandled
        # exceptions via a done-callback so nothing fails silently.
        self._background_supervisor: TaskSupervisor = TaskSupervisor()
        # Per-session pending queues for mid-turn message injection.
        # When a session has an active task, new messages for that session
        # are routed here instead of creating a new task.
        self._pending_queues: dict[str, asyncio.Queue] = {}
        # MINIUNICORN_MAX_CONCURRENT_REQUESTS: <=0 means unlimited; default 3.
        _max = int(os.environ.get("MINIUNICORN_MAX_CONCURRENT_REQUESTS", "3"))
        # TurnCoordinator owns per-session locks (weakly held) and the global
        # concurrency semaphore. Lock acquisition precedes semaphore
        # acquisition so a task waiting on its session lock cannot consume a
        # global permit. Every turn entry point (_dispatch and process_direct)
        # routes through coordinator.scope to bind a TurnRuntime for the turn.
        self._turn_coordinator = TurnCoordinator(max_concurrent_requests=_max)
        # Read-only compatibility alias for code that inspects session locks
        # (e.g. /stop introspection). Mutations of this dict still flow
        # through the coordinator.
        self._session_locks = self._turn_coordinator.session_locks
        # TurnExecutor owns the single-turn state-machine driver. It sees the
        # host through a narrow protocol and never imports AgentLoop at runtime.
        self._turn_executor = TurnExecutor(self)
        # AgentRunAdapter is the single thick adapter between the loop and
        # AgentRunner. The loop delegates _run_agent_loop here so the runner
        # invocation logic can stay out of the facade body.
        self._agent_run_adapter = AgentRunAdapter(self)
        # Telemetry sink: one structured record per turn. Defaults to
        # LogTelemetrySink (Loguru ``turn_completed`` event). Sink exceptions
        # are logged and suppressed so telemetry can never break a turn.
        self.telemetry_sink: TelemetrySink = telemetry_sink or LogTelemetrySink()
        self.consolidator = Consolidator(
            store=self.context.memory,
            provider=provider,
            model=self.model,
            sessions=self.sessions,
            context_window_tokens=self.context_window_tokens,
            build_messages=self.context.build_messages,
            get_tool_definitions=self.tools.get_definitions,
            max_completion_tokens=provider.generation.max_tokens,
            consolidation_ratio=consolidation_ratio,
        )
        self.auto_compact = AutoCompact(
            sessions=self.sessions,
            consolidator=self.consolidator,
            session_ttl_minutes=session_ttl_minutes,
        )
        self.dream = Dream(
            store=self.context.memory,
            provider=provider,
            model=self.model,
            max_batch_size=defaults.dream.max_batch_size,
            max_iterations=defaults.dream.max_iterations,
            annotate_line_ages=defaults.dream.annotate_line_ages,
        )
        # Dream 空闲触发器：用户停用时后台触发 Dream，不依赖 cron 定时。
        # 解决"用户不 24h 运行 gateway，凌晨 cron 点大概率关机"的问题。
        # gateway 启动时由 _gateway_runner 从 DreamConfig 同步配置。
        from miniunicorn.agent.dream_trigger import DreamIdleTrigger

        self.dream_idle_trigger = DreamIdleTrigger(
            self.dream,
            enabled=defaults.dream.idle_trigger_enabled,
            min_idle_seconds=defaults.dream.idle_trigger_min_seconds,
            min_entries=defaults.dream.idle_trigger_min_entries,
            min_interval_s=defaults.dream.idle_trigger_min_interval_s,
        )
        # Attach vector store to memory if enabled (optional sqlite-vec dependency).
        # Vector memory uses a dedicated local CPU embedding provider
        # (FastEmbed/BGE) that is independent of the chat LLM provider.
        # Runtime chat-provider switching never touches ``_embedding_provider``.
        self._vector_recall = vector_recall
        self._embedding_model = embedding_model
        self._embedding_provider = None
        if vector_recall:
            from miniunicorn.agent.vector_memory import create_vector_store
            from miniunicorn.providers.local_embedding import LocalEmbeddingProvider

            embedding_provider = LocalEmbeddingProvider(model_name=embedding_model)
            vector_store = create_vector_store(
                self.workspace / "memory" / "memory.db",
                embedding_dim=embedding_provider.dimension,
                model_id=embedding_provider.model_name,
            )
            self.context.memory.attach_vector_store(vector_store)
            # MemoryStore.index_text and the recall tool read the provider
            # back via MemoryStore._embed_provider, so hand them the same
            # local instance rather than the chat provider.
            self.context.memory.set_embed_provider(embedding_provider, model=embedding_model)
            self._embedding_provider = embedding_provider
        self.model_presets: dict[str, ModelPresetConfig] = model_presets or {}
        self._active_preset: str | None = None
        if model_preset:
            self.set_model_preset(model_preset, publish_update=False)
        self._register_default_tools()
        self._runtime_vars: dict[str, Any] = {}
        self.commands = CommandRouter()
        register_builtin_commands(self.commands)

    @classmethod
    def from_config(
        cls,
        config: Any,
        bus: MessageBus | None = None,
        **extra: Any,
    ) -> AgentLoop:
        """Create an AgentLoop from config with the common parameter set.

        Extra keyword arguments are forwarded to ``AgentLoop.__init__``,
        allowing callers to override or extend the standard config-derived
        parameters (e.g. ``cron_service``, ``session_manager``).

        内部委托给 ``AgentLoopBuilder.from_config`` 以保持单一的参数解析路径。
        """
        from miniunicorn.agent.loop_builder import AgentLoopBuilder

        return AgentLoopBuilder.from_config(config, bus=bus, **extra).build()

    def _set_tool_context(
        self,
        channel: str,
        chat_id: str,
        message_id: str | None = None,
        metadata: dict | None = None,
        session_key: str | None = None,
    ) -> None:
        """Update context for all tools that need routing info."""
        from miniunicorn.agent.tools.context import ContextAware

        if session_key is not None:
            effective_key = session_key
        elif self._unified_session:
            effective_key = UNIFIED_SESSION_KEY
        else:
            effective_key = f"{channel}:{chat_id}"

        request_ctx = RequestContext(
            channel=channel,
            chat_id=chat_id,
            message_id=message_id,
            session_key=effective_key,
            metadata=dict(metadata or {}),
        )

        for name in self.tools.tool_names:
            tool = self.tools.get(name)
            if tool and isinstance(tool, ContextAware):
                tool.set_context(request_ctx)

    @staticmethod
    def _runtime_chat_id(msg: InboundMessage) -> str:
        """Return the chat id shown in runtime metadata for the model."""
        return str(msg.metadata.get("context_chat_id") or msg.chat_id)

    async def _build_bus_progress_callback(
        self, msg: InboundMessage
    ) -> Callable[..., Awaitable[None]]:
        """Build a progress callback that publishes to the message bus."""
        return build_bus_progress_callback(self.bus, msg)

    async def _build_retry_wait_callback(
        self, msg: InboundMessage
    ) -> Callable[[str], Awaitable[None]]:
        """Build a retry-wait callback that publishes to the message bus."""

        async def _on_retry_wait(content: str) -> None:
            meta = dict(msg.metadata or {})
            meta["_retry_wait"] = True
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=content,
                    metadata=meta,
                )
            )

        return _on_retry_wait

    def _persist_user_message_early(
        self,
        msg: InboundMessage,
        session: Session,
        **kwargs: Any,
    ) -> bool:
        """Persist the triggering user message before the turn starts.

        Returns True if the message was persisted.
        """
        media_paths = [p for p in (msg.media or []) if isinstance(p, str) and p]
        has_text = isinstance(msg.content, str) and msg.content.strip()
        if has_text or media_paths:
            extra: dict[str, Any] = (
                {"media": list(media_paths)} if media_paths else {}
            ) | agent_context.session_extra(msg.metadata)
            extra.update(kwargs)
            text = msg.content if isinstance(msg.content, str) else ""
            session.add_message("user", text, **extra)
            self._mark_pending_user_turn(session)
            self.sessions.save(session)
            return True
        return False

    async def _compute_query_embedding(self, text: str) -> list[float] | None:
        """Compute embedding for *text* when vector recall is enabled.

        Uses the dedicated local embedding provider, never the chat provider,
        so switching chat providers mid-session does not perturb recall.
        """
        if not self._vector_recall or not text:
            return None
        provider = self._embedding_provider
        if provider is None:
            return None
        try:
            embeddings = await provider.embed(
                [text[:500]],
                model=self._embedding_model,
            )
            if embeddings:
                return embeddings[0]
        except Exception:
            logger.debug("Query embedding failed", exc_info=True)
        return None

    async def _build_initial_messages(
        self,
        msg: InboundMessage,
        session: Session,
        history: list[dict[str, Any]],
        pending_summary: str | None,
        agent_override: SubagentDefinition | None = None,
    ) -> list[dict[str, Any]]:
        """Build the initial message list for the LLM turn."""
        scope = self.workspace_scopes.for_message(msg, session.metadata)
        query_embedding = await self._compute_query_embedding(msg.content)
        return self.context.build_messages(
            history=history,
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=msg.channel,
            chat_id=self._runtime_chat_id(msg),
            sender_id=msg.sender_id,
            session_summary=pending_summary,
            session_metadata=session.metadata,
            workspace=scope.project_path,
            runtime_state=self,
            inbound_message=msg,
            query_embedding=query_embedding,
            vector_recall=self._vector_recall,
            agent_override=agent_override,
        )

    async def _dispatch_command_inline(
        self,
        msg: InboundMessage,
        key: str,
        raw: str,
        dispatch_fn: Callable[[CommandContext], Awaitable[OutboundMessage | None]],
    ) -> None:
        """Dispatch a command directly from the run() loop and publish the result."""
        ctx = CommandContext(msg=msg, session=None, key=key, raw=raw, loop=self)
        result = await dispatch_fn(ctx)
        if result:
            await self.bus.publish_outbound(result)
        else:
            logger.warning("Command '{}' matched but dispatch returned None", raw)

    async def _cancel_active_tasks(self, key: str) -> int:
        """Cancel and await all active tasks and subagents for *key*.

        Returns the total number of cancelled tasks + subagents.
        """
        tasks = self._active_tasks.pop(key, [])
        cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
        # 为每个任务等待设置超时，避免 /stop 因单个任务卡死而长时间阻塞
        for t in tasks:
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(t, timeout=10.0)
        # subagents 取消也加超时保护，超时则返回已取消的部分
        try:
            sub_cancelled = await asyncio.wait_for(
                self.subagents.cancel_by_session(key), timeout=10.0
            )
        except asyncio.TimeoutError:
            sub_cancelled = 0
        return cancelled + sub_cancelled

    def _effective_session_key(self, msg: InboundMessage) -> str:
        """Return the session key used for task routing and mid-turn injections."""
        if msg.session_key_override:
            return msg.session_key_override
        # When a subagent is manually selected via @, isolate its session
        # history under a namespaced key so it does not collide with the
        # parent (main agent) session.
        agent_id = msg.metadata.get("agent_id") if msg.metadata else None
        if agent_id:
            return make_session_key(msg.channel, msg.chat_id, agent_id=agent_id)
        if self._unified_session:
            return UNIFIED_SESSION_KEY
        return msg.session_key

    def _resolve_agent_override(self, msg: InboundMessage) -> SubagentDefinition | None:
        """Resolve a SubagentDefinition from ``msg.metadata.agent_id`` if present.

        Returns None when no agent_id is set or the named subagent is not
        registered (in which case the default agent identity is used).
        """
        agent_id = msg.metadata.get("agent_id") if msg.metadata else None
        if not agent_id:
            return None
        defn = self.subagent_registry.get(agent_id)
        if defn is None:
            logger.warning(
                "agent_id '{}' from metadata not found in subagent registry; "
                "falling back to default identity",
                agent_id,
            )
            return None
        return defn

    def _filter_tools_for_override(self, whitelist: list[str]) -> ToolRegistry:
        """Return a filtered copy of the loop's tool registry by whitelist."""
        filtered = ToolRegistry()
        for name in whitelist:
            tool = self.tools.get(name)
            if tool is not None:
                filtered.register(tool)
        return filtered

    def _replay_token_budget(self) -> int:
        """Derive a token budget for session history replay from the context window."""
        if self.context_window_tokens <= 0:
            return 0
        max_output = getattr(getattr(self.provider, "generation", None), "max_tokens", 4096)
        try:
            reserved_output = int(max_output)
        except (TypeError, ValueError):
            reserved_output = 4096
        budget = self.context_window_tokens - max(1, reserved_output) - 1024
        return budget if budget > 0 else max(128, self.context_window_tokens // 2)

    async def _run_agent_loop(
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

        Delegates to :class:`AgentRunAdapter`. Existing monkeypatches of this
        method continue to intercept calls because state handlers and
        ``TurnExecutor`` call ``self._run_agent_loop`` through the host.
        """
        return await self._agent_run_adapter.run(
            initial_messages,
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            on_retry_wait=on_retry_wait,
            session=session,
            channel=channel,
            chat_id=chat_id,
            message_id=message_id,
            metadata=metadata,
            session_key=session_key,
            pending_queue=pending_queue,
            agent_override=agent_override,
            turn_hooks=turn_hooks,
        )

    async def run(self) -> None:
        """Run the agent loop, dispatching messages as tasks to stay responsive to /stop."""
        self._running = True
        await self._connect_mcp()
        logger.info("Agent loop started")

        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                self.auto_compact.check_expired(
                    self._schedule_background,
                    active_session_keys=self._pending_queues.keys(),
                )
                # Dream 空闲触发：无活跃会话且有积压数据时后台触发 Dream
                await self.dream_idle_trigger.maybe_trigger(
                    active_session_keys=self._pending_queues.keys(),
                )
                continue
            except asyncio.CancelledError:
                # Preserve real task cancellation so shutdown can complete cleanly.
                # Only ignore non-task CancelledError signals that may leak from integrations.
                # 兼容写法：低版本 Python 没有 Task.cancelling()
                _task = asyncio.current_task()
                _cancelling = getattr(_task, "cancelling", lambda: 0)() if _task else 0
                if not self._running or _cancelling:
                    raise
                continue
            except Exception as e:
                logger.warning("Error consuming inbound message: {}, continuing...", e)
                continue

            raw = msg.content.strip()
            # 标记用户有新活动，重置 Dream 空闲触发器的空闲计时器
            self.dream_idle_trigger.notify_user_activity()
            effective_key = self._effective_session_key(msg)
            if await agent_context.handle_runtime_control(self, msg, self.tools):
                continue
            if self.commands.is_priority(raw):
                await self._dispatch_command_inline(
                    msg,
                    effective_key,
                    raw,
                    self.commands.dispatch_priority,
                )
                continue
            # If this session already has an active pending queue (i.e. a task
            # is processing this session), route the message there for mid-turn
            # injection instead of creating a competing task.
            if effective_key in self._pending_queues:
                # Non-priority commands must not be queued for injection;
                # dispatch them directly (same pattern as priority commands).
                if self.commands.is_dispatchable_command(raw):
                    await self._dispatch_command_inline(
                        msg,
                        effective_key,
                        raw,
                        self.commands.dispatch,
                    )
                    continue
                pending_msg = msg
                if effective_key != msg.session_key:
                    pending_msg = dataclasses.replace(
                        msg,
                        session_key_override=effective_key,
                    )
                try:
                    self._pending_queues[effective_key].put_nowait(pending_msg)
                except asyncio.QueueFull:
                    logger.warning(
                        "Pending queue full for session {}, falling back to queued task",
                        effective_key,
                    )
                else:
                    logger.info(
                        "Routed follow-up message to pending queue for session {}",
                        effective_key,
                    )
                    continue
            # Compute the effective session key before dispatching
            # This ensures /stop command can find tasks correctly when unified session is enabled
            task = asyncio.create_task(self._dispatch(msg))
            self._active_tasks.setdefault(effective_key, []).append(task)
            task.add_done_callback(
                lambda t, k=effective_key: (
                    self._active_tasks.get(k, []) and self._active_tasks[k].remove(t)
                    if t in self._active_tasks.get(k, [])
                    else None
                )
            )

    async def _dispatch(self, msg: InboundMessage) -> None:
        """Process a message: per-session serial, cross-session concurrent."""
        session_key = self._effective_session_key(msg)
        if session_key != msg.session_key:
            msg = dataclasses.replace(msg, session_key_override=session_key)
        # TurnCoordinator owns the per-session lock (weakly held so idle
        # sessions are GC'd) and the global concurrency semaphore. Lock
        # acquisition precedes semaphore acquisition inside ``scope`` so a
        # task waiting on its session lock cannot consume a global permit.
        # The same coordinator is shared with ``process_direct`` so bus and
        # direct entry points serialize against the same per-session lock.
        pending: asyncio.Queue | None = None
        try:
            async with self._turn_coordinator.scope(session_key) as turn_runtime:
                # Only the task that owns the session lock may publish the
                # active mid-turn injection queue for this session.
                pending = asyncio.Queue(maxsize=20)
                self._pending_queues[session_key] = pending
                try:
                    on_stream = on_stream_end = None
                    if msg.metadata.get("_wants_stream"):
                        # Split one answer into distinct stream segments.
                        stream_base_id = f"{msg.session_key}:{time.time_ns()}"
                        stream_segment = 0

                        def _current_stream_id() -> str:
                            return f"{stream_base_id}:{stream_segment}"

                        async def on_stream(delta: str) -> None:
                            meta = dict(msg.metadata or {})
                            meta["_stream_delta"] = True
                            meta["_stream_id"] = _current_stream_id()
                            await self.bus.publish_outbound(
                                OutboundMessage(
                                    channel=msg.channel,
                                    chat_id=msg.chat_id,
                                    content=delta,
                                    metadata=meta,
                                )
                            )

                        async def on_stream_end(*, resuming: bool = False) -> None:
                            nonlocal stream_segment
                            meta = dict(msg.metadata or {})
                            meta["_stream_end"] = True
                            meta["_resuming"] = resuming
                            meta["_stream_id"] = _current_stream_id()
                            await self.bus.publish_outbound(
                                OutboundMessage(
                                    channel=msg.channel,
                                    chat_id=msg.chat_id,
                                    content="",
                                    metadata=meta,
                                )
                            )
                            stream_segment += 1

                    result = await self._execute_message(
                        msg,
                        on_stream=on_stream,
                        on_stream_end=on_stream_end,
                        pending_queue=pending,
                    )
                    # Copy cumulative usage/latency from the completed turn
                    # context into the bound TurnRuntime so telemetry and
                    # turn-end reads see this turn's metrics only.
                    complete_turn_runtime(turn_runtime, result.context)
                    response = result.outbound
                    if response is not None:
                        await self.bus.publish_outbound(response)
                    elif msg.channel == "cli":
                        await self.bus.publish_outbound(
                            OutboundMessage(
                                channel=msg.channel,
                                chat_id=msg.chat_id,
                                content="",
                                metadata=msg.metadata or {},
                            )
                        )
                    if msg.channel == "websocket":
                        await self._webui_turns.handle_turn_end(
                            msg,
                            session_key=session_key,
                            latency_ms=turn_runtime.latency_ms,
                            context_usage=turn_runtime.last_call_usage,
                        )
                    await self._emit_telemetry(turn_runtime)
                except asyncio.CancelledError:
                    logger.info("Task cancelled for session {}", session_key)
                    # Preserve partial context from the interrupted turn so
                    # the user does not lose tool results and assistant
                    # messages accumulated before /stop.  The checkpoint was
                    # already persisted to session metadata by
                    # _emit_checkpoint during tool execution; materializing
                    # it into session history now makes it visible in the
                    # next conversation turn.
                    try:
                        key = self._effective_session_key(msg)
                        session = self.sessions.get_or_create(key)
                        if self._restore_runtime_checkpoint(session):
                            self._clear_pending_user_turn(session)
                            self.sessions.save(session)
                            logger.info(
                                "Restored partial context for cancelled session {}",
                                key,
                            )
                    except Exception:
                        logger.debug(
                            "Could not restore checkpoint for cancelled session {}",
                            session_key,
                            exc_info=True,
                        )
                    # Emit a telemetry record with stop_reason="cancelled"
                    # before re-raising so the turn is still observable.
                    turn_runtime.stop_reason = "cancelled"
                    await self._emit_telemetry(turn_runtime)
                    raise
                except Exception:
                    logger.exception("Error processing message for session {}", session_key)
                    await self.bus.publish_outbound(
                        OutboundMessage(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            content="Sorry, I encountered an error.",
                        )
                    )
                    if not turn_runtime.stop_reason:
                        turn_runtime.stop_reason = "error"
                    await self._emit_telemetry(turn_runtime)
                finally:
                    # Drain any messages still in the pending queue and re-publish
                    # them to the bus so they are processed as fresh inbound messages
                    # rather than silently lost.  Only remove our own queue; a
                    # later task waiting on the lock must not be able to steal
                    # cleanup ownership.
                    queue = None
                    if self._pending_queues.get(session_key) is pending:
                        queue = self._pending_queues.pop(session_key, None)
                    else:
                        queue = pending
                    if queue is not None:
                        leftover = 0
                        while True:
                            try:
                                item = queue.get_nowait()
                            except asyncio.QueueEmpty:
                                break
                            await self.bus.publish_inbound(item)
                            leftover += 1
                        if leftover:
                            logger.info(
                                "Re-published {} leftover message(s) to bus for session {}",
                                leftover,
                                session_key,
                            )
                    await self._webui_turns.publish_run_status(msg, "idle")
                    self._webui_turns.discard(session_key)
        finally:
            if pending is None:
                await self._webui_turns.publish_run_status(msg, "idle")
                self._webui_turns.discard(session_key)

    def _schedule_background(self, coro, *, name: str = "agent-background") -> None:
        """Schedule a coroutine as a tracked background task (drained on shutdown)."""
        self._background_supervisor.create(coro, name=name)

    async def _emit_telemetry(self, turn_runtime) -> None:
        """Emit one structured telemetry record for a completed turn.

        Sink exceptions are logged and suppressed so a telemetry failure
        can never break an outbound turn.
        """
        try:
            await self.telemetry_sink.emit_turn(build_turn_telemetry(turn_runtime))
        except Exception:
            logger.exception("Telemetry sink failed for turn {}", turn_runtime.turn_id)

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")

    async def _process_system_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        pending_queue: asyncio.Queue | None = None,
    ) -> OutboundMessage | None:
        """Process a system inbound message (e.g. subagent announce)."""
        return await self._turn_executor.process_system_message(
            msg,
            session_key=session_key,
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            pending_queue=pending_queue,
        )

    async def _execute_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        pending_queue: asyncio.Queue | None = None,
        turn_hooks: list[AgentHook] | None = None,
    ) -> ProcessedTurn:
        """Execute a single inbound message and return the outbound + context."""
        return await self._turn_executor.execute(
            msg,
            session_key=session_key,
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            pending_queue=pending_queue,
            turn_hooks=turn_hooks,
        )

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        pending_queue: asyncio.Queue | None = None,
        turn_hooks: list[AgentHook] | None = None,
    ) -> OutboundMessage | None:
        """Compatibility wrapper returning only the outbound payload.

        Existing callers that only need the response message can keep using
        this thin wrapper. New internal callers should use ``_execute_message``
        directly so they can copy cumulative metrics into the bound
        ``TurnRuntime`` via :func:`complete_turn_runtime`.
        """
        result = await self._execute_message(
            msg,
            session_key=session_key,
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            pending_queue=pending_queue,
            turn_hooks=turn_hooks,
        )
        return result.outbound

    def _assemble_outbound(
        self,
        msg: InboundMessage,
        final_content: str,
        all_msgs: list[dict[str, Any]],
        stop_reason: str,
        had_injections: bool,
        on_stream: Callable[[str], Awaitable[None]] | None,
        *,
        turn_latency_ms: int | None = None,
    ) -> OutboundMessage | None:
        """Assemble the final outbound message from turn results."""
        # MessageTool suppression
        if (mt := self.tools.get("message")) and isinstance(mt, MessageTool) and mt._sent_in_turn:
            if not had_injections or stop_reason == "empty_final_response":
                return None

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info("Response to {}:{}: {}", msg.channel, msg.sender_id, preview)

        meta = dict(msg.metadata or {})
        if on_stream is not None and stop_reason not in {"error", "tool_error"}:
            meta["_streamed"] = True
        if turn_latency_ms is not None:
            meta["latency_ms"] = int(turn_latency_ms)

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            metadata=meta,
        )

    def _sanitize_persisted_blocks(
        self,
        content: list[dict[str, Any]],
        *,
        should_truncate_text: bool = False,
        drop_runtime: bool = False,
    ) -> list[dict[str, Any]]:
        """Strip volatile multimodal payloads before writing session history."""
        filtered: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                filtered.append(block)
                continue

            if (
                drop_runtime
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
                and block["text"].startswith(ContextBuilder._RUNTIME_CONTEXT_TAG)
            ):
                continue

            if block.get("type") == "image_url" and block.get("image_url", {}).get(
                "url", ""
            ).startswith("data:image/"):
                path = (block.get("_meta") or {}).get("path", "")
                filtered.append({"type": "text", "text": image_placeholder_text(path)})
                continue

            if block.get("type") == "text" and isinstance(block.get("text"), str):
                text = block["text"]
                if should_truncate_text and len(text) > self.max_tool_result_chars:
                    text = truncate_text_fn(text, self.max_tool_result_chars)
                filtered.append({**block, "text": text})
                continue

            filtered.append(block)

        return filtered

    def _save_turn(
        self,
        session: Session,
        messages: list[dict],
        skip: int,
        *,
        turn_latency_ms: int | None = None,
    ) -> None:
        """Save new-turn messages into session, truncating large tool results."""
        from datetime import datetime

        last_assistant_idx: int | None = None
        for m in messages[skip:]:
            entry = dict(m)
            role, content = entry.get("role"), entry.get("content")
            if role == "assistant" and not content and not entry.get("tool_calls"):
                continue  # skip empty assistant messages — they poison session context
            if role == "tool":
                if isinstance(content, str) and len(content) > self.max_tool_result_chars:
                    entry["content"] = truncate_text_fn(content, self.max_tool_result_chars)
                elif isinstance(content, list):
                    filtered = self._sanitize_persisted_blocks(content, should_truncate_text=True)
                    if not filtered:
                        continue
                    entry["content"] = filtered
            elif role == "user":
                if isinstance(content, str) and ContextBuilder._RUNTIME_CONTEXT_TAG in content:
                    # Strip the runtime-context block appended at the end.
                    tag_pos = content.find(ContextBuilder._RUNTIME_CONTEXT_TAG)
                    before = content[:tag_pos].rstrip("\n ")
                    if before:
                        entry["content"] = before
                    else:
                        continue
                if isinstance(content, list):
                    filtered = self._sanitize_persisted_blocks(content, drop_runtime=True)
                    if not filtered:
                        continue
                    entry["content"] = filtered
            entry.setdefault("timestamp", datetime.now().isoformat())
            session.messages.append(entry)
            if role == "assistant":
                last_assistant_idx = len(session.messages) - 1
        if turn_latency_ms is not None and last_assistant_idx is not None:
            session.messages[last_assistant_idx]["latency_ms"] = int(turn_latency_ms)
        session.updated_at = datetime.now()

    def _persist_subagent_followup(self, session: Session, msg: InboundMessage) -> bool:
        """Persist subagent follow-ups before prompt assembly so history stays durable.

        Returns True if a new entry was appended; False if the follow-up was
        deduped (same ``subagent_task_id`` already in session) or carries no
        content worth persisting.
        """
        if not msg.content:
            return False
        task_id = msg.metadata.get("subagent_task_id") if isinstance(msg.metadata, dict) else None
        if task_id and any(
            m.get("injected_event") == "subagent_result" and m.get("subagent_task_id") == task_id
            for m in session.messages
        ):
            return False
        session.add_message(
            "assistant",
            msg.content,
            sender_id=msg.sender_id,
            injected_event="subagent_result",
            subagent_task_id=task_id,
        )
        return True

    def _set_runtime_checkpoint(self, session: Session, payload: dict[str, Any]) -> None:
        """Persist the latest in-flight turn state into session metadata."""
        session.metadata[self._RUNTIME_CHECKPOINT_KEY] = payload
        self.sessions.save(session)

    def _mark_pending_user_turn(self, session: Session) -> None:
        session.metadata[self._PENDING_USER_TURN_KEY] = True

    def _clear_pending_user_turn(self, session: Session) -> None:
        session.metadata.pop(self._PENDING_USER_TURN_KEY, None)

    def _clear_runtime_checkpoint(self, session: Session) -> None:
        if self._RUNTIME_CHECKPOINT_KEY in session.metadata:
            session.metadata.pop(self._RUNTIME_CHECKPOINT_KEY, None)

    @staticmethod
    def _checkpoint_message_key(message: dict[str, Any]) -> tuple[Any, ...]:
        return (
            message.get("role"),
            message.get("content"),
            message.get("tool_call_id"),
            message.get("name"),
            message.get("tool_calls"),
            message.get("reasoning_content"),
            message.get("thinking_blocks"),
        )

    def _restore_runtime_checkpoint(self, session: Session) -> bool:
        """Materialize an unfinished turn into session history before a new request."""
        from datetime import datetime

        checkpoint = session.metadata.get(self._RUNTIME_CHECKPOINT_KEY)
        if not isinstance(checkpoint, dict):
            return False

        assistant_message = checkpoint.get("assistant_message")
        completed_tool_results = checkpoint.get("completed_tool_results") or []
        pending_tool_calls = checkpoint.get("pending_tool_calls") or []

        restored_messages: list[dict[str, Any]] = []
        if isinstance(assistant_message, dict):
            restored = dict(assistant_message)
            restored.setdefault("timestamp", datetime.now().isoformat())
            restored_messages.append(restored)
        for message in completed_tool_results:
            if isinstance(message, dict):
                restored = dict(message)
                restored.setdefault("timestamp", datetime.now().isoformat())
                restored_messages.append(restored)
        for tool_call in pending_tool_calls:
            if not isinstance(tool_call, dict):
                continue
            tool_id = tool_call.get("id")
            name = ((tool_call.get("function") or {}).get("name")) or "tool"
            restored_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "name": name,
                    "content": "Error: Task interrupted before this tool finished.",
                    "timestamp": datetime.now().isoformat(),
                }
            )

        overlap = 0
        max_overlap = min(len(session.messages), len(restored_messages))
        for size in range(max_overlap, 0, -1):
            existing = session.messages[-size:]
            restored = restored_messages[:size]
            if all(
                self._checkpoint_message_key(left) == self._checkpoint_message_key(right)
                for left, right in zip(existing, restored)
            ):
                overlap = size
                break
        session.messages.extend(restored_messages[overlap:])

        self._clear_pending_user_turn(session)
        self._clear_runtime_checkpoint(session)
        return True

    def _restore_pending_user_turn(self, session: Session) -> bool:
        """Close a turn that only persisted the user message before crashing."""
        from datetime import datetime

        if not session.metadata.get(self._PENDING_USER_TURN_KEY):
            return False

        if session.messages and session.messages[-1].get("role") == "user":
            session.messages.append(
                {
                    "role": "assistant",
                    "content": "Error: Task interrupted before a response was generated.",
                    "timestamp": datetime.now().isoformat(),
                }
            )
            session.updated_at = datetime.now()

        self._clear_pending_user_turn(session)
        return True

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        media: list[str] | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        hooks: list[AgentHook] | None = None,
    ) -> OutboundMessage | None:
        """Process a message directly and return the outbound payload.

        *hooks*: per-call lifecycle hooks bound to this single turn only.
        The SDK uses this instead of mutating the loop's shared
        ``_extra_hooks`` list, so concurrent ``process_direct`` calls with
        different hooks no longer cross-contaminate each other's results.

        Direct calls share the same :class:`TurnCoordinator` as bus
        dispatches: same-session ``process_direct`` calls serialize against
        each other and against any concurrent ``_dispatch`` for the same
        effective session key, but they are never transformed into bus
        messages. The coordinator also binds a per-turn
        :class:`~miniunicorn.agent.turn_runtime.TurnRuntime` for the duration
        of this call so concurrent direct turns cannot share mutable state.
        """
        await self._connect_mcp()
        msg = InboundMessage(
            channel=channel,
            sender_id="user",
            chat_id=chat_id,
            content=content,
            media=media or [],
        )
        effective_key = session_key
        async with self._turn_coordinator.scope(effective_key) as turn_runtime:
            try:
                result = await self._execute_message(
                    msg,
                    session_key=effective_key,
                    on_progress=on_progress,
                    on_stream=on_stream,
                    on_stream_end=on_stream_end,
                    turn_hooks=hooks,
                )
                complete_turn_runtime(turn_runtime, result.context)
                await self._emit_telemetry(turn_runtime)
                return result.outbound
            except asyncio.CancelledError:
                turn_runtime.stop_reason = "cancelled"
                await self._emit_telemetry(turn_runtime)
                raise
            except Exception:
                if not turn_runtime.stop_reason:
                    turn_runtime.stop_reason = "error"
                await self._emit_telemetry(turn_runtime)
                raise
            finally:
                # WebUI run-status cleanup mirrors _dispatch: publish the
                # terminal "idle" status and drop cached title context so a
                # later direct call for the same session starts fresh.
                if channel == "websocket":
                    await self._webui_turns.publish_run_status(msg, "idle")
                    self._webui_turns.discard(effective_key)


# Re-export for backwards compatibility (tests/extensions may import from loop)
__all__ = [
    "AgentLoop",
    "StateTraceEntry",
    "TurnContext",
    "TurnState",
    "extract_documents",
]
