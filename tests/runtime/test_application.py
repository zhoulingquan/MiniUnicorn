"""Tests for the durable Runtime application façade (design §8.1, §11.3, Task 4).

Covers:

- ``RuntimeApplication.submit_and_wait`` returns a ``RuntimeTurnResult``
  with a terminal snapshot and a ``DurableReply``.
- ``stop_accepting`` rejects new submits without writing a task.
- ``wait`` and ``read_reply`` continue to work for already-accepted tasks.
- ``RealtimeSubscriptionHub`` delivers published events to subscribers.
- ``build_inbound_envelope`` produces a deterministic envelope with the
  correct dedup key and payload hash.
- ``build_internal_envelope`` produces an envelope with inline payload.
- ``local_request_scope`` derives a stable workspace_id from the config.
- ``read_final_reply`` returns ``None`` when no final reply exists and a
  populated ``DurableReply`` when one does.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from miniunicorn.runtime.application import (
    RuntimeApplication,
    RuntimeInboundRequest,
    RuntimeTurnResult,
)
from miniunicorn.runtime.ingress import (
    build_inbound_envelope,
    build_internal_envelope,
    local_request_scope,
)
from miniunicorn.runtime.models import (
    DurableReply,
    RequestScope,
)
from miniunicorn.runtime.realtime import RealtimeSubscriptionHub

# ---------------------------------------------------------------------------
# Fake store for façade tests
# ---------------------------------------------------------------------------


class _FakeStore:
    """Minimal store that auto-completes tasks for façade testing.

    Implements just enough of the ``TaskIngressStore`` + result-read
    surface to drive ``RuntimeApplication`` without a real Worker.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, dict[str, Any]] = {}
        self._replies: dict[str, DurableReply] = {}
        self._next_seq = 1

    # --- TaskIngressStore surface ---

    def submit_task(self, envelope: Any) -> Any:
        from miniunicorn.runtime.contracts import SubmitResult

        task_id = f"task-{self._next_seq}"
        self._next_seq += 1
        seq = self._next_seq
        self._tasks[task_id] = {
            "task_id": task_id,
            "state": "COMPLETED",
            "session_sequence": seq,
            "scope": envelope.scope,
        }
        # Auto-populate a final reply so read_final_reply works.
        self._replies[task_id] = DurableReply(
            content="world",
            outbox_id=1,
            metadata={"task_id": task_id},
        )
        return SubmitResult(status="ACCEPTED", task_id=task_id, session_sequence=seq)

    def submit_internal(self, envelope: Any) -> Any:
        from miniunicorn.runtime.contracts import SubmitResult

        task_id = f"internal-{self._next_seq}"
        self._next_seq += 1
        self._tasks[task_id] = {
            "task_id": task_id,
            "state": "COMPLETED",
            "session_sequence": 0,
            "scope": envelope.scope,
        }
        return SubmitResult(status="ACCEPTED", task_id=task_id, session_sequence=0)

    def read_task_snapshot(self, scope: Any, task_id: str) -> Any:
        from miniunicorn.runtime.models import TaskSnapshot

        info = self._tasks.get(task_id)
        if info is None:
            return None
        return TaskSnapshot(
            task_id=task_id,
            state=info["state"],
            checkpoint_phase="DONE",
            run_segment=0,
            root_attempt_count=1,
            max_root_attempts=3,
            recovery_pending=0,
            session_sequence=info["session_sequence"],
        )

    def read_final_reply(self, scope: Any, task_id: str) -> DurableReply | None:
        return self._replies.get(task_id)

    def append_control(self, control: Any) -> Any:
        from miniunicorn.runtime.contracts import ControlResult

        return ControlResult(status="APPENDED", control_id="ctrl-1")

    def read_task(self, task_id: str) -> Any:
        return self._tasks.get(task_id)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runtime_application() -> RuntimeApplication:
    """A RuntimeApplication backed by a fake auto-completing store."""
    from miniunicorn.runtime.task_service import TaskService

    store = _FakeStore()
    task_service = TaskService(store)
    realtime = RealtimeSubscriptionHub(capacity=16)
    return RuntimeApplication(
        task_service=task_service,
        result_store=store,
        realtime=realtime,
    )


@pytest.fixture
def sample_scope() -> RequestScope:
    return RequestScope(
        tenant_id="local",
        principal_id="user",
        agent_id="default",
        workspace_id="default",
    )


def _make_request(scope: RequestScope, **overrides: Any) -> RuntimeInboundRequest:
    defaults: dict[str, Any] = dict(
        content="hello",
        media=(),
        metadata={},
        session_key="api:test",
        channel="api",
        channel_account="user",
        channel_message_id="msg-1",
        scope=scope,
    )
    defaults.update(overrides)
    return RuntimeInboundRequest(**defaults)


# ---------------------------------------------------------------------------
# Tests: RuntimeApplication façade
# ---------------------------------------------------------------------------


class TestRuntimeApplication:
    """RuntimeApplication submit/wait/result façade (design §8.1, Task 4)."""

    @pytest.mark.asyncio
    async def test_submit_and_wait_returns_durable_reply(
        self, runtime_application: RuntimeApplication, sample_scope: RequestScope
    ) -> None:
        result = await runtime_application.submit_and_wait(
            _make_request(sample_scope),
            timeout_s=5,
        )
        assert isinstance(result, RuntimeTurnResult)
        assert result.snapshot.state == "COMPLETED"
        assert result.reply.content == "world"
        assert result.reply.outbox_id == 1

    @pytest.mark.asyncio
    async def test_submit_returns_task_handle(
        self, runtime_application: RuntimeApplication, sample_scope: RequestScope
    ) -> None:
        handle = await runtime_application.submit(_make_request(sample_scope))
        assert handle.task_id is not None
        assert handle.snapshot is not None

    @pytest.mark.asyncio
    async def test_stop_accepting_rejects_new_submit(
        self, runtime_application: RuntimeApplication, sample_scope: RequestScope
    ) -> None:
        runtime_application.stop_accepting()
        with pytest.raises(RuntimeError, match="draining"):
            await runtime_application.submit(_make_request(sample_scope))

    @pytest.mark.asyncio
    async def test_start_accepting_allows_submit(
        self, runtime_application: RuntimeApplication, sample_scope: RequestScope
    ) -> None:
        runtime_application.stop_accepting()
        runtime_application.start_accepting()
        handle = await runtime_application.submit(_make_request(sample_scope))
        assert handle.task_id is not None

    @pytest.mark.asyncio
    async def test_read_reply_returns_empty_for_unknown_task(
        self, runtime_application: RuntimeApplication, sample_scope: RequestScope
    ) -> None:
        reply = runtime_application.read_reply(sample_scope, "nonexistent")
        assert reply.content == ""
        assert reply.outbox_id is None

    @pytest.mark.asyncio
    async def test_read_reply_returns_durable_reply_for_completed_task(
        self, runtime_application: RuntimeApplication, sample_scope: RequestScope
    ) -> None:
        handle = await runtime_application.submit(_make_request(sample_scope))
        reply = runtime_application.read_reply(sample_scope, handle.task_id)
        assert reply.content == "world"
        assert reply.outbox_id == 1

    @pytest.mark.asyncio
    async def test_wait_returns_terminal_snapshot(
        self, runtime_application: RuntimeApplication, sample_scope: RequestScope
    ) -> None:
        handle = await runtime_application.submit(_make_request(sample_scope))
        snapshot = await runtime_application.wait(sample_scope, handle.task_id, timeout_s=5)
        assert snapshot.state == "COMPLETED"


# ---------------------------------------------------------------------------
# Tests: RealtimeSubscriptionHub
# ---------------------------------------------------------------------------


class TestRealtimeSubscriptionHub:
    """RealtimeSubscriptionHub bounded fan-out (design Task 4, Task 7)."""

    @pytest.mark.asyncio
    async def test_subscribe_receives_published_events(self) -> None:
        hub = RealtimeSubscriptionHub(capacity=16)
        async with hub.subscribe("task-1") as queue:
            hub.publish("task-1", {"kind": "delta", "text": "hello"})
            event = await asyncio.wait_for(queue.get(), timeout=1.0)
            assert event["kind"] == "delta"
            assert event["text"] == "hello"

    @pytest.mark.asyncio
    async def test_subscribe_does_not_receive_other_task_events(self) -> None:
        hub = RealtimeSubscriptionHub(capacity=16)
        async with hub.subscribe("task-1") as queue:
            hub.publish("task-2", {"kind": "delta"})
            # Should not receive task-2's event.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(queue.get(), timeout=0.1)

    @pytest.mark.asyncio
    async def test_full_queue_drops_event_and_increments_counter(self) -> None:
        hub = RealtimeSubscriptionHub(capacity=2)
        async with hub.subscribe("task-1") as _queue:
            # Fill the queue.
            hub.publish("task-1", {"i": 0})
            hub.publish("task-1", {"i": 1})
            # This one should be dropped.
            hub.publish("task-1", {"i": 2})
            assert hub.dropped_events == 1

    @pytest.mark.asyncio
    async def test_unsubscribe_cleans_up(self) -> None:
        hub = RealtimeSubscriptionHub(capacity=16)
        async with hub.subscribe("task-1"):
            pass
        # After unsubscribe, publishing should not crash.
        hub.publish("task-1", {"kind": "delta"})
        assert hub.dropped_events == 0


# ---------------------------------------------------------------------------
# Tests: Ingress helpers
# ---------------------------------------------------------------------------


class TestIngressHelpers:
    """build_inbound_envelope, build_internal_envelope, local_request_scope."""

    def test_build_inbound_envelope_is_deterministic(self, sample_scope: RequestScope) -> None:
        request = RuntimeInboundRequest(
            content="hello",
            media=("file1.txt",),
            metadata={"key": "value"},
            session_key="api:test",
            channel="api",
            channel_account="user",
            channel_message_id="msg-1",
            scope=sample_scope,
        )
        env1 = build_inbound_envelope(request, now_ms=1_000_000)
        env2 = build_inbound_envelope(request, now_ms=1_000_000)
        assert env1.payload_hash == env2.payload_hash
        assert env1.dedup_key == "api:api:test:msg-1"
        assert env1.task_kind == "USER_TURN"
        assert env1.priority == 100
        assert env1.received_at_ms == 1_000_000
        assert env1.payload_content is not None

    def test_build_inbound_envelope_dedup_key_none_without_channel_message_id(
        self, sample_scope: RequestScope
    ) -> None:
        request = RuntimeInboundRequest(
            content="hello",
            media=(),
            metadata={},
            session_key="cli:session",
            channel="cli",
            channel_account="local-user",
            channel_message_id=None,
            scope=sample_scope,
        )
        env = build_inbound_envelope(request, now_ms=1_000_000)
        assert env.dedup_key is None

    def test_build_inbound_envelope_payload_hash_matches(self, sample_scope: RequestScope) -> None:
        request = RuntimeInboundRequest(
            content="hello",
            media=("a.txt", "b.txt"),
            metadata={"k": "v"},
            session_key="api:test",
            channel="api",
            channel_account="user",
            channel_message_id="msg-1",
            scope=sample_scope,
        )
        env = build_inbound_envelope(request, now_ms=1_000_000)
        expected_payload = json.dumps(
            {
                "content": "hello",
                "media": ["a.txt", "b.txt"],
                "metadata": {"k": "v"},
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        expected_hash = hashlib.sha256(expected_payload).hexdigest()
        assert env.payload_hash == expected_hash
        assert env.payload_content == expected_payload

    def test_build_internal_envelope_has_inline_payload(self, sample_scope: RequestScope) -> None:
        env = build_internal_envelope(
            kind="DREAM",
            scope=sample_scope,
            session_key="dream:sess",
            dedup_key="dream:sess:rev1",
            payload={"topic": "memory"},
            priority=10,
            now_ms=2_000_000,
        )
        assert env.task_kind == "DREAM"
        assert env.dedup_key == "dream:sess:rev1"
        assert env.priority == 10
        assert env.received_at_ms == 2_000_000
        assert env.payload_content is not None
        expected = json.dumps(
            {"topic": "memory"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        assert env.payload_content == expected
        assert env.payload_hash == hashlib.sha256(expected).hexdigest()

    def test_local_request_scope_is_stable(self, tmp_path: Path) -> None:
        from miniunicorn.config.schema import Config

        config = Config.model_validate({"agents": {"defaults": {"workspace": str(tmp_path)}}})
        scope1 = local_request_scope(config)
        scope2 = local_request_scope(config)
        assert scope1.workspace_id == scope2.workspace_id
        assert scope1.tenant_id == "local"
        assert scope1.agent_id == "default"
        assert len(scope1.workspace_id) == 16

    def test_local_request_scope_different_workspaces_different_ids(self, tmp_path: Path) -> None:
        from miniunicorn.config.schema import Config

        ws1 = tmp_path / "ws1"
        ws2 = tmp_path / "ws2"
        ws1.mkdir()
        ws2.mkdir()
        config1 = Config.model_validate({"agents": {"defaults": {"workspace": str(ws1)}}})
        config2 = Config.model_validate({"agents": {"defaults": {"workspace": str(ws2)}}})
        scope1 = local_request_scope(config1)
        scope2 = local_request_scope(config2)
        assert scope1.workspace_id != scope2.workspace_id


# ---------------------------------------------------------------------------
# Tests: DurableReply model
# ---------------------------------------------------------------------------


class TestDurableReply:
    """DurableReply DTO (design Task 4)."""

    def test_durable_reply_is_frozen(self) -> None:
        reply = DurableReply(content="hello", outbox_id=42, metadata={"k": "v"})
        with pytest.raises(Exception):
            reply.content = "changed"  # type: ignore[misc]

    def test_durable_reply_defaults(self) -> None:
        reply = DurableReply(content="", outbox_id=None, metadata={})
        assert reply.content == ""
        assert reply.outbox_id is None
        assert reply.metadata == {}


# ---------------------------------------------------------------------------
# Tests: submit_and_wait result determinism (Task 11 Step 4)
# ---------------------------------------------------------------------------


def _complete_task_with_reply(
    store: Any,
    scope: RequestScope,
    make_inbound_envelope: Any,
    *,
    content: str,
    channel_message_id: str,
) -> str:
    """Submit, claim, mark-running, and complete a task with a FINAL_REPLY row.

    Returns the task_id. Used by the determinism race test so a reply
    exists for the racing OutboxSender to claim (Task 11 Step 4).
    """
    import hashlib
    import time

    from miniunicorn.runtime.contracts import ClaimRequest
    from miniunicorn.runtime.models import BlobWrite, CompletionWrite
    from miniunicorn.runtime.outbox_payload import encode_outbox_payload

    now_ms = int(time.time() * 1000)
    env = make_inbound_envelope(scope, channel_message_id=channel_message_id)
    submit = store.submit_task(env)
    assert submit.status == "ACCEPTED"
    result = store.claim_next(ClaimRequest(worker_id="test-worker", now_ms=now_ms, lease_ms=60_000))
    assert result.claimed is not None
    claim = result.claimed.claim
    store.mark_running(claim, now_ms=now_ms + 1)

    payload_bytes = encode_outbox_payload(content=content)
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
    completion = CompletionWrite(
        final_reply_blob_id=blob.blob_id,
        final_reply_hash=hashlib.sha256(payload_bytes).hexdigest(),
        final_reply_dedup_key=None,
        suppress_final=False,
        completed_at_ms=now_ms + 2,
        channel="websocket",
        channel_account="account-1",
        target_key="chat-race",
    )
    result = store.complete_with_outbox(claim, completion)
    assert result.outbox_id is not None
    return submit.task_id


class TestSubmitAndWaitResultDeterminism:
    """``submit_and_wait`` result retrieval stays deterministic against a
    racing OutboxSender (Task 11 Step 4).

    After a task becomes terminal, ``RuntimeApplication.submit_and_wait``
    (via ``read_reply`` → ``read_final_reply``) must return the durable
    final-reply content regardless of whether the OutboxSender claims the
    FINAL_REPLY row concurrently. Reply reads are independent from the
    Outbox delivery state (design §17.8, Task 7 Step 7), so a racing
    sender flipping the row through PENDING/SENDING/DELIVERED cannot mask
    a completed reply.
    """

    @pytest.mark.asyncio
    async def test_read_reply_returns_durable_content_across_100_races(
        self, store: Any, sample_scope: RequestScope, make_inbound_envelope: Any
    ) -> None:
        from miniunicorn.runtime.application import RuntimeApplication
        from miniunicorn.runtime.contracts import ClaimRequest  # noqa: F401
        from miniunicorn.runtime.models import DeliveryReceipt
        from miniunicorn.runtime.outbox import OutboxSender
        from miniunicorn.runtime.realtime import RealtimeSubscriptionHub
        from miniunicorn.runtime.task_service import TaskService

        runtime = RuntimeApplication(
            task_service=TaskService(store),
            result_store=store,
            realtime=RealtimeSubscriptionHub(capacity=16),
        )

        # Pre-create 100 completed tasks, each with a FINAL_REPLY outbox row.
        expected: dict[str, str] = {}
        for i in range(100):
            content = f"reply-{i}"
            task_id = _complete_task_with_reply(
                store,
                sample_scope,
                make_inbound_envelope,
                content=content,
                channel_message_id=f"race-msg-{i}",
            )
            expected[task_id] = content

        class _FastFakeSender:
            """ChannelSender that delivers immediately, racing read_reply."""

            async def send_with_receipt(self, channel_name: str, msg: Any) -> DeliveryReceipt:
                # A tiny delay spreads deliveries across the read window so
                # the 100 reads observe a mix of PENDING and DELIVERED rows.
                await asyncio.sleep(0.001)
                return DeliveryReceipt(status="DELIVERED")

            def get_channel_recovery(self, channel_name: str) -> str:
                return "NONE"

        sender = OutboxSender(
            store,
            _FastFakeSender(),
            sender_id="race-sender",
            poll_interval_s=0.001,
            lease_ms=60_000,
            send_timeout_s=5,
        )
        await sender.start()
        try:
            # Let the sender deliver a portion of the rows (~30ms), then
            # stop it so the outbox is frozen with a mix of DELIVERED and
            # PENDING rows — a snapshot of a mid-race outbox.
            await asyncio.sleep(0.03)
        finally:
            await sender.stop()

        # Now read every reply. Reads observe a mix of delivery_state
        # values (DELIVERED for rows the sender reached, PENDING for the
        # rest). Every read must return the durable content regardless of
        # the row's delivery state — this is the determinism guarantee
        # (design §17.8, Task 7 Step 7, Task 11 Step 4).
        observed_states: set[str] = set()
        for task_id, content in expected.items():
            reply = runtime.read_reply(sample_scope, task_id)
            assert reply.content == content, (
                f"reply content mismatch for {task_id}: got {reply.content!r}, want {content!r}"
            )
            observed_states.add(reply.metadata.get("delivery_state", ""))

        # The race must be real: reads must have observed both untouched
        # (PENDING) and sender-advanced (DELIVERED) rows, proving the
        # determinism assertion is exercised across delivery states —
        # not only against untouched PENDING rows.
        assert "PENDING" in observed_states, (
            f"race did not leave any PENDING rows: {observed_states}"
        )
        assert "DELIVERED" in observed_states, (
            f"sender did not deliver any rows during race window: {observed_states}"
        )
