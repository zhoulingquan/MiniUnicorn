"""Read-only embedding-memory status snapshot shared by CLI and WebUI.

The service derives discovered/pending counts from the authoritative source
catalog without creating the index database, and reports the pinned model
status through the model manager, so every surface reports the same shape.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from miniunicorn.agent.memory_sources import MemorySourceCatalog
from miniunicorn.agent.vector_index import VectorIndexManager
from miniunicorn.embedding.model_manager import EmbeddingModelManager
from miniunicorn.embedding.types import (
    EmbeddingStatus,
    IndexStatus,
    RecallStatus,
    SourceStatus,
)


def _probe_index(workspace: Path) -> VectorIndexManager | None:
    """Open the existing index read-only; never create the database file."""
    db_path = Path(workspace) / "memory" / "memory.db"
    if not db_path.is_file():
        return None
    return VectorIndexManager(db_path)


class EmbeddingStatusService:
    """Compose one consistent status snapshot without side effects."""

    def __init__(
        self,
        workspace: Path,
        *,
        model_manager: EmbeddingModelManager,
        catalog: MemorySourceCatalog,
        index_manager: object | None = None,
        index_state: str = "missing",
    ) -> None:
        self.workspace = Path(workspace)
        self.model_manager = model_manager
        self.catalog = catalog
        self.index_manager = index_manager
        self.index_state = index_state

    @classmethod
    def from_config(cls, config: Any) -> "EmbeddingStatusService":
        """Build the service from a config with read-only index probing."""
        workspace = Path(config.workspace_path)
        return cls(
            workspace,
            model_manager=EmbeddingModelManager(),
            catalog=MemorySourceCatalog(workspace),
            index_manager=_probe_index(workspace),
        )

    def snapshot(self, *, configured: bool) -> EmbeddingStatus:
        model = self.model_manager.status()
        index = self._index_status()
        scan = self.catalog.scan()
        indexed = 0
        if self.index_manager is not None and getattr(
            self.index_manager, "is_search_ready", lambda: False
        )():
            indexed = self.index_manager.count_active_sources()
        sources = SourceStatus(
            discovered=len(scan.records),
            indexed=indexed,
            pending=max(0, len(scan.records) - indexed),
            stale=0,
            invalid=len(scan.errors),
            inactive=0,
            errors=tuple(
                {"file": error.source_file, "code": error.code, "message": error.message}
                for error in scan.errors
            ),
        )
        recall = self._recall_status(configured, model, index)
        return EmbeddingStatus(model=model, index=index, sources=sources, recall=recall)

    # ---------------------------------------------------------------- helpers

    def _index_status(self) -> IndexStatus:
        if self.index_manager is not None and hasattr(self.index_manager, "status"):
            return self.index_manager.status()
        return IndexStatus(state=self.index_state, message="索引未构建")  # type: ignore[arg-type]

    @staticmethod
    def _recall_status(
        configured: bool, model: object, index: IndexStatus
    ) -> RecallStatus:
        if not configured:
            return RecallStatus(configured=False, active=False, fallback_reason="disabled")
        if index.state != "ready":
            return RecallStatus(
                configured=True,
                active=False,
                fallback_reason=EmbeddingStatusService._index_reason(index),
            )
        if model.state != "ready":  # type: ignore[attr-defined]
            return RecallStatus(
                configured=True,
                active=False,
                fallback_reason="model_not_ready",
                last_self_test=model.last_self_test,  # type: ignore[attr-defined]
            )
        return RecallStatus(
            configured=True,
            active=True,
            fallback_reason=None,
            last_self_test=model.last_self_test,  # type: ignore[attr-defined]
        )

    @staticmethod
    def _index_reason(index: IndexStatus) -> str:
        if index.state == "stale":
            return "index_stale"
        if index.state in ("failed", "corrupt"):
            if index.last_error_code == "dependency_missing":
                return "dependency_missing"
            return "index_corrupt"
        return "index_missing"
