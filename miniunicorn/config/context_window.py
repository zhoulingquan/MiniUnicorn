"""Pure validation boundary for resolved context window tokens.

Runtime consumers use :func:`require_context_window` to validate that a model
has a concrete, positive context window integer. This module performs no
network access, no cache lookup, and no default guessing. Configuration-time
resolution (Hugging Face / ModelScope discovery) lives in
``miniunicorn.cli.models`` and must be called by save handlers before
persisting a model configuration.
"""

from __future__ import annotations


class UnresolvedModelContextError(RuntimeError):
    """Raised when a model lacks a resolved context window at runtime.

    Identifies the unusable model and tells the operator to complete its
    configuration.
    """

    def __init__(self, model: str) -> None:
        super().__init__(
            f"Model {model!r} has no resolved context window. "
            f"Complete its configuration by setting context_window_tokens "
            f"in the model configuration before starting or switching."
        )
        self.model = model


def require_context_window(model: str, configured_value: int | None) -> int:
    """Return a validated positive context window integer.

    Pure validation only — no network, no cache, no fallback, no default guess.

    Raises:
        UnresolvedModelContextError: when the value is missing or invalid,
            identifying the unusable model.
    """
    if isinstance(configured_value, int) and configured_value > 0:
        return configured_value
    raise UnresolvedModelContextError(model)
