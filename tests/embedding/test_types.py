from dataclasses import asdict

import pytest

from miniunicorn.embedding.types import (
    EmbeddingFailure,
    EmbeddingResult,
    EmbeddingStatus,
    IndexStatus,
    ModelStatus,
    RecallStatus,
    SourceStatus,
)


def test_embedding_result_never_contains_vectors_and_failure_together():
    failure = EmbeddingFailure("dependency_missing", "install vector extra", True)
    with pytest.raises(ValueError):
        EmbeddingResult(vectors=((1.0,),), failure=failure)


def test_embedding_result_empty_is_ok_without_failure():
    assert EmbeddingResult().ok is True


def test_shared_status_contract_has_four_sections():
    status = EmbeddingStatus(
        model=ModelStatus(state="not_downloaded"),
        index=IndexStatus(state="missing"),
        sources=SourceStatus(discovered=0, indexed=0, pending=0, stale=0, invalid=0, inactive=0),
        recall=RecallStatus(configured=True, active=False, fallback_reason="model_not_ready"),
    ).to_dict()
    assert set(status) == {"model", "index", "sources", "recall"}
    assert status["recall"]["fallback_reason"] == "model_not_ready"
    assert status["model"]["state"] == "not_downloaded"


def test_status_contract_never_exposes_vectors_or_secrets():
    model = asdict(ModelStatus(state="ready", dimension=512))
    index = asdict(IndexStatus(state="ready"))
    assert "vector" not in model
    assert "vector" not in index
    assert "embedding" not in model
    assert "api_key" not in str(EmbeddingStatus(
        model=ModelStatus(state="ready", dimension=512),
        index=IndexStatus(state="ready"),
        sources=SourceStatus(discovered=2, indexed=2, pending=0, stale=0, invalid=0, inactive=0),
        recall=RecallStatus(configured=True, active=True, fallback_reason=None),
    ).to_dict()).lower()
