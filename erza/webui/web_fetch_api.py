"""WebFetch 领域模块:web_fetch 工具配置(Jina Reader 开关)。

web_fetch 配置独立成域模块:
- ``web_fetch_payload``: 构造 settings payload 中 ``web`` 区域
- ``update_web_fetch_settings``: 处理 web-fetch 配置更新请求

公共 API(经 ``settings_api.py`` re-export)。
"""

from __future__ import annotations

from typing import Any

from erza.config.loader import load_config, save_config

from ._query import QueryParams, _query_first_alias
from ._runtime import WebUISettingsError


# === Payload builder ===


def web_fetch_payload(config: Any) -> dict[str, Any]:
    """构造 settings payload 中 ``web`` 区域。

    由 ``settings_api.settings_payload`` 调用聚合。
    """
    return {
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


def update_web_fetch_settings(query: QueryParams) -> dict[str, Any]:
    """更新 web_fetch 工具配置。

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