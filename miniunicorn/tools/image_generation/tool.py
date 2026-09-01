"""ImageGenerationTool — generate_image 工具入口。

工具名: generate_image
配置 key: image_generation (挂在 ToolsConfig.image_generation)

执行流程:
1. 解析 provider 配置 + 协议适配器 (api_type 由 preset 的 provider 自动推断)
2. 上限校验: count <= maxImagesPerTurn
3. 参考图路径安全校验 (path_guard: workspace/media 边界 + MIME 检测)
4. 串行调用 adapter.generate() (count>1 时多次调用, break 防超取)
5. 落盘到 media/generated/YYYY-MM-DD/img_<uuid>.<ext> + sidecar JSON (复用 utils/artifacts)
6. 返回 artifact 元数据 JSON 给 LLM (不含 base64, 避免 context 爆炸)

LLM 拿到 artifact path 后:
- 调用 message 工具的 media 参数把图片发给用户
- 后续编辑把 path 作为 reference_images 传回 generate_image

凭证来源 (参考 deep_research/tool.py 的复用模式):
- tool.create() 时通过 ctx.provider_snapshot_loader(preset_name=ig.preset)
  拿到 ProviderSnapshot, 从 snapshot.signature 提取 api_key/api_base/model/extra_headers/extra_body
- api_type 根据 signature 的 provider 自动推断 (infer_api_type_from_provider):
  dashscope → dashscope_multimodal; openrouter → chat_completions; 其他 → images_generations
- response_format 也根据 signature 的 provider 自动推断 (infer_response_format_from_provider):
  zhipu/stepfun/minimax → url; 其他 → b64_json (仅对 images_generations 协议生效)
- 组合成 ImageGenerationProviderConfig 实例
- 不再从 config.json 的 image_generation.providers 读取凭证
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger

from miniunicorn.agent.safety_policy import RiskLevel
from miniunicorn.config.paths import get_media_dir
from miniunicorn.tools.base import Tool, tool_parameters
from miniunicorn.tools.image_generation.config import (
    ImageGenerationConfig,
    ImageGenerationProviderConfig,
    infer_api_type_from_provider,
    infer_response_format_from_provider,
)
from miniunicorn.tools.image_generation.path_guard import (
    ReferenceImageError,
    resolve_allowed_image_path,
)
from miniunicorn.tools.image_generation.providers import get_adapter
from miniunicorn.tools.image_generation.providers._http import build_async_client
from miniunicorn.tools.image_generation.providers.base import (
    ImageGenerationAdapter,
    ImageGenerationError,
)
from miniunicorn.tools.schema import (
    ArraySchema,
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)
from miniunicorn.utils.artifacts import (
    ArtifactError,
    generated_image_tool_result,
    store_generated_image_artifact,
)


@tool_parameters(
    tool_parameters_schema(
        prompt=StringSchema(
            "Detailed image generation or edit prompt. Describe subject, scene, "
            "composition, style, mood, lighting, color palette, and any text that must "
            "appear in the image. For edits, describe what should change and what must "
            "stay fixed (e.g. 'keep the same character, change the background to sunset').",
            min_length=1,
        ),
        reference_images=ArraySchema(
            StringSchema(
                "Local path of an existing image artifact (returned from a previous "
                "generate_image call) or a user-uploaded image. Use the most recent "
                "artifact path for iterative edits like 'make it warmer', 'change the "
                "background', or 'try another version'."
            ),
            description=(
                "Optional local image paths for edit/fusion. Pass prior artifact paths "
                "to iteratively refine an image across turns."
            ),
        ),
        aspect_ratio=StringSchema(
            "Optional output aspect ratio. Examples: 1:1, 16:9, 9:16, 4:3, 3:4. "
            "When omitted, the configured defaultAspectRatio is used."
        ),
        image_size=StringSchema(
            "Optional output size hint. Examples: 1K, 2K, 4K, or explicit dimensions "
            "like 1024x1024 (images_generations) or 1024*1024 (dashscope). "
            "When omitted, the configured defaultImageSize is used."
        ),
        count=IntegerSchema(
            description="Number of images to generate in this single tool call. "
            "Each image is produced by a separate provider call. "
            "Must be between 1 and maxImagesPerTurn (default 4).",
            minimum=1,
            maximum=8,
        ),
        required=["prompt"],
    )
)
class ImageGenerationTool(Tool):
    """Generate images from text prompts or edit existing images.

    Returns structured artifact metadata (id, path, mime, prompt, model,
    source_images, created_at). Pass returned artifact paths to the ``message``
    tool's ``media`` parameter to deliver images to the user. For follow-up
    edits, pass prior artifact paths back as ``reference_images``.

    Provider credentials are sourced from the model preset referenced by
    ``tools.imageGeneration.preset`` (same mechanism as Plan & Execute's
    ``planner_model``). This tool does not accept provider/model as parameters.
    """

    config_key = "image_generation"
    _scopes = {"core", "subagent"}  # 允许子代理 (如 deep_research) 调用

    name = "generate_image"
    description = (
        "Generate images from text prompts, or edit existing images by passing their "
        "artifact paths as reference_images. Returns artifact metadata (id, path, "
        "mime, prompt, model, source_images, created_at). Use the message tool with "
        "the media parameter to deliver images to the user. For iterative edits, "
        "pass the most recent artifact path back as reference_images. Provider and "
        "model are configured in tools.imageGeneration (preset field references a "
        "model_preset); this tool does not accept them as parameters."
    )

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        # ctx.config 是 ToolsConfig; image_generation 字段默认存在 (enabled=False)
        cfg = getattr(ctx.config, "image_generation", None)
        return cfg is not None and cfg.enabled

    @classmethod
    def create(cls, ctx: Any) -> "ImageGenerationTool":
        cfg = getattr(ctx.config, "image_generation", None)
        if cfg is None:
            from miniunicorn.tools.image_generation.config import ImageGenerationConfig

            cfg = ImageGenerationConfig()

        # 通过 provider_snapshot_loader 解析 preset 拿凭证 (与 deep_research 同模式)
        provider_cfg: ImageGenerationProviderConfig | None = None
        model_id: str = ""
        preset_name = cfg.preset or "default"

        loader = getattr(ctx, "provider_snapshot_loader", None)
        if callable(loader):
            try:
                snapshot = loader(preset_name=preset_name) if preset_name else loader()
            except Exception as exc:
                logger.warning(
                    "image_generation: failed to load preset snapshot for '{}': {}",
                    preset_name,
                    exc,
                )
                snapshot = None
            if snapshot is not None:
                signature = getattr(snapshot, "signature", None)
                # ProviderSignature 是 dataclass, 有具名字段; 短元组则是 preset 快照格式
                api_key = None
                api_base = None
                extra_headers = None
                extra_body = None
                # provider 用于自动推断 image api_type:
                # 优先用 provider_name (解析后的具体名), 后备用 provider (用户原始配置)
                preset_provider: str | None = None
                if signature is not None and hasattr(signature, "api_key"):
                    api_key = getattr(signature, "api_key", None)
                    api_base = getattr(signature, "api_base", None)
                    extra_headers = getattr(signature, "extra_headers", None)
                    extra_body = getattr(signature, "extra_body", None)
                    preset_provider = getattr(signature, "provider_name", None) or getattr(
                        signature, "provider", None
                    )
                model_id = getattr(snapshot, "model", "") or ""
                # api_type / response_format 都由 preset 的 provider 自动推断, 不再用 cfg 中的值
                inferred_api_type = infer_api_type_from_provider(preset_provider)
                inferred_response_format = infer_response_format_from_provider(preset_provider)
                provider_cfg = ImageGenerationProviderConfig(
                    api_key=api_key,
                    api_base=api_base,
                    api_type=inferred_api_type,
                    response_format=inferred_response_format,
                    extra_headers=extra_headers,
                    extra_body=extra_body,
                )
                if (
                    inferred_api_type != cfg.api_type
                    or inferred_response_format != cfg.response_format
                ):
                    logger.info(
                        "image_generation: inferred from provider '{}': "
                        "api_type={} (config had {}), response_format={} (config had {})",
                        preset_provider,
                        inferred_api_type,
                        cfg.api_type,
                        inferred_response_format,
                        cfg.response_format,
                    )

        return cls(
            config=cfg,
            workspace=ctx.workspace,
            provider_config=provider_cfg,
            model_id=model_id,
            preset_name=preset_name,
        )

    def __init__(
        self,
        config: ImageGenerationConfig,
        workspace: str | Path,
        provider_config: ImageGenerationProviderConfig | None = None,
        model_id: str = "",
        preset_name: str = "default",
    ) -> None:
        self.config = config
        self.workspace = str(workspace)
        self._provider_config = provider_config
        self._model_id = model_id
        self._preset_name = preset_name
        # 延迟构建适配器: enabled 时才查注册表
        self._adapter: ImageGenerationAdapter | None = None
        if provider_config is not None:
            self._adapter = get_adapter(provider_config.api_type)
            if self._adapter is None:
                logger.warning(
                    "image_generation: no adapter registered for apiType '{}'; "
                    "tool will return error on first call",
                    provider_config.api_type,
                )

    @property
    def read_only(self) -> bool:
        return False

    @property
    def compactable(self) -> bool:
        # artifact 元数据较紧凑且对后续编辑闭环有引用价值, 不参与自动压缩
        return False

    @property
    def exclusive(self) -> bool:
        # 图片生成涉及外部 API + 落盘, 不与其他 side-effect 工具并发
        return True

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.MEDIUM

    @property
    def importance(self) -> float:
        # artifact path 对后续编辑闭环重要, 避免被 microcompact 丢弃
        return 0.9

    @property
    def cacheable(self) -> bool:
        # 每次生成都不同 (有随机性), 不缓存
        return False

    async def execute(
        self,
        prompt: str,
        reference_images: list[str] | None = None,
        aspect_ratio: str | None = None,
        image_size: str | None = None,
        count: int | None = None,
        **kwargs: Any,
    ) -> str:
        # 1. 适配器就绪校验
        if self._adapter is None or self._provider_config is None:
            return _error_result(
                "image generation provider/adapter not configured. "
                "Set tools.imageGeneration.enabled=true and ensure the referenced "
                "model_preset has valid api_key/api_base. "
                "Also check apiType (images_generations / chat_completions / dashscope_multimodal)."
            )

        # 2. count 上限校验
        requested = count or 1
        if requested < 1:
            return _error_result("count must be >= 1")
        if requested > self.config.max_images_per_turn:
            return _error_result(
                f"count={requested} exceeds maxImagesPerTurn={self.config.max_images_per_turn}. "
                f"Reduce count or ask the user to raise the limit in config."
            )

        # 3. 参考图路径安全校验
        try:
            refs = self._resolve_reference_images(reference_images or [])
        except ReferenceImageError as exc:
            return _error_result(f"reference image rejected: {exc}")

        # 4. 解析 provider 配置 (在 create() 时已组装, 这里直接用)
        provider_cfg = self._provider_config

        # 5. 串行调用 adapter.generate(); count>1 时多次调用
        client = build_async_client(timeout=provider_cfg.timeout or self._adapter.default_timeout)
        try:
            artifacts: list[dict[str, Any]] = []
            try:
                while len(artifacts) < requested:
                    resp = await self._adapter.generate(
                        prompt=prompt,
                        model=self._model_id,
                        provider_config=provider_cfg,
                        reference_images=refs or None,
                        aspect_ratio=aspect_ratio or self.config.default_aspect_ratio,
                        image_size=image_size or self.config.default_image_size,
                        http_client=client,
                    )
                    # 防御: provider 返回空 images 时不能无限重试, 直接 break 走 error 分支
                    if not resp.images:
                        logger.warning(
                            "image generation provider returned no images; breaking loop "
                            "(artifacts so far: {})",
                            len(artifacts),
                        )
                        break
                    for data_url in resp.images:
                        artifact = store_generated_image_artifact(
                            data_url,
                            prompt=prompt,
                            model=self._model_id,
                            source_images=[str(p) for p in refs],
                            save_dir=self.config.save_dir,
                            provider=self._preset_name,
                        )
                        artifacts.append(artifact)
                        if len(artifacts) >= requested:
                            break
            except ImageGenerationError as exc:
                # 已生成的 artifact 仍可返回给 LLM (部分成功)
                if artifacts:
                    logger.warning(
                        "image generation partially failed: {} artifacts saved before error: {}",
                        len(artifacts),
                        exc,
                    )
                    return generated_image_tool_result(artifacts)
                return _error_result(f"image generation failed: {exc}")
            except (ArtifactError, OSError) as exc:
                if artifacts:
                    logger.warning(
                        "artifact persistence partially failed: {} saved before error: {}",
                        len(artifacts),
                        exc,
                    )
                    return generated_image_tool_result(artifacts)
                return _error_result(f"failed to store generated image: {exc}")
        finally:
            await client.aclose()

        if not artifacts:
            return _error_result(
                "provider returned no images and no error was raised. "
                "[Analyze the situation and try a different prompt or provider.]"
            )

        return generated_image_tool_result(artifacts)

    def _resolve_reference_images(self, paths: list[str]) -> list[Path]:
        """对 LLM 传入的参考图路径列表做安全校验, 返回绝对路径列表。"""
        resolved: list[Path] = []
        for p in paths:
            if not isinstance(p, str) or not p:
                continue
            resolved.append(
                resolve_allowed_image_path(p, workspace=self.workspace, media_root=get_media_dir())
            )
        return resolved


def _error_result(message: str) -> str:
    """构造错误返回字符串 (统一格式, 末尾追加 LLM 自我修正提示)。"""
    return json.dumps(
        {
            "error": message,
            "next_step": (
                "Analyze the error above and try a different approach. "
                "Common fixes: check provider configuration, reduce count, "
                "use a supported aspect ratio, or verify reference image paths."
            ),
        },
        ensure_ascii=False,
    )
