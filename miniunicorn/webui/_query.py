"""通用查询参数解析工具。

从 ``_helpers.py`` / ``_runtime.py`` 抽出的无业务依赖工具,
被各 settings 域 api 模块(model_settings_api / web_fetch_api / ...)
以及 channels_api / cron_api / tools_api 等独立领域模块共用。

放在独立模块,避免这些模块借道 settings 的 ``_helpers`` 取工具。
"""

from __future__ import annotations

from typing import Any

from ._runtime import WebUISettingsError

# QueryParams: HTTP query string 解析后的多值字典 {key: [values]}。
# 从 _runtime.py 迁入,保持 ``QueryParams`` 在各模块间的统一类型别名。
QueryParams = dict[str, list[str]]


def _query_first(query: QueryParams, key: str) -> str | None:
    """返回 query 中 key 的首个值,不存在返回 None。"""
    values = query.get(key)
    return values[0] if values else None


def _query_first_alias(query: QueryParams, snake: str, camel: str) -> str | None:
    """优先取 snake_case key,缺失时回退 camelCase key。"""
    value = _query_first(query, snake)
    return _query_first(query, camel) if value is None else value


def _clip_ws_string(value: Any, limit: int = 240) -> str | None:
    """截断字符串到指定长度,非字符串或空字符串返回 None。

    用于规范化 WebUI 提交的 mention/attachment 字段值。
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text[:limit]


def _parse_bool(value: str, field: str) -> bool:
    """将 query 字符串解析为布尔值,非法值抛 WebUISettingsError。"""
    normalized = value.strip().lower()
    if normalized not in {"1", "0", "true", "false", "yes", "no"}:
        raise WebUISettingsError(f"{field} must be boolean")
    return normalized in {"1", "true", "yes"}
