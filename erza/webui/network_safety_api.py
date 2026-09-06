"""Network safety 领域模块:本地服务访问控制 + 默认访问模式。

从 ``_payload.py`` / ``_updates.py`` 迁入,集中 advanced section 中
network safety 相关的 payload builder 和 update handler。

公共 API(经 ``settings_api.py`` re-export):
- ``advanced_payload``: 构造 advanced 区域(含只读的 sandbox/exec 状态)
- ``update_network_safety_settings``
"""

from __future__ import annotations

from typing import Any

from erza.config.loader import load_config, save_config
from erza.security.workspace_access import workspace_sandbox_status
from erza.webui.workspaces import (
    read_webui_default_access_mode,
    write_webui_default_access_mode,
)

from ._query import QueryParams, _parse_bool, _query_first_alias
from ._runtime import WebUISettingsError

# === Payload builder ===


def advanced_payload(config: Any) -> dict[str, Any]:
    """构造 settings payload 中 advanced 区域。

    包含可编辑字段(webui_allow_local_service_access / webui_default_access_mode)
    和大量只读状态字段(workspace_sandbox / exec / ssrf / mcp_server_count)。
    由 ``settings_api.settings_payload`` 调用聚合。
    """
    exec_config = config.tools.exec
    sandbox_status = workspace_sandbox_status(
        restrict_to_workspace=config.tools.restrict_to_workspace,
        workspace=config.workspace_path,
    )
    return {
        "advanced": {
            "restrict_to_workspace": config.tools.restrict_to_workspace,
            "workspace_sandbox": sandbox_status.as_dict(),
            "webui_allow_local_service_access": config.tools.webui_allow_local_service_access,
            "allow_local_preview_access": config.tools.webui_allow_local_service_access,
            "webui_default_access_mode": read_webui_default_access_mode(),
            "private_service_protection_enabled": True,
            "ssrf_whitelist_count": len(config.tools.ssrf_whitelist),
            "mcp_server_count": len(config.tools.mcp_servers),
            "exec_enabled": exec_config.enable,
            "exec_sandbox": exec_config.sandbox or None,
            "exec_path_append_set": bool(exec_config.path_append),
        }
    }


# === Update handlers ===


def update_network_safety_settings(query: QueryParams) -> dict[str, Any]:
    raw_allow = _query_first_alias(
        query, "webui_allow_local_service_access", "webuiAllowLocalServiceAccess"
    ) or _query_first_alias(query, "allow_local_preview_access", "allowLocalPreviewAccess")
    raw_default_access_mode = _query_first_alias(
        query, "webui_default_access_mode", "webuiDefaultAccessMode"
    )
    if raw_allow is None and raw_default_access_mode is None:
        raise WebUISettingsError(
            "webui_allow_local_service_access or webui_default_access_mode is required"
        )

    config = load_config()
    changed = False
    if raw_allow is not None:
        webui_allow_local_service_access = _parse_bool(
            raw_allow, "webui_allow_local_service_access"
        )
        if config.tools.webui_allow_local_service_access != webui_allow_local_service_access:
            config.tools.webui_allow_local_service_access = webui_allow_local_service_access
            changed = True

    if changed:
        save_config(config)
    if raw_default_access_mode is not None:
        default_access_mode = raw_default_access_mode.strip().lower()
        if default_access_mode == "restricted":
            default_access_mode = "default"
        if default_access_mode not in {"default", "full"}:
            raise WebUISettingsError("webui_default_access_mode must be default or full")
        try:
            write_webui_default_access_mode(default_access_mode)
        except ValueError as exc:
            raise WebUISettingsError(str(exc)) from exc
    from .settings_api import settings_payload

    return settings_payload(requires_restart=changed)
