"""chat_completions 协议适配器 (OpenRouter 风格 /chat/completions with modalities=["image"])。

适用于 OpenRouter / 任何通过 Chat Completions API 返回图片的端点
(如 OpenRouter 的 openai/gpt-5.4-image-2 等模型)。

请求: POST {api_base}/chat/completions
Body: {
    "model": "...",
    "messages": [{"role": "user", "content": "..."}],
    "modalities": ["image", "text"],
    "image_config": {"aspect_ratio": "...", "image_size": "..."},  // 可选
    "stream": false
}
Response: {
    "choices": [{
        "message": {
            "images": [{"image_url": {"url": "data:image/png;base64,..."}}],
            "content": "..."  // 可选文本说明
        }
    }]
}

参考图编辑: 通过 messages content 的多模态结构传入 image_url。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from miniunicorn.agent.tools.image_generation.config import ImageGenerationProviderConfig
from miniunicorn.agent.tools.image_generation.providers._http import build_async_client
from miniunicorn.agent.tools.image_generation.providers.base import (
    GeneratedImageResponse,
    ImageGenerationAdapter,
    ImageGenerationError,
    build_headers,
    file_to_data_url,
    get_api_base,
    merge_extra_body,
    resolve_timeout,
)

_DEFAULT_BASE = "https://openrouter.ai/api/v1"


class ChatCompletionsAdapter(ImageGenerationAdapter):
    """OpenRouter 风格 chat_completions with modalities=["image"] 协议适配器。"""

    @classmethod
    def api_type(cls) -> str:
        return "chat_completions"

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
        base = get_api_base(provider_config, fallback=_DEFAULT_BASE)
        url = f"{base}/chat/completions"
        timeout = resolve_timeout(provider_config, self.default_timeout)

        # 构造 messages content
        content: list[dict[str, Any]] | str
        if reference_images:
            # 多模态: 先图片后文本 (参考图编辑场景)
            content = []
            for img_path in reference_images:
                try:
                    data_url = file_to_data_url(img_path)
                except OSError as exc:
                    raise ImageGenerationError(
                        f"cannot read reference image {img_path}: {exc}"
                    ) from exc
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url},
                    }
                )
            content.append({"type": "text", "text": prompt})
        else:
            content = prompt

        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "modalities": ["image", "text"],
            "stream": False,
        }

        # image_config (OpenRouter 扩展字段)
        image_config: dict[str, str] = {}
        if aspect_ratio:
            image_config["aspect_ratio"] = aspect_ratio
        if image_size:
            image_config["image_size"] = image_size
        if image_config:
            body["image_config"] = image_config

        body = merge_extra_body(body, provider_config)
        headers = build_headers(provider_config)

        client_owned = http_client is None
        client = http_client or build_async_client(timeout=timeout)
        try:
            try:
                resp = await client.post(url, json=body, headers=headers, timeout=timeout)
            except httpx.HTTPError as exc:
                raise ImageGenerationError(f"chat_completions request failed: {exc}") from exc

            if resp.status_code >= 400:
                raise ImageGenerationError(
                    f"chat_completions returned {resp.status_code}: {resp.text[:500]}"
                )

            try:
                data = resp.json()
            except ValueError as exc:
                raise ImageGenerationError(
                    f"chat_completions returned non-JSON: {resp.text[:200]}"
                ) from exc

            return self._parse_response(data)
        finally:
            if client_owned:
                await client.aclose()

    def _parse_response(self, data: dict[str, Any]) -> GeneratedImageResponse:
        """解析 chat_completions 响应, 提取 message.images[].image_url.url。"""
        choices = data.get("choices") or []
        images: list[str] = []
        text_parts: list[str] = []

        for choice in choices:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message") or {}
            if not isinstance(message, dict):
                continue

            # 提取图片
            for image in message.get("images") or []:
                if not isinstance(image, dict):
                    continue
                image_url = image.get("image_url") or image.get("imageUrl") or {}
                if isinstance(image_url, dict):
                    url_value = image_url.get("url")
                else:
                    url_value = image_url  # 兼容直接字符串形式
                if isinstance(url_value, str) and url_value.startswith("data:image/"):
                    images.append(url_value)

            # 提取文本说明 (可选)
            content = message.get("content")
            if isinstance(content, str):
                text_parts.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text = part.get("text")
                        if isinstance(text, str):
                            text_parts.append(text)

        if not images:
            raise ImageGenerationError(
                f"chat_completions response parsed no images: {str(data)[:300]}"
            )

        return GeneratedImageResponse(
            images=images,
            content="\n".join(text_parts) if text_parts else "",
            raw=data,
        )
