"""ContextBuilder structured memory injection tests: legacy/shadow/governed."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from miniunicorn.agent.context import ContextBuilder
from miniunicorn.agent.memory import MemoryStore
from miniunicorn.agent.memory_lifecycle import IngestContext
from miniunicorn.agent.memory_models import (
    ActorKind,
    EvidenceKind,
    EvidenceRef,
    MemoryScope,
    RecallResult,
    ScopeKind,
)
from miniunicorn.config.schema import StructuredMemoryConfig

UTC = timezone.utc

RECALL_HEADER = "# Recalled Memory (Deterministic)"


def make_proposal(statement: str, slot: str = "memory.retrieval.strategy", **overrides):
    p = {
        "proposal_index": 0,
        "kind": "decision",
        "scope_hint": "project",
        "subject": "MiniUnicorn",
        "slot": slot,
        "statement": statement,
        "detail": "",
        "tags": ["architecture.memory"],
        "aliases": [],
        "confidence": 1.0,
        "importance": 5,
        "evidence_refs": ["history:1"],
        "speech_act": "confirmed_decision",
        "expires_at": None,
    }
    p.update(overrides)
    return p


def seed_active_record(
    store: MemoryStore,
    statement: str,
    slot: str = "memory.retrieval.strategy",
    scope: MemoryScope | None = None,
):
    """Ingest a proposal and promote it to ACTIVE via the lifecycle."""
    evidence_catalog = {
        "history:1": EvidenceRef(
            kind=EvidenceKind.HISTORY,
            ref="history:1",
            excerpt=statement,
            observed_at=datetime(2026, 8, 11, 8, 30, tzinfo=UTC),
        )
    }
    from miniunicorn.agent.memory_extraction import parse_extraction_batch

    extracted = parse_extraction_batch(
        json.dumps({"schema_version": 1, "proposals": [make_proposal(statement, slot)]}),
        evidence_catalog,
        store.structured_repository.tag_catalog,
    )
    context = IngestContext(
        actor=ActorKind.DREAM,
        reason="test seed",
        source_batch=f"seed:{statement}",
        scope=scope or MemoryScope(kind=ScopeKind.PROJECT, key=store.project_scope_key),
        evidence_catalog=evidence_catalog,
        now=datetime.now(UTC),
    )
    result = store.structured_lifecycle.ingest(extracted.proposals[0], context)
    store.structured_lifecycle.promote(
        result.candidate_id,
        actor=ActorKind.SYSTEM,
        reason="test seed promote",
    )


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Agent\n- Workflow rule\n", encoding="utf-8")
    (tmp_path / "SOUL.md").write_text("# Soul\n- Helpful\n", encoding="utf-8")
    (tmp_path / "USER.md").write_text("# User\n- Alice the developer\n", encoding="utf-8")
    store = MemoryStore(tmp_path)
    store.write_memory("# Memory\n- Legacy memory fact\n")
    return tmp_path


def make_builder(workspace, mode: str | None, **kwargs) -> ContextBuilder:
    config = None if mode is None else StructuredMemoryConfig(mode=mode, **kwargs)
    return ContextBuilder(workspace, structured_memory_config=config)


class TestGovernedMode:
    def test_omits_legacy_memory_and_shared(self, workspace):
        shared = workspace / "memory" / "shared" / "MEMORY_SHARED.md"
        shared.parent.mkdir(parents=True, exist_ok=True)
        shared.write_text("- shared legacy fact\n", encoding="utf-8")
        builder = make_builder(workspace, "governed")

        prompt = builder.build_system_prompt()

        assert "Legacy memory fact" not in prompt
        assert "shared legacy fact" not in prompt

    def test_omits_user_bootstrap_keeps_agent_and_soul(self, workspace):
        builder = make_builder(workspace, "governed")

        prompt = builder.build_system_prompt()

        assert "Alice the developer" not in prompt
        assert "Workflow rule" in prompt
        assert "Helpful" in prompt

    def test_skips_recall_without_query(self, workspace):
        builder = make_builder(workspace, "governed")

        prompt = builder.build_system_prompt()

        assert RECALL_HEADER not in prompt

    def test_injects_recall_hits(self, workspace):
        builder = make_builder(workspace, "governed")
        seed_active_record(builder.memory, "Main uses deterministic structured recall.")

        prompt = builder.build_system_prompt(
            recall_query="architecture.memory recall strategy"
        )

        assert RECALL_HEADER in prompt
        assert "deterministic structured recall" in prompt

    def test_injects_custom_policy(self, workspace):
        policy = workspace / "memory" / "shared" / "POLICY.md"
        policy.parent.mkdir(parents=True, exist_ok=True)
        policy.write_text("Never modify production configs without approval.\n", encoding="utf-8")
        builder = make_builder(workspace, "governed")

        prompt = builder.build_system_prompt()

        assert "Never modify production configs without approval" in prompt

    def test_build_messages_feeds_current_message_as_recall_query(self, workspace):
        builder = make_builder(workspace, "governed")
        seed_active_record(builder.memory, "MiniUnicorn recall stays local without embeddings.")

        messages = builder.build_messages(
            history=[], current_message="how does MiniUnicorn recall memory?"
        )

        system = messages[0]["content"]
        assert RECALL_HEADER in system
        assert "stays local" in system

    def test_governed_recall_degraded_injects_diagnostic_without_facts(
        self, workspace, monkeypatch
    ):
        builder = make_builder(workspace, "governed")
        monkeypatch.setattr(
            builder.memory,
            "recall_structured",
            lambda _query: RecallResult(
                degraded=True,
                error_code="journal_corrupt",
                error_message="invalid transaction",
            ),
        )

        prompt = builder.build_system_prompt(recall_query="MiniUnicorn memory")

        assert "Structured memory recall is unavailable" in prompt
        assert "journal_corrupt" in prompt
        assert "invalid transaction" not in prompt
        assert RECALL_HEADER not in prompt

    def test_build_messages_recall_includes_exact_session_and_user_scopes(self, workspace):
        builder = make_builder(workspace, "governed")
        seed_active_record(
            builder.memory,
            "Alice prefers compact responses.",
            slot="response.style",
            scope=MemoryScope(kind=ScopeKind.USER, key="user:alice"),
        )
        seed_active_record(
            builder.memory,
            "This session is debugging caching.",
            slot="session.topic",
            scope=MemoryScope(kind=ScopeKind.SESSION, key="session:web:chat-7"),
        )

        messages = builder.build_messages(
            history=[],
            current_message="MiniUnicorn Alice caching response session",
            sender_id="alice",
            session_key="web:chat-7",
        )

        system = messages[0]["content"]
        assert "Alice prefers compact responses" in system
        assert "This session is debugging caching" in system

    def test_build_messages_uses_default_user_scope_without_sender(self, workspace):
        builder = make_builder(workspace, "governed")
        seed_active_record(
            builder.memory,
            "Default user prefers Chinese.",
            slot="response.language",
            scope=MemoryScope(kind=ScopeKind.USER, key="user:default"),
        )

        messages = builder.build_messages(
            history=[], current_message="MiniUnicorn default user language", session_key="cli:direct"
        )

        assert "Default user prefers Chinese" in messages[0]["content"]

    def test_subagent_scope_uses_parent_session_and_user_identity(self, workspace):
        builder = make_builder(workspace, "governed")
        seed_active_record(
            builder.memory,
            "Parent user wants terse output.",
            slot="response.style",
            scope=MemoryScope(kind=ScopeKind.USER, key="user:alice"),
        )

        messages = builder.build_messages(
            history=[{"role": "user", "content": "task", "sender_id": "alice"}],
            current_message="MiniUnicorn parent user terse",
            sender_id="subagent",
            session_key="web:chat-7#sub:task-1",
            memory_user_key="user:alice",
        )

        assert "Parent user wants terse output" in messages[0]["content"]


class TestShadowMode:
    def test_keeps_legacy_injection_without_recall(self, workspace):
        shared = workspace / "memory" / "shared" / "MEMORY_SHARED.md"
        shared.parent.mkdir(parents=True, exist_ok=True)
        shared.write_text("- shared legacy fact\n", encoding="utf-8")
        builder = make_builder(workspace, "shadow")

        prompt = builder.build_system_prompt(recall_query="anything")

        assert "Legacy memory fact" in prompt
        assert "shared legacy fact" in prompt
        assert "Alice the developer" in prompt
        assert RECALL_HEADER not in prompt

    def test_audit_logs_without_query_text(self, workspace, monkeypatch):
        builder = make_builder(workspace, "shadow", recall_audit_enabled=True)
        seed_active_record(builder.memory, "Audit target fact about caching.")
        logged: list[str] = []
        monkeypatch.setattr(
            "miniunicorn.agent.context.logger.info", lambda msg, *args, **kwargs: logged.append(str(msg))
        )

        builder.build_system_prompt(recall_query="tell me about caching")

        assert any("structured_recall_shadow" in line for line in logged)
        assert all("tell me about caching" not in line for line in logged)

    def test_audit_disabled_skips_recall(self, workspace, monkeypatch):
        builder = make_builder(workspace, "shadow", recall_audit_enabled=False)
        logged: list[str] = []
        monkeypatch.setattr(
            "miniunicorn.agent.context.logger.info", lambda msg, *args, **kwargs: logged.append(str(msg))
        )

        builder.build_system_prompt(recall_query="anything")

        assert not any("structured_recall_shadow" in line for line in logged)


class TestLegacyMode:
    def test_unaffected_by_structured_changes(self, workspace):
        builder = make_builder(workspace, None)

        prompt = builder.build_system_prompt()

        assert "Legacy memory fact" in prompt
        assert "Alice the developer" in prompt
        assert "Workflow rule" in prompt
        assert RECALL_HEADER not in prompt
        assert "Shared Policy" not in prompt
