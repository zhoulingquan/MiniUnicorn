"""Slash commands for governed structured memory (C2 spec §15).

All mutating commands use actor=user and record a reason in the transaction.
Evidence excerpts are truncated to 200 characters in every response.
"""

from __future__ import annotations

import hashlib
import shlex
from datetime import datetime, timezone

from miniunicorn.agent.memory_lifecycle import IngestContext, MemoryLifecycleError
from miniunicorn.agent.memory_migration import load_migration_state
from miniunicorn.agent.memory_models import (
    ActorKind,
    CandidateProposal,
    EvidenceKind,
    EvidenceRef,
    MemoryError,
    MemoryKind,
    MemoryScope,
    MemoryStatus,
    ScopeKind,
    SourceLevel,
    normalize_text,
)
from miniunicorn.bus.events import OutboundMessage
from miniunicorn.command.router import CommandContext, CommandRouter

_MAX_LIST_ITEMS = 20
_EXCERPT_LIMIT = 200
_STATEMENT_DISPLAY_LIMIT = 100

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


def _stack(ctx: CommandContext, *, allow_legacy: bool = False):
    """Return (store, repository, lifecycle); None repository when not structured."""
    loop = ctx.loop
    if loop is None:
        raise MemoryError("memory commands require an agent loop")
    store = loop.context.memory
    if store.structured_repository is None:
        if allow_legacy:
            store._structured_stack_or_build()
        else:
            raise MemoryError("structured memory is not active (mode: legacy)")
    return store, store.structured_repository, store.structured_lifecycle


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
    """Show mode, health, status counts, migration state and last write error."""
    store, repository, _ = _stack(ctx)
    counts = {s.value: len(repository.current_records(s)) for s in MemoryStatus}  # type: ignore[union-attr]
    state = load_migration_state(store.workspace)
    migration = (
        f"completed at `{state.completed_at.isoformat()}`"
        if state.completed_at is not None
        else "pending (run `/memory-migrate --apply`)"
    )
    health = repository.health  # type: ignore[union-attr]
    lines = [
        "## Memory status",
        f"- Mode: `{store.structured_config.mode if store.structured_config else 'legacy'}`",
        f"- Health: `{health.state}` (last valid journal line: `{health.last_valid_line}`)",
        (
            f"- Records: candidate={counts['candidate']} active={counts['active']} "
            f"superseded={counts['superseded']} revoked={counts['revoked']} expired={counts['expired']}"
        ),
        f"- Migration: {migration}",
    ]
    if health.error_message:
        lines.append(f"- Last write error: `{health.error_message}`")
    return _reply(ctx, "\n".join(lines))


@_requires_stack
async def cmd_memory_list(ctx: CommandContext) -> OutboundMessage:
    """List records, newest status first, at most 20."""
    _, repository, _ = _stack(ctx)
    status_arg = ctx.args.strip()
    if status_arg:
        if status_arg not in _LIST_STATUSES:
            return _usage(ctx, "/memory-list [candidate|active|superseded|revoked|expired]")
        status = MemoryStatus(status_arg)
    else:
        status = None
    records = repository.current_records(status)  # type: ignore[union-attr]
    total = len(records)
    records = records[: _MAX_LIST_ITEMS]
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
    _, repository, _ = _stack(ctx)
    parts = shlex.split(ctx.args)
    if len(parts) != 1:
        return _usage(ctx, "/memory-show <id>")
    memory_id = parts[0]
    record = repository.get(memory_id)  # type: ignore[union-attr]
    if record is None:
        return _reply(ctx, f"No memory record with id `{memory_id}`.")
    lines = [f"## Memory {memory_id}"]
    for revision in repository.revisions(memory_id):  # type: ignore[union-attr]
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
    _, _, lifecycle = _stack(ctx)
    parts = shlex.split(ctx.args)
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
    try:
        result = lifecycle.promote(
            memory_id,
            actor=ActorKind.USER,
            reason="user:/memory-promote",
            replace_id=replace_id,
        )
    except MemoryLifecycleError as exc:
        return _reply(ctx, str(exc))
    return _reply(ctx, f"Promoted `{result.candidate_id}` -> `{result.final_status.value}`.")


@_requires_stack
async def cmd_memory_revoke(ctx: CommandContext) -> OutboundMessage:
    """Revoke a candidate/active record; the reason is required."""
    _, _, lifecycle = _stack(ctx)
    parts = shlex.split(ctx.args)
    if len(parts) < 2:
        return _usage(ctx, "/memory-revoke <id> <reason>")
    memory_id, reason = parts[0], " ".join(parts[1:])
    try:
        revoked = lifecycle.revoke(memory_id, reason=f"user:{reason}")
    except MemoryLifecycleError as exc:
        return _reply(ctx, str(exc))
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
    return _reply(ctx, f"Corrected `{result.candidate_id}` -> `{result.final_status.value}`.")


# ---------------------------------------------------------------------------
# Migration (works in any mode)
# ---------------------------------------------------------------------------


async def cmd_memory_migrate(ctx: CommandContext) -> OutboundMessage:
    """Run the legacy -> structured migration (dry-run by default)."""
    store, _, _ = _stack(ctx, allow_legacy=True)
    arg = ctx.args.strip()
    try:
        if arg in ("", "--dry-run"):
            report = store.run_migration(dry_run=True)
            lines = [
                "## Migration dry-run",
                f"- Scanned: {report.scanned} importable: {report.imported} "
                f"already-migrated: {report.skipped} failed: {len(report.failed)}",
                "_Run `/memory-migrate --apply` to import._",
            ]
        elif arg == "--apply":
            report = store.run_migration()
            lines = [
                "## Migration applied",
                f"- Imported: {report.imported} skipped: {report.skipped} failed: {len(report.failed)}",
                f"- completed_at: {report.completed_at.isoformat() if report.completed_at else 'not set'}",
            ]
        else:
            return _usage(ctx, "/memory-migrate [--dry-run|--apply]")
    except MemoryError as exc:
        return _reply(ctx, str(exc))
    for issue in report.issues[:5]:
        lines.append(f"- issue: `{issue.relative_path}` {issue.locator}: {issue.reason}")
    if len(report.issues) > 5:
        lines.append(f"_... {len(report.issues) - 5} more issues_")
    for failure in report.failed[:5]:
        lines.append(f"- failed: `{failure.item.relative_path}` {failure.item.locator}: {failure.error}")
    if len(report.failed) > 5:
        lines.append(f"_... {len(report.failed) - 5} more failures_")
    if not arg:
        lines.append("_Run `/memory-migrate --apply` to import._")
    return _reply(ctx, "\n".join(lines))


def register_memory_commands(router: CommandRouter) -> None:
    """Register the governed structured memory command set."""
    router.exact("/memory-status", cmd_memory_status)
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
    router.exact("/memory-migrate", cmd_memory_migrate)
    router.prefix("/memory-migrate ", cmd_memory_migrate)
