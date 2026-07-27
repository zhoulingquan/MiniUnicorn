"""Runtime settings 领域模块:heartbeat 间隔 + dream 间隔配置。

从 ``_payload.py`` / ``_updates.py`` 迁入,集中 runtime section 相关的
payload builder 和 update handler。

公共 API(经 ``settings_api.py`` re-export):
- ``runtime_payload``: 构造 runtime 区域(config_path/workspace/heartbeat/dream)
- ``update_runtime_settings``
"""

from __future__ import annotations

from typing import Any

from miniunicorn.config.loader import get_config_path, load_config, save_config

from ._query import QueryParams, _query_first, _query_first_alias
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
                "active_hours": config.gateway.heartbeat.active_hours,
                "light_context": config.gateway.heartbeat.light_context,
                "isolated_session": config.gateway.heartbeat.isolated_session,
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
    """Update heartbeat interval and/or dream cron from WebUI query params."""
    raw_heartbeat_interval = _query_first_alias(query, "heartbeat_interval_s", "heartbeatIntervalS")
    raw_dream_cron = _query_first(query, "dream_cron")
    raw_light_context = _query_first_alias(
        query, "heartbeat_light_context", "heartbeatLightContext"
    )
    raw_isolated_session = _query_first_alias(
        query, "heartbeat_isolated_session", "heartbeatIsolatedSession"
    )
    raw_active_hours_start = _query_first_alias(
        query, "heartbeat_active_hours_start", "heartbeatActiveHoursStart"
    )
    raw_active_hours_end = _query_first_alias(
        query, "heartbeat_active_hours_end", "heartbeatActiveHoursEnd"
    )
    if (
        raw_heartbeat_interval is None
        and raw_dream_cron is None
        and raw_light_context is None
        and raw_isolated_session is None
        and raw_active_hours_start is None
        and raw_active_hours_end is None
    ):
        raise WebUISettingsError("heartbeat_interval_s or dream_cron is required")

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

    if raw_light_context is not None:
        light_context = raw_light_context.lower() in ("true", "1", "yes")
        if config.gateway.heartbeat.light_context != light_context:
            config.gateway.heartbeat.light_context = light_context
            changed = True

    if raw_isolated_session is not None:
        isolated_session = raw_isolated_session.lower() in ("true", "1", "yes")
        if config.gateway.heartbeat.isolated_session != isolated_session:
            config.gateway.heartbeat.isolated_session = isolated_session
            changed = True

    # active_hours: 两个参数都传才更新;传空字符串表示清除限制
    if raw_active_hours_start is not None or raw_active_hours_end is not None:
        start = (raw_active_hours_start or "").strip()
        end = (raw_active_hours_end or "").strip()
        if not start and not end:
            # 清除 active_hours 限制
            if config.gateway.heartbeat.active_hours is not None:
                config.gateway.heartbeat.active_hours = None
                changed = True
        else:
            # 校验 HH:MM 格式
            for label, val in [("start", start), ("end", end)]:
                if val:
                    parts = val.split(":")
                    if len(parts) != 2 or not (
                        0 <= int(parts[0]) <= 24 and 0 <= int(parts[1]) <= 59
                    ):
                        raise WebUISettingsError(f"Invalid active_hours {label}: {val}")
            new_active_hours = {"start": start or "00:00", "end": end or "24:00"}
            if config.gateway.heartbeat.active_hours != new_active_hours:
                config.gateway.heartbeat.active_hours = new_active_hours
                changed = True

    if raw_dream_cron is not None:
        cron_expr = raw_dream_cron.strip()
        if not cron_expr:
            raise WebUISettingsError("dream_cron must not be empty")
        # 校验 cron 表达式合法性（5 字段 Unix cron）
        try:
            from datetime import datetime

            from croniter import croniter

            croniter(cron_expr, datetime.now())
        except Exception:
            raise WebUISettingsError(f"Invalid cron expression: {cron_expr}") from None
        if config.agents.defaults.dream.cron != cron_expr:
            config.agents.defaults.dream.cron = cron_expr
            changed = True

    if changed:
        save_config(config)
    # Heartbeat/dream intervals are re-registered on the running cron service
    # by the WebSocket channel handler, so no gateway restart is required.
    from .settings_api import settings_payload

    return settings_payload(requires_restart=False)
