"""Local CPU embedding provider for vector memory.

Uses FastEmbed (ONNX Runtime under the hood) to run a small Chinese-capable
embedding model (`BAAI/bge-small-zh-v1.5`, 512 dimensions) entirely on CPU,
independent of the chat LLM provider. The same instance is shared by
``MemoryStore.index_text``, the automatic query-embedding path in
``AgentLoop``, and the ``recall`` tool.

Design contract (see docs/superpowers/specs/2026-07-28-comprehensive-code-review-remediation-design.md §5.4):

- model loading is lazy (first ``embed`` call);
- the execution provider is CPU (no CUDA / GPU packages are added);
- blocking model load and inference run outside the event loop
  (``asyncio.to_thread``);
- concurrent first calls load the model once (``asyncio.Lock``);
- empty input returns an empty list without loading the model;
- output is L2-normalized and converted to ``list[list[float]]``;
- every returned vector is exactly 512 elements;
- a model override different from the configured local model is rejected;
- missing dependency, download failure, and inference failure produce a
  clear diagnostic and return control to the existing non-vector memory
  path (callers treat ``[]`` as "skip indexing / recall unavailable").
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

#: Canonical local model identifier. FastEmbed's maintained model list
#: identifies this as a Chinese 512-dimensional model.
DEFAULT_LOCAL_MODEL = "BAAI/bge-small-zh-v1.5"

#: Vector dimensionality for :data:`DEFAULT_LOCAL_MODEL`.
DEFAULT_LOCAL_DIMENSION = 512


class LocalEmbeddingProvider:
    """CPU-only local embedding provider backed by FastEmbed.

    The provider is intentionally cheap to construct: the ONNX model is
    downloaded and loaded on the first ``embed`` call, then reused for the
    lifetime of the instance. A single instance is meant to be shared by
    every memory-retrieval call site so the model stays resident.
    """

    def __init__(
        self,
        model_name: str = DEFAULT_LOCAL_MODEL,
        dimension: int = DEFAULT_LOCAL_DIMENSION,
    ) -> None:
        self.model_name = model_name
        self.dimension = dimension
        self._model: Any = None
        self._load_lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        """Whether the FastEmbed dependency is importable.

        ``True`` does not guarantee the model is already loaded or that a
        subsequent ``embed`` call will succeed offline; it only reports that
        the ``fastembed`` package is installed.
        """
        try:
            import fastembed  # noqa: F401  # pragma: no cover - import probe
        except ImportError:
            return False
        return True

    async def embed(
        self,
        texts: list[str],
        model: str | None = None,
    ) -> list[list[float]]:
        """Embed *texts* using the local CPU model.

        ``model`` may be passed by callers that previously targeted an
        external endpoint (e.g. ``MemoryStore.index_text``). Only ``None``
        or the configured local model id is accepted; any other value is
        rejected with a clear diagnostic and an empty list, so the caller
        falls back to the non-vector memory path instead of silently
        mixing vectors from different models.
        """
        if not texts:
            return []
        if model is not None and model != self.model_name:
            logger.warning(
                "LocalEmbeddingProvider rejected model override: requested={!r} "
                "but local model is {!r}. Vector recall will be skipped for this call; "
                "remove the stale model override or set agents.defaults.embeddingModel "
                "to the local model id.",
                model,
                self.model_name,
            )
            return []

        try:
            await self._ensure_model_loaded()
        except ImportError:
            logger.warning(
                "fastembed is not installed; vector memory disabled. "
                "Install with: pip install 'miniunicorn[vector]'"
            )
            return []
        except Exception:
            logger.exception(
                "LocalEmbeddingProvider failed to load model {!r}; "
                "vector recall disabled for this call",
                self.model_name,
            )
            return []

        if self._model is None:  # pragma: no cover - defensive
            return []

        try:
            raw_embeddings = await asyncio.to_thread(self._raw_embed, texts)
        except Exception:
            logger.exception(
                "LocalEmbeddingProvider inference failed for model {!r}",
                self.model_name,
            )
            return []

        result: list[list[float]] = []
        for vec in raw_embeddings:
            normalized = _l2_normalize(vec)
            if len(normalized) != self.dimension:
                logger.error(
                    "LocalEmbeddingProvider dimension mismatch: model {!r} produced "
                    "{}-dim vector, expected {}. Vector recall disabled for this call.",
                    self.model_name,
                    len(normalized),
                    self.dimension,
                )
                return []
            result.append(normalized)
        return result

    async def _ensure_model_loaded(self) -> None:
        """Load the ONNX model on the first call, guarded by a lock.

        Concurrent first callers wait on the lock; only the holder performs
        the blocking load (via ``to_thread``), then everyone reuses the
        cached instance.
        """
        if self._model is not None:
            return
        async with self._load_lock:
            if self._model is not None:
                return
            # Import inside the lock so a concurrent caller that lost the
            # race does not also trigger the import.
            from fastembed import TextEmbedding

            logger.info(
                "LocalEmbeddingProvider loading model {!r} (CPU, dim={}) ...",
                self.model_name,
                self.dimension,
            )
            self._model = await asyncio.to_thread(TextEmbedding, model_name=self.model_name)
            logger.info("LocalEmbeddingProvider model {!r} loaded", self.model_name)

    def _raw_embed(self, texts: list[str]) -> list[list[float]]:
        """Run the blocking FastEmbed inference and collect to a list.

        FastEmbed's ``embed`` returns a generator of numpy arrays; we
        materialize them here so the event-loop thread never iterates
        the generator.
        """
        assert self._model is not None
        # FastEmbed yields numpy arrays; convert to plain Python lists.
        return [list(map(float, vec)) for vec in self._model.embed(texts)]


def _l2_normalize(vec: list[float]) -> list[float]:
    """L2-normalize a vector. Returns the zero vector if norm is 0.

    FastEmbed's BGE models already emit normalized vectors, but we
    normalize defensively so downstream cosine-similarity math stays
    correct even if a future model swap returns un-normalized output.
    """
    norm = sum(v * v for v in vec) ** 0.5
    if norm == 0.0:
        return vec
    return [v / norm for v in vec]
