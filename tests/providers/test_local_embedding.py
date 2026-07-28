"""Tests for the local CPU embedding provider.

FastEmbed is mocked throughout — the unit suite never performs a model
download. A separate optional smoke test (``TestRealModelSmoke``) downloads
the real BGE model and is skipped by default; enable it by setting the
``MINIUNICORN_RUN_EMBEDDING_SMOKE=1`` environment variable.
"""

from __future__ import annotations

import asyncio
import os
import sys
import types

import pytest

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


class TestLocalEmbeddingProviderConstruction:
    def test_default_model_and_dimension(self):
        provider = LocalEmbeddingProvider()
        assert provider.model_name == DEFAULT_LOCAL_MODEL
        assert provider.dimension == DEFAULT_LOCAL_DIMENSION
        assert provider.dimension == 512

    def test_model_not_loaded_on_construction(self):
        provider = LocalEmbeddingProvider()
        assert provider._model is None


class TestLocalEmbeddingProviderEmbed:
    @pytest.fixture
    def fake_fastembed_module(self, monkeypatch):
        """Inject a fake ``fastembed`` module into ``sys.modules``."""
        fake_module = types.ModuleType("fastembed")

        class FakeTextEmbedding:
            instances: list["FakeTextEmbedding"] = []

            def __init__(self, model_name: str = DEFAULT_LOCAL_MODEL):
                self.model_name = model_name
                FakeTextEmbedding.instances.append(self)

            def embed(self, texts):
                # Return deterministic 512-dim vectors derived from text length
                # so tests can assert dimensionality without a real model.
                for i, t in enumerate(texts):
                    base = float(len(t) + i)
                    vec = [base / (j + 1) for j in range(DEFAULT_LOCAL_DIMENSION)]
                    yield vec

        fake_module.TextEmbedding = FakeTextEmbedding
        monkeypatch.setitem(sys.modules, "fastembed", fake_module)
        return fake_module

    @pytest.mark.asyncio
    async def test_empty_input_returns_empty_without_loading(self, fake_fastembed_module):
        provider = LocalEmbeddingProvider()
        result = await provider.embed([])
        assert result == []
        assert provider._model is None  # model not loaded

    @pytest.mark.asyncio
    async def test_embed_returns_512_dim_vectors(self, fake_fastembed_module):
        provider = LocalEmbeddingProvider()
        result = await provider.embed(["hello", "world"])
        assert len(result) == 2
        for vec in result:
            assert len(vec) == 512

    @pytest.mark.asyncio
    async def test_model_loaded_lazily_on_first_call(self, fake_fastembed_module):
        FakeTextEmbedding = fake_fastembed_module.TextEmbedding
        FakeTextEmbedding.instances.clear()
        provider = LocalEmbeddingProvider()
        assert provider._model is None
        await provider.embed(["text"])
        assert provider._model is not None
        assert len(FakeTextEmbedding.instances) == 1

    @pytest.mark.asyncio
    async def test_model_loaded_only_once_across_calls(self, fake_fastembed_module):
        FakeTextEmbedding = fake_fastembed_module.TextEmbedding
        FakeTextEmbedding.instances.clear()
        provider = LocalEmbeddingProvider()
        await provider.embed(["a"])
        await provider.embed(["b"])
        await provider.embed(["c"])
        assert len(FakeTextEmbedding.instances) == 1

    @pytest.mark.asyncio
    async def test_concurrent_first_calls_load_model_once(self, fake_fastembed_module):
        FakeTextEmbedding = fake_fastembed_module.TextEmbedding
        FakeTextEmbedding.instances.clear()
        provider = LocalEmbeddingProvider()
        await asyncio.gather(
            provider.embed(["a"]),
            provider.embed(["b"]),
            provider.embed(["c"]),
        )
        assert len(FakeTextEmbedding.instances) == 1

    @pytest.mark.asyncio
    async def test_model_override_rejected(self, fake_fastembed_module):
        provider = LocalEmbeddingProvider()
        result = await provider.embed(["text"], model="text-embedding-3-small")
        assert result == []
        assert provider._model is None  # never loaded

    @pytest.mark.asyncio
    async def test_matching_model_override_accepted(self, fake_fastembed_module):
        provider = LocalEmbeddingProvider()
        result = await provider.embed(["text"], model=DEFAULT_LOCAL_MODEL)
        assert len(result) == 1
        assert len(result[0]) == 512

    @pytest.mark.asyncio
    async def test_missing_dependency_returns_empty(self, monkeypatch):
        # Ensure fastembed is NOT importable.
        monkeypatch.setitem(sys.modules, "fastembed", None)
        provider = LocalEmbeddingProvider()
        result = await provider.embed(["text"])
        assert result == []

    @pytest.mark.asyncio
    async def test_inference_failure_returns_empty(self, fake_fastembed_module):
        class FailingEmbedding:
            def __init__(self, model_name):
                pass

            def embed(self, texts):
                raise RuntimeError("ONNX inference crashed")

        fake_fastembed_module.TextEmbedding = FailingEmbedding
        provider = LocalEmbeddingProvider()
        result = await provider.embed(["text"])
        assert result == []

    @pytest.mark.asyncio
    async def test_dimension_mismatch_returns_empty(self, fake_fastembed_module):
        class WrongDimEmbedding:
            def __init__(self, model_name):
                pass

            def embed(self, texts):
                for _t in texts:
                    yield [0.1] * 128  # wrong dimension

        fake_fastembed_module.TextEmbedding = WrongDimEmbedding
        provider = LocalEmbeddingProvider()
        result = await provider.embed(["text"])
        assert result == []

    @pytest.mark.asyncio
    async def test_output_is_l2_normalized(self, fake_fastembed_module):
        class UnnormalizedEmbedding:
            def __init__(self, model_name):
                pass

            def embed(self, texts):
                for _t in texts:
                    # 512-dim vector with norm != 1
                    yield [3.0] * DEFAULT_LOCAL_DIMENSION

        fake_fastembed_module.TextEmbedding = UnnormalizedEmbedding
        provider = LocalEmbeddingProvider()
        result = await provider.embed(["text"])
        assert len(result) == 1
        vec = result[0]
        norm = sum(v * v for v in vec) ** 0.5
        assert pytest.approx(norm, abs=1e-6) == 1.0


class TestLocalEmbeddingProviderEnabled:
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
