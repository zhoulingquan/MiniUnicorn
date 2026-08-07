"""Tests for the local CPU embedding provider.

FastEmbed is mocked throughout — the unit suite never performs a model
download. The manager is mocked to produce a ready hash-verified model dir.
download. A separate optional smoke test (``TestRealModelSmoke``) downloads
the real BGE model and is skipped by default; enable it by setting the
``MINIUNICORN_RUN_EMBEDDING_SMOKE=1`` environment variable.
"""

from __future__ import annotations

import asyncio
import os
import sys
import types
from pathlib import Path

import pytest

from miniunicorn.embedding import MODEL_DIMENSION, MODEL_ID
from miniunicorn.embedding.model_manager import EmbeddingModelManager
from miniunicorn.providers.local_embedding import (
    DEFAULT_LOCAL_DIMENSION,
    DEFAULT_LOCAL_MODEL,
    LocalEmbeddingProvider,
    _l2_normalize,
)


class TestL2Normalize:
    def test_unit_vector_unchanged(self):
        vec = [1.0, 0.0, 0.0]
        assert _l2_normalize(vec) == [1.0, 0.0, 0.0]

    def test_scaled_vector_normalized(self):
        vec = [3.0, 0.0, 0.0]
        result = _l2_normalize(vec)
        assert pytest.approx(result[0], abs=1e-6) == 1.0

    def test_zero_vector_returned_as_is(self):
        vec = [0.0, 0.0, 0.0]
        assert _l2_normalize(vec) == [0.0, 0.0, 0.0]


def _make_ready_manager(model_dir: Path) -> EmbeddingModelManager:
    manager = EmbeddingModelManager(model_dir)
    (model_dir / "model_optimized.onnx").write_bytes(b"onnx")
    (model_dir / "tokenizer.json").write_text("{}", encoding="utf-8")
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    manager._write_manifest_atomic(manager._build_manifest())
    manager._write_state("ready", None, "", last_self_test="2026-08-04 10:00")
    return manager


@pytest.fixture
def fake_ready_manager(tmp_path: Path) -> EmbeddingModelManager:
    return _make_ready_manager(tmp_path)


@pytest.fixture
def fake_fastembed_module(monkeypatch):
    """Inject a fake ``fastembed`` module into ``sys.modules``."""
    fake_module = types.ModuleType("fastembed")
    calls: dict = {}

    class FakeTextEmbedding:
        instances: list["FakeTextEmbedding"] = []

        def __init__(self, **kwargs):
            calls["specific_model_path"] = kwargs.get("specific_model_path")
            calls["providers"] = kwargs.get("providers")
            FakeTextEmbedding.instances.append(self)

        def embed(self, texts):
            for i, t in enumerate(texts):
                base = float(len(t) + i)
                vec = [base / (j + 1) for j in range(MODEL_DIMENSION)]
                yield vec

    fake_module.TextEmbedding = FakeTextEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", fake_module)
    fake_module.calls = calls
    return fake_module


@pytest.mark.asyncio
async def test_missing_validated_model_is_typed_failure(tmp_path):
    provider = LocalEmbeddingProvider(manager=EmbeddingModelManager(tmp_path))
    result = await provider.embed(["测试"])
    assert result.vectors == ()
    assert result.failure is not None
    assert result.failure.code == "not_downloaded"


@pytest.mark.asyncio
async def test_dependency_missing_is_typed_failure(fake_ready_manager, monkeypatch):
    fake_ready_manager._write_state("failed", "dependency_missing", "缺少 fastembed")
    provider = LocalEmbeddingProvider(manager=fake_ready_manager)
    result = await provider.embed(["测试"])
    assert result.failure is not None
    assert result.failure.code == "dependency_missing"


@pytest.mark.asyncio
async def test_non_finite_vector_is_rejected(fake_ready_manager, monkeypatch):
    class FakeModel:
        def embed(self, texts):
            for _t in texts:
                yield [float("nan")] * MODEL_DIMENSION

    provider = LocalEmbeddingProvider(manager=fake_ready_manager)
    provider._model = FakeModel()
    provider._model_path = fake_ready_manager.model_dir
    result = await provider.embed(["测试"])
    assert result.vectors == ()
    assert result.failure is not None
    assert result.failure.code == "non_finite"


@pytest.mark.asyncio
async def test_loader_uses_verified_path_and_cpu(fake_ready_manager, fake_fastembed_module):
    provider = LocalEmbeddingProvider(manager=fake_ready_manager)
    result = await provider.embed(["中文记忆"])
    assert result.ok
    assert len(result.vectors) == 1
    assert len(result.vectors[0]) == MODEL_DIMENSION
    assert fake_fastembed_module.calls["specific_model_path"] == str(
        fake_ready_manager.model_dir
    )
    assert fake_fastembed_module.calls["providers"] == ["CPUExecutionProvider"]


@pytest.mark.asyncio
async def test_empty_input_returns_empty_without_loading(fake_fastembed_module, tmp_path):
    provider = LocalEmbeddingProvider(manager=_make_ready_manager(tmp_path))
    result = await provider.embed([])
    assert result.ok
    assert result.vectors == ()
    assert result.failure is None
    assert provider._model is None  # model not loaded


@pytest.mark.asyncio
async def test_model_loaded_lazily_on_first_call(fake_ready_manager, fake_fastembed_module):
    FakeTextEmbedding = fake_fastembed_module.TextEmbedding
    FakeTextEmbedding.instances.clear()
    provider = LocalEmbeddingProvider(manager=fake_ready_manager)
    assert provider._model is None
    await provider.embed(["text"])
    assert provider._model is not None
    assert len(FakeTextEmbedding.instances) == 1


@pytest.mark.asyncio
async def test_model_loaded_only_once_across_calls(fake_ready_manager, fake_fastembed_module):
    FakeTextEmbedding = fake_fastembed_module.TextEmbedding
    FakeTextEmbedding.instances.clear()
    provider = LocalEmbeddingProvider(manager=fake_ready_manager)
    await provider.embed(["a"])
    await provider.embed(["b"])
    await provider.embed(["c"])
    assert len(FakeTextEmbedding.instances) == 1


@pytest.mark.asyncio
async def test_concurrent_first_calls_load_model_once(
    fake_ready_manager, fake_fastembed_module
):
    FakeTextEmbedding = fake_fastembed_module.TextEmbedding
    FakeTextEmbedding.instances.clear()
    provider = LocalEmbeddingProvider(manager=fake_ready_manager)
    await asyncio.gather(
        provider.embed(["a"]),
        provider.embed(["b"]),
        provider.embed(["c"]),
    )
    assert len(FakeTextEmbedding.instances) == 1


@pytest.mark.asyncio
async def test_model_override_rejected(fake_ready_manager, fake_fastembed_module):
    provider = LocalEmbeddingProvider(manager=fake_ready_manager)
    result = await provider.embed(["text"], model="text-embedding-3-small")
    assert result.vectors == ()
    assert result.failure is not None
    assert result.failure.code == "model_mismatch"
    assert provider._model is None  # never loaded


@pytest.mark.asyncio
async def test_matching_model_override_accepted(fake_ready_manager, fake_fastembed_module):
    provider = LocalEmbeddingProvider(manager=fake_ready_manager)
    result = await provider.embed(["text"], model=MODEL_ID)
    assert result.ok
    assert len(result.vectors) == 1
    assert len(result.vectors[0]) == MODEL_DIMENSION


@pytest.mark.asyncio
async def test_missing_dependency_returns_typed_failure(fake_ready_manager, monkeypatch):
    monkeypatch.setitem(sys.modules, "fastembed", None)
    provider = LocalEmbeddingProvider(manager=fake_ready_manager)
    result = await provider.embed(["text"])
    assert result.failure is not None
    assert result.failure.code == "inference_failed"


@pytest.mark.asyncio
async def test_inference_failure_returns_typed_failure(fake_ready_manager, fake_fastembed_module):
    class FailingEmbedding:
        def __init__(self, **kwargs):
            pass

        def embed(self, texts):
            raise RuntimeError("ONNX inference crashed")

    fake_fastembed_module.TextEmbedding = FailingEmbedding
    provider = LocalEmbeddingProvider(manager=fake_ready_manager)
    result = await provider.embed(["text"])
    assert result.failure is not None
    assert result.failure.code == "inference_failed"


@pytest.mark.asyncio
async def test_dimension_mismatch_returns_typed_failure(fake_ready_manager, fake_fastembed_module):
    class WrongDimEmbedding:
        def __init__(self, **kwargs):
            pass

        def embed(self, texts):
            for _t in texts:
                yield [0.1] * 128  # wrong dimension

    fake_fastembed_module.TextEmbedding = WrongDimEmbedding
    provider = LocalEmbeddingProvider(manager=fake_ready_manager)
    result = await provider.embed(["text"])
    assert result.vectors == ()
    assert result.failure is not None
    assert result.failure.code == "dimension_mismatch"


@pytest.mark.asyncio
async def test_output_is_l2_normalized(fake_ready_manager, fake_fastembed_module):
    class UnnormalizedEmbedding:
        def __init__(self, **kwargs):
            pass

        def embed(self, texts):
            for _t in texts:
                # 512-dim vector with norm != 1
                yield [3.0] * MODEL_DIMENSION

    fake_fastembed_module.TextEmbedding = UnnormalizedEmbedding
    provider = LocalEmbeddingProvider(manager=fake_ready_manager)
    result = await provider.embed(["text"])
    assert result.ok
    vec = result.vectors[0]
    norm = sum(v * v for v in vec) ** 0.5
    assert pytest.approx(norm, abs=1e-6) == 1.0


class TestLocalEmbeddingProviderEnabled:
    def test_default_constants_alias_pinned_model(self):
        assert DEFAULT_LOCAL_MODEL == "BAAI/bge-small-zh-v1.5"
        assert DEFAULT_LOCAL_DIMENSION == 512

    def test_provider_exposes_model_name_and_dimension(self):
        provider = LocalEmbeddingProvider()
        assert provider.model_name == DEFAULT_LOCAL_MODEL
        assert provider.dimension == DEFAULT_LOCAL_DIMENSION

    def test_enabled_false_when_fastembed_missing(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "fastembed", None)
        provider = LocalEmbeddingProvider()
        assert provider.enabled is False

    def test_enabled_true_when_fastembed_importable(self, monkeypatch):
        fake_module = types.ModuleType("fastembed")
        fake_module.TextEmbedding = type("TextEmbedding", (), {})
        monkeypatch.setitem(sys.modules, "fastembed", fake_module)
        provider = LocalEmbeddingProvider()
        assert provider.enabled is True


_SMOKE_ENABLED = os.environ.get("MINIUNICORN_RUN_EMBEDDING_SMOKE") == "1"
_SMOKE_REASON = (
    "set MINIUNICORN_RUN_EMBEDDING_SMOKE=1 to run the real BGE model smoke test "
    "(requires network access to download the approved BAAI/bge-small-zh-v1.5 model)"
)


@pytest.mark.skipif(not _SMOKE_ENABLED, reason=_SMOKE_REASON)
class TestRealModelSmoke:
    """Optional CPU smoke test that downloads the real BGE model.

    Verifies the end-to-end contract on CPU: a Chinese text is embedded to
    exactly 512 dimensions and the output is L2-normalized. This test is
    intentionally skipped in CI and the default suite — it only runs when a
    developer explicitly opts in via the environment variable.
    """

    @pytest.mark.asyncio
    async def test_chinese_embedding_is_512_dim_and_normalized(self):
        provider = LocalEmbeddingProvider()
        assert provider.enabled, "fastembed must be installed for the smoke test"
        result = await provider.embed([" MiniUnicorn 本地嵌入中文测试用例"])
        assert len(result) == 1
        vec = result[0]
        assert len(vec) == DEFAULT_LOCAL_DIMENSION
        norm = sum(v * v for v in vec) ** 0.5
        assert pytest.approx(norm, abs=1e-5) == 1.0
