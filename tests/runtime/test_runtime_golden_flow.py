"""Golden-flow parity test: lightweight and supervised produce same facts.

Task 14 Step 3: run the same deterministic Provider stub and task
scenario in both ``lightweight`` and ``supervised`` modes. Assert the
normalized durable facts match after volatile IDs, timestamps, lease
tokens, PIDs, and epochs are excluded (design §30 Task 14).

The test also verifies the exact final transcript and delivery target
are correct in both modes.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from miniunicorn.runtime.application import RuntimeInboundRequest
from miniunicorn.runtime.bootstrap import build_lightweight_runtime, build_supervised_runtime
from miniunicorn.runtime.ingress import build_inbound_envelope, local_request_scope
from miniunicorn.runtime.sqlite import SqliteRuntimeStore, open_connection
from miniunicorn.runtime.task_service import TaskService
from miniunicorn.session.manager import SessionManager
from tests.runtime.faults import collect_durable_facts
from tests.runtime.support.openai_stub import OpenAIStubServer, chat_completion

# ---------------------------------------------------------------------------
# Config builder (shared by both modes)
# ---------------------------------------------------------------------------


def _make_config(
    workspace: Path,
    api_base: str,
    *,
    mode: str,
) -> Any:
    """Build a root Config pointing at *api_base* with the given runtime mode."""
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
                "mode": mode,
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


def _normalize_facts(facts: dict[str, Any]) -> dict[str, Any]:
    """Normalize durable facts for parity comparison.

    Excludes volatile fields (outbox state, timestamps, UUIDs) that
    differ between lightweight and supervised modes due to delivery
    timing and process identity (design Task 14 Step 2).
    """
    # Deep-copy and strip outbox delivery state (timing-dependent).
    normalized = {
        "task_state": facts["task_state"],
        "session_sequence": facts["session_sequence"],
        "session_messages": facts["session_messages"],
        "model_states": facts["model_states"],
        "tool_states": facts["tool_states"],
        "outbox": [
            {
                "kind": row["kind"],
                "channel": row["channel"],
                "target": row["target"],
                "content": row["content"],
            }
            for row in facts["outbox"]
        ],
    }
    return normalized


async def _run_golden_scenario(
    config: Any,
    workspace: Path,
    api_base: str,
    *,
    mode: str,
) -> dict[str, Any]:
    """Run one golden task and return normalized durable facts.

    Submits one USER_TURN with content ``"hello"`` and waits for
    COMPLETED. The OpenAI stub responds with ``"final answer"`` so
    the session transcript is ``[("user","hello"),("assistant","final answer")]``.
    """
    if mode == "lightweight":
        resources = build_lightweight_runtime(config)
        await resources.start()
        try:
            return await _submit_and_collect(config, resources, workspace)
        finally:
            await resources.stop()
    elif mode == "supervised":
        resources = build_supervised_runtime(config)
        await resources.start()
        try:
            return await _submit_and_collect(config, resources, workspace)
        finally:
            await resources.stop()
    else:  # pragma: no cover
        raise ValueError(f"unknown mode: {mode}")


async def _submit_and_collect(
    config: Any,
    resources: Any,
    workspace: Path,
) -> dict[str, Any]:
    """Submit the golden task and collect durable facts."""
    from miniunicorn.config.runtime import resolve_runtime_paths

    resolved = resolve_runtime_paths(config.runtime, config.workspace_path)
    db_path = resolved.database_path_resolved or Path(resolved.database_path)
    test_conn = open_connection(db_path)
    test_store = SqliteRuntimeStore(test_conn)
    task_service = TaskService(test_store)
    sessions = SessionManager(config.workspace_path)

    try:
        scope = local_request_scope(config)
        request = RuntimeInboundRequest(
            content="hello",
            media=(),
            metadata={},
            session_key="golden-parity-session",
            channel="cli",
            channel_account="test-user",
            channel_message_id=None,
            scope=scope,
            target_key="direct",
        )
        envelope = build_inbound_envelope(request, now_ms=int(time.time() * 1000))
        handle = await task_service.submit(envelope)

        snapshot = await task_service.wait_terminal(scope, handle.task_id, timeout_s=30)
        assert snapshot.state == "COMPLETED", (
            f"task did not complete in {mode_label(resources)} mode: {snapshot.state}"
        )

        facts = collect_durable_facts(
            test_store,
            handle.task_id,
            "golden-parity-session",
            session_manager=sessions,
        )
        return facts
    finally:
        test_conn.close()


def mode_label(resources: Any) -> str:
    """Infer the runtime mode label from resources for error messages."""
    from miniunicorn.runtime.hosts.lightweight import LightweightHost
    from miniunicorn.runtime.hosts.supervised import SupervisedHost

    if isinstance(resources.host, LightweightHost):
        return "lightweight"
    if isinstance(resources.host, SupervisedHost):
        return "supervised"
    return "unknown"


# ---------------------------------------------------------------------------
# Parity test
# ---------------------------------------------------------------------------


@pytest.mark.slow
async def test_lightweight_and_supervised_produce_same_durable_facts(
    tmp_path: Path,
) -> None:
    """Both modes produce identical normalized durable facts (Task 14 Step 3).

    The same Provider stub, task content, session key, and delivery
    routing are used in both modes. After excluding volatile fields
    (timestamps, lease tokens, PIDs, epochs, delivery state), the
    durable facts must match exactly.
    """
    stub = OpenAIStubServer([chat_completion("final answer"), chat_completion("final answer")])
    stub.start()
    try:
        # Lightweight mode.
        lw_workspace = tmp_path / "lightweight"
        lw_workspace.mkdir()
        lw_config = _make_config(lw_workspace, stub.api_base, mode="lightweight")
        lw_facts = await _run_golden_scenario(
            lw_config, lw_workspace, stub.api_base, mode="lightweight"
        )

        # Supervised mode.
        sup_workspace = tmp_path / "supervised"
        sup_workspace.mkdir()
        sup_config = _make_config(sup_workspace, stub.api_base, mode="supervised")
        sup_facts = await _run_golden_scenario(
            sup_config, sup_workspace, stub.api_base, mode="supervised"
        )

        # Normalize for parity comparison.
        lw_normalized = _normalize_facts(lw_facts)
        sup_normalized = _normalize_facts(sup_facts)

        assert lw_normalized == sup_normalized, (
            "lightweight and supervised durable facts differ:\n"
            f"lightweight: {lw_normalized}\n"
            f"supervised:  {sup_normalized}"
        )

        # Verify task state and delivery routing are correct in both modes.
        assert lw_facts["task_state"] == "COMPLETED"
        assert sup_facts["task_state"] == "COMPLETED"
        assert lw_facts["session_sequence"] == sup_facts["session_sequence"]

        # The session transcript must contain at least the user message
        # and the assistant's final answer (in order).
        lw_roles = [role for role, _ in lw_facts["session_messages"]]
        assert "user" in lw_roles
        assert "assistant" in lw_roles
        assert lw_roles.index("user") < lw_roles.index("assistant"), (
            f"lightweight: user message must precede assistant: {lw_roles}"
        )
        sup_roles = [role for role, _ in sup_facts["session_messages"]]
        assert "user" in sup_roles
        assert "assistant" in sup_roles
        assert sup_roles.index("user") < sup_roles.index("assistant"), (
            f"supervised: user message must precede assistant: {sup_roles}"
        )

        # Verify delivery routing matches in both modes.
        assert len(lw_facts["outbox"]) == 1
        assert len(sup_facts["outbox"]) == 1
        assert lw_facts["outbox"][0]["channel"] == "cli"
        assert lw_facts["outbox"][0]["target"] == "direct"
        assert lw_facts["outbox"][0]["content"] == "final answer"
        assert sup_facts["outbox"][0]["channel"] == "cli"
        assert sup_facts["outbox"][0]["target"] == "direct"
        assert sup_facts["outbox"][0]["content"] == "final answer"

        # Exactly one Provider request per mode (no duplicate calls).
        assert stub.request_count == 2, (
            f"expected exactly 2 Provider requests (one per mode), got {stub.request_count}"
        )
    finally:
        stub.close()
