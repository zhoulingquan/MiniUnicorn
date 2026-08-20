"""ProviderRegistry: single owner of the runtime provider/model/context-window triple.

``AgentLoop`` and, through it, ``AgentRunner`` delegate their ``provider`` /
``model`` / ``context_window_tokens`` reads and writes to this small object so
provider hot-swaps converge on a single owner instead of being written into
several private attributes across modules.
"""

from __future__ import annotations

from miniunicorn.providers.base import LLMProvider


class ProviderRegistry:
    """Own the runtime provider/model/context-window triple for one loop.

    ``AgentRunner`` holds a reference at construction time; every
    ``runner.provider`` read reflects the registry's current ``provider``.
    The loop's ``provider`` / ``model`` / ``context_window_tokens``
    properties delegate here as well, so all swap paths (``_apply_provider_snapshot``,
    the gateway heartbeat, test overrides) converge on one object.
    """

    def __init__(
        self,
        provider: LLMProvider,
        model: str | None,
        context_window_tokens: int | None,
    ) -> None:
        self.provider: LLMProvider = provider
        self.model: str | None = model
        self.context_window_tokens: int | None = context_window_tokens

    def __repr__(self) -> str:
        return (
            f"ProviderRegistry(provider={self.provider!r}, model={self.model!r}, "
            f"context_window_tokens={self.context_window_tokens!r})"
        )
