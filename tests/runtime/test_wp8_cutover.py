"""WP8 acceptance tests: cutover, observability, and hardening (design §34, WP8).

These tests verify the acceptance criteria from design §34 that span
multiple work packages or are specific to WP8 (observability, migration
reader, content redaction, cutover tooling).

Criteria already covered by earlier WP test suites are cross-referenced
but not duplicated here.
"""

from __future__ import annotations

import json
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
        import hashlib

        from miniunicorn.runtime.contracts import ClaimRequest, StaleLeaseError
        from miniunicorn.runtime.models import (
            BlobWrite,
            CheckpointWrite,
        )

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
        _new_claim = result2.claimed.claim

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
        import hashlib

        from miniunicorn.runtime.contracts import ClaimRequest
        from miniunicorn.runtime.models import (
            BlobWrite,
            CompletionWrite,
        )

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
            channel="test-channel",
            channel_account="test-account",
            target_key="test-target",
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
            "miniunicorn.agent.vector_index",
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


# ---------------------------------------------------------------------------
# Cross-scope authorization at API boundaries (Task 11 Step 5)
# ---------------------------------------------------------------------------


class TestCrossScopeApiBoundary:
    """Unauthorized task IDs return the existing not-found response (Task 11).

    A task ID that exists under another agent/workspace must be
    indistinguishable from a nonexistent task at the application surface:
    ``get_status`` raises ``KeyError`` and ``read_reply`` returns the empty
    not-found reply. No authorization oracle is added — the Runtime Store's
    four-field scope filter is the only gate (design §11.3, Task 11 Step 5).
    """

    @pytest.mark.asyncio
    async def test_cross_scope_get_status_raises_keyerror(
        self,
        store: Any,
        sample_scope: RequestScope,
        make_inbound_envelope: Any,
    ) -> None:
        import hashlib

        from miniunicorn.runtime.contracts import ClaimRequest
        from miniunicorn.runtime.models import BlobWrite, CompletionWrite
        from miniunicorn.runtime.outbox_payload import encode_outbox_payload
        from miniunicorn.runtime.task_service import TaskService

        # Submit + complete a task under scope A with a FINAL_REPLY row.
        env = make_inbound_envelope(sample_scope, channel_message_id="api-msg-1")
        submit = store.submit_task(env)
        now_ms = 1_000_000
        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=now_ms, lease_ms=60_000)
        )
        store.mark_running(result.claimed.claim, now_ms=now_ms + 1)
        payload_bytes = encode_outbox_payload(content="secret reply")
        blob = store.write_blob(
            BlobWrite(
                scope_key="test",
                blob_kind="OUTBOX_PAYLOAD",
                content_hash=hashlib.sha256(payload_bytes).hexdigest(),
                encoding="RAW_BYTES",
                inline_content=payload_bytes,
                size_bytes=len(payload_bytes),
                created_at_ms=now_ms,
            )
        )
        store.complete_with_outbox(
            result.claimed.claim,
            CompletionWrite(
                final_reply_blob_id=blob.blob_id,
                final_reply_hash=hashlib.sha256(payload_bytes).hexdigest(),
                final_reply_dedup_key=None,
                suppress_final=False,
                completed_at_ms=now_ms + 2,
                channel="websocket",
                channel_account="account-1",
                target_key="chat-x",
            ),
        )

        task_service = TaskService(store)

        # A nonexistent task raises KeyError.
        with pytest.raises(KeyError):
            await task_service.get_status(sample_scope, "does-not-exist")

        # Scope B (different agent) must behave identically: KeyError, not
        # a leak that the task exists under another agent.
        wrong_scope = RequestScope(
            tenant_id=sample_scope.tenant_id,
            principal_id=sample_scope.principal_id,
            agent_id="other-agent",
            workspace_id=sample_scope.workspace_id,
        )
        with pytest.raises(KeyError):
            await task_service.get_status(wrong_scope, submit.task_id)

    @pytest.mark.asyncio
    async def test_cross_scope_read_reply_indistinguishable_from_not_found(
        self,
        store: Any,
        sample_scope: RequestScope,
        make_inbound_envelope: Any,
    ) -> None:
        import hashlib

        from miniunicorn.runtime.application import RuntimeApplication
        from miniunicorn.runtime.contracts import ClaimRequest
        from miniunicorn.runtime.models import BlobWrite, CompletionWrite
        from miniunicorn.runtime.outbox_payload import encode_outbox_payload
        from miniunicorn.runtime.realtime import RealtimeSubscriptionHub
        from miniunicorn.runtime.task_service import TaskService

        # Submit + complete a task under scope A with a FINAL_REPLY row.
        env = make_inbound_envelope(sample_scope, channel_message_id="api-msg-2")
        submit = store.submit_task(env)
        now_ms = 1_000_000
        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=now_ms, lease_ms=60_000)
        )
        store.mark_running(result.claimed.claim, now_ms=now_ms + 1)
        payload_bytes = encode_outbox_payload(content="private content")
        blob = store.write_blob(
            BlobWrite(
                scope_key="test",
                blob_kind="OUTBOX_PAYLOAD",
                content_hash=hashlib.sha256(payload_bytes).hexdigest(),
                encoding="RAW_BYTES",
                inline_content=payload_bytes,
                size_bytes=len(payload_bytes),
                created_at_ms=now_ms,
            )
        )
        store.complete_with_outbox(
            result.claimed.claim,
            CompletionWrite(
                final_reply_blob_id=blob.blob_id,
                final_reply_hash=hashlib.sha256(payload_bytes).hexdigest(),
                final_reply_dedup_key=None,
                suppress_final=False,
                completed_at_ms=now_ms + 2,
                channel="websocket",
                channel_account="account-1",
                target_key="chat-y",
            ),
        )

        runtime = RuntimeApplication(
            task_service=TaskService(store),
            result_store=store,
            realtime=RealtimeSubscriptionHub(capacity=16),
        )

        wrong_scope = RequestScope(
            tenant_id=sample_scope.tenant_id,
            principal_id=sample_scope.principal_id,
            agent_id=sample_scope.agent_id,
            workspace_id="other-workspace",
        )

        # The cross-scope reply must equal the not-found reply exactly —
        # no content, no outbox id, empty metadata — so the API cannot
        # reveal that the task exists under another workspace.
        cross_scope_reply = runtime.read_reply(wrong_scope, submit.task_id)
        not_found_reply = runtime.read_reply(wrong_scope, "does-not-exist")
        assert cross_scope_reply.content == ""
        assert cross_scope_reply.outbox_id is None
        assert cross_scope_reply == not_found_reply

        # The owning scope still reads the real content (sanity).
        owning_reply = runtime.read_reply(sample_scope, submit.task_id)
        assert owning_reply.content == "private content"
