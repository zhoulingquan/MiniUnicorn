"""Atomic checksummed JSONL journal with rebuildable in-process indexes.

The journal at ``memory/structured/journal.jsonl`` is the sole source of
structured memory truth. This module owns transaction append, replay,
health state, and the in-process indexes; it makes no business decisions.

Normative source: docs/superpowers/specs/2026-08-11-c2-governed-structured-memory-design.md
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path

from filelock import FileLock
from filelock import Timeout as FileLockTimeout
from loguru import logger
from pydantic import ValidationError

from miniunicorn.agent.memory_models import (
    InvalidMemoryTransition,
    MemoryLockTimeout,
    MemoryRecord,
    MemoryRevisionConflict,
    MemoryStatus,
    MemoryTransaction,
    MemoryWriteError,
    RepositoryDegradedError,
    RepositoryHealth,
    ScopeKind,
    TagCatalog,
    UnknownMemoryTag,
    assert_transition,
    normalize_match_text,
    transaction_checksum,
)

_SUPPORTED_TAGS_FILE = "tags.json"
_JOURNAL_FILE = "journal.jsonl"
_LOCK_FILE = "journal.lock"


class StructuredMemoryRepository:
    """Locked, checksummed append-only journal with rebuildable indexes."""

    def __init__(self, workspace: Path, *, lock_timeout_s: float = 5.0):
        self.workspace = Path(workspace)
        self.lock_timeout_s = lock_timeout_s
        self.structured_dir = self.workspace / "memory" / "structured"
        self.journal_path = self.structured_dir / _JOURNAL_FILE
        self.tags_path = self.structured_dir / _SUPPORTED_TAGS_FILE
        self.lock_path = self.structured_dir / _LOCK_FILE
        self._tag_catalog = TagCatalog()
        self._health = RepositoryHealth()
        self._clear_index()
        self.rebuild()
        logger.info("memory_repository_loaded workspace={} health={}", self.workspace, self._health.state)

    @property
    def workspace(self) -> Path:
        return self._workspace

    @workspace.setter
    def workspace(self, value: Path) -> None:
        self._workspace = Path(value)

    @property
    def health(self) -> RepositoryHealth:
        return self._health

    def _clear_index(self) -> None:
        self._current: dict[str, MemoryRecord] = {}
        self._revision_history: dict[str, list[MemoryRecord]] = defaultdict(list)
        self._by_status: dict[MemoryStatus, set[str]] = defaultdict(set)
        self._by_scope: dict[tuple[ScopeKind, str], set[str]] = defaultdict(set)
        self._by_subject: dict[str, set[str]] = defaultdict(set)
        self._by_tag: dict[str, set[str]] = defaultdict(set)
        self._by_alias: dict[str, set[str]] = defaultdict(set)
        self._active_by_conflict: dict[str, str] = {}
        self._candidate_by_source: dict[str, set[str]] = defaultdict(set)

    def _load_tag_catalog(self) -> None:
        if self.tags_path.exists():
            try:
                self._tag_catalog = TagCatalog.load(self.tags_path)
            except (OSError, ValueError, ValidationError):
                logger.warning("memory_tags_unreadable path={}", self.tags_path)
                self._tag_catalog = TagCatalog()
        else:
            self._tag_catalog = TagCatalog()

    # ------------------------------------------------------------------
    # Rebuild
    # ------------------------------------------------------------------

    def rebuild(self) -> RepositoryHealth:
        """Replay the journal into the in-process indexes; fail closed on corruption."""
        self._load_tag_catalog()
        self._clear_index()
        self._health = RepositoryHealth()
        if not self.journal_path.exists():
            return self._health
        try:
            with self.journal_path.open("r", encoding="utf-8") as stream:
                for line_number, raw in enumerate(stream, start=1):
                    if not raw.strip():
                        continue
                    if not self._replay_line(raw, line_number):
                        # Fail closed: never trust a partially replayed journal.
                        self._clear_index()
                        return self._health
        except OSError as exc:
            self._degrade(0, "invalid_json", f"cannot read journal: {exc}")
            self._clear_index()
        return self._health

    def _replay_line(self, raw: str, line_number: int) -> bool:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._degrade(line_number - 1, "invalid_json", f"line {line_number}: {exc}")
            return False
        if not isinstance(data, dict):
            self._degrade(line_number - 1, "invalid_json", f"line {line_number}: not an object")
            return False
        try:
            transaction = MemoryTransaction.model_validate(data)
        except ValidationError as exc:
            code = "unsupported_schema" if data.get("schema_version") != 1 else "invalid_transaction"
            self._degrade(line_number - 1, code, f"line {line_number}: {exc}")
            return False
        try:
            self._validate_against_current(transaction)
        except MemoryRevisionConflict as exc:
            self._degrade(line_number - 1, "revision_conflict", f"line {line_number}: {exc}")
            return False
        except InvalidMemoryTransition as exc:
            self._degrade(line_number - 1, "invalid_transition", f"line {line_number}: {exc}")
            return False
        except UnknownMemoryTag as exc:
            self._degrade(line_number - 1, "unknown_tag", f"line {line_number}: {exc}")
            return False
        except MemoryWriteError as exc:
            self._degrade(line_number - 1, "checksum_mismatch", f"line {line_number}: {exc}")
            return False
        self._publish_transaction(transaction)
        return True

    def _degrade(self, last_valid_line: int, code: str, message: str) -> None:
        self._health = RepositoryHealth(
            state="degraded",
            last_valid_line=last_valid_line,
            error_code=code,
            error_message=message,
        )
        logger.error("memory_repository_degraded code={} last_valid_line={} error={}", code, last_valid_line, message)

    # ------------------------------------------------------------------
    # Append protocol
    # ------------------------------------------------------------------

    def append_transaction(self, transaction: MemoryTransaction) -> None:
        """Append one durable transaction under the journal lock (design section 12.1)."""
        try:
            with FileLock(str(self.lock_path), timeout=self.lock_timeout_s):
                self._require_healthy()
                validated = self._validate_against_current(transaction)
                line = self._canonical_transaction_line(validated)
                with self.journal_path.open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write(line + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                self._publish_transaction(validated)
        except FileLockTimeout as exc:
            raise MemoryLockTimeout(f"journal lock timeout after {self.lock_timeout_s}s") from exc
        except MemoryError:
            raise
        except OSError as exc:
            raise MemoryWriteError(str(exc)) from exc
        logger.info("memory_transaction_committed tx={} actor={} reason={}", transaction.tx_id, transaction.actor, transaction.reason)

    def _require_healthy(self) -> None:
        if self._health.state != "healthy":
            raise RepositoryDegradedError(
                f"journal degraded (code={self._health.error_code}, last_valid_line={self._health.last_valid_line}); writes disabled"
            )

    def _validate_against_current(self, transaction: MemoryTransaction) -> MemoryTransaction:
        if transaction_checksum(transaction) != transaction.checksum_sha256:
            raise MemoryWriteError("transaction checksum mismatch")
        for operation in transaction.operations:
            record = operation.record
            self._tag_catalog.validate_record(record)
            previous = self._current.get(record.id)
            expected = transaction.expected_revisions[record.id]
            if previous is None:
                if expected != 0 or record.revision != 1:
                    raise MemoryRevisionConflict(
                        f"expected revision 0 and revision 1 for new record {record.id}, got expected={expected} revision={record.revision}"
                    )
                continue
            if expected != previous.revision:
                raise MemoryRevisionConflict(
                    f"expected revision {previous.revision} for {record.id}, got {expected}"
                )
            if record.revision != previous.revision + 1:
                raise MemoryRevisionConflict(
                    f"revision must increment by exactly one for {record.id}: {previous.revision} -> {record.revision}"
                )
            assert_transition(previous.status, record.status)
        return transaction

    def _canonical_transaction_line(self, transaction: MemoryTransaction) -> str:
        return json.dumps(
            transaction.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    # ------------------------------------------------------------------
    # Index publication
    # ------------------------------------------------------------------

    def _publish_transaction(self, transaction: MemoryTransaction) -> None:
        for operation in transaction.operations:
            record = operation.record
            previous = self._current.get(record.id)
            self._unindex(previous)
            self._current[record.id] = record
            self._revision_history[record.id].append(record)
            self._index(record, transaction.source_batch)

    def _index(self, record: MemoryRecord, source_batch: str) -> None:
        self._by_status[record.status].add(record.id)
        self._by_scope[(record.scope.kind, record.scope.key)].add(record.id)
        self._by_subject[normalize_match_text(record.subject)].add(record.id)
        for tag in record.tags:
            self._by_tag[normalize_match_text(tag)].add(record.id)
        for alias in record.aliases:
            self._by_alias[normalize_match_text(alias)].add(record.id)
        if record.status is MemoryStatus.ACTIVE:
            self._active_by_conflict[record.conflict_key] = record.id
        if record.status is MemoryStatus.CANDIDATE:
            self._candidate_by_source[source_batch].add(record.id)

    def _unindex(self, record: MemoryRecord | None) -> None:
        if record is None:
            return
        self._by_status[record.status].discard(record.id)
        self._by_scope[(record.scope.kind, record.scope.key)].discard(record.id)
        self._by_subject[normalize_match_text(record.subject)].discard(record.id)
        for tag in record.tags:
            self._by_tag[normalize_match_text(tag)].discard(record.id)
        for alias in record.aliases:
            self._by_alias[normalize_match_text(alias)].discard(record.id)
        if record.status is MemoryStatus.ACTIVE and self._active_by_conflict.get(record.conflict_key) == record.id:
            del self._active_by_conflict[record.conflict_key]
        for batches in self._candidate_by_source.values():
            batches.discard(record.id)

    # ------------------------------------------------------------------
    # Read accessors
    # ------------------------------------------------------------------

    def get(self, memory_id: str) -> MemoryRecord | None:
        return self._current.get(memory_id)

    def revisions(self, memory_id: str) -> tuple[MemoryRecord, ...]:
        return tuple(self._revision_history[memory_id])

    def current_records(self, status: MemoryStatus | None = None) -> tuple[MemoryRecord, ...]:
        if status is not None:
            return tuple(sorted((self._current[i] for i in self._by_status[status]), key=lambda r: r.id))
        return tuple(sorted(self._current.values(), key=lambda r: r.id))

    def active_for_conflict_key(self, key: str) -> MemoryRecord | None:
        memory_id = self._active_by_conflict.get(key)
        return self._current.get(memory_id) if memory_id else None

    def candidate_records(self) -> tuple[MemoryRecord, ...]:
        return self.current_records(MemoryStatus.CANDIDATE)

    def candidate_ids_for_source(self, source_batch: str) -> frozenset[str]:
        return frozenset(self._candidate_by_source[source_batch])
