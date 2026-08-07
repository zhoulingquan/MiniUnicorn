"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

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
from miniunicorn.agent.tools.registry import ToolRegistry
from miniunicorn.agent.turn_coordinator import TurnCoordinator
from miniunicorn.agent.turn_dispatcher import TurnDispatcher
from miniunicorn.agent.turn_executor import TURN_TRANSITIONS, TurnExecutor
from miniunicorn.agent.turn_persistence import (
    TurnPersistence,
)
from miniunicorn.agent.turn_runtime import (
    AgentLoopRunResult,
    ProcessedTurn,
    TurnRuntime,
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

        Returns None unless either max_input_tokens_per_turn or
        max_cost_per_turn_usd is set; unset dimensions default to None.
        """
        if self._max_input_tokens_per_turn is None and self._max_cost_per_turn_usd is None:
            return None
        from miniunicorn.agent.turn_budget import TurnBudget

        return TurnBudget(
            max_input_tokens=self._max_input_tokens_per_turn,
            max_output_tokens=None,
            max_cost_usd=self._max_cost_per_turn_usd,
        )

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
        vector_memory_factory: Callable[..., Any] | None = None,
        maintenance_enqueue: Callable[..., Any] | None = None,
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
        self._maintenance_enqueue = maintenance_enqueue
        self._provider_signature = provider_signature
        self._default_selection_signature = preset_helpers.default_selection_signature(
            provider_signature
        )
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = (
            max_iterations if max_iterations is not None else defaults.max_tool_iterations
        )
        # Runtime contract: require a resolved positive integer (no network
        # lookup/default guess); config-time resolution must persist a value.
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
        # One file-read/write tracker per logical session; tools resolve state via contextvars.
        self._file_state_store = FileStateStore()
        # TurnPersistence owns checkpoint/pending-turn/history-write algorithms.
        self._turn_persistence = TurnPersistence(self)
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
        # Supervised fire-and-forget background jobs (archives, consolidation,
        # etc.). The supervisor owns strong references and surfaces unhandled
        # exceptions via a done-callback so nothing fails silently.
        self._background_supervisor: TaskSupervisor = TaskSupervisor()
        # MINIUNICORN_MAX_CONCURRENT_REQUESTS: <=0 means unlimited; default 3.
        _max = int(os.environ.get("MINIUNICORN_MAX_CONCURRENT_REQUESTS", "3"))
        # TurnCoordinator owns per-session locks (weakly held) and the global
        # concurrency semaphore. Lock precedes semaphore so a waiting task
        # cannot consume a global permit. Every turn entry point routes
        # through coordinator.scope to bind a TurnRuntime for the turn.
        self._turn_coordinator = TurnCoordinator(max_concurrent_requests=_max)
        # Read-only compatibility alias for code that inspects session locks.
        self._session_locks = self._turn_coordinator.session_locks
        # TurnExecutor owns the single-turn state-machine driver.
        self._turn_executor = TurnExecutor(self)
        # AgentRunAdapter is the single thick adapter between the loop and AgentRunner.
        self._agent_run_adapter = AgentRunAdapter(self)
        # Telemetry sink: one structured record per turn. Defaults to LogTelemetrySink.
        # Sink exceptions are logged and suppressed so telemetry can never break a turn.
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
        # Dream idle trigger: fires Dream when user is idle, independent of cron.
        from miniunicorn.agent.dream_trigger import DreamIdleTrigger

        self.dream_idle_trigger = DreamIdleTrigger(
            self.dream,
            enabled=defaults.dream.idle_trigger_enabled,
            min_idle_seconds=defaults.dream.idle_trigger_min_seconds,
            min_entries=defaults.dream.idle_trigger_min_entries,
            min_interval_s=defaults.dream.idle_trigger_min_interval_s,
        )
        # Shared single-process embedding-memory runtime (model manager,
        # catalog, index, recall service, prompt policy and status). The
        # disabled instance stays lazy: it never creates memory/memory.db and
        # never loads the embedding model. The local CPU embedding provider
        # (FastEmbed/BGE) is independent of the chat LLM provider, so runtime
        # chat-provider switching never perturbs recall.
        self._vector_recall = vector_recall
        self._embedding_model = embedding_model
        from miniunicorn.embedding.control import EmbeddingControl

        self.embedding_control = EmbeddingControl.for_workspace(
            self.workspace, configured=vector_recall
        )
        self.context.memory.set_reconcile_hook(self.embedding_control.request_reconcile)
        from miniunicorn.agent.explicit_memory import (
            ExplicitMemoryJournal,
            ExplicitMemoryService,
        )

        self.explicit_memory = ExplicitMemoryService(
            ExplicitMemoryJournal(self.workspace),
            control=self.embedding_control,
            provider=provider,
        )
        self.model_presets: dict[str, ModelPresetConfig] = model_presets or {}
        self._active_preset: str | None = None
        if model_preset:
            self.set_model_preset(model_preset, publish_update=False)
        self._register_default_tools()
        self._runtime_vars: dict[str, Any] = {}
        self.commands = CommandRouter()
        register_builtin_commands(self.commands)
        # TurnDispatcher owns the process_message normalization bridge
        # used by the SDK path. Legacy bus-consume loop, dispatch,
        # process_direct, pending_queues, and active_tasks were removed
        # in design Task 10 — inbound work is submitted through
        # RuntimeApplication and final replies go through the Outbox.
        self._turn_dispatcher = TurnDispatcher(self, self._turn_coordinator)

    @property
    def core_dispatcher(self) -> TurnDispatcher:
        """Read-only access to the TurnDispatcher (design Task 5)."""
        return self._turn_dispatcher

    @property
    def turn_coordinator(self) -> TurnCoordinator:
        """Read-only access to the TurnCoordinator (design Task 5)."""
        return self._turn_coordinator

    @classmethod
    def from_config(
        cls,
        config: Any,
        bus: MessageBus | None = None,
        **extra: Any,
    ) -> AgentLoop:
        """Create an AgentLoop from config with the common parameter set.

        Extra keyword arguments are forwarded to ``AgentLoop.__init__``.
        Delegates to ``AgentLoopBuilder.from_config``.
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
        """Persist the triggering user message before the turn starts."""
        return self._turn_persistence.persist_user_message_early(msg, session, **kwargs)

    async def _compute_query_embedding(self, text: str) -> list[float] | None:
        """Compute embedding for *text* when vector recall is enabled.

        Uses the dedicated local embedding provider, never the chat provider,
        so switching chat providers mid-session does not perturb recall.
        """
        if not self._vector_recall or not text:
            return None
        try:
            embeddings = await self.embedding_control.provider.embed(
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
        recall = await self.embedding_control.recall_for_turn(msg.content)
        memory_prompt = self.embedding_control.prompt_policy.build(recall)
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
            memory_prompt=memory_prompt,
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
        turn_query: str | None = None,
    ) -> AgentLoopRunResult:
        """Run the agent iteration loop. Delegates to :class:`AgentRunAdapter`."""
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
            turn_query=turn_query,
        )

    async def run(self) -> None:
        """Run the agent loop.

        Legacy bus-consume loop removed in design Task 10. Production
        entry points use the durable Runtime (``RuntimeApplication``);
        the SDK path uses :meth:`_process_message` directly.
        """
        raise NotImplementedError(
            "AgentLoop.run() bus-consume loop was removed in design Task 10. "
            "Use RuntimeApplication.submit_and_wait() or the Worker callback "
            "for durable execution, or Miniunicorn.run() for the SDK path."
        )

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
        *,
        runtime_mode: bool = False,
    ) -> ProcessedTurn:
        """Execute a single inbound message and return the outbound + context.

        When ``runtime_mode=True`` (design §18.1, §29.3), uses
        :data:`RUNTIME_TURN_TRANSITIONS` which removes the
        ``COMMAND -> DONE`` shortcut so all user-visible commands flow
        through SAVE and RESPOND (durable commit + Outbox).
        """
        return await self._turn_executor.execute(
            msg,
            session_key=session_key,
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            pending_queue=pending_queue,
            turn_hooks=turn_hooks,
            runtime_mode=runtime_mode,
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

        Delegates to :class:`TurnDispatcher`, which calls
        ``_execute_message`` and completes the bound ``TurnRuntime``.
        """
        return await self._turn_dispatcher.process_message(
            msg,
            session_key=session_key,
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            pending_queue=pending_queue,
            turn_hooks=turn_hooks,
        )

    async def _publish_turn_end(
        self,
        msg: InboundMessage,
        session_key: str,
        runtime: TurnRuntime,
    ) -> None:
        """Publish the WebUI turn-end envelope while the runtime is still bound."""
        if msg.channel != "websocket":
            return
        await self._webui_turns.handle_turn_end(
            msg,
            session_key=session_key,
            latency_ms=None,
            context_usage=dict(runtime.last_call_usage) if runtime.last_call_usage else None,
        )

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
        return self._turn_persistence.assemble_outbound(
            msg,
            final_content,
            all_msgs,
            stop_reason,
            had_injections,
            on_stream,
            turn_latency_ms=turn_latency_ms,
        )

    def _sanitize_persisted_blocks(
        self,
        content: list[dict[str, Any]],
        *,
        should_truncate_text: bool = False,
        drop_runtime: bool = False,
    ) -> list[dict[str, Any]]:
        """Strip volatile multimodal payloads before writing session history."""
        return self._turn_persistence.sanitize_persisted_blocks(
            content,
            should_truncate_text=should_truncate_text,
            drop_runtime=drop_runtime,
        )

    def _save_turn(
        self,
        session: Session,
        messages: list[dict],
        skip: int,
        *,
        turn_latency_ms: int | None = None,
    ) -> None:
        """Save new-turn messages into session, truncating large tool results."""
        self._turn_persistence.save_turn(
            session,
            messages,
            skip,
            turn_latency_ms=turn_latency_ms,
        )

    def _persist_subagent_followup(self, session: Session, msg: InboundMessage) -> bool:
        """Persist subagent follow-ups before prompt assembly so history stays durable."""
        return self._turn_persistence.persist_subagent_followup(session, msg)


# Re-export for backwards compatibility (tests/extensions may import from loop)
__all__ = [
    "AgentLoop",
    "StateTraceEntry",
    "TurnContext",
    "TurnState",
    "extract_documents",
]
