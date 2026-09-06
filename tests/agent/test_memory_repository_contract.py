"""Contract tests pinning the public repository surface (design section 9)."""

from __future__ import annotations

REQUIRED_METHODS = {
    "append_transaction",
    "append_create_if_absent",
    "get",
    "get_current",
    "revisions",
    "current_records",
    "active_for_conflict_key",
    "candidate_records",
    "candidate_ids_for_source",
    "record_created_for",
    "record_ids_for_source",
    "recall_candidates",
    "transaction_log",
    "storage_stats",
}


def test_repository_public_contract() -> None:
    from erza.memory.repository import StructuredMemoryRepository

    assert REQUIRED_METHODS <= set(dir(StructuredMemoryRepository))
