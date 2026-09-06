"""Memory system facade: re-exports the split memory modules (store / consolidator / dream)."""

from erza.memory.consolidator import (  # noqa: F401
    _ARCHIVE_SUMMARY_MAX_CHARS,
    Consolidator,
)
from erza.memory.dream import (  # noqa: F401
    Dream,
    _dream_source_batch,
    _parse_datetime_loose,
    count_pending_dream_entries,
    reflection_evidence_id,
)
from erza.memory.jsonl_import import (  # noqa: F401
    LegacyJournalImportError,
    migrate_legacy_journal,
)
from erza.memory.store import (  # noqa: F401
    _HISTORY_ENTRY_HARD_CAP,
    _RAW_ARCHIVE_MAX_CHARS,
    MemoryStore,
    WorkspaceMemoryRegistry,
)
