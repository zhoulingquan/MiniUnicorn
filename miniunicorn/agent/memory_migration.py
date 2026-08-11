"""Legacy memory -> governed structured memory migration (C2 spec §14).

Deterministic, idempotent, no-LLM import of legacy memory files into the
structured repository. ``dry_run`` performs zero writes; ``apply`` imports
through the journal-backed lifecycle, tracks per-item progress in
``memory/migration-v1.json`` and only writes ``completed_at`` after the full
source scan finished.

Normative source: docs/superpowers/specs/2026-08-11-c2-governed-structured-memory-design.md
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from loguru import logger

from miniunicorn.agent.memory_lifecycle import IngestContext, StructuredMemoryLifecycle
from miniunicorn.agent.memory_models import (
    ActorKind,
    CandidateProposal,
    EvidenceKind,
    EvidenceRef,
    MemoryKind,
    MemoryScope,
    ScopeKind,
    SourceLevel,
    normalize_match_text,
    normalize_text,
)
from miniunicorn.agent.memory_repository import StructuredMemoryRepository

MIGRATION_SOURCE_BATCH = "migration:v1"
MIGRATION_STATE_FILE = "memory/migration-v1.json"

_MD_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*$")
_MD_BULLET_RE = re.compile(r"^\s{0,3}[-*+]\s+(?P<body>\S.*?)\s*$")
_MD_ORDERED_RE = re.compile(r"^\s{0,3}\d{1,6}[.)]\s+(?P<body>\S.*?)\s*$")

# 确定性标题 -> (kind, tag) 映射（NFKC + casefold，不调用 LLM）。
_USER_HEADING_KINDS: dict[tuple[str, ...], tuple[MemoryKind, str]] = {
    ("preference", "preferences", "偏好", "喜好"): (MemoryKind.PREFERENCE, "user.preference"),
    ("identity", "identities", "user profile", "用户", "身份"): (MemoryKind.IDENTITY, "user.identity"),
}
_USER_DEFAULT_KIND_TAG: tuple[MemoryKind, str] = (MemoryKind.IDENTITY, "user.identity")

_MEMORY_HEADING_KINDS: dict[tuple[str, ...], tuple[MemoryKind, str]] = {
    ("decision", "decisions", "决策", "决定"): (MemoryKind.DECISION, "project.decision"),
    ("constraint", "constraints", "约束", "限制"): (MemoryKind.CONSTRAINT, "project.constraint"),
    ("requirement", "requirements", "需求", "要求"): (MemoryKind.FACT, "project.requirement"),
}
_MEMORY_DEFAULT_KIND_TAG: tuple[MemoryKind, str] = (MemoryKind.FACT, "project.fact")

_SHARED_KIND_TAG: tuple[MemoryKind, str] = (MemoryKind.FACT, "shared.fact")
_PROCEDURE_TAGS_PROJECT: tuple[str, ...] = ("workflow.procedure",)
_PROCEDURE_TAGS_SHARED: tuple[str, ...] = ("workflow.procedure", "shared.fact")
_EVENT_TAGS: tuple[str, ...] = ("session.event",)

_MAX_STATEMENT_CHARS = 500
_MAX_SUBJECT_CHARS = 160
_MAX_EVIDENCE_EXCERPT = 1000


@dataclass(frozen=True)
class MigrationItem:
    """One atomic, importable legacy entry with its deterministic identity."""

    relative_path: str
    locator: str
    subject: str
    statement: str
    kind: MemoryKind
    scope_kind: ScopeKind
    tags: tuple[str, ...]
    confidence: float

    def legacy_key(self) -> str:
        payload = f"{self.relative_path}\n{self.locator}\n{normalize_text(self.statement)}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MigrationIssue:
    """Scan-time issue (skipped paragraph, unreadable line, ...)."""

    relative_path: str
    locator: str
    reason: str


@dataclass(frozen=True)
class MigrationFailure:
    """Apply-time ingest failure; the item is retried on the next run."""

    item: MigrationItem
    error: str


@dataclass(frozen=True)
class MigrationReport:
    dry_run: bool
    scanned: int
    imported: int
    skipped: int
    failed: tuple[MigrationFailure, ...]
    issues: tuple[MigrationIssue, ...]
    completed_at: datetime | None


# ---------------------------------------------------------------------------
# Scanning (read-only)
# ---------------------------------------------------------------------------


def _map_heading(
    title: str, kinds: Mapping[tuple[str, ...], tuple[MemoryKind, str]], default: tuple[MemoryKind, str]
) -> tuple[MemoryKind, str]:
    normalized = normalize_match_text(title)
    for keywords, kind_tag in kinds.items():
        if any(keyword in normalized for keyword in keywords):
            return kind_tag
    return default


def _scan_markdown(
    path: Path,
    relative_path: str,
    kinds: Mapping[tuple[str, ...], tuple[MemoryKind, str]],
    default: tuple[MemoryKind, str],
    scope_kind: ScopeKind,
    items: list[MigrationItem],
    issues: list[MigrationIssue],
) -> None:
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        issues.append(MigrationIssue(relative_path, "file", f"unreadable: {exc}"))
        return
    heading: tuple[str, tuple[MemoryKind, str]] | None = None
    for lineno, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped:
            continue
        match = _MD_HEADING_RE.match(raw)
        if match is not None:
            heading = (match.group(2).strip(), _map_heading(match.group(2), kinds, default))
            continue
        body = None
        bullet = _MD_BULLET_RE.match(raw)
        if bullet is not None:
            body = bullet.group("body")
        else:
            ordered = _MD_ORDERED_RE.match(raw)
            if ordered is not None:
                body = ordered.group("body")
        if body is not None:
            title, (kind, tag) = heading if heading is not None else ("", default)
            subject = normalize_text(title)[:_MAX_SUBJECT_CHARS] or "general"
            items.append(
                MigrationItem(
                    relative_path=relative_path,
                    locator=f"L{lineno}",
                    subject=subject,
                    statement=body,
                    kind=kind,
                    scope_kind=scope_kind,
                    tags=(tag,),
                    confidence=0.9,
                )
            )
            continue
        issues.append(MigrationIssue(relative_path, f"L{lineno}", "non-atomic paragraph skipped"))


def _scan_jsonl(
    path: Path,
    relative_path: str,
    kind: MemoryKind,
    tags: tuple[str, ...],
    scope_kind: ScopeKind,
    subject: str,
    confidence: float,
    items: list[MigrationItem],
    issues: list[MigrationIssue],
) -> None:
    if not path.exists():
        return
    try:
        with path.open("r", encoding="utf-8") as stream:
            for lineno, raw in enumerate(stream, start=1):
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    payload = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    issues.append(MigrationIssue(relative_path, f"L{lineno}", f"invalid jsonl: {exc}"))
                    continue
                if not isinstance(payload, dict) or not isinstance(payload.get("content"), str):
                    issues.append(MigrationIssue(relative_path, f"L{lineno}", "missing content field"))
                    continue
                content = payload["content"].strip()
                if not content:
                    issues.append(MigrationIssue(relative_path, f"L{lineno}", "empty content"))
                    continue
                items.append(
                    MigrationItem(
                        relative_path=relative_path,
                        locator=f"L{lineno}",
                        subject=subject,
                        statement=content,
                        kind=kind,
                        scope_kind=scope_kind,
                        tags=tags,
                        confidence=confidence,
                    )
                )
    except (OSError, UnicodeError) as exc:
        issues.append(MigrationIssue(relative_path, "file", f"unreadable: {exc}"))


def scan_legacy_memory(workspace: Path) -> tuple[list[MigrationItem], list[MigrationIssue]]:
    """Scan all legacy sources (read-only). Returns (items, issues)."""
    items: list[MigrationItem] = []
    issues: list[MigrationIssue] = []
    _scan_markdown(
        workspace / "USER.md", "USER.md", _USER_HEADING_KINDS, _USER_DEFAULT_KIND_TAG, ScopeKind.USER, items, issues
    )
    _scan_markdown(
        workspace / "memory" / "MEMORY.md",
        "memory/MEMORY.md",
        _MEMORY_HEADING_KINDS,
        _MEMORY_DEFAULT_KIND_TAG,
        ScopeKind.PROJECT,
        items,
        issues,
    )
    _scan_markdown(
        workspace / "memory" / "shared" / "MEMORY_SHARED.md",
        "memory/shared/MEMORY_SHARED.md",
        {},
        _SHARED_KIND_TAG,
        ScopeKind.SHARED,
        items,
        issues,
    )
    _scan_jsonl(
        workspace / "memory" / "procedural.jsonl",
        "memory/procedural.jsonl",
        MemoryKind.PROCEDURE,
        _PROCEDURE_TAGS_PROJECT,
        ScopeKind.PROJECT,
        "procedure",
        0.9,
        items,
        issues,
    )
    _scan_jsonl(
        workspace / "memory" / "shared" / "procedural_shared.jsonl",
        "memory/shared/procedural_shared.jsonl",
        MemoryKind.PROCEDURE,
        _PROCEDURE_TAGS_SHARED,
        ScopeKind.SHARED,
        "procedure",
        0.9,
        items,
        issues,
    )
    _scan_jsonl(
        workspace / "memory" / "episodic.jsonl",
        "memory/episodic.jsonl",
        MemoryKind.OUTCOME,
        _EVENT_TAGS,
        ScopeKind.PROJECT,
        "session",
        0.6,
        items,
        issues,
    )
    return items, issues


# ---------------------------------------------------------------------------
# Proposal / evidence construction
# ---------------------------------------------------------------------------


def _evidence_for(item: MigrationItem) -> tuple[EvidenceRef, ...]:
    normalized = normalize_text(item.statement)
    excerpt = normalized[:_MAX_EVIDENCE_EXCERPT]
    if item.kind in (MemoryKind.PROCEDURE, MemoryKind.OUTCOME):
        # 程序性经验 = 重复观察（两个独立 ref）→ repeated_experience；
        # 会话事件只作 inferred 证据，永不自动提升。
        return (
            EvidenceRef(kind=EvidenceKind.MODEL_INFERENCE, ref=f"{item.relative_path}#{item.locator}", excerpt=excerpt),
            EvidenceRef(kind=EvidenceKind.MODEL_INFERENCE, ref=f"{item.relative_path}#{item.locator}.v2", excerpt=excerpt),
        )
    sha256 = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
    base = EvidenceRef(
        kind=EvidenceKind.FILE,
        ref=f"{item.relative_path}#{item.locator}",
        excerpt=excerpt,
        sha256=sha256,
    )
    return (base,)


def _proposal_for(item: MigrationItem, index: int) -> CandidateProposal:
    evidence = _evidence_for(item)
    return CandidateProposal(
        proposal_index=index,
        kind=item.kind,
        scope_hint=ScopeKind.PROJECT,
        subject=item.subject,
        slot="general",
        statement=item.statement,
        tags=item.tags,
        confidence=item.confidence,
        importance=3,
        evidence_refs=tuple(f"src:{i}" for i in range(len(evidence))),
        speech_act=SourceLevel.INFERRED,
    )


def _validate_item(item: MigrationItem) -> str | None:
    statement = normalize_text(item.statement)
    if not 1 <= len(statement) <= _MAX_STATEMENT_CHARS:
        return f"statement must be 1..{_MAX_STATEMENT_CHARS} characters after normalization"
    if not 1 <= len(item.subject) <= _MAX_SUBJECT_CHARS:
        return f"subject must be 1..{_MAX_SUBJECT_CHARS} characters"
    return None


# ---------------------------------------------------------------------------
# State file (memory/migration-v1.json)
# ---------------------------------------------------------------------------


@dataclass
class MigrationState:
    entries: dict[str, str]
    completed_at: datetime | None

    @classmethod
    def load(cls, path: Path) -> "MigrationState":
        if not path.exists():
            return cls(entries={}, completed_at=None)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            entries = {
                str(key): str(value)
                for key, value in payload.get("entries", {}).items()
                if isinstance(key, str) and isinstance(value, str)
            }
            completed_raw = payload.get("completed_at")
            completed_at: datetime | None = None
            if isinstance(completed_raw, str):
                try:
                    completed_at = datetime.fromisoformat(completed_raw)
                except ValueError:
                    completed_at = None
            return cls(entries=entries, completed_at=completed_at)
        except (OSError, ValueError) as exc:
            logger.warning("memory_migration_state_unreadable path={} error={}", path, exc)
            return cls(entries={}, completed_at=None)

    def save(self, path: Path) -> None:
        payload = {
            "schema_version": 1,
            "entries": dict(sorted(self.entries.items())),
            "completed_at": self.completed_at.isoformat() if self.completed_at is not None else None,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp")
        with tmp.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Migrator
# ---------------------------------------------------------------------------


class MemoryMigration:
    """Run a dry-run (zero writes) or apply (journal-backed) migration."""

    def __init__(
        self,
        workspace: Path,
        repository: StructuredMemoryRepository | None,
        lifecycle: StructuredMemoryLifecycle | None,
        project_scope_key: str,
    ):
        self.workspace = Path(workspace)
        self.repository = repository
        self.lifecycle = lifecycle
        self.project_scope_key = project_scope_key
        self.state_path = self.workspace / MIGRATION_STATE_FILE

    def plan(self) -> tuple[list[MigrationItem], list[MigrationIssue]]:
        return scan_legacy_memory(self.workspace)

    def dry_run(self) -> MigrationReport:
        items, issues = scan_legacy_memory(self.workspace)
        failed: list[MigrationFailure] = []
        imported = 0
        for index, item in enumerate(items):
            error = _validate_item(item)
            if error is None:
                try:
                    _proposal_for(item, index)
                    imported += 1
                except Exception as exc:  # noqa: BLE001 - report, never drop silently
                    failed.append(MigrationFailure(item, f"{type(exc).__name__}: {exc}"))
            else:
                failed.append(MigrationFailure(item, error))
        state = MigrationState.load(self.state_path)
        skipped = sum(1 for item in items if item.legacy_key() in state.entries)
        return MigrationReport(
            dry_run=True,
            scanned=len(items),
            imported=imported,
            skipped=skipped,
            failed=tuple(failed),
            issues=tuple(issues),
            completed_at=state.completed_at,
        )

    def apply(self) -> MigrationReport:
        if self.repository is None or self.lifecycle is None:
            from miniunicorn.agent.memory_models import MemoryWriteError

            raise MemoryWriteError("apply requires the structured repository/lifecycle stack")
        items, issues = scan_legacy_memory(self.workspace)
        state = MigrationState.load(self.state_path)
        failed: list[MigrationFailure] = []
        imported = 0
        skipped = 0
        for index, item in enumerate(items):
            key = item.legacy_key()
            if key in state.entries:
                skipped += 1
                continue
            error = _validate_item(item)
            if error is None:
                try:
                    memory_id = self._ingest_item(item, index)
                except Exception as exc:  # noqa: BLE001 - report, never drop silently
                    logger.warning("memory_migration_item_failed path={} locator={} error={}", item.relative_path, item.locator, exc)
                    failed.append(MigrationFailure(item, f"{type(exc).__name__}: {exc}"))
                    continue
            else:
                failed.append(MigrationFailure(item, error))
                continue
            state.entries[key] = memory_id
            imported += 1
            if len(state.entries) % 100 == 0:
                state.save(self.state_path)
        state.completed_at = datetime.now(timezone.utc)
        state.save(self.state_path)
        logger.info(
            "memory_migration_completed dry_run=false scanned={} imported={} skipped={} failed={}",
            len(items),
            imported,
            skipped,
            len(failed),
        )
        return MigrationReport(
            dry_run=False,
            scanned=len(items),
            imported=imported,
            skipped=skipped,
            failed=tuple(failed),
            issues=tuple(issues),
            completed_at=state.completed_at,
        )

    def _ingest_item(self, item: MigrationItem, index: int) -> str:
        evidence = _evidence_for(item)
        catalog = {f"src:{i}": ref for i, ref in enumerate(evidence)}
        scope = MemoryScope(kind=item.scope_kind, key=self._scope_key(item.scope_kind))
        proposal = _proposal_for(item, index)
        context = IngestContext(
            actor=ActorKind.MIGRATION,
            reason=f"migration:v1 {item.relative_path} {item.locator}",
            source_batch=MIGRATION_SOURCE_BATCH,
            scope=scope,
            evidence_catalog=catalog,
            now=datetime.now(timezone.utc),
        )
        result = self.lifecycle.ingest(proposal, context)  # type: ignore[union-attr]
        return result.candidate_id

    def _scope_key(self, kind: ScopeKind) -> str:
        if kind is ScopeKind.USER:
            return "user:default"
        if kind is ScopeKind.SHARED:
            return "shared:*"
        return self.project_scope_key
