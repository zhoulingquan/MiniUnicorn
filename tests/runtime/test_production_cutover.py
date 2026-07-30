"""Source-boundary tests for the CLI/API production cutover (Task 6).

These tests assert that the one-shot CLI and the OpenAI-compatible API
no longer reach into the legacy in-process ``AgentLoop.process_direct``
path and that the API factory receives a ``RuntimeApplication`` rather
than an ``AgentLoop``. They are intentionally source-level (no process
startup) so they break fast when the cutover regresses.
"""

from __future__ import annotations

from pathlib import Path


def test_cli_does_not_call_process_direct() -> None:
    """``agent`` command must route through ``RuntimeApplication``.

    Reading the source keeps the test hermetic — no event loop, no
    workspace bootstrap. Any residual ``.process_direct(`` call (the
    legacy in-process path) fails the cutover.
    """
    source = Path("miniunicorn/cli/commands.py").read_text(encoding="utf-8")
    assert ".process_direct(" not in source


def test_api_does_not_receive_agent_loop() -> None:
    """``create_app`` must store a ``RuntimeApplication``, not an AgentLoop."""
    source = Path("miniunicorn/api/server.py").read_text(encoding="utf-8")
    assert 'app["agent_loop"]' not in source
    assert "agent_loop.process_direct" not in source
