"""SQLite implementation of the Runtime Store (design §10.2, §16).

This subpackage is the only production SQL implementation. Importing it
imports ``sqlite3`` (a standard-library module); Agent Core never imports
this package (design §6.17, acceptance #23).

Public surface:

- :class:`SqliteRuntimeStore` — the only production Runtime Store façade.
- :func:`open_connection` — connection factory with WAL pragmas.
- :func:`run_migrations` — schema migration owner.
"""

from __future__ import annotations

from miniunicorn.runtime.sqlite.connection import open_connection
from miniunicorn.runtime.sqlite.migrations import (
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
    run_migrations,
    validate_schema_version,
)
from miniunicorn.runtime.sqlite.store import SqliteRuntimeStore

__all__ = [
    "SqliteRuntimeStore",
    "open_connection",
    "run_migrations",
    "validate_schema_version",
    "CURRENT_SCHEMA_VERSION",
    "MIGRATIONS",
]
