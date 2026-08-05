"""Pinned local embedding model contract shared across the runtime."""

from __future__ import annotations

#: Canonical local model identifier (FastEmbed-supported name).
MODEL_ID = "BAAI/bge-small-zh-v1.5"

#: Upstream model revision pinned in the local manifest and index fingerprint.
#: Download artifacts are hash-verified file-by-file, so integrity is anchored
#: by the manifest rather than by a single git revision.
MODEL_REVISION = "7999e1d3359715c523056ef9478215996d62a620"

#: Vector dimensionality produced by the pinned model.
MODEL_DIMENSION = 512

__all__ = ["MODEL_DIMENSION", "MODEL_ID", "MODEL_REVISION"]
