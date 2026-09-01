"""Per-workspace runtime resource registry.

Owns the agent's runtime helpers that were formerly created inline by the
agent loop: the default ``Consolidator`` / ``Dream``, the per-workspace
Consolidator/Dream caches, ``AutoCompact`` and the ``DreamIdleTrigger``.  The
registry resolves one governed ``MemoryStore`` per effective workspace path
and lazily builds (and caches) one Consolidator/Dream per resolved workspace
under a lock so concurrent turns cannot build duplicates.

The agent loop keeps read-only delegating properties and methods for these
members so commands, tools, and tests continue to use the loop's public
surface unchanged.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from miniunicorn.agent.autocompact import AutoCompact
from miniunicorn.agent.memory import Consolidator, Dream, MemoryStore

if TYPE_CHECKING:
    from miniunicorn.agent.context import ContextBuilder
    from miniunicorn.config.schema import DreamConfig
    from miniunicorn.providers.base import LLMProvider
    from miniunicorn.security.workspace_access import WorkspaceScopeResolver
    from miniunicorn.session.manager import SessionManager
    from miniunicorn.tools.registry import ToolRegistry


class RuntimeResourceRegistry:
    """Own the default and per-workspace consolidation / dream helpers."""

    def __init__(
        self,
        workspace: Path | str,
        *,
        context: ContextBuilder,
        workspace_scopes: WorkspaceScopeResolver,
        sessions: SessionManager,
        provider: LLMProvider,
        model: str,
        context_window_tokens: int,
        tools: ToolRegistry,
        max_completion_tokens: int,
        consolidation_ratio: float = 0.5,
        session_ttl_minutes: int = 0,
        max_batch_size: int = 20,
        dream_idle: DreamConfig | None = None,
    ) -> None:
        self.workspace = workspace
        self.context = context
        self.workspace_scopes = workspace_scopes
        self.sessions = sessions
        self.provider = provider
        self.model = model
        self.context_window_tokens = context_window_tokens
        self.tools = tools
        self.consolidation_ratio = consolidation_ratio
        self.max_completion_tokens = max_completion_tokens

        # Default workspace's helpers are always ``self.consolidator`` /
        # ``self.dream``.  Per-workspace helpers are lazily created under a
        # lock so concurrent turns cannot build duplicates.
        self._default_root = self._resolved_root(workspace)
        self._workspace_helpers_lock = threading.Lock()
        self._workspace_consolidators: dict[str, Consolidator] = {}
        self._workspace_dreams: dict[str, Dream] = {}

        self.consolidator = Consolidator(
            store=self.context.memory,
            provider=provider,
            model=model,
            sessions=self.sessions,
            context_window_tokens=self.context_window_tokens,
            build_messages=self.context.build_messages,
            get_tool_definitions=self.tools.get_definitions,
            max_completion_tokens=self.max_completion_tokens,
            consolidation_ratio=consolidation_ratio,
        )
        self.auto_compact = AutoCompact(
            sessions=self.sessions,
            consolidator=self.consolidator,
            session_ttl_minutes=session_ttl_minutes,
            consolidator_for=self._consolidator_for_session,
        )
        self.dream = Dream(
            store=self.context.memory,
            provider=provider,
            model=model,
            max_batch_size=max_batch_size,
            context_window_tokens=self.context_window_tokens,
            max_completion_tokens=self.max_completion_tokens,
        )
        # Dream 空闲触发器：用户停用时后台触发 Dream，不依赖 cron 定时。
        # 解决"用户不 24h 运行 gateway，凌晨 cron 点大概率关机"的问题。
        # gateway 启动时由组合根从 DreamConfig 同步配置。
        from miniunicorn.agent.dream_trigger import DreamIdleTrigger

        self.dream_idle_trigger = DreamIdleTrigger(
            self.dream,
            dreams=self.all_dreams,
            enabled=dream_idle.idle_trigger_enabled if dream_idle is not None else True,
            min_idle_seconds=(
                dream_idle.idle_trigger_min_seconds if dream_idle is not None else 300
            ),
            min_entries=dream_idle.idle_trigger_min_entries if dream_idle is not None else 5,
            min_interval_s=(
                dream_idle.idle_trigger_min_interval_s if dream_idle is not None else 3600
            ),
        )

    # -- lifecycle -----------------------------------------------------------

    async def shutdown(self) -> None:
        """Stop idle dream triggering and cancel any in-flight background dream."""
        await self.dream_idle_trigger.shutdown()

    # -- per-workspace memory / runtime helpers ------------------------------

    @staticmethod
    def _resolved_root(workspace: Path | str) -> str:
        return str(Path(workspace).expanduser().resolve())

    def memory_for(self, workspace: Path | str | None = None) -> MemoryStore:
        """Resolve the governed ``MemoryStore`` for an effective workspace."""
        root = workspace or self.context.workspace
        return self.context.memory_for(root)

    def _consolidator_for(self, workspace: Path | str) -> Consolidator:
        """Return the Consolidator bound to a resolved effective workspace."""
        root = self._resolved_root(workspace)
        if root == self._default_root:
            return self.consolidator
        helper = self._workspace_consolidators.get(root)
        if helper is not None:
            return helper
        with self._workspace_helpers_lock:
            helper = self._workspace_consolidators.get(root)
            if helper is None:
                helper = Consolidator(
                    store=self.memory_for(Path(root)),
                    provider=self.provider,
                    model=self.consolidator.model,
                    sessions=self.sessions,
                    context_window_tokens=self.context_window_tokens,
                    build_messages=self.context.build_messages,
                    get_tool_definitions=self.tools.get_definitions,
                    max_completion_tokens=self.consolidator.max_completion_tokens,
                    consolidation_ratio=self.consolidator.consolidation_ratio,
                )
                self._workspace_consolidators[root] = helper
        return helper

    def _dream_for(self, workspace: Path | str) -> Dream:
        """Return the Dream bound to a resolved effective workspace."""
        root = self._resolved_root(workspace)
        if root == self._default_root:
            return self.dream
        helper = self._workspace_dreams.get(root)
        if helper is not None:
            return helper
        with self._workspace_helpers_lock:
            helper = self._workspace_dreams.get(root)
            if helper is None:
                helper = Dream(
                    store=self.memory_for(Path(root)),
                    provider=self.provider,
                    model=self.dream.model,
                    max_batch_size=self.dream.max_batch_size,
                    context_window_tokens=self.dream.context_window_tokens,
                    max_completion_tokens=self.dream.max_completion_tokens,
                )
                self._workspace_dreams[root] = helper
        return helper

    def all_dreams(self) -> list[Dream]:
        """Return the default Dream plus a Dream for every known workspace store.

        Lazy-creates (and caches) Dreams for effective workspaces that have a
        governed store but have not been dream-processed yet, so idle/cron
        Dream runs cover them even before any B turn triggers consolidation.
        """
        dreams = [self.dream]
        for store in self.context.memory_registry.known_stores():
            root = self._resolved_root(store.workspace)
            if root != self._default_root:
                dreams.append(self._dream_for(Path(root)))
        return dreams

    async def run_all_dreams(self) -> bool:
        """Run every known Dream (default + effective workspaces)."""
        did_work = False
        for dream in self.all_dreams():
            try:
                if await dream.run():
                    did_work = True
            except Exception:
                logger.exception("dream_failed workspace={}", dream.store.workspace)
        return did_work

    def _consolidator_for_session(self, session_key: str) -> Consolidator:
        """Return the Consolidator for a session's persisted effective workspace.

        AutoCompact resolves idle sessions by key only, so the persisted
        ``workspace_scope`` session metadata is re-read here to route archival
        to the session's workspace consolidator instead of the default one.
        """
        session = self.sessions.get_or_create(session_key)
        scope = self.workspace_scopes.for_turn(
            channel=self.workspace_scopes.scoped_channel,
            message_metadata=None,
            session_metadata=session.metadata,
        )
        return self._consolidator_for(scope.project_path)

    def _sync_runtime_helpers(
        self,
        provider: LLMProvider,
        model: str,
        context_window_tokens: int,
    ) -> None:
        """Propagate a provider snapshot to every cached workspace helper."""
        with self._workspace_helpers_lock:
            consolidators = list(self._workspace_consolidators.values())
            dreams = list(self._workspace_dreams.values())
        for helper in consolidators:
            helper.set_provider(provider, model, context_window_tokens)
        for helper in dreams:
            helper.set_provider(provider, model, context_window_tokens)
