"""Configuration schema using Pydantic."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel
from pydantic_settings import BaseSettings

from miniunicorn.cron.types import CronSchedule

if TYPE_CHECKING:
    from miniunicorn.agent.tools.cli_apps import CliAppsToolConfig
    from miniunicorn.agent.tools.deep_research.config import DeepResearchConfig
    from miniunicorn.agent.tools.image_generation.config import ImageGenerationConfig
    from miniunicorn.agent.tools.self import MyToolConfig
    from miniunicorn.agent.tools.shell import ExecToolConfig
    from miniunicorn.agent.tools.web import WebToolsConfig
    from miniunicorn.agent.tools.web_search.config import WebSearchConfig


class Base(BaseModel):
    """Base model that accepts both camelCase and snake_case keys."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ChannelsConfig(Base):
    """Configuration for chat channels.

    QwenPaw-style: built-in channel configs are declared as explicit fields
    (each channel lives in ``miniunicorn/channels/<name>/`` and parses its
    own config dict in ``__init__``). Plugin channel configs are still
    stored via ``extra="allow"`` (``__pydantic_extra__`` dict).
    Per-channel ``"streaming": true`` enables streaming output (requires
    send_delta impl).
    """

    model_config = ConfigDict(extra="allow")

    send_progress: bool = True  # stream agent's text progress to the channel
    send_tool_hints: bool = False  # stream tool-call hints (e.g. read_file("…"))
    show_reasoning: bool = True  # surface model reasoning when channel implements it
    extract_document_text: bool = True  # extract text from document attachments before sending to the model
    send_max_retries: int = Field(default=3, ge=0, le=10)  # Max delivery attempts (initial send included)
    transcription_provider: str = "groq"  # Voice transcription backend: "groq" or "openai"
    transcription_language: str | None = Field(default=None, pattern=r"^[a-z]{2,3}$")  # Optional ISO-639-1 hint for audio transcription

    # Built-in channel configs (QwenPaw-style explicit fields). None = not
    # configured / disabled; dict = parsed by the channel's own Config class
    # in __init__. Plugin channels still use the extras dict.
    feishu: dict[str, Any] | None = None
    dingtalk: dict[str, Any] | None = None
    qq: dict[str, Any] | None = None
    wecom: dict[str, Any] | None = None
    weixin: dict[str, Any] | None = None
    websocket: dict[str, Any] | None = None


class DreamConfig(Base):
    """Dream memory consolidation configuration.

    Dream 走标准 cron 表达式,默认每天凌晨 3 点触发一次。
    Gateway 启动时会检查距上次做梦是否错过了一个或多个触发点,
    若错过则立刻补跑一次,然后按当前时间重排下一次执行。
    """

    enabled: bool = True  # Register the periodic Dream consolidation job on startup
    # 5 字段 Unix cron 表达式(minute hour day-of-month month day-of-week)。
    # 默认 "0 3 * * *" = 每天凌晨 3 点。时区由 agents.defaults.timezone 决定。
    cron: str = Field(default="0 3 * * *")
    model_override: str | None = Field(
        default=None,
        validation_alias=AliasChoices("modelOverride", "model", "model_override"),
    )  # Optional Dream-specific model override
    max_batch_size: int = Field(default=20, ge=1)  # Max history entries per run
    # Bumped from 10 to 15 in #3212 (exp002: +30% dedup, no accuracy loss; >15 plateaus).
    max_iterations: int = Field(default=15, ge=1)  # Max tool calls per Phase 2
    # Per-line git-blame age annotation in Phase 1 prompt (see #3212). Default
    # on — set to False to feed MEMORY.md raw if a specific LLM reacts poorly
    # to the `← Nd` suffix or you want deterministic, git-independent prompts.
    annotate_line_ages: bool = True
    # 空闲触发：用户不使用时在后台触发 Dream，不依赖 cron 定时。
    # 解决"用户不 24 小时运行 gateway，凌晨 cron 点大概率关机"的问题。
    # 借鉴 Claude Dreaming 的"会话间空闲自动触发"机制。
    idle_trigger_enabled: bool = True
    idle_trigger_min_seconds: int = Field(default=300, ge=0)  # 空闲多久才触发（5分钟）
    idle_trigger_min_entries: int = Field(default=5, ge=1)  # 至少多少新数据才值得 dream
    idle_trigger_min_interval_s: int = Field(default=3600, ge=0)  # 两次 dream 最小间隔
    # 启动时积压检查：gateway 启动时若未处理历史超过此阈值，立即触发一次 dream。
    # 解决"连续多天不开，catch_up 只补 1 次"的漏洞。
    # 默认 2 * max_batch_size，设为 0 禁用。
    startup_backlog_threshold: int = Field(default=40, ge=0)

    def build_schedule(self, timezone: str) -> CronSchedule:
        """Build the runtime cron schedule for this Dream config."""
        return CronSchedule(kind="cron", expr=self.cron, tz=timezone)

    def describe_schedule(self) -> str:
        """Return a human-readable summary for logs and startup output."""
        return f"cron {self.cron}"


class InlineFallbackConfig(Base):
    """One inline fallback model configuration."""

    model: str
    provider: str
    max_tokens: int | None = None
    context_window_tokens: int | None = None
    temperature: float | None = None
    reasoning_effort: str | None = None


FallbackCandidate = str | InlineFallbackConfig


class ModelPresetConfig(Base):
    """A named set of model + generation parameters for quick switching."""

    label: str | None = None
    model: str
    provider: str = "auto"
    max_tokens: int = 8192
    # None means auto-detect from the built-in model metadata table
    # (see cli.models.get_model_context_limit). Falls back to 65_536 when
    # the model is not in the table. Set an explicit int to override.
    context_window_tokens: int | None = None
    temperature: float = 0.1
    reasoning_effort: str | None = None

    # Per-preset credentials (optional). When non-empty, these override the
    # corresponding fields on ``config.providers.<name>`` at runtime. This
    # enables multiple independent "custom" OpenAI-compatible endpoints —
    # each preset carries its own api_key/api_base instead of sharing the
    # single ``config.providers.custom`` slot.
    api_key: str | None = None
    api_base: str | None = None
    extra_headers: dict[str, str] | None = None
    extra_body: dict[str, Any] | None = None

    def to_generation_settings(self) -> Any:
        from miniunicorn.providers.base import GenerationSettings
        return GenerationSettings(
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            reasoning_effort=self.reasoning_effort,
        )


class AgentDefaults(Base):
    """Default agent configuration."""

    workspace: str = "~/.miniunicorn/workspace"
    model_preset: str | None = None  # Active preset name — takes precedence over fields below
    # Empty by default — the user must configure a real model before first
    # use. Previous default "deepseek/deepseek-chat" was only a placeholder
    # and caused HF lookups to match the wrong open-source repo.
    model: str = ""
    provider: str = (
        "auto"  # Provider name (e.g. "anthropic", "openrouter") or "auto" for auto-detection
    )
    max_tokens: int = 8192
    # None means auto-detect via Hugging Face search
    # (see cli.models.get_model_context_limit). Falls back to 65_536 when
    # the model is not found on HF. Set an explicit int to override.
    context_window_tokens: int | None = None
    context_block_limit: int | None = None
    temperature: float = 0.1
    fallback_models: list[FallbackCandidate] = Field(default_factory=list)
    max_tool_iterations: int = 200
    # None = 自适应：根据 provider.is_local 选择默认值（本地 1，云端 4）。
    # 显式指定 int 时，按用户配置生效（向下兼容旧配置）。
    max_concurrent_subagents: int | None = Field(default=None, ge=1)
    # 递归深度限制：子代理可以再 delegate 的层数。
    # 0 = 子代理不能 delegate（主代理仍可，等同原 _scopes={"core"} 硬限制行为）
    # 1 = 允许一层递归（子代理可再 delegate 一次，孙代理不可）— 默认值
    # 2+ = 允许更多层递归
    max_subagent_recursion_depth: int = Field(default=1, ge=0)
    max_tool_result_chars: int = 16_000
    provider_retry_mode: Literal["standard", "persistent"] = "standard"
    tool_hint_max_length: int = Field(
        default=40,
        ge=20,
        le=500,
        validation_alias=AliasChoices("toolHintMaxLength"),
        serialization_alias="toolHintMaxLength",
    )  # Max characters for tool hint display (e.g. "$ cd …/project && npm test")
    reasoning_effort: str | None = None  # low / medium / high / adaptive / none — LLM thinking effort; None preserves the provider default
    timezone: str = "UTC"  # IANA timezone, e.g. "Asia/Shanghai", "America/New_York"
    bot_name: str = "MiniUnicorn"  # Display name shown in CLI prompts (e.g. "{name} is thinking...")
    bot_icon: str = "🐱"  # Short icon (emoji or text) shown next to the bot name in CLI; "" to omit
    unified_session: bool = False  # Share one session across all channels (single-user multi-device)
    disabled_skills: list[str] = Field(default_factory=list)  # Skill names to exclude from loading (e.g. ["summarize", "skill-creator"])
    session_ttl_minutes: int = Field(
        default=1440,
        ge=0,
        validation_alias=AliasChoices("idleCompactAfterMinutes", "sessionTtlMinutes"),
        serialization_alias="idleCompactAfterMinutes",
    )  # Auto-compact idle threshold in minutes (0 = disabled; 默认 1440=24h)
    max_messages: int = Field(
        default=120,
        ge=0,
    )  # Max messages to replay from session history (0 = use default 120, respects token budget)
    consolidation_ratio: float = Field(
        default=0.5,
        ge=0.1,
        le=0.95,
        validation_alias=AliasChoices("consolidationRatio"),
        serialization_alias="consolidationRatio",
    )  # Consolidation target ratio (0.5 = 50% of budget retained after compression)
    checkpoint_ratio: float = Field(
        default=0.7,
        ge=0.3,
        le=1.0,
        validation_alias=AliasChoices("checkpointRatio"),
        serialization_alias="checkpointRatio",
    )  # 提前 checkpoint 触发比例 (0.7 = 70% 预算时触发归档，借鉴 MiMo Code 提前提取思想；1.0 = 旧行为)
    vector_recall: bool = Field(
        default=False,
        validation_alias=AliasChoices("vectorRecall"),
        serialization_alias="vectorRecall",
    )  # Enable vector-based memory recall instead of full MEMORY.md injection
    use_planner: bool = Field(
        default=False,
        validation_alias=AliasChoices("usePlanner"),
        serialization_alias="usePlanner",
    )  # Enable plan-and-execute: decompose complex tasks before execution
    planner_model: str | None = Field(
        default=None,
        validation_alias=AliasChoices("plannerModel"),
        serialization_alias="plannerModel",
    )  # Model for planning (None = use main model)
    planner_max_replans: int = Field(
        default=3,
        ge=0,
        validation_alias=AliasChoices("plannerMaxReplans"),
        serialization_alias="plannerMaxReplans",
    )  # Max replan attempts on step failure
    enable_reflection: bool = Field(
        default=False,
        validation_alias=AliasChoices("enableReflection"),
        serialization_alias="enableReflection",
    )  # Enable post-turn reflection for cross-turn learning
    reflection_interval: int = Field(
        default=5,
        ge=1,
        validation_alias=AliasChoices("reflectionInterval"),
        serialization_alias="reflectionInterval",
    )  # Run reflection every N turns (or on failure)
    max_input_tokens_per_turn: int | None = Field(
        default=None,
        ge=1000,
        validation_alias=AliasChoices("maxInputTokensPerTurn"),
        serialization_alias="maxInputTokensPerTurn",
    )  # Per-turn input token budget (None = unlimited)
    max_cost_per_turn_usd: float | None = Field(
        default=None,
        ge=0.0,
        validation_alias=AliasChoices("maxCostPerTurnUsd"),
        serialization_alias="maxCostPerTurnUsd",
    )  # Per-turn cost budget in USD (None = unlimited)
    embedding_model: str = Field(
        default="text-embedding-3-small",
        validation_alias=AliasChoices("embeddingModel"),
        serialization_alias="embeddingModel",
    )  # Model for generating embeddings
    dream: DreamConfig = Field(default_factory=DreamConfig)


class AgentsConfig(Base):
    """Agent configuration."""

    defaults: AgentDefaults = Field(default_factory=AgentDefaults)


class ProviderConfig(Base):
    """LLM provider configuration."""

    api_key: str | None = None
    api_base: str | None = None
    api_type: Literal["auto", "chat_completions", "responses"] = "auto"  # Request API surface
    extra_headers: dict[str, str] | None = None  # Custom headers (e.g. APP-Code for AiHubMix)
    extra_body: dict[str, Any] | None = None  # Extra provider request fields; shape depends on provider/API surface


class ProvidersConfig(Base):
    """Configuration for LLM providers.

    内置 provider（custom/deepseek/opencode/agnes）已声明字段；其他 provider 通过
    extra="allow" 接受，由 _coerce_extra_providers 把 dict 转为 ProviderConfig。
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="allow")

    custom: ProviderConfig = Field(default_factory=ProviderConfig)  # Any OpenAI-compatible endpoint
    deepseek: ProviderConfig = Field(default_factory=ProviderConfig)
    opencode: ProviderConfig = Field(default_factory=ProviderConfig)
    agnes: ProviderConfig = Field(default_factory=ProviderConfig)

    # Optional separate embedding provider — allows using a different backend
    # for embeddings than for chat (e.g. Anthropic Claude for chat + OpenAI
    # text-embedding-3-small for embeddings). When embedding_provider is None,
    # the main LLM provider's embed() is used (OpenAI-compatible endpoints).
    embedding_provider: str | None = Field(
        default=None,
        validation_alias=AliasChoices("embeddingProvider"),
        serialization_alias="embeddingProvider",
    )  # e.g. "openai" / "custom"; None = reuse the main chat provider
    embedding_model: str = Field(
        default="text-embedding-3-small",
        validation_alias=AliasChoices("embeddingModel"),
        serialization_alias="embeddingModel",
    )  # 嵌入模型（优先级高于 agents.defaults.embedding_model，仅当使用独立 embedding_provider 时生效）
    embedding_api_base: str | None = Field(
        default=None,
        validation_alias=AliasChoices("embeddingApiBase"),
        serialization_alias="embeddingApiBase",
    )  # Optional custom endpoint; defaults to OpenAI when omitted
    embedding_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("embeddingApiKey"),
        serialization_alias="embeddingApiKey",
    )  # Optional custom key; inherits from chat provider when omitted

    @model_validator(mode="after")
    def _coerce_extra_providers(self) -> "ProvidersConfig":
        """把 extra 字段中的 dict 转为 ProviderConfig，保证访问一致。

        同时拒绝与内置 provider 同名的自定义 provider，避免覆盖内置配置。
        """
        _BUILTIN_PROVIDER_NAMES = {"custom", "deepseek", "opencode", "agnes", "openai"}
        for name in list(self.__pydantic_extra__.keys()):
            if name in _BUILTIN_PROVIDER_NAMES:
                raise ValueError(f"自定义 provider 名 '{name}' 与内置 provider 冲突")
        for name, value in list(self.__pydantic_extra__.items()):
            if isinstance(value, dict):
                self.__pydantic_extra__[name] = ProviderConfig.model_validate(value)
        return self


class HeartbeatConfig(Base):
    """Heartbeat service configuration (now backed by cron)."""

    enabled: bool = True
    interval_s: int = 60 * 60  # 1 hour
    keep_recent_messages: int = 8
    # 可选:为 heartbeat 单独指定一个已配置的 model_preset 名称。
    # 留空时 heartbeat 复用 agent 主 provider/model;指定时从 config.model_presets
    # 中加载对应的 preset(包含 model/provider/api_key/api_base),构建独立的
    # provider 供 heartbeat 使用,不影响主对话。
    model_preset: str | None = Field(
        default=None,
        validation_alias=AliasChoices("modelPreset", "model_preset"),
        serialization_alias="modelPreset",
    )
    # 活跃时段限制:仅在指定本地时段内触发心跳,避免半夜空跑浪费 token。
    # 格式 {"start": "08:00", "end": "24:00"};留空则不限制。
    # 默认 08:00-24:00,避免凌晨空跑。
    active_hours: dict[str, str] | None = Field(
        default_factory=lambda: {"start": "08:00", "end": "24:00"}
    )
    # 轻量上下文:心跳跳过 bootstrap 文件(AGENTS.md/SOUL.md/USER.md)注入,
    # 仅保留身份+工具契约+记忆,显著降低 token 消耗。默认开启。
    light_context: bool = Field(default=True)
    # 隔离会话:每次心跳用独立 session_key(heartbeat_<ts>),不累积历史,
    # 避免心跳历史污染主对话上下文。默认开启。
    isolated_session: bool = Field(default=True)


class ApiConfig(Base):
    """OpenAI-compatible API server configuration."""

    host: str = "127.0.0.1"  # Safer default: local-only bind.
    port: int = 8900
    timeout: float = 120.0  # Per-request timeout in seconds.
    # Bearer token required for /v1/* endpoints. Empty = no auth (dev only).
    # When set, clients must send ``Authorization: Bearer <api_key>``.
    api_key: str = ""

    @model_validator(mode="after")
    def _validate_api_security(self) -> "ApiConfig":
        # 绑定到非本地地址时建议设置 api_key，避免公网暴露无鉴权
        if self.host not in ("127.0.0.1", "localhost", "::1") and not self.api_key:
            import logging
            logging.getLogger(__name__).warning(
                "API 绑定到非本地地址 %s 但未设置 api_key，存在公网无鉴权暴露风险", self.host
            )
        return self


class GatewayConfig(Base):
    """Gateway/server configuration."""

    host: str = "127.0.0.1"
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)


class MCPServerConfig(Base):
    """MCP server connection configuration (stdio or HTTP)."""

    type: Literal["stdio", "sse", "streamableHttp"] | None = None  # auto-detected if omitted
    command: str = ""  # Stdio: command to run (e.g. "npx")
    args: list[str] = Field(default_factory=list)  # Stdio: command arguments
    env: dict[str, str] = Field(default_factory=dict)  # Stdio: extra env vars
    cwd: str = ""  # Stdio: working directory for MCP server runtime artifacts
    url: str = ""  # HTTP/SSE: endpoint URL
    headers: dict[str, str] = Field(default_factory=dict)  # HTTP/SSE: custom headers
    tool_timeout: int = 30  # seconds before a tool call is cancelled
    enabled_tools: list[str] = Field(default_factory=lambda: ["*"])  # Only register these tools; accepts raw MCP names or wrapped mcp_<server>_<tool> names; ["*"] = all tools; [] = no tools

    @model_validator(mode="after")
    def _validate_stdio_safety(self) -> "MCPServerConfig":
        # stdio 类型 MCP 服务器:校验 cwd 不能指向系统关键目录,
        # 避免 MCP 服务器进程在系统目录下运行(可能造成文件越权读写)。
        if self.type == "stdio" and self.cwd:
            cwd_path = Path(self.cwd).expanduser()
            # 简单黑名单:不允许 cwd 命中以下系统关键目录
            blocked = {
                "/",
                "/etc",
                "/usr",
                "/bin",
                "/sbin",
                "/var",
                "/root",
                "/boot",
                "/sys",
                "/proc",
                # macOS 上 /etc、/var 等是符号链接,解析后实际指向 /private/...,
                # 一并加入黑名单避免通过符号链接绕过
                "/private/etc",
                "/private/var",
                "/private/tmp",
            }
            # 同时检查原始路径与解析后路径,覆盖符号链接场景
            raw_str = str(cwd_path)
            try:
                resolved = str(cwd_path.resolve())
            except (OSError, RuntimeError):
                # 路径无法解析时只检查原始路径
                resolved = raw_str
            if raw_str in blocked or resolved in blocked:
                raise ValueError(
                    f"MCP server cwd 不允许指向系统目录: {self.cwd}"
                )
        return self


def _lazy_default(module_path: str, class_name: str) -> Any:
    """Deferred import helper for ToolsConfig default factories."""
    import importlib
    module = importlib.import_module(module_path)
    return getattr(module, class_name)()


class ToolsConfig(Base):
    """Tools configuration.

    Field types for tool-specific sub-configs are resolved via model_rebuild()
    at the bottom of this file to avoid circular imports (tool modules import
    Base from schema.py).
    """

    web: WebToolsConfig = Field(default_factory=lambda: _lazy_default("miniunicorn.agent.tools.web", "WebToolsConfig"))
    exec: ExecToolConfig = Field(default_factory=lambda: _lazy_default("miniunicorn.agent.tools.shell", "ExecToolConfig"))
    cli_apps: CliAppsToolConfig = Field(default_factory=lambda: _lazy_default("miniunicorn.agent.tools.cli_apps", "CliAppsToolConfig"))
    my: MyToolConfig = Field(default_factory=lambda: _lazy_default("miniunicorn.agent.tools.self", "MyToolConfig"))
    web_search: WebSearchConfig = Field(default_factory=lambda: _lazy_default("miniunicorn.agent.tools.web_search.config", "WebSearchConfig"))
    deep_research: "DeepResearchConfig" = Field(default_factory=lambda: _lazy_default("miniunicorn.agent.tools.deep_research.config", "DeepResearchConfig"))
    image_generation: "ImageGenerationConfig" = Field(default_factory=lambda: _lazy_default("miniunicorn.agent.tools.image_generation.config", "ImageGenerationConfig"))
    # 默认开启工作区隔离,避免工具越权访问工作区外路径;已有 config.json 中的显式值会覆盖此默认
    restrict_to_workspace: bool = True
    webui_allow_local_service_access: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "webuiAllowLocalServiceAccess",
            "webui_allow_local_service_access",
            "allowLocalPreviewAccess",
            "allow_local_preview_access",
        ),
    )  # allow WebUI Full Access shell checks against localhost services; legacy allowLocalPreviewAccess still reads
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)
    mcp_presets_auto_enabled: bool = False  # one-time flag: auto-enable no-credential MCP presets only once
    ssrf_whitelist: list[str] = Field(default_factory=list)  # CIDR ranges to exempt from SSRF blocking (e.g. ["100.64.0.0/10"] for Tailscale)


class Config(BaseSettings):
    """Root configuration for MiniUnicorn."""

    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    model_presets: dict[str, ModelPresetConfig] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("modelPresets", "model_presets"),
    )

    def __init__(self, **values: Any) -> None:
        if not type(self).__pydantic_complete__:
            _resolve_tool_config_refs()
        super().__init__(**values)

    @model_validator(mode="after")
    def _validate_model_preset(self) -> "Config":
        if "default" in self.model_presets:
            raise ValueError("model_preset name 'default' is reserved for agents.defaults")
        name = self.agents.defaults.model_preset
        if name and name != "default" and name not in self.model_presets:
            raise ValueError(f"model_preset {name!r} not found in model_presets")
        for fallback in self.agents.defaults.fallback_models:
            if isinstance(fallback, str) and fallback not in self.model_presets:
                raise ValueError(f"fallback_models entry {fallback!r} not found in model_presets")
        return self

    def resolve_default_preset(self) -> ModelPresetConfig:
        """Return the implicit `default` preset from agents.defaults fields."""
        d = self.agents.defaults
        return ModelPresetConfig(
            model=d.model, provider=d.provider, max_tokens=d.max_tokens,
            context_window_tokens=d.context_window_tokens,
            temperature=d.temperature, reasoning_effort=d.reasoning_effort,
        )

    def resolve_preset(self, name: str | None = None) -> ModelPresetConfig:
        """Return effective model params from a named preset or the implicit default."""
        name = self.agents.defaults.model_preset if name is None else name
        if not name or name == "default":
            return self.resolve_default_preset()
        if name not in self.model_presets:
            raise KeyError(f"model_preset {name!r} not found in model_presets")
        return self.model_presets[name]

    @property
    def workspace_path(self) -> Path:
        """Get expanded workspace path."""
        return Path(self.agents.defaults.workspace).expanduser()

    def _match_provider(
        self, model: str | None = None,
        *,
        preset: ModelPresetConfig | None = None,
    ) -> tuple["ProviderConfig | None", str | None]:
        """Match provider config and its registry name. Returns (config, spec_name)."""
        from miniunicorn.providers.registry import PROVIDERS, find_by_name

        resolved = preset or self.resolve_preset()

        def _with_preset_creds(p: "ProviderConfig | None") -> "ProviderConfig | None":
            """若 preset 自带凭证,合成一个 ProviderConfig 覆盖 p 的对应字段。

            用于支持多个独立 custom endpoint:每个 model_preset 携带自己的
            api_key/api_base,运行时覆盖 config.providers.<name> 的单例值。
            """
            if p is None:
                # preset 自带凭证时,即使 providers.<name> 不存在也能构造
                if resolved.api_key or resolved.api_base or resolved.extra_headers or resolved.extra_body:
                    return ProviderConfig(
                        api_key=resolved.api_key,
                        api_base=resolved.api_base,
                        extra_headers=resolved.extra_headers,
                        extra_body=resolved.extra_body,
                    )
                return None
            if not (resolved.api_key or resolved.api_base or resolved.extra_headers or resolved.extra_body):
                return p
            return ProviderConfig(
                api_key=resolved.api_key if resolved.api_key is not None else p.api_key,
                api_base=resolved.api_base if resolved.api_base is not None else p.api_base,
                api_type=p.api_type,
                extra_headers=resolved.extra_headers if resolved.extra_headers is not None else p.extra_headers,
                extra_body=resolved.extra_body if resolved.extra_body is not None else p.extra_body,
            )

        forced = resolved.provider
        if forced != "auto":
            spec = find_by_name(forced)
            if spec:
                p = getattr(self.providers, spec.name, None)
                # preset 自带凭证时,即使 providers.<name> 未配置也允许返回
                if p is None and (resolved.api_key or resolved.api_base):
                    return _with_preset_creds(None), spec.name
                return (_with_preset_creds(p), spec.name) if p else (None, None)
            return None, None

        model_lower = (model or resolved.model).lower()
        model_normalized = model_lower.replace("-", "_")

        def _kw_matches(kw: str) -> bool:
            kw = kw.lower()
            return kw in model_lower or kw.replace("-", "_") in model_normalized

        # 单次循环 + 优先级排序：把原来的 4 个串行 for 合并为一次遍历。
        # 优先级（数字越小越优先，与原 4 个 pass 的先后顺序一一对应）：
        #   0 = 关键词命中且有可用凭证（is_local/is_direct 或 api_key）
        #       —— 对应原 first pass：精确匹配且能立即用
        #   1 = 关键词命中但无凭证
        #       —— 对应原 second pass：延后配置场景，仍优先于无关键词命中
        #   2 = 无关键词命中但有 api_key
        #       —— 对应原 third pass：网关兜底，按注册表顺序取首个有 key 的
        #   3 = 任意已配置 provider
        #       —— 对应原 fourth pass：最终兜底，按注册表顺序取首个
        # 同一优先级内按 PROVIDERS 注册表顺序取第一个（与原串行 for 语义一致），
        # 因此用 ``priority < best[0]`` 严格小于比较，遇到同优先级不替换。
        best: tuple[int, "ProviderConfig", str] | None = None
        for spec in PROVIDERS:
            p = getattr(self.providers, spec.name, None)
            if p is None:
                continue
            kw_match = any(_kw_matches(kw) for kw in spec.keywords)
            has_key = bool(p.api_key)
            is_local_or_direct = spec.is_local or spec.is_direct
            if kw_match and (is_local_or_direct or has_key):
                priority = 0
            elif kw_match:
                priority = 1
            elif has_key:
                priority = 2
            else:
                priority = 3
            if best is None or priority < best[0]:
                best = (priority, p, spec.name)
                if priority == 0:
                    # 已是最优，后续不可能更优；提前退出
                    break

        if best is None:
            return None, None
        return _with_preset_creds(best[1]), best[2]

    def get_provider(
        self,
        model: str | None = None,
        *,
        preset: ModelPresetConfig | None = None,
    ) -> ProviderConfig | None:
        """Get matched provider config (api_key, api_base, extra_headers). Falls back to first available."""
        p, _ = self._match_provider(model, preset=preset)
        return p

    def get_provider_name(
        self,
        model: str | None = None,
        *,
        preset: ModelPresetConfig | None = None,
    ) -> str | None:
        """Get the registry name of the matched provider (e.g. "deepseek", "openrouter")."""
        _, name = self._match_provider(model, preset=preset)
        return name

    def get_api_key(
        self,
        model: str | None = None,
        *,
        preset: ModelPresetConfig | None = None,
    ) -> str | None:
        """Get API key for the given model. Falls back to first available key."""
        p = self.get_provider(model, preset=preset)
        return p.api_key if p else None

    def get_api_base(
        self,
        model: str | None = None,
        *,
        preset: ModelPresetConfig | None = None,
    ) -> str | None:
        """Get API base URL for the given model, falling back to the provider default when present."""
        from miniunicorn.providers.registry import find_by_name

        p, name = self._match_provider(model, preset=preset)
        if p and p.api_base:
            return p.api_base
        if name:
            spec = find_by_name(name)
            if spec and spec.default_api_base:
                return spec.default_api_base
        return None

    model_config = ConfigDict(env_prefix="MINIUNICORN_", env_nested_delimiter="__")


def _resolve_tool_config_refs() -> None:
    """Resolve forward references in ToolsConfig by importing tool config classes.

    Must be called after all modules are loaded (breaks circular imports).
    Re-exports the classes into this module's namespace so existing imports
    like ``from miniunicorn.config.schema import ExecToolConfig`` continue to work.
    """
    import sys

    from miniunicorn.agent.tools.cli_apps import CliAppsToolConfig
    from miniunicorn.agent.tools.deep_research.config import DeepResearchConfig
    from miniunicorn.agent.tools.image_generation.config import ImageGenerationConfig
    from miniunicorn.agent.tools.self import MyToolConfig
    from miniunicorn.agent.tools.shell import ExecToolConfig
    from miniunicorn.agent.tools.web import WebFetchConfig, WebToolsConfig
    from miniunicorn.agent.tools.web_search.config import WebSearchConfig

    # Re-export into this module's namespace
    mod = sys.modules[__name__]
    mod.ExecToolConfig = ExecToolConfig  # type: ignore[attr-defined]
    mod.CliAppsToolConfig = CliAppsToolConfig  # type: ignore[attr-defined]
    mod.WebToolsConfig = WebToolsConfig  # type: ignore[attr-defined]
    mod.WebFetchConfig = WebFetchConfig  # type: ignore[attr-defined]
    mod.MyToolConfig = MyToolConfig  # type: ignore[attr-defined]
    mod.WebSearchConfig = WebSearchConfig  # type: ignore[attr-defined]
    mod.DeepResearchConfig = DeepResearchConfig  # type: ignore[attr-defined]
    mod.ImageGenerationConfig = ImageGenerationConfig  # type: ignore[attr-defined]

    ToolsConfig.model_rebuild()
    Config.model_rebuild()


# Eagerly resolve when the import chain allows it (no circular deps at this
# point).  If it fails (first import triggers a cycle), the rebuild will
# happen lazily when Config/ToolsConfig is first used at runtime.
try:
    _resolve_tool_config_refs()
except ImportError:
    pass
