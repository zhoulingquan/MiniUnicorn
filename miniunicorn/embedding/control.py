"""Shared single-process assembly of the embedding-memory runtime.

``EmbeddingControl`` is the composition root the CLI, WebUI and AgentLoop all
go through: one model manager, one source catalog, one index, one recall
service and one prompt policy per ``(workspace, configured)`` pair. A disabled
instance stays lazy -- it never creates ``memory/memory.db`` and never loads
the embedding model. Index reconciliation is serialized per workspace, and
background rebuilds/reconciles run as guarded background tasks.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, ClassVar

from miniunicorn.agent.memory_prompt import MemoryPromptPolicy
from miniunicorn.agent.memory_recall import MemoryRecallService, RecallOutcome
from miniunicorn.agent.memory_sources import MemorySourceCatalog
from miniunicorn.agent.vector_index import VectorIndexManager
from miniunicorn.embedding.model_manager import EmbeddingModelManager
from miniunicorn.embedding.status_service import EmbeddingStatusService
from miniunicorn.embedding.types import EmbeddingStatus
from miniunicorn.providers.local_embedding import LocalEmbeddingProvider

logger = logging.getLogger(__name__)

#: Index states that a background full rebuild can recover from.
_REBUILDABLE = ("index_stale", "index_corrupt")


class EmbeddingControl:
    """Shared single-process embedding-memory services for one workspace."""

    _instances: ClassVar[dict[tuple[Path, bool], "EmbeddingControl"]] = {}

    def __init__(self, workspace: Path, *, configured: bool = True) -> None:
        self.workspace = Path(workspace)
        self.configured = bool(configured)
        self.model_manager = EmbeddingModelManager()
        self.catalog = MemorySourceCatalog(self.workspace)
        self.prompt_policy = MemoryPromptPolicy(self.workspace)
        self._provider = LocalEmbeddingProvider(manager=self.model_manager)
        self._index: VectorIndexManager | None = None
        self._recall_service: MemoryRecallService | None = None
        self._status_service: EmbeddingStatusService | None = None
        self._reconcile_lock = asyncio.Lock()
        self._rebuild_task: asyncio.Task[Any] | None = None
        self._reconcile_task: asyncio.Task[Any] | None = None
        self.last_recall: RecallOutcome | None = None

    @classmethod
    def for_workspace(
        cls, workspace: Path, *, configured: bool = True
    ) -> "EmbeddingControl":
        """Return the shared instance for a ``(workspace, configured)`` key."""
        key = (Path(workspace).resolve(), bool(configured))
        instance = cls._instances.get(key)
        if instance is None:
            instance = cls(key[0], configured=configured)
            cls._instances[key] = instance
        return instance

    @property
    def db_path(self) -> Path:
        return self.workspace / "memory" / "memory.db"

    @property
    def provider(self) -> LocalEmbeddingProvider:
        """Shared embedding provider (cheap to construct, lazy model load)."""
        return self._provider

    @property
    def recall_service(self) -> MemoryRecallService:
        if self._recall_service is None:
            self._recall_service = MemoryRecallService(
                index=self._ensure_index(), embedder=self._provider
            )
        return self._recall_service

    # --------------------------------------------------------------- recall

    async def recall_for_turn(self, query: str) -> RecallOutcome:
        """Return bounded recall for one user turn, with safe fallbacks."""
        if not self.configured:
            return RecallOutcome((), "disabled", 0.0)
        index = self._ensure_index()
        if index is None:
            self.start_guarded_rebuild()
            return RecallOutcome((), "index_missing", 0.0)
        if not index.is_search_ready():
            reason = index.fallback_reason()
            if reason in _REBUILDABLE:
                self.start_guarded_rebuild()
            return RecallOutcome((), reason, 0.0)
        await self.reconcile_guarded()
        core = self.prompt_policy.core_texts()
        outcome = await self.recall_service.recall(query, core_texts=core)
        self.last_recall = outcome
        return outcome

    async def reconcile_guarded(self) -> Any:
        """Incrementally sync the index with the catalog, serialized."""
        index = self._ensure_index()
        if index is None:
            return None
        async with self._reconcile_lock:
            scan = self.catalog.scan()
            return await index.reconcile(scan, self._provider)

    def request_reconcile(self) -> None:
        """Ask for a background reconcile (writers call after writing)."""
        if not self.configured:
            return
        if self._reconcile_task is not None and not self._reconcile_task.done():
            return
        try:
            self._reconcile_task = asyncio.get_running_loop().create_task(
                self.reconcile_guarded()
            )
        except RuntimeError:
            pass

    def start_guarded_rebuild(self) -> None:
        """Kick a background full rebuild; never more than one at a time."""
        if not self.configured:
            return
        if self._rebuild_task is not None and not self._rebuild_task.done():
            return
        try:
            self._rebuild_task = asyncio.get_running_loop().create_task(
                self._rebuild_guarded()
            )
        except RuntimeError:
            logger.warning("no running event loop; skipping background rebuild")

    async def _rebuild_guarded(self) -> None:
        try:
            index = VectorIndexManager(self.db_path)
            await index.rebuild(self.catalog, self._provider)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("embedding-memory rebuild failed")

    # --------------------------------------------------------------- status

    def status(self, *, configured: bool) -> EmbeddingStatus:
        """One consistent status snapshot over the shared components."""
        if self._status_service is None:
            index = self._index
            if index is None and self.db_path.is_file():
                index = VectorIndexManager(self.db_path)
            self._status_service = EmbeddingStatusService(
                self.workspace,
                model_manager=self.model_manager,
                catalog=self.catalog,
                index_manager=index,
            )
        return self._status_service.snapshot(configured=configured)

    # --------------------------------------------------------------- helpers

    def _ensure_index(self) -> VectorIndexManager | None:
        """Open the existing index lazily; never create the database."""
        if not self.configured:
            return None
        if self._index is None:
            if not self.db_path.is_file():
                return None
            self._index = VectorIndexManager(self.db_path)
        return self._index
