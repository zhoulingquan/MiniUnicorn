"""Heartbeat constants and helpers (extracted from cli/commands.py).

This module isolates the heartbeat preamble text, the cached HEARTBEAT.md
template loader, and the helper that builds a dedicated provider for the
heartbeat job based on ``HeartbeatConfig.model_preset``.
"""

import functools
from datetime import datetime, time as dt_time

from loguru import logger

_HEARTBEAT_PREAMBLE = (
    "[Your response will be delivered directly to the user's messaging app. "
    "Output ONLY the final user-facing message. Never reference internal "
    "files (HEARTBEAT.md, AWARENESS.md, etc.), your instructions, or your "
    "decision process. If nothing needs reporting, respond with just "
    "'All clear.' and nothing else.]\n\n"
)

# 轻量巡检 preamble:HEARTBEAT.md 为空时仍允许 agent 做一次轻量巡检
# (检查 cron 任务状态、检查 dream 产物等),不因空模板而短路。
_HEARTBEAT_LIGHT_PREAMBLE = (
    "[Your response will be delivered directly to the user's messaging app. "
    "Output ONLY the final user-facing message. Never reference internal "
    "files (HEARTBEAT.md, AWARENESS.md, etc.), your instructions, or your "
    "decision process. If nothing needs reporting, respond with just "
    "'All clear.' and nothing else.]\n\n"
    "Perform a lightweight proactive check: review recent cron jobs, dream "
    "outputs, and any pending tasks. Report only if something needs the "
    "user's attention.\n\n"
)


@functools.lru_cache(maxsize=None)
def _heartbeat_template() -> str | None:
    from miniUnicorn.utils.helpers import load_bundled_template
    return load_bundled_template("HEARTBEAT.md")


def _build_heartbeat_provider(hb_cfg, config):
    """根据 HeartbeatConfig.model_preset 构建一个独立的 provider + model。

    返回 (provider, model) 或 None。当 hb_cfg.model_preset 为空或指向
    不存在的 preset 时返回 None,表示 heartbeat 复用 agent 主 provider。
    """
    preset_name = hb_cfg.model_preset
    if not preset_name:
        return None
    preset = config.model_presets.get(preset_name)
    if preset is None:
        logger.warning("Heartbeat: model_preset '{}' not found, fallback to main provider", preset_name)
        return None
    from miniUnicorn.providers.factory import make_provider

    provider = make_provider(config, preset_name=preset_name)
    return provider, preset.model


def _is_within_active_hours(active_hours: dict[str, str] | None, tz) -> bool:
    """检查当前时间(指定时区)是否在 active_hours 窗口内。

    active_hours 格式: {"start": "08:00", "end": "24:00"}
    - end > start: 普通窗口(如 08:00-24:00)
    - end < start: 跨夜窗口(如 22:00-06:00)
    - "24:00" 视为当天结束
    active_hours 为 None 时始终返回 True(不限制)。
    """
    if not active_hours:
        return True
    start_str = active_hours.get("start")
    end_str = active_hours.get("end")
    if not start_str or not end_str:
        return True
    try:
        now = datetime.now(tz).time()
        start = _parse_hhmm(start_str)
        end = _parse_hhmm(end_str)
    except (ValueError, TypeError):
        logger.warning("Heartbeat: invalid active_hours {}-{}, skipping check", start_str, end_str)
        return True
    if start == end:
        return True
    # 跨夜窗口(如 22:00-06:00):当前 >= start 或 < end
    if start > end:
        return now >= start or now < end
    # 普通窗口(如 08:00-24:00):start <= now < end
    return start <= now < end


def _parse_hhmm(s: str) -> dt_time:
    """解析 'HH:MM' 字符串为 time 对象;'24:00' 转为 time(23, 59, 59)。"""
    if s == "24:00":
        return dt_time(23, 59, 59)
    parts = s.split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid HH:MM format: {s}")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 24 and 0 <= m <= 59):
        raise ValueError(f"HH or MM out of range: {s}")
    if h == 24:
        return dt_time(23, 59, 59)
    return dt_time(h, m)
