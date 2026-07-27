"""dashscope_multimodal 协议适配器 (阿里通义万相 MultiModalConversation.call)。

适用于阿里云 DashScope 的通义万相系列模型 (qwen-image-2.0-pro / qwen-image-max /
qwen-image-plus 等)。通过 dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation
端点调用。

请求: POST {api_base}/services/aigc/multimodal-generation/generation
Headers:
    Authorization: Bearer {api_key}
    X-DashScope-Async: enable  (可选, 启用异步任务模式)
Body: {
    "model": "qwen-image-2.0-pro",
    "input": {
        "messages": [{"role": "user", "content": [{"text": "..."}, {"image": "data:image/..."}]}]
    },
    "parameters": {
        "size": "1024*1024",
        "n": 1,
        "watermark": false,
        "prompt_extend": true
    }
}
Response: {
    "output": {
        "choices": [{
            "message": {
                "content": [{"image": "https://dashscope-result-xx.oss-..."}]
            }
        }]
    }
}

参考图编辑: 通过 messages content 的 image 字段传入 (data URL 或 HTTP URL)。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from miniunicorn.agent.tools.image_generation.config import ImageGenerationProviderConfig
from miniunicorn.agent.tools.image_generation.providers.base import (
    GeneratedImageResponse,
    ImageGenerationAdapter,
    ImageGenerationError,
    build_headers,
    download_url_to_data_url,
    file_to_data_url,
    get_api_base,
    merge_extra_body,
    resolve_timeout,
)

_DEFAULT_BASE = "https://dashscope.aliyuncs.com/api/v1"
_GENERATION_PATH = "/services/aigc/multimodal-generation/generation"


class DashscopeMultimodalAdapter(ImageGenerationAdapter):
    """阿里通义万相 MultiModalConversation 协议适配器。"""

    @classmethod
    def api_type(cls) -> str:
        return "dashscope_multimodal"

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
        url = f"{base}{_GENERATION_PATH}"
        timeout = resolve_timeout(provider_config, self.default_timeout)

        # 构造 messages content
        content: list[dict[str, str]] = [{"text": prompt}]
        if reference_images:
            for img_path in reference_images:
                try:
                    data_url = file_to_data_url(img_path)
                except OSError as exc:
                    raise ImageGenerationError(
                        f"cannot read reference image {img_path}: {exc}"
                    ) from exc
                content.append({"image": data_url})

        # 尺寸映射: image_size (如 "1024x1024") 优先, 否则 aspect_ratio 映射
        size_hint = image_size or _aspect_to_dashscope_size(aspect_ratio)

        parameters: dict[str, Any] = {
            "n": 1,
            "watermark": False,
        }
        if size_hint:
            parameters["size"] = size_hint

        body: dict[str, Any] = {
            "model": model,
            "input": {
                "messages": [{"role": "user", "content": content}],
            },
            "parameters": parameters,
        }

        body = merge_extra_body(body, provider_config)
        # extra_body 可能在顶层 (如 X-DashScope-Async), 也可能用户想覆盖 parameters;
        # merge_extra_body 已做浅合并, 这里若用户 extra_body 含 parameters, 进一步合并
        if provider_config.extra_body and "parameters" in provider_config.extra_body:
            user_params = provider_config.extra_body["parameters"]
            if isinstance(user_params, dict):
                body["parameters"].update(user_params)

        headers = build_headers(provider_config)

        client_owned = http_client is None
        client = http_client or httpx.AsyncClient(timeout=timeout)
        try:
            try:
                resp = await client.post(url, json=body, headers=headers, timeout=timeout)
            except httpx.HTTPError as exc:
                raise ImageGenerationError(f"dashscope_multimodal request failed: {exc}") from exc

            if resp.status_code >= 400:
                raise ImageGenerationError(
                    f"dashscope_multimodal returned {resp.status_code}: {resp.text[:500]}"
                )

            try:
                data = resp.json()
            except ValueError as exc:
                raise ImageGenerationError(
                    f"dashscope_multimodal returned non-JSON: {resp.text[:200]}"
                ) from exc

            return await self._parse_response(data, client=client)
        finally:
            if client_owned:
                await client.aclose()

    async def _parse_response(
        self,
        data: dict[str, Any],
        *,
        client: httpx.AsyncClient,
    ) -> GeneratedImageResponse:
        """解析 dashscope multimodal-generation 响应。"""
        # 错误响应: { "code": "...", "message": "...", "request_id": "..." }
        if "code" in data and data.get("code"):
            code = data.get("code")
            message = data.get("message", "")
            request_id = data.get("request_id", "")
            raise ImageGenerationError(
                f"dashscope API error: code={code}, message={message}, request_id={request_id}"
            )

        output = data.get("output") or {}
        if not isinstance(output, dict):
            raise ImageGenerationError(
                f"dashscope response has no 'output' object: {str(data)[:300]}"
            )

        choices = output.get("choices") or []
        if not choices:
            raise ImageGenerationError(
                f"dashscope response has no 'choices' in output: {str(data)[:300]}"
            )

        images: list[str] = []
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message") or {}
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue

            for item in content:
                if not isinstance(item, dict):
                    continue
                # 通义万相返回 image 字段为 HTTP URL (24h 有效), 需下载转 base64
                image_value = item.get("image")
                if isinstance(image_value, str):
                    if image_value.startswith("data:image/"):
                        images.append(image_value)
                    elif image_value.startswith(("http://", "https://")):
                        data_url = await download_url_to_data_url(image_value, client=client)
                        images.append(data_url)

        if not images:
            raise ImageGenerationError(f"dashscope response parsed no images: {str(data)[:300]}")

        return GeneratedImageResponse(images=images, raw=data)


def _aspect_to_dashscope_size(aspect_ratio: str | None) -> str:
    """把宽高比映射到 DashScope size 参数 (格式: WIDTH*HEIGHT)。"""
    if not aspect_ratio:
        return ""
    mapping = {
        "1:1": "1024*1024",
        "16:9": "1280*720",
        "9:16": "720*1280",
        "4:3": "1024*768",
        "3:4": "768*1024",
        "3:2": "1200*800",
        "2:3": "800*1200",
        "21:9": "1280*560",
    }
    return mapping.get(aspect_ratio, "")
