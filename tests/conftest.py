"""Root test configuration: hermetic network, deterministic core-test markers."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

_ORIGINAL_LOOKUP: object = None


@pytest.fixture(autouse=True)
def _offline_model_context_lookup():
    """Short-circuit HF/ModelScope context-window lookups during tests.

    AgentLoop auto-detects the context window via a live HuggingFace/ModelScope
    query whenever ``context_window_tokens`` is not configured (loop.py).
    Tests construct loops with fake model names (``test-model``,
    ``openai/gpt-4.1``) that can never resolve, so the query would either fail
    or wait on a network timeout. Return the default limit instead; tests that
    need a specific value pass ``context_window_tokens`` explicitly.

    IMPORTANT: this fixture must NOT depend on ``monkeypatch``. An autouse
    fixture that takes ``monkeypatch`` forces it to be set up before regular
    fixtures, reversing teardown order so ``monkeypatch`` restores run *after*
    fixture ``patch()`` exits and re-apply stale mocks (observed leaking
    ``loader.load_config``). We swap the module attribute directly instead.
    """
    import miniunicorn.cli.models as models_mod

    global _ORIGINAL_LOOKUP
    _ORIGINAL_LOOKUP = models_mod.get_model_context_limit

    def _fast(model: str, provider: str = "auto", *, raise_on_unknown: bool = False) -> int:
        return models_mod.DEFAULT_CONTEXT_LIMIT

    models_mod.get_model_context_limit = _fast
    yield
    models_mod.get_model_context_limit = _ORIGINAL_LOOKUP  # type: ignore[assignment]


@pytest.fixture(autouse=True)
def _restore_loguru_miniunicorn_activation():
    """Re-enable the ``miniunicorn`` loguru logger after every test.

    The CLI ``serve``/``api`` commands call ``logger.disable("miniunicorn")``
    to silence library logs in quiet mode, and that global activation state
    survives the test that invoked them. Without this restore, every later
    test asserting loguru output from ``miniunicorn.*`` modules (e.g. the
    task supervisor's background-exception log) silently loses those records.
    """
    yield
    from loguru import logger as _loguru

    _loguru.enable("miniunicorn")


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
