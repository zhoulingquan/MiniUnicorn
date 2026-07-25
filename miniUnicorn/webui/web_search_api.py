"""WebSearch + WebFetch 领域模块:搜索 provider 配置 + Jina Reader 开关。

从 ``_payload.py`` / ``_updates.py`` 迁入,集中 web_search 相关的常量、
payload builder 和 update handler,避免与 model/network_safety/runtime
等其它设置混在通用文件里。

公共 API(经 ``settings_api.py`` re-export):
- ``web_search_payload``: 构造 web_search + web 区域
- ``update_web_search_settings`` / ``update_web_fetch_settings``
"""

from __future__ import annotations

from typing import Any

from miniUnicorn.config.loader import load_config, save_config

from ._query import QueryParams, _query_first, _query_first_alias
from ._runtime import WebUISettingsError, _mask_secret_hint

# === 常量 ===

# web_search 后端选项,必须与 miniUnicorn/agent/tools/web_search/backends/__init__.py
# 中的 BACKEND_REGISTRY 保持一致。auto 模式会并发调用所有后端,这里仅列出可在
# 单后端模式显式选择的 provider。credential 字段告诉 UI 是否需要 api_key/base_url。
# 精简为以 SearXNG 为主力的三层架构(2026-07-23):
#   searxng(主力,自托管) + tavily(AI 摘要) + bing_cn(国内免 Key 兜底)
_WEB_SEARCH_PROVIDER_OPTIONS: tuple[dict[str, str], ...] = (
    {"name": "auto", "label": "Auto (并发聚合)", "credential": "none"},
    # 主力:自托管元搜索(需配置 base_url,无需 api_key)
    {"name": "searxng", "label": "SearXNG (自托管)", "credential": "base_url"},
    # AI 摘要增强(需 api_key)
    {"name": "tavily", "label": "Tavily (AI Search)", "credential": "api_key"},
    # 国内免 Key 兜底
    {"name": "bing_cn", "label": "Bing (国内)", "credential": "none"},
)
_WEB_SEARCH_PROVIDER_BY_NAME = {
    provider["name"]: provider for provider in _WEB_SEARCH_PROVIDER_OPTIONS
}


# === Payload builder ===


def web_search_payload(config: Any) -> dict[str, Any]:
    """构造 settings payload 中 web_search + web 区域。

    由 ``settings_api.settings_payload`` 调用聚合。
    """
    search_config = config.tools.web_search
    # 空字符串或未配置时回退到 "auto"(并发聚合所有后端)。
    search_provider = (
        search_config.provider
        if search_config.provider in _WEB_SEARCH_PROVIDER_BY_NAME
        else "auto"
    )
    # 当前选中 provider 的凭证:从 backends[name] 读取(bocha 用 api_key,
    # 其余国内后端不需要凭证)。auto 模式下不展示凭证。
    _selected_provider_option = _WEB_SEARCH_PROVIDER_BY_NAME.get(search_provider, {})
    _selected_credential = _selected_provider_option.get("credential", "none")
    if _selected_credential == "api_key":
        _bocha_cfg = search_config.get_backend_config(search_provider)
        search_api_key_hint = _mask_secret_hint(_bocha_cfg.api_key or None)
    else:
        search_api_key_hint = None

    return {
        "web_search": {
            "enable": search_config.enable,
            "provider": search_provider,
            "api_key_hint": search_api_key_hint,
            "max_results": search_config.max_results,
            "timeout": search_config.timeout,
            "proxy": search_config.proxy,
            "backends": {
                name: cfg.model_dump()
                for name, cfg in search_config.backends.items()
            },
            "providers": list(_WEB_SEARCH_PROVIDER_OPTIONS),
        },
        "web": {
            "enable": config.tools.web.enable,
            "proxy": config.tools.web.proxy,
            "user_agent": config.tools.web.user_agent,
            "fetch": {
                "use_jina_reader": config.tools.web.fetch.use_jina_reader,
            },
        },
    }


# === Update handlers ===


def update_web_search_settings(query: QueryParams) -> dict[str, Any]:
    provider_name = (_query_first(query, "provider") or "").strip().lower()
    provider_option = _WEB_SEARCH_PROVIDER_BY_NAME.get(provider_name)
    if provider_option is None:
        raise WebUISettingsError("unknown web search provider")

    config = load_config()
    search_config = config.tools.web_search
    previous_provider = search_config.provider
    changed = False
    restart_required = False

    def set_search_value(attr: str, value: object) -> None:
        nonlocal changed
        if getattr(search_config, attr) != value:
            setattr(search_config, attr, value)
            changed = True

    def set_backend_credential(name: str, *, api_key: str | None = None) -> None:
        """更新 backends[name] 的凭证字段;空值时移除该条目以保持配置干净。"""
        nonlocal changed
        existing = search_config.backends.get(name)
        if api_key is not None:
            new_cfg = existing or search_config.get_backend_config(name)
            if new_cfg.api_key != api_key:
                new_cfg.api_key = api_key
                search_config.backends[name] = new_cfg
                changed = True

    if search_config.provider != provider_name:
        search_config.provider = provider_name
        changed = True

    credential = provider_option["credential"]
    if credential == "none":
        # auto / 免 Key 后端:清空选中 provider 的 backends 凭证
        if provider_name != "auto" and provider_name in search_config.backends:
            search_config.backends.pop(provider_name, None)
            changed = True
    elif credential == "base_url":
        # 当前 _WEB_SEARCH_PROVIDER_OPTIONS 不含 base_url 类型,保留分支以备扩展。
        base_url = _query_first_alias(query, "base_url", "baseUrl")
        base_url = base_url.strip() if base_url is not None else None
        existing_cfg = search_config.backends.get(provider_name)
        if not base_url and previous_provider == provider_name and existing_cfg and existing_cfg.base_url:
            base_url = existing_cfg.base_url
        if not base_url:
            raise WebUISettingsError("base_url is required")
        new_cfg = existing_cfg or search_config.get_backend_config(provider_name)
        if new_cfg.base_url != base_url:
            new_cfg.base_url = base_url
            search_config.backends[provider_name] = new_cfg
            changed = True
    else:
        # api_key 类后端(bocha):从 query 或保留旧值
        api_key = _query_first_alias(query, "api_key", "apiKey")
        api_key = api_key.strip() if api_key is not None else None
        existing_cfg = search_config.backends.get(provider_name)
        if not api_key and previous_provider == provider_name and existing_cfg and existing_cfg.api_key:
            api_key = existing_cfg.api_key
        if not api_key:
            raise WebUISettingsError("api_key is required")
        set_backend_credential(provider_name, api_key=api_key)

    max_results = _query_first_alias(query, "max_results", "maxResults")
    if max_results is not None:
        try:
            parsed = int(max_results)
        except ValueError:
            raise WebUISettingsError("max_results must be an integer") from None
        if parsed < 1 or parsed > 10:
            raise WebUISettingsError("max_results must be between 1 and 10")
        set_search_value("max_results", parsed)

    timeout = _query_first(query, "timeout")
    if timeout is not None:
        try:
            parsed_timeout = int(timeout)
        except ValueError:
            raise WebUISettingsError("timeout must be an integer") from None
        if parsed_timeout < 1 or parsed_timeout > 120:
            raise WebUISettingsError("timeout must be between 1 and 120")
        set_search_value("timeout", parsed_timeout)

    if changed:
        save_config(config)
    from .settings_api import settings_payload
    return settings_payload(requires_restart=restart_required)


def update_web_fetch_settings(query: QueryParams) -> dict[str, Any]:
    """更新 web_fetch 工具配置(独立于 web_search)。

    目前唯一可配置项是 Jina Reader 开关(``use_jina_reader``)。
    """
    use_jina_reader = _query_first_alias(query, "use_jina_reader", "useJinaReader")
    if use_jina_reader is None:
        raise WebUISettingsError("use_jina_reader is required")

    normalized = use_jina_reader.strip().lower()
    if normalized not in {"1", "0", "true", "false", "yes", "no"}:
        raise WebUISettingsError("use_jina_reader must be boolean")

    config = load_config()
    new_value = normalized in {"1", "true", "yes"}
    restart_required = False
    if config.tools.web.fetch.use_jina_reader != new_value:
        config.tools.web.fetch.use_jina_reader = new_value
        save_config(config)
        restart_required = True

    from .settings_api import settings_payload
    return settings_payload(requires_restart=restart_required)
