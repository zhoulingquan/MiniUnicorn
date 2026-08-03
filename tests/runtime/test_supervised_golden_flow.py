"""Supervised golden-flow test: a real spawned Worker executes a real turn.

This test drives the production supervised topology (one Control Plane +
three Worker processes) against a local OpenAI-compatible HTTP stub and
asserts that a task submitted after readiness is claimed and completed by
a real spawned Worker (Task 1 Step 2).

The stub Provider returns a deterministic Chat Completions response with
no tool calls and ``finish_reason="stop"`` so the Agent completes in
exactly one model call. The test does NOT pass a Provider object across
the spawn boundary — the Worker reconstructs its own Provider from the
config's ``apiBase``.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from miniunicorn.runtime.bootstrap import build_supervised_runtime


def _make_supervised_config(workspace: Path, api_base: str) -> Any:
    """Build a root Config with supervised runtime and a chat_completions provider."""
    from miniunicorn.config.schema import Config

    return Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "workspace": str(workspace),
                    "provider": "custom",
                    "model": "stub-model",
                    "contextWindowTokens": 32768,
                },
            },
            "providers": {
                "custom": {
                    "apiKey": "test-key",
                    "apiBase": api_base,
                    "apiType": "chat_completions",
                },
            },
            "runtime": {
                "mode": "supervised",
                "workerCount": 3,
                "heartbeatIntervalS": 2,
                "leaseTimeoutS": 30,
                "progressTimeoutS": 60,
                "queuePollMaxMs": 500,
                "leaseScanIntervalS": 5,
                "outboxLeaseTimeoutS": 30,
                "channelSendTimeoutS": 10,
                "sqliteBusyTimeoutMs": 1000,
                "shutdownGraceS": 5,
            },
        }
    )


@pytest.mark.slow
async def test_supervised_processes_real_turn_after_readiness(
    tmp_path: Path,
) -> None:
    """A real spawned Worker claims and completes a task after readiness.

    Before the Task 1 fix: the Worker's event loop is starved by the
    synchronous ``child_poll`` call, so the task times out while all
    three Worker records report ready.

    Task 24 Step 2 strengthens the postconditions: after terminal
    completion the durable session has user then assistant content,
    exactly one FINAL_REPLY Outbox fact, exactly one logical Provider
    decision, and exactly one stub request. After ``resources.stop()``
    no child process may remain alive.
    """
    from miniunicorn.runtime.application import RuntimeInboundRequest
    from miniunicorn.runtime.ingress import (
        build_inbound_envelope,
        local_request_scope,
    )
    from miniunicorn.runtime.sqlite import SqliteRuntimeStore, open_connection
    from miniunicorn.runtime.task_service import TaskService
    from miniunicorn.session.manager import SessionManager
    from tests.runtime.support.openai_stub import OpenAIStubServer, chat_completion

    # 1. Start the OpenAI-compatible stub server with one deterministic response.
    stub = OpenAIStubServer([chat_completion("final answer")])
    stub.start()
    try:
        # 2. Build the supervised runtime config pointing at the stub.
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        config = _make_supervised_config(workspace, stub.api_base)

        # 3. Start the supervised runtime (Control Plane + 3 Workers).
        resources = build_supervised_runtime(config)
        await resources.start()
        try:
            assert resources.host.ready_workers() == 3

            # 4. Open a SEPARATE parent test connection to the same SQLite
            #    file the Control Plane created. The test process submits
            #    and waits from here; the Workers claim from their own
            #    connections.
            from miniunicorn.config.runtime import resolve_runtime_paths

            resolved = resolve_runtime_paths(config.runtime, config.workspace_path)
            db_path = resolved.database_path_resolved or Path(resolved.database_path)
            test_conn = open_connection(db_path)
            test_store = SqliteRuntimeStore(test_conn)
            task_service = TaskService(test_store)

            try:
                # 5. Submit an inbound task via the production ingress path.
                scope = local_request_scope(config)
                request = RuntimeInboundRequest(
                    content="hello",
                    media=(),
                    metadata={},
                    session_key="golden-flow-session",
                    channel="cli",
                    channel_account="test-user",
                    channel_message_id=None,
                    scope=scope,
                    # Task 7: immutable delivery target copied into the
                    # FINAL_REPLY Outbox row (design §17.8). "direct"
                    # denotes an interactive CLI session; the Worker
                    # copies this verbatim so the Agent's final text
                    # cannot alter routing.
                    target_key="direct",
                )
                envelope = build_inbound_envelope(request, now_ms=int(time.time() * 1000))
                handle = await task_service.submit(envelope)

                # 6. Wait for terminal state. Before the fix this times out
                #    because the Worker event loop is starved.
                snapshot = await task_service.wait_terminal(scope, handle.task_id, timeout_s=30)
                assert snapshot.state == "COMPLETED"
                # Exactly one Provider request — the stub was called once.
                assert len(stub.requests) == 1, (
                    f"expected exactly 1 stub request, got {len(stub.requests)}"
                )

                # Task 24 Step 2: strengthened postconditions.
                # (a) Durable session has user then assistant content (read
                #     directly from the session file via SessionManager so
                #     we do not depend on the Worker's process-local cache).
                session_snapshot = SessionManager(workspace).load_fresh("golden-flow-session")
                roles = [m.get("role") for m in session_snapshot.messages]
                assert "user" in roles, f"durable session missing user message: roles={roles}"
                assert "assistant" in roles, (
                    f"durable session missing assistant message: roles={roles}"
                )
                user_idx = roles.index("user")
                asst_idx = roles.index("assistant")
                assert user_idx < asst_idx, f"user must precede assistant: roles={roles}"

                # (b) Exactly one final-reply Outbox fact for this task.
                outbox_count = test_conn.execute(
                    "SELECT COUNT(*) AS n FROM outbox WHERE task_id=?",
                    (handle.task_id,),
                ).fetchone()["n"]
                assert outbox_count == 1, f"expected exactly 1 outbox row, got {outbox_count}"
                outbox_kind = test_conn.execute(
                    "SELECT message_kind FROM outbox WHERE task_id=?",
                    (handle.task_id,),
                ).fetchone()["message_kind"]
                assert outbox_kind == "FINAL_REPLY", (
                    f"expected FINAL_REPLY outbox kind, got {outbox_kind!r}"
                )

                # (c) Exactly one logical Provider decision: exactly one
                #     COMPLETED model attempt for this task.
                completed_attempts = test_conn.execute(
                    "SELECT COUNT(*) AS n FROM model_attempts "
                    "WHERE task_id=? AND state='COMPLETED'",
                    (handle.task_id,),
                ).fetchone()["n"]
                assert completed_attempts == 1, (
                    f"expected exactly 1 COMPLETED model attempt, got {completed_attempts}"
                )
            finally:
                test_conn.close()
        finally:
            await resources.stop()
            # Task 24 Step 2: after stop(), no child process may remain alive.
            post_snapshot = resources.host.snapshot()
            alive_children = [c for c in post_snapshot["children"] if c.get("alive")]
            assert not alive_children, f"child processes still alive after stop(): {alive_children}"
    finally:
        stub.close()
