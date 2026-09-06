"""Root conftest — keep the whole suite hermetic.

``AgentLoop`` resolves an unset ``context_window_tokens`` through a
machine-global learning table and the Hugging Face API, and fail-louds with
``RuntimeError`` when neither can answer.  Tests must not depend on machine
state or the network, so this conftest:

- sets ``ERZA_NO_AUTO_LOOKUP=1`` for the whole run (no network), and
- wraps ``get_model_context_limit`` so the fail-loud ``RuntimeError``
  degrades to the documented default (65_536) instead of aborting
  ``AgentLoop`` construction.

Tests that assert specific window behaviour still pass
``context_window_tokens`` explicitly (see ``tests/agent/conftest.py``).
"""

import os

import pytest

os.environ.setdefault("ERZA_NO_AUTO_LOOKUP", "1")


@pytest.fixture(autouse=True)
def _hermetic_model_context(monkeypatch: pytest.MonkeyPatch) -> None:
    from erza.providers import model_catalog as models_module

    real = models_module.get_model_context_limit

    def _hermetic(model, provider="auto", *, raise_on_unknown=False):
        try:
            return real(model, provider, raise_on_unknown=raise_on_unknown)
        except RuntimeError:
            return models_module.DEFAULT_CONTEXT_LIMIT

    monkeypatch.setattr(models_module, "get_model_context_limit", _hermetic)
