"""Helpers for runtime model preset selection."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from miniunicorn.config.schema import ModelPresetConfig
from miniunicorn.providers.base import LLMProvider
from miniunicorn.providers.factory import (
    ProviderSignature,
    ProviderSnapshot,
    build_provider_snapshot,
)

PresetSnapshotLoader = Callable[[str], ProviderSnapshot]


def default_selection_signature(
    signature: ProviderSignature | tuple[object, ...] | None,
) -> tuple[object, ...] | None:
    """提取“默认选择”的标识键。

    - 对于 ``ProviderSignature``(由 ``provider_signature`` 产出),取 ``(model, provider)``;
    - 对于 preset 快照短元组 ``("model_preset", name, json)``,取前两元素作为 preset 标识;
    - ``None`` 时返回 ``None``。
    """
    if signature is None:
        return None
    if isinstance(signature, ProviderSignature):
        return (signature.model, signature.provider)
    # preset 快照仍以短元组形式存储,沿用切片语义。
    return signature[:2]


def configured_model_presets(config: Any) -> dict[str, ModelPresetConfig]:
    return {**config.model_presets, "default": config.resolve_default_preset()}


def make_preset_snapshot_loader(
    config: Any,
    provider_snapshot_loader: Callable[..., ProviderSnapshot] | None,
) -> PresetSnapshotLoader:
    if provider_snapshot_loader is not None:
        return lambda name: provider_snapshot_loader(preset_name=name)
    return lambda name: build_provider_snapshot(config, preset_name=name)


def build_static_preset_snapshot(
    provider: LLMProvider,
    name: str,
    preset: ModelPresetConfig,
) -> ProviderSnapshot:
    provider.generation = preset.to_generation_settings()
    # Auto-detect context window when preset leaves it unset (None).
    from miniunicorn.providers.model_catalog import get_model_context_limit

    ctx = preset.context_window_tokens
    if ctx is None:
        ctx = get_model_context_limit(preset.model, preset.provider)
    return ProviderSnapshot(
        provider=provider,
        model=preset.model,
        context_window_tokens=ctx,
        signature=("model_preset", name, preset.model_dump_json()),
    )


def build_runtime_preset_snapshot(
    *,
    name: str,
    presets: dict[str, ModelPresetConfig],
    provider: LLMProvider,
    loader: PresetSnapshotLoader | None,
) -> ProviderSnapshot:
    if loader is not None:
        return loader(name)
    return build_static_preset_snapshot(provider, name, presets[name])


def normalize_preset_name(name: str | None, presets: dict[str, ModelPresetConfig]) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("model_preset must be a non-empty string")
    name = name.strip()
    if name not in presets:
        raise KeyError(
            f"model_preset {name!r} not found. Available: {', '.join(presets) or '(none)'}"
        )
    return name
