"""Structured memory config validation (C2)."""

import pytest
from pydantic import ValidationError

from miniunicorn.config.schema import AgentDefaults, StructuredMemoryConfig


def test_structured_memory_defaults_to_shadow() -> None:
    config = StructuredMemoryConfig()

    assert config.mode == "shadow"
    assert config.recall_token_budget == 2500
    assert config.max_recall_hits == 20


@pytest.mark.parametrize("field", ["embeddingModel", "vectorStore", "vectorSearch"])
def test_structured_memory_rejects_vector_fields(field) -> None:
    with pytest.raises(ValidationError):
        StructuredMemoryConfig.model_validate({field: "forbidden"})


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("recallTokenBudget", 100),
        ("recallTokenBudget", 20_000),
        ("maxRecallHits", 0),
        ("maxRecallHits", 200),
        ("lockTimeoutS", 0.01),
        ("minRepeatedEvidence", 1),
        ("candidateTtlDays", 0),
    ],
)
def test_structured_memory_rejects_out_of_range(field, bad) -> None:
    with pytest.raises(ValidationError):
        StructuredMemoryConfig.model_validate({field: bad})


def test_structured_memory_camel_case_alias_round_trip() -> None:
    config = StructuredMemoryConfig.model_validate(
        {
            "mode": "governed",
            "recallTokenBudget": 1000,
            "maxRecallHits": 5,
            "lockTimeoutS": 2.0,
            "autoPromoteVerified": False,
            "minRepeatedEvidence": 3,
            "candidateTtlDays": 14,
            "recallAuditEnabled": True,
        }
    )

    assert config.mode == "governed"
    assert config.recall_token_budget == 1000
    assert config.auto_promote_verified is False
    assert config.recall_audit_enabled is True
    dumped = config.model_dump(by_alias=True)
    assert dumped["recallTokenBudget"] == 1000
    assert dumped["autoPromoteVerified"] is False


def test_structured_memory_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        StructuredMemoryConfig.model_validate({"mode": "governed", "vectorPath": "x"})


def test_structured_memory_rejects_invalid_mode() -> None:
    with pytest.raises(ValidationError):
        StructuredMemoryConfig.model_validate({"mode": "vector"})


def test_agent_defaults_carry_structured_memory_config() -> None:
    defaults = AgentDefaults()

    assert defaults.structured_memory.mode == "shadow"
    assert defaults.structured_memory.recall_token_budget == 2500
