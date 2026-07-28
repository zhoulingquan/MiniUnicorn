"""模型设置领域模块:provider 配置 + model preset CRUD + 上下文窗口学习。

从 ``_helpers.py`` / ``_payload.py`` / ``_updates.py`` 迁入,集中模型设置
相关的 helper、payload builder 和 update handler,避免与 web_search /
network_safety / runtime 等其它设置混在通用文件里。

公共 API(经 ``settings_api.py`` re-export):
- ``model_settings_payload``: 构造 agent + model_presets + providers 区域
- ``update_agent_settings``: 切换 model/preset/provider/context_window/planner
- ``create_model_configuration`` / ``update_model_configuration`` / ``delete_model_configuration``
- ``update_provider_settings`` / ``delete_provider_settings`` / ``delete_all_providers``
- ``login_oauth_provider`` / ``logout_oauth_provider`` (stub)
- ``list_provider_models`` (async, 拉取 provider 模型列表)
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from miniunicorn.config.loader import load_config, save_config
from miniunicorn.config.schema import ModelPresetConfig
from miniunicorn.providers.registry import PROVIDERS, find_by_name

from ._query import QueryParams, _query_first, _query_first_alias
from ._runtime import WebUISettingsError, _mask_secret_hint

# === 专属常量 ===

_MODEL_CONFIGURATION_SLUG_RE = re.compile(r"[^a-z0-9_-]+")

# context_window_tokens 合理取值范围。
# 接受任意正整数(1024 ~ 10_000_000),允许 HF/ModelScope 自动发现的值
# (如 128000、1000000)回写配置。不再使用 {65536, 262144} 白名单。
_MIN_CONTEXT_WINDOW = 1024
_MAX_CONTEXT_WINDOW = 10_000_000


# === 专属 helper ===


def _provider_requires_api_key(spec: Any) -> bool:
    if spec.is_local or spec.is_direct:
        return False
    return True


def _provider_configured_for_settings(spec: Any, provider_config: Any) -> bool:
    if _provider_requires_api_key(spec):
        return bool(provider_config.api_key)
    return bool(
        provider_config.api_key
        or provider_config.api_base
        or getattr(provider_config, "region", None)
        or getattr(provider_config, "profile", None)
    )


def _validate_configured_provider(config: Any, provider: str) -> None:
    if provider == "auto":
        return
    spec = find_by_name(provider)
    if spec is None:
        raise WebUISettingsError("unknown provider")
    provider_config = getattr(config.providers, provider, None)
    if provider_config is None or not _provider_configured_for_settings(spec, provider_config):
        # custom provider 允许通过 per-preset 凭证绕过单例校验:
        # 调用方(create/update_model_configuration)会在传入 api_key+api_base
        # 后再调用本函数,这里只做 provider 注册表校验。
        if provider != "custom":
            raise WebUISettingsError("provider is not configured")


def _parse_context_window_tokens(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        raise WebUISettingsError("context_window_tokens must be an integer") from None
    if parsed < _MIN_CONTEXT_WINDOW or parsed > _MAX_CONTEXT_WINDOW:
        raise WebUISettingsError(
            f"context_window_tokens must be between {_MIN_CONTEXT_WINDOW} and {_MAX_CONTEXT_WINDOW} (got {parsed})"
        )
    return parsed


def _resolve_context_window_for_settings(model: str, configured: int | None) -> dict[str, Any]:
    """Return the effective context window size and its resolution status.

    Resolution order (read-only, does NOT trigger HF queries):
    1. Explicit user-configured value (``context_window_tokens`` in config).
    2. Permanent learning table entry (already learned by a prior save).

    Use :func:`_resolve_context_window_for_save` to actively query HF when a
    model is saved/selected.
    """
    if isinstance(configured, int) and configured > 0:
        return {"limit": configured, "status": "configured", "error": None}
    if not model:
        return {"limit": 65_536, "status": "default", "error": None}
    try:
        from miniunicorn.cli.models import _load_learned_entry, _normalize_model_name

        key = _normalize_model_name(model)
        entry = _load_learned_entry(key) if key else None
    except Exception:
        entry = None

    if entry is not None and isinstance(entry.get("limit"), int):
        return {
            "limit": entry["limit"],
            "status": "learned",
            "error": None,
        }
    # Not yet learned — surface the last failure reason if any.
    error = entry.get("error") if isinstance(entry, dict) else None
    return {
        "limit": 65_536,
        "status": "unknown",
        "error": error or "尚未查询,保存模型后将自动从 HuggingFace 查询",
    }


def _resolve_context_window_for_save(
    model: str,
    explicit_value: int | None,
) -> dict[str, Any]:
    """Resolve context window for a model save operation (synchronous).

    Replaces the former background-thread + poll pattern. Called by
    create/update model handlers when a model is saved or changed.

    1. A valid explicit value bypasses discovery (zero network requests).
    2. Otherwise, runs Hugging Face → ModelScope discovery synchronously.
    3. On success, returns the concrete integer for the caller to persist.
    4. On failure, returns ``limit=None`` so the caller can apply
       incomplete-model failure semantics.

    Returns ``{"limit": int | None, "status": str, "error": str | None}``.
    """
    # 1. Explicit value bypasses discovery.
    if isinstance(explicit_value, int) and explicit_value > 0:
        return {"limit": explicit_value, "status": "configured", "error": None}

    if not model:
        return {"limit": 65_536, "status": "default", "error": None}

    # 2. Run discovery (synchronous, no config lock held).
    try:
        from miniunicorn.cli.models import learn_model_context_limit

        result = learn_model_context_limit(model)
    except Exception as exc:
        return {"limit": None, "status": "failed", "error": str(exc)}

    if result.get("status") == "ok" and isinstance(result.get("limit"), int):
        return {"limit": result["limit"], "status": "learned", "error": None}

    # 3. Persist the failure reason so the settings page can show it.
    error = result.get("error") or "未知错误"
    try:
        from miniunicorn.cli.models import _save_learned_failure

        _save_learned_failure(model, error)
    except Exception:
        pass
    return {"limit": None, "status": "failed", "error": error}


def _model_configuration_slug(label: str) -> str:
    normalized = _MODEL_CONFIGURATION_SLUG_RE.sub("-", label.strip().lower())
    normalized = normalized.strip("-_")
    if not normalized:
        raise WebUISettingsError("configuration name is required")
    if normalized == "default":
        raise WebUISettingsError("configuration name is reserved")
    if len(normalized) > 48:
        normalized = normalized[:48].rstrip("-_")
    return normalized


def _extract_label_from_base_url(base_url: str | None) -> str | None:
    """从 api_base URL 提取 provider 显示名。

    规则:
    1. 解析 host,去掉通用前缀(www/api/apihub/api-gateway 等)
    2. 去掉顶级域名后缀(.com/.cn/.org/.ai/.io/.net 等)
    3. 返回主域名(支持多级子域,如 apihub.agnes-ai.com → agnes-ai)
    """
    if not base_url:
        return None
    try:
        parsed = urlparse(base_url)
        host = parsed.hostname or ""
    except Exception:
        return None
    if not host:
        return None
    parts = host.split(".")
    while parts and parts[0].lower() in {"www", "api", "apihub", "api-gateway", "gateway"}:
        parts = parts[1:]
    if not parts:
        return None
    if len(parts) >= 2:
        parts = parts[:-1]
    label = ".".join(parts) if parts else host
    return label[:24].capitalize() if label else None


# === Payload builder ===


def model_settings_payload(config: Any) -> dict[str, Any]:
    """构造 settings payload 中模型相关的区域:agent + model_presets + providers。

    由 ``settings_api.settings_payload`` 调用聚合,不直接附加 runtime 元数据。
    """
    defaults = config.agents.defaults
    active_preset_name = defaults.model_preset or "default"
    try:
        effective_preset = config.resolve_preset()
    except Exception:
        effective_preset = config.resolve_default_preset()
        active_preset_name = "default"

    provider_name = (
        config.get_provider_name(effective_preset.model, preset=effective_preset)
        or effective_preset.provider
    )
    provider = config.get_provider(effective_preset.model, preset=effective_preset)
    selected_provider = provider_name
    if effective_preset.provider != "auto":
        spec = find_by_name(effective_preset.provider)
        selected_provider = spec.name if spec else provider_name

    providers = []
    _presets_by_provider: dict[str, list[dict[str, Any]]] = {}
    active_preset_name = config.agents.defaults.model_preset
    for preset_name, preset_cfg in config.model_presets.items():
        provider_key = preset_cfg.provider or "auto"
        _presets_by_provider.setdefault(provider_key, []).append(
            {
                "name": preset_name,
                "label": preset_cfg.label or preset_name,
                "model": preset_cfg.model,
                "active": preset_name == active_preset_name,
            }
        )
    for spec in PROVIDERS:
        provider_config = getattr(config.providers, spec.name, None)
        if provider_config is None:
            continue
        provider_presets = _presets_by_provider.get(spec.name, [])
        configured = _provider_configured_for_settings(spec, provider_config)
        display_api_key = provider_config.api_key
        display_api_base = provider_config.api_base
        if spec.name == "custom" and provider_presets:
            custom_preset_cfgs = [
                (name, cfg)
                for name, cfg in config.model_presets.items()
                if cfg.provider == "custom"
            ]
            represet = next(
                (cfg for _, cfg in custom_preset_cfgs if cfg.api_key),
                None,
            )
            if represet is None and custom_preset_cfgs:
                represet = custom_preset_cfgs[0][1]
            if represet is not None:
                display_api_key = represet.api_key
                display_api_base = represet.api_base
        if spec.name == "custom" and provider_presets:
            display_label = _extract_label_from_base_url(display_api_base) or spec.label
        else:
            display_label = spec.label
        is_custom_singleton = spec.name == "custom"
        row = {
            "name": spec.name,
            "label": display_label,
            "configured": configured,
            "auth_type": "api_key",
            "api_key_required": _provider_requires_api_key(spec),
            "api_key_hint": _mask_secret_hint(display_api_key),
            "api_base": display_api_base,
            "default_api_base": spec.default_api_base or None,
            "preset_count": 0 if is_custom_singleton else len(provider_presets),
            "presets": [] if is_custom_singleton else provider_presets,
        }
        providers.append(row)

    # 为每个 custom preset 生成独立虚拟 provider row
    for preset_name, preset_cfg in config.model_presets.items():
        if preset_cfg.provider != "custom":
            continue
        virtual_row = {
            "name": f"custom__{preset_name}",
            "label": _extract_label_from_base_url(preset_cfg.api_base)
            or preset_cfg.label
            or preset_name,
            "configured": True,
            "auth_type": "api_key",
            "api_key_required": True,
            "api_key_hint": _mask_secret_hint(preset_cfg.api_key),
            "api_base": preset_cfg.api_base,
            "default_api_base": None,
            "is_custom_preset": True,
            "preset_name": preset_name,
            "provider": "custom",
            "model": preset_cfg.model,
            "preset_count": 0,
            "presets": [],
        }
        providers.append(virtual_row)

    def _ctx_fields(model: str, configured: int | None) -> dict[str, Any]:
        info = _resolve_context_window_for_settings(model, configured)
        return {
            "resolved_context_window_tokens": info["limit"],
            "resolved_context_window_status": info["status"],
            "resolved_context_window_error": info["error"],
        }

    model_presets = [
        {
            "name": "default",
            "label": "Default",
            "active": active_preset_name == "default",
            "is_default": True,
            "model": defaults.model,
            "provider": defaults.provider,
            "max_tokens": defaults.max_tokens,
            "context_window_tokens": defaults.context_window_tokens,
            **_ctx_fields(defaults.model, defaults.context_window_tokens),
            "temperature": defaults.temperature,
            "reasoning_effort": defaults.reasoning_effort,
        }
    ]
    for name, preset in config.model_presets.items():
        model_presets.append(
            {
                "name": name,
                "label": preset.label or name,
                "active": active_preset_name == name,
                "is_default": False,
                "model": preset.model,
                "provider": preset.provider,
                "max_tokens": preset.max_tokens,
                "context_window_tokens": preset.context_window_tokens,
                **_ctx_fields(preset.model, preset.context_window_tokens),
                "temperature": preset.temperature,
                "reasoning_effort": preset.reasoning_effort,
            }
        )

    return {
        "agent": {
            "model": effective_preset.model,
            "provider": selected_provider,
            "resolved_provider": provider_name,
            "has_api_key": bool(provider and provider.api_key),
            "model_preset": active_preset_name,
            "max_tokens": effective_preset.max_tokens,
            "context_window_tokens": effective_preset.context_window_tokens,
            **_ctx_fields(effective_preset.model, effective_preset.context_window_tokens),
            "temperature": effective_preset.temperature,
            "reasoning_effort": effective_preset.reasoning_effort,
            "tool_hint_max_length": defaults.tool_hint_max_length,
            "use_planner": defaults.use_planner,
            "planner_model": defaults.planner_model,
            "planner_max_replans": defaults.planner_max_replans,
        },
        "model_presets": model_presets,
        "providers": providers,
    }


# === Update handlers ===


def update_agent_settings(query: QueryParams) -> dict[str, Any]:
    """切换 model/preset/provider/context_window/tool_hint/planner 配置。

    保留单一入口(前端 ``updateSettings`` 一次可传多个字段),
    内部按字段分段处理。planner 虽语义独立,但修改的是 agents.defaults,
    与 model 同源,保留在此避免前端多次调用。
    """
    config = load_config()
    defaults = config.agents.defaults
    changed = False
    restart_required = False

    if "model_preset" in query or "modelPreset" in query:
        preset = (_query_first_alias(query, "model_preset", "modelPreset") or "").strip()
        preset_value = None if not preset or preset == "default" else preset
        if preset_value is not None and preset_value not in config.model_presets:
            raise WebUISettingsError("unknown model preset")
        if defaults.model_preset != preset_value:
            defaults.model_preset = preset_value
            changed = True

    model = _query_first(query, "model")
    model_changed = False
    old_model = defaults.model
    if model is not None:
        model = model.strip()
        if not model:
            raise WebUISettingsError("model is required")
        if defaults.model != model:
            defaults.model = model
            changed = True
            model_changed = True

    provider = _query_first(query, "provider")
    if provider is not None:
        provider = provider.strip()
        if not provider:
            raise WebUISettingsError("provider is required")
        _validate_configured_provider(config, provider)
        if defaults.provider != provider:
            defaults.provider = provider
            changed = True

    explicit_cw = _parse_context_window_tokens(
        _query_first_alias(query, "context_window_tokens", "contextWindowTokens")
    )
    if explicit_cw is not None and defaults.context_window_tokens != explicit_cw:
        defaults.context_window_tokens = explicit_cw
        changed = True

    tool_hint_max_length = _query_first_alias(
        query,
        "tool_hint_max_length",
        "toolHintMaxLength",
    )
    if tool_hint_max_length is not None:
        try:
            parsed = int(tool_hint_max_length)
        except ValueError:
            raise WebUISettingsError("tool_hint_max_length must be an integer") from None
        if parsed < 20 or parsed > 500:
            raise WebUISettingsError("tool_hint_max_length must be between 20 and 500")
        if defaults.tool_hint_max_length != parsed:
            defaults.tool_hint_max_length = parsed
            changed = True
            restart_required = True

    # Plan & Execute 双模型配置:开关 + 规划模型选择。
    raw_use_planner = _query_first_alias(query, "use_planner", "usePlanner")
    if raw_use_planner is not None:
        from ._query import _parse_bool

        use_planner = _parse_bool(raw_use_planner, "use_planner")
        if defaults.use_planner != use_planner:
            defaults.use_planner = use_planner
            changed = True
            restart_required = True

    raw_planner_model = _query_first_alias(query, "planner_model", "plannerModel")
    if raw_planner_model is not None:
        preset_name = raw_planner_model.strip()
        preset_value = None if not preset_name or preset_name == "default" else preset_name
        if preset_value is not None and preset_value not in config.model_presets:
            raise WebUISettingsError("unknown planner model preset")
        if defaults.planner_model != preset_value:
            defaults.planner_model = preset_value
            changed = True
            restart_required = True

    # Context window resolution for model change (synchronous, replaces
    # the former background-thread + poll pattern). A valid explicit value
    # bypasses discovery. On failure, the prior config is preserved.
    if model_changed and explicit_cw is None:
        resolution = _resolve_context_window_for_save(defaults.model, None)
        if resolution["status"] == "failed":
            raise WebUISettingsError(
                f"无法确定模型 {defaults.model!r} 的上下文窗口: {resolution['error']}"
            )
        if resolution.get("limit") is not None:
            defaults.context_window_tokens = resolution["limit"]
            changed = True
        # Concurrency check: reload and verify target hasn't changed.
        reloaded = load_config()
        if reloaded.agents.defaults.model != old_model:
            raise WebUISettingsError("另一个保存操作已更改模型,请重试")

    if changed:
        save_config(config)
    from .settings_api import settings_payload

    return settings_payload(requires_restart=restart_required)


def create_model_configuration(query: QueryParams) -> dict[str, Any]:
    label = (_query_first_alias(query, "label", "displayName") or "").strip()
    raw_name = (_query_first(query, "name") or label).strip()
    model = (_query_first(query, "model") or "").strip()
    provider = (_query_first(query, "provider") or "").strip()

    if not label:
        label = raw_name
    if not model:
        raise WebUISettingsError("model is required")
    if not provider:
        raise WebUISettingsError("provider is required")

    name = _model_configuration_slug(raw_name or label)
    config = load_config()
    if name in config.model_presets:
        raise WebUISettingsError("configuration already exists", status=409)

    api_key = _query_first_alias(query, "api_key", "apiKey")
    api_key = (api_key or "").strip() or None
    api_base = _query_first_alias(query, "api_base", "apiBase")
    api_base = (api_base or "").strip() or None
    if provider == "custom":
        if not (api_key and api_base):
            raise WebUISettingsError("custom provider requires api_key and api_base")
    else:
        spec = find_by_name(provider)
        if spec is None:
            raise WebUISettingsError("unknown provider")
        provider_config = getattr(config.providers, spec.name, None)
        if provider_config is None:
            raise WebUISettingsError("unknown provider")
        if api_key and api_base:
            if not _provider_configured_for_settings(spec, provider_config):
                if provider_config.api_key != api_key:
                    provider_config.api_key = api_key
                if provider_config.api_base != api_base:
                    provider_config.api_base = api_base
        _validate_configured_provider(config, provider)
        api_key = None
        api_base = None

    base = config.resolve_default_preset()
    explicit_cw = _parse_context_window_tokens(
        _query_first_alias(query, "context_window_tokens", "contextWindowTokens")
    )

    # Context window resolution (synchronous). A valid explicit value bypasses
    # discovery. On failure, the model is saved as incomplete (inactive).
    if explicit_cw is not None:
        resolved_cw: int | None = explicit_cw
        incomplete = False
    else:
        resolution = _resolve_context_window_for_save(model, None)
        if resolution["status"] == "failed":
            resolved_cw = None
            incomplete = True
        else:
            resolved_cw = resolution.get("limit")
            incomplete = False

    config.model_presets[name] = ModelPresetConfig(
        label=label,
        model=model,
        provider=provider,
        max_tokens=base.max_tokens,
        context_window_tokens=resolved_cw
        if resolved_cw is not None
        else base.context_window_tokens,
        temperature=base.temperature,
        reasoning_effort=base.reasoning_effort,
        api_key=api_key,
        api_base=api_base,
    )
    # Only activate the new preset when it is not incomplete.
    if not incomplete:
        config.agents.defaults.model_preset = name
    save_config(config)
    from .settings_api import settings_payload

    return settings_payload()


def update_model_configuration(query: QueryParams) -> dict[str, Any]:
    name = (_query_first(query, "name") or "").strip()
    if not name or name == "default":
        raise WebUISettingsError("model configuration is required")

    config = load_config()
    preset = config.model_presets.get(name)
    if preset is None:
        raise WebUISettingsError("unknown model configuration")

    changed = False
    label = _query_first_alias(query, "label", "displayName")
    if label is not None:
        label = label.strip()
        if not label:
            raise WebUISettingsError("label is required")
        if preset.label != label:
            preset.label = label
            changed = True

    model = _query_first(query, "model")
    model_changed = False
    old_model = preset.model
    if model is not None:
        model = model.strip()
        if not model:
            raise WebUISettingsError("model is required")
        if preset.model != model:
            preset.model = model
            changed = True
            model_changed = True

    provider = _query_first(query, "provider")
    if provider is not None:
        provider = provider.strip()
        if not provider:
            raise WebUISettingsError("provider is required")
        _validate_configured_provider(config, provider)
        if preset.provider != provider:
            preset.provider = provider
            changed = True

    explicit_cw = _parse_context_window_tokens(
        _query_first_alias(query, "context_window_tokens", "contextWindowTokens")
    )
    if explicit_cw is not None and preset.context_window_tokens != explicit_cw:
        preset.context_window_tokens = explicit_cw
        changed = True

    if "api_key" in query or "apiKey" in query:
        api_key = _query_first_alias(query, "api_key", "apiKey")
        api_key = (api_key or "").strip() or None
        if preset.api_key != api_key:
            preset.api_key = api_key
            changed = True

    if "api_base" in query or "apiBase" in query:
        api_base = _query_first_alias(query, "api_base", "apiBase")
        api_base = (api_base or "").strip() or None
        if preset.api_base != api_base:
            preset.api_base = api_base
            changed = True

    if config.agents.defaults.model_preset != name:
        config.agents.defaults.model_preset = name
        changed = True

    # Context window resolution for model change (synchronous). On failure,
    # the prior config is preserved.
    if model_changed and explicit_cw is None:
        resolution = _resolve_context_window_for_save(preset.model, None)
        if resolution["status"] == "failed":
            raise WebUISettingsError(
                f"无法确定模型 {preset.model!r} 的上下文窗口: {resolution['error']}"
            )
        if resolution.get("limit") is not None:
            preset.context_window_tokens = resolution["limit"]
            changed = True
        # Concurrency check: reload and verify target hasn't changed.
        reloaded = load_config()
        reloaded_preset = reloaded.model_presets.get(name)
        if reloaded_preset is None or reloaded_preset.model != old_model:
            raise WebUISettingsError("另一个保存操作已更改该模型配置,请重试")

    if changed:
        save_config(config)
    from .settings_api import settings_payload

    return settings_payload()


def update_provider_settings(query: QueryParams) -> dict[str, Any]:
    """Atomically update provider credentials and optional active model.

    Accepts the historical credential fields (``api_key``/``api_base``) plus an
    optional ``model``. When ``model`` is supplied, the active provider/model on
    ``agents.defaults`` is updated in the same in-memory config object and
    persisted with a single ``save_config()`` call, so a validation failure
    writes nothing and the frontend no longer needs two sequential requests.
    Context-window auto-learning is preserved for a changed model.
    """
    provider_name = (_query_first(query, "provider") or "").strip()
    if not provider_name:
        raise WebUISettingsError("provider is required")
    spec = find_by_name(provider_name)
    if spec is None:
        raise WebUISettingsError("unknown provider")

    config = load_config()
    provider_config = getattr(config.providers, spec.name, None)
    if provider_config is None:
        raise WebUISettingsError("unknown provider")

    changed = False
    if "api_key" in query or "apiKey" in query:
        api_key = _query_first_alias(query, "api_key", "apiKey")
        api_key = (api_key or "").strip() or None
        if provider_config.api_key != api_key:
            provider_config.api_key = api_key
            changed = True

    if "api_base" in query or "apiBase" in query:
        api_base = _query_first_alias(query, "api_base", "apiBase")
        api_base = (api_base or "").strip() or None
        if provider_config.api_base != api_base:
            provider_config.api_base = api_base
            changed = True

    # Optional model selection: collapse the previous two-step flow
    # (updateProviderSettings → updateSettings) into one atomic save. The
    # provider was just configured on the in-memory config object above, so
    # ``_validate_configured_provider`` now succeeds without a separate save.
    model_changed = False
    old_model = config.agents.defaults.model
    explicit_cw = _parse_context_window_tokens(
        _query_first_alias(query, "context_window_tokens", "contextWindowTokens")
    )
    model = _query_first(query, "model")
    if model is not None:
        model = model.strip()
        if not model:
            raise WebUISettingsError("model is required")
        _validate_configured_provider(config, provider_name)
        defaults = config.agents.defaults
        if defaults.provider != provider_name:
            defaults.provider = provider_name
            changed = True
        if defaults.model != model:
            defaults.model = model
            changed = True
            model_changed = True
        if explicit_cw is not None:
            if defaults.context_window_tokens != explicit_cw:
                defaults.context_window_tokens = explicit_cw
                changed = True

    # Context window resolution for model change (synchronous). On failure,
    # the prior config is preserved.
    if model_changed and explicit_cw is None:
        resolution = _resolve_context_window_for_save(config.agents.defaults.model, None)
        if resolution["status"] == "failed":
            raise WebUISettingsError(
                f"无法确定模型 {config.agents.defaults.model!r} 的上下文窗口: {resolution['error']}"
            )
        if resolution.get("limit") is not None:
            config.agents.defaults.context_window_tokens = resolution["limit"]
            changed = True
        # Concurrency check: reload and verify target hasn't changed.
        reloaded = load_config()
        if reloaded.agents.defaults.model != old_model:
            raise WebUISettingsError("另一个保存操作已更改模型,请重试")

    if changed:
        save_config(config)
    from .settings_api import settings_payload

    return settings_payload(requires_restart=False)


def login_oauth_provider(query: QueryParams) -> dict[str, Any]:
    raise WebUISettingsError("No OAuth providers available in this build")


def logout_oauth_provider(query: QueryParams) -> dict[str, Any]:
    raise WebUISettingsError("No OAuth providers available in this build")


async def list_provider_models(query: QueryParams) -> dict[str, Any]:
    """Fetch available models from a provider's /v1/models endpoint."""
    provider_name = (_query_first(query, "provider") or "").strip()
    if not provider_name:
        raise WebUISettingsError("provider is required")

    spec = find_by_name(provider_name)
    if spec is None:
        raise WebUISettingsError("unknown provider")

    config = load_config()
    provider_config = getattr(config.providers, spec.name, None)
    if provider_config is None:
        raise WebUISettingsError("unknown provider")

    api_key = _query_first_alias(query, "api_key", "apiKey") or (
        provider_config.api_key if provider_config else None
    )
    api_base = (
        _query_first_alias(query, "api_base", "apiBase")
        or provider_config.api_base
        or spec.default_api_base
    )
    if not api_base:
        raise WebUISettingsError("api_base is required")

    try:
        from openai import AsyncOpenAI

        if not api_key and not (spec.is_local or spec.is_direct):
            raise WebUISettingsError("api_key is required to fetch models")
        client = AsyncOpenAI(api_key=api_key or "unused", base_url=api_base)
        models = await client.models.list()
        model_ids = sorted(
            [m.id for m in models.data if m.id],
            key=lambda x: x.lower(),
        )
        await client.close()
        return {"provider": provider_name, "models": model_ids}
    except Exception as exc:
        raise WebUISettingsError(f"Failed to fetch models: {exc}") from exc


def delete_model_configuration(query: QueryParams) -> dict[str, Any]:
    """删除一个 model_preset 配置。"""
    name = (_query_first(query, "name") or "").strip()
    if not name or name == "default":
        raise WebUISettingsError("model configuration is required")

    config = load_config()
    if name not in config.model_presets:
        raise WebUISettingsError("unknown model configuration")

    del config.model_presets[name]

    if config.agents.defaults.model_preset == name:
        config.agents.defaults.model_preset = "default"
        default_preset = config.model_presets.get("default")
        if default_preset is not None:
            config.agents.defaults.model = default_preset.model
            config.agents.defaults.provider = default_preset.provider
        else:
            config.agents.defaults.model = ""
            config.agents.defaults.provider = "auto"

    save_config(config)
    from .settings_api import settings_payload

    return settings_payload()


def delete_provider_settings(query: QueryParams) -> dict[str, Any]:
    """清除一个 provider 的配置并将其移回未配置区域。"""
    provider_name = (_query_first(query, "provider") or "").strip()
    if not provider_name:
        raise WebUISettingsError("provider is required")
    spec = find_by_name(provider_name)
    if spec is None:
        raise WebUISettingsError("unknown provider")

    config = load_config()
    provider_config = getattr(config.providers, spec.name, None)
    if provider_config is None:
        raise WebUISettingsError("unknown provider")

    changed = False

    if provider_config.api_key or provider_config.api_base:
        provider_config.api_key = None
        provider_config.api_base = None
        changed = True

    preset_names_to_remove = [
        name
        for name, preset in config.model_presets.items()
        if name != "default" and preset.provider == spec.name
    ]
    for name in preset_names_to_remove:
        del config.model_presets[name]
        changed = True

    active_preset = config.agents.defaults.model_preset
    if active_preset and active_preset in preset_names_to_remove:
        config.agents.defaults.model_preset = "default"
        default_preset = config.model_presets.get("default")
        if default_preset is not None:
            config.agents.defaults.model = default_preset.model
            config.agents.defaults.provider = default_preset.provider
        else:
            config.agents.defaults.model = ""
            config.agents.defaults.provider = "auto"
        changed = True

    if changed:
        save_config(config)
    from .settings_api import settings_payload

    return settings_payload(requires_restart=False)


def delete_all_providers(_query: QueryParams) -> dict[str, Any]:
    """一键清除所有 provider 配置,恢复初始状态。"""
    config = load_config()
    changed = False

    for spec in PROVIDERS:
        provider_config = getattr(config.providers, spec.name, None)
        if provider_config is not None and (provider_config.api_key or provider_config.api_base):
            provider_config.api_key = None
            provider_config.api_base = None
            changed = True

    preset_names_to_remove = [name for name in config.model_presets if name != "default"]
    for name in preset_names_to_remove:
        del config.model_presets[name]
        changed = True

    if config.agents.defaults.model_preset and config.agents.defaults.model_preset != "default":
        config.agents.defaults.model_preset = "default"
        config.agents.defaults.model = ""
        config.agents.defaults.provider = "auto"
        changed = True

    if changed:
        save_config(config)
    from .settings_api import settings_payload

    return settings_payload(requires_restart=False)
