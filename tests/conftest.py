"""Root test configuration: keep model-context lookups hermetic (no network)."""

from __future__ import annotations

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
