# Rename `vectors.db` to `memory.db`

Date: 2026-07-28

## Context

MiniUnicorn stores embedding-based internal memory in
`workspace/memory/vectors.db`. The file contains recalled conversation
summaries and procedural lessons, so its product role is internal long-term
memory rather than a general-purpose vector database.

The project is still under development and has no production data requiring
backward compatibility.

## Decision

Rename the database file to:

```text
workspace/memory/memory.db
```

Update every product-code, test, and documentation reference that depends on
the database filename.

Keep the implementation names `vector_memory.py`, `VectorMemoryStore`, and
`create_vector_store`. These names accurately describe the vector retrieval
mechanism and distinguish it from the broader memory system in `memory.py`.

## Compatibility

Do not add migration, fallback, copy, or dual-file detection for the old
`vectors.db` filename.

Existing development databases may be deleted and rebuilt. This deliberately
avoids permanent compatibility branches for data that has never been released.

## Behavior

When vector recall is enabled, MiniUnicorn creates or opens
`workspace/memory/memory.db`. All storage schema, indexing, retrieval, ranking,
and optional `sqlite-vec` behavior remain unchanged.

No configuration keys or public Python APIs change.

## Documentation

Add `memory.db` to the memory workspace layout and describe it as the local
SQLite index used for semantic recall. Clarify that it is an internal memory
index, not an external document knowledge base.

## Verification

Tests must confirm that:

- vector recall passes `workspace/memory/memory.db` to the vector-store factory;
- normal vector-store behavior remains unchanged;
- no tracked source or documentation still references `vectors.db`.

Run the focused agent tests and the repository's standard Python checks
appropriate to the touched files.

## Non-goals

- Renaming vector-store classes or modules.
- Changing the embedding provider or embedding model.
- Replacing `sqlite-vec` with Zvec.
- Building an external document knowledge base.
- Migrating development copies of `vectors.db`.
