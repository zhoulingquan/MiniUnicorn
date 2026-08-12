"""Hard boundaries for governed structured memory."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from miniunicorn.agent.context import ContextBuilder
from miniunicorn.agent.memory import MemoryStore
from miniunicorn.agent.memory_models import MemoryScope, RecallQuery, RecallResult, ScopeKind
from miniunicorn.config.schema import StructuredMemoryConfig

UTC = timezone.utc


def query(secret: str = "private query text") -> RecallQuery:
    return RecallQuery(
        query_text=secret,
        allowed_scopes=(
            MemoryScope(kind=ScopeKind.USER, key="user:alice-secret"),
            MemoryScope(kind=ScopeKind.PROJECT, key="project:abc123"),
        ),
        now=datetime(2026, 8, 12, 9, 0, tzinfo=UTC),
    )


def test_recall_audit_is_redacted_rotated_and_not_git_tracked(tmp_path):
    store = MemoryStore(tmp_path, structured_config=StructuredMemoryConfig())
    audit_path = tmp_path / "memory" / "structured" / "recall-audit.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    old = {"timestamp": "old", "scope_hashes": [], "hits": []}
    audit_path.write_text(
        "".join(json.dumps({**old, "index": index}) + "\n" for index in range(1000)),
        encoding="utf-8",
    )

    store.write_recall_audit(query(), RecallResult(candidates=3, filtered=2, tokens_used=0))

    lines = audit_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1000
    assert '"index": 0' not in lines[0]
    latest = json.loads(lines[-1])
    serialized = json.dumps(latest, ensure_ascii=False)
    assert "private query text" not in serialized
    assert "alice-secret" not in serialized
    assert "project:abc123" not in serialized
    assert latest["scope_hashes"] == [
        hashlib.sha256("user:alice-secret".encode()).hexdigest(),
        hashlib.sha256("project:abc123".encode()).hexdigest(),
    ]
    assert set(latest) == {
        "timestamp",
        "scope_hashes",
        "hits",
        "candidates",
        "filtered",
        "excluded_by_budget",
        "tokens_used",
        "degraded",
        "error_code",
    }
    assert "memory/structured/recall-audit.jsonl" not in store.git._tracked_files


def test_governed_recall_writes_audit_when_enabled(tmp_path, monkeypatch):
    builder = ContextBuilder(
        tmp_path,
        structured_memory_config=StructuredMemoryConfig(
            mode="governed", recall_audit_enabled=True
        ),
    )
    written: list[tuple[RecallQuery, RecallResult]] = []
    result = RecallResult(candidates=1, filtered=1)
    monkeypatch.setattr(builder.memory, "recall_structured", lambda _query: result)
    monkeypatch.setattr(
        builder.memory,
        "write_recall_audit",
        lambda recall_query, recall_result: written.append((recall_query, recall_result)),
    )

    builder.build_system_prompt(recall_query="private governed query")

    assert len(written) == 1
    assert written[0][0].query_text == "private governed query"
    assert written[0][1] is result
