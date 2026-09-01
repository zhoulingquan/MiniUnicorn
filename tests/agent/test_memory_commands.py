"""Command tests for governed structured memory management (design section 15)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from miniunicorn.agent.memory_models import MemoryKind, MemoryScope, MemoryStatus, ScopeKind
from miniunicorn.bus.events import InboundMessage
from miniunicorn.command.router import CommandContext, CommandRouter
from miniunicorn.config.schema import StructuredMemoryConfig


@pytest.fixture
def workspace(tmp_path):
    return tmp_path


def _store(workspace):
    from miniunicorn.agent.memory import MemoryStore

    return MemoryStore(workspace, structured_config=StructuredMemoryConfig())


def _router() -> CommandRouter:
    from miniunicorn.command.builtin import register_builtin_commands

    router = CommandRouter()
    register_builtin_commands(router)
    return router


async def _dispatch(
    router: CommandRouter,
    store,
    raw: str,
    *,
    message_id: str = "msg-test",
    session_key: str = "k1",
    sender_id: str = "u1",
) -> str:
    loop = SimpleNamespace(context=SimpleNamespace(memory=store))
    msg = InboundMessage(
        channel="test",
        sender_id=sender_id,
        chat_id="c1",
        content=raw,
        metadata={"message_id": message_id},
    )
    ctx = CommandContext(msg=msg, session=None, key=session_key, raw=raw, loop=loop)
    result = await router.dispatch(ctx)
    assert result is not None, f"unhandled command: {raw}"
    return result.content


def _seed_scope(
    store,
    scope,
    statement,
    *,
    subject="Decision",
    slot="general",
    kind=MemoryKind.DECISION,
    promote=False,
    excerpt=None,
):
    """Ingest a record in an explicit scope; optionally promote it to active."""
    from miniunicorn.agent.memory_lifecycle import IngestContext
    from miniunicorn.agent.memory_models import (
        ActorKind,
        CandidateProposal,
        EvidenceKind,
        EvidenceRef,
        SourceLevel,
    )

    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    evidence = EvidenceRef(
        kind=EvidenceKind.MODEL_INFERENCE,
        ref="seed",
        excerpt=excerpt if excerpt is not None else statement,
    )
    proposal = CandidateProposal(
        proposal_index=0,
        kind=kind,
        scope_hint=scope.kind,
        subject=subject,
        slot=slot,
        statement=statement,
        tags=("project.fact",),
        confidence=0.5 if not promote else 0.9,
        importance=3,
        evidence_refs=("src:0",),
        speech_act=SourceLevel.INFERRED,
    )
    result = store.structured_lifecycle.ingest(
        proposal,
        IngestContext(
            actor=ActorKind.DREAM,
            reason="seed",
            source_batch="seed:test",
            scope=scope,
            evidence_catalog={"src:0": evidence},
            now=now,
        ),
    )
    if promote:
        store.structured_lifecycle.promote(
            result.candidate_id, actor=ActorKind.SYSTEM, reason="seed promote"
        )
    return store.structured_repository.get(result.candidate_id)


def _real_loop(workspace):
    """Build a production AgentLoop so the real WorkspaceScopeResolver runs."""
    from miniunicorn.agent.loop import AgentLoop
    from miniunicorn.bus.queue import MessageBus

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = SimpleNamespace(max_tokens=0)
    provider.estimate_prompt_tokens.return_value = (1_000, "test")
    return AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=workspace,
        model="test-model",
    )


def _scoped_session(loop, workspace):
    session = loop.sessions.get_or_create("websocket:chat-b")
    session.metadata["workspace_scope"] = {"project_path": str(workspace), "access_mode": "full"}
    return session


async def _dispatch_loop(router, loop, session, raw: str) -> str:
    msg = InboundMessage(
        channel="websocket",
        sender_id="u1",
        chat_id="chat-b",
        content=raw,
        metadata={"message_id": "msg-test"},
    )
    ctx = CommandContext(msg=msg, session=session, key=session.key, raw=raw, loop=loop)
    result = await router.dispatch(ctx)
    assert result is not None, f"unhandled command: {raw}"
    return result.content


def _seed(workspace):
    store = _store(workspace)
    from miniunicorn.agent.memory_lifecycle import IngestContext
    from miniunicorn.agent.memory_models import (
        ActorKind,
        CandidateProposal,
        EvidenceKind,
        EvidenceRef,
        MemoryKind,
        MemoryScope,
        ScopeKind,
        SourceLevel,
    )

    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    evidence = EvidenceRef(
        kind=EvidenceKind.FILE,
        ref="config/database.toml#L2",
        excerpt="用 SQLite 做存储",
        sha256=hashlib.sha256("用 SQLite 做存储".encode()).hexdigest(),
    )
    proposal = CandidateProposal(
        proposal_index=0,
        kind=MemoryKind.DECISION,
        scope_hint=ScopeKind.PROJECT,
        subject="Decision",
        slot="general",
        statement="用 SQLite 做存储",
        tags=("project.decision",),
        confidence=0.9,
        importance=4,
        evidence_refs=("src:0",),
        speech_act=SourceLevel.INFERRED,
    )
    result = store.structured_lifecycle.ingest(
        proposal,
        IngestContext(
            actor=ActorKind.SYSTEM,
            reason="seed",
            source_batch="seed:test",
            scope=MemoryScope(kind=ScopeKind.PROJECT, key=store.project_scope_key),
            evidence_catalog={"src:0": evidence},
            now=now,
        ),
    )
    assert result.final_status is MemoryStatus.ACTIVE
    return store


class TestStatus:
    async def test_status_shows_architecture_health_and_counts(self, workspace):
        store = _seed(workspace)
        router = _router()
        content = await _dispatch(router, store, "/memory-status")
        assert "Architecture: `governed`" in content
        assert "Backend: `sqlite`" in content
        assert "Schema: `v1`" in content
        assert "Health: `healthy`" in content
        assert "Transactions: `2`" in content
        assert "Revisions: `" in content
        assert "Current: `" in content
        assert "Database size:" in content
        assert "Audit exported seq:" in content
        assert "candidate=0 active=1" in content
        assert "Migration:" in content

    async def test_status_fresh_store_shows_not_needed_migration(self, workspace):
        store = _store(workspace)
        router = _router()
        content = await _dispatch(router, store, "/memory-status")
        assert "Architecture: `governed`" in content
        assert "Migration: `not_needed`" in content


class TestList:
    async def test_list_shows_fields_and_default_limit(self, workspace):
        store = _seed(workspace)
        router = _router()
        content = await _dispatch(router, store, "/memory-list")
        assert "## Memory records (1)" in content
        assert "mem_" in content
        assert "`active` `decision` `project:" in content
        assert "用 SQLite 做存储" in content

    async def test_list_status_filter(self, workspace):
        store = _seed(workspace)
        router = _router()
        content = await _dispatch(router, store, "/memory-list active")
        assert "## Memory records (1)" in content
        content = await _dispatch(router, store, "/memory-list candidate")
        assert "## Memory records (0)" in content
        content = await _dispatch(router, store, "/memory-list bogus")
        assert "Usage:" in content

    async def test_list_caps_at_20(self, workspace):
        from miniunicorn.agent.memory_lifecycle import IngestContext
        from miniunicorn.agent.memory_models import (
            ActorKind,
            CandidateProposal,
            EvidenceKind,
            EvidenceRef,
            MemoryKind,
            MemoryScope,
            ScopeKind,
            SourceLevel,
        )

        store = _store(workspace)
        lifecycle = store.structured_lifecycle
        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        for index in range(25):
            proposal = CandidateProposal(
                proposal_index=index,
                kind=MemoryKind.FACT,
                scope_hint=ScopeKind.PROJECT,
                subject=f"subject-{index}",
                slot="general",
                statement=f"事实编号 {index}",
                tags=("project.fact",),
                confidence=0.9,
                importance=3,
                evidence_refs=("src:0",),
                speech_act=SourceLevel.INFERRED,
            )
            evidence = EvidenceRef(
                kind=EvidenceKind.MODEL_INFERENCE,
                ref=f"dream#L{index}",
                excerpt=f"事实编号 {index}",
            )
            lifecycle.ingest(
                proposal,
                IngestContext(
                    actor=ActorKind.SYSTEM,
                    reason="test",
                    source_batch="test:seed",
                    scope=MemoryScope(kind=ScopeKind.PROJECT, key=store.project_scope_key),
                    evidence_catalog={"src:0": evidence},
                    now=now,
                ),
            )
        router = _router()
        content = await _dispatch(router, store, "/memory-list candidate")
        assert "## Memory records (25)" in content
        assert "more" in content


class TestShow:
    async def test_show_revisions_and_evidence_excerpt_truncated(self, workspace):
        store = _seed(workspace)
        router = _router()
        record = store.structured_repository.current_records()[0]
        content = await _dispatch(router, store, f"/memory-show {record.id}")
        assert "### rev 1 `candidate`" in content
        assert "### rev 2 `active`" in content
        assert "kind: `decision`" in content
        assert "用 SQLite 做存储" in content
        assert "evidence 1: kind=`file` ref=`config/database.toml#L2`" in content
        assert "supersedes" not in content

    async def test_show_unknown_id(self, workspace):
        store = _seed(workspace)
        router = _router()
        content = await _dispatch(router, store, "/memory-show mem_nonexistent")
        assert "No memory record" in content

    async def test_show_evidence_excerpt_limited_to_200_chars(self, workspace):
        store = _store(workspace)
        router = _router()
        long = "x" * 500
        from miniunicorn.agent.memory_lifecycle import IngestContext
        from miniunicorn.agent.memory_models import (
            ActorKind,
            CandidateProposal,
            EvidenceKind,
            EvidenceRef,
            MemoryKind,
            MemoryScope,
            ScopeKind,
            SourceLevel,
        )

        evidence = EvidenceRef(kind=EvidenceKind.USER_MESSAGE, ref="test", excerpt=long)
        proposal = CandidateProposal(
            proposal_index=0,
            kind=MemoryKind.FACT,
            scope_hint=ScopeKind.PROJECT,
            subject="s",
            slot="general",
            statement="短陈述",
            tags=("project.fact",),
            confidence=0.9,
            importance=3,
            evidence_refs=("src:0",),
            speech_act=SourceLevel.INFERRED,
        )
        store.structured_lifecycle.ingest(
            proposal,
            IngestContext(
                actor=ActorKind.USER,
                reason="test",
                source_batch="test",
                scope=MemoryScope(kind=ScopeKind.PROJECT, key=store.project_scope_key),
                evidence_catalog={"src:0": evidence},
                now=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            ),
        )
        record = store.structured_repository.current_records()[0]
        content = await _dispatch(router, store, f"/memory-show {record.id}")
        assert "excerpt=" in content
        assert "x" * 201 not in content


class TestPromote:
    async def test_promote_candidate(self, workspace):
        store = _store(workspace)
        router = _router()
        from miniunicorn.agent.memory_lifecycle import IngestContext
        from miniunicorn.agent.memory_models import (
            ActorKind,
            CandidateProposal,
            EvidenceKind,
            EvidenceRef,
            MemoryKind,
            MemoryScope,
            ScopeKind,
            SourceLevel,
        )

        evidence = EvidenceRef(kind=EvidenceKind.MODEL_INFERENCE, ref="dream#L1", excerpt="候选")
        proposal = CandidateProposal(
            proposal_index=0,
            kind=MemoryKind.FACT,
            scope_hint=ScopeKind.PROJECT,
            subject="候选主题",
            slot="general",
            statement="候选陈述",
            tags=("project.fact",),
            confidence=0.5,
            importance=3,
            evidence_refs=("src:0",),
            speech_act=SourceLevel.INFERRED,
        )
        result = store.structured_lifecycle.ingest(
            proposal,
            IngestContext(
                actor=ActorKind.DREAM,
                reason="dream",
                source_batch="dream:test",
                scope=MemoryScope(kind=ScopeKind.PROJECT, key=store.project_scope_key),
                evidence_catalog={"src:0": evidence},
                now=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            ),
        )
        assert result.final_status is MemoryStatus.CANDIDATE
        content = await _dispatch(router, store, f"/memory-promote {result.candidate_id}")
        assert "Promoted" in content
        assert store.structured_repository.get(result.candidate_id).status is MemoryStatus.ACTIVE

    async def test_promote_conflict_requires_replace(self, workspace):
        store = _seed(workspace)
        router = _router()
        active = store.structured_repository.current_records()[0]
        from miniunicorn.agent.memory_lifecycle import IngestContext
        from miniunicorn.agent.memory_models import (
            ActorKind,
            CandidateProposal,
            EvidenceKind,
            EvidenceRef,
            MemoryKind,
            MemoryScope,
            ScopeKind,
            SourceLevel,
        )

        evidence = EvidenceRef(
            kind=EvidenceKind.MODEL_INFERENCE, ref="dream#L9", excerpt="不同内容"
        )
        proposal = CandidateProposal(
            proposal_index=0,
            kind=MemoryKind.DECISION,
            scope_hint=ScopeKind.PROJECT,
            subject="Decision",
            slot=active.slot,
            statement="替换后的决策",
            tags=("project.decision",),
            confidence=0.9,
            importance=4,
            evidence_refs=("src:0",),
            speech_act=SourceLevel.INFERRED,
        )
        result = store.structured_lifecycle.ingest(
            proposal,
            IngestContext(
                actor=ActorKind.DREAM,
                reason="dream",
                source_batch="dream:test",
                scope=MemoryScope(kind=ScopeKind.PROJECT, key=store.project_scope_key),
                evidence_catalog={"src:0": evidence},
                now=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            ),
        )
        content = await _dispatch(router, store, f"/memory-promote {result.candidate_id}")
        assert "conflict with active" in content
        assert "--replace" in content
        assert active.id in content
        # 显式 --replace 成功
        content = await _dispatch(
            router, store, f"/memory-promote {result.candidate_id} --replace {active.id}"
        )
        assert "Promoted" in content

    async def test_promote_usage(self, workspace):
        store = _seed(workspace)
        router = _router()
        content = await _dispatch(router, store, "/memory-promote")
        assert "Usage:" in content


class TestRevoke:
    async def test_revoke_requires_reason(self, workspace):
        store = _seed(workspace)
        router = _router()
        record = store.structured_repository.current_records()[0]
        content = await _dispatch(router, store, f"/memory-revoke {record.id}")
        assert "Usage:" in content

    async def test_revoke_with_reason(self, workspace):
        store = _seed(workspace)
        router = _router()
        record = store.structured_repository.current_records()[0]
        content = await _dispatch(router, store, f"/memory-revoke {record.id} 过时了")
        assert "Revoked" in content
        assert store.structured_repository.get(record.id).status is MemoryStatus.REVOKED


class TestCorrect:
    async def test_correct_creates_and_promotes(self, workspace):
        store = _store(workspace)
        router = _router()
        content = await _dispatch(router, store, "/memory-correct 用户偏好|general|喜欢用英文交流")
        assert "Corrected" in content
        assert "active" in content
        records = store.structured_repository.current_records()
        assert len(records) == 1
        record = records[0]
        assert record.status is MemoryStatus.ACTIVE
        assert record.source_level.value == "explicit_correction"
        assert record.evidence[0].kind.value == "user_message"

    async def test_correct_usage(self, workspace):
        store = _store(workspace)
        router = _router()
        content = await _dispatch(router, store, "/memory-correct a|b")
        assert "Usage:" in content

    @pytest.mark.parametrize(
        "raw",
        [
            "/memory-correct |slot|statement",
            "/memory-correct subject| |statement",
            "/memory-correct subject|slot| ",
        ],
    )
    async def test_correct_requires_all_three_non_empty_fields(self, workspace, raw):
        store = _store(workspace)

        content = await _dispatch(_router(), store, raw)

        assert "Usage:" in content
        assert store.structured_repository.current_records() == ()

    async def test_correct_evidence_uses_inbound_message_id(self, workspace):
        store = _store(workspace)

        await _dispatch(
            _router(),
            store,
            "/memory-correct 用户偏好|general|喜欢用英文交流",
            message_id="om_abc123",
        )

        record = store.structured_repository.current_records()[0]
        assert record.evidence[0].ref == "command:om_abc123"


async def test_memory_migrate_absent_from_router(workspace):
    router = _router()
    assert not router.is_dispatchable_command("/memory-migrate")


class TestMalformedQuotes:
    @pytest.mark.parametrize(
        ("raw", "usage"),
        [
            ('/memory-show "', "/memory-show <id>"),
            ('/memory-promote "', "/memory-promote <id> [--replace <active-id>]"),
            ('/memory-revoke " reason', "/memory-revoke <id> <reason>"),
        ],
    )
    async def test_malformed_quote_returns_usage(self, workspace, raw, usage):
        store = _seed(workspace)

        content = await _dispatch(_router(), store, raw)

        assert "Usage:" in content
        assert usage in content


_NOT_FOUND_PREFIX = "No memory record with id"


def _project_scope(store):
    return MemoryScope(kind=ScopeKind.PROJECT, key=store.project_scope_key)


class TestScopeAuthorization:
    async def test_workspace_b_command_reads_and_writes_b_memory_only(self, tmp_path):
        a = tmp_path / "A"
        b = tmp_path / "B"
        a.mkdir()
        b.mkdir()
        loop = _real_loop(a)
        store_b = loop.memory_for(b)
        _seed_scope(store_b, _project_scope(store_b), "FACT B ONLY")
        _seed_scope(loop.context.memory, _project_scope(loop.context.memory), "FACT A ONLY")

        router = _router()
        session = _scoped_session(loop, b)

        content = await _dispatch_loop(router, loop, session, "/memory-list")
        assert "FACT B ONLY" in content
        assert "FACT A ONLY" not in content

        content = await _dispatch_loop(
            router, loop, session, "/memory-correct 用户偏好|general|英文交流"
        )
        assert "Corrected" in content
        assert any(
            "英文交流" in record.statement
            for record in store_b.structured_repository.current_records()
        )
        assert not any(
            "英文交流" in record.statement
            for record in loop.context.memory.structured_repository.current_records()
        )

    async def test_workspace_b_command_never_uses_default_store_for_show(self, tmp_path):
        a = tmp_path / "A"
        b = tmp_path / "B"
        a.mkdir()
        b.mkdir()
        loop = _real_loop(a)
        store_b = loop.memory_for(b)
        only_b = _seed_scope(store_b, _project_scope(store_b), "ONLY B RECORD", promote=True)

        router = _router()
        session = _scoped_session(loop, b)

        content = await _dispatch_loop(router, loop, session, f"/memory-show {only_b.id}")
        assert "ONLY B RECORD" in content
        assert "### rev" in content

    async def test_list_and_status_do_not_reveal_other_identity_records(self, workspace):
        store = _store(workspace)
        _seed_scope(store, _project_scope(store), "PROJECT FACT", promote=True)
        _seed_scope(
            store,
            MemoryScope(kind=ScopeKind.USER, key="user:other"),
            "OTHER USER FACT",
            promote=True,
        )
        _seed_scope(
            store,
            MemoryScope(kind=ScopeKind.SESSION, key="session:other-session"),
            "OTHER SESSION FACT",
            promote=True,
        )
        router = _router()

        content = await _dispatch(router, store, "/memory-list")
        assert "PROJECT FACT" in content
        assert "OTHER USER FACT" not in content
        assert "OTHER SESSION FACT" not in content

        status = await _dispatch(router, store, "/memory-status")
        assert "active=1" in status

    async def test_show_hides_unauthorized_record_and_its_evidence(self, workspace):
        store = _store(workspace)
        other = _seed_scope(
            store,
            MemoryScope(kind=ScopeKind.SESSION, key="session:other-session"),
            "OTHER SESSION FACT",
            promote=True,
            excerpt="SECRET EVIDENCE",
        )
        router = _router()

        content = await _dispatch(router, store, f"/memory-show {other.id}")

        assert content == f"No memory record with id `{other.id}`."
        assert "SECRET EVIDENCE" not in content
        assert "OTHER SESSION FACT" not in content

    async def test_show_skips_revisions_from_a_different_scope(self, workspace):
        from miniunicorn.agent.memory_models import (
            ActorKind,
            EvidenceKind,
            EvidenceRef,
            MemoryOperation,
            MemoryTransaction,
            new_transaction_id,
            transaction_checksum,
        )

        store = _store(workspace)
        candidate = _seed_scope(
            store,
            MemoryScope(kind=ScopeKind.SESSION, key="session:other-session"),
            "CANDIDATE FROM OTHER SCOPE",
            excerpt="LEAKED SECRET",
        )
        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        corrupted = candidate.model_copy(
            update={
                "revision": candidate.revision + 1,
                "status": MemoryStatus.ACTIVE,
                "scope": _project_scope(store),
                "evidence": (
                    EvidenceRef(kind=EvidenceKind.HISTORY, ref="clean", excerpt="CLEAN EVIDENCE"),
                ),
                "blocked_by": (),
                "updated_at": now,
                "status_reason": "corrupt history",
            }
        )
        tx = MemoryTransaction(
            tx_id=new_transaction_id(),
            recorded_at=now,
            actor=ActorKind.SYSTEM,
            reason="corrupt history",
            source_batch="",
            expected_revisions={candidate.id: candidate.revision},
            operations=(MemoryOperation(record=corrupted),),
            checksum_sha256="0" * 64,
        )
        tx = tx.model_copy(update={"checksum_sha256": transaction_checksum(tx)})
        store.structured_repository.append_transaction(tx)
        assert store.structured_repository.get(candidate.id).scope == _project_scope(store)

        router = _router()
        content = await _dispatch(router, store, f"/memory-show {candidate.id}")

        assert "### rev 2 `active`" in content
        assert "CLEAN EVIDENCE" in content
        assert "### rev 1" not in content
        assert "LEAKED SECRET" not in content

    async def test_promote_and_revoke_cannot_mutate_other_identity_records(self, workspace):
        store = _store(workspace)
        other_candidate = _seed_scope(
            store,
            MemoryScope(kind=ScopeKind.SESSION, key="session:other-session"),
            "OTHER CANDIDATE",
        )
        other_active = _seed_scope(
            store, MemoryScope(kind=ScopeKind.USER, key="user:other"), "OTHER ACTIVE", promote=True
        )
        router = _router()

        promote_content = await _dispatch(router, store, f"/memory-promote {other_candidate.id}")
        assert promote_content == f"No memory record with id `{other_candidate.id}`."
        assert store.structured_repository.get(other_candidate.id).status is MemoryStatus.CANDIDATE

        revoke_content = await _dispatch(router, store, f"/memory-revoke {other_active.id} 过时")
        assert revoke_content == f"No memory record with id `{other_active.id}`."
        assert store.structured_repository.get(other_active.id).status is MemoryStatus.ACTIVE

    async def test_promote_replace_cannot_reference_unauthorized_replacement(self, workspace):
        store = _store(workspace)
        active = _seed_scope(store, _project_scope(store), "ACTIVE A", promote=True)
        candidate = _seed_scope(
            store,
            _project_scope(store),
            "CANDIDATE C",
            subject="Decision",
            slot=active.slot,
            kind=MemoryKind.DECISION,
        )
        unauthorized_active = _seed_scope(
            store,
            MemoryScope(kind=ScopeKind.SESSION, key="session:other-session"),
            "OTHER ACTIVE X",
            promote=True,
        )
        router = _router()

        content = await _dispatch(
            router, store, f"/memory-promote {candidate.id} --replace {unauthorized_active.id}"
        )

        assert content == f"No memory record with id `{unauthorized_active.id}`."
        assert store.structured_repository.get(candidate.id).status is MemoryStatus.CANDIDATE
        assert store.structured_repository.get(active.id).status is MemoryStatus.ACTIVE
        assert store.structured_repository.get(unauthorized_active.id).status is MemoryStatus.ACTIVE

    async def test_project_user_session_and_shared_scopes_remain_accessible(self, workspace):
        store = _store(workspace)
        _seed_scope(store, _project_scope(store), "PROJECT FACT", promote=True)
        _seed_scope(
            store, MemoryScope(kind=ScopeKind.USER, key="user:u1"), "MY USER FACT", promote=True
        )
        _seed_scope(
            store,
            MemoryScope(kind=ScopeKind.SESSION, key="session:k1"),
            "MY SESSION FACT",
            promote=True,
        )
        _seed_scope(
            store, MemoryScope(kind=ScopeKind.SHARED, key="shared:*"), "SHARED FACT", promote=True
        )
        router = _router()

        content = await _dispatch(router, store, "/memory-list")

        for expected in ("PROJECT FACT", "MY USER FACT", "MY SESSION FACT", "SHARED FACT"):
            assert expected in content

    async def test_forked_session_key_canonicalizes_like_recall(self, workspace):
        store = _store(workspace)
        _seed_scope(
            store,
            MemoryScope(kind=ScopeKind.SESSION, key="session:base"),
            "CANONICAL SESSION FACT",
            promote=True,
        )
        _seed_scope(
            store,
            MemoryScope(kind=ScopeKind.SESSION, key="session:base#fork"),
            "FORKED SESSION FACT",
            promote=True,
        )
        router = _router()

        content = await _dispatch(router, store, "/memory-list", session_key="base#fork")

        assert "CANONICAL SESSION FACT" in content
        assert "FORKED SESSION FACT" not in content

    async def test_missing_and_unauthorized_ids_have_identical_not_found(self, workspace):
        store = _store(workspace)
        other = _seed_scope(
            store,
            MemoryScope(kind=ScopeKind.SESSION, key="session:other-session"),
            "OTHER FACT",
            promote=True,
        )
        router = _router()

        missing = await _dispatch(router, store, "/memory-show mem_nonexistent")
        unauthorized = await _dispatch(router, store, f"/memory-show {other.id}")
        assert missing == f"{_NOT_FOUND_PREFIX} `mem_nonexistent`."
        assert unauthorized == f"{_NOT_FOUND_PREFIX} `{other.id}`."

        missing_promote = await _dispatch(router, store, "/memory-promote mem_nonexistent")
        unauthorized_promote = await _dispatch(router, store, f"/memory-promote {other.id}")
        assert missing_promote == f"{_NOT_FOUND_PREFIX} `mem_nonexistent`."
        assert unauthorized_promote == f"{_NOT_FOUND_PREFIX} `{other.id}`."

        missing_revoke = await _dispatch(router, store, "/memory-revoke mem_nonexistent 原因")
        unauthorized_revoke = await _dispatch(router, store, f"/memory-revoke {other.id} 原因")
        assert missing_revoke == f"{_NOT_FOUND_PREFIX} `mem_nonexistent`."
        assert unauthorized_revoke == f"{_NOT_FOUND_PREFIX} `{other.id}`."


class TestAuditExportTrigger:
    async def test_mutating_command_exports_audit_before_returning(self, workspace):
        store = _store(workspace)
        router = _router()
        candidate = _seed_scope(store, _project_scope(store), "PROJECT CANDIDATE")
        assert candidate.status is MemoryStatus.CANDIDATE

        content = await _dispatch(router, store, f"/memory-promote {candidate.id}")

        assert "Promoted" in content
        stats = store.structured_repository.storage_stats()
        assert stats.transaction_count == 2
        assert stats.audit_lag == 0
        manifest = json.loads(
            (Path(workspace) / "memory" / "structured" / "audit" / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        assert manifest["database_last_tx_seq"] == stats.last_transaction_seq

    async def test_failed_command_does_not_fail_on_export(self, workspace, monkeypatch):
        store = _store(workspace)
        router = _router()
        candidate = _seed_scope(store, _project_scope(store), "PROJECT CANDIDATE")

        def boom(*args, **kwargs):
            raise OSError("injected audit export failure")

        monkeypatch.setattr(store.audit_exporter, "export_pending", boom)

        content = await _dispatch(router, store, f"/memory-promote {candidate.id}")

        assert "Promoted" in content
        assert store.structured_repository.storage_stats().audit_lag == 2


class TestMemoryLog:
    async def test_log_default_lists_recent_transactions_without_evidence(self, workspace):
        store = _seed(workspace)
        router = _router()
        content = await _dispatch(router, store, "/memory-log")
        assert "## Memory transaction log (2)" in content
        assert "`mtx_" in content
        assert "actor=`system`" in content
        assert "reason=seed" in content
        assert "records=`mem_" in content
        assert "excerpt" not in content
        assert "用 SQLite 做存储" not in content

    async def test_log_with_tx_id_shows_operations_and_evidence_excerpt(self, workspace):
        store = _seed(workspace)
        router = _router()
        tx = store.structured_repository.transaction_log()[0]
        content = await _dispatch(router, store, f"/memory-log {tx.tx_id}")
        assert f"## Memory transaction {tx.tx_id}" in content
        assert "actor: `system`" in content
        assert "operation 1: `put`" in content
        assert "用 SQLite 做存储" in content
        assert "excerpt=" in content

    async def test_log_evidence_excerpt_truncated_to_200_chars(self, workspace):
        from miniunicorn.agent.memory_lifecycle import IngestContext
        from miniunicorn.agent.memory_models import (
            ActorKind,
            CandidateProposal,
            EvidenceKind,
            EvidenceRef,
            MemoryKind,
            MemoryScope,
            ScopeKind,
            SourceLevel,
        )

        store = _store(workspace)
        long = "x" * 500
        evidence = EvidenceRef(kind=EvidenceKind.USER_MESSAGE, ref="test", excerpt=long)
        proposal = CandidateProposal(
            proposal_index=0,
            kind=MemoryKind.FACT,
            scope_hint=ScopeKind.PROJECT,
            subject="s",
            slot="general",
            statement="短陈述",
            tags=("project.fact",),
            confidence=0.9,
            importance=3,
            evidence_refs=("src:0",),
            speech_act=SourceLevel.INFERRED,
        )
        store.structured_lifecycle.ingest(
            proposal,
            IngestContext(
                actor=ActorKind.USER,
                reason="test",
                source_batch="test",
                scope=MemoryScope(kind=ScopeKind.PROJECT, key=store.project_scope_key),
                evidence_catalog={"src:0": evidence},
                now=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            ),
        )
        tx = store.structured_repository.transaction_log()[0]
        content = await _dispatch(_router(), store, f"/memory-log {tx.tx_id}")
        assert "excerpt=" in content
        assert "x" * 201 not in content

    async def test_log_unknown_and_unauthorized_tx_ids_respond_identically(self, workspace):
        store = _store(workspace)
        hidden = _seed_scope(
            store,
            MemoryScope(kind=ScopeKind.USER, key="user:other"),
            "HIDDEN USER FACT",
            promote=True,
            excerpt="HIDDEN EVIDENCE",
        )
        hidden_tx = next(
            tx
            for tx in store.structured_repository.transaction_log()
            if any(op.record.id == hidden.id for op in tx.operations)
        )
        router = _router()
        missing_id = "mtx_" + "0" * 32
        missing = await _dispatch(router, store, f"/memory-log {missing_id}")
        unauthorized = await _dispatch(router, store, f"/memory-log {hidden_tx.tx_id}")
        assert missing == f"No memory transaction with id `{missing_id}`."
        assert unauthorized == f"No memory transaction with id `{hidden_tx.tx_id}`."
        assert "HIDDEN" not in unauthorized

    async def test_log_default_hides_transactions_of_other_identities(self, workspace):
        store = _store(workspace)
        _seed_scope(
            store,
            _project_scope(store),
            "VISIBLE PROJECT FACT",
            promote=True,
            subject="Visible",
        )
        _seed_scope(
            store,
            MemoryScope(kind=ScopeKind.USER, key="user:other"),
            "HIDDEN USER FACT",
            promote=True,
            subject="Hidden",
        )
        content = await _dispatch(_router(), store, "/memory-log")
        assert "## Memory transaction log (2)" in content
        assert "HIDDEN USER FACT" not in content
        assert "VISIBLE PROJECT FACT" not in content


class TestMemoryBackup:
    async def test_backup_creates_verified_snapshot_and_reports_id(self, workspace):
        import sqlite3

        store = _seed(workspace)
        content = await _dispatch(_router(), store, "/memory-backup")
        assert "## Memory backup created" in content
        assert "- Backup id: `memory-" in content
        assert "- SHA-256: `" in content
        assert "- Integrity: `ok`" in content
        backups = Path(workspace) / "memory" / "structured" / "backups"
        files = list(backups.glob("memory-*.db"))
        assert len(files) == 1
        with sqlite3.connect(files[0]) as connection:
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert connection.execute("SELECT COUNT(*) FROM memory_transactions").fetchone()[0] == 2

    async def test_backup_when_loop_missing_is_friendly(self, workspace):
        store = _seed(workspace)
        loop = SimpleNamespace(context=SimpleNamespace(memory=store))
        msg = InboundMessage(channel="test", sender_id="u1", chat_id="c1", content="/memory-backup")
        ctx = CommandContext(msg=msg, session=None, key="k1", raw="/memory-backup", loop=loop)
        result = await _router().dispatch(ctx)
        assert result is not None
        assert "Memory backup" in result.content


class TestMemoryRestore:
    async def test_restore_replaces_database_and_reports_safety_backup(self, workspace):
        from miniunicorn.agent.memory_backup import MemoryBackupManager

        store = _seed(workspace)
        manager = MemoryBackupManager(store.structured_repository)
        backup = manager.create_backup()
        _seed_scope(
            store,
            _project_scope(store),
            "SECOND SEED FACT",
            promote=True,
            subject="Second",
        )
        assert store.structured_repository.storage_stats().transaction_count == 4

        content = await _dispatch(_router(), store, f"/memory-restore {backup.backup_id}")

        assert "## Memory restored" in content
        assert f"- Backup: `{backup.backup_id}`" in content
        assert "- Safety backup: `recovery/" in content
        assert f"- Transaction seq: `{backup.last_transaction_seq}`" in content
        assert store.structured_repository.health.state == "healthy"
        assert store.structured_repository.storage_stats().transaction_count == 2

    async def test_restore_unknown_backup_id_reports_not_found(self, workspace):
        store = _seed(workspace)
        backup_id = "memory-2099-01-01T00-00-00Z-1.db"
        content = await _dispatch(_router(), store, f"/memory-restore {backup_id}")
        assert f"No memory backup with id `{backup_id}`." in content

    async def test_restore_rejects_malformed_backup_id(self, workspace):
        store = _seed(workspace)
        content = await _dispatch(_router(), store, "/memory-restore ../outside.db")
        assert "invalid backup id" in content

    async def test_restore_usage(self, workspace):
        store = _seed(workspace)
        content = await _dispatch(_router(), store, "/memory-restore")
        assert "Usage: /memory-restore <backup-id>" in content


class TestMemoryExportAudit:
    async def test_export_pending_flushes_lag(self, workspace):
        store = _store(workspace)
        _seed_scope(store, _project_scope(store), "PENDING FACT", promote=True)
        content = await _dispatch(_router(), store, "/memory-export-audit")
        assert "## Audit export" in content
        assert "- Rows: `2`" in content
        assert store.structured_repository.storage_stats().audit_lag == 0

    async def test_export_audit_full_rebuild(self, workspace):
        store = _seed(workspace)
        content = await _dispatch(_router(), store, "/memory-export-audit --rebuild")
        assert "## Audit export" in content
        assert store.structured_repository.storage_stats().audit_lag == 0
        manifest = json.loads(
            (Path(workspace) / "memory" / "structured" / "audit" / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        assert (
            manifest["database_last_tx_seq"]
            == store.structured_repository.storage_stats().last_transaction_seq
        )

    async def test_export_audit_usage(self, workspace):
        store = _seed(workspace)
        content = await _dispatch(_router(), store, "/memory-export-audit --bogus")
        assert "Usage: /memory-export-audit [--rebuild]" in content


class TestDiagnosticScopeIsolation:
    async def test_restore_cannot_target_another_workspace_backup(self, tmp_path):
        from miniunicorn.agent.memory_backup import MemoryBackupManager

        a = tmp_path / "A"
        b = tmp_path / "B"
        a.mkdir()
        b.mkdir()
        loop = _real_loop(a)
        store_a = loop.context.memory
        store_b = loop.memory_for(b)
        _seed_scope(store_a, _project_scope(store_a), "FACT IN A", promote=True)
        backup_a = MemoryBackupManager(store_a.structured_repository).create_backup()

        session = _scoped_session(loop, b)
        content = await _dispatch_loop(
            _router(), loop, session, f"/memory-restore {backup_a.backup_id}"
        )

        assert f"No memory backup with id `{backup_a.backup_id}`." in content
        assert store_b.structured_repository.storage_stats().transaction_count == 0

    async def test_backup_lands_in_effective_workspace(self, tmp_path):
        a = tmp_path / "A"
        b = tmp_path / "B"
        a.mkdir()
        b.mkdir()
        loop = _real_loop(a)
        session = _scoped_session(loop, b)
        content = await _dispatch_loop(_router(), loop, session, "/memory-backup")
        assert "- Backup id: `memory-" in content
        backups_b = b / "memory" / "structured" / "backups"
        assert list(backups_b.glob("memory-*.db"))
        backups_a = a / "memory" / "structured" / "backups"
        assert not backups_a.exists()
