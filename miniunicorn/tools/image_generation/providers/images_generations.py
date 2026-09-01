"""images_generations 协议适配器 (OpenAI 标准 /images/generations API)。

适用于 OpenAI (dall-e-3, gpt-image-1) / 智谱 cogview / 阶跃星辰 / AIHubMix /
自部署 SD WebUI OpenAI 兼容接口等大多数 OpenAI 风格图片生成端点。

请求: POST {api_base}/images/generations
Body: { "model", "prompt", "n", "size", "response_format"|"response_format", ... }
Response:
  - responseFormat="b64_json" (默认): { "data": [{"b64_json": "..."}] }
  - responseFormat="url": { "data": [{"url": "..."}] }

参考图编辑: OpenAI gpt-image-1 支持 multipart/form-data 的 /images/edits 端点;
本适配器在收到参考图时自动切换到 /images/edits, 用 multipart 上传 image 文件。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from miniunicorn.tools.image_generation.config import ImageGenerationProviderConfig
from miniunicorn.tools.image_generation.providers._http import build_async_client
from miniunicorn.tools.image_generation.providers.base import (
    GeneratedImageResponse,
    ImageGenerationAdapter,
    ImageGenerationError,
    build_headers,
    download_url_to_data_url,
    get_api_base,
    merge_extra_body,
    resolve_timeout,
)

# OpenAI 标准 images API 默认 base URL (与 openai Python SDK 一致)
_DEFAULT_OPENAI_BASE = "https://api.openai.com/v1"

# 支持编辑的 OpenAI 模型 (走 /images/edits multipart 端点)
_EDIT_CAPABLE_MODELS: frozenset[str] = frozenset({"gpt-image-1", "dall-e-2"})


class ImagesGenerationsAdapter(ImageGenerationAdapter):
    """OpenAI 标准 /images/generations 协议适配器。"""

    @classmethod
    def api_type(cls) -> str:
        return "images_generations"

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
        base = get_api_base(provider_config, fallback=_DEFAULT_OPENAI_BASE)
        timeout = resolve_timeout(provider_config, self.default_timeout)

        # 参考图编辑: OpenAI gpt-image-1 / dall-e-2 走 /images/edits
        if reference_images and model in _EDIT_CAPABLE_MODELS:
            return await self._call_images_edits(
                prompt=prompt,
                model=model,
                reference_images=reference_images,
                provider_config=provider_config,
                base=base,
                timeout=timeout,
                http_client=http_client,
                aspect_ratio=aspect_ratio,
                image_size=image_size,
            )

        # 文生图 (或参考图但模型不支持 edits, 退化为文生图 + 提示词引导)
        return await self._call_images_generations(
            prompt=prompt,
            model=model,
            reference_images=reference_images,
            provider_config=provider_config,
            base=base,
            timeout=timeout,
            http_client=http_client,
            aspect_ratio=aspect_ratio,
            image_size=image_size,
        )

    async def _call_images_generations(
        self,
        *,
        prompt: str,
        model: str,
        reference_images: list[Path] | None,
        provider_config: ImageGenerationProviderConfig,
        base: str,
        timeout: float,
        http_client: httpx.AsyncClient | None,
        aspect_ratio: str | None,
        image_size: str | None,
    ) -> GeneratedImageResponse:
        url = f"{base}/images/generations"

        body: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "n": 1,
        }

        # 尺寸: image_size 优先 (如 "1024x1024"), 否则用 aspect_ratio 映射
        size_hint = image_size or _aspect_to_size(aspect_ratio)
        if size_hint:
            body["size"] = size_hint

        # 响应格式 (b64_json 默认; url 时由适配器下载转 base64)
        body["response_format"] = provider_config.response_format

        # OpenAI 兼容端点可能要求 model 必填; 不要传 extra_body 中已有的字段
        body = merge_extra_body(body, provider_config)

        headers = build_headers(provider_config)

        # 参考图但模型不支持 edits: 把参考图描述塞进 prompt (best-effort, 不改变请求结构)
        # 部分国产 OpenAI 兼容端点会用 extra_body.image 字段传参考图; 由用户在
        # extra_body 自行配置即可, 这里不强制约定。
        effective_prompt = prompt
        if reference_images:
            effective_prompt = (
                f"{prompt}\n\n[Reference images provided but model '{model}' does not "
                f"support /images/edits; passing paths for reference only: "
                f"{', '.join(str(p) for p in reference_images)}]"
            )
            body["prompt"] = effective_prompt

        client_owned = http_client is None
        client = http_client or build_async_client(timeout=timeout)
        try:
            try:
                resp = await client.post(url, json=body, headers=headers, timeout=timeout)
            except httpx.HTTPError as exc:
                raise ImageGenerationError(f"images_generations request failed: {exc}") from exc

            if resp.status_code >= 400:
                raise ImageGenerationError(
                    f"images_generations returned {resp.status_code}: {resp.text[:500]}"
                )

            try:
                data = resp.json()
            except ValueError as exc:
                raise ImageGenerationError(
                    f"images_generations returned non-JSON: {resp.text[:200]}"
                ) from exc

            return await self._parse_response(data, provider_config=provider_config)
        finally:
            if client_owned:
                await client.aclose()

    async def _call_images_edits(
        self,
        *,
        prompt: str,
        model: str,
        reference_images: list[Path],
        provider_config: ImageGenerationProviderConfig,
        base: str,
        timeout: float,
        http_client: httpx.AsyncClient | None,
        aspect_ratio: str | None,
        image_size: str | None,
    ) -> GeneratedImageResponse:
        """OpenAI /images/edits multipart 端点 (仅 gpt-image-1 / dall-e-2)。"""
        url = f"{base}/images/edits"

        # OpenAI edits 只支持单张参考图 (image 参数); 取第一张
        image_path = reference_images[0]
        size_hint = image_size or _aspect_to_size(aspect_ratio)

        # multipart/form-data; 不传 Content-Type 让 httpx 自动加 boundary
        form_fields: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "n": "1",
        }
        if size_hint:
            form_fields["size"] = size_hint
        if provider_config.response_format:
            form_fields["response_format"] = provider_config.response_format

        # extra_body 中的字段合并到 form (用户额外字段如 quality/style)
        if provider_config.extra_body:
            for k, v in provider_config.extra_body.items():
                if k not in form_fields:
                    form_fields[k] = str(v) if not isinstance(v, str) else v

        # image 文件
        try:
            with image_path.open("rb") as f:
                image_bytes = f.read()
        except OSError as exc:
            raise ImageGenerationError(f"cannot read reference image {image_path}: {exc}") from exc

        files = {"image": (image_path.name, image_bytes, "application/octet-stream")}

        # multipart 请求不用 build_headers 里的 Content-Type (httpx 自动设置)
        headers = build_headers(provider_config, extra={"Content-Type": None} if False else None)
        headers.pop("Content-Type", None)

        client_owned = http_client is None
        client = http_client or build_async_client(timeout=timeout)
        try:
            try:
                resp = await client.post(
                    url, data=form_fields, files=files, headers=headers, timeout=timeout
                )
            except httpx.HTTPError as exc:
                raise ImageGenerationError(f"images_edits request failed: {exc}") from exc

            if resp.status_code >= 400:
                raise ImageGenerationError(
                    f"images_edits returned {resp.status_code}: {resp.text[:500]}"
                )

            try:
                data = resp.json()
            except ValueError as exc:
                raise ImageGenerationError(
                    f"images_edits returned non-JSON: {resp.text[:200]}"
                ) from exc

            return await self._parse_response(data, provider_config=provider_config)
        finally:
            if client_owned:
                await client.aclose()

    async def _parse_response(
        self,
        data: dict[str, Any],
        *,
        provider_config: ImageGenerationProviderConfig,
    ) -> GeneratedImageResponse:
        """解析 OpenAI 风格响应, 统一转为 data URL 列表。"""
        items = data.get("data") or []
        if not isinstance(items, list) or not items:
            raise ImageGenerationError(
                f"images_generations response has no 'data' array: {str(data)[:300]}"
            )

        images: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            if provider_config.response_format == "url":
                url_value = item.get("url")
                if not isinstance(url_value, str):
                    continue
                # URL 形式: 下载并转 base64 data URL
                data_url = await download_url_to_data_url(url_value)
                images.append(data_url)
            else:
                b64 = item.get("b64_json")
                if isinstance(b64, str) and b64:
                    # 校验并补全 MIME (data URL 协议要求前缀)
                    mime = _detect_b64_mime(b64)
                    images.append(f"data:{mime};base64,{b64}")

        if not images:
            raise ImageGenerationError(
                f"images_generations response parsed no images: {str(data)[:300]}"
            )

        return GeneratedImageResponse(images=images, raw=data)


def _aspect_to_size(aspect_ratio: str | None) -> str:
    """把宽高比映射到 OpenAI 标准尺寸 (gpt-image-1 / dall-e-3 接受的尺寸枚举)。"""
    if not aspect_ratio:
        return ""
    mapping = {
        "1:1": "1024x1024",
        "16:9": "1792x1024",
        "9:16": "1024x1792",
        "4:3": "",  # OpenAI 不直接支持, 留空让端点用默认
        "3:4": "",
    }
    return mapping.get(aspect_ratio, "")


def _detect_b64_mime(b64: str) -> str:
    """从 base64 字符串前几字节探测 MIME (用于 b64_json 响应补全 data URL 前缀)。"""
    import base64 as _b64

    try:
        head = _b64.b64decode(b64[:32], validate=False)[:16]
    except Exception:
        return "image/png"  # 默认 PNG (大多数 b64_json 响应都是 PNG)

    from miniunicorn.utils.helpers import detect_image_mime

    detected = detect_image_mime(head)
    return detected or "image/png"
