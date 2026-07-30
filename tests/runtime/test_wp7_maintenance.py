"""WP7 — Durable maintenance and memory hardening (design §22, §33.4, WP7).

Covers:

- Maintenance enqueue with deterministic dedup keys (design §13.1, §22.3).
- Maintenance priority and one-task quota (design §22.4, §25.2).
- User work preempts new maintenance claims (design §22.4, §15.2).
- Source revision checks in dedup keys (design §13.1).
- Retention: list, delete, blob GC, WAL checkpoint (design §33.4, §16.16).
- Duplicate index task is harmless (design §13.1).
- Stale consolidation cannot overwrite newer memory (design §13.1).
- Required work has a durable task row (design §22.3).
- Restart during maintenance is safe (design §22.3).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

import pytest

from miniunicorn.runtime.maintenance import (
    MAINTENANCE_CAPACITY,
    MAINTENANCE_HOLDER_KIND,
    MAINTENANCE_RESOURCE_KEY,
    PRIORITY_MEMORY,
    PRIORITY_REFLECTION_DREAM,
    PRIORITY_RETENTION_CLEANUP,
    PRIORITY_USER_TURN,
    dedup_key_for_backup,
    dedup_key_for_consolidation,
    dedup_key_for_dream,
    dedup_key_for_index,
    dedup_key_for_reflection,
    dedup_key_for_retention,
    dedup_key_for_wal_checkpoint,
    enqueue_maintenance,
    is_low_priority_maintenance,
    is_maintenance_task_kind,
    run_backup,
    run_blob_gc,
    run_retention_batch,
    run_wal_checkpoint,
    user_work_is_queued,
)
from miniunicorn.runtime.models import (
    InternalTaskEnvelope,
    RequestScope,
    RetentionPolicy,
)


# ---------------------------------------------------------------------------
# Dedup key builder tests
# ---------------------------------------------------------------------------


class TestDedupKeys:
    """Dedup keys encode source revisions (design §13.1)."""

    def test_dream_dedup_key_includes_source_revision(self) -> None:
        k1 = dedup_key_for_dream(source_revision="rev-1")
        k2 = dedup_key_for_dream(source_revision="rev-1")
        k3 = dedup_key_for_dream(source_revision="rev-2")
        assert k1 == k2, "same source revision must produce same dedup key"
        assert k1 != k3, "different source revision must produce different key"

    def test_consolidation_dedup_key_includes_source_revision(self) -> None:
        k1 = dedup_key_for_consolidation(source_revision="cursor-42")
        k2 = dedup_key_for_consolidation(source_revision="cursor-43")
        assert k1 != k2

    def test_index_dedup_key_includes_source_revision(self) -> None:
        k1 = dedup_key_for_index(source_revision="idx-1")
        k2 = dedup_key_for_index(source_revision="idx-1")
        assert k1 == k2

    def test_retention_dedup_key_includes_window(self) -> None:
        k1 = dedup_key_for_retention(window_id="2026-07-30T00")
        k2 = dedup_key_for_retention(window_id="2026-07-30T01")
        assert k1 != k2

    def test_different_kinds_have_distinct_prefixes(self) -> None:
        assert dedup_key_for_dream(source_revision="x").startswith("dream:")
        assert dedup_key_for_consolidation(source_revision="x").startswith("consolidation:")
        assert dedup_key_for_index(source_revision="x").startswith("index:")
        assert dedup_key_for_reflection(source_revision="x").startswith("reflection:")
        assert dedup_key_for_retention(window_id="x").startswith("retention:")
        assert dedup_key_for_backup(backup_id="x").startswith("backup:")
        assert dedup_key_for_wal_checkpoint(window_id="x").startswith("wal_checkpoint:")


# ---------------------------------------------------------------------------
# Maintenance enqueue tests (durable task row)
# ---------------------------------------------------------------------------


class TestMaintenanceEnqueue:
    """Enqueuing maintenance creates a durable task row (design §22.3)."""

    @pytest.mark.asyncio
    async def test_enqueue_creates_durable_task_row(
        self,
        store: Any,
        sample_scope: RequestScope,
    ) -> None:
        from miniunicorn.runtime.task_service import TaskService

        task_service = TaskService(store)
        task_id = await enqueue_maintenance(
            task_service,
            task_kind="DREAM",
            scope=sample_scope,
            dedup_key=dedup_key_for_dream(source_revision="rev-1"),
            priority=PRIORITY_REFLECTION_DREAM,
            payload={"source": "test"},
        )
        # The task must exist in the store.
        record = store.read_task(task_id)
        assert record is not None
        assert record.state == "QUEUED"
        assert record.task_kind == "DREAM"

    @pytest.mark.asyncio
    async def test_duplicate_enqueue_returns_same_task_id(
        self,
        store: Any,
        sample_scope: RequestScope,
    ) -> None:
        from miniunicorn.runtime.task_service import TaskService

        task_service = TaskService(store)
        dedup_key = dedup_key_for_consolidation(source_revision="rev-1")
        task_id_1 = await enqueue_maintenance(
            task_service,
            task_kind="MEMORY_CONSOLIDATION",
            scope=sample_scope,
            dedup_key=dedup_key,
            priority=PRIORITY_MEMORY,
        )
        task_id_2 = await enqueue_maintenance(
            task_service,
            task_kind="MEMORY_CONSOLIDATION",
            scope=sample_scope,
            dedup_key=dedup_key,
            priority=PRIORITY_MEMORY,
        )
        assert task_id_1 == task_id_2, "duplicate submission must return same task id"

    @pytest.mark.asyncio
    async def test_different_source_revision_creates_new_task(
        self,
        store: Any,
        sample_scope: RequestScope,
    ) -> None:
        from miniunicorn.runtime.task_service import TaskService

        task_service = TaskService(store)
        task_id_1 = await enqueue_maintenance(
            task_service,
            task_kind="MEMORY_INDEX",
            scope=sample_scope,
            dedup_key=dedup_key_for_index(source_revision="rev-1"),
            priority=PRIORITY_MEMORY,
        )
        task_id_2 = await enqueue_maintenance(
            task_service,
            task_kind="MEMORY_INDEX",
            scope=sample_scope,
            dedup_key=dedup_key_for_index(source_revision="rev-2"),
            priority=PRIORITY_MEMORY,
        )
        assert task_id_1 != task_id_2, "different source revision must create new task"

    @pytest.mark.asyncio
    async def test_maintenance_priority_applied(
        self,
        store: Any,
        sample_scope: RequestScope,
    ) -> None:
        from miniunicorn.runtime.task_service import TaskService

        task_service = TaskService(store)
        task_id = await enqueue_maintenance(
            task_service,
            task_kind="MAINTENANCE",
            scope=sample_scope,
            dedup_key=dedup_key_for_retention(window_id="w1"),
            priority=PRIORITY_RETENTION_CLEANUP,
        )
        record = store.read_task(task_id)
        assert record is not None
        assert record.priority == PRIORITY_RETENTION_CLEANUP


# ---------------------------------------------------------------------------
# User work preempts maintenance claims (design §22.4, §15.2)
# ---------------------------------------------------------------------------


class TestUserWorkPreemptsMaintenance:
    """Maintenance does not claim while an eligible user task waits (§22.4)."""

    def test_maintenance_not_claimed_when_user_work_queued(
        self,
        store: Any,
        sample_scope: RequestScope,
        make_inbound_envelope: Any,
    ) -> None:
        from miniunicorn.runtime.contracts import ClaimRequest
        from miniunicorn.runtime.models import InternalTaskEnvelope

        # Submit a user task.
        env = make_inbound_envelope(sample_scope)
        store.submit_task(env)

        # Submit a maintenance task with lower priority.
        maint_env = InternalTaskEnvelope(
            protocol_version=1,
            task_kind="DREAM",
            priority=PRIORITY_REFLECTION_DREAM,
            scope=sample_scope,
            session_key="system:dream",
            dedup_key=dedup_key_for_dream(source_revision="rev-1"),
            normalized_payload_ref="inline:test",
            payload_hash="hash",
            received_at_ms=1_000_000,
        )
        store.submit_internal(maint_env)

        # Claim — must get the user task, not the maintenance task.
        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )
        assert result.claimed is not None
        assert result.claimed.record.task_kind == "USER_TURN"

    def test_maintenance_claimed_when_no_user_work(
        self,
        store: Any,
        sample_scope: RequestScope,
    ) -> None:
        from miniunicorn.runtime.contracts import ClaimRequest
        from miniunicorn.runtime.models import InternalTaskEnvelope

        # Submit only a maintenance task.
        maint_env = InternalTaskEnvelope(
            protocol_version=1,
            task_kind="DREAM",
            priority=PRIORITY_REFLECTION_DREAM,
            scope=sample_scope,
            session_key="system:dream",
            dedup_key=dedup_key_for_dream(source_revision="rev-1"),
            normalized_payload_ref="inline:test",
            payload_hash="hash",
            received_at_ms=1_000_000,
        )
        store.submit_internal(maint_env)

        # Claim — must get the maintenance task since no user work.
        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )
        assert result.claimed is not None
        assert result.claimed.record.task_kind == "DREAM"

    def test_user_work_is_queued_helper(
        self,
        store: Any,
        sample_scope: RequestScope,
        make_inbound_envelope: Any,
    ) -> None:
        # No tasks → not queued.
        assert user_work_is_queued(store, now_ms=2_000_000) is False

        # Submit a user task → queued.
        env = make_inbound_envelope(sample_scope)
        store.submit_task(env)
        assert user_work_is_queued(store, now_ms=2_000_000) is True

    def test_is_maintenance_task_kind(self) -> None:
        assert is_maintenance_task_kind("DREAM") is True
        assert is_maintenance_task_kind("MAINTENANCE") is True
        assert is_maintenance_task_kind("MEMORY_CONSOLIDATION") is True
        assert is_maintenance_task_kind("USER_TURN") is False

    def test_is_low_priority_maintenance(self) -> None:
        assert is_low_priority_maintenance("DREAM", 20) is True
        assert is_low_priority_maintenance("USER_TURN", 50) is False
        assert is_low_priority_maintenance("MEMORY_CONSOLIDATION", 50) is True


# ---------------------------------------------------------------------------
# Retention tests (design §33.4, §16.16)
# ---------------------------------------------------------------------------


class TestRetention:
    """Retention list/delete and blob GC (design §33.4, §16.16)."""

    def test_list_retention_batch_empty_on_fresh_store(
        self,
        store: Any,
    ) -> None:
        from miniunicorn.runtime.models import RetentionPolicy

        batch = store.list_retention_batch(RetentionPolicy(), now_ms=2_000_000)
        assert len(batch.task_ids) == 0
        assert len(batch.outbox_ids) == 0
        assert len(batch.blob_ids) == 0

    def test_retention_deletes_old_terminal_tasks(
        self,
        store: Any,
        sample_scope: RequestScope,
        make_inbound_envelope: Any,
    ) -> None:
        from miniunicorn.runtime.contracts import ClaimRequest
        from miniunicorn.runtime.models import (
            BlobWrite,
            CompletionWrite,
            DeliveryReceipt,
            RetentionPolicy,
        )

        # Submit and complete a task.
        env = make_inbound_envelope(sample_scope)
        submit = store.submit_task(env)
        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )
        claim = result.claimed.claim
        store.mark_running(claim, now_ms=2_000_001)

        content = b"reply"
        blob = store.write_blob(
            BlobWrite(
                scope_key="test",
                blob_kind="OUTBOX_PAYLOAD",
                content_hash=hashlib.sha256(content).hexdigest(),
                encoding="RAW_BYTES",
                inline_content=content,
                size_bytes=len(content),
                created_at_ms=2_000_002,
            )
        )
        completion = CompletionWrite(
            final_reply_blob_id=blob.blob_id,
            final_reply_hash=hashlib.sha256(content).hexdigest(),
            final_reply_dedup_key=None,
            suppress_final=False,
            completed_at_ms=2_000_003,
        )
        store.complete_with_outbox(claim, completion)

        # Deliver the outbox row so the task becomes retention-eligible
        # (design §16.16: tasks with non-terminal outbox rows are skipped).
        outbox_claim = store.claim_next_delivery(
            sender_id="s-1", now_ms=2_000_004, lease_ms=60_000
        )
        assert outbox_claim is not None
        store.mark_delivered(
            outbox_claim,
            DeliveryReceipt(
                status="DELIVERED",
                provider_message_id="msg-1",
                receipt_ref="rcpt-1",
            ),
        )

        # With a 0-day retention, the task is eligible.
        policy = RetentionPolicy(
            successful_task_age_days=0,
            failed_task_age_days=0,
            batch_size=10,
        )
        batch = store.list_retention_batch(policy, now_ms=2_100_000)
        assert submit.task_id in batch.task_ids

        result = store.delete_retention_batch(batch)
        assert result.deleted_tasks >= 1

        # Task should be gone.
        assert store.read_task(submit.task_id) is None

    def test_retention_skips_non_terminal_tasks(
        self,
        store: Any,
        sample_scope: RequestScope,
        make_inbound_envelope: Any,
    ) -> None:
        from miniunicorn.runtime.models import RetentionPolicy

        env = make_inbound_envelope(sample_scope)
        submit = store.submit_task(env)
        # Task is QUEUED (not terminal).

        policy = RetentionPolicy(
            successful_task_age_days=0,
            failed_task_age_days=0,
        )
        batch = store.list_retention_batch(policy, now_ms=2_000_000)
        assert submit.task_id not in batch.task_ids

    def test_list_unreferenced_blobs(
        self,
        store: Any,
    ) -> None:
        from miniunicorn.runtime.models import BlobWrite

        # Write a blob with no references.
        content = b"orphan"
        blob = store.write_blob(
            BlobWrite(
                scope_key="test",
                blob_kind="OUTBOX_PAYLOAD",
                content_hash=hashlib.sha256(content).hexdigest(),
                encoding="RAW_BYTES",
                inline_content=content,
                size_bytes=len(content),
                created_at_ms=2_000_000,
            )
        )
        orphans = store.list_unreferenced_blobs(limit=10)
        assert blob.blob_id in orphans

        # Delete it.
        deleted = store.delete_unreferenced_blobs([blob.blob_id])
        assert deleted == 1
        assert blob.blob_id not in store.list_unreferenced_blobs(limit=10)

    def test_run_retention_batch_wrapper(
        self,
        store: Any,
    ) -> None:
        """run_retention_batch selects and deletes in one call."""
        result = run_retention_batch(store, now_ms=2_000_000)
        assert result.deleted_tasks == 0
        assert result.deleted_outbox == 0

    def test_run_blob_gc_wrapper(self, store: Any) -> None:
        deleted = run_blob_gc(store, limit=10)
        assert deleted == 0

    def test_wal_checkpoint_does_not_raise(self, store: Any) -> None:
        run_wal_checkpoint(store)

    def test_backup_to_temp_file(self, store: Any, tmp_path) -> None:
        dest = str(tmp_path / "backup.db")
        run_backup(store, dest_path=dest)
        import os
        assert os.path.exists(dest)


# ---------------------------------------------------------------------------
# Stale consolidation cannot overwrite newer memory (design §13.1)
# ---------------------------------------------------------------------------


class TestStaleConsolidationFencing:
    """A consolidation task with an old source revision cannot overwrite newer memory.

    The dedup key encodes the source revision (design §13.1). A stale
    task (old revision) either deduplicates against the original
    submission (same revision) or creates a new task (different revision)
    that will see the newer memory state when it runs.
    """

    def test_old_revision_dedup_key_differs_from_new(self) -> None:
        old_key = dedup_key_for_consolidation(source_revision="rev-old")
        new_key = dedup_key_for_consolidation(source_revision="rev-new")
        assert old_key != new_key

    @pytest.mark.asyncio
    async def test_duplicate_consolidation_is_idempotent(
        self,
        store: Any,
        sample_scope: RequestScope,
    ) -> None:
        from miniunicorn.runtime.task_service import TaskService

        task_service = TaskService(store)
        dedup_key = dedup_key_for_consolidation(source_revision="rev-1")

        task_id_1 = await enqueue_maintenance(
            task_service,
            task_kind="MEMORY_CONSOLIDATION",
            scope=sample_scope,
            dedup_key=dedup_key,
            priority=PRIORITY_MEMORY,
        )
        task_id_2 = await enqueue_maintenance(
            task_service,
            task_kind="MEMORY_CONSOLIDATION",
            scope=sample_scope,
            dedup_key=dedup_key,
            priority=PRIORITY_MEMORY,
        )
        # Same dedup key → same task (no duplicate row).
        assert task_id_1 == task_id_2


# ---------------------------------------------------------------------------
# Restart during maintenance (design §22.3)
# ---------------------------------------------------------------------------


class TestMaintenanceRestartSafety:
    """A crash during maintenance is recoverable because work is durable (§22.3)."""

    @pytest.mark.asyncio
    async def test_maintenance_task_survives_simulated_restart(
        self,
        store: Any,
        sample_scope: RequestScope,
    ) -> None:
        from miniunicorn.runtime.task_service import TaskService

        task_service = TaskService(store)
        task_id = await enqueue_maintenance(
            task_service,
            task_kind="MAINTENANCE",
            scope=sample_scope,
            dedup_key=dedup_key_for_retention(window_id="w1"),
            priority=PRIORITY_RETENTION_CLEANUP,
        )

        # Simulate a restart: the task_service object is discarded and
        # rebuilt. The task row must still be in the store.
        new_task_service = TaskService(store)
        snapshot = await new_task_service.get_status(sample_scope, task_id)
        assert snapshot.state == "QUEUED"
        record = store.read_task(task_id)
        assert record is not None
        assert record.task_kind == "MAINTENANCE"


# ---------------------------------------------------------------------------
# MaintenanceExecutor tests (design §22.3, §29.16)
# ---------------------------------------------------------------------------


class TestMaintenanceExecutor:
    """MaintenanceExecutor dispatches maintenance task kinds to runners."""

    @pytest.mark.asyncio
    async def test_dream_dispatch(self, store: Any) -> None:
        from miniunicorn.runtime.maintenance_executor import MaintenanceExecutor

        called: list[bool] = []

        async def dream_runner() -> bool:
            called.append(True)
            return True

        executor = MaintenanceExecutor(store, dream_runner=dream_runner)
        result = await executor.execute(task_kind="DREAM", payload={})
        assert result.success is True
        assert result.items_processed == 1
        assert called == [True]

    @pytest.mark.asyncio
    async def test_consolidation_dispatch(self, store: Any) -> None:
        from miniunicorn.runtime.maintenance_executor import MaintenanceExecutor

        received_keys: list[str] = []

        async def consolidation_runner(key: str) -> str | None:
            received_keys.append(key)
            return "summary"

        executor = MaintenanceExecutor(
            store, consolidation_runner=consolidation_runner
        )
        result = await executor.execute(
            task_kind="MEMORY_CONSOLIDATION",
            payload={"session_key": "session-1"},
        )
        assert result.success is True
        assert result.items_processed == 1
        assert received_keys == ["session-1"]

    @pytest.mark.asyncio
    async def test_maintenance_retention_op(self, store: Any) -> None:
        from miniunicorn.runtime.maintenance_executor import MaintenanceExecutor

        executor = MaintenanceExecutor(store)
        result = await executor.execute(
            task_kind="MAINTENANCE",
            payload={"op": "retention"},
        )
        assert result.success is True

    @pytest.mark.asyncio
    async def test_unsupported_kind_fails_gracefully(self, store: Any) -> None:
        from miniunicorn.runtime.maintenance_executor import MaintenanceExecutor

        executor = MaintenanceExecutor(store)
        result = await executor.execute(
            task_kind="UNKNOWN_KIND", payload={}
        )
        assert result.success is False
        assert "unsupported" in result.detail

    @pytest.mark.asyncio
    async def test_missing_runner_fails_gracefully(self, store: Any) -> None:
        from miniunicorn.runtime.maintenance_executor import MaintenanceExecutor

        executor = MaintenanceExecutor(store)  # no dream_runner configured
        result = await executor.execute(task_kind="DREAM", payload={})
        assert result.success is False
        assert "not configured" in result.detail

    @pytest.mark.asyncio
    async def test_backup_op(self, store: Any, tmp_path) -> None:
        from miniunicorn.runtime.maintenance_executor import MaintenanceExecutor

        executor = MaintenanceExecutor(store)
        dest = str(tmp_path / "backup.db")
        result = await executor.execute(
            task_kind="MAINTENANCE",
            payload={"op": "backup", "dest_path": dest},
        )
        assert result.success is True
        import os
        assert os.path.exists(dest)

    @pytest.mark.asyncio
    async def test_wal_checkpoint_op(self, store: Any) -> None:
        from miniunicorn.runtime.maintenance_executor import MaintenanceExecutor

        executor = MaintenanceExecutor(store)
        result = await executor.execute(
            task_kind="MAINTENANCE",
            payload={"op": "wal_checkpoint"},
        )
        assert result.success is True


# ---------------------------------------------------------------------------
# VectorMemoryStore hardening tests (design §22.2)
# ---------------------------------------------------------------------------


class TestVectorMemoryStoreHardening:
    """WP7 vector store hardening: idempotency, scope, tombstones, rebuild.

    These tests require sqlite-vec. If sqlite-vec is unavailable, the
    tests are skipped (the NoOp fallback path is covered by existing
    tests).
    """

    @pytest.fixture
    def vector_store(self, tmp_path):
        """Create a real VectorMemoryStore (requires sqlite-vec)."""
        pytest.importorskip("sqlite_vec")
        from miniunicorn.runtime.sqlite.vector_memory_store import VectorMemoryStore

        store = VectorMemoryStore(
            tmp_path / "test_memory.db",
            embedding_dim=4,
            model_id="test-model",
        )
        if not store.enabled:
            pytest.skip("sqlite-vec not available")
        yield store
        store.close()

    def test_idempotent_index_same_source_revision(
        self, vector_store
    ) -> None:
        """Duplicate index with same source revision is idempotent (§22.2)."""
        vec = [0.1, 0.2, 0.3, 0.4]
        id1 = vector_store.index(
            "test text",
            vec,
            kind="history",
            source_identity="history.jsonl",
            source_revision="rev-1",
        )
        id2 = vector_store.index(
            "test text",
            vec,
            kind="history",
            source_identity="history.jsonl",
            source_revision="rev-1",
        )
        assert id1 == id2, "same source revision must dedup"
        assert vector_store.count() == 1

    def test_different_source_revision_creates_new_entry(
        self, vector_store
    ) -> None:
        """Different source revision creates a new entry (§22.2)."""
        vec = [0.1, 0.2, 0.3, 0.4]
        id1 = vector_store.index(
            "text v1",
            vec,
            kind="history",
            source_identity="history.jsonl",
            source_revision="rev-1",
        )
        id2 = vector_store.index(
            "text v2",
            vec,
            kind="history",
            source_identity="history.jsonl",
            source_revision="rev-2",
        )
        assert id1 != id2
        assert vector_store.count() == 2

    def test_tombstone_excludes_from_search_and_count(
        self, vector_store
    ) -> None:
        """Tombstoned entries are excluded from search and count (§22.2)."""
        vec = [0.1, 0.2, 0.3, 0.4]
        vector_store.index(
            "to be deleted",
            vec,
            kind="history",
            source_identity="history.jsonl",
            source_revision="rev-1",
        )
        assert vector_store.count() == 1

        n = vector_store.tombstone_by_source_revision(
            source_identity="history.jsonl",
            source_revision="rev-1",
        )
        assert n == 1
        assert vector_store.count() == 0
        # Search must not return tombstoned entries.
        results = vector_store.search(vec, k=5)
        assert results == []

    def test_scope_filtering_in_search(self, vector_store) -> None:
        """Scoped search filters by tenant/principal/agent/workspace (§22.2)."""
        vec = [0.1, 0.2, 0.3, 0.4]
        vector_store.index(
            "tenant A entry",
            vec,
            kind="history",
            scope={"tenant_id": "tenant-a", "workspace_id": "ws-1"},
        )
        vector_store.index(
            "tenant B entry",
            vec,
            kind="history",
            scope={"tenant_id": "tenant-b", "workspace_id": "ws-2"},
        )
        # Filter by tenant A — only one result.
        results_a = vector_store.search(
            vec, k=5, scope={"tenant_id": "tenant-a"}
        )
        assert len(results_a) == 1
        # No scope filter — both results.
        results_all = vector_store.search(vec, k=5)
        assert len(results_all) == 2

    def test_rebuild_reconstructs_from_authoritative_sources(
        self, vector_store
    ) -> None:
        """Rebuild wipes and re-indexes from authoritative entries (§22.2)."""
        vec = [0.1, 0.2, 0.3, 0.4]
        # Seed with some entries.
        vector_store.index(
            "old entry 1",
            vec,
            kind="history",
            source_identity="history.jsonl",
            source_revision="rev-old",
        )
        assert vector_store.count() == 1

        # Rebuild with new authoritative entries.
        entries = [
            {
                "text": "new entry 1",
                "kind": "history",
                "source_identity": "history.jsonl",
                "source_revision": "rev-new-1",
                "importance": 0.8,
            },
            {
                "text": "new entry 2",
                "kind": "episodic",
                "source_identity": "episodic.jsonl",
                "source_revision": "rev-new-2",
                "importance": 0.6,
            },
        ]
        count = vector_store.rebuild(
            embed_fn=lambda _text: vec,
            entries=entries,
        )
        assert count == 2
        assert vector_store.count() == 2
        # The old entry is gone.
        results = vector_store.search(vec, k=5)
        texts = [r["text"] for r in results]
        assert "old entry 1" not in texts
        assert "new entry 1" in texts
        assert "new entry 2" in texts

    def test_rebuild_is_idempotent(self, vector_store) -> None:
        """Running rebuild twice with same entries produces same state (§22.2)."""
        vec = [0.1, 0.2, 0.3, 0.4]
        entries = [
            {
                "text": "entry",
                "kind": "history",
                "source_identity": "history.jsonl",
                "source_revision": "rev-1",
            },
        ]
        count1 = vector_store.rebuild(
            embed_fn=lambda _text: vec, entries=entries
        )
        count2 = vector_store.rebuild(
            embed_fn=lambda _text: vec, entries=entries
        )
        assert count1 == count2 == 1
        assert vector_store.count() == 1


# ---------------------------------------------------------------------------
# Trigger integration tests (design §22.3, §29.16)
# ---------------------------------------------------------------------------


class TestDreamIdleTriggerDurableMode:
    """DreamIdleTrigger enqueues a durable task when callback is set (§22.3)."""

    @pytest.mark.asyncio
    async def test_durable_mode_enqueues_task(self, tmp_path) -> None:
        """When enqueue_callback is set, maybe_trigger calls it instead of create_task."""
        import asyncio
        from miniunicorn.agent.dream_trigger import DreamIdleTrigger

        # Minimal fake Dream with the attributes maybe_trigger reads.
        class FakeDreamStore:
            def get_last_dream_cursor(self):
                return "cursor-1"

            def read_unprocessed_history(self, *, since_cursor):
                return [{"content": "x"}] * 10  # exceeds min_entries

        class FakeDream:
            store = FakeDreamStore()

            async def run(self):
                return True

        enqueued: list[str] = []

        async def enqueue_cb(source_revision: str):
            enqueued.append(source_revision)
            return "task-id-1"

        trigger = DreamIdleTrigger(
            FakeDream(),  # type: ignore[arg-type]
            enabled=True,
            min_idle_seconds=0,
            min_entries=1,
            min_interval_s=0,
            enqueue_callback=enqueue_cb,
        )
        # Force idle condition.
        import time as _time
        trigger._last_user_activity_ts = _time.monotonic() - 1000
        trigger._last_trigger_ts = 0.0

        await trigger.maybe_trigger(active_session_keys=())
        # Let the event loop process any pending tasks.
        await asyncio.sleep(0)
        assert enqueued == ["cursor-1"], "enqueue callback must be called with the cursor"

    @pytest.mark.asyncio
    async def test_legacy_mode_uses_create_task(self, tmp_path) -> None:
        """Without enqueue_callback, maybe_trigger uses asyncio.create_task."""
        import asyncio
        from miniunicorn.agent.dream_trigger import DreamIdleTrigger

        class FakeDreamStore:
            def get_last_dream_cursor(self):
                return None

            def read_unprocessed_history(self, *, since_cursor):
                return [{"content": "x"}] * 10

        class FakeDream:
            store = FakeDreamStore()
            ran: list[bool] = []

            async def run(self):
                self.ran.append(True)  # type: ignore[attr-defined]
                return True

        dream = FakeDream()
        trigger = DreamIdleTrigger(
            dream,  # type: ignore[arg-type]
            enabled=True,
            min_idle_seconds=0,
            min_entries=1,
            min_interval_s=0,
        )
        import time as _time
        trigger._last_user_activity_ts = _time.monotonic() - 1000
        trigger._last_trigger_ts = 0.0

        await trigger.maybe_trigger(active_session_keys=())
        # The _dream_task should have been created.
        assert trigger._dream_task is not None
        # Wait for it to complete.
        await asyncio.sleep(0.05)
        assert dream.ran == [True]  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# CronService system enqueue callback tests (design §22.3, §29.16)
# ---------------------------------------------------------------------------


class TestCronSystemEnqueueCallback:
    """CronService enqueues durable tasks for system_event jobs (§22.3)."""

    @pytest.mark.asyncio
    async def test_system_event_job_uses_enqueue_callback(self, tmp_path) -> None:
        from miniunicorn.cron.service import CronService
        from miniunicorn.cron.types import (
            CronJob,
            CronJobState,
            CronPayload,
            CronSchedule,
        )

        cron = CronService(tmp_path / "cron.json")
        enqueued: list[str] = []

        async def system_enqueue_cb(job):
            enqueued.append(job.name)
            return "task-id"

        cron.system_enqueue_callback = system_enqueue_cb

        # Create a system_event job.
        job = CronJob(
            id="test-dream",
            name="dream",
            enabled=True,
            schedule=CronSchedule(kind="at", at_ms=1),
            payload=CronPayload(kind="system_event"),
            state=CronJobState(next_run_at_ms=1),
            created_at_ms=0,
            updated_at_ms=0,
        )
        # Directly invoke _execute_job to avoid timer/scheduling logic.
        await cron._execute_job(job)
        assert enqueued == ["dream"], "system_event job must use enqueue callback"
        assert job.state.last_status == "ok"

    @pytest.mark.asyncio
    async def test_agent_turn_job_uses_on_job(self, tmp_path) -> None:
        from miniunicorn.cron.service import CronService
        from miniunicorn.cron.types import (
            CronJob,
            CronJobState,
            CronPayload,
            CronSchedule,
        )

        cron = CronService(tmp_path / "cron.json")
        on_job_called: list[str] = []
        enqueue_called: list[str] = []

        async def on_job(job):
            on_job_called.append(job.name)
            return None

        async def system_enqueue_cb(job):
            enqueue_called.append(job.name)
            return "task-id"

        cron.on_job = on_job
        cron.system_enqueue_callback = system_enqueue_cb

        # Create an agent_turn job (not system_event).
        job = CronJob(
            id="test-agent",
            name="agent",
            enabled=True,
            schedule=CronSchedule(kind="at", at_ms=1),
            payload=CronPayload(kind="agent_turn"),
            state=CronJobState(next_run_at_ms=1),
            created_at_ms=0,
            updated_at_ms=0,
        )
        await cron._execute_job(job)
        assert on_job_called == ["agent"], "agent_turn job must use on_job"
        assert enqueue_called == [], "agent_turn job must not use enqueue callback"
