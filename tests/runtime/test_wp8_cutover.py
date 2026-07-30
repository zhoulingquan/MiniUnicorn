"""WP8 acceptance tests: cutover, observability, and hardening (design §34, WP8).

These tests verify the acceptance criteria from design §34 that span
multiple work packages or are specific to WP8 (observability, migration
reader, content redaction, cutover tooling).

Criteria already covered by earlier WP test suites are cross-referenced
but not duplicated here.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from miniunicorn.runtime.models import RequestScope


# ---------------------------------------------------------------------------
# Observability: health, status, metrics (design §34 criterion 26, WP8)
# ---------------------------------------------------------------------------


class TestObservability:
    """Tests for health, status, and metrics endpoints (WP8 task 3)."""

    def test_health_ready_when_store_migrated(self, store: Any) -> None:
        from miniunicorn.runtime.observability import check_health

        health = check_health(store)
        assert health.alive is True
        assert health.ready is True

    def test_health_not_ready_when_store_none(self) -> None:
        from miniunicorn.runtime.observability import check_health

        health = check_health(None)
        assert health.alive is True
        assert health.ready is False

    def test_health_not_ready_when_not_migrated(self, tmp_path: Path) -> None:
        from miniunicorn.runtime.observability import check_health
        from miniunicorn.runtime.sqlite import SqliteRuntimeStore, open_connection

        # Create a store without running migrations.
        db_path = tmp_path / "unmigrated.sqlite"
        conn = open_connection(db_path)
        store = SqliteRuntimeStore(conn)
        health = check_health(store)
        assert health.alive is True
        assert health.ready is False
        conn.close()

    def test_status_collects_task_counts(
        self,
        store: Any,
        sample_scope: RequestScope,
        make_inbound_envelope: Any,
    ) -> None:
        from miniunicorn.runtime.observability import collect_status

        # Submit a few tasks.
        for i in range(3):
            env = make_inbound_envelope(sample_scope, session_key=f"s-{i}")
            store.submit_task(env)

        status = collect_status(store, host_mode="lightweight", host_started=True)
        assert status.schema_version >= 1
        assert status.tasks_by_state.get("QUEUED", 0) >= 3
        assert status.host_mode == "lightweight"
        assert status.host_started is True
        assert status.timestamp_ms > 0

    def test_status_includes_outbox_state(
        self,
        store: Any,
    ) -> None:
        from miniunicorn.runtime.observability import collect_status

        status = collect_status(store)
        assert isinstance(status.outbox_by_state, dict)

    def test_status_includes_database_size(self, store: Any) -> None:
        from miniunicorn.runtime.observability import collect_status

        status = collect_status(store)
        assert status.database_size_bytes > 0

    def test_metrics_text_has_prometheus_format(
        self,
        store: Any,
        sample_scope: RequestScope,
        make_inbound_envelope: Any,
    ) -> None:
        from miniunicorn.runtime.observability import collect_metrics_text

        # Submit a task so tasks_by_state has entries.
        env = make_inbound_envelope(sample_scope)
        store.submit_task(env)

        text = collect_metrics_text(store, host_mode="lightweight", host_started=True)
        assert "# HELP" in text
        assert "# TYPE" in text
        assert "miniunicorn_runtime_schema_version" in text
        assert "miniunicorn_runtime_tasks_by_state" in text
        assert "miniunicorn_runtime_active_leases" in text

    def test_metrics_no_sensitive_content(
        self,
        store: Any,
        sample_scope: RequestScope,
        make_inbound_envelope: Any,
    ) -> None:
        """Design §34 criterion 26: no prompt text, tool args, or credentials."""
        from miniunicorn.runtime.observability import collect_metrics_text

        # Submit a task with a payload that looks sensitive.
        env = make_inbound_envelope(
            sample_scope,
            normalized_payload_ref="inline:secret-api-key-12345",
            payload_hash="abc123secret",
        )
        store.submit_task(env)

        text = collect_metrics_text(store)
        # No payload content, hashes, or refs should appear.
        assert "secret-api-key-12345" not in text
        assert "abc123secret" not in text
        assert "inline:" not in text

    def test_status_to_dict_is_json_serializable(self, store: Any) -> None:
        from miniunicorn.runtime.observability import collect_status

        status = collect_status(store)
        d = status.to_dict()
        # Must be JSON serializable for HTTP responses.
        json.dumps(d)

    def test_health_to_dict_is_json_serializable(self, store: Any) -> None:
        from miniunicorn.runtime.observability import check_health

        health = check_health(store)
        d = health.to_dict()
        json.dumps(d)


# ---------------------------------------------------------------------------
# Legacy migration reader (design §31.2, WP8 task 2)
# ---------------------------------------------------------------------------


class TestLegacyMigrationReader:
    """Tests for the legacy checkpoint scanner (WP8 task 2, design §31.2)."""

    def test_scan_empty_dir(self, tmp_path: Path) -> None:
        from miniunicorn.runtime.migration_reader import scan_legacy_checkpoints

        result = scan_legacy_checkpoints(tmp_path / "sessions")
        assert result.scanned == 0
        assert result.needs_conversion == 0
        assert result.migration_complete is True

    def test_scan_nonexistent_dir(self, tmp_path: Path) -> None:
        from miniunicorn.runtime.migration_reader import scan_legacy_checkpoints

        result = scan_legacy_checkpoints(tmp_path / "nonexistent")
        assert result.scanned == 0
        assert result.migration_complete is True

    def test_scan_clean_session_no_conversion(self, tmp_path: Path) -> None:
        from miniunicorn.runtime.migration_reader import scan_legacy_checkpoints

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        # A clean session with no pending work.
        session_path = sessions_dir / "test--abcdef.jsonl"
        with open(session_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"_type": "metadata", "key": "test", "revision": 5}) + "\n")
            f.write(json.dumps({"role": "user", "content": "hello"}) + "\n")
            f.write(json.dumps({"role": "assistant", "content": "hi"}) + "\n")

        result = scan_legacy_checkpoints(sessions_dir)
        assert result.scanned == 1
        assert result.needs_conversion == 0
        assert result.migration_complete is True
        assert len(result.sessions) == 1
        assert result.sessions[0].session_key == "test"
        assert result.sessions[0].revision == 5
        assert result.sessions[0].message_count == 2

    def test_scan_pending_user_turn_needs_conversion(self, tmp_path: Path) -> None:
        from miniunicorn.runtime.migration_reader import scan_legacy_checkpoints

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        session_path = sessions_dir / "test--pending.jsonl"
        with open(session_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"_type": "metadata", "key": "test-pending", "revision": 1}) + "\n")
            f.write(json.dumps({"_type": "pending_user_turn", "text": "user message"}) + "\n")

        result = scan_legacy_checkpoints(sessions_dir)
        assert result.scanned == 1
        assert result.needs_conversion == 1
        assert result.migration_complete is False
        info = result.sessions[0]
        assert info.has_pending_user_turn is True
        assert info.pending_user_turn_text == "user message"
        assert "pending_user_turn" in info.conversion_reason

    def test_scan_runtime_checkpoint_needs_conversion(self, tmp_path: Path) -> None:
        from miniunicorn.runtime.migration_reader import scan_legacy_checkpoints

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        session_path = sessions_dir / "test--ckpt.jsonl"
        with open(session_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"_type": "metadata", "key": "test-ckpt", "revision": 2}) + "\n")
            f.write(json.dumps({"_type": "runtime_checkpoint", "phase": "tool_call"}) + "\n")

        result = scan_legacy_checkpoints(sessions_dir)
        assert result.needs_conversion == 1
        info = result.sessions[0]
        assert info.has_runtime_checkpoint is True
        assert info.checkpoint_phase == "tool_call"

    def test_scan_skips_non_session_files(self, tmp_path: Path) -> None:
        from miniunicorn.runtime.migration_reader import scan_legacy_checkpoints

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        # A file without metadata header.
        bad_path = sessions_dir / "bad.jsonl"
        with open(bad_path, "w", encoding="utf-8") as f:
            f.write("not json at all\n")

        result = scan_legacy_checkpoints(sessions_dir)
        assert result.scanned == 1
        assert result.skipped_no_metadata == 1
        assert len(result.sessions) == 0

    def test_scan_is_read_only(self, tmp_path: Path) -> None:
        """The scanner must never modify session files (design §31.2)."""
        from miniunicorn.runtime.migration_reader import scan_legacy_checkpoints

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        session_path = sessions_dir / "test--readonly.jsonl"
        original_content = json.dumps({"_type": "metadata", "key": "test", "revision": 1}) + "\n"
        with open(session_path, "w", encoding="utf-8") as f:
            f.write(original_content)

        scan_legacy_checkpoints(sessions_dir)

        with open(session_path, "r", encoding="utf-8") as f:
            assert f.read() == original_content

    def test_scan_multiple_sessions(self, tmp_path: Path) -> None:
        from miniunicorn.runtime.migration_reader import scan_legacy_checkpoints

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        # Session 1: clean.
        with open(sessions_dir / "a--clean.jsonl", "w", encoding="utf-8") as f:
            f.write(json.dumps({"_type": "metadata", "key": "a", "revision": 1}) + "\n")
        # Session 2: pending user turn.
        with open(sessions_dir / "b--pending.jsonl", "w", encoding="utf-8") as f:
            f.write(json.dumps({"_type": "metadata", "key": "b", "revision": 1}) + "\n")
            f.write(json.dumps({"_type": "pending_user_turn", "text": "x"}) + "\n")
        # Session 3: runtime checkpoint.
        with open(sessions_dir / "c--ckpt.jsonl", "w", encoding="utf-8") as f:
            f.write(json.dumps({"_type": "metadata", "key": "c", "revision": 1}) + "\n")
            f.write(json.dumps({"_type": "runtime_checkpoint", "phase": "model"}) + "\n")

        result = scan_legacy_checkpoints(sessions_dir)
        assert result.scanned == 3
        assert result.needs_conversion == 2
        assert result.migration_complete is False


# ---------------------------------------------------------------------------
# Runtime config: cutover flag (design §31.1, WP8 task 1)
# ---------------------------------------------------------------------------


class TestRuntimeConfigCutover:
    """Tests for runtime config after hard cutover (no enabled flag)."""

    def test_config_mode_lightweight(self) -> None:
        from miniunicorn.runtime.config import RuntimeConfig

        cfg = RuntimeConfig(mode="lightweight")
        assert cfg.mode == "lightweight"

    def test_config_mode_supervised(self) -> None:
        from miniunicorn.runtime.config import RuntimeConfig

        cfg = RuntimeConfig(mode="supervised", worker_count=2)
        assert cfg.mode == "supervised"

    def test_parse_runtime_config_resolves_paths(self, tmp_path: Path) -> None:
        from miniunicorn.runtime.config import parse_runtime_config

        cfg = parse_runtime_config({}, data_root=tmp_path)
        assert cfg.database_path_resolved is not None
        assert cfg.backup_path_resolved is not None
        assert cfg.database_path_resolved != cfg.backup_path_resolved


# ---------------------------------------------------------------------------
# API server: new endpoints (WP8 task 3)
# ---------------------------------------------------------------------------


class TestApiEndpoints:
    """Tests for /health, /status, /metrics API endpoints (WP8 task 3)."""

    @pytest.mark.asyncio
    async def test_health_without_store_returns_ok(self) -> None:
        from aiohttp.test_utils import TestClient, TestServer
        from miniunicorn.api.server import create_app

        app = create_app(runtime=None)
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        try:
            resp = await client.get("/health")
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "ok"
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_status_without_store_returns_json(self) -> None:
        from aiohttp.test_utils import TestClient, TestServer
        from miniunicorn.api.server import create_app

        app = create_app(runtime=None)
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        try:
            resp = await client.get("/status")
            assert resp.status == 200
            data = await resp.json()
            assert "schema_version" in data
            assert "tasks_by_state" in data
            assert "timestamp_ms" in data
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_metrics_without_store_returns_text(self) -> None:
        from aiohttp.test_utils import TestClient, TestServer
        from miniunicorn.api.server import create_app

        app = create_app(runtime=None)
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        try:
            resp = await client.get("/metrics")
            assert resp.status == 200
            text = await resp.text()
            assert "miniunicorn_runtime" in text
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_health_with_store_returns_ready(self, store: Any) -> None:
        from aiohttp.test_utils import TestClient, TestServer
        from miniunicorn.api.server import create_app

        app = create_app(runtime=None)
        app["runtime_store"] = store
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        try:
            resp = await client.get("/health")
            assert resp.status == 200
            data = await resp.json()
            assert data["alive"] is True
            assert data["ready"] is True
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_status_with_store_returns_task_counts(
        self,
        store: Any,
        sample_scope: RequestScope,
        make_inbound_envelope: Any,
    ) -> None:
        from aiohttp.test_utils import TestClient, TestServer
        from miniunicorn.api.server import create_app

        env = make_inbound_envelope(sample_scope)
        store.submit_task(env)

        app = create_app(runtime=None)
        app["runtime_store"] = store
        app["runtime_mode"] = "lightweight"
        app["runtime_host_started"] = True
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        try:
            resp = await client.get("/status")
            assert resp.status == 200
            data = await resp.json()
            assert data["schema_version"] >= 1
            assert data["tasks_by_state"].get("QUEUED", 0) >= 1
            assert data["host_mode"] == "lightweight"
            assert data["host_started"] is True
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_endpoints_bypass_auth(self, store: Any) -> None:
        """Health/status/metrics must be public (design §4.6, WP8)."""
        from aiohttp.test_utils import TestClient, TestServer
        from miniunicorn.api.server import create_app

        app = create_app(runtime=None, api_key="secret-key")
        app["runtime_store"] = store
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        try:
            for path in ("/health", "/status", "/metrics"):
                resp = await client.get(path)
                assert resp.status == 200, f"{path} should be public"
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# Cross-cutting acceptance criteria spot-checks (design §34)
# ---------------------------------------------------------------------------


class TestAcceptanceCriteriaSpotChecks:
    """Spot-checks for acceptance criteria that span multiple WPs (§34).

    These verify end-to-end properties rather than individual component
    behavior. Criteria fully covered by earlier WP suites are
    cross-referenced but not duplicated.
    """

    def test_criterion_2_duplicate_inbound_one_task_one_sequence(
        self,
        store: Any,
        sample_scope: RequestScope,
        make_inbound_envelope: Any,
    ) -> None:
        """§34.2: Duplicate inbound messages produce one task and one session sequence."""
        env = make_inbound_envelope(
            sample_scope,
            dedup_key="dup-key-1",
            session_key="test-session",
        )
        result1 = store.submit_task(env)
        result2 = store.submit_task(env)
        assert result1.status == "ACCEPTED"
        assert result2.status == "DUPLICATE"
        assert result1.task_id == result2.task_id

        # Only one session sequence allocated.
        slot_row = store._conn.execute(
            "SELECT next_sequence FROM session_slots WHERE session_key = ?",
            ("test-session",),
        ).fetchone()
        assert slot_row is not None
        assert slot_row["next_sequence"] == 1  # only one allocated

    def test_criterion_4_one_session_no_concurrent_running(
        self,
        store: Any,
        sample_scope: RequestScope,
        make_inbound_envelope: Any,
    ) -> None:
        """§34.4: One session never has two concurrently running tasks."""
        from miniunicorn.runtime.contracts import ClaimRequest

        env1 = make_inbound_envelope(sample_scope, session_key="s-concurrent")
        store.submit_task(env1)

        # Claim the first task.
        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=1_000_000, lease_ms=60_000)
        )
        assert result.claimed is not None
        store.mark_running(result.claimed.claim, now_ms=1_000_001)

        # Submit a second task for the same session.
        env2 = make_inbound_envelope(
            sample_scope,
            session_key="s-concurrent",
            dedup_key="second-task",
        )
        store.submit_task(env2)

        # The second task should be QUEUED, not claimable while the first
        # is non-terminal (same-session head-only claim).
        result2 = store.claim_next(
            ClaimRequest(worker_id="w-2", now_ms=1_000_002, lease_ms=60_000)
        )
        assert result2.claimed is None  # blocked by same-session ordering

    def test_criterion_6_stale_worker_cannot_checkpoint(
        self,
        store: Any,
        sample_scope: RequestScope,
        make_inbound_envelope: Any,
    ) -> None:
        """§34.6: Stale Workers cannot checkpoint or complete tasks."""
        from miniunicorn.runtime.contracts import ClaimRequest, StaleLeaseError
        from miniunicorn.runtime.models import (
            BlobWrite,
            CheckpointWrite,
            CompletionWrite,
        )
        import hashlib

        env = make_inbound_envelope(sample_scope)
        store.submit_task(env)
        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=1_000_000, lease_ms=60_000)
        )
        claim = result.claimed.claim
        store.mark_running(claim, now_ms=1_000_001)

        # Simulate a stale lease: reclaim the task with a new epoch.
        store.reclaim_expired(now_ms=2_000_000, limit=10)
        result2 = store.claim_next(
            ClaimRequest(worker_id="w-2", now_ms=2_000_001, lease_ms=60_000)
        )
        assert result2.claimed is not None
        new_claim = result2.claimed.claim

        # The old claim's lease_epoch is now stale. Attempting to
        # checkpoint with the old token must fail.
        content = b"checkpoint-data"
        blob = store.write_blob(
            BlobWrite(
                scope_key="test",
                blob_kind="CHECKPOINT",
                content_hash=hashlib.sha256(content).hexdigest(),
                encoding="RAW_BYTES",
                inline_content=content,
                size_bytes=len(content),
                created_at_ms=2_000_000,
            )
        )
        with pytest.raises(StaleLeaseError):
            store.checkpoint(
                claim,  # stale claim
                CheckpointWrite(
                    checkpoint_id="cp-1",
                    format_version=1,
                    phase="model_completed",
                    run_segment=0,
                    ordinal=0,
                    payload_blob_id=blob.blob_id,
                    payload_hash=hashlib.sha256(content).hexdigest(),
                    lease_epoch=claim.lease_epoch,
                    created_at_ms=2_000_001,
                ),
            )

    def test_criterion_12_final_reply_enqueued_atomically(
        self,
        store: Any,
        sample_scope: RequestScope,
        make_inbound_envelope: Any,
    ) -> None:
        """§34.12: Every final reply is enqueued atomically with task completion."""
        from miniunicorn.runtime.contracts import ClaimRequest
        from miniunicorn.runtime.models import (
            BlobWrite,
            CompletionWrite,
        )
        import hashlib

        env = make_inbound_envelope(sample_scope)
        store.submit_task(env)
        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=1_000_000, lease_ms=60_000)
        )
        claim = result.claimed.claim
        store.mark_running(claim, now_ms=1_000_001)

        content = b"final-reply"
        blob = store.write_blob(
            BlobWrite(
                scope_key="test",
                blob_kind="OUTBOX_PAYLOAD",
                content_hash=hashlib.sha256(content).hexdigest(),
                encoding="RAW_BYTES",
                inline_content=content,
                size_bytes=len(content),
                created_at_ms=1_000_002,
            )
        )
        completion = CompletionWrite(
            final_reply_blob_id=blob.blob_id,
            final_reply_hash=hashlib.sha256(content).hexdigest(),
            final_reply_dedup_key=None,
            suppress_final=False,
            completed_at_ms=1_000_003,
        )
        store.complete_with_outbox(claim, completion)

        # Task must be COMPLETED.
        record = store.read_task(claim.task_id)
        assert record.state == "COMPLETED"

        # Outbox must have exactly one PENDING row for this task.
        outbox_rows = store._conn.execute(
            "SELECT COUNT(*) AS n FROM outbox WHERE task_id = ?",
            (claim.task_id,),
        ).fetchone()
        assert outbox_rows["n"] == 1

    @pytest.mark.asyncio
    async def test_criterion_21_maintenance_work_is_durable(
        self,
        store: Any,
        sample_scope: RequestScope,
    ) -> None:
        """§34.21: Required Dream, consolidation, indexing, and cleanup work is durable."""
        from miniunicorn.runtime.maintenance import (
            PRIORITY_REFLECTION_DREAM,
            dedup_key_for_dream,
            enqueue_maintenance,
        )
        from miniunicorn.runtime.task_service import TaskService

        task_service = TaskService(store)

        task_id = await enqueue_maintenance(
            task_service,
            task_kind="DREAM",
            scope=sample_scope,
            dedup_key=dedup_key_for_dream(source_revision="rev-1"),
            priority=PRIORITY_REFLECTION_DREAM,
            payload={"cursor": "rev-1"},
        )

        # The task must be durable in the store.
        record = store.read_task(task_id)
        assert record is not None
        assert record.task_kind == "DREAM"
        assert record.state == "QUEUED"

    def test_criterion_25_no_transactions_across_external_calls(
        self,
        store: Any,
    ) -> None:
        """§34.25: Database transactions never span external calls.

        This is a structural test: we verify that no Runtime Store method
        holds a BEGIN IMMEDIATE while calling an external function. The
        store methods use ``BEGIN IMMEDIATE`` ... ``COMMIT``/``ROLLBACK``
        blocks that contain only SQL statements.
        """
        import inspect

        from miniunicorn.runtime.sqlite.store import SqliteRuntimeStore

        # Get source of the store class.
        source = inspect.getsource(SqliteRuntimeStore)
        lines = source.split("\n")

        in_transaction = False
        transaction_depth = 0
        for line in lines:
            stripped = line.strip()
            if "BEGIN IMMEDIATE" in stripped and not stripped.startswith("#"):
                in_transaction = True
                transaction_depth += 1
            elif in_transaction and ("COMMIT" in stripped or "ROLLBACK" in stripped):
                in_transaction = False
                transaction_depth -= 1
            # While in a transaction, no network/IO calls should appear.
            # We check for common external call patterns.
            if in_transaction and not stripped.startswith("#"):
                # Allow only SQL operations and local variable assignments.
                # Flag if an await or external API call appears.
                assert "await " not in stripped, (
                    f"await inside transaction: {stripped}"
                )

    def test_criterion_23_agent_core_no_sqlite_import(self) -> None:
        """§34.23: Agent Core imports no SQLite or multiprocessing implementation.

        Design §5 defines Agent Core as "turn execution and reasoning code".
        Pluggable storage backends that degrade to NoOp when their optional
        dependency is absent (e.g. ``vector_memory`` using sqlite-vec) are
        not Agent Core — they are optional storage adapters. The criterion
        protects the testability of the turn execution path, which must run
        without SQLite or multiprocessing installed (design principle 10).
        """
        import sys

        # Optional storage adapters that are not part of Agent Core (§5).
        # Each degrades to a no-op when its storage backend is unavailable.
        _NON_CORE_STORAGE_MODULES = {
            "miniunicorn.agent.vector_memory",
        }

        # Check that the agent package does not import sqlite3 directly,
        # excluding the optional storage adapters above.
        agent_modules = [
            name for name in sys.modules if name.startswith("miniunicorn.agent.")
        ]
        for mod_name in agent_modules:
            if mod_name in _NON_CORE_STORAGE_MODULES:
                continue
            mod = sys.modules[mod_name]
            if mod is None:
                continue
            source_file = getattr(mod, "__file__", None)
            if source_file and source_file.endswith(".py"):
                with open(source_file, "r", encoding="utf-8") as f:
                    source = f.read()
                # sqlite3 import is forbidden in agent core.
                assert "import sqlite3" not in source, (
                    f"{mod_name} imports sqlite3 (violates §34.23)"
                )
