"""ContextGovernor built-in pipeline contract."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from erza.agent.context_governor import ContextGovernor, GovernanceContext


def test_default_governor_executes_exact_declared_builtin_pipeline(monkeypatch) -> None:
    monkeypatch.setattr("erza.agent.context_governor.entry_points", lambda **_kw: [])
    governor = ContextGovernor()
    calls: list[str] = []
    for strategy in governor._strategies:
        name = strategy.name

        def record(messages, _ctx, *, _name=name):
            calls.append(_name)
            return messages

        monkeypatch.setattr(strategy, "apply", record)

    context = GovernanceContext(
        spec=SimpleNamespace(session_key="test"),
        tools=MagicMock(),
        provider=MagicMock(),
        iteration=0,
    )

    governor.govern([], context)

    assert calls == list(ContextGovernor.BUILTIN_PIPELINE)
