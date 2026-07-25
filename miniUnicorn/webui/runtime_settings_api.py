"""Runtime settings 领域模块:heartbeat 间隔 + dream 间隔配置。

从 ``_payload.py`` / ``_updates.py`` 迁入,集中 runtime section 相关的
payload builder 和 update handler。

公共 API(经 ``settings_api.py`` re-export):
- ``runtime_payload``: 构造 runtime 区域(config_path/workspace/heartbeat/dream)
- ``update_runtime_settings``
"""

from __future__ import annotations

from typing import Any

from miniUnicorn.config.loader import get_config_path, load_config, save_config

from ._query import QueryParams, _query_first_alias
from ._runtime import WebUISettingsError


# === Payload builder ===


def runtime_payload(config: Any) -> dict[str, Any]:
    """构造 settings payload 中 runtime 区域。

    包含 config_path / workspace_path / gateway 信息 / heartbeat / dream /
    unified_session。由 ``settings_api.settings_payload`` 调用聚合。
    """
    defaults = config.agents.defaults
    websocket_channel = getattr(config.channels, "websocket", None)
    if isinstance(websocket_channel, dict):
        ws_port = websocket_channel.get("port", 8765)
    elif websocket_channel is not None:
        ws_port = getattr(websocket_channel, "port", 8765)
    else:
        ws_port = 8765

    return {
        "runtime": {
            "config_path": str(get_config_path().expanduser()),
            "workspace_path": str(config.workspace_path),
            "gateway_host": config.gateway.host,
            "gateway_port": ws_port,
            "heartbeat": {
                "enabled": config.gateway.heartbeat.enabled,
                "interval_s": config.gateway.heartbeat.interval_s,
                "keep_recent_messages": config.gateway.heartbeat.keep_recent_messages,
            },
            "dream": {
                "schedule": defaults.dream.describe_schedule(),
                "max_batch_size": defaults.dream.max_batch_size,
                "max_iterations": defaults.dream.max_iterations,
                "annotate_line_ages": defaults.dream.annotate_line_ages,
            },
            "unified_session": defaults.unified_session,
        }
    }


# === Update handlers ===


def update_runtime_settings(query: QueryParams) -> dict[str, Any]:
    """Update heartbeat interval and/or dream interval from WebUI query params."""
    raw_heartbeat_interval = _query_first_alias(
        query, "heartbeat_interval_s", "heartbeatIntervalS"
    )
    raw_dream_interval = _query_first_alias(
        query, "dream_interval_h", "dreamIntervalH"
    )
    if raw_heartbeat_interval is None and raw_dream_interval is None:
        raise WebUISettingsError("heartbeat_interval_s or dream_interval_h is required")

    config = load_config()
    changed = False

    if raw_heartbeat_interval is not None:
        try:
            heartbeat_interval = int(raw_heartbeat_interval)
        except ValueError:
            raise WebUISettingsError("heartbeat_interval_s must be an integer") from None
        if heartbeat_interval < 60 or heartbeat_interval > 86400:
            raise WebUISettingsError("heartbeat_interval_s must be between 60 and 86400")
        if config.gateway.heartbeat.interval_s != heartbeat_interval:
            config.gateway.heartbeat.interval_s = heartbeat_interval
            changed = True

    if raw_dream_interval is not None:
        try:
            dream_interval = int(raw_dream_interval)
        except ValueError:
            raise WebUISettingsError("dream_interval_h must be an integer") from None
        if dream_interval < 1 or dream_interval > 48:
            raise WebUISettingsError("dream_interval_h must be between 1 and 48")
        if config.agents.defaults.dream.interval_h != dream_interval:
            config.agents.defaults.dream.interval_h = dream_interval
            changed = True

    if changed:
        save_config(config)
    # Heartbeat/dream intervals are re-registered on the running cron service
    # by the WebSocket channel handler, so no gateway restart is required.
    from .settings_api import settings_payload
    return settings_payload(requires_restart=False)
