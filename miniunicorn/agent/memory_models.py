"""Governed structured memory: schema contract, normalization, and state machine.

This module owns the Pydantic models, enums, canonical normalization helpers,
content hashing, transaction checksums, and the legal status transition table.
It performs no file I/O and no recall logic.

Normative source: docs/superpowers/specs/2026-08-11-c2-governed-structured-memory-design.md
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = 1
MEMORY_ID_RE = re.compile(r"^mem_[0-9a-f]{32}$")
TX_ID_RE = re.compile(r"^mtx_[0-9a-f]{32}$")
SLOT_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class MemoryError(Exception):
    """Base class for governed structured memory errors."""


# Exception names are part of the normative plan interface; N818 suffix rule
# is intentionally waived for these legacy-named memory exceptions.


class InvalidMemoryTransition(MemoryError):  # noqa: N818
    """Raised when a status transition is outside the legal state machine."""


class DuplicateMemoryIdempotencyKey(InvalidMemoryTransition):
    """Two record IDs claim the same source-batch/content creation key."""


class UnknownMemoryTag(MemoryError):  # noqa: N818
    """Raised when a record references a tag missing from the catalog."""


class RepositoryDegradedError(MemoryError):
    """Raised when the journal is corrupt and writes must fail closed."""


class MemoryWriteError(MemoryError):
    """Raised when appending/fsyncing a transaction fails."""


class MemoryLockTimeout(MemoryError):  # noqa: N818
    """Raised when the journal lock cannot be acquired in time."""


class MemoryRevisionConflict(MemoryError):  # noqa: N818
    """Raised when an expected revision does not match the current journal."""


class MemoryExtractionError(MemoryError):
    """Raised when Dream output does not satisfy the strict extraction contract."""


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class MemoryStatus(str, Enum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REVOKED = "revoked"
    EXPIRED = "expired"


class MemoryKind(str, Enum):
    IDENTITY = "identity"
    PREFERENCE = "preference"
    CONSTRAINT = "constraint"
    DECISION = "decision"
    FACT = "fact"
    PROCEDURE = "procedure"
    RELATIONSHIP = "relationship"
    OUTCOME = "outcome"


class ScopeKind(str, Enum):
    SESSION = "session"
    PROJECT = "project"
    USER = "user"
    SHARED = "shared"


class SourceLevel(str, Enum):
    INFERRED = "inferred"
    REPEATED_EXPERIENCE = "repeated_experience"
    VERIFIED = "verified"
    CONFIRMED_DECISION = "confirmed_decision"
    EXPLICIT_CORRECTION = "explicit_correction"


class EvidenceKind(str, Enum):
    USER_MESSAGE = "user_message"
    HISTORY = "history"
    REFLECTION = "reflection"
    TOOL_RESULT = "tool_result"
    FILE = "file"
    GIT = "git"
    MANUAL = "manual"
    MODEL_INFERENCE = "model_inference"


class ActorKind(str, Enum):
    DREAM = "dream"
    USER = "user"
    SYSTEM = "system"


TERMINAL_STATUSES = frozenset({MemoryStatus.SUPERSEDED, MemoryStatus.REVOKED, MemoryStatus.EXPIRED})


def new_memory_id() -> str:
    return f"mem_{uuid.uuid4().hex}"


def new_transaction_id() -> str:
    return f"mtx_{uuid.uuid4().hex}"


def normalize_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).strip().split())


def normalize_match_text(value: str) -> str:
    return normalize_text(value).casefold()


def normalize_slot(value: str) -> str:
    normalized = normalize_match_text(value).replace(" ", ".")
    if not SLOT_RE.fullmatch(normalized):
        raise ValueError("invalid memory slot")
    return normalized


def _coerce_status(value: MemoryStatus | str) -> MemoryStatus:
    if isinstance(value, MemoryStatus):
        return value
    return MemoryStatus(value)


LEGAL_STATUS_TRANSITIONS = {
    MemoryStatus.CANDIDATE: frozenset(
        {
            MemoryStatus.CANDIDATE,
            MemoryStatus.ACTIVE,
            MemoryStatus.SUPERSEDED,
            MemoryStatus.REVOKED,
            MemoryStatus.EXPIRED,
        }
    ),
    MemoryStatus.ACTIVE: frozenset(
        {
            MemoryStatus.ACTIVE,
            MemoryStatus.SUPERSEDED,
            MemoryStatus.REVOKED,
            MemoryStatus.EXPIRED,
        }
    ),
    MemoryStatus.SUPERSEDED: frozenset(),
    MemoryStatus.REVOKED: frozenset(),
    MemoryStatus.EXPIRED: frozenset(),
}


def assert_transition(old: MemoryStatus | str, new: MemoryStatus | str) -> None:
    old_status = _coerce_status(old)
    new_status = _coerce_status(new)
    if new_status not in LEGAL_STATUS_TRANSITIONS[old_status]:
        raise InvalidMemoryTransition(f"illegal memory transition: {old_status.value} -> {new_status.value}")


def content_hash(kind: MemoryKind, scope: MemoryScope, subject: str, slot: str, statement: str) -> str:
    canonical = json.dumps(
        [
            kind.value,
            scope.model_dump(mode="json"),
            normalize_match_text(subject),
            normalize_slot(slot),
            normalize_text(statement),
        ],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def conflict_key(scope: MemoryScope, subject: str, kind: MemoryKind, slot: str) -> str:
    return "|".join(
        (
            scope.kind.value,
            scope.key,
            normalize_match_text(subject),
            kind.value,
            normalize_slot(slot),
        )
    )


def transaction_checksum(transaction: MemoryTransaction) -> str:
    payload = transaction.model_dump(mode="json", exclude={"checksum_sha256"})
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _normalize_datetime(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("datetime must carry a timezone")
    return value.astimezone(timezone.utc)


def _parse_datetime(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    return _normalize_datetime(value)


# ---------------------------------------------------------------------------
# Evidence and scope
# ---------------------------------------------------------------------------


class EvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: EvidenceKind
    ref: str = Field(min_length=1, max_length=512)
    excerpt: str = Field(default="", max_length=1000)
    sha256: str | None = None
    observed_at: datetime | None = None

    @field_validator("ref", "excerpt", mode="before")
    @classmethod
    def _strip_text(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("sha256")
    @classmethod
    def _validate_sha256(cls, value, info):
        if value is None:
            kind = info.data.get("kind")
            if kind in {EvidenceKind.FILE, EvidenceKind.TOOL_RESULT, EvidenceKind.GIT}:
                raise ValueError(f"evidence of kind {kind.value} requires sha256")
            return None
        if not SHA256_RE.fullmatch(value):
            raise ValueError("sha256 must be 64 lowercase hex characters")
        return value

    @field_validator("observed_at")
    @classmethod
    def _validate_observed_at(cls, value):
        return _parse_datetime(value)


class MemoryScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ScopeKind
    key: str = Field(min_length=1)

    @field_validator("key")
    @classmethod
    def _key_must_match_kind(cls, value, info):
        kind = info.data.get("kind")
        key = value.strip()
        if kind is None:
            return key
        prefix = f"{kind.value}:"
        if not key.startswith(prefix) or key == prefix:
            raise ValueError(f"scope key must start with {prefix!r}")
        if kind is ScopeKind.SHARED and key != "shared:*":
            raise ValueError("shared scope key must be exactly 'shared:*'")
        return key


# ---------------------------------------------------------------------------
# MemoryRecord
# ---------------------------------------------------------------------------


class MemoryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = SCHEMA_VERSION
    id: str
    revision: int = Field(ge=1)
    status: MemoryStatus
    kind: MemoryKind
    scope: MemoryScope
    subject: str
    slot: str
    statement: str
    detail: str = ""
    tags: tuple[str, ...]
    aliases: tuple[str, ...] = ()
    source_level: SourceLevel
    confidence: float = Field(ge=0.0, le=1.0)
    importance: int = Field(ge=1, le=5)
    evidence: tuple[EvidenceRef, ...]
    content_hash: str
    derived_from: tuple[str, ...] = ()
    supersedes: tuple[str, ...] = ()
    replacement_id: str | None = None
    blocked_by: tuple[str, ...] = ()
    valid_from: datetime
    expires_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    status_reason: str = ""

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value):
        if value != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema version: {value}")
        return value

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value):
        if not MEMORY_ID_RE.fullmatch(value):
            raise ValueError("memory id must match mem_[0-9a-f]{32}")
        return value

    @field_validator("content_hash")
    @classmethod
    def _validate_content_hash(cls, value):
        if not SHA256_RE.fullmatch(value):
            raise ValueError("content_hash must be 64 lowercase hex characters")
        return value

    @field_validator("valid_from", "created_at", "updated_at", "expires_at")
    @classmethod
    def _validate_datetime(cls, value):
        return _parse_datetime(value)

    @model_validator(mode="after")
    def _normalize(self):
        subject = normalize_text(self.subject)
        if not 1 <= len(subject) <= 160:
            raise ValueError("subject must be 1..160 characters after normalization")
        slot = normalize_slot(self.slot)
        statement = normalize_text(self.statement)
        if not 1 <= len(statement) <= 500:
            raise ValueError("statement must be 1..500 characters after normalization")
        detail = unicodedata.normalize("NFKC", self.detail).strip()
        if len(detail) > 2000:
            raise ValueError("detail must be at most 2000 characters")
        tags = tuple(sorted({normalize_text(tag) for tag in self.tags}))
        if not 1 <= len(tags) <= 12:
            raise ValueError("tags must contain 1..12 entries")
        for tag in tags:
            if not SLOT_RE.fullmatch(tag):
                raise ValueError(f"invalid tag name: {tag}")
        aliases = tuple(sorted({normalize_text(alias) for alias in self.aliases}))
        if len(aliases) > 20:
            raise ValueError("aliases must contain at most 20 entries")
        for alias in aliases:
            if not 1 <= len(alias) <= 80:
                raise ValueError("alias must be 1..80 characters")
        evidence: tuple[EvidenceRef, ...] = tuple(
            sorted({(e.kind, e.ref, e.sha256): e for e in self.evidence}.values(), key=lambda e: (e.kind.value, e.ref))
        )
        if not evidence:
            raise ValueError("at least one evidence is required")
        derived_from = tuple(sorted(set(self.derived_from)))
        supersedes = tuple(sorted(set(self.supersedes)))
        blocked_by = tuple(sorted(set(self.blocked_by)))
        if self.status is not MemoryStatus.SUPERSEDED and self.replacement_id is not None:
            raise ValueError("replacement_id is only valid for superseded records")
        if self.status is not MemoryStatus.CANDIDATE and blocked_by:
            raise ValueError("blocked_by is only valid for candidate records")
        content = content_hash(self.kind, self.scope, subject, slot, statement)
        for name, value in (
            ("subject", subject),
            ("slot", slot),
            ("statement", statement),
            ("detail", detail),
            ("tags", tags),
            ("aliases", aliases),
            ("evidence", evidence),
            ("content_hash", content),
            ("derived_from", derived_from),
            ("supersedes", supersedes),
            ("blocked_by", blocked_by),
        ):
            object.__setattr__(self, name, value)
        return self

    @property
    def conflict_key(self) -> str:
        return conflict_key(self.scope, self.subject, self.kind, self.slot)


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


class MemoryOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    op: Literal["put"] = "put"
    record: MemoryRecord


class MemoryTransaction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = SCHEMA_VERSION
    tx_id: str
    recorded_at: datetime
    actor: ActorKind
    reason: str = ""
    source_batch: str = ""
    expected_revisions: dict[str, int]
    operations: tuple[MemoryOperation, ...]
    checksum_sha256: str

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value):
        if value != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema version: {value}")
        return value

    @field_validator("tx_id")
    @classmethod
    def _validate_tx_id(cls, value):
        if not TX_ID_RE.fullmatch(value):
            raise ValueError("transaction id must match mtx_[0-9a-f]{32}")
        return value

    @field_validator("recorded_at")
    @classmethod
    def _validate_recorded_at(cls, value):
        return _parse_datetime(value)

    @field_validator("checksum_sha256")
    @classmethod
    def _validate_checksum(cls, value):
        if not SHA256_RE.fullmatch(value):
            raise ValueError("checksum_sha256 must be 64 lowercase hex characters")
        return value

    @model_validator(mode="after")
    def _validate_structure(self):
        operations = self.operations
        if not 1 <= len(operations) <= 100:
            raise ValueError("operations must contain 1..100 entries")
        ids = [op.record.id for op in operations]
        if len(set(ids)) != len(ids):
            raise ValueError("a transaction may touch each memory id at most once")
        expected = dict(sorted(self.expected_revisions.items()))
        actual = sorted(ids)
        if list(expected) != actual:
            raise ValueError("expected_revisions must exactly cover every operation id")
        return self


# ---------------------------------------------------------------------------
# Extraction proposals
# ---------------------------------------------------------------------------


class CandidateProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal_index: int = Field(ge=0)
    kind: MemoryKind
    scope_hint: ScopeKind = ScopeKind.PROJECT
    subject: str
    slot: str
    statement: str
    detail: str = ""
    tags: tuple[str, ...]
    aliases: tuple[str, ...] = ()
    confidence: float = Field(ge=0.0, le=1.0)
    importance: int = Field(ge=1, le=5)
    evidence_refs: tuple[str, ...]
    speech_act: SourceLevel = SourceLevel.INFERRED
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def _validate_expires_at(cls, value):
        return _parse_datetime(value)


class MemoryExtractionBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = SCHEMA_VERSION
    proposals: tuple[CandidateProposal, ...] = ()

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value):
        if value != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema version: {value}")
        return value

    @model_validator(mode="after")
    def _validate_indices(self):
        indices = [p.proposal_index for p in self.proposals]
        if len(set(indices)) != len(indices):
            raise ValueError("proposal_index values must be unique")
        return self


# ---------------------------------------------------------------------------
# Tag catalog
# ---------------------------------------------------------------------------


class TagDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    aliases: tuple[str, ...] = ()

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value):
        name = normalize_text(value)
        if not SLOT_RE.fullmatch(name):
            raise ValueError(f"invalid tag name: {name}")
        return name

    @field_validator("aliases")
    @classmethod
    def _validate_aliases(cls, value):
        return tuple(sorted({normalize_text(alias) for alias in value if alias.strip()}))


class TagCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = SCHEMA_VERSION
    tags: tuple[TagDefinition, ...] = ()

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value):
        if value != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema version: {value}")
        return value

    @model_validator(mode="after")
    def _validate_unique_names(self):
        names = [normalize_match_text(tag.name) for tag in self.tags]
        if len(set(names)) != len(names):
            raise ValueError("tag catalog contains duplicate names")
        return self

    @classmethod
    def load(cls, path: Path) -> TagCatalog:
        with path.open(encoding="utf-8") as stream:
            data = json.load(stream)
        return cls.model_validate(data)

    def canonical_names(self) -> frozenset[str]:
        return frozenset(tag.name for tag in self.tags)

    def contains(self, name: str) -> bool:
        wanted = normalize_match_text(name)
        return any(normalize_match_text(tag.name) == wanted for tag in self.tags)

    def aliases_for(self, name: str) -> tuple[str, ...]:
        wanted = normalize_match_text(name)
        for tag in self.tags:
            if normalize_match_text(tag.name) == wanted:
                return tag.aliases
        return ()

    def alias_map(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for tag in self.tags:
            for alias in tag.aliases:
                mapping[normalize_match_text(alias)] = tag.name
        return mapping

    def validate_record(self, record: MemoryRecord) -> None:
        for tag in record.tags:
            if not self.contains(tag):
                raise UnknownMemoryTag(f"unknown tag: {tag}")


# ---------------------------------------------------------------------------
# Recall models
# ---------------------------------------------------------------------------


class RecallQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query_text: str
    allowed_scopes: tuple[MemoryScope, ...] = Field(min_length=1)
    now: datetime
    token_budget: int = Field(default=2500, ge=256, le=16_000)
    max_hits: int = Field(default=20, ge=1, le=100)
    requested_kinds: tuple[MemoryKind, ...] = ()
    explicit_tags: tuple[str, ...] = ()
    explicit_ids: tuple[str, ...] = ()

    @field_validator("now")
    @classmethod
    def _validate_now(cls, value):
        return _parse_datetime(value)


class RecallHit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    record: MemoryRecord
    score: int
    reasons: tuple[str, ...]
    tokens: int = 0


class RecallResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    hits: tuple[RecallHit, ...] = ()
    candidates: int = 0
    filtered: int = 0
    excluded_by_budget: int = 0
    tokens_used: int = 0
    degraded: bool = False
    error_code: str | None = None
    error_message: str | None = None


# ---------------------------------------------------------------------------
# Repository health
# ---------------------------------------------------------------------------


class RepositoryHealth(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state: Literal["healthy", "degraded"] = "healthy"
    last_valid_line: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    backend: Literal["sqlite"] = "sqlite"
    schema_version: int | None = None
    last_transaction_seq: int = 0
    migration_state: Literal["not_needed", "pending", "completed", "failed"] = "not_needed"
    audit_exported_seq: int = 0
    database_bytes: int = 0


class MemoryStorageStats(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    backend: Literal["sqlite"] = "sqlite"
    schema_version: int = Field(ge=1)
    transaction_count: int = Field(ge=0)
    revision_count: int = Field(ge=0)
    current_count: int = Field(ge=0)
    last_transaction_seq: int = Field(ge=0)
    audit_exported_seq: int = Field(ge=0)
    database_bytes: int = Field(ge=0)

    @property
    def audit_lag(self) -> int:
        return max(0, self.last_transaction_seq - self.audit_exported_seq)


# ---------------------------------------------------------------------------
# Same-status revision rules (design section 7)
# ---------------------------------------------------------------------------

_SAME_STATUS_CANDIDATE_FIELDS = frozenset({"evidence", "derived_from", "blocked_by", "status_reason", "updated_at"})
_SAME_STATUS_ACTIVE_FIELDS = frozenset({"evidence", "derived_from", "updated_at"})
_IDENTITY_FIELDS = frozenset({"schema_version", "id", "revision"})


def validate_same_status_revision(previous: MemoryRecord, current: MemoryRecord) -> None:
    """Validate a revision that keeps the same status (design section 7).

    Same-status candidate revisions may add evidence/derived_from/blocked_by/
    status_reason; same-status active revisions may only merge evidence and
    derived_from for the identical content hash. Terminal records cannot be
    revised at all.
    """
    if previous.status != current.status:
        return
    if previous.status in TERMINAL_STATUSES:
        raise InvalidMemoryTransition(f"terminal status {previous.status.value} cannot be revised")
    changed = {
        field for field in type(previous).model_fields if field not in _IDENTITY_FIELDS and getattr(previous, field) != getattr(current, field)
    }
    if previous.status is MemoryStatus.CANDIDATE:
        allowed = _SAME_STATUS_CANDIDATE_FIELDS
        must_superset = ("evidence", "derived_from", "blocked_by")
    else:
        assert previous.status is MemoryStatus.ACTIVE
        allowed = _SAME_STATUS_ACTIVE_FIELDS
        must_superset = ("evidence", "derived_from")
        if previous.content_hash != current.content_hash:
            raise InvalidMemoryTransition("active same-status revision cannot change fact fields")
    extra = changed - allowed
    if extra:
        raise InvalidMemoryTransition(f"same-status revision cannot change fields: {sorted(extra)}")
    for field in must_superset:
        if not set(getattr(previous, field)).issubset(set(getattr(current, field))):
            raise InvalidMemoryTransition(f"same-status revision cannot remove {field}")
