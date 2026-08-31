"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable
from weakref import WeakValueDictionary

from loguru import logger

from miniunicorn.agent import model_presets as preset_helpers
from miniunicorn.agent import turn_telemetry
from miniunicorn.agent._mcp_lifecycle import McpLifecycleMixin
from miniunicorn.agent._provider_switching import ProviderSwitchingMixin
from miniunicorn.agent.autocompact import AutoCompact
from miniunicorn.agent.context import ContextBuilder
from miniunicorn.agent.dispatch import UNIFIED_SESSION_KEY, MessageDispatcher
from miniunicorn.agent.hook import AgentHook, CompositeHook
from miniunicorn.agent.memory import Consolidator, Dream, MemoryStore
from miniunicorn.agent.planning_policy import PlanningMode, PlanningPolicy
from miniunicorn.agent.progress_hook import AgentProgressHook
from miniunicorn.agent.provider_registry import ProviderRegistry
from miniunicorn.agent.response import ResponseAssembler
from miniunicorn.agent.runner import _MAX_INJECTIONS_PER_TURN, AgentRunner, AgentRunSpec
from miniunicorn.agent.runtime_resources import RuntimeResourceRegistry
from miniunicorn.agent.session_turn import SessionTurnService
from miniunicorn.agent.subagent import SubagentManager
from miniunicorn.agent.subagent_registry import SubagentDefinition, SubagentRegistry
from miniunicorn.agent.tools.context import (
    RequestContext,
    bind_request_context,
    reset_request_context,
)
from miniunicorn.agent.tools.file_state import FileStateStore, bind_file_states, reset_file_states
from miniunicorn.agent.tools.registry import LazyToolRegistry, ToolRegistry
from miniunicorn.agent.turn_orchestrator import (
    StateMixin,
    StateTraceEntry,
    TurnContext,
    TurnDeps,
    TurnOrchestrator,
    TurnState,
)
from miniunicorn.bus.events import InboundMessage, OutboundMessage
from miniunicorn.bus.queue import MessageBus
from miniunicorn.command import (
    CommandApplicationService,
    CommandContext,
    CommandRouter,
    register_builtin_commands,
)
from miniunicorn.composition.mcp_runtime import McpRuntime
from miniunicorn.config.schema import AgentDefaults, ModelPresetConfig, StructuredMemoryConfig
from miniunicorn.providers.base import LLMProvider
from miniunicorn.providers.factory import ProviderSnapshot
from miniunicorn.security.workspace_access import (
    WorkspaceScopeResolver,
    bind_workspace_scope,
    reset_workspace_scope,
)
from miniunicorn.session.goal_state import (
    goal_state_runtime_lines,
    runner_wall_llm_timeout_s,
    sustained_goal_active,
)
from miniunicorn.session.manager import Session, SessionManager
from miniunicorn.session.webui_turns import (
    WebuiTurnCoordinator,
    build_bus_progress_callback,
)
from miniunicorn.utils.callback_types import ProgressCallback
from miniunicorn.utils.document import extract_documents  # re-export for tests/extensions
from miniunicorn.utils.llm_runtime import LLMRuntime
from miniunicorn.utils.runtime import (
    SUSTAINED_GOAL_CONTINUE_PROMPT,
)

if TYPE_CHECKING:
    from miniunicorn.config.schema import (
        ChannelsConfig,
        ToolsConfig,
    )
    from miniunicorn.cron.service import CronService


# 主循环/任务管理相关超时 (集中定义, 避免魔法数字散落):
_IDLE_POLL_INTERVAL_S = 1.0  # 空闲时轮询入站消息的间隔 (auto-compact/dream 空闲触发节拍)
_SUBAGENT_DRAIN_WAIT_S = 300.0  # 等待子代理产出注入队列的上限 (阻塞保持注入顺序)


def _get_session_turn(host: Any) -> SessionTurnService:
    """Resolve the ``SessionTurnService`` bound to a session-turn host.

    Reads the host's ``__dict__`` directly so the service also resolves for
    minimal stand-up stubs that expose the loop's delegate surface without
    being ``AgentLoop`` instances (the ``_session_turn`` property descriptor
    only fires on real loops).
    """
    service = getattr(host, "__dict__", {}).get("_session_turn")
    if service is None:
        service = SessionTurnService(
            sessions=getattr(host, "sessions", None),
            workspace=getattr(host, "workspace", None),
            webui_turns=getattr(host, "_webui_turns", None),
            max_tool_result_chars=host.max_tool_result_chars,
        )
        try:
            host.__dict__["_session_turn"] = service
        except (AttributeError, TypeError):
            pass
    return service


@dataclass
class AgentLoopConfig:
    """Configuration bundle absorbed from the legacy ``AgentLoop.__init__`` kwargs.

    Phase 4 shrinks ``AgentLoop.__init__`` down to ``bus`` + ``workspace`` +
    this bundle + five service objects. Every legacy keyword parameter (the
    ones the 58 direct ``AgentLoop(...)`` test call sites still use) folds
    into this bundle; the facade constructs its services from it by default.
    """

    provider: LLMProvider | None = None
    model: str | None = None
    max_iterations: int | None = None
    max_concurrent_subagents: int | None = None
    max_subagent_recursion_depth: int | None = None
    context_window_tokens: int | None = None
    context_block_limit: int | None = None
    max_tool_result_chars: int | None = None
    max_tool_result_tokens: int | None = None
    provider_retry_mode: str = "standard"
    tool_hint_max_length: int | None = None
    cron_service: "CronService | None" = None
    restrict_to_workspace: bool = False
    high_risk_policy: str = "allow"
    session_manager: "SessionManager | None" = None
    subagent_manager: "SubagentManager | None" = None
    mcp_servers: dict | None = None
    mcp_runtime: "McpRuntime | None" = None
    channels_config: "ChannelsConfig | None" = None
    timezone: str | None = None
    session_ttl_minutes: int = 0
    consolidation_ratio: float = 0.5
    max_messages: int = 120
    hooks: "list[AgentHook] | None" = None
    unified_session: bool = False
    disabled_skills: list[str] | None = None
    tools_config: "ToolsConfig | None" = None
    provider_snapshot_loader: "Callable[..., ProviderSnapshot] | None" = None
    provider_signature: tuple[object, ...] | None = None
    model_presets: "dict[str, ModelPresetConfig] | None" = None
    model_preset: str | None = None
    preset_snapshot_loader: "preset_helpers.PresetSnapshotLoader | None" = None
    runtime_model_publisher: "Callable[[str, str | None], None] | None" = None
    structured_memory_config: StructuredMemoryConfig | None = None
    use_planner: bool = False
    planner_model: str | None = None
    planner_max_replans: int = 3
    # Explicit PlanningPolicy (P1). When set, AgentLoop derives
    # use_planner/planner_model/planner_max_replans from it instead.
    planning_policy: PlanningPolicy | None = None
    enable_reflection: bool = False
    reflection_interval: int = 5
    max_input_tokens_per_turn: int | None = None
    max_cost_per_turn_usd: float | None = None
    # P2-T3 tiered per-turn ceilings; resolved by planning mode when the
    # explicit fields above are unset.
    managed_max_input_tokens_per_turn: int | None = None
    managed_max_cost_per_turn_usd: float | None = None
    fast_max_input_tokens_per_turn: int | None = None
    fast_max_cost_per_turn_usd: float | None = None
    max_turn_wall_time_s: float | None = None
    # T1: Tiered max tool iterations per planning mode
    fast_max_tool_iterations: int | None = None
    managed_max_tool_iterations: int | None = None
    # T5: Enable LLM verifier fallback for step acceptance
    enable_step_verifier: bool = False


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
        """Current iteration, preferring the bound per-turn telemetry.

        Falls back to the loop-level value so direct reads/writes (the ``my``
        tool reflection, tests, and non-dispatch callers) keep working outside
        an agent turn (Phase 6: per-turn telemetry).
        """
        telemetry = turn_telemetry.current()
        if telemetry is not None:
            return telemetry.iteration
        return self.__dict__.get("_current_iteration_fallback", 0)

    @_current_iteration.setter
    def _current_iteration(self, value: int) -> None:
        telemetry = turn_telemetry.current()
        if telemetry is not None:
            telemetry.iteration = int(value)
        self.__dict__["_current_iteration_fallback"] = int(value)

    @property
    def tool_names(self) -> list[str]:
        return self.tools.tool_names

    # -- provider registry delegation ------------------------------------------
    #
    # The runtime provider/model/context-window triple is owned by a shared
    # ``ProviderRegistry`` (Phase 4). These properties keep the loop's
    # historical ``provider`` / ``model`` / ``context_window_tokens`` surface
    # reading and writing the same object the runner holds, so hot-swap paths
    # converge without cross-module attribute writes. When no registry exists
    # (minimal stand-up stubs built via ``__new__``), they fall back to plain
    # instance attributes.

    @property
    def provider(self) -> LLMProvider:
        registry = self.__dict__.get("_provider_registry")
        if registry is not None:
            return registry.provider
        return self.__dict__.get("_provider")

    @provider.setter
    def provider(self, value: LLMProvider) -> None:
        registry = self.__dict__.get("_provider_registry")
        if registry is not None:
            registry.provider = value
        else:
            self.__dict__["_provider"] = value

    @property
    def model(self) -> str:
        registry = self.__dict__.get("_provider_registry")
        if registry is not None:
            return registry.model
        return self.__dict__.get("_model")

    @model.setter
    def model(self, value: str) -> None:
        registry = self.__dict__.get("_provider_registry")
        if registry is not None:
            registry.model = value
        else:
            self.__dict__["_model"] = value

    @property
    def context_window_tokens(self) -> int | None:
        registry = self.__dict__.get("_provider_registry")
        if registry is not None:
            return registry.context_window_tokens
        return self.__dict__.get("_context_window_tokens")

    @context_window_tokens.setter
    def context_window_tokens(self, value: int | None) -> None:
        registry = self.__dict__.get("_provider_registry")
        if registry is not None:
            registry.context_window_tokens = value
        else:
            self.__dict__["_context_window_tokens"] = value

    def llm_runtime(self) -> LLMRuntime:
        """Return the current provider/model pair owned by this loop."""
        self._refresh_provider_snapshot()
        return LLMRuntime(self.provider, self.model)

    def _build_turn_budget(self):
        """Construct a fresh TurnBudget for one turn (P2-T3 tiered resolution).

        Priority: explicit ``max_input_tokens_per_turn`` /
        ``max_cost_per_turn_usd`` wins; otherwise the planning-mode tier
        supplies the ceiling (MANAGED keeps P0 headroom 200k/$5; FAST lowers
        ordinary-turn ceilings to 80k/$2). Cost tracking is only *required*
        when the legacy cost field is set explicitly — tiered cost caps stay
        advisory when the provider does not report cost, so default
        deployments never hard-fail on missing pricing.
        """
        from miniunicorn.agent.turn_budget import (
            DEFAULT_FAST_MAX_COST_USD,
            DEFAULT_FAST_MAX_INPUT_TOKENS,
            DEFAULT_MANAGED_MAX_COST_USD,
            DEFAULT_MANAGED_MAX_INPUT_TOKENS,
            TurnBudget,
        )

        managed = self.planning_policy.mode == PlanningMode.MANAGED
        if self._max_input_tokens_per_turn is not None:
            max_input = self._max_input_tokens_per_turn
        elif managed:
            max_input = self._managed_max_input_tokens_per_turn
            if max_input is None:
                max_input = DEFAULT_MANAGED_MAX_INPUT_TOKENS
        else:
            max_input = self._fast_max_input_tokens_per_turn
            if max_input is None:
                max_input = DEFAULT_FAST_MAX_INPUT_TOKENS
        if self._max_cost_per_turn_usd is not None:
            max_cost = self._max_cost_per_turn_usd
        elif managed:
            max_cost = self._managed_max_cost_per_turn_usd
            if max_cost is None:
                max_cost = DEFAULT_MANAGED_MAX_COST_USD
        else:
            max_cost = self._fast_max_cost_per_turn_usd
            if max_cost is None:
                max_cost = DEFAULT_FAST_MAX_COST_USD
        return TurnBudget(
            max_input_tokens=max_input,
            max_output_tokens=None,
            max_cost_usd=max_cost,
            require_cost_tracking=self._max_cost_per_turn_usd is not None,
        )

    _RUNTIME_CHECKPOINT_KEY = SessionTurnService._RUNTIME_CHECKPOINT_KEY
    _PENDING_USER_TURN_KEY = SessionTurnService._PENDING_USER_TURN_KEY

    def __init__(
        self,
        bus: MessageBus,
        workspace: Path,
        *,
        config: AgentLoopConfig | None = None,
        dispatcher: MessageDispatcher | None = None,
        session_turn: SessionTurnService | None = None,
        resources: RuntimeResourceRegistry | None = None,
        turn_orchestrator: TurnOrchestrator | None = None,
        response: ResponseAssembler | None = None,
        **legacy: Any,
    ):
        from miniunicorn.config.schema import ToolsConfig

        cfg = config or AgentLoopConfig(**legacy)
        if cfg.provider is None:
            raise TypeError(
                "AgentLoop requires a provider; pass it via the config bundle "
                "(config=...) or as a legacy keyword argument (provider=...)."
            )
        _tc = cfg.tools_config or ToolsConfig()
        defaults = AgentDefaults()
        self._init_command_layer(bus, dispatcher, response, cfg)
        self._init_provider_layer(cfg, workspace)
        self._init_execution_limits(cfg, defaults)
        self._init_policy_and_workspace(cfg, workspace, _tc)
        self._init_session_layer(cfg, workspace, session_turn, _tc)
        self._init_subagent_layer(cfg, workspace, bus, _tc)
        self._init_resource_layer(cfg, workspace, resources, defaults)
        self._init_turn_orchestrator(turn_orchestrator)

    def _init_command_layer(
        self,
        bus: MessageBus,
        dispatcher: MessageDispatcher | None,
        response: ResponseAssembler | None,
        cfg: AgentLoopConfig,
    ) -> None:
        """消息/命令/工具注册表装配。A 区段。"""
        self.bus = bus
        self.commands = CommandRouter()
        register_builtin_commands(self.commands)
        self._commands = CommandApplicationService(bus=bus, router=self.commands)
        self.tools: ToolRegistry = LazyToolRegistry(load_hook=self._register_default_tools)
        self._response = response or ResponseAssembler(bus=bus, tools=self.tools)
        self._dispatcher = dispatcher or MessageDispatcher(self, bus, commands=self._commands)
        self.channels_config = cfg.channels_config

    def _init_provider_layer(self, cfg: AgentLoopConfig, workspace: Path) -> None:
        """Provider registry 与模型标识装配。B 区段(含 workspace 归属)。"""
        # ProviderRegistry: single owner of the runtime provider/model/context
        # window triple. The loop's ``provider`` / ``model`` /
        # ``context_window_tokens`` properties and the runner delegate to it,
        # so every swap path (provider snapshots, the gateway heartbeat, test
        # overrides) converges on one object.
        self._provider_registry = ProviderRegistry(
            cfg.provider,
            cfg.model,
            cfg.context_window_tokens,
        )
        self.provider = cfg.provider
        self._provider_snapshot_loader = cfg.provider_snapshot_loader
        self._preset_snapshot_loader = cfg.preset_snapshot_loader
        self._runtime_model_publisher = cfg.runtime_model_publisher
        self._provider_signature = cfg.provider_signature
        self._default_selection_signature = preset_helpers.default_selection_signature(
            cfg.provider_signature
        )
        self.workspace = workspace
        self.model = cfg.model or self.provider.get_default_model()

    def _init_execution_limits(self, cfg: AgentLoopConfig, defaults: AgentDefaults) -> None:
        """迭代/上下文窗口/工具结果预算派生。C 区段。"""
        # T1: Explicit max_iterations wins; otherwise select tier by planning mode
        if cfg.max_iterations is not None:
            self.max_iterations = cfg.max_iterations
        else:
            managed = (
                cfg.planning_policy.mode == PlanningMode.MANAGED
                if cfg.planning_policy
                else cfg.use_planner
            )
            if managed:
                self.max_iterations = (
                    cfg.managed_max_tool_iterations
                    if cfg.managed_max_tool_iterations is not None
                    else defaults.managed_max_tool_iterations
                )
            else:
                self.max_iterations = (
                    cfg.fast_max_tool_iterations
                    if cfg.fast_max_tool_iterations is not None
                    else defaults.fast_max_tool_iterations
                )
        self.context_window_tokens = (
            cfg.context_window_tokens
            if cfg.context_window_tokens is not None
            else defaults.context_window_tokens
        )
        # Auto-detect context window when still unset. Resolution chain:
        # permanent learning table → Hugging Face API → fail-loud.
        # ``raise_on_unknown=True`` enforces the user's preference: when the
        # model is not in the learning table AND HF lookup fails, surface a
        # clear error instead of silently defaulting to 65_536. Setting
        # ``MINIUNICORN_NO_AUTO_LOOKUP=1`` also fails loud here (the error
        # message tells the user to set context_window_tokens explicitly).
        if self.context_window_tokens is None:
            from miniunicorn.cli.models import get_model_context_limit

            self.context_window_tokens = get_model_context_limit(self.model, raise_on_unknown=True)
        # resolved_context_window_tokens: 后端解析值,用于 /status 等显示场景
        # 优先使用用户显式配置,否则回退到默认值(DEFAULT_CONTEXT_LIMIT)
        # 不依赖学习表缓存,因为学习表可能包含过时或误匹配的值(如 test-model → 512)
        from miniunicorn.cli.models import DEFAULT_CONTEXT_LIMIT

        self.resolved_context_window_tokens = (
            cfg.context_window_tokens
            if cfg.context_window_tokens is not None
            else defaults.context_window_tokens
        ) or DEFAULT_CONTEXT_LIMIT
        self.context_block_limit = cfg.context_block_limit
        # T4: Token budget primary; chars derived from tokens (chars = tokens * 4).
        # Builder already applied priority: explicit chars > explicit tokens > default 4000.
        if cfg.max_tool_result_tokens is not None:
            self.max_tool_result_tokens = cfg.max_tool_result_tokens
            self.max_tool_result_chars = cfg.max_tool_result_tokens * 4
        elif cfg.max_tool_result_chars is not None:
            self.max_tool_result_chars = cfg.max_tool_result_chars
            self.max_tool_result_tokens = cfg.max_tool_result_chars // 4
        else:
            self.max_tool_result_tokens = 4000
            self.max_tool_result_chars = 16_000
        self.provider_retry_mode = cfg.provider_retry_mode
        self.tool_hint_max_length = (
            cfg.tool_hint_max_length
            if cfg.tool_hint_max_length is not None
            else defaults.tool_hint_max_length
        )

    def _init_policy_and_workspace(
        self, cfg: AgentLoopConfig, workspace: Path, _tc: "ToolsConfig"
    ) -> None:
        """规划策略、审批策略与工作区解析器装配。D 区段。"""
        # Execution policies are propagated through the bundle for both
        # from_config() and legacy direct AgentLoop(...) construction.
        # PlanningPolicy (P1): an explicitly provided policy wins; otherwise
        # resolve from the legacy use_planner fields for backward compat.
        if cfg.planning_policy is not None:
            self.planning_policy = cfg.planning_policy
        else:
            self.planning_policy = PlanningPolicy.from_use_planner(
                cfg.use_planner, cfg.planner_model, cfg.planner_max_replans
            )
        self.use_planner = self.planning_policy.mode == PlanningMode.MANAGED
        self.planner_model = self.planning_policy.planner_model
        self.planner_max_replans = self.planning_policy.planner_max_replans
        self.enable_reflection = cfg.enable_reflection
        self.reflection_interval = cfg.reflection_interval
        self._max_input_tokens_per_turn = cfg.max_input_tokens_per_turn
        self._max_cost_per_turn_usd = cfg.max_cost_per_turn_usd
        self._managed_max_input_tokens_per_turn = cfg.managed_max_input_tokens_per_turn
        self._managed_max_cost_per_turn_usd = cfg.managed_max_cost_per_turn_usd
        self._fast_max_input_tokens_per_turn = cfg.fast_max_input_tokens_per_turn
        self._fast_max_cost_per_turn_usd = cfg.fast_max_cost_per_turn_usd
        self.tools_config = _tc
        self.web_config = _tc.web
        self.exec_config = _tc.exec
        self.cron_service = cfg.cron_service
        self.restrict_to_workspace = cfg.restrict_to_workspace
        self.high_risk_policy = cfg.high_risk_policy
        self.workspace_scopes = WorkspaceScopeResolver(
            default_workspace=workspace,
            default_restrict_to_workspace=cfg.restrict_to_workspace,
        )
        self._start_time = time.time()
        self._last_usage: dict[str, int] = {}
        self._extra_hooks: list[AgentHook] = cfg.hooks or []

    def _init_session_layer(
        self,
        cfg: AgentLoopConfig,
        workspace: Path,
        session_turn: SessionTurnService | None,
        _tc: "ToolsConfig",
    ) -> None:
        """上下文构建器、会话存储与单轮服务装配。E 区段。"""
        self.context = ContextBuilder(
            workspace,
            timezone=cfg.timezone,
            disabled_skills=cfg.disabled_skills,
            structured_memory_config=cfg.structured_memory_config,
            cli_apps_enabled=_tc.cli_apps.enabled,
        )
        self.sessions = cfg.session_manager or SessionManager(workspace)
        self._webui_turns = WebuiTurnCoordinator(
            bus=self.bus,
            sessions=self.sessions,
            schedule_background=lambda coro: self._schedule_background(coro),
        )
        self.__dict__["_session_turn"] = session_turn or SessionTurnService(
            sessions=self.sessions,
            workspace=workspace,
            webui_turns=self._webui_turns,
            max_tool_result_chars=self.max_tool_result_chars,
        )
        # One file-read/write tracker per logical session. The tool registry is
        # shared by this loop, so tools resolve the active state via contextvars.
        self._file_state_store = FileStateStore()
        self.runner = AgentRunner(self.provider, provider_registry=self._provider_registry)

    def _init_subagent_layer(
        self, cfg: AgentLoopConfig, workspace: Path, bus: MessageBus, _tc: "ToolsConfig"
    ) -> None:
        """Subagent/注册表/MCP runtime(注入-回退双路径)。F 区段。"""
        self.subagents = (
            cfg.subagent_manager
            if cfg.subagent_manager is not None
            else SubagentManager(
                provider=self.provider,
                workspace=workspace,
                bus=bus,
                model=self.model,
                tools_config=_tc,
                max_tool_result_chars=self.max_tool_result_chars,
                restrict_to_workspace=cfg.restrict_to_workspace,
                disabled_skills=cfg.disabled_skills,
                max_iterations=self.max_iterations,
                max_concurrent_subagents=cfg.max_concurrent_subagents,
                llm_wall_timeout_for_session=lambda sk: runner_wall_llm_timeout_s(
                    self.sessions, sk
                ),
                max_subagent_recursion_depth=cfg.max_subagent_recursion_depth,
                turn_budget_factory=self._build_turn_budget,
                high_risk_policy=self.high_risk_policy,
            )
        )
        # Declarative subagent registry (TRAE-style .md definitions in agents/).
        # Loaded once at startup; empty when no agents/ dir exists.
        self.subagent_registry = SubagentRegistry(workspace)
        self.subagent_registry.load()
        self.context.subagent_registry = self.subagent_registry
        self._unified_session = cfg.unified_session
        self._max_messages = cfg.max_messages if cfg.max_messages > 0 else 120
        self._mcp_runtime = cfg.mcp_runtime or McpRuntime(cfg.mcp_servers or {})

    def _init_resource_layer(
        self,
        cfg: AgentLoopConfig,
        workspace: Path,
        resources: RuntimeResourceRegistry | None,
        defaults: AgentDefaults,
    ) -> None:
        """RuntimeResourceRegistry(注入-回退)与 per-workspace 别名。G 区段。"""
        # Consolidator / AutoCompact / Dream / DreamIdleTrigger and the
        # per-workspace helper caches are owned by ``RuntimeResourceRegistry``;
        # the loop exposes them through read-only delegating properties (below)
        # and aliases the per-workspace caches so commands, tools, and tests
        # keep reading the same objects.
        self._resources = resources or RuntimeResourceRegistry(
            workspace=workspace,
            context=self.context,
            workspace_scopes=self.workspace_scopes,
            sessions=self.sessions,
            provider=self.provider,
            model=self.model,
            context_window_tokens=self.context_window_tokens,
            tools=self.tools,
            max_completion_tokens=self.provider.generation.max_tokens,
            consolidation_ratio=cfg.consolidation_ratio,
            session_ttl_minutes=cfg.session_ttl_minutes,
            max_batch_size=defaults.dream.max_batch_size,
            dream_idle=defaults.dream,
        )
        self._default_root = self._resources._default_root
        self._workspace_helpers_lock = self._resources._workspace_helpers_lock
        self._workspace_consolidators = self._resources._workspace_consolidators
        self._workspace_dreams = self._resources._workspace_dreams
        self.model_presets: dict[str, ModelPresetConfig] = cfg.model_presets or {}
        self._active_preset: str | None = None
        if cfg.model_preset:
            self.set_model_preset(cfg.model_preset, publish_update=False)
        self._runtime_vars: dict[str, Any] = {}
        self._current_iteration: int = 0

    def _init_turn_orchestrator(self, turn_orchestrator: TurnOrchestrator | None) -> None:
        """TurnOrchestrator(注入-回退)与 TurnDeps 晚绑定依赖。H 区段。"""
        # Turn state machine: handlers are provided by the orchestrator; the
        # callable deps are late-bound so test monkeypatches on the loop's
        # methods keep taking effect after construction.
        self._turn_orchestrator = turn_orchestrator or TurnOrchestrator(
            TurnDeps(
                session_turn=self._session_turn,
                resources=self._resources,
                response=self._response,
                runner=self.runner,
                tools=self.tools,
                context_builder=self.context,
                commands=self._dispatch_command_for_turn,
                webui_turns=self._webui_turns,
                sessions=self.sessions,
                channels_config=self.channels_config,
                max_messages=self._max_messages,
                run_agent_loop=lambda *args, **kwargs: self._run_agent_loop(*args, **kwargs),
                build_bus_progress_callback=lambda msg: self._build_bus_progress_callback(msg),
                build_retry_wait_callback=lambda msg: self._build_retry_wait_callback(msg),
                assemble_outbound=self._assemble_outbound,
                schedule_background=lambda coro: self._schedule_background(coro),
                set_tool_context=self._set_tool_context,
                build_initial_messages=self._build_initial_messages,
                replay_token_budget=self._replay_token_budget,
                llm_runtime=self.llm_runtime,
                refresh_provider_snapshot=self._refresh_provider_snapshot,
                resolve_agent_override=self._resolve_agent_override,
                process_system_message=self._process_system_message,
                build_turn_budget=self._build_turn_budget,
            )
        )

    # -- runtime state view ---------------------------------------------------
    #
    # Read-only MCP runtime state for prompt assembly (see ``runtime_view``).
    # The mutable tables stay private to the MCP lifecycle module.

    @property
    def mcp_servers(self) -> dict[str, Any]:
        """Configured MCP server table (name -> server config)."""
        return self._mcp_servers

    @property
    def mcp_stacks(self) -> dict[str, AsyncExitStack]:
        """Live MCP connection stacks (name -> ``AsyncExitStack``)."""
        return self._mcp_stacks

    # -- MCP runtime delegation ----------------------------------------------
    #
    # The live MCP connection state is owned by ``self._mcp_runtime`` (a
    # composition-root-owned ``McpRuntime``). The loop stays a valid
    # ``RuntimeState`` for ``tools/mcp.py`` (incl. the webui hot-reload path,
    # which receives the loop as state) by delegating every private MCP
    # attribute read/write to the runtime instead of owning its own copies.

    @property
    def _mcp_servers(self) -> dict[str, Any]:
        return self._mcp_runtime._mcp_servers

    @_mcp_servers.setter
    def _mcp_servers(self, servers: dict[str, Any]) -> None:
        self._mcp_runtime._mcp_servers = servers

    @property
    def _mcp_stacks(self) -> dict[str, AsyncExitStack]:
        return self._mcp_runtime._mcp_stacks

    @_mcp_stacks.setter
    def _mcp_stacks(self, stacks: dict[str, AsyncExitStack]) -> None:
        self._mcp_runtime._mcp_stacks = stacks

    @property
    def _mcp_connected(self) -> bool:
        return self._mcp_runtime._mcp_connected

    @_mcp_connected.setter
    def _mcp_connected(self, value: bool) -> None:
        self._mcp_runtime._mcp_connected = value

    @property
    def _mcp_connecting(self) -> bool:
        return self._mcp_runtime._mcp_connecting

    @_mcp_connecting.setter
    def _mcp_connecting(self, value: bool) -> None:
        self._mcp_runtime._mcp_connecting = value

    # -- response assembler ---------------------------------------------------
    #
    # Final outbound assembly and trailing telemetry live in
    # ``ResponseAssembler``; this read/write property keeps the loop's
    # historical ``_last_usage`` surface intact for commands, tools, and tests.
    # ``_last_call_usage`` moved to per-turn telemetry (Phase 6): the turn_end
    # consumer reads ``turn_telemetry`` with a snapshot fallback, so the loop
    # property was removed.

    @property
    def _last_usage(self) -> dict[str, int]:
        return self._response._last_usage

    @_last_usage.setter
    def _last_usage(self, value: dict[str, int]) -> None:
        self._response._last_usage = value

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
        """Compatibility delegate for the response assembler."""
        return self._response._assemble_outbound(
            msg,
            final_content,
            all_msgs,
            stop_reason,
            had_injections,
            on_stream,
            turn_latency_ms=turn_latency_ms,
        )

    # -- session turn service ------------------------------------------------
    #
    # The durable session-turn persistence and checkpoint recovery machinery
    # lives in ``SessionTurnService``; the loop exposes it through a lazily
    # constructed reference so minimal stand-up loops (``__new__`` + a couple
    # of attributes, as some tests do) can still call the delegate methods.

    @property
    def _session_turn(self) -> SessionTurnService:
        return _get_session_turn(self)

    @_session_turn.setter
    def _session_turn(self, service: SessionTurnService) -> None:
        self.__dict__["_session_turn"] = service

    # -- runtime resource registry -------------------------------------------
    #
    # Consolidator / AutoCompact / Dream / DreamIdleTrigger live in
    # ``RuntimeResourceRegistry``; these read-only properties expose them
    # through the loop's historical surface.

    @property
    def consolidator(self) -> Consolidator:
        return self._resources.consolidator

    @property
    def auto_compact(self) -> AutoCompact:
        return self._resources.auto_compact

    @property
    def dream(self) -> Dream:
        return self._resources.dream

    @property
    def dream_idle_trigger(self) -> Any:
        return self._resources.dream_idle_trigger

    def _persist_user_message_early(
        self,
        msg: InboundMessage,
        session: Session,
        **kwargs: Any,
    ) -> bool:
        return _get_session_turn(self)._persist_user_message_early(msg, session, **kwargs)

    def _sanitize_persisted_blocks(
        self,
        content: list[dict[str, Any]],
        *,
        should_truncate_text: bool = False,
        drop_runtime: bool = False,
    ) -> list[dict[str, Any]]:
        return _get_session_turn(self)._sanitize_persisted_blocks(
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
        _get_session_turn(self)._save_turn(
            session,
            messages,
            skip,
            turn_latency_ms=turn_latency_ms,
        )

    def _persist_subagent_followup(self, session: Session, msg: InboundMessage) -> bool:
        return _get_session_turn(self)._persist_subagent_followup(session, msg)

    def _set_runtime_checkpoint(self, session: Session, payload: dict[str, Any]) -> None:
        _get_session_turn(self)._set_runtime_checkpoint(session, payload)

    def _restore_runtime_checkpoint(self, session: Session) -> bool:
        return _get_session_turn(self)._restore_runtime_checkpoint(session)

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

    # -- per-workspace memory / runtime helpers ------------------------------
    #
    # These live in ``RuntimeResourceRegistry``; the loop keeps thin delegates
    # so commands, tools, and tests keep using the loop's surface unchanged.

    @staticmethod
    def _resolved_root(workspace: Path | str) -> str:
        return RuntimeResourceRegistry._resolved_root(workspace)

    def memory_for(self, workspace: Path | str | None = None) -> MemoryStore:
        """Resolve the governed ``MemoryStore`` for an effective workspace."""
        return self._resources.memory_for(workspace)

    def _dream_for(self, workspace: Path | str) -> Dream:
        """Return the Dream bound to a resolved effective workspace."""
        return self._resources._dream_for(workspace)

    async def run_all_dreams(self) -> bool:
        """Run every known Dream (default + effective workspaces)."""
        return await self._resources.run_all_dreams()

    def _sync_runtime_helpers(
        self,
        provider: LLMProvider,
        model: str,
        context_window_tokens: int,
    ) -> None:
        """Propagate a provider snapshot to every cached workspace helper."""
        self._resources._sync_runtime_helpers(provider, model, context_window_tokens)

    async def _build_bus_progress_callback(self, msg: InboundMessage) -> ProgressCallback:
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
        return self.context.build_messages(
            history=history,
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=msg.channel,
            chat_id=self._runtime_chat_id(msg),
            sender_id=msg.sender_id,
            session_key=session.key,
            session_summary=pending_summary,
            session_metadata=session.metadata,
            workspace=scope.project_path,
            runtime_state=self,
            inbound_message=msg,
            agent_override=agent_override,
        )

    # -- dispatcher delegation ------------------------------------------------
    #
    # Inbound-message routing (the run loop, per-session dispatch, command
    # shortcuts, cancellation, and background tasks) lives in
    # ``MessageDispatcher``; these thin delegates keep the loop's public
    # surface unchanged for commands, tools, and tests.

    @property
    def _running(self) -> bool:
        return self._dispatcher._running

    @_running.setter
    def _running(self, value: bool) -> None:
        self._dispatcher._running = value

    @property
    def _active_tasks(self) -> dict[str, list[asyncio.Task]]:
        return self._dispatcher._active_tasks

    @property
    def _background_tasks(self) -> set[asyncio.Task]:
        return self._dispatcher._background_tasks

    @property
    def _session_locks(self) -> WeakValueDictionary[str, asyncio.Lock]:
        return self._dispatcher._session_locks

    @property
    def _pending_queues(self) -> dict[str, asyncio.Queue]:
        return self._dispatcher._pending_queues

    @property
    def _concurrency_gate(self) -> asyncio.Semaphore | None:
        return self._dispatcher._concurrency_gate

    @_concurrency_gate.setter
    def _concurrency_gate(self, value: asyncio.Semaphore | None) -> None:
        self._dispatcher._concurrency_gate = value

    async def run(self) -> None:
        await self._dispatcher.run()

    def stop(self) -> None:
        self._dispatcher.stop()

    async def _dispatch(self, msg: InboundMessage) -> None:
        await self._dispatcher._dispatch(msg)

    async def _cancel_active_tasks(self, key: str) -> int:
        return await self._dispatcher._cancel_active_tasks(key)

    def _schedule_background(self, coro) -> None:
        self._dispatcher._schedule_background(coro)

    async def _process_system_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: ProgressCallback | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        pending_queue: asyncio.Queue | None = None,
    ) -> OutboundMessage | None:
        return await self._dispatcher._process_system_message(
            msg,
            session_key=session_key,
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            pending_queue=pending_queue,
        )

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
        on_progress: ProgressCallback | None = None,
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
        user_key: str | None = None,
        pending_queue: asyncio.Queue | None = None,
        agent_override: SubagentDefinition | None = None,
        turn_hooks: list[AgentHook] | None = None,
    ) -> tuple[str | None, list[str], list[dict], str, bool]:
        """Run the agent iteration loop.

        *on_stream*: called with each content delta during streaming.
        *on_stream_end(resuming)*: called when a streaming session finishes.
        ``resuming=True`` means tool calls follow (spinner should restart);
        ``resuming=False`` means this is the final response.

        *turn_hooks*: per-dispatch hooks bound to this single turn (e.g. SDK
        capture hook). Combined with the loop-level ``_extra_hooks`` so the
        SDK no longer mutates shared state for concurrent runs.

        Returns (final_content, tools_used, messages, stop_reason, had_injections).
        """
        self._sync_subagent_runtime_limits()

        loop_hook = AgentProgressHook(
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            channel=channel,
            chat_id=chat_id,
            message_id=message_id,
            metadata=metadata,
            session_key=session_key,
            tool_hint_max_length=self.tool_hint_max_length,
            set_tool_context=self._set_tool_context,
            on_iteration=lambda iteration: setattr(self, "_current_iteration", iteration),
        )
        # Per-turn hooks take precedence over loop-level _extra_hooks so the
        # SDK can pass distinct hooks for concurrent runs without serializing
        # through shared mutable state.
        extra = list(self._extra_hooks) + list(turn_hooks or [])
        hook: AgentHook = CompositeHook([loop_hook] + extra) if extra else loop_hook

        async def _checkpoint(payload: dict[str, Any]) -> None:
            if session is None:
                return
            self._set_runtime_checkpoint(session, payload)

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

            def _to_user_message(pending_msg: InboundMessage) -> dict[str, Any]:
                content = pending_msg.content
                media = pending_msg.media if pending_msg.media else None
                if media:
                    content, media = self._prepare_message_media(content, media)
                    media = media or None
                user_content = self.context._build_user_content(content, media)
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
                and self.subagents.get_running_count_by_session(session.key) > 0
            ):
                try:
                    msg = await asyncio.wait_for(
                        pending_queue.get(), timeout=_SUBAGENT_DRAIN_WAIT_S
                    )
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
        effective_scope = self.workspace_scopes.for_turn(
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
        file_state_token = bind_file_states(self._file_state_store.for_session(active_session_key))
        request_token = bind_request_context(request_ctx)
        workspace_token = bind_workspace_scope(effective_scope)
        # Per-turn telemetry: reuse a telemetry already bound by the dispatch
        # layer (so usage survives through turn_end), otherwise create one for
        # direct _run_agent_loop callers and reset it when the turn completes.
        telemetry = turn_telemetry.current()
        telemetry_token = None
        if telemetry is None:
            telemetry = turn_telemetry.TurnTelemetry()
            telemetry_token = turn_telemetry.bind(telemetry)
        # Apply subagent takeover overrides: filter tools to the subagent's
        # whitelist (if any) and select its model (falling back to self.model).
        if agent_override is not None:
            if agent_override.tools is not None:
                tools = self._filter_tools_for_override(agent_override.tools)
            else:
                tools = self.tools
            run_model = agent_override.model or self.model
        else:
            tools = self.tools
            run_model = self.model
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
            result = await self.runner.run(
                AgentRunSpec(
                    initial_messages=initial_messages,
                    tools=tools,
                    model=run_model,
                    max_iterations=self.max_iterations,
                    max_tool_result_chars=self.max_tool_result_chars,
                    hook=hook,
                    error_message="Sorry, I encountered an error calling the AI model.",
                    concurrent_tools=True,
                    workspace=effective_scope.project_path,
                    session_key=session.key if session else session_key,
                    user_key=user_key,
                    context_window_tokens=self.context_window_tokens,
                    context_block_limit=self.context_block_limit,
                    provider_retry_mode=self.provider_retry_mode,
                    progress_callback=on_progress,
                    stream_progress_deltas=on_stream is not None,
                    retry_wait_callback=on_retry_wait,
                    checkpoint_callback=_checkpoint,
                    high_risk_policy=self.high_risk_policy,
                    injection_callback=_drain_pending,
                    # Sustained goals may legitimately exceed MINIUNICORN_LLM_TIMEOUT_S; idle stall
                    # is still capped by MINIUNICORN_STREAM_IDLE_TIMEOUT_S in streaming providers.
                    llm_timeout_s=runner_wall_llm_timeout_s(
                        self.sessions,
                        session.key if session is not None else session_key,
                        metadata=(session.metadata if session is not None else None),
                    ),
                    goal_active_predicate=lambda: (
                        sustained_goal_active(session.metadata) if session is not None else False
                    ),
                    goal_continue_message=_goal_continue,
                    # Plan-and-Execute / Reflection / TurnBudget (opt-in via config).
                    use_planner=self.use_planner,
                    planner_model=self.planner_model,
                    planner_max_replans=self.planner_max_replans,
                    planning_policy=self.planning_policy,
                    enable_reflection=self.enable_reflection,
                    reflection_interval=self.reflection_interval,
                    turn_budget=self._build_turn_budget(),
                )
            )
        finally:
            reset_workspace_scope(workspace_token)
            reset_request_context(request_token)
            reset_file_states(file_state_token)
            if telemetry_token is not None:
                turn_telemetry.reset(telemetry_token)
        self._response.record_last_usage(result)
        telemetry = turn_telemetry.current()
        if telemetry is not None:
            telemetry.usage = dict(result.usage or {})
            telemetry.last_call_usage = dict(result.last_call_usage or {})
        if result.stop_reason == "max_iterations":
            logger.warning("Max iterations ({}) reached", self.max_iterations)
            # Push final content through stream so streaming channels (e.g. Feishu)
            # update the card instead of leaving it empty.
            if on_stream and on_stream_end:
                await on_stream(result.final_content or "")
                await on_stream_end(resuming=False)
        elif result.stop_reason == "error":
            logger.error("LLM returned error: {}", (result.final_content or "")[:200])
        return (
            result.final_content,
            result.tools_used,
            result.messages,
            result.stop_reason,
            result.had_injections,
        )

    async def _dispatch_command_for_turn(
        self,
        msg: InboundMessage,
        session: Session | None,
        key: str,
        raw: str,
    ) -> OutboundMessage | None:
        """Dispatch a slash command, binding this loop into the CommandContext.

        Injected into the turn orchestrator's ``commands`` dep so shortcut
        commands can keep reaching the loop (commands read ``ctx.loop`` for
        cancellation, sessions, consolidator, …) without the orchestrator
        ever referencing the loop type.
        """
        cmd_ctx = CommandContext(msg=msg, session=session, key=key, raw=raw, loop=self)
        return await self.commands.dispatch(cmd_ctx)

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: ProgressCallback | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        pending_queue: asyncio.Queue | None = None,
        turn_hooks: list[AgentHook] | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response."""
        return await self._turn_orchestrator.process_turn(
            msg,
            session_key=session_key,
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            pending_queue=pending_queue,
            turn_hooks=turn_hooks,
        )

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        media: list[str] | None = None,
        on_progress: ProgressCallback | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        hooks: list[AgentHook] | None = None,
    ) -> OutboundMessage | None:
        """Process a message directly and return the outbound payload.

        *hooks*: per-call lifecycle hooks bound to this single turn only.
        The SDK uses this instead of mutating the loop's shared
        ``_extra_hooks`` list, so concurrent ``process_direct`` calls with
        different hooks no longer cross-contaminate each other's results.
        """
        await self._connect_mcp()
        msg = InboundMessage(
            channel=channel,
            sender_id="user",
            chat_id=chat_id,
            content=content,
            media=media or [],
        )
        try:
            return await self._process_message(
                msg,
                session_key=session_key,
                on_progress=on_progress,
                on_stream=on_stream,
                on_stream_end=on_stream_end,
                turn_hooks=hooks,
            )
        finally:
            if channel == "websocket":
                await self._webui_turns.publish_run_status(msg, "idle")
                self._response.pop_pending_turn_latency(session_key)
                self._webui_turns.discard(session_key)


# Re-export for backwards compatibility (tests/extensions may import from loop)
__all__ = [
    "AgentLoop",
    "StateTraceEntry",
    "TurnContext",
    "TurnState",
    "extract_documents",
]
