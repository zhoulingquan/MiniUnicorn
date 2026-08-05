"""Embedding memory REST API service: status, operations, and search.

``EmbeddingApiService`` wraps the shared :class:`EmbeddingControl` so the
WebUI HTTP handler layer stays thin. Mutating operations (setup/verify/
rebuild) are started as background tasks via ``EmbeddingControl.start_operation``
and the HTTP layer returns ``202`` immediately — the frontend polls
``status`` to observe progress. Search runs synchronously and sanitises
``source_file`` to workspace-relative paths so absolute paths never leak.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from miniunicorn.embedding.control import EmbeddingControl


class EmbeddingApiError(Exception):
    """Raised for expected HTTP error conditions (conflict, bad input, disabled)."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class EmbeddingApiService:
    """Thin service layer over ``EmbeddingControl`` for the WebUI REST API."""

    def __init__(self, workspace: Path, *, configured: bool) -> None:
        self.control = EmbeddingControl.for_workspace(workspace, configured=configured)
        self._workspace = Path(workspace)

    # --------------------------------------------------------------- status

    def status(self) -> dict[str, Any]:
        """Return the shared status payload including any live operation."""
        configured = self.control.configured
        return self.control.status(configured=configured).to_dict()

    # ------------------------------------------------------------ operations

    def start(self, kind: str) -> dict[str, Any]:
        """Start a background setup/verify/rebuild; return 202 payload.

        Raises :class:`EmbeddingApiError` (409) if an operation is already
        running or embedding is disabled.
        """
        if not self.control.configured:
            raise EmbeddingApiError(409, "embedding_disabled", "向量记忆已关闭")
        try:
            operation = self.control.start_operation(kind)  # type: ignore[arg-type]
        except RuntimeError as exc:
            if "already_running" in str(exc):
                raise EmbeddingApiError(
                    409, "operation_already_running", "已有模型或索引任务正在运行"
                ) from exc
            raise
        return {"accepted": True, "operation": operation.to_dict()}

    # --------------------------------------------------------------- search

    async def search(self, query: str) -> dict[str, Any]:
        """Run a bounded recall search; sanitize paths and strip vectors."""
        clean = query.strip()
        if not clean or len(clean) > 500:
            raise EmbeddingApiError(400, "invalid_query", "搜索内容长度必须为 1 到 500 字符")
        if not self.control.configured:
            raise EmbeddingApiError(409, "embedding_disabled", "向量记忆已关闭")
        outcome = await self.control.recall_service.recall(clean)
        results = []
        for record in outcome.records:
            row = asdict(record)
            # Never expose absolute paths — make source_file workspace-relative.
            source_file = str(row.get("source_file") or "")
            if source_file:
                try:
                    row["source_file"] = str(
                        Path(source_file).relative_to(self._workspace)
                    )
                except ValueError:
                    row["source_file"] = Path(source_file).name
            # Defensive: never expose raw vectors even if the record grew them.
            row.pop("embedding", None)
            row.pop("vector", None)
            results.append(row)
        return {
            "results": results,
            "fallback_reason": outcome.fallback_reason,
            "latency_ms": outcome.latency_ms,
        }
