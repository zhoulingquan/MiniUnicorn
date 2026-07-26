"""协议适配器抽象基类。

所有 apiType 适配器 (images_generations / chat_completions / dashscope_multimodal)
继承此类, 实现 generate() 方法。工具层只调 adapter.generate(),
不感知具体协议; 切换 provider 协议只需改 config.apiType。

设计要点:
- 输入: prompt/model/参考图本地路径列表/宽高比/尺寸提示
- 输出: GeneratedImageResponse, images 列表每项必须是
        `data:image/<mime>;base64,...` 形式的 data URL
        (URL 形式的响应由适配器自行下载并转 base64, 上层无感)
- 错误: 统一抛 ImageGenerationError, 由工具层捕获转 ToolResult.error
"""

from __future__ import annotations

import base64
import mimetypes
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from miniunicorn.agent.tools.image_generation.config import ImageGenerationProviderConfig
from miniunicorn.utils.helpers import detect_image_mime


class ImageGenerationError(RuntimeError):
    """图片生成 provider 调用失败。"""


@dataclass
class GeneratedImageResponse:
    """所有协议适配器的统一返回值。

    Attributes:
        images: data:image/<mime>;base64,... 形式的 data URL 列表
                (URL 形式的响应必须由适配器自行下载转 base64)
        content: provider 返回的文本说明 (可选, 用于日志/调试)
        raw: 原始响应字典 (可选, 用于调试; 不会传给 LLM)
    """

    images: list[str] = field(default_factory=list)
    content: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class ImageGenerationAdapter(ABC):
    """协议适配器抽象基类。

    子类需实现 :meth:`generate`, 接收用户配置 (api_key/api_base/extra_headers/extra_body/...)
    和调用参数 (prompt/model/reference_images/aspect_ratio/image_size), 返回
    :class:`GeneratedImageResponse`。

    子类应通过 :meth:`api_type` 类方法声明自己负责的 apiType (用于注册和校验)。
    """

    @classmethod
    @abstractmethod
    def api_type(cls) -> str:
        """此适配器负责的 apiType 字符串 (与 config.apiType 匹配)。"""
        ...

    @property
    def default_timeout(self) -> float:
        """默认请求超时秒数。"""
        return 120.0

    @abstractmethod
    async def generate(
        self,
        *,
        prompt: str,
        model: str,
        provider_config: ImageGenerationProviderConfig,
        reference_images: list[Path] | None = None,
        aspect_ratio: str | None = None,
        image_size: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> GeneratedImageResponse:
        """构造并发送请求, 解析响应, 返回统一结构的 GeneratedImageResponse。

        Args:
            prompt: 用户/LLM 提供的图像生成或编辑提示词
            model: provider 对应的模型 ID
            provider_config: 用户在 config.providers.<name> 下的配置
            reference_images: 已通过 path_guard 校验的本地参考图路径列表 (空表示文生图)
            aspect_ratio: 输出宽高比 (例如 "1:1", "16:9")
            image_size: 输出尺寸提示 (例如 "1K", "2K", "1024x1024")
            http_client: 可选的共享 httpx 客户端 (由工具层管理生命周期); None 时自建临时客户端
        """
        ...


# ---- 共享辅助函数 (供具体适配器复用) ----


def file_to_data_url(path: Path) -> str:
    """读取本地图片文件并编码为 data:image/<mime>;base64,... 形式的 data URL。"""
    raw = path.read_bytes()
    mime = detect_image_mime(raw[:16])
    if mime is None:
        # 回退到扩展名推断
        ext_mime, _ = mimetypes.guess_type(str(path))
        mime = ext_mime or "application/octet-stream"
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


async def download_url_to_data_url(
    url: str,
    *,
    client: httpx.AsyncClient,
    timeout: float | None = None,
) -> str:
    """下载 HTTP(S) 图片 URL 并转为 data URL (用于 url 响应格式适配器)。

    Raises:
        ImageGenerationError: 下载失败或响应不是有效图片
    """
    try:
        resp = await client.get(url, timeout=timeout or 60.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise ImageGenerationError(f"failed to download image from {url}: {exc}") from exc

    raw = resp.content
    mime = detect_image_mime(raw[:16])
    if mime is None:
        # 回退到 Content-Type
        ct = resp.headers.get("content-type", "").split(";")[0].strip()
        mime = ct or "image/png"
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def build_headers(
    provider_config: ImageGenerationProviderConfig,
    *,
    auth_prefix: str = "Bearer",
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """构造请求头: Authorization + Content-Type + extra_headers + 调用方传入的 extra。"""
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if provider_config.api_key:
        headers["Authorization"] = f"{auth_prefix} {provider_config.api_key}"
    if provider_config.extra_headers:
        headers.update(provider_config.extra_headers)
    if extra:
        headers.update(extra)
    return headers


def merge_extra_body(body: dict[str, Any], provider_config: ImageGenerationProviderConfig) -> dict[str, Any]:
    """把 provider_config.extra_body 浅合并到请求体 (用户传入字段优先级最低, 不覆盖已设字段)。"""
    if not provider_config.extra_body:
        return body
    merged = dict(provider_config.extra_body)
    merged.update(body)  # 已设字段优先
    return merged


def get_api_base(provider_config: ImageGenerationProviderConfig, *, fallback: str) -> str:
    """取 apiBase; 空时使用 fallback (子类提供的协议默认值)。"""
    base = (provider_config.api_base or "").rstrip("/")
    return base or fallback


def resolve_timeout(provider_config: ImageGenerationProviderConfig, adapter_default: float) -> float:
    """取超时秒数; None 时使用适配器默认值。"""
    if provider_config.timeout is None or provider_config.timeout <= 0:
        return adapter_default
    return provider_config.timeout
