#!/usr/bin/env python
"""End-to-end embedding memory persistence verifier (Task 23 Step 5–7).

Runs the real ``BAAI/bge-small-zh-v1.5`` model on CPU, indexes two Chinese
documents into a real ``VectorMemoryStore`` (sqlite-vec), reopens the
database, recalls the top match for a Chinese query, verifies idempotent
re-indexing, and checks the ``NoOpVectorStore`` fallback.

Usage::

    uv run --extra vector python scripts/verify_embedding_memory.py \
        --workspace .embedding-evidence-workspace

Writes ``embedding-memory-evidence.json`` in the current directory.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from pathlib import Path

from miniunicorn.agent.memory import MemoryStore
from miniunicorn.agent.vector_memory import NoOpVectorStore
from miniunicorn.providers.local_embedding import (
    DEFAULT_LOCAL_DIMENSION,
    DEFAULT_LOCAL_MODEL,
    LocalEmbeddingProvider,
)
from miniunicorn.runtime.sqlite.vector_memory_store import create_vector_store

DOCUMENTS = [
    "MiniUnicorn 使用本地 CPU 嵌入保存长期记忆。",
    "三工作进程运行时负责持久任务执行。",
]
QUERY = "本地嵌入如何保存记忆？"
SCOPE = {
    "tenant_id": "local",
    "principal_id": "owner",
    "agent_id": "main",
    "workspace_id": "embedding-proof",
}


def _validate_vectors(vectors: list[list[float]], label: str) -> None:
    """Assert every vector has 512 finite values and norm ≈ 1.0."""
    for i, vec in enumerate(vectors):
        if len(vec) != DEFAULT_LOCAL_DIMENSION:
            print(
                f"FAIL: {label}[{i}] has {len(vec)} dimensions, expected {DEFAULT_LOCAL_DIMENSION}",
                file=sys.stderr,
            )
            sys.exit(1)
        if not all(math.isfinite(v) for v in vec):
            print(f"FAIL: {label}[{i}] contains non-finite values", file=sys.stderr)
            sys.exit(1)
        norm = math.sqrt(sum(v * v for v in vec))
        if abs(norm - 1.0) > 1e-5:
            print(
                f"FAIL: {label}[{i}] norm={norm:.8f}, expected 1.0±1e-5",
                file=sys.stderr,
            )
            sys.exit(1)


async def _run(workspace: Path) -> dict:
    model = DEFAULT_LOCAL_MODEL
    dim = DEFAULT_LOCAL_DIMENSION

    # -- 1. Real embedding provider on CPU --------------------------------
    provider = LocalEmbeddingProvider(model_name=model, dimension=dim)
    if not provider.enabled:
        print("FAIL: fastembed is not installed", file=sys.stderr)
        sys.exit(1)

    # Embed documents + query through the real model.
    all_texts = DOCUMENTS + [QUERY]
    all_vecs = await provider.embed(all_texts, model=model)
    if len(all_vecs) != len(all_texts):
        print(
            f"FAIL: embed returned {len(all_vecs)} vectors for {len(all_texts)} texts",
            file=sys.stderr,
        )
        sys.exit(1)

    _validate_vectors(all_vecs, "embed")
    doc_vecs = all_vecs[: len(DOCUMENTS)]
    query_vec = all_vecs[len(DOCUMENTS)]

    # -- 2. Attach real vector store + MemoryStore -------------------------
    memory_dir = workspace / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    db_path = memory_dir / "memory.db"
    db_path.unlink(missing_ok=True)

    store = MemoryStore(workspace)
    store.set_embed_provider(provider, model=model)

    vs = create_vector_store(db_path, embedding_dim=dim, model_id=model)
    if not vs.enabled:
        print("FAIL: VectorMemoryStore not enabled (sqlite-vec missing?)", file=sys.stderr)
        sys.exit(1)
    store.attach_vector_store(vs)

    # -- 3. Index documents through MemoryStore.index_text ----------------
    for i, doc in enumerate(DOCUMENTS):
        await store.index_text(
            doc,
            kind="history",
            source_identity="verify_embedding_memory.py",
            source_revision=f"doc-{i}",
            scope=SCOPE,
        )

    row_count = vs.count()
    if row_count != len(DOCUMENTS):
        print(
            f"FAIL: expected {len(DOCUMENTS)} rows after indexing, got {row_count}",
            file=sys.stderr,
        )
        sys.exit(1)

    # -- 4. Close/reopen and recall ---------------------------------------
    vs.close()
    vs_reopened = create_vector_store(db_path, embedding_dim=dim, model_id=model)
    if not vs_reopened.enabled:
        print("FAIL: reopened VectorMemoryStore not enabled", file=sys.stderr)
        sys.exit(1)

    reopened_count = vs_reopened.count()
    if reopened_count != len(DOCUMENTS):
        print(
            f"FAIL: expected {len(DOCUMENTS)} rows after reopen, got {reopened_count}",
            file=sys.stderr,
        )
        sys.exit(1)

    store.attach_vector_store(vs_reopened)

    results = vs_reopened.search(query_vec, k=5, scope=SCOPE)
    if not results:
        print("FAIL: search returned no results", file=sys.stderr)
        sys.exit(1)

    top_text = results[0]["text"]
    if top_text != DOCUMENTS[0]:
        print(
            f"FAIL: expected top result={DOCUMENTS[0]!r}, got {top_text!r}",
            file=sys.stderr,
        )
        sys.exit(1)

    # -- 5. Idempotent re-index (same source identity/revision) -----------
    await store.index_text(
        DOCUMENTS[0],
        kind="history",
        source_identity="verify_embedding_memory.py",
        source_revision="doc-0",
        scope=SCOPE,
    )
    row_count_after_reindex = vs_reopened.count()
    if row_count_after_reindex != len(DOCUMENTS):
        print(
            f"FAIL: re-index produced {row_count_after_reindex} rows, "
            f"expected {len(DOCUMENTS)} (no duplicate)",
            file=sys.stderr,
        )
        sys.exit(1)

    # -- 6. NoOp fallback --------------------------------------------------
    noop = NoOpVectorStore()
    assert noop.enabled is False
    assert noop.index("authoritative text", [0.0] * dim) is None
    assert noop.search([0.0] * dim) == []

    vs_reopened.close()

    return {
        "model_id": model,
        "dimension": dim,
        "database_path": str(db_path),
        "row_count": row_count,
        "row_count_after_reindex": row_count_after_reindex,
        "top_text": top_text,
        "top_similarity": results[0].get("similarity"),
        "fallback_status": "safe-noop",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify embedding memory persistence")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path(".embedding-evidence-workspace"),
        help="Workspace directory for memory.db",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("embedding-memory-evidence.json"),
        help="Output JSON evidence file",
    )
    args = parser.parse_args()

    workspace = args.workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    evidence = asyncio.run(_run(workspace))

    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
