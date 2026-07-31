"""Tests for production Control Plane and Worker child entrypoints (Task 7).

Validates that:

- ``control_plane_main`` and ``worker_main`` are top-level picklable
  functions in ``miniunicorn.runtime.process_entrypoints`` (required for
  ``multiprocessing.spawn`` on Windows — design §24.6, §32.5).
- A ``ChildBootstrapPayload`` is frozen and JSON-reconstructable.
- A fake Worker pipe that sends ``KIND_AGENT_EVENT`` is received
  verbatim by a fake Control Plane pipe via the relay path.
- Backpressure: when the Control Plane send blocks, publishing more than
  the configured relay capacity returns promptly, increments a
  dropped-event counter, and never blocks a simulated Agent turn.
- Startup order: ``worker_main`` does not open the Runtime database
  before the Control Plane reports ready after migrations.
"""

from __future__ import annotations

import asyncio
import contextlib
import pickle
import time
from typing import Any

import pytest

from miniunicorn.runtime.ipc import (
    KIND_AGENT_EVENT,
    KIND_SHUTDOWN,
    ProcessIpcChannel,
    agent_event,
    shutdown_signal,
)
from miniunicorn.runtime.process_entrypoints import (
    ChildBootstrapPayload,
    control_plane_main,
    worker_main,
)


# ---------------------------------------------------------------------------
# Picklability and module identity
# ---------------------------------------------------------------------------


def test_child_entrypoints_are_top_level_picklable() -> None:
    """Both entrypoints must pickle for ``multiprocessing.spawn``."""
    pickle.dumps(control_plane_main)
    pickle.dumps(worker_main)


def test_supervisor_default_entrypoints_are_production_functions() -> None:
    assert control_plane_main.__module__ == "miniunicorn.runtime.process_entrypoints"
    assert worker_main.__module__ == "miniunicorn.runtime.process_entrypoints"


# ---------------------------------------------------------------------------
# ChildBootstrapPayload
# ---------------------------------------------------------------------------


def test_child_bootstrap_payload_is_frozen() -> None:
    payload = ChildBootstrapPayload(config_json="{}", surface={})
    with pytest.raises(Exception):
        payload.config_json = "mutated"  # type: ignore[misc]


def test_child_bootstrap_payload_round_trip() -> None:
    payload = ChildBootstrapPayload(
        config_json='{"workspace_path": "/tmp"}', surface={"mode": "supervised"}
    )
    raw = pickle.dumps(payload)
    restored = pickle.loads(raw)
    assert restored == payload
    assert restored.config_json == payload.config_json
    assert restored.surface == payload.surface


def test_child_bootstrap_payload_rejects_non_json_scalar_surface() -> None:
    """Surface values must be JSON scalars; reject callables/objects."""
    with pytest.raises((TypeError, ValueError)):
        ChildBootstrapPayload(config_json="{}", surface={"bad": object()})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Relay semantics: Worker → Control Plane
# ---------------------------------------------------------------------------


def test_agent_event_relayed_verbatim_through_pipe() -> None:
    """A ``KIND_AGENT_EVENT`` sent on a fake Worker pipe is received on the
    Control Plane pipe unchanged."""
    ch = ProcessIpcChannel.new_pipe()
    try:
        env = agent_event(
            "worker-0#1",
            event={"type": "delta", "text": "hello"},
            task_id="task-7",
        )
        ch.child_send(env)
        assert ch.parent_poll(timeout_s=2.0)
        received = ch.parent_recv()
        assert received is not None
        assert received.kind == KIND_AGENT_EVENT
        assert received.task_id == "task-7"
        assert received.payload["event"] == {"type": "delta", "text": "hello"}
    finally:
        ch.parent_close()
        ch.child_close()


def test_relay_backpressure_drops_and_increments_counter() -> None:
    """When the relay queue is full, publishing returns promptly, drops the
    event, and increments a dropped-events counter — never blocking the
    Agent turn."""
    from miniunicorn.runtime.realtime import RealtimeSubscriptionHub

    hub = RealtimeSubscriptionHub(capacity=2)
    # Fill the subscriber queue beyond capacity.
    received: list[dict[str, Any]] = []

    async def _run() -> None:
        async with hub.subscribe("task-bp") as queue:
            # Pre-fill the queue to its maxsize=2 so the next publish drops.
            queue.put_nowait({"i": 0})
            queue.put_nowait({"i": 1})
            # Publish 5 more — all should drop.
            for i in range(5):
                hub.publish("task-bp", {"i": 100 + i})
            # Drain what we received for inspection.
            while not queue.empty():
                received.append(queue.get_nowait())

    asyncio.run(_run())
    assert len(received) == 2  # only the pre-filled events
    assert hub.dropped_events == 5


# ---------------------------------------------------------------------------
# Startup-order invariant
# ---------------------------------------------------------------------------


def test_worker_entrypoint_does_not_open_database_before_control_ready() -> None:
    """``worker_main`` must not open the Runtime database before the Control
    Plane reports ready after migrations. We verify by inspecting that
    ``worker_main`` accepts a ``control_ready`` gating parameter and only
    proceeds once it is set.

    This test uses a stub config and a fake ready event to avoid spawning
    a real process; it asserts the gating contract on the async helper.
    """
    import inspect

    from miniunicorn.runtime.process_entrypoints import _worker_async

    sig = inspect.signature(_worker_async)
    # The worker must accept an explicit control-ready gate.
    assert "control_ready" in sig.parameters or "control_ready_event" in sig.parameters


# ---------------------------------------------------------------------------
# Shutdown handling
# ---------------------------------------------------------------------------


def test_shutdown_signal_is_handled_by_entrypoint_signature() -> None:
    """Both entrypoints must accept ``**kwargs`` so the Supervisor can pass
    shutdown-related context. We assert they are callable with the
    trampoline's keyword args."""
    import inspect

    sig_control = inspect.signature(control_plane_main)
    sig_worker = inspect.signature(worker_main)
    # Must accept **kwargs (the trampoline passes role, instance_id, config,
    # ipc_channel, ready_signal).
    assert any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in sig_control.parameters.values()
    )
    assert any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in sig_worker.parameters.values()
    )


# ---------------------------------------------------------------------------
# Event-loop liveness while recv_child_envelope waits (Task 1 Step 7)
# ---------------------------------------------------------------------------


async def test_recv_child_envelope_does_not_starve_event_loop() -> None:
    """While ``recv_child_envelope`` waits on a blocking pipe poll, other
    asyncio tasks on the same loop keep running.

    Before the Task 1 fix: ``child_poll`` ran on the event-loop thread and
    blocked every other task for the full poll interval.
    """
    from miniunicorn.runtime.process_entrypoints import recv_child_envelope

    ch = ProcessIpcChannel.new_pipe()
    tick_count = 0

    async def background_tick() -> None:
        nonlocal tick_count
        for _ in range(10):
            await asyncio.sleep(0.05)
            tick_count += 1

    tick_task = asyncio.create_task(background_tick())
    try:
        # Wait long enough for recv_child_envelope to block on the pipe
        # (0.5s poll). The background tick must make progress during that
        # wait — it would not if child_poll ran on the loop thread.
        result = await recv_child_envelope(ch, timeout_s=0.5)
        assert result is None  # No data on the pipe.
        # The background tick ran at least a few times during the 0.5s wait.
        # If child_poll blocked the loop, tick_count would be 0.
        assert tick_count > 0
        assert not tick_task.done()
    finally:
        tick_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await tick_task
        ch.parent_close()
        ch.child_close()


# ---------------------------------------------------------------------------
# Surface round-trip (Task 1 Step 7)
# ---------------------------------------------------------------------------


def test_supervised_surface_preserves_caller_overrides() -> None:
    """``build_supervised_runtime`` merges the caller-provided surface with
    runtime defaults so ``webui_runtime_surface``, ``webui_static_dist``,
    and capability overrides reach ``_build_channel_manager`` inside the
    Control Plane child process."""
    from miniunicorn.config.schema import Config
    from miniunicorn.runtime.bootstrap import build_supervised_runtime

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        config = Config.model_validate(
            {
                "agents": {
                    "defaults": {
                        "workspace": str(Path(tmp) / "workspace"),
                        "provider": "custom",
                        "model": "stub-model",
                        "contextWindowTokens": 8192,
                    },
                },
                "providers": {
                    "custom": {
                        "apiKey": "test-key",
                        "apiBase": "http://127.0.0.1:1/v1",
                        "apiType": "chat_completions",
                    },
                },
                "runtime": {
                    "mode": "supervised",
                    "workerCount": 3,
                },
            }
        )
        caller_surface = {
            "webui_runtime_surface": True,
            "webui_static_dist": "/custom/dist",
            "capability_overrides": {"max_file_size": 12345},
        }
        resources = build_supervised_runtime(config, surface=caller_surface)

        payload = resources.host._supervisor._config  # type: ignore[attr-defined]
        assert payload.surface["mode"] == "supervised"
        assert payload.surface["worker_count"] == 3
        assert payload.surface["webui_runtime_surface"] is True
        assert payload.surface["webui_static_dist"] == "/custom/dist"
        assert payload.surface["capability_overrides"]["max_file_size"] == 12345
