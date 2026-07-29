"""WP0 — Shared fixtures for crash-boundary characterization tests.

These fixtures construct minimal in-process AgentLoop stand-ins so the
recovery semantics can be characterized without spawning real Channel or
Provider traffic. They are intentionally lightweight: the goal is to pin
*current* behavior so WP3 can prove the durable path is equivalent.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from miniunicorn.agent.turn_persistence import (
    PENDING_USER_TURN_KEY,
    RUNTIME_CHECKPOINT_KEY,
    TurnPersistence,
)
from miniunicorn.bus.events import InboundMessage
from miniunicorn.session.manager import Session, SessionManager


@pytest.fixture
def crash_workspace(tmp_path: Path) -> Path:
    """Isolated workspace directory for a crash-boundary scenario."""
    ws = tmp_path / "crash-ws"
    ws.mkdir()
    return ws


@pytest.fixture
def crash_session_manager(crash_workspace: Path) -> SessionManager:
    """Fresh SessionManager rooted at the crash workspace."""
    return SessionManager(crash_workspace)


@pytest.fixture
def crash_session(crash_session_manager: SessionManager) -> Session:
    """Empty session key used by crash-boundary scenarios."""
    return crash_session_manager.get_or_create("websocket:crash-test:1")


@pytest.fixture
def crash_inbound_message() -> InboundMessage:
    """Minimal inbound user message for crash characterization."""
    return InboundMessage(
        channel="websocket",
        sender_id="test-user",
        chat_id="crash-test:1",
        content="hello, please reply",
        metadata={},
    )


@pytest.fixture
def persistence_host(crash_session_manager: SessionManager) -> SimpleNamespace:
    """Minimal host satisfying ``TurnPersistenceHost`` for crash tests."""
    from miniunicorn.agent.context import ContextBuilder
    from miniunicorn.agent.tools.registry import ToolRegistry

    return SimpleNamespace(
        sessions=crash_session_manager,
        tools=ToolRegistry(),
        context=ContextBuilder(Path(".")),
        max_tool_result_chars=16_000,
    )


@pytest.fixture
def crash_persistence(persistence_host: SimpleNamespace) -> TurnPersistence:
    """TurnPersistence instance bound to the crash workspace."""
    return TurnPersistence(persistence_host)


@pytest.fixture
def checkpoint_payload() -> dict[str, Any]:
    """A representative runtime checkpoint payload used by characterization."""
    return {
        "phase": "RUN",
        "inner_iteration": 3,
        "model_call_ordinal": 1,
        "completed_tool_call_ids": ["call_1", "call_2"],
        "pending_tool_call_ids": ["call_3"],
        "budget": {"input_tokens": 1200, "output_tokens": 300},
    }
