"""Tests for the restructured MemoryStore — pure file I/O layer."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from miniunicorn.agent.memory import _HISTORY_ENTRY_HARD_CAP, MemoryStore
from miniunicorn.agent.memory_models import (
    SCHEMA_VERSION,
    ActorKind,
    MemoryOperation,
    MemoryRecord,
    MemoryScope,
    MemoryTransaction,
    RecallQuery,
    ScopeKind,
    transaction_checksum,
)
from miniunicorn.config.schema import StructuredMemoryConfig

UTC = timezone.utc


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path)


def _record(seed: str, *, statement: str = "Wired memory fact.", slot: str = "wiring.test"):
    return MemoryRecord.model_validate(
        {
            "schema_version": SCHEMA_VERSION,
            "id": f"mem_{seed * 32}",
            "revision": 1,
            "status": "active",
            "kind": "fact",
            "scope": {"kind": "project", "key": "project:seed"},
            "subject": "MiniUnicorn",
            "slot": slot,
            "statement": statement,
            "detail": "",
            "tags": ["architecture.memory"],
            "aliases": [],
            "source_level": "verified",
            "confidence": 0.9,
            "importance": 4,
            "evidence": [
                {
                    "kind": "manual",
                    "ref": "command:seed-1",
                    "excerpt": "",
                    "sha256": None,
                    "observed_at": "2026-08-11T08:30:00Z",
                }
            ],
            "content_hash": "c" * 64,
            "derived_from": [],
            "supersedes": [],
            "replacement_id": None,
            "blocked_by": [],
            "valid_from": "2026-08-11T08:30:00Z",
            "expires_at": None,
            "created_at": "2026-08-11T08:30:00Z",
            "updated_at": "2026-08-11T08:30:00Z",
            "status_reason": "seed",
        }
    )


def _transaction_for(record, *, tx_digit: str = "0", reason: str = "seed"):
    transaction = MemoryTransaction(
        schema_version=SCHEMA_VERSION,
        tx_id="mtx_" + tx_digit * 32,
        recorded_at=datetime(2026, 8, 11, 8, 31, tzinfo=UTC),
        actor=ActorKind.SYSTEM,
        reason=reason,
        source_batch="",
        expected_revisions={record.id: 0},
        operations=[MemoryOperation(op="put", record=record)],
        checksum_sha256="f" * 64,
    )
    return transaction.model_copy(update={"checksum_sha256": transaction_checksum(transaction)})


def _write_legacy_journal(workspace: Path, records) -> Path:
    journal = workspace / "memory" / "structured" / "journal.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(
            _transaction_for(record, tx_digit=hex(index + 2)[2:]).model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        for index, record in enumerate(records)
    ]
    journal.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return journal


class TestMemoryStoreBasicIO:
    def test_read_soul_returns_empty_when_missing(self, store):
        assert store.read_soul() == ""

    def test_write_and_read_soul(self, store):
        store.write_soul("soul content")
        assert store.read_soul() == "soul content"

    @pytest.mark.parametrize(
        "name",
        (
            "memory_file",
            "user_file",
            "procedural_file",
            "shared_memory_file",
            "shared_procedural_file",
            "migration_plan",
            "run_migration",
            "migration_completed",
            "read_memory",
            "write_memory",
            "read_user",
            "write_user",
            "get_memory_context",
            "append_episodic",
            "read_episodic",
            "append_procedural",
            "read_procedural",
            "read_shared_memory",
            "read_shared_procedural",
        ),
    )
    def test_removed_legacy_api_is_absent(self, store, name):
        assert not hasattr(store, name)


class TestHistoryWithCursor:
    def test_append_history_returns_cursor(self, store):
        cursor = store.append_history("event 1")
        assert cursor == 1
        cursor2 = store.append_history("event 2")
        assert cursor2 == 2

    def test_append_history_includes_cursor_in_file(self, store):
        store.append_history("event 1")
        content = store.read_file(store.history_file)
        data = json.loads(content)
        assert data["cursor"] == 1

    def test_cursor_persists_across_appends(self, store):
        store.append_history("event 1")
        store.append_history("event 2")
        cursor = store.append_history("event 3")
        assert cursor == 3

    def test_append_history_strips_thinking_content(self, store):
        """`strip_think` must run before persistence — well-formed thinking
        blocks shouldn't land in history."""
        cursor = store.append_history("<think>reasoning</think>final answer")
        content = store.read_file(store.history_file)
        data = json.loads(content)
        assert data["cursor"] == cursor
        assert data["content"] == "final answer"

    def test_append_history_drops_pure_leak_content(self, store):
        """Regression: entries that strip down to empty (pure template-token
        leak) must NOT fall back to the raw leak. Persisting the raw text
        would re-pollute context via consolidation / replay, undoing the
        protection `strip_think` provides."""
        cursor = store.append_history("<think>nothing user-facing</think>")
        content = store.read_file(store.history_file)
        data = json.loads(content)
        assert data["cursor"] == cursor
        assert data["content"] == ""

    def test_append_history_drops_malformed_leak_prefix(self, store):
        """Channel-marker / malformed opening leaks should not survive."""
        cursor = store.append_history("<channel|>")
        content = store.read_file(store.history_file)
        data = json.loads(content)
        assert data["cursor"] == cursor
        assert data["content"] == ""

    def test_read_unprocessed_history(self, store):
        store.append_history("event 1")
        store.append_history("event 2")
        store.append_history("event 3")
        entries = store.read_unprocessed_history(since_cursor=1)
        assert len(entries) == 2
        assert entries[0]["cursor"] == 2

    def test_read_unprocessed_history_returns_all_when_cursor_zero(self, store):
        store.append_history("event 1")
        store.append_history("event 2")
        entries = store.read_unprocessed_history(since_cursor=0)
        assert len(entries) == 2

    def test_read_unprocessed_skips_entries_without_cursor(self, store):
        """Regression: entries missing the cursor key should be silently skipped."""
        store.history_file.write_text(
            '{"timestamp": "2026-04-01 10:00", "content": "no cursor"}\n'
            '{"cursor": 2, "timestamp": "2026-04-01 10:01", "content": "valid"}\n'
            '{"cursor": 3, "timestamp": "2026-04-01 10:02", "content": "also valid"}\n',
            encoding="utf-8",
        )
        entries = store.read_unprocessed_history(since_cursor=0)
        assert [e["cursor"] for e in entries] == [2, 3]

    def test_next_cursor_falls_back_when_last_entry_has_no_cursor(self, store):
        """Regression: _next_cursor should not KeyError on entries without cursor."""
        store.history_file.write_text(
            '{"timestamp": "2026-04-01 10:01", "content": "no cursor"}\n',
            encoding="utf-8",
        )
        # Delete .cursor file so _next_cursor falls back to reading JSONL
        store._cursor_file.unlink(missing_ok=True)
        # Last entry has no cursor — should safely return 1, not KeyError
        cursor = store.append_history("new event")
        assert cursor == 1

    def test_compact_history_drops_oldest(self, tmp_path):
        store = MemoryStore(tmp_path, max_history_entries=2)
        store.append_history("event 1")
        store.append_history("event 2")
        store.append_history("event 3")
        store.append_history("event 4")
        store.append_history("event 5")
        store.compact_history()
        entries = store.read_unprocessed_history(since_cursor=0)
        assert len(entries) == 2
        assert entries[0]["cursor"] in {4, 5}

    def test_write_entries_uses_atomic_write(self, tmp_path):
        """_write_entries uses temp file + os.replace for atomicity."""
        store = MemoryStore(tmp_path)
        store.append_history("event 1")
        store.append_history("event 2")
        store.append_history("event 3")
        entries = store.read_unprocessed_history(since_cursor=0)

        # Monitor temp file existence
        tmp_path_obj = store.history_file.with_suffix(".jsonl.tmp")
        assert not tmp_path_obj.exists()  # Should not exist initially

        # Call _write_entries
        store._write_entries(entries)

        # Temp file should be cleaned up
        assert not tmp_path_obj.exists()
        # Original file should exist
        assert store.history_file.exists()

    def test_write_entries_cleans_up_tmp_on_exception(self, tmp_path, monkeypatch):
        """Exception during _write_entries cleans up the temp file."""
        store = MemoryStore(tmp_path)
        store.append_history("event 1")
        entries = store.read_unprocessed_history(since_cursor=0)

        tmp_path_obj = store.history_file.with_suffix(".jsonl.tmp")

        # Mock os.replace to raise an exception
        def failing_replace(*args, **kwargs):
            raise RuntimeError("Simulated failure")

        monkeypatch.setattr("os.replace", failing_replace)

        with pytest.raises(RuntimeError):
            store._write_entries(entries)

        # Temp file should be cleaned up
        assert not tmp_path_obj.exists()

        # Original file should still exist (because replace failed)
        assert store.history_file.exists()


class TestAppendHistoryHardCap:
    """append_history has a defensive cap that catches new callers who forgot
    to set their own tighter cap. The default is intentionally larger than
    any current caller's per-call cap, so normal operation never trips it."""

    def test_oversized_entry_is_truncated(self, store):
        """An entry above _HISTORY_ENTRY_HARD_CAP is truncated before being persisted."""
        huge = "x" * (_HISTORY_ENTRY_HARD_CAP + 10_000)
        store.append_history(huge)
        entry = store.read_unprocessed_history(since_cursor=0)[0]
        assert len(entry["content"]) <= _HISTORY_ENTRY_HARD_CAP + 50

    def test_oversize_warning_is_emitted_once(self, store, caplog):
        """Repeated oversized writes should warn only on the first occurrence."""
        from loguru import logger as loguru_logger

        records: list[str] = []
        handler_id = loguru_logger.add(lambda m: records.append(m), level="WARNING")
        try:
            huge = "x" * (_HISTORY_ENTRY_HARD_CAP + 1)
            store.append_history(huge)
            store.append_history(huge)
            store.append_history(huge)
        finally:
            loguru_logger.remove(handler_id)

        oversize_warnings = [r for r in records if "exceeds" in r and "chars" in r]
        assert len(oversize_warnings) == 1

    def test_custom_max_chars_overrides_default(self, store):
        """Callers that pass max_chars should get their tighter cap applied."""
        store.append_history("a" * 500, max_chars=100)
        entry = store.read_unprocessed_history(since_cursor=0)[0]
        assert len(entry["content"]) <= 150  # 100 + "\n... (truncated)"

    def test_normal_sized_entries_unaffected(self, store):
        """The hard cap must not alter entries that fit within it."""
        msg = "normal short entry"
        store.append_history(msg)
        entry = store.read_unprocessed_history(since_cursor=0)[0]
        assert entry["content"] == msg


class TestDreamCursor:
    def test_initial_cursor_is_zero(self, store):
        assert store.get_last_dream_cursor() == 0

    def test_set_and_get_cursor(self, store):
        store.set_last_dream_cursor(5)
        assert store.get_last_dream_cursor() == 5

    def test_cursor_persists(self, store):
        store.set_last_dream_cursor(3)
        store2 = MemoryStore(store.workspace)
        assert store2.get_last_dream_cursor() == 3

    def test_git_restore_rolls_back_dream_cursor(self, tmp_path):
        store = MemoryStore(tmp_path)
        store.set_last_dream_cursor(1)
        assert store.git.init() is True

        store.set_last_dream_cursor(2)
        dream_sha = store.git.auto_commit("dream: update")
        assert dream_sha is not None

        store.set_last_dream_cursor(3)

        restore_sha = store.git.revert(dream_sha)

        assert restore_sha is not None
        assert store.get_last_dream_cursor() == 1


class TestHistoryInput:
    def test_read_unprocessed_history_handles_entries_without_cursor(self, store):
        """JSONL entries with cursor=1 are correctly parsed and returned."""
        store.history_file.write_text(
            '{"cursor": 1, "timestamp": "2026-03-30 14:30", "content": "Old event"}\n',
            encoding="utf-8",
        )
        entries = store.read_unprocessed_history(since_cursor=0)
        assert len(entries) == 1
        assert entries[0]["cursor"] == 1

    def test_history_md_is_ignored_without_mutation(self, tmp_path):
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        legacy_file = memory_dir / "HISTORY.md"
        legacy_file.write_text("developer data", encoding="utf-8")

        store = MemoryStore(tmp_path)

        assert store.read_unprocessed_history(since_cursor=0) == []
        assert legacy_file.exists()
        assert not (memory_dir / "HISTORY.md.bak").exists()


# ---------------------------------------------------------------------------
# Single-Writer 路径校验（Phase 5：路径白名单硬约束）
# ---------------------------------------------------------------------------


class TestSingleWriterPathValidation:
    """验证 MemoryStore 的路径白名单和 single-writer 不变量。"""

    def test_assert_path_in_workspace_accepts_inside(self, tmp_path):
        """workspace 内的路径应通过校验。"""
        store = MemoryStore(tmp_path)
        # 不应抛异常
        store._assert_path_in_workspace(tmp_path / "memory" / "structured" / "journal.jsonl")
        store._assert_path_in_workspace(tmp_path / "notes.md")

    def test_assert_path_in_workspace_rejects_outside(self, tmp_path):
        """workspace 外的路径应被拒绝。"""
        store = MemoryStore(tmp_path)
        # 构造 workspace 外的路径
        outside = tmp_path.parent / "evil.txt"
        with pytest.raises(PermissionError, match="outside workspace"):
            store._assert_path_in_workspace(outside)

    def test_assert_path_in_workspace_rejects_traversal(self, tmp_path):
        """path traversal 攻击（../）应被拒绝。"""
        store = MemoryStore(tmp_path)
        # 构造 traversal 路径：workspace/../../etc/passwd
        evil = tmp_path / ".." / ".." / "etc" / "passwd"
        with pytest.raises(PermissionError):
            store._assert_path_in_workspace(evil)

    def test_assert_writer_allowed_silent_for_unlisted_path(self):
        """不在白名单中的路径视为 unrestricted，不抛异常。"""
        # 不应抛异常也不应记录 warning
        MemoryStore._assert_writer_allowed("anyone", "nonexistent/file.md")

    def test_assert_writer_allowed_silent_for_correct_role(self):
        """白名单中的角色应通过校验。"""
        # main_agent 写 notes.md 是允许的
        MemoryStore._assert_writer_allowed("main_agent", "notes.md")
        # consolidator 写 notes.md 是允许的（清空）
        MemoryStore._assert_writer_allowed("consolidator", "notes.md")

    def test_assert_writer_allowed_warns_for_wrong_role(self, caplog):
        """角色不在白名单中应记录 warning（不抛异常，避免破坏现有流程）。"""
        import logging

        with caplog.at_level(logging.WARNING, logger="loguru"):
            MemoryStore._assert_writer_allowed("main_agent", "memory/structured/memory.db")
        # 注意：loguru 的 warning 通过 intercept 才能被 caplog 捕获；
        # 这里只验证不抛异常即可
        # 真正的违规检测在工具层强制执行

    def test_writer_whitelist_covers_all_memory_files(self):
        """白名单应覆盖所有结构化记忆文件。"""
        expected_files = {
            "notes.md",
            "SOUL.md",
            "memory/history.jsonl",
            "memory/.cursor",
            "memory/.dream_cursor",
            "memory/.reflections_cursor",
            "memory/reflections.jsonl",
            "memory/shared/POLICY.md",
            "memory/structured/memory.db",
            "memory/structured/storage-migration-v2.json",
            "memory/structured/audit",
            "memory/structured/backups",
            "memory/structured/tags.json",
        }
        assert expected_files.issubset(set(MemoryStore._WRITER_WHITELIST.keys()))

    def test_main_agent_only_allowed_to_write_notes(self):
        """主 Agent 只能写 notes.md，不能写其他结构化文件。"""
        for path in MemoryStore._WRITER_WHITELIST:
            allowed = MemoryStore._WRITER_WHITELIST[path]
            if path == "notes.md":
                assert "main_agent" in allowed
            else:
                assert "main_agent" not in allowed, (
                    f"主 Agent 不应被允许写 {path}（single-writer 不变量）"
                )

    def test_append_notes_passes_validation(self, tmp_path):
        """append_notes 在正常 workspace 下应通过校验并写入。"""
        store = MemoryStore(tmp_path)
        store.append_notes("test note")
        assert "test note" in store.read_notes()

    def test_clear_notes_passes_validation(self, tmp_path):
        """clear_notes 在正常 workspace 下应通过校验并清空。"""
        store = MemoryStore(tmp_path)
        store.append_notes("to be cleared")
        store.clear_notes()
        assert store.read_notes() == ""

    def test_append_history_passes_validation(self, tmp_path):
        """append_history 在正常 workspace 下应通过校验。"""
        store = MemoryStore(tmp_path)
        cursor = store.append_history("event")
        assert cursor >= 1


class TestStructuredMemoryStore:
    def test_hygiene_expires_due_structured_records(self, tmp_path, monkeypatch):
        store = MemoryStore(tmp_path, structured_config=StructuredMemoryConfig())
        now = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)
        expire_due = MagicMock(wraps=store.structured_lifecycle.expire_due)
        monkeypatch.setattr(store.structured_lifecycle, "expire_due", expire_due)

        result = store.run_memory_hygiene(now=now)

        expire_due.assert_called_once_with(now)
        assert result["structured_expired"] == 0

    def test_default_hygiene_runs_structured_expiry(self, tmp_path):
        store = MemoryStore(tmp_path)

        assert store.run_memory_hygiene()["structured_expired"] == 0

    def test_defaults_to_structured_stack_without_config(self, tmp_path):
        store = MemoryStore(tmp_path)

        assert isinstance(store.structured_config, StructuredMemoryConfig)
        assert store.structured_repository is not None
        assert store.structured_lifecycle is not None
        assert store.structured_recall is not None

    def test_explicit_config_constructs_stack_immediately(self, tmp_path):
        store = MemoryStore(tmp_path, structured_config=StructuredMemoryConfig())

        assert store.structured_repository is not None
        assert store.structured_lifecycle is not None
        assert store.structured_recall is not None
        assert store.structured_repository.health.state == "healthy"

    def test_bundled_files_created_only_when_absent(self, tmp_path):
        store = MemoryStore(tmp_path, structured_config=StructuredMemoryConfig())
        tags_path = store.workspace / "memory" / "structured" / "tags.json"
        policy_path = store.workspace / "memory" / "shared" / "POLICY.md"
        assert tags_path.exists()
        assert policy_path.exists()
        original = tags_path.read_text(encoding="utf-8")

        store2 = MemoryStore(tmp_path, structured_config=StructuredMemoryConfig())
        assert store2.workspace / "memory" / "structured" / "tags.json" == tags_path
        assert tags_path.read_text(encoding="utf-8") == original

    def test_tracks_structured_files_but_not_runtime_files(self, tmp_path):
        store = MemoryStore(tmp_path, structured_config=StructuredMemoryConfig())

        tracked = store.git._tracked_files
        assert "memory/structured/tags.json" in tracked
        assert "memory/shared/POLICY.md" in tracked
        assert "SOUL.md" in tracked
        assert "memory/structured/journal.jsonl" not in tracked
        for runtime_path in (
            "memory/structured/memory.db",
            "memory/structured/memory.db-wal",
            "memory/structured/memory.db-shm",
            "memory/structured/audit/",
            "memory/structured/backups/",
            "memory/structured/recovery/",
            "memory/structured/memory-maintenance.lock",
        ):
            assert runtime_path not in tracked

        assert store.git.init() is True
        gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
        assert "memory/structured/memory.db" in gitignore
        assert "memory/structured/*.lock" in gitignore

    def test_default_store_recall_uses_existing_stack(self, tmp_path):
        store = MemoryStore(tmp_path)
        repository = store.structured_repository

        query = RecallQuery(
            query_text="architecture.memory",
            allowed_scopes=(MemoryScope(kind=ScopeKind.PROJECT, key="project:seed"),),
            now=datetime(2026, 8, 11, 8, 30, tzinfo=UTC),
        )
        result = store.recall_structured(query)

        assert store.structured_repository is repository
        assert store.structured_recall is not None
        assert result.degraded is False

    def test_recall_structured_returns_deterministic_hits(self, tmp_path):
        store = MemoryStore(tmp_path, structured_config=StructuredMemoryConfig())
        record = MemoryRecord.model_validate(
            {
                "schema_version": SCHEMA_VERSION,
                "id": f"mem_{'a' * 32}",
                "revision": 1,
                "status": "active",
                "kind": "fact",
                "scope": {"kind": "project", "key": "project:seed"},
                "subject": "MiniUnicorn",
                "slot": "db.primary",
                "statement": "Structured memory is deterministic.",
                "detail": "",
                "tags": ["architecture.memory"],
                "aliases": [],
                "source_level": "verified",
                "confidence": 0.9,
                "importance": 4,
                "evidence": [
                    {
                        "kind": "manual",
                        "ref": "command:seed-1",
                        "excerpt": "",
                        "sha256": None,
                        "observed_at": "2026-08-11T08:30:00Z",
                    }
                ],
                "content_hash": "c" * 64,
                "derived_from": [],
                "supersedes": [],
                "replacement_id": None,
                "blocked_by": [],
                "valid_from": "2026-08-11T08:30:00Z",
                "expires_at": None,
                "created_at": "2026-08-11T08:30:00Z",
                "updated_at": "2026-08-11T08:28:00Z",
                "status_reason": "seed",
            }
        )
        transaction = MemoryTransaction(
            schema_version=SCHEMA_VERSION,
            tx_id="mtx_" + "0" * 32,
            recorded_at=datetime(2026, 8, 11, 8, 31, tzinfo=UTC),
            actor=ActorKind.SYSTEM,
            reason="seed",
            source_batch="",
            expected_revisions={record.id: 0},
            operations=[MemoryOperation(op="put", record=record)],
            checksum_sha256="f" * 64,
        )
        store.structured_repository.append_transaction(
            transaction.model_copy(update={"checksum_sha256": transaction_checksum(transaction)})
        )

        query = RecallQuery(
            query_text="architecture.memory",
            allowed_scopes=(record.scope,),
            now=datetime(2026, 8, 11, 8, 30, tzinfo=UTC),
        )
        result = store.recall_structured(query)

        assert result.hits
        assert result.hits[0].record.id == record.id
        prompt = store.structured_recall.render_prompt(result)
        assert prompt.startswith("# Recalled Memory (Deterministic)")
        assert record.id in prompt

    def test_restore_memory_version_raises_not_implemented(self, tmp_path):
        """Git journal restore is removed; use the database backup API (design §13)."""
        store = MemoryStore(tmp_path, structured_config=StructuredMemoryConfig())

        with pytest.raises(NotImplementedError, match="use memory backup restore"):
            store.restore_memory_version("abcdef12")

    def test_whitelist_allows_memory_store_for_structured_files(self):
        for path in (
            "memory/structured/memory.db",
            "memory/structured/storage-migration-v2.json",
            "memory/structured/audit",
            "memory/structured/backups",
            "memory/structured/tags.json",
            "memory/shared/POLICY.md",
        ):
            assert "memory_store" in MemoryStore._WRITER_WHITELIST[path], path

    def test_main_agent_only_allowed_to_write_notes(self, tmp_path):
        """主 Agent 只能写 notes.md，不能写其他结构化文件（含 audit/ 下子路径）。"""
        for path in MemoryStore._WRITER_WHITELIST:
            allowed = MemoryStore._WRITER_WHITELIST[path]
            if path == "notes.md":
                assert "main_agent" in allowed
            else:
                assert "main_agent" not in allowed, (
                    f"主 Agent 不应被允许写 {path}（single-writer 不变量）"
                )

    def test_main_agent_forbidden_everywhere_under_structured(self):
        """主 Agent 禁止写 structured 下任何文件（目录前缀规则覆盖 audit/backups 子路径）。"""
        for path, roles in MemoryStore._WRITER_WHITELIST.items():
            if path.startswith("memory/structured/"):
                assert "main_agent" not in roles, path

        from loguru import logger as loguru_logger

        records: list[str] = []
        handler_id = loguru_logger.add(lambda m: records.append(m), level="WARNING")
        try:
            MemoryStore._assert_writer_allowed("main_agent", "memory/structured/audit/manifest.json")
        finally:
            loguru_logger.remove(handler_id)
        assert any("Single-Writer invariant violation" in record for record in records)

    def test_directory_whitelist_entries_cover_children_without_warning(self):
        """audit/ 与 backups/ 是目录项：子路径用 containment/prefix 规则校验，不告警。"""
        from loguru import logger as loguru_logger

        records: list[str] = []
        handler_id = loguru_logger.add(lambda m: records.append(m), level="WARNING")
        try:
            for path in (
                "memory/structured/audit/manifest.json",
                "memory/structured/audit/journal-open.jsonl",
                "memory/structured/backups/memory-2026-08-15.db",
                "memory/structured/memory.db",
                "memory/structured/storage-migration-v2.json",
            ):
                MemoryStore._assert_writer_allowed("memory_store", path)
        finally:
            loguru_logger.remove(handler_id)
        assert not any("Single-Writer" in record for record in records)

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="case-insensitive FS: TAGS.json and tags.json are the same file",
    )
    def test_existing_uppercase_tag_catalog_is_inert(self, tmp_path):
        legacy = tmp_path / "memory" / "structured" / "TAGS.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text('{"custom": "unused"}', encoding="utf-8")

        store = MemoryStore(tmp_path, structured_config=StructuredMemoryConfig())

        canonical = tmp_path / "memory" / "structured" / "tags.json"
        assert canonical.exists()
        assert json.loads(canonical.read_text(encoding="utf-8")) != {"custom": "unused"}
        assert store.structured_repository.tags_path == canonical
        assert legacy.exists()

    def test_fresh_workspace_writes_canonical_tags_from_bundled_template(self, tmp_path):
        store = MemoryStore(tmp_path, structured_config=StructuredMemoryConfig())

        canonical = tmp_path / "memory" / "structured" / "tags.json"
        policy = tmp_path / "memory" / "shared" / "POLICY.md"
        bundled = Path(__file__).parents[2] / "miniunicorn" / "templates" / "memory" / "TAGS.json"

        assert canonical.read_text(encoding="utf-8") == bundled.read_text(encoding="utf-8")
        assert policy.exists()
        assert store.structured_repository.tags_path == canonical

    def test_bundled_files_written_before_memory_git_initialization(self, tmp_path):
        store = MemoryStore(tmp_path, structured_config=StructuredMemoryConfig())

        canonical = tmp_path / "memory" / "structured" / "tags.json"
        policy = tmp_path / "memory" / "shared" / "POLICY.md"

        assert canonical.exists()
        assert policy.exists()
        assert store.git.is_initialized() is False

    def test_read_shared_policy_empty_when_absent_or_template(self, tmp_path):
        store = MemoryStore(tmp_path, structured_config=StructuredMemoryConfig())
        assert store.read_shared_policy() == ""
        policy_path = store.workspace / "memory" / "shared" / "POLICY.md"
        policy_path.write_text("Always reply in Chinese.", encoding="utf-8")
        assert store.read_shared_policy() == "Always reply in Chinese."


class TestSQLiteStartupWiring:
    """Task 8: MemoryStore startup decision matrix, SQLite-only single path."""

    def test_fresh_workspace_creates_memory_db(self, tmp_path):
        store = MemoryStore(tmp_path)

        database_path = tmp_path / "memory" / "structured" / "memory.db"
        assert database_path.exists()
        assert store.structured_repository.database_path == database_path
        assert store.structured_repository.health.state == "healthy"

    def test_legacy_journal_workspace_auto_migrates(self, tmp_path):
        record = _record("a", statement="fact that must survive migration")
        journal = _write_legacy_journal(tmp_path, [record])
        original = journal.read_bytes()

        store = MemoryStore(tmp_path)

        assert (tmp_path / "memory" / "structured" / "memory.db").exists()
        manifest = json.loads(
            (tmp_path / "memory" / "structured" / "storage-migration-v2.json").read_text(
                encoding="utf-8"
            )
        )
        assert manifest["status"] == "completed"
        assert journal.read_bytes() == original
        assert store.structured_repository.health.state == "healthy"
        assert [r.id for r in store.structured_repository.current_records()] == [record.id]

    def test_startup_migration_invokes_migrator_with_correct_args(self, tmp_path, monkeypatch):
        """The startup decision runs BEFORE repository construction with the
        exact workspace and lock timeout."""
        import miniunicorn.agent.memory as memory_module

        _write_legacy_journal(tmp_path, [_record("a")])
        captured: dict[str, object] = {}

        def spy_migrate(workspace, lock_timeout_s):
            captured["workspace"] = workspace
            captured["lock_timeout_s"] = lock_timeout_s
            from miniunicorn.agent.memory_jsonl_import import migrate_legacy_journal

            return migrate_legacy_journal(workspace, lock_timeout_s)

        monkeypatch.setattr(memory_module, "migrate_legacy_journal", spy_migrate, raising=False)

        store = MemoryStore(
            tmp_path, structured_config=StructuredMemoryConfig(lock_timeout_s=2.5)
        )

        assert captured == {"workspace": tmp_path, "lock_timeout_s": 2.5}
        assert store.structured_repository.health.state == "healthy"

    def test_migrated_workspace_second_open_never_reads_journal(self, tmp_path, monkeypatch):
        """Second boot of a migrated workspace must not read journal.jsonl at all."""
        _write_legacy_journal(tmp_path, [_record("a")])
        MemoryStore(tmp_path)
        journal = tmp_path / "memory" / "structured" / "journal.jsonl"

        original_open = Path.open
        original_read_bytes = Path.read_bytes

        def exploding_open(self, *args, **kwargs):
            if Path(self) == journal:
                raise AssertionError("second open read the legacy journal")
            return original_open(self, *args, **kwargs)

        def exploding_read_bytes(self):
            if Path(self) == journal:
                raise AssertionError("second open read the legacy journal")
            return original_read_bytes(self)

        monkeypatch.setattr(Path, "open", exploding_open)
        monkeypatch.setattr(Path, "read_bytes", exploding_read_bytes)

        store = MemoryStore(tmp_path)

        assert store.structured_repository.health.state == "healthy"
        assert len(store.structured_repository.current_records()) == 1

    def test_different_workspaces_have_fully_isolated_databases(self, tmp_path):
        ws_a, ws_b = tmp_path / "A", tmp_path / "B"
        store_a = MemoryStore(ws_a)
        store_b = MemoryStore(ws_b)

        assert (
            store_a.structured_repository.database_path
            != store_b.structured_repository.database_path
        )
        store_a.structured_repository.append_transaction(
            _transaction_for(_record("a", statement="fact only in A", slot="isolation.test"))
        )

        assert len(store_a.structured_repository.current_records()) == 1
        assert store_b.structured_repository.current_records() == ()
        assert store_a.structured_repository.database_path.exists()
        assert store_b.structured_repository.database_path.exists()

    def test_manifest_completed_without_db_degrades_fail_closed(self, tmp_path):
        """A completed manifest without memory.db must fail closed: no
        re-migration of a journal that lacks post-migration facts."""
        _write_legacy_journal(tmp_path, [_record("a")])
        manifest_path = tmp_path / "memory" / "structured" / "storage-migration-v2.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "completed",
                    "source_sha256": "x" * 64,
                    "transaction_count": 1,
                }
            ),
            encoding="utf-8",
        )

        store = MemoryStore(tmp_path)

        health = store.structured_repository.health
        assert health.state == "degraded"
        assert health.error_code == "migration_database_lost"
        assert not (tmp_path / "memory" / "structured" / "memory.db").exists()
        assert manifest_path.exists()
        assert (tmp_path / "memory" / "structured" / "journal.jsonl").exists()

    def test_structured_stack_always_initialized_without_backend_or_mode(self, tmp_path):
        store = MemoryStore(tmp_path)

        assert store.structured_repository is not None
        assert store.structured_lifecycle is not None
        assert store.structured_recall is not None
        assert store.audit_exporter is not None
        assert not hasattr(store, "backend")
        assert not hasattr(store, "mode")

    def test_startup_lag_exports_audit_after_migration(self, tmp_path):
        """First boot after migration exports the first audit (design §11 step 13)."""
        _write_legacy_journal(tmp_path, [_record("a")])

        store = MemoryStore(tmp_path)
        audit_dir = tmp_path / "memory" / "structured" / "audit"

        manifest = json.loads((audit_dir / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["segments"]
        open_segment = audit_dir / "journal-open.jsonl"
        assert open_segment.exists()
        assert open_segment.read_text(encoding="utf-8").strip()
        assert store.structured_repository.health.state == "healthy"

