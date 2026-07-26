"""Recall tool: let the agent actively search its own memory.

When vector recall is enabled (AgentDefaults.vector_recall=True and
sqlite-vec installed), this tool lets the agent query past memories,
conversation summaries, and lessons by semantic similarity instead of
relying only on what was injected into the system prompt.
"""
from __future__ import annotations

import re
from typing import Any

from loguru import logger

from miniUnicorn.agent.tools.base import Tool, tool_parameters
from miniUnicorn.agent.tools.schema import (
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)


@tool_parameters(
    tool_parameters_schema(
        query=StringSchema("What to search for in past memories and conversations."),
        k=IntegerSchema(
            5,
            description="Number of results to return (default 5, max 20).",
            minimum=1,
            maximum=20,
            nullable=True,
        ),
        kind=StringSchema(
            "Optional filter: 'history', 'episodic', 'semantic', 'procedural', 'shared'. "
            "Null = search all kinds.",
            enum=["history", "episodic", "semantic", "procedural", "shared"],
            nullable=True,
        ),
        required=["query"],
    )
)
class RecallTool(Tool):
    """Search past memories, conversations, and lessons for relevant context."""

    _scopes = {"core", "subagent"}

    def __init__(self, memory_store: Any = None):
        self._memory_store = memory_store

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        # memory_store is attached to ToolContext by the loop when vector_recall is on
        ms = getattr(ctx, "memory_store", None)
        return cls(memory_store=ms)

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        # Only enable when vector recall is configured
        ms = getattr(ctx, "memory_store", None)
        if ms is None:
            return False
        vs = getattr(ms, "vector_store", None)
        return vs is not None and getattr(vs, "enabled", False)

    @property
    def name(self) -> str:
        return "recall"

    @property
    def description(self) -> str:
        return (
            "Search your own past memories, conversation summaries, and lessons "
            "learned. Use this when you need to recall something discussed earlier "
            "or check for relevant past experience. Returns the most relevant "
            "matches with similarity scores. Optionally filter by kind: "
            "'history' (conversation summaries), 'episodic' (events), "
            "'semantic' (facts), 'procedural' (lessons/skills), "
            "'shared' (cross-session global facts/lessons)."
        )

    async def execute(
        self,
        query: str | None = None,
        k: int | None = 5,
        kind: str | None = None,
        **kwargs: Any,
    ) -> str:
        if not query:
            return "Error: query is required"
        ms = self._memory_store
        if ms is None:
            return "Recall is not available (memory store not configured)."
        vs = getattr(ms, "vector_store", None)
        if vs is None or not getattr(vs, "enabled", False):
            return "Recall is not available (vector store not enabled)."
        provider = getattr(ms, "_embed_provider", None)
        if provider is None:
            return "Recall is not available (embedding provider not set)."
        embed_model = getattr(ms, "_embed_model", "text-embedding-3-small")
        max_k = min(max(k or 5, 1), 20)

        try:
            embeddings = await provider.embed(
                [query[:1000]], model=embed_model,
            )
        except NotImplementedError:
            return "Recall is not available (provider does not support embeddings)."
        except Exception as exc:
            logger.exception("recall: embedding failed")
            # 错误信息脱敏:遮蔽可能泄露的 API key / Bearer token 等敏感凭证,
            # 避免这些值原样回传给模型并可能被记录到对话历史中。
            _safe_msg = re.sub(
                r"(sk-[A-Za-z0-9]{8,}|Bearer\s+[A-Za-z0-9]{8,})",
                "***",
                str(exc),
            )
            return f"Error generating query embedding: {_safe_msg}"

        if not embeddings:
            return "No results (empty embedding)."

        results = vs.search(embeddings[0], k=max_k, kind=kind)
        if not results:
            return "No matching memories found."

        lines = [f"Found {len(results)} relevant memories:"]
        for r in results:
            # Prefer the weighted importance-aware score when present (P2-1);
            # fall back to raw similarity for stores that don't compute it yet.
            score = r.get("score", r.get("similarity", 0.0))
            rkind = r.get("kind", "?")
            text = r.get("text", "")
            ts = r.get("created_at", "")
            # Truncate long entries for readability
            if len(text) > 300:
                text = text[:300] + "..."
            meta = r.get("metadata", {})
            meta_str = ""
            if isinstance(meta, dict) and meta:
                # Show cursor or other key metadata
                cursor = meta.get("cursor")
                if cursor is not None:
                    meta_str = f" [cursor={cursor}]"
            ts_str = f" ({ts})" if ts else ""
            lines.append(f"- [{rkind}]{ts_str}{meta_str} (score={score:.2f}) {text}")
        return "\n".join(lines)
