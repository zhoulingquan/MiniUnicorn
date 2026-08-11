"""Command tests for governed structured memory management (design section 15)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from miniunicorn.agent.memory_models import MemoryStatus
from miniunicorn.bus.events import InboundMessage
from miniunicorn.command.router import CommandContext, CommandRouter
from miniunicorn.config.schema import StructuredMemoryConfig


@pytest.fixture
def workspace(tmp_path):
    return tmp_path


def _store(workspace, mode: str = "governed"):
    from miniunicorn.agent.memory import MemoryStore

    return MemoryStore(workspace, structured_config=StructuredMemoryConfig(mode=mode))


def _router() -> CommandRouter:
    from miniunicorn.command.builtin import register_builtin_commands

    router = CommandRouter()
    register_builtin_commands(router)
    return router


async def _dispatch(router: CommandRouter, store, raw: str) -> str:
    loop = SimpleNamespace(context=SimpleNamespace(memory=store))
    msg = InboundMessage(channel="test", sender_id="u1", chat_id="c1", content=raw)
    ctx = CommandContext(msg=msg, session=None, key="k1", raw=raw, loop=loop)
    result = await router.dispatch(ctx)
    assert result is not None, f"unhandled command: {raw}"
    return result.content


def _seed(workspace) -> None:
    (workspace / "memory").mkdir(exist_ok=True)
    (workspace / "memory" / "MEMORY.md").write_text("## Decision\n- 用 SQLite 做存储\n", encoding="utf-8")
    store = _store(workspace)
    store.run_migration()
    return store


class TestStatus:
    async def test_status_shows_mode_health_counts_and_migration(self, workspace):
        store = _seed(workspace)
        router = _router()
        content = await _dispatch(router, store, "/memory-status")
        assert "Mode: `governed`" in content
        assert "Health: `healthy`" in content
        assert "candidate=0 active=1" in content
        assert "Migration: completed" in content

    async def test_status_pending_migration(self, workspace):
        store = _store(workspace)
        router = _router()
        content = await _dispatch(router, store, "/memory-status")
        assert "Migration: pending" in content

    async def test_status_legacy_mode(self, workspace):
        store = _store(workspace, mode="legacy")
        router = _router()
        content = await _dispatch(router, store, "/memory-status")
        assert "not active" in content


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
                    actor=ActorKind.MIGRATION,
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
        assert "evidence 1: kind=`file` ref=`memory/MEMORY.md#L2`" in content
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

        evidence = EvidenceRef(kind=EvidenceKind.MODEL_INFERENCE, ref="dream#L9", excerpt="不同内容")
        proposal = CandidateProposal(
            proposal_index=0,
            kind=MemoryKind.DECISION,
            scope_hint=ScopeKind.PROJECT,
            subject="Decision",
            slot="general",
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
        content = await _dispatch(router, store, f"/memory-promote {result.candidate_id} --replace {active.id}")
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


class TestMigrate:
    async def test_migrate_dry_run_zero_writes(self, workspace):
        (workspace / "memory").mkdir(exist_ok=True)
        (workspace / "memory" / "MEMORY.md").write_text("- 事实A\n", encoding="utf-8")
        store = _store(workspace)
        router = _router()
        before = {p: "dir" if p.is_dir() else "file" for p in workspace.rglob("*")}
        content = await _dispatch(router, store, "/memory-migrate --dry-run")
        assert "## Migration dry-run" in content
        assert "Scanned: 1 importable: 1" in content
        assert "Run `/memory-migrate --apply`" in content
        assert {p: "dir" if p.is_dir() else "file" for p in workspace.rglob("*")} == before
        assert store.structured_repository.current_records() == ()

    async def test_migrate_apply(self, workspace):
        (workspace / "memory").mkdir(exist_ok=True)
        (workspace / "memory" / "MEMORY.md").write_text("- 事实A\n", encoding="utf-8")
        store = _store(workspace)
        router = _router()
        content = await _dispatch(router, store, "/memory-migrate --apply")
        assert "## Migration applied" in content
        assert "Imported: 1" in content
        assert "completed_at" in content
        assert len(store.structured_repository.current_records()) == 1

    async def test_migrate_usage(self, workspace):
        store = _store(workspace)
        router = _router()
        content = await _dispatch(router, store, "/memory-migrate --bogus")
        assert "Usage:" in content
