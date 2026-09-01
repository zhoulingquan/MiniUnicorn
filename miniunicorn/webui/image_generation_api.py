"""Image generation 领域模块: 复用 model_preset 凭证 + 工具开关。

参考 Plan & Execute 的 planner_model 模式:
- ``image_generation_payload``: 构造 settings payload 中 image_generation 区域
- ``update_image_generation_settings``: 处理配置更新请求

Provider 凭证完全复用 ``agents.model_presets`` 中已配置的预设,
不再维护独立 providers 字典。前端在 Settings → Image 页面让用户从
已配置的 model presets 中选一个作为图像生成模型来源。
"""

from __future__ import annotations

from typing import Any

from miniunicorn.config.loader import load_config

from ._query import QueryParams, _query_first, _query_first_alias
from ._runtime import WebUISettingsError

# 与 miniunicorn.tools.image_generation.providers.SUPPORTED_API_TYPES 保持一致
_SUPPORTED_API_TYPES: tuple[str, ...] = (
    "images_generations",
    "chat_completions",
    "dashscope_multimodal",
)

# 可选宽高比下拉项 (与 ImageGenerationConfig.default_aspect_ratio 对齐)
_ASPECT_RATIO_OPTIONS: tuple[str, ...] = (
    "1:1",
    "16:9",
    "9:16",
    "4:3",
    "3:4",
    "3:2",
    "2:3",
)


# === Payload builder ===


def image_generation_payload(config: Any) -> dict[str, Any]:
    """构造 settings payload 中 image_generation 区域。

    由 ``settings_api.settings_payload`` 调用聚合。
    凭证不下发 (复用 model_preset, 这里只返回选中的 preset 名 + 图像 API 特有字段)。
    前端直接复用 settings.model_presets 作为下拉选项 (与 PlannerConfig 同模式),
    因此这里不再单独构造 available_presets 副本。
    """
    from miniunicorn.config.paths import get_media_dir

    ig = config.tools.image_generation
    # 完整保存路径 = media 根目录 / save_dir 子目录 (供前端展示)
    save_dir_full = str(get_media_dir() / ig.save_dir)
    return {
        "image_generation": {
            "enabled": ig.enabled,
            "preset": ig.preset,
            "api_type": ig.api_type,
            "response_format": ig.response_format,
            "default_aspect_ratio": ig.default_aspect_ratio,
            "default_image_size": ig.default_image_size,
            "max_images_per_turn": ig.max_images_per_turn,
            "save_dir": ig.save_dir,
            "save_dir_full": save_dir_full,
            "supported_api_types": list(_SUPPORTED_API_TYPES),
            "aspect_ratio_options": list(_ASPECT_RATIO_OPTIONS),
        }
    }


# === Update handler ===


def update_image_generation_settings(query: QueryParams) -> dict[str, Any]:
    """处理 image_generation 配置更新请求。

    接受 query 参数:
    - enable (bool): 启用/禁用工具
    - preset (str): 引用 model_preset 名 ("default" 或空 = 主模型; 其他必须在 model_presets 中)
    - default_aspect_ratio (str)
    - default_image_size (str)
    - max_images_per_turn (int)
    - save_dir (str)

    注: api_type 和 response_format 不再由用户配置, 运行时根据 preset 的 provider
    自动推断 (见 image_generation.config.infer_api_type_from_provider /
    infer_response_format_from_provider)。

    启用时校验 preset 对应的 model_preset 存在且配置了 api_key (否则无法调用图像 API)。
    """
    config = load_config()
    ig = config.tools.image_generation
    changed = False
    restart_required = False

    def set_value(attr: str, value: object) -> None:
        nonlocal changed
        if getattr(ig, attr) != value:
            setattr(ig, attr, value)
            changed = True

    # 1. enable (启用/禁用): 启用时校验可用 preset
    enable_val = _query_first(query, "enable")
    if enable_val is not None:
        normalized = enable_val.strip().lower()
        if normalized not in {"1", "0", "true", "false", "yes", "no"}:
            raise WebUISettingsError("enable must be boolean")
        new_enable = normalized in {"1", "true", "yes"}
        if new_enable:
            # 启用时校验 preset 可用 (有 model_presets 或 default preset 有效)
            target_preset = ig.preset or "default"
            _resolve_preset_or_raise(config, target_preset)
        set_value("enabled", new_enable)

    # 2. preset (切换 model_preset): 必须存在于 model_presets 或为 "default"
    preset_val = _query_first(query, "preset")
    if preset_val is not None and preset_val.strip():
        preset_name = preset_val.strip()
        # "default" 或空 = 主模型预设 (无需在 model_presets 中)
        if preset_name != "default":
            _resolve_preset_or_raise(config, preset_name)
        if ig.preset != preset_name:
            ig.preset = preset_name
            changed = True

    # 3. 工具全局字段: default_aspect_ratio / default_image_size / max_images_per_turn / save_dir
    aspect_val = _query_first_alias(query, "default_aspect_ratio", "defaultAspectRatio")
    if aspect_val is not None and aspect_val.strip():
        # 允许任意字符串 (某些 provider 可能支持非标准比例); 不做强校验
        set_value("default_aspect_ratio", aspect_val.strip())

    size_val = _query_first_alias(query, "default_image_size", "defaultImageSize")
    if size_val is not None:
        set_value("default_image_size", size_val.strip())

    max_val = _query_first_alias(query, "max_images_per_turn", "maxImagesPerTurn")
    if max_val is not None and max_val.strip():
        try:
            max_images = int(max_val)
        except ValueError:
            raise WebUISettingsError("max_images_per_turn must be an integer") from None
        if max_images < 1 or max_images > 8:
            raise WebUISettingsError("max_images_per_turn must be between 1 and 8")
        set_value("max_images_per_turn", max_images)

    save_dir_val = _query_first_alias(query, "save_dir", "saveDir")
    if save_dir_val is not None:
        set_value("save_dir", save_dir_val.strip())

    if changed:
        # 校验完整配置
        try:
            ig.model_validate(ig)  # type: ignore[arg-type]
        except Exception as exc:
            raise WebUISettingsError(f"config validation failed: {exc}") from exc
        from miniunicorn.config.loader import save_config

        save_config(config)
        restart_required = True  # image_generation 配置变更需要重启 gateway 加载

    from .settings_api import settings_payload

    return settings_payload(requires_restart=restart_required)


def _resolve_preset_or_raise(config: Any, preset_name: str) -> None:
    """校验 preset 名可用 (default 或在 model_presets 中)。

    不强制要求 api_key 已配置——某些 provider 可能用环境变量或无需 key,
    留给 tool.create() 在运行时通过 provider_snapshot_loader 解析。
    """
    if preset_name == "default":
        return
    if preset_name not in config.model_presets:
        available = ["default", *sorted(config.model_presets.keys())]
        raise WebUISettingsError(
            f"preset '{preset_name}' not found in model_presets; available: {available}"
        )
