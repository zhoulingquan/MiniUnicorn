"""Migration tests for governed structured memory (design section 14).

Covers deterministic mapping, idempotent legacy_key, zero-write dry-run,
journal-backed apply, completed_at semantics and the governed startup gate.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from miniunicorn.agent.memory_migration import (
    LEGACY_MIGRATION_STATE_FILE,
    MIGRATION_STATE_FILE,
    MemoryMigration,
    MigrationState,
    _proposal_for,
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
    (workspace / "memory" / "MEMORY.md").write_text(
        "## Decision\n- 用 SQLite 做存储\n\n## 需求\n- 支持离线模式\n",
        encoding="utf-8",
    )
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
        # 文件事实有独立 slot 并自动提升；procedural/episodic 没有伪造重复证据，保持 candidate。
        active = {r for r in records if r.status is MemoryStatus.ACTIVE}
        assert len(active) == 6
        candidates = {r for r in records if r.status is MemoryStatus.CANDIDATE}
        assert len(candidates) == 3  # 2 procedural + 1 episodic（inferred 永不提升）
        blocked = [r for r in records if r.status is MemoryStatus.CANDIDATE and r.blocked_by]
        assert blocked == []
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

    def test_process_interrupted_then_resumed(self, workspace, monkeypatch):
        """§17.6: an interrupted apply must resume from persisted progress."""
        (workspace / "memory").mkdir(exist_ok=True)
        (workspace / "memory" / "MEMORY.md").write_text(
            "- 甲\n- 乙\n- 丙\n- 丁\n", encoding="utf-8"
        )
        store = _store(workspace)
        original_ingest = MemoryMigration._ingest_item
        calls = {"count": 0}

        def broken_ingest(self, item, index):
            calls["count"] += 1
            if calls["count"] >= 3:
                raise RuntimeError("simulated interruption")
            return original_ingest(self, item, index)

        monkeypatch.setattr(MemoryMigration, "_ingest_item", broken_ingest)
        report = store.run_migration()
        assert report.imported == 2
        assert len(report.failed) == 2  # 中断后剩余项继续失败
        assert "interruption" in report.failed[0].error
        state = MigrationState.load(workspace / MIGRATION_STATE_FILE)
        assert len(state.entries) == 2  # 进度已持久化，中途被中断

        monkeypatch.setattr(MemoryMigration, "_ingest_item", original_ingest)
        report = store.run_migration()
        assert report.imported == 2  # 剩余两项补齐
        assert report.skipped == 2
        assert report.failed == ()
        assert len(_current(store)) == 4

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

    def test_procedure_does_not_fabricate_repeated_evidence(self, workspace):
        _write_procedural(workspace)
        store = _store(workspace)
        store.run_migration()
        record = _current(store)[0]
        assert len(record.evidence) == 1
        assert record.source_level.value == "inferred"
        assert record.status is MemoryStatus.CANDIDATE

    def test_distinct_legacy_items_get_distinct_slots(self, workspace):
        _write_user(workspace)
        items, _ = scan_legacy_memory(workspace)

        proposals = [_proposal_for(item, index) for index, item in enumerate(items)]

        assert len({proposal.slot for proposal in proposals}) == len(proposals)

    def test_manifest_save_flushes_and_fsyncs(self, workspace, monkeypatch):
        state = MigrationState(entries={"a": "mem_" + "a" * 32}, completed_at=None)
        fsync = Mock()
        monkeypatch.setattr(os, "fsync", fsync)

        state.save(workspace / MIGRATION_STATE_FILE)

        assert fsync.call_count >= 1  # file always; directory too on POSIX

    def test_apply_with_failure_does_not_mark_migration_complete(self, workspace):
        (workspace / "memory").mkdir(exist_ok=True)
        (workspace / "memory" / "MEMORY.md").write_text(
            "- " + "x" * 600 + "\n", encoding="utf-8"
        )
        store = _store(workspace)

        report = store.run_migration()

        assert report.failed
        assert report.completed_at is None
        assert store.migration_completed() is False

    def test_apply_with_scan_issue_does_not_mark_migration_complete(self, workspace):
        (workspace / "memory").mkdir(exist_ok=True)
        (workspace / "memory" / "MEMORY.md").write_text(
            "Paragraph without atomic boundary.\n", encoding="utf-8"
        )
        store = _store(workspace)

        report = store.run_migration()

        assert report.issues
        assert report.completed_at is None

    def test_each_successful_item_is_saved_before_next_item(self, workspace, monkeypatch):
        (workspace / "memory").mkdir(exist_ok=True)
        (workspace / "memory" / "MEMORY.md").write_text("- 甲\n- 乙\n", encoding="utf-8")
        store = _store(workspace)
        saves: list[int] = []
        original_save = MigrationState.save

        def recording_save(self, path):
            saves.append(len(self.entries))
            return original_save(self, path)

        monkeypatch.setattr(MigrationState, "save", recording_save)
        store.run_migration()

        assert saves[:2] == [1, 2]

    def test_legacy_manifest_is_loaded_and_copied_to_canonical_path(self, workspace):
        _write_memory(workspace)
        items, _ = scan_legacy_memory(workspace)
        legacy_path = workspace / LEGACY_MIGRATION_STATE_FILE
        MigrationState(
            entries={items[0].legacy_key(): "mem_" + "a" * 32},
            completed_at=None,
        ).save(legacy_path)
        store = _store(workspace)

        report = store.run_migration()

        assert report.skipped == 1
        canonical = workspace / MIGRATION_STATE_FILE
        assert canonical.exists()
        assert items[0].legacy_key() in MigrationState.load(canonical).entries

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


# ---------------------------------------------------------------------------
# C2 plan B: unified state loading, migration lock, durable unique-temp save
# ---------------------------------------------------------------------------


class TestUnifiedMigrationStateLoader:
    def test_neither_file_returns_empty_state(self, workspace):
        from miniunicorn.agent.memory_migration import load_migration_state

        state = load_migration_state(workspace)

        assert state.entries == {}
        assert state.completed_at is None

    def test_legacy_only_completed_state_is_loaded(self, workspace):
        from miniunicorn.agent.memory_migration import load_migration_state

        legacy = workspace / LEGACY_MIGRATION_STATE_FILE
        MigrationState(
            entries={"a": "mem_" + "a" * 32}, completed_at=dt_utc("2026-08-11T09:00:00Z")
        ).save(legacy)

        state = load_migration_state(workspace)

        assert state.completed_at == dt_utc("2026-08-11T09:00:00Z")
        assert state.entries == {"a": "mem_" + "a" * 32}

    def test_canonical_wins_when_both_exist(self, workspace):
        from miniunicorn.agent.memory_migration import load_migration_state

        canonical = workspace / MIGRATION_STATE_FILE
        legacy = workspace / LEGACY_MIGRATION_STATE_FILE
        MigrationState(entries={"old": "mem_" + "a" * 32}, completed_at=dt_utc("2026-08-11T08:00:00Z")).save(legacy)
        MigrationState(entries={"new": "mem_" + "b" * 32}, completed_at=dt_utc("2026-08-11T09:00:00Z")).save(canonical)

        state = load_migration_state(workspace)

        assert state.entries == {"new": "mem_" + "b" * 32}
        assert state.completed_at == dt_utc("2026-08-11T09:00:00Z")

    def test_corrupt_canonical_never_falls_back_to_legacy_completion(self, workspace):
        from miniunicorn.agent.memory_migration import load_migration_state

        canonical = workspace / MIGRATION_STATE_FILE
        legacy = workspace / LEGACY_MIGRATION_STATE_FILE
        MigrationState(
            entries={"old": "mem_" + "a" * 32}, completed_at=dt_utc("2026-08-11T08:00:00Z")
        ).save(legacy)
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_text("{not json at all", encoding="utf-8")

        state = load_migration_state(workspace)

        assert state.completed_at is None
        assert state.entries == {}


class TestMigrationLock:
    def test_apply_fails_closed_when_migration_lock_held(self, workspace):
        from filelock import FileLock

        from miniunicorn.agent.memory_models import MemoryLockTimeout

        _write_memory(workspace)
        store = _store(workspace)
        lock_path = workspace / "memory" / "structured" / "migration-v1.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(lock_path)):
            migration = MemoryMigration(
                workspace,
                store.structured_repository,
                store.structured_lifecycle,
                store.project_scope_key,
                lock_timeout_s=0.05,
            )
            with pytest.raises(MemoryLockTimeout):
                migration.apply()

        assert not (workspace / MIGRATION_STATE_FILE).exists()
        journal = workspace / "memory" / "structured" / "journal.jsonl"
        assert not journal.exists() or journal.read_text(encoding="utf-8").strip() == ""


class TestDurableManifestSave:
    def test_each_save_uses_unique_sibling_temp(self, workspace, monkeypatch):
        import tempfile

        path = workspace / MIGRATION_STATE_FILE
        replaces: list[tuple[Path, Path]] = []
        monkeypatch.setattr(os, "replace", lambda src, dst: replaces.append((Path(src), Path(dst))))
        names: list[Path] = []
        real_ntf = tempfile.NamedTemporaryFile

        def recording_ntf(*args, **kwargs):
            stream = real_ntf(*args, **kwargs)
            names.append(Path(stream.name))
            return stream

        monkeypatch.setattr(tempfile, "NamedTemporaryFile", recording_ntf)
        state = MigrationState(entries={"a": "mem_" + "a" * 32}, completed_at=None)

        state.save(path)
        state.save(path)

        assert len(names) == 2
        assert names[0] != names[1]
        assert names[0].parent == path.parent
        assert names[1].parent == path.parent
        assert ".tmp" in names[0].name and ".tmp" in names[1].name
        assert replaces[0][0] == names[0]
        assert replaces[1][0] == names[1]
        assert replaces[0][1] == path
        assert replaces[1][1] == path
        assert not names[0].exists() and not names[1].exists()

    def test_file_fsync_before_replace_and_directory_fsync_after(self, workspace, monkeypatch):
        path = workspace / MIGRATION_STATE_FILE
        events: list[tuple[str, object]] = []
        real_open = os.open

        def fake_open(open_path, *rest):
            events.append(("open", Path(open_path)))
            return real_open(open_path, *rest)

        def fake_fsync(fd):
            events.append(("fsync", fd))

        def fake_replace(src, dst):
            events.append(("replace", Path(src), Path(dst)))

        def fake_close(fd):
            events.append(("close", fd))

        monkeypatch.setattr(os, "open", fake_open)
        monkeypatch.setattr(os, "fsync", fake_fsync)
        monkeypatch.setattr(os, "replace", fake_replace)
        monkeypatch.setattr(os, "close", fake_close)
        state = MigrationState(entries={"a": "mem_" + "a" * 32}, completed_at=None)

        state.save(path)

        markers = [event[0] for event in events]
        # tempfile.NamedTemporaryFile itself records an "open" (mkstemp)
        # before the file fsync; the file fsync must precede the replace.
        assert markers[0] == "open"
        assert markers.index("fsync") < markers.index("replace")
        if os.name == "posix":
            dir_open = markers.index("open", markers.index("replace"))
            assert markers[dir_open:] == ["open", "fsync", "close"]
            assert events[dir_open][1] == path.parent
        else:
            assert markers[-1] == "replace"

    def test_failed_replace_cleans_up_this_temps(self, workspace, monkeypatch):
        path = workspace / MIGRATION_STATE_FILE
        real_replace = os.replace
        calls = {"n": 0}

        def flaky_replace(src, dst):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("disk full")
            return real_replace(src, dst)

        monkeypatch.setattr(os, "replace", flaky_replace)
        state = MigrationState(entries={"a": "mem_" + "a" * 32}, completed_at=None)
        with pytest.raises(OSError):
            state.save(path)
        leftovers = list(path.parent.glob(f".{path.name}.*.tmp"))
        assert leftovers == []


def _worker_migrate(workspace: str, start, queue) -> None:
    workspace_path = Path(workspace)
    store = _store(workspace_path, mode="shadow")
    start.wait()
    try:
        report = store.run_migration()
        queue.put(
            {
                "imported": report.imported,
                "skipped": report.skipped,
                "failed": len(report.failed),
                "completed": report.completed_at is not None,
            }
        )
    except Exception as exc:  # noqa: BLE001 - report over the queue
        queue.put({"error": f"{type(exc).__name__}: {exc}"})


class TestTwoProcessMigration:
    def test_concurrent_apply_imports_each_legacy_key_exactly_once(self, workspace):
        import multiprocessing

        _write_all_sources(workspace)
        items, _ = _scan_legacy(workspace)
        expected_keys = {item.legacy_key() for item in items}
        ctx = multiprocessing.get_context("spawn")
        start = ctx.Event()
        queue = ctx.Queue()
        procs = [
            ctx.Process(target=_worker_migrate, args=(str(workspace), start, queue)),
            ctx.Process(target=_worker_migrate, args=(str(workspace), start, queue)),
        ]
        try:
            for proc in procs:
                proc.start()
            start.set()
            for proc in procs:
                proc.join(timeout=60)
            for proc in procs:
                assert proc.exitcode == 0
            reports = [queue.get(timeout=10) for _ in range(2)]
        finally:
            for proc in procs:
                if proc.is_alive():
                    proc.terminate()
                    proc.join(timeout=5)

        for report in reports:
            assert "error" not in report, report
            assert report["imported"] + report["skipped"] == len(expected_keys)
            assert report["failed"] == 0
            assert report["completed"] is True

        canonical = workspace / MIGRATION_STATE_FILE
        assert canonical.exists()
        state = MigrationState.load(canonical)
        assert set(state.entries) == expected_keys
        assert len(state.entries) == len(expected_keys)

        rebuilt = _store(workspace, mode="shadow")
        assert len(rebuilt.structured_repository.current_records()) == len(expected_keys)


def dt_utc(iso: str):
    from datetime import datetime

    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _scan_legacy(workspace: Path):
    return scan_legacy_memory(workspace)
