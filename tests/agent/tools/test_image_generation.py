"""Image generation tool tests.

覆盖:
- 配置 schema 校验 (启用/未启用, preset 字段, apiType 白名单, snake/camel case)
- 路径守卫 (workspace/media 边界, 路径穿越, MIME 伪造)
- 工具执行流程 (文生图, count 多张, count 超限, 参考图闭环, 错误处理)
- 协议适配器响应解析 (images_generations / chat_completions / dashscope_multimodal)
- 工具 create() 通过 provider_snapshot_loader 解析 preset 凭证 (参考 deep_research 模式)

测试不实际调用外部 API; 通过 mock adapter / mock httpx 响应验证逻辑。
"""

from __future__ import annotations

import base64
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from typing import Any

import pytest

# 1x1 PNG 字节流, 用于构造合法测试图
_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c63f8cf000000030001005d41d7e90000000049454e44ae426082"
)
_PNG_DATA_URL = "data:image/png;base64," + base64.b64encode(_PNG_BYTES).decode()


# -----------------------------
# 1. 配置 schema 校验 (preset 模式, 不再有 providers 字典)
# -----------------------------


def test_default_disabled():
    """默认 enabled=False, preset="default", 无需任何凭证。"""
    from miniUnicorn.config.schema import Config

    cfg = Config.model_validate({})
    assert cfg.tools.image_generation.enabled is False
    assert cfg.tools.image_generation.preset == "default"
    assert cfg.tools.image_generation.api_type == "images_generations"
    assert cfg.tools.image_generation.response_format == "b64_json"


def test_enabled_without_preset_auto_falls_back_to_default():
    """启用时 preset 为空字符串自动回退到 "default"。"""
    from miniUnicorn.agent.tools.image_generation.config import ImageGenerationConfig

    cfg = ImageGenerationConfig(enabled=True, preset="")
    # model_validator 把空串回退到 "default"
    assert cfg.preset == "default"


def test_invalid_api_type_raises():
    """apiType 必须在白名单内 (Literal 已保证, 这里二次确认)。"""
    from miniUnicorn.agent.tools.image_generation.config import (
        ImageGenerationProviderConfig,
    )
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ImageGenerationProviderConfig(api_type="totally_unknown_protocol")  # type: ignore[arg-type]


def test_invalid_api_type_on_config_raises():
    """ImageGenerationConfig 启用时 apiType 必须在白名单。"""
    from miniUnicorn.agent.tools.image_generation.config import ImageGenerationConfig
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ImageGenerationConfig(enabled=True, api_type="totally_unknown_protocol")  # type: ignore[arg-type]


def test_invalid_response_format_raises():
    """responseFormat 必须是 b64_json 或 url。"""
    from miniUnicorn.agent.tools.image_generation.config import ImageGenerationConfig
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ImageGenerationConfig(enabled=True, response_format="weird_format")  # type: ignore[arg-type]


def test_snake_and_camel_case_accepted():
    """snake_case 与 camelCase 都应被接受 (preset / apiType / responseFormat)。"""
    from miniUnicorn.config.schema import Config

    # camelCase
    cfg_camel = Config.model_validate({
        "tools": {
            "imageGeneration": {
                "enabled": True,
                "preset": "my_openai",
                "apiType": "chat_completions",
                "responseFormat": "url",
                "defaultAspectRatio": "16:9",
                "maxImagesPerTurn": 2,
                "saveDir": "out",
            }
        },
    })
    ig_camel = cfg_camel.tools.image_generation
    assert ig_camel.enabled is True
    assert ig_camel.preset == "my_openai"
    assert ig_camel.api_type == "chat_completions"
    assert ig_camel.response_format == "url"
    assert ig_camel.default_aspect_ratio == "16:9"
    assert ig_camel.max_images_per_turn == 2
    assert ig_camel.save_dir == "out"

    # snake_case
    cfg_snake = Config.model_validate({
        "tools": {
            "image_generation": {
                "enabled": True,
                "preset": "p1",
                "api_type": "images_generations",
                "response_format": "b64_json",
                "default_aspect_ratio": "1:1",
                "max_images_per_turn": 4,
                "save_dir": "generated",
            }
        },
    })
    ig_snake = cfg_snake.tools.image_generation
    assert ig_snake.api_type == "images_generations"
    assert ig_snake.response_format == "b64_json"


def test_max_images_per_turn_bounds():
    """max_images_per_turn 必须在 1-8 之间。"""
    from miniUnicorn.agent.tools.image_generation.config import ImageGenerationConfig
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ImageGenerationConfig(enabled=True, max_images_per_turn=0)
    with pytest.raises(ValidationError):
        ImageGenerationConfig(enabled=True, max_images_per_turn=9)


def test_three_api_types_accepted():
    """三种 apiType 都应被接受。"""
    from miniUnicorn.agent.tools.image_generation.config import ImageGenerationConfig

    for api_type in ("images_generations", "chat_completions", "dashscope_multimodal"):
        cfg = ImageGenerationConfig(enabled=True, api_type=api_type)  # type: ignore[arg-type]
        assert cfg.api_type == api_type


@pytest.mark.parametrize(
    "provider,expected",
    [
        ("dashscope", "dashscope_multimodal"),
        ("openrouter", "chat_completions"),
        ("openai", "images_generations"),
        ("zhipu", "images_generations"),
        ("stepfun", "images_generations"),
        ("aihubmix", "images_generations"),
        ("minimax", "images_generations"),
        ("gemini", "images_generations"),
        ("ollama", "images_generations"),
        ("custom", "images_generations"),
        ("auto", "images_generations"),
        ("", "images_generations"),
        (None, "images_generations"),
        ("unknown_xyz", "images_generations"),
    ],
)
def test_infer_api_type_from_provider(provider, expected):
    """infer_api_type_from_provider 根据 provider 名推断 image api_type。"""
    from miniUnicorn.agent.tools.image_generation.config import infer_api_type_from_provider

    assert infer_api_type_from_provider(provider) == expected


@pytest.mark.parametrize(
    "provider,expected",
    [
        ("zhipu", "url"),
        ("stepfun", "url"),
        ("minimax", "url"),
        ("openai", "b64_json"),
        ("aihubmix", "b64_json"),
        ("gemini", "b64_json"),
        ("ollama", "b64_json"),
        ("custom", "b64_json"),
        ("dashscope", "b64_json"),
        ("openrouter", "b64_json"),
        ("auto", "b64_json"),
        ("", "b64_json"),
        (None, "b64_json"),
        ("unknown_xyz", "b64_json"),
    ],
)
def test_infer_response_format_from_provider(provider, expected):
    """infer_response_format_from_provider 根据 provider 名推断 image response_format。"""
    from miniUnicorn.agent.tools.image_generation.config import (
        infer_response_format_from_provider,
    )

    assert infer_response_format_from_provider(provider) == expected


# -----------------------------
# 2. 路径守卫
# -----------------------------


def _write_png(path: Path) -> None:
    path.write_bytes(_PNG_BYTES)


def test_path_guard_workspace_image_ok():
    from miniUnicorn.agent.tools.image_generation.path_guard import resolve_allowed_image_path

    with tempfile.TemporaryDirectory() as ws_tmp, tempfile.TemporaryDirectory() as media_tmp:
        ws = Path(ws_tmp)
        media = Path(media_tmp)
        img = ws / "test.png"
        _write_png(img)
        resolved = resolve_allowed_image_path(str(img), workspace=ws, media_root=media)
        assert resolved == img.resolve()


def test_path_guard_media_image_ok():
    from miniUnicorn.agent.tools.image_generation.path_guard import resolve_allowed_image_path

    with tempfile.TemporaryDirectory() as ws_tmp, tempfile.TemporaryDirectory() as media_tmp:
        ws = Path(ws_tmp)
        media = Path(media_tmp)
        img = media / "uploaded.png"
        _write_png(img)
        resolved = resolve_allowed_image_path(str(img), workspace=ws, media_root=media)
        assert resolved == img.resolve()


def test_path_guard_outside_rejected():
    from miniUnicorn.agent.tools.image_generation.path_guard import (
        ReferenceImageError,
        resolve_allowed_image_path,
    )

    with tempfile.TemporaryDirectory() as ws_tmp, tempfile.TemporaryDirectory() as media_tmp, \
            tempfile.TemporaryDirectory() as outside_tmp:
        ws = Path(ws_tmp)
        media = Path(media_tmp)
        outside = Path(outside_tmp) / "outside.png"
        _write_png(outside)
        with pytest.raises(ReferenceImageError, match="must be inside"):
            resolve_allowed_image_path(str(outside), workspace=ws, media_root=media)


def test_path_guard_traversal_rejected():
    from miniUnicorn.agent.tools.image_generation.path_guard import (
        ReferenceImageError,
        resolve_allowed_image_path,
    )

    with tempfile.TemporaryDirectory() as ws_tmp, tempfile.TemporaryDirectory() as media_tmp:
        ws = Path(ws_tmp)
        media = Path(media_tmp)
        # 构造一个 workspace 内不存在的子目录用于 ../ 跳出
        with pytest.raises(ReferenceImageError):
            resolve_allowed_image_path(
                str(ws / ".." / "etc" / "passwd"), workspace=ws, media_root=media
            )


def test_path_guard_mime_forgery_rejected():
    """扩展名是 .png 但内容非图片应被拒绝。"""
    from miniUnicorn.agent.tools.image_generation.path_guard import (
        ReferenceImageError,
        resolve_allowed_image_path,
    )

    with tempfile.TemporaryDirectory() as ws_tmp, tempfile.TemporaryDirectory() as media_tmp:
        ws = Path(ws_tmp)
        media = Path(media_tmp)
        fake = ws / "fake.png"
        fake.write_bytes(b"this is not an image, just text")
        with pytest.raises(ReferenceImageError, match="magic bytes mismatch"):
            resolve_allowed_image_path(str(fake), workspace=ws, media_root=media)


def test_path_guard_nonexistent_rejected():
    from miniUnicorn.agent.tools.image_generation.path_guard import (
        ReferenceImageError,
        resolve_allowed_image_path,
    )

    with tempfile.TemporaryDirectory() as ws_tmp, tempfile.TemporaryDirectory() as media_tmp:
        ws = Path(ws_tmp)
        media = Path(media_tmp)
        with pytest.raises(ReferenceImageError, match="not found"):
            resolve_allowed_image_path(str(ws / "nope.png"), workspace=ws, media_root=media)


def test_path_guard_unsupported_extension_rejected():
    from miniUnicorn.agent.tools.image_generation.path_guard import (
        ReferenceImageError,
        resolve_allowed_image_path,
    )

    with tempfile.TemporaryDirectory() as ws_tmp, tempfile.TemporaryDirectory() as media_tmp:
        ws = Path(ws_tmp)
        media = Path(media_tmp)
        bad = ws / "image.txt"
        bad.write_bytes(b"text content")
        with pytest.raises(ReferenceImageError, match="unsupported reference image extension"):
            resolve_allowed_image_path(str(bad), workspace=ws, media_root=media)


# -----------------------------
# 3. 协议适配器响应解析 (mock httpx)
# -----------------------------


def _mock_response(status_code: int = 200, json_data: dict | None = None, text: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text or (json.dumps(json_data) if json_data else "")
    resp.json = MagicMock(return_value=json_data or {})
    resp.content = (json_data or {}).get("__content__", b"")
    resp.headers = {"content-type": "image/png"}
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
    return resp


@pytest.mark.asyncio
async def test_images_generations_adapter_b64_json():
    """images_generations 协议: 解析 data[].b64_json 字段。"""
    from miniUnicorn.agent.tools.image_generation.config import ImageGenerationProviderConfig
    from miniUnicorn.agent.tools.image_generation.providers.images_generations import (
        ImagesGenerationsAdapter,
    )

    b64 = base64.b64encode(_PNG_BYTES).decode()
    fake_resp = _mock_response(200, {"data": [{"b64_json": b64}]})

    fake_client = AsyncMock()
    fake_client.post = AsyncMock(return_value=fake_resp)
    fake_client.aclose = AsyncMock()

    adapter = ImagesGenerationsAdapter()
    cfg = ImageGenerationProviderConfig(api_key="k", api_type="images_generations", response_format="b64_json")
    result = await adapter.generate(
        prompt="a cat",
        model="dall-e-3",
        provider_config=cfg,
        reference_images=None,
        aspect_ratio="1:1",
        image_size=None,
        http_client=fake_client,
    )
    assert len(result.images) == 1
    assert result.images[0].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_images_generations_adapter_url_response():
    """images_generations 协议 response_format=url: 自动下载转 data URL。"""
    from miniUnicorn.agent.tools.image_generation.config import ImageGenerationProviderConfig
    from miniUnicorn.agent.tools.image_generation.providers.images_generations import (
        ImagesGenerationsAdapter,
    )

    fake_resp = _mock_response(200, {"data": [{"url": "https://example.com/img.png"}]})

    # mock 下载
    fake_download_resp = MagicMock()
    fake_download_resp.status_code = 200
    fake_download_resp.content = _PNG_BYTES
    fake_download_resp.headers = {"content-type": "image/png"}
    fake_download_resp.raise_for_status = MagicMock()

    fake_client = AsyncMock()
    fake_client.post = AsyncMock(return_value=fake_resp)
    fake_client.get = AsyncMock(return_value=fake_download_resp)
    fake_client.aclose = AsyncMock()

    adapter = ImagesGenerationsAdapter()
    cfg = ImageGenerationProviderConfig(api_key="k", api_type="images_generations", response_format="url")
    result = await adapter.generate(
        prompt="a cat",
        model="dall-e-3",
        provider_config=cfg,
        reference_images=None,
        aspect_ratio="1:1",
        image_size=None,
        http_client=fake_client,
    )
    assert len(result.images) == 1
    assert result.images[0].startswith("data:image/png;base64,")
    fake_client.get.assert_awaited()


@pytest.mark.asyncio
async def test_images_edits_uploads_image_only(tmp_path):
    """images_generations /images/edits: 上传 image 文件, 不上传 mask。"""
    from miniUnicorn.agent.tools.image_generation.config import ImageGenerationProviderConfig
    from miniUnicorn.agent.tools.image_generation.providers.images_generations import (
        ImagesGenerationsAdapter,
    )

    ref_img = tmp_path / "ref.png"
    _write_png(ref_img)

    b64 = base64.b64encode(_PNG_BYTES).decode()
    fake_resp = _mock_response(200, {"data": [{"b64_json": b64}]})
    fake_client = AsyncMock()
    fake_client.post = AsyncMock(return_value=fake_resp)
    fake_client.aclose = AsyncMock()

    adapter = ImagesGenerationsAdapter()
    cfg = ImageGenerationProviderConfig(
        api_key="k", api_type="images_generations", response_format="b64_json",
    )
    result = await adapter.generate(
        prompt="edit", model="gpt-image-1", provider_config=cfg,
        reference_images=[ref_img],
        aspect_ratio="1:1", image_size=None, http_client=fake_client,
    )
    assert len(result.images) == 1
    # 验证请求 URL 走的是 /images/edits
    post_url = str(fake_client.post.call_args.args[0])
    assert post_url.endswith("/images/edits")
    # 验证 files 字典只含 image, 不含 mask
    files = fake_client.post.call_args.kwargs.get("files", {})
    assert "image" in files
    assert "mask" not in files


@pytest.mark.asyncio
async def test_chat_completions_adapter_parses_images():
    """chat_completions 协议: 解析 message.images[].image_url.url 字段。"""
    from miniUnicorn.agent.tools.image_generation.config import ImageGenerationProviderConfig
    from miniUnicorn.agent.tools.image_generation.providers.chat_completions import (
        ChatCompletionsAdapter,
    )

    fake_resp = _mock_response(200, {
        "choices": [{
            "message": {
                "images": [{"image_url": {"url": _PNG_DATA_URL}}],
                "content": "here is your image",
            }
        }]
    })

    fake_client = AsyncMock()
    fake_client.post = AsyncMock(return_value=fake_resp)
    fake_client.aclose = AsyncMock()

    adapter = ChatCompletionsAdapter()
    cfg = ImageGenerationProviderConfig(api_key="k", api_type="chat_completions")
    result = await adapter.generate(
        prompt="a cat",
        model="openai/gpt-5.4-image-2",
        provider_config=cfg,
        reference_images=None,
        aspect_ratio="16:9",
        image_size=None,
        http_client=fake_client,
    )
    assert len(result.images) == 1
    assert result.images[0] == _PNG_DATA_URL
    assert "here is your image" in result.content

    # 验证请求体包含 modalities + image_config
    call_body = fake_client.post.call_args.kwargs["json"]
    assert call_body["modalities"] == ["image", "text"]
    assert call_body["image_config"]["aspect_ratio"] == "16:9"


@pytest.mark.asyncio
async def test_chat_completions_adapter_with_reference_images():
    """chat_completions 协议: 参考图以 image_url 形式拼入 messages content。"""
    from miniUnicorn.agent.tools.image_generation.config import ImageGenerationProviderConfig
    from miniUnicorn.agent.tools.image_generation.providers.chat_completions import (
        ChatCompletionsAdapter,
    )

    with tempfile.TemporaryDirectory() as ws_tmp:
        ws = Path(ws_tmp)
        ref_img = ws / "ref.png"
        _write_png(ref_img)

        fake_resp = _mock_response(200, {
            "choices": [{"message": {"images": [{"image_url": {"url": _PNG_DATA_URL}}]}}]
        })
        fake_client = AsyncMock()
        fake_client.post = AsyncMock(return_value=fake_resp)
        fake_client.aclose = AsyncMock()

        adapter = ChatCompletionsAdapter()
        cfg = ImageGenerationProviderConfig(api_key="k", api_type="chat_completions")
        result = await adapter.generate(
            prompt="make it warmer",
            model="m",
            provider_config=cfg,
            reference_images=[ref_img],
            aspect_ratio=None,
            image_size=None,
            http_client=fake_client,
        )
        assert len(result.images) == 1

        # 验证 messages content 是多模态数组 (image_url + text)
        call_body = fake_client.post.call_args.kwargs["json"]
        content = call_body["messages"][0]["content"]
        assert isinstance(content, list)
        assert content[0]["type"] == "image_url"
        assert content[-1]["type"] == "text"
        assert content[-1]["text"] == "make it warmer"


@pytest.mark.asyncio
async def test_dashscope_adapter_parses_url_response():
    """dashscope_multimodal 协议: 解析 output.choices[].message.content[].image URL 并下载。"""
    from miniUnicorn.agent.tools.image_generation.config import ImageGenerationProviderConfig
    from miniUnicorn.agent.tools.image_generation.providers.dashscope_multimodal import (
        DashscopeMultimodalAdapter,
    )

    fake_resp = _mock_response(200, {
        "output": {
            "choices": [{
                "message": {
                    "content": [{"image": "https://dashscope-result.example.com/x.png"}]
                }
            }]
        }
    })

    fake_download_resp = MagicMock()
    fake_download_resp.status_code = 200
    fake_download_resp.content = _PNG_BYTES
    fake_download_resp.headers = {"content-type": "image/png"}
    fake_download_resp.raise_for_status = MagicMock()

    fake_client = AsyncMock()
    fake_client.post = AsyncMock(return_value=fake_resp)
    fake_client.get = AsyncMock(return_value=fake_download_resp)
    fake_client.aclose = AsyncMock()

    adapter = DashscopeMultimodalAdapter()
    cfg = ImageGenerationProviderConfig(api_key="k", api_type="dashscope_multimodal")
    result = await adapter.generate(
        prompt="a cat",
        model="qwen-image-2.0-pro",
        provider_config=cfg,
        reference_images=None,
        aspect_ratio="1:1",
        image_size=None,
        http_client=fake_client,
    )
    assert len(result.images) == 1
    assert result.images[0].startswith("data:image/png;base64,")
    fake_client.get.assert_awaited()


@pytest.mark.asyncio
async def test_dashscope_adapter_error_response():
    """dashscope 错误响应: code/message 字段存在时应抛 ImageGenerationError。"""
    from miniUnicorn.agent.tools.image_generation.config import ImageGenerationProviderConfig
    from miniUnicorn.agent.tools.image_generation.providers.base import ImageGenerationError
    from miniUnicorn.agent.tools.image_generation.providers.dashscope_multimodal import (
        DashscopeMultimodalAdapter,
    )

    fake_resp = _mock_response(200, {
        "code": "InvalidApiKey",
        "message": "Invalid API-key",
        "request_id": "req-123",
    })

    fake_client = AsyncMock()
    fake_client.post = AsyncMock(return_value=fake_resp)
    fake_client.aclose = AsyncMock()

    adapter = DashscopeMultimodalAdapter()
    cfg = ImageGenerationProviderConfig(api_key="bad", api_type="dashscope_multimodal")
    with pytest.raises(ImageGenerationError, match="InvalidApiKey"):
        await adapter.generate(
            prompt="x", model="m", provider_config=cfg,
            reference_images=None, aspect_ratio=None, image_size=None,
            http_client=fake_client,
        )


# -----------------------------
# 4. 工具入口完整流程 (mock adapter + 真 artifacts 落盘到临时目录)
# -----------------------------


@pytest.fixture
def isolated_media(monkeypatch, tmp_path):
    """重定向 media_dir 到临时目录, 避免污染用户 home。"""
    media_root = tmp_path / "media"
    media_root.mkdir()
    # patch 所有内部调用 get_media_dir 的入口
    monkeypatch.setattr(
        "miniUnicorn.agent.tools.image_generation.tool.get_media_dir",
        lambda: media_root,
    )
    monkeypatch.setattr("miniUnicorn.utils.artifacts.get_media_dir", lambda: media_root)
    monkeypatch.setattr(
        "miniUnicorn.agent.tools.image_generation.path_guard.get_media_dir",
        lambda: media_root,
    )
    return media_root


@pytest.fixture
def image_tool(isolated_media):
    """构造一个 enabled 的 ImageGenerationTool, provider_config 显式注入, adapter 已被 mock。

    新的 preset 模式下, ImageGenerationTool 不再从 config.providers 读取凭证,
    而是在 create() 时通过 provider_snapshot_loader 拿到 preset 凭证并组装
    ImageGenerationProviderConfig 实例传入。这里直接构造 tool 实例,
    跳过 create() 流程以隔离单元测试。
    """
    from miniUnicorn.agent.tools.image_generation import ImageGenerationTool
    from miniUnicorn.agent.tools.image_generation.config import (
        ImageGenerationConfig,
        ImageGenerationProviderConfig,
    )
    from miniUnicorn.agent.tools.image_generation.providers.base import (
        GeneratedImageResponse,
        ImageGenerationAdapter,
    )

    class _StubAdapter(ImageGenerationAdapter):
        """记录每次调用参数的 stub adapter。"""

        def __init__(self) -> None:
            self.calls: list[dict] = []

        @classmethod
        def api_type(cls) -> str:
            return "stub"

        async def generate(self, **kwargs):
            self.calls.append(kwargs)
            return GeneratedImageResponse(images=[_PNG_DATA_URL])

    cfg = ImageGenerationConfig(
        enabled=True,
        preset="test_preset",
        api_type="images_generations",
        response_format="b64_json",
        default_aspect_ratio="1:1",
        default_image_size="1K",
        max_images_per_turn=4,
        save_dir="generated",
    )
    provider_cfg = ImageGenerationProviderConfig(
        api_key="k",
        api_base="https://example.com/v1",
        api_type="images_generations",
        response_format="b64_json",
    )

    tool = ImageGenerationTool(
        config=cfg,
        workspace=str(isolated_media.parent),
        provider_config=provider_cfg,
        model_id="test-model",
        preset_name="test_preset",
    )
    tool._adapter = _StubAdapter()
    return tool, cfg


@pytest.mark.asyncio
async def test_tool_text_to_image_single(image_tool):
    """文生图: 单张生成, 返回 artifact 元数据。"""
    tool, _ = image_tool
    result = await tool.execute(prompt="a cat")
    parsed = json.loads(result)
    assert "artifacts" in parsed
    assert len(parsed["artifacts"]) == 1
    art = parsed["artifacts"][0]
    assert art["prompt"] == "a cat"
    assert art["model"] == "test-model"
    assert art["provider"] == "test_preset"
    assert art["source_images"] == []
    assert art["id"].startswith("img_")
    assert Path(art["path"]).exists()
    # sidecar JSON 应存在
    sidecar = Path(art["path"]).with_suffix(".json")
    assert sidecar.exists()


@pytest.mark.asyncio
async def test_tool_count_multiple(image_tool):
    """count=3: 串行调用 adapter 3 次, 生成 3 张。"""
    tool, _ = image_tool
    result = await tool.execute(prompt="three cats", count=3)
    parsed = json.loads(result)
    assert len(parsed["artifacts"]) == 3
    # 3 个 artifact id 应不同
    ids = {a["id"] for a in parsed["artifacts"]}
    assert len(ids) == 3


@pytest.mark.asyncio
async def test_tool_count_over_limit(image_tool):
    """count 超过 maxImagesPerTurn: 返回 error, 不调用 adapter。"""
    tool, _ = image_tool
    result = await tool.execute(prompt="too many", count=99)
    parsed = json.loads(result)
    assert "error" in parsed
    assert "maxImagesPerTurn" in parsed["error"]


@pytest.mark.asyncio
async def test_tool_reference_image_closure(image_tool, isolated_media):
    """参考图闭环: 第一轮生成的 artifact path, 第二轮作为 reference_images 传入。"""
    tool, _ = image_tool
    # 第一轮: 文生图
    r1 = json.loads(await tool.execute(prompt="a cat"))
    prev_path = r1["artifacts"][0]["path"]
    assert Path(prev_path).exists()

    # 第二轮: 编辑
    r2 = json.loads(await tool.execute(prompt="add a hat", reference_images=[prev_path]))
    assert "artifacts" in r2, f"unexpected: {r2}"
    art2 = r2["artifacts"][0]
    assert art2["source_images"] == [prev_path]
    # adapter 应收到 reference_images 参数为 Path 列表
    last_call = tool._adapter.calls[-1]  # type: ignore[attr-defined]
    assert last_call["reference_images"] is not None
    assert len(last_call["reference_images"]) == 1
    assert Path(last_call["reference_images"][0]) == Path(prev_path)


@pytest.mark.asyncio
async def test_tool_reference_image_outside_rejected(image_tool):
    """参考图在 workspace/media 之外: 返回 error。"""
    tool, _ = image_tool
    with tempfile.TemporaryDirectory() as outside_tmp:
        outside = Path(outside_tmp) / "outside.png"
        _write_png(outside)
        result = await tool.execute(prompt="edit", reference_images=[str(outside)])
        parsed = json.loads(result)
        assert "error" in parsed
        assert "must be inside" in parsed["error"] or "reference image" in parsed["error"]


@pytest.mark.asyncio
async def test_tool_adapter_error_returns_error(image_tool):
    """adapter 抛 ImageGenerationError: 工具返回 error, 不崩溃。"""
    from miniUnicorn.agent.tools.image_generation.providers.base import (
        ImageGenerationError,
    )

    tool, _ = image_tool
    tool._adapter.generate = AsyncMock(side_effect=ImageGenerationError("API rate limited"))  # type: ignore[attr-defined]
    result = await tool.execute(prompt="error case")
    parsed = json.loads(result)
    assert "error" in parsed
    assert "API rate limited" in parsed["error"]


@pytest.mark.asyncio
async def test_tool_empty_response_returns_error(image_tool):
    """adapter 返回空 images: 工具返回 error。"""
    from miniUnicorn.agent.tools.image_generation.providers.base import (
        GeneratedImageResponse,
    )

    tool, _ = image_tool
    tool._adapter.generate = AsyncMock(return_value=GeneratedImageResponse(images=[]))  # type: ignore[attr-defined]
    result = await tool.execute(prompt="empty")
    parsed = json.loads(result)
    assert "error" in parsed


@pytest.mark.asyncio
async def test_tool_adapter_none_returns_error(isolated_media, monkeypatch):
    """未配置 adapter (provider_config 为 None): 返回 error 而不是崩溃。"""
    from miniUnicorn.agent.tools.image_generation import ImageGenerationTool
    from miniUnicorn.agent.tools.image_generation.config import ImageGenerationConfig

    cfg = ImageGenerationConfig(
        enabled=True,
        preset="p",
        api_type="images_generations",
        response_format="b64_json",
    )
    # provider_config=None 模拟 create() 时 preset snapshot 解析失败的情况
    tool = ImageGenerationTool(
        config=cfg,
        workspace=str(isolated_media.parent),
        provider_config=None,
        model_id="",
        preset_name="p",
    )
    result = await tool.execute(prompt="x")
    parsed = json.loads(result)
    assert "error" in parsed
    assert "not configured" in parsed["error"] or "adapter" in parsed["error"]


# -----------------------------
# 5. 工具 create() 流程 (mock provider_snapshot_loader)
# -----------------------------


def _make_fake_signature(
    *,
    api_key: str | None = "key123",
    api_base: str | None = "https://api.example.com/v1",
    model: str = "dall-e-3",
    extra_headers: dict[str, str] | None = None,
    extra_body: dict[str, Any] | None = None,
) -> SimpleNamespace:
    """构造一个假的 ProviderSignature (与 miniUnicorn.providers.factory.ProviderSignature 同字段)。"""
    return SimpleNamespace(
        model=model,
        provider="openai",
        provider_name="openai",
        api_key=api_key,
        api_base=api_base,
        extra_headers=extra_headers,
        extra_body=extra_body,
        api_type="images_generations",
        region=None,
        profile=None,
        max_tokens=None,
        temperature=None,
        reasoning_effort=None,
        context_window_tokens=None,
        fallbacks=(),
    )


def _make_fake_snapshot(*, signature=None, model="dall-e-3") -> SimpleNamespace:
    """构造一个假的 ProviderSnapshot。"""
    return SimpleNamespace(
        provider=MagicMock(),
        model=model,
        context_window_tokens=4096,
        signature=signature if signature is not None else _make_fake_signature(model=model),
    )


def test_tool_create_resolves_preset_credentials():
    """create() 通过 provider_snapshot_loader(preset_name=...) 解析 preset 拿凭证。"""
    from miniUnicorn.agent.tools.image_generation import ImageGenerationTool
    from miniUnicorn.agent.tools.image_generation.config import ImageGenerationConfig

    cfg = ImageGenerationConfig(
        enabled=True,
        preset="my_openai",
        api_type="images_generations",
        response_format="b64_json",
    )
    fake_snapshot = _make_fake_snapshot(
        signature=_make_fake_signature(
            api_key="sk-test",
            api_base="https://api.openai.com/v1",
            model="dall-e-3",
        ),
        model="dall-e-3",
    )
    captured_calls: list[dict] = []

    def _loader(preset_name=None):
        captured_calls.append({"preset_name": preset_name})
        return fake_snapshot

    ctx = SimpleNamespace(
        config=SimpleNamespace(image_generation=cfg),
        workspace="/tmp/test-workspace",
        provider_snapshot_loader=_loader,
    )

    tool = ImageGenerationTool.create(ctx)
    assert isinstance(tool, ImageGenerationTool)
    # loader 应被以 preset_name="my_openai" 调用
    assert captured_calls == [{"preset_name": "my_openai"}]
    # provider_config 应包含从 snapshot.signature 提取的凭证
    assert tool._provider_config is not None
    assert tool._provider_config.api_key == "sk-test"
    assert tool._provider_config.api_base == "https://api.openai.com/v1"
    assert tool._provider_config.api_type == "images_generations"
    assert tool._provider_config.response_format == "b64_json"
    assert tool._model_id == "dall-e-3"
    assert tool._preset_name == "my_openai"
    # adapter 应已构建 (api_type=images_generations 在注册表中)
    assert tool._adapter is not None


def test_tool_create_default_preset_passes_default_to_loader():
    """preset="default" 时, create() 仍以 preset_name="default" 调用 loader。"""
    from miniUnicorn.agent.tools.image_generation import ImageGenerationTool
    from miniUnicorn.agent.tools.image_generation.config import ImageGenerationConfig

    cfg = ImageGenerationConfig(
        enabled=True,
        preset="default",
        api_type="images_generations",
        response_format="b64_json",
    )
    fake_snapshot = _make_fake_snapshot(
        signature=_make_fake_signature(model="gpt-4o"),
        model="gpt-4o",
    )
    captured_calls: list[dict] = []

    def _loader(preset_name=None):
        captured_calls.append({"preset_name": preset_name})
        return fake_snapshot

    ctx = SimpleNamespace(
        config=SimpleNamespace(image_generation=cfg),
        workspace="/tmp/ws",
        provider_snapshot_loader=_loader,
    )

    tool = ImageGenerationTool.create(ctx)
    # loader 被调用一次
    assert len(captured_calls) == 1
    # preset="default" 时仍传入 preset_name="default" (cfg.preset or "default" 兜底)
    assert captured_calls[0]["preset_name"] == "default"
    assert tool._model_id == "gpt-4o"
    assert tool._preset_name == "default"


def test_tool_create_loader_failure_returns_tool_without_provider():
    """provider_snapshot_loader 抛异常时, create() 仍返回 tool 实例但 provider_config=None。"""
    from miniUnicorn.agent.tools.image_generation import ImageGenerationTool
    from miniUnicorn.agent.tools.image_generation.config import ImageGenerationConfig

    cfg = ImageGenerationConfig(
        enabled=True,
        preset="broken",
        api_type="images_generations",
        response_format="b64_json",
    )

    def _loader(preset_name=None):
        raise RuntimeError("preset not found")

    ctx = SimpleNamespace(
        config=SimpleNamespace(image_generation=cfg),
        workspace="/tmp/ws",
        provider_snapshot_loader=_loader,
    )

    # create() 应吞掉异常并返回 tool (provider_config=None, 调用时再返回 error)
    tool = ImageGenerationTool.create(ctx)
    assert isinstance(tool, ImageGenerationTool)
    assert tool._provider_config is None
    assert tool._adapter is None
    assert tool._preset_name == "broken"


def test_tool_create_legacy_tuple_signature():
    """provider_snapshot_loader 返回短元组格式 signature 时, create() 应安全跳过凭证提取。

    signature 是元组 (没有 .api_key 属性), create() 走 hasattr(signature, "api_key")
    False 分支, api_key/api_base 等保持 None, 但仍构造 provider_config 实例
    (api_type/response_format 来自 cfg, 其余为 None)。
    """
    from miniUnicorn.agent.tools.image_generation import ImageGenerationTool
    from miniUnicorn.agent.tools.image_generation.config import ImageGenerationConfig

    cfg = ImageGenerationConfig(
        enabled=True,
        preset="legacy",
        api_type="images_generations",
        response_format="b64_json",
    )
    # 模拟 ProviderSnapshot.signature 是短元组 (model_preset preset 快照格式)
    fake_snapshot = SimpleNamespace(
        provider=MagicMock(),
        model="some-model",
        context_window_tokens=4096,
        signature=("model_preset", "legacy", '{"model": "some-model"}'),
    )

    ctx = SimpleNamespace(
        config=SimpleNamespace(image_generation=cfg),
        workspace="/tmp/ws",
        provider_snapshot_loader=lambda preset_name=None: fake_snapshot,
    )

    tool = ImageGenerationTool.create(ctx)
    # signature 没有 .api_key 属性 -> api_key/api_base 为 None, 但 provider_config 仍被构造
    assert tool._provider_config is not None
    assert tool._provider_config.api_key is None
    assert tool._provider_config.api_base is None
    # api_type/response_format 仍从 cfg 取
    assert tool._provider_config.api_type == "images_generations"
    assert tool._provider_config.response_format == "b64_json"
    # model_id 仍应从 snapshot.model 提取
    assert tool._model_id == "some-model"
    assert tool._preset_name == "legacy"


@pytest.mark.parametrize(
    "provider_name,provider,expected_api_type,expected_response_format",
    [
        ("dashscope", None, "dashscope_multimodal", "b64_json"),
        (None, "dashscope", "dashscope_multimodal", "b64_json"),
        ("openrouter", None, "chat_completions", "b64_json"),
        (None, "openrouter", "chat_completions", "b64_json"),
        ("openai", None, "images_generations", "b64_json"),
        ("zhipu", None, "images_generations", "url"),
        ("stepfun", None, "images_generations", "url"),
        ("minimax", None, "images_generations", "url"),
        (None, "auto", "images_generations", "b64_json"),
        (None, None, "images_generations", "b64_json"),
        ("unknown_provider", None, "images_generations", "b64_json"),
    ],
)
def test_tool_create_infers_api_type_from_provider(
    provider_name, provider, expected_api_type, expected_response_format
):
    """create() 根据 preset signature 的 provider 自动推断 api_type 和 response_format,
    不再用 cfg 中的值。

    api_type:
    - dashscope → dashscope_multimodal
    - openrouter → chat_completions
    - 其他 (含 None / "auto" / 未知) → images_generations

    response_format (仅对 images_generations 协议生效):
    - zhipu / stepfun / minimax → url
    - 其他 → b64_json

    provider_name 优先于 provider
    """
    from miniUnicorn.agent.tools.image_generation import ImageGenerationTool
    from miniUnicorn.agent.tools.image_generation.config import ImageGenerationConfig

    # cfg 故意配成与推断结果不同的值, 验证运行时确实用推断值
    cfg_api_type = (
        "images_generations" if expected_api_type != "images_generations" else "chat_completions"
    )
    cfg_response_format = (
        "b64_json" if expected_response_format != "b64_json" else "url"
    )
    cfg = ImageGenerationConfig(
        enabled=True,
        preset="test",
        api_type=cfg_api_type,  # type: ignore[arg-type]
        response_format=cfg_response_format,  # type: ignore[arg-type]
    )
    signature = _make_fake_signature(model="test-model")
    # 覆盖 provider 字段
    signature.provider_name = provider_name
    signature.provider = provider
    fake_snapshot = _make_fake_snapshot(signature=signature, model="test-model")

    ctx = SimpleNamespace(
        config=SimpleNamespace(image_generation=cfg),
        workspace="/tmp/ws",
        provider_snapshot_loader=lambda preset_name=None: fake_snapshot,
    )

    tool = ImageGenerationTool.create(ctx)
    assert tool._provider_config is not None
    # api_type 和 response_format 都应为推断值, 而非 cfg 中的值
    assert tool._provider_config.api_type == expected_api_type
    assert tool._provider_config.response_format == expected_response_format


# -----------------------------
# 6. 工具自动发现 + schema
# -----------------------------


def test_tool_loader_discovers_image_generation():
    """ToolLoader 应自动发现 ImageGenerationTool 类。"""
    from miniUnicorn.agent.tools import ToolLoader
    from miniUnicorn.agent.tools.image_generation import ImageGenerationTool

    loader = ToolLoader()
    discovered = loader.discover()
    cls_set = set(discovered)
    assert ImageGenerationTool in cls_set, (
        f"ImageGenerationTool not discovered by ToolLoader; found: {discovered}"
    )


def test_tool_schema_has_required_prompt_only():
    """工具 schema 的 required 字段应只有 prompt。"""
    from miniUnicorn.agent.tools.image_generation import ImageGenerationTool
    from miniUnicorn.agent.tools.image_generation.config import (
        ImageGenerationConfig,
        ImageGenerationProviderConfig,
    )

    cfg = ImageGenerationConfig(
        enabled=True,
        preset="p",
        api_type="images_generations",
        response_format="b64_json",
    )
    provider_cfg = ImageGenerationProviderConfig(api_key="k", api_type="images_generations")
    tool = ImageGenerationTool(
        config=cfg,
        workspace="/tmp",
        provider_config=provider_cfg,
        model_id="m",
        preset_name="p",
    )
    schema = tool.to_schema()
    # OpenAI function schema 包装
    fn = schema.get("function", schema)
    params = fn.get("parameters", {})
    assert params.get("required") == ["prompt"]
    # 应包含所有 5 个参数 (prompt / reference_images / aspect_ratio / image_size / count)
    prop_names = set(params.get("properties", {}).keys())
    assert prop_names == {"prompt", "reference_images", "aspect_ratio", "image_size", "count"}
