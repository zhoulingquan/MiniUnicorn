"""AgentLoop provider/preset switching mixin.

Holds the runtime model/provider swap machinery that lets an :class:`AgentLoop`
hot-swap its LLM provider or apply a named model preset between turns. The
methods here are extracted from ``miniunicorn.agent.loop.AgentLoop`` purely to
keep that module focused on orchestration; ``AgentLoop`` re-combines them
through multiple inheritance (see :class:`ProviderSwitchingMixin`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from miniunicorn.agent import model_presets as preset_helpers
from miniunicorn.providers.factory import ProviderSnapshot

if TYPE_CHECKING:
    from miniunicorn.agent.loop import AgentLoop


class ProviderSwitchingMixin:
    """Runtime provider / model preset swapping for :class:`AgentLoop`.

    These methods read and mutate several ``self`` attributes that are owned by
    :class:`AgentLoop` (``provider``, ``model``, ``context_window_tokens``,
    ``runner``, ``subagents``, ``consolidator``, ``dream``, ``model_presets``,
    ``_active_preset``, ``_provider_signature``, ``_default_selection_signature``,
    ``_provider_snapshot_loader``, ``_preset_snapshot_loader``,
    ``_runtime_model_publisher``). They are kept as a mixin so the entire
    swap pipeline lives in one place.
    """

    def _sync_subagent_runtime_limits(self: "AgentLoop") -> None:
        """Keep subagent runtime limits aligned with mutable loop settings."""
        self.subagents.max_iterations = self.max_iterations

    def _apply_provider_snapshot(
        self: "AgentLoop",
        snapshot: ProviderSnapshot,
        *,
        publish_update: bool = True,
        model_preset: str | None = None,
    ) -> None:
        """Swap model/provider for future turns without disturbing an active one."""
        provider = snapshot.provider
        model = snapshot.model
        context_window_tokens = snapshot.context_window_tokens
        # Auto-detect when snapshot didn't carry a concrete value.
        # Resolution: built-in table → cache → Hugging Face API → fail-loud.
        if context_window_tokens is None:
            from miniunicorn.cli.models import get_model_context_limit

            context_window_tokens = get_model_context_limit(model, raise_on_unknown=True)
        old_model = self.model
        self.provider = provider
        self.model = model
        self.context_window_tokens = context_window_tokens
        self.runner.provider = provider
        self.subagents.set_provider(provider, model)
        self.consolidator.set_provider(provider, model, context_window_tokens)
        self.dream.set_provider(provider, model)
        self._provider_signature = snapshot.signature
        if publish_update and self._runtime_model_publisher is not None:
            self._runtime_model_publisher(
                self.model,
                model_preset if model_preset is not None else self.model_preset,
            )
        logger.info("Runtime model switched for next turn: {} -> {}", old_model, model)

    def _refresh_provider_snapshot(self: "AgentLoop") -> None:
        if self._provider_snapshot_loader is None:
            return
        try:
            snapshot = self._provider_snapshot_loader()
        except Exception:
            logger.exception("Failed to refresh provider config")
            return
        default_selection = preset_helpers.default_selection_signature(snapshot.signature)
        if self._active_preset and self._default_selection_signature in (None, default_selection):
            self._default_selection_signature = default_selection
            try:
                snapshot = self._build_model_preset_snapshot(self._active_preset)
            except Exception:
                logger.exception("Failed to refresh active model preset")
                return
        else:
            self._active_preset = None
            self._default_selection_signature = default_selection
        if snapshot.signature == self._provider_signature:
            return
        self._default_selection_signature = preset_helpers.default_selection_signature(
            snapshot.signature
        )
        self._apply_provider_snapshot(snapshot)

    @property
    def model_preset(self: "AgentLoop") -> str | None:
        return self._active_preset

    @model_preset.setter
    def model_preset(self: "AgentLoop", name: str | None) -> None:
        self.set_model_preset(name)

    def _build_model_preset_snapshot(self: "AgentLoop", name: str) -> ProviderSnapshot:
        return preset_helpers.build_runtime_preset_snapshot(
            name=name,
            presets=self.model_presets,
            provider=self.provider,
            loader=self._preset_snapshot_loader,
        )

    def set_model_preset(
        self: "AgentLoop", name: str | None, *, publish_update: bool = True
    ) -> None:
        """Resolve a preset by name and apply all runtime model dependents."""
        name = preset_helpers.normalize_preset_name(name, self.model_presets)
        snapshot = self._build_model_preset_snapshot(name)
        self._apply_provider_snapshot(snapshot, publish_update=publish_update, model_preset=name)
        self._active_preset = name
