"""Builder pattern for AgentLoop construction.

提供链式 API 分步构建 AgentLoop,减少直接调用 ``AgentLoop.__init__`` 时
大量位置参数带来的可读性问题。``AgentLoop.from_config`` 内部使用此 builder。
Phase 4 起 ``AgentLoop`` 退化为兼容 Facade:builder 把 ``with_*`` 收集的参数
折叠进 ``AgentLoopConfig`` 配置束并注入,五个服务对象缺省时由 Facade 构造。

典型用法::

    loop = (
        AgentLoopBuilder(bus, provider, workspace)
        .with_model("openai/gpt-4o")
        .with_max_iterations(20)
        .with_context_window_tokens(128_000)
        .build()
    )

从 config 构建::

    loop = AgentLoopBuilder.from_config(config, bus=bus).build()
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Any

from miniunicorn.agent import model_presets as preset_helpers
from miniunicorn.agent.planning_policy import PlanningPolicy
from miniunicorn.bus.queue import MessageBus
from miniunicorn.providers.base import LLMProvider
from miniunicorn.providers.factory import make_provider

if TYPE_CHECKING:
    from miniunicorn.agent.hook import AgentHook
    from miniunicorn.agent.loop import AgentLoop
    from miniunicorn.agent.subagent import SubagentManager
    from miniunicorn.config.schema import ModelPresetConfig, ToolsConfig
    from miniunicorn.cron.service import CronService
    from miniunicorn.session.manager import SessionManager


# Facade 注入的服务对象参数名(AgentLoop.__init__ 关键字参数)。
_SERVICE_PARAMS = (
    "dispatcher",
    "session_turn",
    "resources",
    "turn_orchestrator",
    "response",
)


class AgentLoopBuilder:
    """链式构建 AgentLoop 实例。

    必需参数(bus/provider/workspace)在构造时传入,其余参数通过
    ``with_*`` 方法设置,未设置的参数使用 ``AgentLoop.__init__`` 的默认值。
    """

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
    ) -> None:
        self._kwargs: dict[str, Any] = {
            "bus": bus,
            "provider": provider,
            "workspace": workspace,
        }

    # --- 模型 / 迭代参数 ---

    def with_model(self, model: str | None) -> AgentLoopBuilder:
        self._kwargs["model"] = model
        return self

    def with_max_iterations(self, max_iterations: int | None) -> AgentLoopBuilder:
        self._kwargs["max_iterations"] = max_iterations
        return self

    def with_fast_max_tool_iterations(
        self, fast_max_tool_iterations: int | None
    ) -> AgentLoopBuilder:
        self._kwargs["fast_max_tool_iterations"] = fast_max_tool_iterations
        return self

    def with_managed_max_tool_iterations(
        self, managed_max_tool_iterations: int | None
    ) -> AgentLoopBuilder:
        self._kwargs["managed_max_tool_iterations"] = managed_max_tool_iterations
        return self

    def with_max_concurrent_subagents(
        self, max_concurrent_subagents: int | None
    ) -> AgentLoopBuilder:
        self._kwargs["max_concurrent_subagents"] = max_concurrent_subagents
        return self

    def with_max_subagent_recursion_depth(
        self, max_subagent_recursion_depth: int | None
    ) -> AgentLoopBuilder:
        self._kwargs["max_subagent_recursion_depth"] = max_subagent_recursion_depth
        return self

    def with_context_window_tokens(self, context_window_tokens: int | None) -> AgentLoopBuilder:
        self._kwargs["context_window_tokens"] = context_window_tokens
        return self

    def with_context_block_limit(self, context_block_limit: int | None) -> AgentLoopBuilder:
        self._kwargs["context_block_limit"] = context_block_limit
        return self

    def with_max_tool_result_chars(self, max_tool_result_chars: int | None) -> AgentLoopBuilder:
        self._kwargs["max_tool_result_chars"] = max_tool_result_chars
        return self

    def with_max_tool_result_tokens(self, max_tool_result_tokens: int | None) -> AgentLoopBuilder:
        self._kwargs["max_tool_result_tokens"] = max_tool_result_tokens
        return self

    def with_provider_retry_mode(self, provider_retry_mode: str) -> AgentLoopBuilder:
        self._kwargs["provider_retry_mode"] = provider_retry_mode
        return self

    def with_tool_hint_max_length(self, tool_hint_max_length: int | None) -> AgentLoopBuilder:
        self._kwargs["tool_hint_max_length"] = tool_hint_max_length
        return self

    # --- 工具 / MCP / 安全 ---

    def with_cron_service(self, cron_service: CronService | None) -> AgentLoopBuilder:
        self._kwargs["cron_service"] = cron_service
        return self

    def with_restrict_to_workspace(self, restrict_to_workspace: bool) -> AgentLoopBuilder:
        self._kwargs["restrict_to_workspace"] = restrict_to_workspace
        return self

    def with_high_risk_policy(self, high_risk_policy: str) -> AgentLoopBuilder:
        self._kwargs["high_risk_policy"] = high_risk_policy
        return self

    def with_session_manager(self, session_manager: SessionManager | None) -> AgentLoopBuilder:
        self._kwargs["session_manager"] = session_manager
        return self

    def with_subagent_manager(self, subagent_manager: SubagentManager | None) -> AgentLoopBuilder:
        self._kwargs["subagent_manager"] = subagent_manager
        return self

    def with_mcp_servers(self, mcp_servers: dict | None) -> AgentLoopBuilder:
        self._kwargs["mcp_servers"] = mcp_servers
        return self

    def with_channels_config(self, channels_config: Any | None) -> AgentLoopBuilder:
        self._kwargs["channels_config"] = channels_config
        return self

    def with_timezone(self, timezone: str | None) -> AgentLoopBuilder:
        self._kwargs["timezone"] = timezone
        return self

    def with_session_ttl_minutes(self, session_ttl_minutes: int) -> AgentLoopBuilder:
        self._kwargs["session_ttl_minutes"] = session_ttl_minutes
        return self

    def with_consolidation_ratio(self, consolidation_ratio: float) -> AgentLoopBuilder:
        self._kwargs["consolidation_ratio"] = consolidation_ratio
        return self

    def with_max_messages(self, max_messages: int) -> AgentLoopBuilder:
        self._kwargs["max_messages"] = max_messages
        return self

    def with_hooks(self, hooks: list[AgentHook] | None) -> AgentLoopBuilder:
        self._kwargs["hooks"] = hooks
        return self

    def with_unified_session(self, unified_session: bool) -> AgentLoopBuilder:
        self._kwargs["unified_session"] = unified_session
        return self

    def with_disabled_skills(self, disabled_skills: list[str] | None) -> AgentLoopBuilder:
        self._kwargs["disabled_skills"] = disabled_skills
        return self

    def with_tools_config(self, tools_config: ToolsConfig | None) -> AgentLoopBuilder:
        self._kwargs["tools_config"] = tools_config
        return self

    # --- Provider 切换 / 预设 ---

    def with_provider_snapshot_loader(
        self,
        provider_snapshot_loader: Any | None,
    ) -> AgentLoopBuilder:
        self._kwargs["provider_snapshot_loader"] = provider_snapshot_loader
        return self

    def with_provider_signature(
        self,
        provider_signature: tuple[object, ...] | None,
    ) -> AgentLoopBuilder:
        self._kwargs["provider_signature"] = provider_signature
        return self

    def with_model_presets(
        self,
        model_presets: dict[str, ModelPresetConfig] | None,
    ) -> AgentLoopBuilder:
        self._kwargs["model_presets"] = model_presets
        return self

    def with_model_preset(self, model_preset: str | None) -> AgentLoopBuilder:
        self._kwargs["model_preset"] = model_preset
        return self

    def with_preset_snapshot_loader(
        self,
        preset_snapshot_loader: preset_helpers.PresetSnapshotLoader | None,
    ) -> AgentLoopBuilder:
        self._kwargs["preset_snapshot_loader"] = preset_snapshot_loader
        return self

    def with_runtime_model_publisher(
        self,
        runtime_model_publisher: Any | None,
    ) -> AgentLoopBuilder:
        self._kwargs["runtime_model_publisher"] = runtime_model_publisher
        return self

    def with_structured_memory_config(
        self,
        structured_memory_config: Any | None,
    ) -> AgentLoopBuilder:
        self._kwargs["structured_memory_config"] = structured_memory_config
        return self

    # --- 便捷方法 ---

    def with_use_planner(self, use_planner: bool) -> AgentLoopBuilder:
        self._kwargs["use_planner"] = use_planner
        return self

    def with_planning_policy(self, policy: PlanningPolicy) -> AgentLoopBuilder:
        """Programmatic construction only; from_config() resolves from
        use_planner fields for backward compatibility."""
        self._kwargs["planning_policy"] = policy
        return self

    def with_planner_model(self, planner_model: str | None) -> AgentLoopBuilder:
        self._kwargs["planner_model"] = planner_model
        return self

    def with_planner_max_replans(self, planner_max_replans: int) -> AgentLoopBuilder:
        self._kwargs["planner_max_replans"] = planner_max_replans
        return self

    def with_enable_reflection(self, enable_reflection: bool) -> AgentLoopBuilder:
        self._kwargs["enable_reflection"] = enable_reflection
        return self

    def with_reflection_interval(self, reflection_interval: int) -> AgentLoopBuilder:
        self._kwargs["reflection_interval"] = reflection_interval
        return self

    def with_max_input_tokens_per_turn(
        self, max_input_tokens_per_turn: int | None
    ) -> AgentLoopBuilder:
        self._kwargs["max_input_tokens_per_turn"] = max_input_tokens_per_turn
        return self

    def with_max_cost_per_turn_usd(self, max_cost_per_turn_usd: float | None) -> AgentLoopBuilder:
        self._kwargs["max_cost_per_turn_usd"] = max_cost_per_turn_usd
        return self

    def with_managed_max_input_tokens_per_turn(
        self, managed_max_input_tokens_per_turn: int | None
    ) -> AgentLoopBuilder:
        self._kwargs["managed_max_input_tokens_per_turn"] = managed_max_input_tokens_per_turn
        return self

    def with_managed_max_cost_per_turn_usd(
        self, managed_max_cost_per_turn_usd: float | None
    ) -> AgentLoopBuilder:
        self._kwargs["managed_max_cost_per_turn_usd"] = managed_max_cost_per_turn_usd
        return self

    def with_fast_max_input_tokens_per_turn(
        self, fast_max_input_tokens_per_turn: int | None
    ) -> AgentLoopBuilder:
        self._kwargs["fast_max_input_tokens_per_turn"] = fast_max_input_tokens_per_turn
        return self

    def with_fast_max_cost_per_turn_usd(
        self, fast_max_cost_per_turn_usd: float | None
    ) -> AgentLoopBuilder:
        self._kwargs["fast_max_cost_per_turn_usd"] = fast_max_cost_per_turn_usd
        return self

    def with_max_turn_wall_time_s(self, max_turn_wall_time_s: float | None) -> AgentLoopBuilder:
        self._kwargs["max_turn_wall_time_s"] = max_turn_wall_time_s
        return self

    def with_enable_step_verifier(self, enable_step_verifier: bool) -> AgentLoopBuilder:
        self._kwargs["enable_step_verifier"] = enable_step_verifier
        return self

    def with_extra(self, **kwargs: Any) -> AgentLoopBuilder:
        """追加任意关键字参数(用于注入服务对象或新增参数)。

        过渡期接口：优先使用显式 ``with_*`` 方法，避免依赖关键字名。
        """
        if kwargs:
            warnings.warn(
                "AgentLoopBuilder.with_extra(**kwargs) is deprecated; "
                "use explicit with_* methods instead.",
                DeprecationWarning,
                stacklevel=2,
            )
        self._kwargs.update(kwargs)
        return self

    def build(self) -> AgentLoop:
        """构建服务束并注入 AgentLoop 实例。

        Phase 4 起 ``AgentLoop`` 退化为兼容 Facade:构造参数收窄为
        ``bus`` + ``workspace`` + 配置束 + 五个服务对象。这里把所有
        ``with_*`` 收集的参数折叠进 ``AgentLoopConfig`` 配置束,并把
        通过 ``with_extra`` 注入的五个服务对象(dispatcher/session_turn/
        resources/turn_orchestrator/response)单独透传;未注入的服务由
        Facade 依据配置束默认构造。
        """
        # 延迟导入避免循环依赖(builder 在 loop.py 中被导入)
        from miniunicorn.agent.loop import AgentLoop, AgentLoopConfig

        kwargs = dict(self._kwargs)
        bus = kwargs.pop("bus")
        workspace = kwargs.pop("workspace")
        services = {key: kwargs.pop(key) for key in _SERVICE_PARAMS if key in kwargs}
        return AgentLoop(bus, workspace, config=AgentLoopConfig(**kwargs), **services)

    # --- 从 config 构建 ---

    @classmethod
    def from_config(
        cls,
        config: Any,
        bus: MessageBus | None = None,
        **extra: Any,
    ) -> AgentLoopBuilder:
        """从 Config 对象创建 builder,预填所有 config 派生的参数。

        ``extra`` 中的关键字参数会覆盖 config 派生的值,也可以通过
        ``with_*`` 方法进一步覆盖。
        """
        if bus is None:
            bus = MessageBus()
        defaults = config.agents.defaults
        provider = extra.pop("provider", None) or make_provider(config)
        resolved = config.resolve_preset()
        model = extra.pop("model", None) or resolved.model
        context_window_tokens = (
            extra.pop("context_window_tokens", None) or resolved.context_window_tokens
        )
        provider_snapshot_loader = extra.pop("provider_snapshot_loader", None)
        preset_snapshot_loader = extra.pop(
            "preset_snapshot_loader", None
        ) or preset_helpers.make_preset_snapshot_loader(
            config,
            provider_snapshot_loader,
        )

        builder = cls(bus, provider, config.workspace_path)
        builder.with_model(model)
        # T1: Only pass max_iterations if explicitly set in config (not default),
        # so tiered logic in AgentLoop.__init__ can select by planning mode.
        if "max_tool_iterations" in config.agents.defaults.__pydantic_fields_set__:
            builder.with_max_iterations(defaults.max_tool_iterations)
        builder.with_max_concurrent_subagents(defaults.max_concurrent_subagents)
        builder.with_max_subagent_recursion_depth(defaults.max_subagent_recursion_depth)
        builder.with_context_window_tokens(context_window_tokens)
        builder.with_context_block_limit(defaults.context_block_limit)
        # T4: Token budget priority - explicit chars wins (tokens=chars//4),
        # else explicit tokens, else default 4000.
        if "max_tool_result_chars" in config.agents.defaults.__pydantic_fields_set__:
            builder.with_max_tool_result_chars(defaults.max_tool_result_chars)
        elif "max_tool_result_tokens" in config.agents.defaults.__pydantic_fields_set__:
            builder.with_max_tool_result_tokens(defaults.max_tool_result_tokens)
        else:
            builder.with_max_tool_result_tokens(4000)
        builder.with_provider_retry_mode(defaults.provider_retry_mode)
        builder.with_tool_hint_max_length(defaults.tool_hint_max_length)
        builder.with_use_planner(defaults.use_planner)
        builder.with_planner_model(defaults.planner_model)
        builder.with_planner_max_replans(defaults.planner_max_replans)
        builder.with_enable_reflection(defaults.enable_reflection)
        builder.with_reflection_interval(defaults.reflection_interval)
        builder.with_max_input_tokens_per_turn(defaults.max_input_tokens_per_turn)
        builder.with_max_cost_per_turn_usd(defaults.max_cost_per_turn_usd)
        builder.with_managed_max_input_tokens_per_turn(defaults.managed_max_input_tokens_per_turn)
        builder.with_managed_max_cost_per_turn_usd(defaults.managed_max_cost_per_turn_usd)
        builder.with_fast_max_input_tokens_per_turn(defaults.fast_max_input_tokens_per_turn)
        builder.with_fast_max_cost_per_turn_usd(defaults.fast_max_cost_per_turn_usd)
        builder.with_fast_max_tool_iterations(defaults.fast_max_tool_iterations)
        builder.with_managed_max_tool_iterations(defaults.managed_max_tool_iterations)
        builder.with_max_turn_wall_time_s(defaults.max_turn_wall_time_s)
        builder.with_enable_step_verifier(defaults.enable_step_verifier)
        builder.with_restrict_to_workspace(config.tools.restrict_to_workspace)
        builder.with_high_risk_policy(config.tools.high_risk_policy)
        builder.with_mcp_servers(config.tools.mcp_servers)
        builder.with_channels_config(config.channels)
        builder.with_timezone(defaults.timezone)
        builder.with_unified_session(defaults.unified_session)
        builder.with_disabled_skills(defaults.disabled_skills)
        builder.with_session_ttl_minutes(defaults.session_ttl_minutes)
        builder.with_consolidation_ratio(defaults.consolidation_ratio)
        builder.with_max_messages(defaults.max_messages)
        builder.with_tools_config(config.tools)
        builder.with_model_presets(preset_helpers.configured_model_presets(config))
        builder.with_model_preset(defaults.model_preset)
        builder.with_provider_snapshot_loader(provider_snapshot_loader)
        builder.with_preset_snapshot_loader(preset_snapshot_loader)
        builder.with_structured_memory_config(defaults.structured_memory)
        # 保留 extra 中的覆盖项
        if extra:
            builder.with_extra(**extra)
        return builder
