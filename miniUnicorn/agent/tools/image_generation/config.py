"""image_generation 工具独立配置 schema。

参考 Plan & Execute 的 planner_model 设计:
- 用户在 `preset` 字段引用一个已配置的 model_preset 名 (或 "default" 用主模型)
- 工具运行时从 model_preset 拿 api_key/api_base/model 凭证
- api_type 由 preset 的 provider 自动推断 (见 infer_api_type_from_provider),
  不再暴露给用户手动配置
- response_format 也由 preset 的 provider 自动推断 (见 infer_response_format_from_provider),
  不再暴露给用户手动配置 (仅对 images_generations 协议生效)

api_type 自动推断规则 (见 infer_api_type_from_provider):
- dashscope → dashscope_multimodal (阿里通义万相)
- openrouter → chat_completions (OpenRouter 风格)
- 其他 → images_generations (OpenAI 标准 /images/generations, 默认)

response_format 自动推断规则 (见 infer_response_format_from_provider):
- zhipu / stepfun / minimax → url (返回临时图片 URL, 适配器自动下载)
- 其他 → b64_json (OpenAI 标准, 直接返回 base64, 默认)

ImageGenerationProviderConfig 保留为运行时内部数据结构 (tool.py 用来传给 adapter),
不再从 config.json 读取。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import AliasChoices, Field, model_validator
from pydantic.alias_generators import to_camel
from pydantic import ConfigDict

from miniUnicorn.config.schema import Base


# 支持的协议适配器白名单 (与 providers/__init__.py 的注册表保持一致)
SUPPORTED_API_TYPES: tuple[str, ...] = (
    "images_generations",
    "chat_completions",
    "dashscope_multimodal",
)

# provider → image api_type 自动推断映射
# 仅列出需要特判的 provider, 其余 (openai/zhipu/stepfun/aihubmix/minimax/gemini/ollama/custom 等)
# 全部回退到默认的 images_generations 协议
_PROVIDER_TO_API_TYPE: dict[str, str] = {
    "dashscope": "dashscope_multimodal",
    "openrouter": "chat_completions",
}


def infer_api_type_from_provider(provider: str | None) -> str:
    """根据 model_preset 解析出的 provider 名推断 image api_type。

    - dashscope → dashscope_multimodal (阿里通义万相 MultiModalConversation 协议)
    - openrouter → chat_completions (OpenRouter 风格 /chat/completions with modalities=["image"])
    - 其他 (含 None / "auto" / 未知) → images_generations (OpenAI 标准 /images/generations, 默认)

    这是 best-effort 推断: provider_name 由 config.get_provider_name() 解析,
    对于 "auto" 会通过 model 关键词匹配。匹配失败时回退到默认协议。
    """
    if not provider or provider == "auto":
        return "images_generations"
    return _PROVIDER_TO_API_TYPE.get(provider, "images_generations")


# provider → response_format 自动推断映射 (仅对 images_generations 协议生效;
# 其他协议不读 response_format, 推断结果无影响)
# 仅列出明确返回临时 URL 的 provider, 其余 (openai/aihubmix/gemini/ollama/custom 等) 回退到 b64_json
_PROVIDER_TO_RESPONSE_FORMAT: dict[str, str] = {
    "zhipu": "url",     # 文档明确: glm-image/cogview 返回临时 URL (30 天有效)
    "stepfun": "url",   # 阶跃星辰 step-image 系列返回临时 URL
    "minimax": "url",   # MiniMax image-01 返回临时 URL
}


def infer_response_format_from_provider(provider: str | None) -> str:
    """根据 model_preset 解析出的 provider 名推断 image response_format。

    - zhipu / stepfun / minimax → "url" (国产 OpenAI 兼容端点惯例, 返回临时图片 URL)
    - 其他 (含 None / "auto" / 未知) → "b64_json" (OpenAI 标准, 直接返回 base64)

    仅对 images_generations 协议生效; chat_completions / dashscope_multimodal
    协议各自有固定的响应结构, 不读此字段。
    """
    if not provider or provider == "auto":
        return "b64_json"
    return _PROVIDER_TO_RESPONSE_FORMAT.get(provider, "b64_json")


class ImageGenerationProviderConfig(Base):
    """运行时图片生成 provider 配置 (内部数据结构)。

    tool.py 在 create() 时从 model_preset 解析凭证, 组装本类实例传给 adapter。
    不再从 config.json 读取——凭证完全复用 model_preset。
    """

    api_key: str | None = None
    api_base: str | None = None
    # 协议适配器: 决定如何构造请求 / 解析响应。默认 OpenAI 标准 images API。
    api_type: Literal["images_generations", "chat_completions", "dashscope_multimodal"] = Field(
        default="images_generations",
        validation_alias=AliasChoices("apiType", "api_type"),
        serialization_alias="apiType",
    )
    # 响应数据格式提示 (仅 images_generations 协议使用):
    # - "b64_json" (默认): 期望响应中 data[].b64_json 字段
    # - "url": 期望响应中 data[].url 字段, 适配器会自动下载并转 base64 data URL
    response_format: Literal["b64_json", "url"] = Field(
        default="b64_json",
        validation_alias=AliasChoices("responseFormat", "response_format"),
        serialization_alias="responseFormat",
    )
    extra_headers: dict[str, str] | None = None
    # 额外请求体字段 (合并到请求 JSON); 形状由具体 API 决定
    # 例如 OpenAI gpt-image: {"quality": "high"}, 阶跃星辰: {"style_reference": ...}
    extra_body: dict[str, Any] | None = None
    # 请求超时秒数; None 时使用适配器默认值 (120s)
    timeout: float | None = None


class ImageGenerationConfig(Base):
    """image_generation 工具的独立配置。

    通过 ToolsConfig.image_generation 字段访问。Provider 凭证复用
    `agents.model_presets` 中已配置的预设 (与 Plan & Execute 的
    `planner_model` 字段同模式), 不再维护独立 providers 字典。

    配置示例::

        {
          "enabled": true,
          "preset": "my_openai",
          "apiType": "images_generations",
          "responseFormat": "b64_json",
          "defaultAspectRatio": "1:1",
          "defaultImageSize": "1K",
          "maxImagesPerTurn": 4,
          "saveDir": "generated"
        }

    `preset` 字段:
    - "default" 或 "" : 使用主模型预设 (agents.defaults)
    - 其他字符串    : 必须对应 model_presets 字典中的一个 key
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="allow")

    enabled: bool = False
    # 引用 model_preset 名; "default" 或空串表示用主模型预设
    preset: str = "default"
    # 协议适配器 (图像 API 特有, model_preset 未涵盖)
    api_type: Literal["images_generations", "chat_completions", "dashscope_multimodal"] = Field(
        default="images_generations",
        validation_alias=AliasChoices("apiType", "api_type"),
        serialization_alias="apiType",
    )
    # 响应格式 (仅 images_generations 协议使用)
    response_format: Literal["b64_json", "url"] = Field(
        default="b64_json",
        validation_alias=AliasChoices("responseFormat", "response_format"),
        serialization_alias="responseFormat",
    )
    # 默认宽高比, prompt/工具调用未指定时使用
    default_aspect_ratio: str = "1:1"
    # 默认尺寸提示, 例如 "1K" / "2K" / "4K" / "1024x1024"
    default_image_size: str = "1K"
    # 单次工具调用最大张数 (1-8), 防止 LLM 失控刷爆 API 账单
    max_images_per_turn: int = Field(default=4, ge=1, le=8)
    # 相对 media 根目录的子目录, 用于存放生成的图片 artifact
    save_dir: str = "generated"

    @model_validator(mode="after")
    def _validate_preset(self) -> "ImageGenerationConfig":
        """启用时校验 preset 字段非空 (具体 preset 存在性由 tool.create() 校验)。"""
        if not self.enabled:
            return self
        if not self.preset:
            # 启用时自动回退到 "default"
            self.preset = "default"
        if self.api_type not in SUPPORTED_API_TYPES:
            raise ValueError(
                f"image_generation.apiType '{self.api_type}' is not supported; "
                f"supported: {list(SUPPORTED_API_TYPES)}"
            )
        if self.response_format not in ("b64_json", "url"):
            raise ValueError(
                f"image_generation.responseFormat '{self.response_format}' must be 'b64_json' or 'url'"
            )
        return self
