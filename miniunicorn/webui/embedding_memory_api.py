"""Embedding memory 领域模块:本地向量记忆的只读状态报告。

经 ``settings_api.py`` re-export:
- ``embedding_memory_payload``: 构造 ``embeddingMemory`` 只读状态区域。

与 ``network_safety_api.advanced_payload`` 相同模式:读取配置、聚合
``EmbeddingStatusService`` 的快照,不含任何写路径。
"""

from __future__ import annotations

from typing import Any

from miniunicorn.embedding.status_service import EmbeddingStatusService


def embedding_memory_payload(config: Any) -> dict[str, Any]:
    """Construct the read-only ``embeddingMemory`` status section."""
    configured = bool(config.agents.defaults.vector_recall)
    return {"embeddingMemory": EmbeddingStatusService.from_config(config).snapshot(configured=configured).to_dict()}