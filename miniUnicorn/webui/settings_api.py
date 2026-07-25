"""Settings REST helpers for the WebUI HTTP surface.

重构后按业务域拆分为独立模块,本文件作为聚合门面:
- ``_query``: 通用查询工具(QueryParams/_query_first/_parse_bool 等)
- ``_runtime``: WebUISettingsError、runtime surface capabilities、decorate helper
- ``model_settings_api``: 模型设置(provider/preset/context window)
- ``web_search_api``: WebSearch + WebFetch
- ``network_safety_api``: Advanced(network safety)
- ``runtime_settings_api``: Runtime(heartbeat/dream)

``settings_payload`` 在此聚合各域 payload builder 组装完整 payload,
保持 ``from miniUnicorn.webui.settings_api import ...`` 的向后兼容。
"""

from __future__ import annotations

from typing import Any

from miniUnicorn.config.loader import load_config

from ._query import QueryParams, _clip_ws_string, _parse_bool, _query_first, _query_first_alias
from ._runtime import (
    RuntimeSurface,
    WebUISettingsError,
    _mask_secret_hint,
    decorate_settings_payload,
    restart_behavior_by_section,
    runtime_capabilities,
)
from .model_settings_api import (
    create_model_configuration,
    delete_all_providers,
    delete_model_configuration,
    delete_provider_settings,
    list_provider_models,
    login_oauth_provider,
    logout_oauth_provider,
    model_settings_payload,
    update_agent_settings,
    update_model_configuration,
    update_provider_settings,
)
from .network_safety_api import advanced_payload, update_network_safety_settings
from .runtime_settings_api import runtime_payload, update_runtime_settings
from .web_search_api import (
    update_web_fetch_settings,
    update_web_search_settings,
    web_search_payload,
)
from .image_generation_api import (
    image_generation_payload,
    update_image_generation_settings,
)


def settings_payload(
    *,
    requires_restart: bool = False,
    surface: str | None = "browser",
    runtime_capability_overrides: dict[str, Any] | None = None,
    restart_required_sections: list[str] | None = None,
    apply_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """聚合各域 payload builder,组装完整 settings payload。

    各域 api 模块提供独立的 payload 构造函数,此处调用它们合并。
    最后通过 ``decorate_settings_payload`` 附加 runtime surface 元数据。
    """
    config = load_config()
    payload: dict[str, Any] = {}
    # 各域 payload builder 返回的 key 互不重叠,直接 merge。
    payload.update(model_settings_payload(config))
    payload.update(web_search_payload(config))
    payload.update(runtime_payload(config))
    payload.update(advanced_payload(config))
    payload.update(image_generation_payload(config))
    payload["requires_restart"] = requires_restart
    return decorate_settings_payload(
        payload,
        surface=surface,
        runtime_capability_overrides=runtime_capability_overrides,
        restart_required_sections=restart_required_sections,
        apply_state=apply_state,
    )


__all__ = [
    "QueryParams",
    "RuntimeSurface",
    "WebUISettingsError",
    "advanced_payload",
    "create_model_configuration",
    "decorate_settings_payload",
    "delete_all_providers",
    "delete_model_configuration",
    "delete_provider_settings",
    "image_generation_payload",
    "list_provider_models",
    "login_oauth_provider",
    "logout_oauth_provider",
    "model_settings_payload",
    "restart_behavior_by_section",
    "runtime_capabilities",
    "runtime_payload",
    "settings_payload",
    "update_agent_settings",
    "update_image_generation_settings",
    "update_model_configuration",
    "update_network_safety_settings",
    "update_provider_settings",
    "update_runtime_settings",
    "update_web_fetch_settings",
    "update_web_search_settings",
    "web_search_payload",
    "_clip_ws_string",
    "_mask_secret_hint",
    "_parse_bool",
    "_query_first",
    "_query_first_alias",
]
