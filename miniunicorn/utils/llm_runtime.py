"""Small helpers for passing the active LLM provider/model together."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass

from miniunicorn.providers.base import LLMProvider


@dataclass(frozen=True)
class LLMRuntime:
    provider: LLMProvider
    model: str


LLMRuntimeResolver = Callable[[], LLMRuntime]


def static_llm_runtime(provider: LLMProvider, model: str) -> LLMRuntimeResolver:
    runtime = LLMRuntime(provider=provider, model=model)
    return lambda: runtime


def resolve_llm_timeout(override: float | None = None) -> float | None:
    """Resolve the effective wall-clock LLM call timeout in seconds.

    Uses *override* when given; otherwise reads ``MINIUNICORN_LLM_TIMEOUT_S``
    (default 300). A resolved value ``<= 0`` disables the timeout (returns
    ``None``). Callers should wrap non-streaming provider calls with
    ``asyncio.wait_for(coro, timeout=resolve_llm_timeout())`` so a hung
    provider cannot block a per-session lock indefinitely.
    """
    timeout_s = override
    if timeout_s is None:
        raw = os.environ.get("MINIUNICORN_LLM_TIMEOUT_S", "300").strip()
        try:
            timeout_s = float(raw)
        except (TypeError, ValueError):
            timeout_s = 300.0
    if timeout_s is not None and timeout_s <= 0:
        return None
    return timeout_s
