"""Create LLM providers from config."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from miniunicorn.config.schema import Config, InlineFallbackConfig, ModelPresetConfig
from miniunicorn.providers.base import LLMProvider
from miniunicorn.providers.fallback_provider import FallbackProvider
from miniunicorn.providers.registry import find_by_name


@dataclass(frozen=True)
class ProviderSignature:
    """捕获影响活跃 provider 链的全部配置字段。

    用 frozen dataclass 替代原先 15+ 元素的位置型大元组:
    - 任何字段新增/删除不会再因下标错位而破坏所有调用方;
    - 调用方通过 ``sig.model``、``sig.api_key`` 等具名访问替代 ``sig[0]``、``sig[3]``;
    - frozen=True 使实例可哈希且可在等值比较中等同于值对象。
    """

    model: str
    provider: str | None
    provider_name: str | None
    api_key: str | None
    api_base: str | None
    extra_headers: dict[str, str] | None
    extra_body: dict[str, Any] | None
    api_type: str
    # region / profile 当前未在 ProviderConfig 显式声明,可能通过 extra="allow" 注入,
    # 因此沿用 getattr 兜底语义并保留为可选字段。
    region: str | None
    profile: str | None
    max_tokens: int | None
    temperature: float | None
    reasoning_effort: str | None
    context_window_tokens: int | None
    # 嵌套的 fallback 签名;每个 fallback 自身不再含 fallbacks(避免无限嵌套),
    # 因此 fallbacks 字段为空 tuple。
    fallbacks: tuple["ProviderSignature", ...]


@dataclass(frozen=True)
class ProviderSnapshot:
    provider: LLMProvider
    model: str
    # None means "auto-detect from model name"; resolved to a concrete int by
    # the agent loop (apply_provider_snapshot) or build_provider_snapshot.
    context_window_tokens: int | None
    # 普通构建路径产出 ProviderSignature;但 preset 快照仍使用短元组
    # ("model_preset", name, preset.model_dump_json()) 作为标识,因此联合类型。
    signature: ProviderSignature | tuple[object, ...]


def _resolve_model_preset(
    config: Config,
    *,
    preset_name: str | None = None,
    preset: ModelPresetConfig | None = None,
) -> ModelPresetConfig:
    return preset if preset is not None else config.resolve_preset(preset_name)


def _make_provider_core(
    config: Config,
    *,
    preset_name: str | None = None,
    preset: ModelPresetConfig | None = None,
    model: str | None = None,
) -> LLMProvider:
    """Create a plain LLM provider without failover wrapping."""
    resolved = _resolve_model_preset(config, preset_name=preset_name, preset=preset)
    model = model or resolved.model
    provider_name = config.get_provider_name(model, preset=resolved)
    p = config.get_provider(model, preset=resolved)
    spec = find_by_name(provider_name) if provider_name else None

    # API key may be configured later via WebUI (Settings → BYOK); no warning at startup.
    _ = spec  # kept for future startup-time diagnostics

    # Only openai_compat backend (DeepSeek + custom)
    from miniunicorn.providers.openai_compat_provider import OpenAICompatProvider

    provider = OpenAICompatProvider(
        api_key=p.api_key if p else None,
        api_base=config.get_api_base(model, preset=resolved),
        default_model=model,
        extra_headers=p.extra_headers if p else None,
        spec=spec,
        extra_body=p.extra_body if p else None,
        api_type=p.api_type if p and provider_name == "openai" else "auto",
    )

    provider.generation = resolved.to_generation_settings()
    return provider


def _inline_fallback_preset(
    primary: ModelPresetConfig,
    fallback: InlineFallbackConfig,
) -> ModelPresetConfig:
    return ModelPresetConfig(
        model=fallback.model,
        provider=fallback.provider,
        max_tokens=fallback.max_tokens if fallback.max_tokens is not None else primary.max_tokens,
        context_window_tokens=(
            fallback.context_window_tokens
            if fallback.context_window_tokens is not None
            else primary.context_window_tokens
        ),
        temperature=(
            fallback.temperature if fallback.temperature is not None else primary.temperature
        ),
        reasoning_effort=fallback.reasoning_effort,
    )


def _resolve_fallback_presets(config: Config, primary: ModelPresetConfig) -> list[ModelPresetConfig]:
    presets: list[ModelPresetConfig] = []
    for fallback in config.agents.defaults.fallback_models:
        if isinstance(fallback, str):
            presets.append(config.model_presets[fallback])
        else:
            presets.append(_inline_fallback_preset(primary, fallback))
    return presets


def make_provider(
    config: Config,
    *,
    preset_name: str | None = None,
    preset: ModelPresetConfig | None = None,
    model: str | None = None,
) -> LLMProvider:
    """Create the LLM provider implied by config.

    When *model* is given, it overrides the resolved/preset model — used by
    the failover path to create providers for fallback models.
    """
    resolved = _resolve_model_preset(config, preset_name=preset_name, preset=preset)
    provider = _make_provider_core(config, preset_name=preset_name, preset=preset, model=model)
    fallback_presets = _resolve_fallback_presets(config, resolved)

    if fallback_presets:
        provider = FallbackProvider(
            primary=provider,
            fallback_presets=fallback_presets,
            provider_factory=lambda fb: _make_provider_core(
                config, preset_name=preset_name, preset=fb
            ),
        )

    return provider


def provider_signature(
    config: Config,
    *,
    preset_name: str | None = None,
    preset: ModelPresetConfig | None = None,
) -> ProviderSignature:
    """Return the config fields that affect the active provider chain.

    返回值由原先的位置型大元组改为 ``ProviderSignature`` 值对象:字段语义保持不变,
    仅数据结构发生变化,调用方改用具名字段访问。
    """
    resolved = _resolve_model_preset(config, preset_name=preset_name, preset=preset)
    p = config.get_provider(resolved.model, preset=resolved)
    fallback_presets = _resolve_fallback_presets(config, resolved)

    def _fallback_signature(fallback: ModelPresetConfig) -> ProviderSignature:
        # fallback 自身不再嵌套 fallbacks,故 fallbacks=() 保持空。
        fp = config.get_provider(fallback.model, preset=fallback)
        return ProviderSignature(
            model=fallback.model,
            provider=fallback.provider,
            provider_name=config.get_provider_name(fallback.model, preset=fallback),
            api_key=config.get_api_key(fallback.model, preset=fallback),
            api_base=config.get_api_base(fallback.model, preset=fallback),
            extra_headers=fp.extra_headers if fp else None,
            extra_body=fp.extra_body if fp else None,
            api_type=fp.api_type if fp else "auto",
            region=getattr(fp, "region", None) if fp else None,
            profile=getattr(fp, "profile", None) if fp else None,
            max_tokens=fallback.max_tokens,
            temperature=fallback.temperature,
            reasoning_effort=fallback.reasoning_effort,
            context_window_tokens=fallback.context_window_tokens,
            fallbacks=(),
        )

    return ProviderSignature(
        model=resolved.model,
        provider=resolved.provider,
        provider_name=config.get_provider_name(resolved.model, preset=resolved),
        api_key=config.get_api_key(resolved.model, preset=resolved),
        api_base=config.get_api_base(resolved.model, preset=resolved),
        extra_headers=p.extra_headers if p else None,
        extra_body=p.extra_body if p else None,
        api_type=p.api_type if p else "auto",
        region=getattr(p, "region", None) if p else None,
        profile=getattr(p, "profile", None) if p else None,
        max_tokens=resolved.max_tokens,
        temperature=resolved.temperature,
        reasoning_effort=resolved.reasoning_effort,
        context_window_tokens=resolved.context_window_tokens,
        fallbacks=tuple(_fallback_signature(fallback) for fallback in fallback_presets),
    )


def build_provider_snapshot(
    config: Config,
    *,
    preset_name: str | None = None,
    preset: ModelPresetConfig | None = None,
) -> ProviderSnapshot:
    resolved = _resolve_model_preset(config, preset_name=preset_name, preset=preset)
    # Auto-detect context window from built-in model metadata table when the
    # preset leaves it unset (None). Trae-style: built-in metadata + fallback.
    from miniunicorn.cli.models import get_model_context_limit

    primary_window = resolved.context_window_tokens
    if primary_window is None:
        provider_name = config.get_provider_name(resolved.model, preset=resolved)
        primary_window = get_model_context_limit(resolved.model, provider_name)
    fallback_windows = [
        fallback.context_window_tokens
        if fallback.context_window_tokens is not None
        else get_model_context_limit(fallback.model, fallback.provider)
        for fallback in _resolve_fallback_presets(config, resolved)
    ]
    return ProviderSnapshot(
        provider=make_provider(config, preset=resolved),
        model=resolved.model,
        context_window_tokens=min([primary_window, *fallback_windows]),
        signature=provider_signature(config, preset=resolved),
    )


def load_provider_snapshot(
    config_path: Path | None = None,
    *,
    preset_name: str | None = None,
) -> ProviderSnapshot:
    from miniunicorn.config.loader import load_config, resolve_config_env_vars

    return build_provider_snapshot(
        resolve_config_env_vars(load_config(config_path)),
        preset_name=preset_name,
    )
