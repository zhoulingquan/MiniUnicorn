"""Migration tests for governed structured memory (design section 14).

Covers deterministic mapping, idempotent legacy_key, zero-write dry-run,
journal-backed apply, completed_at semantics and the governed startup gate.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from miniunicorn.agent.memory_migration import (
    MIGRATION_STATE_FILE,
    MigrationState,
    scan_legacy_memory,
)
from miniunicorn.agent.memory_models import (
    EvidenceKind,
    MemoryKind,
    MemoryStatus,
    ScopeKind,
)
from miniunicorn.config.schema import StructuredMemoryConfig


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def bus():
    from miniunicorn.bus.queue import MessageBus

    return MessageBus()


@pytest.fixture
def provider():
    from unittest.mock import MagicMock

    from miniunicorn.providers.base import GenerationSettings

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=0)
    provider.estimate_prompt_tokens.return_value = (10_000, "test")
    return provider


def _store(workspace: Path, mode: str = "governed"):
    from miniunicorn.agent.memory import MemoryStore

    return MemoryStore(workspace, structured_config=StructuredMemoryConfig(mode=mode))


def _snapshot(workspace: Path) -> dict[str, str]:
    return {
        str(path.relative_to(workspace)).replace("\\", "/"): "dir" if path.is_dir() else "file"
        for path in workspace.rglob("*")
    }


def _write_user(workspace: Path) -> None:
    (workspace / "USER.md").write_text(
        "## Preference\n- 使用 English 回复\n- 项目用 uv 管理依赖\n\n## Identity\n- 我叫 OOOH\n",
        encoding="utf-8",
    )


def _write_memory(workspace: Path) -> None:
    (workspace / "memory").mkdir(exist_ok=True)
    (workspace / "memory" / "MEMORY.md").write_text(
        "## Decision\n- 用 SQLite 做存储\n\n## 需求\n- 支持离线模式\n\nParagraph without atomic boundary.\n",
        encoding="utf-8",
    )


def _write_procedural(workspace: Path) -> None:
    (workspace / "memory").mkdir(exist_ok=True)
    (workspace / "memory" / "procedural.jsonl").write_text(
        json.dumps({"content": "发布流程：先跑测试再打包"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _write_shared(workspace: Path) -> None:
    (workspace / "memory" / "shared").mkdir(parents=True, exist_ok=True)
    (workspace / "memory" / "shared" / "MEMORY_SHARED.md").write_text(
        "- 团队约定：代码必须通过 lint\n",
        encoding="utf-8",
    )


def _write_episodic(workspace: Path) -> None:
    (workspace / "memory").mkdir(exist_ok=True)
    (workspace / "memory" / "episodic.jsonl").write_text(
        json.dumps({"content": "本次会话解决了构建超时问题"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _write_procedural_shared(workspace: Path) -> None:
    (workspace / "memory" / "shared").mkdir(parents=True, exist_ok=True)
    (workspace / "memory" / "shared" / "procedural_shared.jsonl").write_text(
        json.dumps({"content": "全局发布检查清单"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _write_all_sources(workspace: Path) -> None:
    _write_user(workspace)
    _write_memory(workspace)
    _write_procedural(workspace)
    _write_shared(workspace)
    _write_episodic(workspace)
    _write_procedural_shared(workspace)


def _current(store) -> tuple:
    return store.structured_repository.current_records()  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Scanning / deterministic mapping
# ---------------------------------------------------------------------------


class TestScanMapping:
    def test_user_heading_mapping(self, workspace):
        _write_user(workspace)
        items, issues = scan_legacy_memory(workspace)
        by_subject = {item.subject: item for item in items}
        assert by_subject["Preference"].kind is MemoryKind.PREFERENCE
        assert by_subject["Preference"].tags == ("user.preference",)
        assert by_subject["Preference"].scope_kind is ScopeKind.USER
        assert by_subject["Identity"].kind is MemoryKind.IDENTITY
        assert by_subject["Identity"].tags == ("user.identity",)
        assert by_subject["Identity"].scope_kind is ScopeKind.USER
        assert not issues

    def test_user_chinese_heading_mapping(self, workspace):
        (workspace / "USER.md").write_text("## 偏好\n- 喜欢喝咖啡\n", encoding="utf-8")
        items, _ = scan_legacy_memory(workspace)
        assert len(items) == 1
        assert items[0].kind is MemoryKind.PREFERENCE
        assert items[0].tags == ("user.preference",)

    def test_memory_heading_mapping(self, workspace):
        _write_memory(workspace)
        items, issues = scan_legacy_memory(workspace)
        by_subject = {item.subject: item for item in items}
        assert by_subject["Decision"].kind is MemoryKind.DECISION
        assert by_subject["Decision"].tags == ("project.decision",)
        assert by_subject["Decision"].scope_kind is ScopeKind.PROJECT
        assert by_subject["需求"].kind is MemoryKind.FACT
        assert by_subject["需求"].tags == ("project.requirement",)
        assert any(issue.reason.startswith("non-atomic") for issue in issues)

    def test_memory_default_and_ordered_items(self, workspace):
        (workspace / "memory").mkdir(exist_ok=True)
        (workspace / "memory" / "MEMORY.md").write_text("- 默认事实\n1. 有序条目\n", encoding="utf-8")
        items, _ = scan_legacy_memory(workspace)
        assert len(items) == 2
        assert items[0].kind is MemoryKind.FACT
        assert items[0].tags == ("project.fact",)
        assert items[1].statement == "有序条目"

    def test_shared_and_jsonl_sources(self, workspace):
        _write_shared(workspace)
        _write_procedural(workspace)
        _write_procedural_shared(workspace)
        _write_episodic(workspace)
        items, issues = scan_legacy_memory(workspace)
        assert not issues
        kinds = {item.kind for item in items}
        scopes = {item.scope_kind for item in items}
        assert kinds == {
            MemoryKind.FACT,
            MemoryKind.PROCEDURE,
            MemoryKind.OUTCOME,
        }
        assert scopes == {ScopeKind.SHARED, ScopeKind.PROJECT}
        shared_items = [item for item in items if item.scope_kind is ScopeKind.SHARED]
        assert shared_items[0].tags == ("shared.fact",)
        procedure_shared = [item for item in shared_items if item.kind is MemoryKind.PROCEDURE]
        assert procedure_shared[0].tags == ("workflow.procedure", "shared.fact")
        episodic = [item for item in items if item.kind is MemoryKind.OUTCOME]
        assert episodic[0].tags == ("session.event",)
        assert episodic[0].confidence == 0.6

    def test_missing_sources_are_empty(self, workspace):
        items, issues = scan_legacy_memory(workspace)
        assert items == []
        assert issues == []

    def test_invalid_jsonl_reported(self, workspace):
        (workspace / "memory").mkdir(exist_ok=True)
        (workspace / "memory" / "procedural.jsonl").write_text("{not json}\n", encoding="utf-8")
        items, issues = scan_legacy_memory(workspace)
        assert items == []
        assert len(issues) == 1
        assert "invalid jsonl" in issues[0].reason

    def test_legacy_key_is_deterministic_and_positional(self, workspace):
        (workspace / "memory").mkdir(exist_ok=True)
        (workspace / "memory" / "MEMORY.md").write_text("- 甲\n- 乙\n", encoding="utf-8")
        first, _ = scan_legacy_memory(workspace)
        (workspace / "memory" / "MEMORY.md").write_text("- 甲\n", encoding="utf-8")
        second, _ = scan_legacy_memory(workspace)
        # 相同内容+相同位置 -> 相同 key；不同位置 -> 不同 key（positional）
        assert first[0].legacy_key() == second[0].legacy_key()
        assert first[0].legacy_key() != first[1].legacy_key()


# ---------------------------------------------------------------------------
# Dry-run: zero writes
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_zero_writes(self, workspace):
        _write_all_sources(workspace)
        _store(workspace)  # 构造 bundled TAGS/POLICY 后再快照
        before = _snapshot(workspace)
        store = _store(workspace)
        report = store.run_migration(dry_run=True)
        assert _snapshot(workspace) == before
        assert report.dry_run is True
        assert report.imported == 9  # user3 + memory2 + procedure + shared + procedure_shared + episodic
        assert report.failed == ()
        assert report.scanned == 9
        assert report.imported == report.scanned

    def test_dry_run_reports_oversized_statement(self, workspace):
        (workspace / "memory").mkdir(exist_ok=True)
        (workspace / "memory" / "MEMORY.md").write_text(f"- {'x' * 600}\n", encoding="utf-8")
        store = _store(workspace)
        before = _snapshot(workspace)
        report = store.run_migration(dry_run=True)
        assert _snapshot(workspace) == before
        assert report.imported == 0
        assert len(report.failed) == 1
        assert "statement" in report.failed[0].error

    def test_dry_run_does_not_create_journal_or_state(self, workspace):
        _write_all_sources(workspace)
        store = _store(workspace)
        store.run_migration(dry_run=True)
        assert not (workspace / "memory" / "structured" / "journal.jsonl").exists()
        assert not (workspace / MIGRATION_STATE_FILE).exists()


# ---------------------------------------------------------------------------
# Apply: journal-backed import, idempotent, completed_at semantics
# ---------------------------------------------------------------------------


class TestApply:
    def test_apply_imports_all_sources(self, workspace):
        _write_all_sources(workspace)
        store = _store(workspace)
        report = store.run_migration()
        assert report.dry_run is False
        assert report.failed == ()
        assert report.imported == 9
        assert report.completed_at is not None
        assert store.migration_completed() is True

        records = _current(store)
        assert len(records) == 9
        by_kind = {r.kind for r in records}
        assert by_kind == {MemoryKind.FACT, MemoryKind.PREFERENCE, MemoryKind.IDENTITY, MemoryKind.DECISION, MemoryKind.PROCEDURE, MemoryKind.OUTCOME}
        # 所有非 episodic 项都是 verified/repeated 来源且 auto-promote -> active；
        # 但第 2 条 preference 与第 1 条同 subject+slot，按冲突语义被 block（candidate）。
        active = {r for r in records if r.status is MemoryStatus.ACTIVE}
        assert len(active) == 7
        candidates = {r for r in records if r.status is MemoryStatus.CANDIDATE}
        assert len(candidates) == 2  # 1 冲突阻塞 + 1 episodic（inferred 永不提升）
        blocked = [r for r in records if r.status is MemoryStatus.CANDIDATE and r.blocked_by]
        assert len(blocked) == 1
        # episodic 保持 candidate（inferred 永不自动提升）
        outcome = [r for r in records if r.kind is MemoryKind.OUTCOME][0]
        assert outcome.status is MemoryStatus.CANDIDATE
        assert outcome.scope.kind is ScopeKind.PROJECT

    def test_apply_is_idempotent(self, workspace):
        _write_all_sources(workspace)
        store = _store(workspace)
        first = store.run_migration()
        second = store.run_migration()
        assert first.imported == 9
        assert second.imported == 0
        assert second.skipped == 9
        assert len(_current(store)) == 9
        state = MigrationState.load(workspace / MIGRATION_STATE_FILE)
        assert len(state.entries) == 9

    def test_failed_items_are_retried_on_next_run(self, workspace):
        _write_memory(workspace)
        store = _store(workspace)
        report = store.run_migration()
        assert report.failed == () and report.imported == 2
        # 第二条（需求）改为超限后，新 key 失败，旧 key 跳过
        (workspace / "memory" / "MEMORY.md").write_text("## Decision\n- 用 SQLite 做存储\n- " + "y" * 600 + "\n", encoding="utf-8")
        report = store.run_migration()
        assert report.skipped == 1
        assert len(report.failed) == 1
        # 修复后重试成功
        (workspace / "memory" / "MEMORY.md").write_text("## Decision\n- 用 SQLite 做存储\n- 修复后的条目\n", encoding="utf-8")
        report = store.run_migration()
        assert report.imported == 1
        assert report.failed == ()
        assert len(_current(store)) == 3

    def test_unknown_tag_fails_item_not_batch(self, workspace):
        (workspace / "memory").mkdir(exist_ok=True)
        (workspace / "memory" / "MEMORY.md").write_text("## Decision\n- 决策A\n- 决策B\n", encoding="utf-8")
        store = _store(workspace)
        report = store.run_migration(dry_run=True)
        assert report.imported == 2

    def test_migration_actor_and_scope(self, workspace):
        _write_user(workspace)
        store = _store(workspace)
        store.run_migration()
        record = _current(store)[0]
        assert record.scope.kind is ScopeKind.USER
        assert record.scope.key == "user:default"
        assert record.evidence[0].kind is EvidenceKind.FILE
        assert record.evidence[0].ref.startswith("USER.md#L")
        assert record.source_level.value == "verified"

    def test_procedure_gets_repeated_experience_level(self, workspace):
        _write_procedural(workspace)
        store = _store(workspace)
        store.run_migration()
        record = _current(store)[0]
        assert len(record.evidence) == 2
        assert record.source_level.value == "repeated_experience"
        assert record.status is MemoryStatus.ACTIVE

    def test_legacy_files_untouched(self, workspace):
        _write_all_sources(workspace)
        before = {
            path: path.read_bytes()
            for path in [
                workspace / "USER.md",
                workspace / "memory" / "MEMORY.md",
                workspace / "memory" / "procedural.jsonl",
                workspace / "memory" / "shared" / "MEMORY_SHARED.md",
                workspace / "memory" / "episodic.jsonl",
            ]
        }
        store = _store(workspace)
        store.run_migration()
        for path, content in before.items():
            assert path.read_bytes() == content


# ---------------------------------------------------------------------------
# Governed startup gate (spec §14.3)
# ---------------------------------------------------------------------------


class TestGovernedStartupGate:
    def test_governed_loop_requires_migration(self, workspace, bus, provider):
        from miniunicorn.agent.loop_builder import AgentLoopBuilder

        _write_all_sources(workspace)
        with pytest.raises(RuntimeError, match="memory-migrate"):
            AgentLoopBuilder(bus, provider, workspace).with_structured_memory_config(
                StructuredMemoryConfig(mode="governed")
            ).build()

    def test_governed_loop_starts_after_migration(self, workspace, bus, provider):
        from miniunicorn.agent.loop_builder import AgentLoopBuilder

        _write_all_sources(workspace)
        store = _store(workspace)
        store.run_migration()
        loop = AgentLoopBuilder(bus, provider, workspace).with_structured_memory_config(
            StructuredMemoryConfig(mode="governed")
        ).build()
        assert loop.context.memory.migration_completed() is True

    def test_shadow_loop_starts_without_migration(self, workspace, bus, provider):
        from miniunicorn.agent.loop_builder import AgentLoopBuilder

        _write_all_sources(workspace)
        loop = AgentLoopBuilder(bus, provider, workspace).with_structured_memory_config(
            StructuredMemoryConfig(mode="shadow")
        ).build()
        assert loop.context.memory.migration_completed() is False
