"""Slash commands for governed structured memory (C2 spec §15).

All mutating commands use actor=user and record a reason in the transaction.
Evidence excerpts are truncated to 200 characters in every response.
"""

from __future__ import annotations

import hashlib
import shlex
from datetime import datetime, timezone

from erza.bus.events import OutboundMessage
from erza.command.router import CommandContext, CommandRouter
from erza.memory import MemoryStore
from erza.memory.backup import MemoryBackupManager
from erza.memory.lifecycle import IngestContext, MemoryLifecycleError
from erza.memory.models import (
    ActorKind,
    CandidateProposal,
    EvidenceKind,
    EvidenceRef,
    MemoryError,
    MemoryKind,
    MemoryRecord,
    MemoryScope,
    MemoryStatus,
    ScopeKind,
    SourceLevel,
    normalize_text,
)

_MAX_LIST_ITEMS = 20
_EXCERPT_LIMIT = 200
_STATEMENT_DISPLAY_LIMIT = 100
_NOT_FOUND = "No memory record with id `{}`."
_NOT_FOUND_TX = "No memory transaction with id `{}`."

_LIST_STATUSES = frozenset({s.value for s in MemoryStatus})


def _reply(ctx: CommandContext, content: str) -> OutboundMessage:
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=content,
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


def _usage(ctx: CommandContext, usage: str) -> OutboundMessage:
    return _reply(ctx, f"Usage: {usage}")


def _split_args_or_usage(
    ctx: CommandContext, usage: str
) -> tuple[list[str] | None, OutboundMessage | None]:
    """Split arguments, returning an usage reply for malformed shell syntax.

    Only shlex syntax errors (for example an unterminated quote) are trapped
    here; genuine program errors must keep raising.
    """
    try:
        return shlex.split(ctx.args), None
    except ValueError:
        return None, _usage(ctx, usage)


def _stack(ctx: CommandContext):
    """Return the effective store, repository, and lifecycle for the command."""
    loop = ctx.loop
    if loop is None:
        raise MemoryError("memory commands require an agent loop")
    store = _effective_store(ctx)
    return store, store.structured_repository, store.structured_lifecycle


def _effective_store(ctx: CommandContext) -> MemoryStore:
    """Resolve the governed store for the command's effective workspace.

    Production loops resolve the scope exactly like a normal agent turn
    (message metadata + persisted session metadata through the loop's
    ``WorkspaceScopeResolver``) and fetch the store with
    ``loop.memory_for(scope.project_path)``, never the default store. Loops
    without a resolver (lightweight test doubles) fall back to their default
    memory store.
    """
    loop = ctx.loop
    if loop is None:
        raise MemoryError("memory commands require an agent loop")
    resolver = getattr(loop, "workspace_scopes", None)
    if resolver is None:
        store = getattr(getattr(loop, "context", None), "memory", None)
        if store is None:
            raise MemoryError("memory commands require an agent loop")
        return store
    session = ctx.session
    if session is None:
        sessions = getattr(loop, "sessions", None)
        if sessions is not None:
            session = sessions.get_or_create(ctx.key)
    session_metadata = session.metadata if session is not None else {}
    scope = resolver.for_message(ctx.msg, session_metadata)
    return loop.memory_for(scope.project_path)


def _allowed_scopes(ctx: CommandContext, store: MemoryStore) -> frozenset[MemoryScope]:
    """Scopes visible to the command caller, canonicalized like recall.

    The session key strips any ``#`` fork suffix before the ``session:``
    prefix, mirroring ``ContextBuilder._build_recall_query``. Missing or
    subagent sender identities fall back to ``user:default`` exactly like
    recall user-key resolution.
    """
    sender_id = getattr(ctx.msg, "sender_id", None)
    user_key = f"user:{sender_id}" if sender_id and sender_id != "subagent" else "user:default"
    scopes = [
        MemoryScope(kind=ScopeKind.PROJECT, key=store.project_scope_key),
        MemoryScope(kind=ScopeKind.USER, key=user_key),
        MemoryScope(kind=ScopeKind.SHARED, key="shared:*"),
    ]
    if ctx.key:
        scopes.append(
            MemoryScope(kind=ScopeKind.SESSION, key=f"session:{ctx.key.split('#', 1)[0]}")
        )
    return frozenset(scopes)


def _record_visible(record: MemoryRecord, allowed: frozenset[MemoryScope]) -> bool:
    return record.scope in allowed


def _transaction_visible(tx, allowed: frozenset[MemoryScope]) -> bool:
    return all(record.scope in allowed for record in (op.record for op in tx.operations))


def _visible_transactions(repository, allowed: frozenset[MemoryScope], limit: int):
    return [
        tx for tx in repository.transaction_log(limit=limit) if _transaction_visible(tx, allowed)
    ]


def _visible_record_by_id(
    repository, memory_id: str, allowed: frozenset[MemoryScope]
) -> MemoryRecord | None:
    record = repository.get(memory_id)
    if record is None or not _record_visible(record, allowed):
        return None
    return record


def _not_found_reply(ctx: CommandContext, memory_id: str) -> OutboundMessage:
    return _reply(ctx, _NOT_FOUND.format(memory_id))


def _requires_stack(handler):
    """Turn stack-unavailable MemoryError into a friendly reply."""

    async def wrapper(ctx: CommandContext) -> OutboundMessage:
        try:
            return await handler(ctx)
        except MemoryError as exc:
            return _reply(ctx, str(exc))

    wrapper.__name__ = getattr(handler, "__name__", "handler")
    wrapper.__doc__ = getattr(handler, "__doc__", None)
    return wrapper


# ---------------------------------------------------------------------------
# Status / list / show
# ---------------------------------------------------------------------------


@_requires_stack
async def cmd_memory_status(ctx: CommandContext) -> OutboundMessage:
    """Show architecture, health, status counts, and the last write error."""
    store, repository, _ = _stack(ctx)
    allowed = _allowed_scopes(ctx, store)
    counts = {
        s.value: sum(
            1 for record in repository.current_records(s) if _record_visible(record, allowed)
        )
        for s in MemoryStatus
    }
    health = repository.health  # type: ignore[union-attr]
    stats = repository.storage_stats()  # type: ignore[union-attr]
    lines = [
        "## Memory status",
        "- Architecture: `governed`",
        f"- Backend: `{stats.backend}`",
        f"- Schema: `v{stats.schema_version}`",
        f"- Health: `{health.state}` (last valid journal line: `{health.last_valid_line}`)",
        (
            f"- Records: candidate={counts['candidate']} active={counts['active']} "
            f"superseded={counts['superseded']} revoked={counts['revoked']} expired={counts['expired']}"
        ),
        f"- Transactions: `{stats.transaction_count}`",
        f"- Revisions: `{stats.revision_count}`",
        f"- Current: `{stats.current_count}`",
        f"- Database size: `{stats.database_bytes:,} bytes`",
        f"- Audit exported seq: `{stats.audit_exported_seq}`",
        f"- Audit lag: `{stats.audit_lag}`",
    ]
    migration = health.migration_state or "not_needed"
    lines.append(f"- Migration: `{migration}`")
    if health.error_message:
        lines.append(f"- Last write error: `{health.error_message}`")
    return _reply(ctx, "\n".join(lines))


@_requires_stack
async def cmd_memory_log(ctx: CommandContext) -> OutboundMessage:
    """Show transaction log entries, newest first, at most 20.

    Without a transaction id this lists recent transactions as one line each
    (timestamps, actors, reasons, touched record ids, no evidence). With a
    transaction id it shows the operations and the evidence excerpts of that
    transaction. Transactions whose records belong to other identities are
    withheld exactly like records in list/show.
    """
    store, repository, _ = _stack(ctx)
    allowed = _allowed_scopes(ctx, store)
    parts, usage = _split_args_or_usage(ctx, "/memory-log [<tx-id>]")
    if usage is not None:
        return usage
    if len(parts) > 1:
        return _usage(ctx, "/memory-log [<tx-id>]")
    if not parts:
        transactions = _visible_transactions(repository, allowed, limit=_MAX_LIST_ITEMS)
        lines = [f"## Memory transaction log ({len(transactions)})"]
        for tx in transactions:
            record_ids = ", ".join(f"`{op.record.id}`" for op in tx.operations)
            lines.append(
                f"- `{tx.tx_id}` {tx.recorded_at} actor=`{tx.actor.value}` "
                f"reason={tx.reason} (records={record_ids})"
            )
        return _reply(ctx, "\n".join(lines))
    tx = repository.transaction_log(limit=1, tx_id=parts[0])  # type: ignore[union-attr]
    if not tx or not _transaction_visible(tx[0], allowed):
        return _reply(ctx, _NOT_FOUND_TX.format(parts[0]))
    lines = [
        f"## Memory transaction {tx[0].tx_id}",
        f"- actor: `{tx[0].actor.value}`",
        f"- reason: {tx[0].reason}",
        f"- timestamp: {tx[0].recorded_at}",
    ]
    for index, operation in enumerate(tx[0].operations, start=1):
        lines.append(
            f"operation {index}: `{operation.op}` record "
            f"`{operation.record.id}` status `{operation.record.status.value}` "
            f"scope `{operation.record.scope.kind.value}:{operation.record.scope.key}`"
        )
        for evidence_index, evidence in enumerate(operation.record.evidence, start=1):
            excerpt = normalize_text(evidence.excerpt)[:_EXCERPT_LIMIT]
            lines.append(
                f"  evidence {evidence_index}: kind=`{evidence.kind.value}` "
                f"ref=`{evidence.ref}` excerpt={excerpt or '_(none)_'}"
            )
        lines.append(f"  statement: {operation.record.statement}")
    return _reply(ctx, "\n".join(lines))


@_requires_stack
async def cmd_memory_backup(ctx: CommandContext) -> OutboundMessage:
    """Create an integrity-verified snapshot of the SQLite memory database."""
    store, repository, _ = _stack(ctx)
    try:
        result = MemoryBackupManager(repository).create_backup()  # type: ignore[union-attr]
    except MemoryError as exc:
        return _reply(ctx, str(exc))
    return _reply(
        ctx,
        "\n".join(
            [
                "## Memory backup created",
                f"- Backup id: `{result.backup_id}`",
                f"- SHA-256: `{result.sha256}`",
                "- Integrity: `ok`",
                f"- Transaction seq: `{result.last_transaction_seq}`",
                f"- Size: `{result.path.stat().st_size:,} bytes`",
            ]
        ),
    )


@_requires_stack
async def cmd_memory_restore(ctx: CommandContext) -> OutboundMessage:
    """Restore the database from one of its own backups (with a safety copy)."""
    store, repository, _ = _stack(ctx)
    parts, usage = _split_args_or_usage(ctx, "/memory-restore <backup-id>")
    if usage is not None:
        return usage
    if len(parts) != 1:
        return _usage(ctx, "/memory-restore <backup-id>")
    backup_id = parts[0]
    try:
        result = MemoryBackupManager(repository).restore_backup(backup_id)  # type: ignore[union-attr]
    except MemoryError as exc:
        if getattr(exc, "code", None) == "backup_not_found":
            return _reply(ctx, f"No memory backup with id `{backup_id}`.")
        return _reply(ctx, str(exc))
    return _reply(
        ctx,
        "\n".join(
            [
                "## Memory restored",
                f"- Backup: `{backup_id}`",
                f"- Safety backup: `{result.safety_backup_id}`",
                f"- Transaction seq: `{result.restored_tx_seq}`",
            ]
        ),
    )


@_requires_stack
async def cmd_memory_export_audit(ctx: CommandContext) -> OutboundMessage:
    """Export pending transactions to the segmented JSONL audit (or rebuild it)."""
    store, _, _ = _stack(ctx)
    args = ctx.args.strip()
    if args and args != "--rebuild":
        return _usage(ctx, "/memory-export-audit [--rebuild]")
    try:
        if args == "--rebuild":
            result = store.audit_exporter.rebuild()  # type: ignore[union-attr]
        else:
            result = store.audit_exporter.export_pending()  # type: ignore[union-attr]
    except MemoryError as exc:
        return _reply(ctx, str(exc))
    return _reply(
        ctx,
        "\n".join(
            [
                "## Audit export",
                f"- Rows: `{result.exported_rows}`",
                f"- Rebuild: `{'yes' if args == '--rebuild' else 'no'}`",
            ]
        ),
    )


@_requires_stack
async def cmd_memory_list(ctx: CommandContext) -> OutboundMessage:
    """List records, newest status first, at most 20."""
    store, repository, _ = _stack(ctx)
    allowed = _allowed_scopes(ctx, store)
    status_arg = ctx.args.strip()
    if status_arg:
        if status_arg not in _LIST_STATUSES:
            return _usage(ctx, "/memory-list [candidate|active|superseded|revoked|expired]")
        status = MemoryStatus(status_arg)
    else:
        status = None
    records = [
        record
        for record in repository.current_records(status)  # type: ignore[union-attr]
        if _record_visible(record, allowed)
    ]
    total = len(records)
    records = records[:_MAX_LIST_ITEMS]
    lines = [
        f"## Memory records ({total})",
    ]
    for record in records:
        statement = normalize_text(record.statement)[:_STATEMENT_DISPLAY_LIMIT]
        lines.append(
            f"- `{record.id}` `{record.status.value}` `{record.kind.value}` "
            f"`{record.scope.kind.value}:{record.scope.key}` "
            f"`{record.source_level.value}` {statement}"
        )
    if total > _MAX_LIST_ITEMS:
        lines.append(f"_... {total - _MAX_LIST_ITEMS} more (use a status filter)_")
    return _reply(ctx, "\n".join(lines))


@_requires_stack
async def cmd_memory_show(ctx: CommandContext) -> OutboundMessage:
    """Show all revisions, evidence and the replace chain for one record."""
    store, repository, _ = _stack(ctx)
    allowed = _allowed_scopes(ctx, store)
    parts, usage = _split_args_or_usage(ctx, "/memory-show <id>")
    if usage is not None:
        return usage
    if len(parts) != 1:
        return _usage(ctx, "/memory-show <id>")
    memory_id = parts[0]
    record = _visible_record_by_id(repository, memory_id, allowed)
    if record is None:
        return _not_found_reply(ctx, memory_id)
    lines = [f"## Memory {memory_id}"]
    for revision in repository.revisions(memory_id):  # type: ignore[union-attr]
        if not _record_visible(revision, allowed):
            continue
        lines.extend(
            [
                f"### rev {revision.revision} `{revision.status.value}`",
                f"- kind: `{revision.kind.value}` scope: `{revision.scope.kind.value}:{revision.scope.key}`",
                f"- subject: `{revision.subject}` slot: `{revision.slot}`",
                f"- statement: {revision.statement}",
                f"- tags: {', '.join(f'`{tag}`' for tag in revision.tags) or '_(none)_'}",
                f"- source: `{revision.source_level.value}` confidence={revision.confidence}",
            ]
        )
        for index, evidence in enumerate(revision.evidence, start=1):
            excerpt = normalize_text(evidence.excerpt)[:_EXCERPT_LIMIT]
            lines.append(
                f"- evidence {index}: kind=`{evidence.kind.value}` ref=`{evidence.ref}` "
                f"excerpt={excerpt or '_(none)_'}"
            )
        if revision.supersedes:
            lines.append(f"- supersedes: {', '.join(f'`{rid}`' for rid in revision.supersedes)}")
        if revision.replacement_id:
            lines.append(f"- replacement: `{revision.replacement_id}`")
        if revision.blocked_by:
            lines.append(f"- blocked by: {', '.join(f'`{rid}`' for rid in revision.blocked_by)}")
    return _reply(ctx, "\n".join(lines))


# ---------------------------------------------------------------------------
# Mutating commands (actor=user, reason recorded in the transaction)
# ---------------------------------------------------------------------------


@_requires_stack
async def cmd_memory_promote(ctx: CommandContext) -> OutboundMessage:
    """Promote a candidate; a same-slot conflict requires --replace <active-id>."""
    store, repository, lifecycle = _stack(ctx)
    allowed = _allowed_scopes(ctx, store)
    parts, usage = _split_args_or_usage(ctx, "/memory-promote <id> [--replace <active-id>]")
    if usage is not None:
        return usage
    replace_id: str | None = None
    if "--replace" in parts:
        index = parts.index("--replace")
        if index + 1 >= len(parts):
            return _usage(ctx, "/memory-promote <id> [--replace <active-id>]")
        replace_id = parts[index + 1]
        parts = parts[:index] + parts[index + 2 :]
    if len(parts) != 1:
        return _usage(ctx, "/memory-promote <id> [--replace <active-id>]")
    memory_id = parts[0]
    if _visible_record_by_id(repository, memory_id, allowed) is None:
        return _not_found_reply(ctx, memory_id)
    if replace_id is not None and _visible_record_by_id(repository, replace_id, allowed) is None:
        return _not_found_reply(ctx, replace_id)
    try:
        result = lifecycle.promote(
            memory_id,
            actor=ActorKind.USER,
            reason="user:/memory-promote",
            replace_id=replace_id,
        )
    except MemoryLifecycleError as exc:
        return _reply(ctx, str(exc))
    store._export_audit_pending()
    return _reply(ctx, f"Promoted `{result.candidate_id}` -> `{result.final_status.value}`.")


@_requires_stack
async def cmd_memory_revoke(ctx: CommandContext) -> OutboundMessage:
    """Revoke a candidate/active record; the reason is required."""
    store, repository, lifecycle = _stack(ctx)
    allowed = _allowed_scopes(ctx, store)
    parts, usage = _split_args_or_usage(ctx, "/memory-revoke <id> <reason>")
    if usage is not None:
        return usage
    if len(parts) < 2:
        return _usage(ctx, "/memory-revoke <id> <reason>")
    memory_id, reason = parts[0], " ".join(parts[1:])
    if _visible_record_by_id(repository, memory_id, allowed) is None:
        return _not_found_reply(ctx, memory_id)
    try:
        revoked = lifecycle.revoke(memory_id, reason=f"user:{reason}")
    except MemoryLifecycleError as exc:
        return _reply(ctx, str(exc))
    store._export_audit_pending()
    return _reply(ctx, f"Revoked `{revoked.id}` (status: `{revoked.status.value}`).")


@_requires_stack
async def cmd_memory_correct(ctx: CommandContext) -> OutboundMessage:
    """Create an explicit correction candidate, then apply the atom rule."""
    store, _, lifecycle = _stack(ctx)
    parts = [part.strip() for part in ctx.args.split("|")]
    if len(parts) != 3 or any(not normalize_text(part) for part in parts):
        return _usage(ctx, "/memory-correct <subject>|<slot>|<statement>")
    subject, slot, statement = (normalize_text(part) for part in parts)
    now = datetime.now(timezone.utc)
    message_id = normalize_text(str((ctx.msg.metadata or {}).get("message_id") or ""))
    if not message_id:
        identity = f"{ctx.msg.session_key}|{ctx.msg.timestamp.isoformat()}|{ctx.msg.content}"
        message_id = "local-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    evidence_ref = f"command:{message_id}"
    evidence = EvidenceRef(
        kind=EvidenceKind.USER_MESSAGE,
        ref=evidence_ref,
        excerpt=statement[:_EXCERPT_LIMIT],
    )
    proposal = CandidateProposal(
        proposal_index=0,
        kind=MemoryKind.FACT,
        scope_hint=ScopeKind.PROJECT,
        subject=subject,
        slot=slot,
        statement=statement,
        tags=("project.fact",),
        confidence=1.0,
        importance=4,
        evidence_refs=("src:0",),
        speech_act=SourceLevel.EXPLICIT_CORRECTION,
    )
    context = IngestContext(
        actor=ActorKind.USER,
        reason="user:/memory-correct",
        source_batch=evidence_ref,
        scope=MemoryScope(kind=ScopeKind.PROJECT, key=store.project_scope_key),
        evidence_catalog={"src:0": evidence},
        now=now,
    )
    try:
        result = lifecycle.ingest(proposal, context)  # type: ignore[union-attr]
    except MemoryError as exc:
        return _reply(ctx, str(exc))
    store._export_audit_pending()
    return _reply(ctx, f"Corrected `{result.candidate_id}` -> `{result.final_status.value}`.")


# ---------------------------------------------------------------------------
# Mutating commands (actor=user, reason recorded in the transaction)
# ---------------------------------------------------------------------------


def register_memory_commands(router: CommandRouter) -> None:
    """Register the governed structured memory command set."""
    router.exact("/memory-status", cmd_memory_status)
    router.exact("/memory-log", cmd_memory_log)
    router.prefix("/memory-log ", cmd_memory_log)
    router.exact("/memory-list", cmd_memory_list)
    router.prefix("/memory-list ", cmd_memory_list)
    router.exact("/memory-show", cmd_memory_show)
    router.prefix("/memory-show ", cmd_memory_show)
    router.exact("/memory-promote", cmd_memory_promote)
    router.prefix("/memory-promote ", cmd_memory_promote)
    router.exact("/memory-revoke", cmd_memory_revoke)
    router.prefix("/memory-revoke ", cmd_memory_revoke)
    router.exact("/memory-correct", cmd_memory_correct)
    router.prefix("/memory-correct ", cmd_memory_correct)
    router.exact("/memory-backup", cmd_memory_backup)
    router.exact("/memory-restore", cmd_memory_restore)
    router.prefix("/memory-restore ", cmd_memory_restore)
    router.exact("/memory-export-audit", cmd_memory_export_audit)
    router.prefix("/memory-export-audit ", cmd_memory_export_audit)
