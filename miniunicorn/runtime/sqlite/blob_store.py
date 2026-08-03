"""Blob store mixin for the SQLite Runtime Store (design §16.15, §12.3).

Holds the protected runtime blob insert/read operations and the
``_row_to_blob`` row mapper. The mixin shares ``self._conn`` with the
other responsibility mixins via the façade (design §7.3, Task 12 Steps 3-5).
"""

from __future__ import annotations

import sqlite3

from miniunicorn.runtime.models import BlobRecord, BlobWrite
from miniunicorn.runtime.sqlite.base_store import _new_uuid, _now_ms


def _row_to_blob(row: sqlite3.Row) -> BlobRecord:
    return BlobRecord(
        blob_id=row["blob_id"],
        scope_key=row["scope_key"],
        blob_kind=row["blob_kind"],
        content_hash=row["content_hash"],
        encoding=row["encoding"],
        compression=row["compression"],
        encryption_key_id=row["encryption_key_id"],
        inline_content=row["inline_content"],
        external_ref=row["external_ref"],
        size_bytes=row["size_bytes"],
        created_at_ms=row["created_at_ms"],
    )


class BlobStoreMixin:
    """Blob insert/read operations (design §16.15, §12.3)."""

    def write_blob(self, write: BlobWrite) -> BlobRecord:
        """Insert or reuse a protected runtime blob (design §16.15).

        Deduplication is by ``(scope_key, blob_kind, content_hash)``.
        ``blob_id`` is generated when absent; the caller may supply a
        deterministic id for known payloads.
        """
        if bool(write.inline_content) == bool(write.external_ref):
            raise ValueError("BlobWrite requires exactly one of inline_content or external_ref")
        blob_id = write.blob_id or _new_uuid()
        now_ms = write.created_at_ms or _now_ms()
        size = write.size_bytes or (len(write.inline_content) if write.inline_content else 0)

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            # Check for an existing blob with the same dedup key.
            existing = self._conn.execute(
                "SELECT * FROM runtime_blobs WHERE scope_key=? AND blob_kind=? AND content_hash=?",
                (write.scope_key, write.blob_kind, write.content_hash),
            ).fetchone()
            if existing is not None:
                self._conn.execute("COMMIT")
                return _row_to_blob(existing)

            self._conn.execute(
                """
                INSERT INTO runtime_blobs (
                    blob_id, scope_key, blob_kind, content_hash, encoding,
                    compression, encryption_key_id, inline_content,
                    external_ref, size_bytes, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    blob_id,
                    write.scope_key,
                    write.blob_kind,
                    write.content_hash,
                    write.encoding,
                    write.compression,
                    write.encryption_key_id,
                    write.inline_content,
                    write.external_ref,
                    size,
                    now_ms,
                ),
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

        row = self._conn.execute(
            "SELECT * FROM runtime_blobs WHERE blob_id=?", (blob_id,)
        ).fetchone()
        return _row_to_blob(row)

    def read_blob(self, blob_id: str) -> BlobRecord | None:
        row = self._conn.execute(
            "SELECT * FROM runtime_blobs WHERE blob_id=?", (blob_id,)
        ).fetchone()
        return _row_to_blob(row) if row else None

    def read_blob_content(self, blob_id: str) -> bytes | None:
        row = self._conn.execute(
            "SELECT inline_content, external_ref FROM runtime_blobs WHERE blob_id=?",
            (blob_id,),
        ).fetchone()
        if row is None:
            return None
        if row["inline_content"] is not None:
            return bytes(row["inline_content"])
        # External ref: caller must resolve via the artifact/media storage.
        return None
