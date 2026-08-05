"""Shared data contracts for the local embedding memory feature.

These typed values are consumed intact by the model manager, the vector
index manager, the recall service, the CLI, and the WebUI so every surface
reports the same status shape. The status API never exposes raw vectors or
secrets.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

FailureCode = Literal[
    "disabled",
    "dependency_missing",
    "not_downloaded",
    "download_failed",
    "hash_mismatch",
    "model_load_failed",
    "inference_failed",
    "model_mismatch",
    "dimension_mismatch",
    "non_finite",
    "index_missing",
    "index_stale",
    "index_corrupt",
    "source_invalid",
    "io_error",
    "cancelled",
]
ModelState = Literal["not_downloaded", "downloading", "verifying", "ready", "corrupt", "failed"]
IndexState = Literal["missing", "building", "ready", "stale", "corrupt", "failed"]


@dataclass(frozen=True)
class EmbeddingFailure:
    code: FailureCode
    message: str
    retryable: bool


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: tuple[tuple[float, ...], ...] = ()
    failure: EmbeddingFailure | None = None

    def __post_init__(self) -> None:
        if self.vectors and self.failure is not None:
            raise ValueError("embedding result cannot contain vectors and failure")

    @property
    def ok(self) -> bool:
        return self.failure is None


@dataclass(frozen=True)
class ModelManifest:
    model_id: str
    revision: str
    dimension: int
    files: dict[str, str]


@dataclass(frozen=True)
class ModelStatus:
    state: ModelState
    model_id: str | None = None
    revision: str | None = None
    dimension: int | None = None
    cache_path: str | None = None
    bytes: int = 0
    last_self_test: str | None = None
    last_error_code: str | None = None
    message: str = ""


@dataclass(frozen=True)
class IndexStatus:
    state: IndexState
    path: str | None = None
    bytes: int = 0
    last_rebuild: str | None = None
    last_error_code: str | None = None
    message: str = ""


@dataclass(frozen=True)
class SourceStatus:
    discovered: int
    indexed: int
    pending: int
    stale: int
    invalid: int
    inactive: int
    errors: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True)
class RecallStatus:
    configured: bool
    active: bool
    fallback_reason: str | None
    last_self_test: str | None = None
    last_latency_ms: float | None = None


@dataclass(frozen=True)
class EmbeddingStatus:
    model: ModelStatus
    index: IndexStatus
    sources: SourceStatus
    recall: RecallStatus
    operation: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        if payload.get("operation") is None:
            payload.pop("operation", None)
        return payload

