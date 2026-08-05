"""Local CPU embedding provider for vector memory.

Runs the pinned FastEmbed BGE model (``BAAI/bge-small-zh-v1.5``, 512
dimensions) entirely on CPU via ONNX Runtime, loading only from a
hash-verified local model directory owned by ``EmbeddingModelManager``.
Every call returns a typed :class:`EmbeddingResult` so callers can
distinguish "no input" from "model not ready" from "inference failed".
"""

from __future__ import annotations

import asyncio
import math
from pathlib import Path
from typing import Any, Sequence

from loguru import logger

from miniunicorn.embedding import MODEL_DIMENSION, MODEL_ID
from miniunicorn.embedding.model_manager import EmbeddingModelManager
from miniunicorn.embedding.types import EmbeddingFailure, EmbeddingResult

#: Backward-compatible aliases kept for existing call sites.
DEFAULT_LOCAL_MODEL = MODEL_ID
DEFAULT_LOCAL_DIMENSION = MODEL_DIMENSION


class LocalEmbeddingProvider:
    """CPU-only local embedding provider backed by FastEmbed.

    The provider is intentionally cheap to construct: the ONNX model is
    loaded lazily on the first ``embed`` call from the manager's verified
    path, then reused for the lifetime of the instance. A single instance
    is meant to be shared by every memory-retrieval call site so the model
    stays resident.
    """

    def __init__(self, manager: EmbeddingModelManager | None = None, **kwargs: Any) -> None:
        model_name = kwargs.pop("model_name", None)
        if kwargs:
            raise TypeError(f"unexpected provider arguments: {sorted(kwargs)}")
        if model_name is not None and model_name != MODEL_ID:
            logger.warning(
                "LocalEmbeddingProvider received legacy model_name {!r}; "
                "the pinned model {} is used instead",
                model_name,
                MODEL_ID,
            )
        self.manager = manager or EmbeddingModelManager()
        self._model: Any = None
        self._model_path: Path | None = None
        self._load_lock = asyncio.Lock()

    @property
    def model_name(self) -> str:
        """Canonical model identifier used by this provider."""
        return MODEL_ID

    @property
    def dimension(self) -> int:
        """Vector dimensionality produced by this provider."""
        return MODEL_DIMENSION

    @property
    def enabled(self) -> bool:
        """Whether the FastEmbed dependency is importable."""
        try:
            import fastembed  # noqa: F401
        except ImportError:
            return False
        return True

    async def embed(
        self,
        texts: list[str],
        model: str | None = None,
    ) -> EmbeddingResult:
        """Embed *texts* and return a typed result.

        Empty input yields an empty result without touching the model.
        ``model`` must be ``None`` or the pinned model id; anything else is
        rejected so vectors from different models never mix.
        """
        if not texts:
            return EmbeddingResult()
        if model not in (None, MODEL_ID):
            return EmbeddingResult(
                failure=EmbeddingFailure(
                    "model_mismatch", f"unsupported embedding model: {model}", False
                )
            )
        path = self.manager.validated_model_path()
        if path is None:
            status = self.manager.status()
            code = (
                "dependency_missing"
                if status.last_error_code == "dependency_missing"
                else "not_downloaded"
            )
            return EmbeddingResult(
                failure=EmbeddingFailure(code, status.message or "模型尚未就绪", True)
            )
        try:
            await self._ensure_model_loaded(path)
            raw = await asyncio.to_thread(self._raw_embed, texts)
        except Exception as exc:
            return EmbeddingResult(
                failure=EmbeddingFailure("inference_failed", str(exc), True)
            )
        vectors: list[tuple[float, ...]] = []
        for raw_vector in raw:
            if len(raw_vector) != MODEL_DIMENSION:
                return EmbeddingResult(
                    failure=EmbeddingFailure(
                        "dimension_mismatch", "expected 512 dimensions", False
                    )
                )
            if not all(math.isfinite(value) for value in raw_vector):
                return EmbeddingResult(
                    failure=EmbeddingFailure(
                        "non_finite", "embedding contains non-finite values", True
                    )
                )
            normalized = _l2_normalize([float(value) for value in raw_vector])
            vectors.append(tuple(normalized))
        return EmbeddingResult(vectors=tuple(vectors))

    async def _ensure_model_loaded(self, path: Path) -> None:
        """Load the verified ONNX model once, guarded by a lock."""
        if self._model is not None and self._model_path == path:
            return
        async with self._load_lock:
            if self._model is not None and self._model_path == path:
                return
            from fastembed import TextEmbedding

            logger.info(
                "LocalEmbeddingProvider loading model {!r} from {} (CPU, dim={}) ...",
                MODEL_ID,
                path,
                MODEL_DIMENSION,
            )
            self._model = await asyncio.to_thread(
                TextEmbedding,
                model_name=MODEL_ID,
                specific_model_path=str(path),
                providers=["CPUExecutionProvider"],
            )
            self._model_path = path
            logger.info("LocalEmbeddingProvider model {!r} loaded", MODEL_ID)

    def _raw_embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Run the blocking FastEmbed inference and collect to a list."""
        assert self._model is not None
        return [list(map(float, vec)) for vec in self._model.embed(texts)]


def _l2_normalize(vec: list[float]) -> list[float]:
    """L2-normalize a vector. Returns the zero vector if norm is 0."""
    norm = sum(v * v for v in vec) ** 0.5
    if norm == 0.0:
        return vec
    return [v / norm for v in vec]
