"""图片生成 provider 协议适配器注册入口。

不预置任何具体厂商 provider 实现。仅注册 3 种通用协议适配器:
- images_generations: OpenAI 标准 /images/generations 协议
- chat_completions: OpenRouter 风格 /chat/completions with modalities=["image"]
- dashscope_multimodal: 阿里通义万相 MultiModalConversation.call 协议

用户在 config.json 的 imageGeneration.providers 下自定义任意 provider 名,
通过 apiType 字段选择使用哪个协议适配器。
"""

from miniUnicorn.agent.tools.image_generation.providers.base import (
    GeneratedImageResponse,
    ImageGenerationAdapter,
    ImageGenerationError,
)
from miniUnicorn.agent.tools.image_generation.providers.chat_completions import (
    ChatCompletionsAdapter,
)
from miniUnicorn.agent.tools.image_generation.providers.dashscope_multimodal import (
    DashscopeMultimodalAdapter,
)
from miniUnicorn.agent.tools.image_generation.providers.images_generations import (
    ImagesGenerationsAdapter,
)

# 协议适配器注册表: apiType -> adapter class
# 与 config.SUPPORTED_API_TYPES 保持一致
_ADAPTERS: dict[str, type[ImageGenerationAdapter]] = {
    "images_generations": ImagesGenerationsAdapter,
    "chat_completions": ChatCompletionsAdapter,
    "dashscope_multimodal": DashscopeMultimodalAdapter,
}


def get_adapter(api_type: str) -> ImageGenerationAdapter | None:
    """工厂方法: 根据 apiType 返回适配器实例, 未知类型返回 None。"""
    cls = _ADAPTERS.get(api_type)
    return cls() if cls else None


def supported_api_types() -> tuple[str, ...]:
    """返回已注册的 apiType 列表, 供配置校验使用。"""
    return tuple(_ADAPTERS.keys())


__all__ = [
    "GeneratedImageResponse",
    "ImageGenerationAdapter",
    "ImageGenerationError",
    "ImagesGenerationsAdapter",
    "ChatCompletionsAdapter",
    "DashscopeMultimodalAdapter",
    "get_adapter",
    "supported_api_types",
]
