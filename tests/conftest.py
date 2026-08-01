"""Root-level pytest configuration.

Defines the deterministic core-test marker policy: tests whose
repository-relative path matches one of ``CORE_PREFIXES`` are automatically
marked with ``pytest.mark.core`` (unless explicitly marked ``slow``), so
the fast CI gate (``pytest -m core``) selects a stable, machine-independent
subset without requiring per-test decorators.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

# Repository-relative path prefixes that identify "core" tests — the fast
# correctness gate covering orchestration, sessions, providers, config, and
# security. Channel adapters, integration suites, and top-level misc tests
# are intentionally excluded so the gate stays fast.
CORE_PREFIXES = (
    "tests/agent/test_runner_",
    "tests/agent/test_loop_runner_integration.py",
    "tests/agent/test_loop_progress.py",
    "tests/agent/test_loop_save_turn.py",
    "tests/agent/test_turn_",
    "tests/session/",
    "tests/providers/",
    "tests/config/",
    "tests/security/",
    "tests/runtime/",
)


def is_core_test_path(path: str) -> bool:
    """Return ``True`` if ``path`` is a core test by repository-relative prefix.

    Backslashes (Windows) are normalized to forward slashes before matching.
    """

    normalized = path.replace("\\", "/")
    return any(normalized.startswith(prefix) for prefix in CORE_PREFIXES)


def pytest_collection_modifyitems(config, items):
    """Auto-mark core tests with ``pytest.mark.core``.

    Tests explicitly marked ``slow`` are skipped so they are excluded from
    the fast gate even when their path would otherwise qualify.
    """

    for item in items:
        repo_relative = item.path.relative_to(config.rootpath).as_posix()
        if is_core_test_path(repo_relative) and "slow" not in item.keywords:
            item.add_marker(pytest.mark.core)


class FakeOutboundPort:
    """Test-only OutboundPort that captures OutboundRequests.

    Captures every :class:`~miniunicorn.agent.ports.OutboundRequest` in
    ``self.requests``.  When a *callback* is provided it is invoked with an
    :class:`~miniunicorn.bus.events.OutboundMessage` constructed from the
    request so legacy test assertions (``sent[0].channel``, ``sent[0].media``,
    etc.) keep working.

    Returns an :class:`~miniunicorn.agent.ports.OutboundReceipt` with an
    incrementing ``outbox_id`` starting at 1.
    """

    def __init__(self, callback=None):
        self._callback = callback
        self.requests: list = []
        self._next_id = 1

    async def enqueue(self, request):
        from miniunicorn.agent.ports import OutboundReceipt

        self.requests.append(request)
        receipt = OutboundReceipt(outbox_id=self._next_id, dedup_key="")
        self._next_id += 1
        if self._callback is not None:
            from miniunicorn.bus.events import OutboundMessage

            msg = OutboundMessage(
                channel=request.channel,
                chat_id=request.target_key,
                content=request.content,
                media=list(request.media),
                metadata={},
                buttons=[],
            )
            await self._callback(msg)
        return receipt


@contextmanager
def bind_fake_outbound(callback=None):
    """Bind a :class:`FakeOutboundPort` to the current turn for tests.

    Usage::

        with bind_fake_outbound() as port:
            tool = MessageTool()
            await tool.execute(content="hi", channel="tg", chat_id="1")
        assert port.requests[0].content == "hi"

    When *callback* is provided it receives an ``OutboundMessage`` for each
    enqueue so existing ``sent``-list assertions keep working.
    """
    from miniunicorn.agent.turn_runtime import (
        TurnRuntime,
        bind_turn_runtime,
        reset_turn_runtime,
    )

    port = FakeOutboundPort(callback)
    rt = TurnRuntime(turn_id="test-turn", session_key="test-session")
    rt.outbound_port = port
    token = bind_turn_runtime(rt)
    try:
        yield port
    finally:
        reset_turn_runtime(token)
