"""Dream: offline knowledge distillation into structured memory (extracted from memory.py)."""

from __future__ import annotations

import hashlib
import re
from dataclasses import replace as dataclasses_replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from loguru import logger

from miniunicorn.agent.call_ledger import CallPurpose, call_purpose
from miniunicorn.bus.events import session_key_base
from miniunicorn.utils.helpers import (
    estimate_message_tokens,
)
from miniunicorn.utils.prompt_templates import render_template

if TYPE_CHECKING:
    from miniunicorn.agent.memory_models import MemoryStatus
    from miniunicorn.agent.memory_store import MemoryStore
    from miniunicorn.providers.base import LLMProvider


def _parse_datetime_loose(value: str | None) -> datetime | None:
    """Parse a history/reflection timestamp into an aware datetime.

    Naive timestamps are interpreted as local time so the result always
    carries a timezone (required by ``EvidenceRef.observed_at``).
    """
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.astimezone()
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.astimezone()
    except ValueError:
        return None


def reflection_evidence_id(entry: Mapping[str, Any]) -> str | None:
    """Return the governed evidence ID, or ``None`` for an invalid entry."""
    raw = str(entry.get("reflection_id") or "")
    if re.fullmatch(r"rfl_[0-9a-f]{32}", raw):
        return raw
    return None


def _dream_source_batch(evidence_refs: Iterable[str]) -> str:
    """Derive the Dream source batch from the actual evidence ref set.

    The same input retried yields the same batch id; a different evidence set
    yields a different id.
    """
    canonical = "\n".join(sorted(set(evidence_refs)))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return f"dream:{digest}"


def count_pending_dream_entries(store: "MemoryStore") -> int:
    """Count cursor-visible history and reflection rows without mutating them."""
    history = store.read_unprocessed_history(since_cursor=store.get_last_dream_cursor())
    reflections = store.read_unprocessed_reflections(
        since_cursor=store.get_last_reflections_cursor()
    )
    return len(history) + len(reflections)


# ---------------------------------------------------------------------------
# Dream — heavyweight cron-scheduled memory consolidation
# ---------------------------------------------------------------------------


class Dream:
    """Extract journal-backed memory proposals from history and reflections."""

    _HISTORY_ENTRY_PREVIEW_MAX_CHARS = 4_000
    _REFLECTION_ENTRY_PREVIEW_MAX_CHARS = 1_000
    _MIN_EVIDENCE_PREVIEW_CHARS = 128
    _EVIDENCE_EXCERPT_MAX_CHARS = 1_000
    _SUMMARY_MAX_RECORDS = 40
    _SUMMARY_RECORD_MAX_CHARS = 500
    _SUMMARY_MAX_CHARS = 8_000
    _PROMPT_SAFETY_TOKENS = 1_024

    def __init__(
        self,
        store: MemoryStore,
        provider: LLMProvider,
        model: str,
        max_batch_size: int = 20,
        context_window_tokens: int | None = None,
        max_completion_tokens: int | None = None,
    ):
        self.store = store
        self.provider = provider
        self.model = model
        self.max_batch_size = max_batch_size
        self.context_window_tokens = context_window_tokens
        provider_max_tokens = getattr(getattr(provider, "generation", None), "max_tokens", None)
        self.max_completion_tokens = (
            max_completion_tokens
            if max_completion_tokens is not None
            else provider_max_tokens
            if isinstance(provider_max_tokens, int)
            else 4_096
        )

    def set_provider(
        self,
        provider: LLMProvider,
        model: str,
        context_window_tokens: int | None = None,
        max_completion_tokens: int | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        if context_window_tokens is not None:
            self.context_window_tokens = context_window_tokens
        provider_max_tokens = getattr(getattr(provider, "generation", None), "max_tokens", None)
        resolved_max_tokens = (
            max_completion_tokens
            if max_completion_tokens is not None
            else provider_max_tokens
            if isinstance(provider_max_tokens, int)
            else None
        )
        if resolved_max_tokens is not None:
            self.max_completion_tokens = resolved_max_tokens

    # -- main entry ----------------------------------------------------------

    async def run(self) -> bool:
        """Process unprocessed history entries. Returns True if work was done."""
        return await self._run_structured_batch()

    # -- strict extraction -> lifecycle candidates --------------------------

    @staticmethod
    def _partition_identity(entry: Mapping[str, Any]) -> tuple[str | None, str | None]:
        raw_session = entry.get("session_key")
        session_key = session_key_base(str(raw_session)) if raw_session else None
        raw_user = entry.get("user_key")
        user_key = str(raw_user) if raw_user else None
        return session_key, user_key

    @staticmethod
    def _entry_timestamp(entry: Mapping[str, Any]) -> datetime:
        return _parse_datetime_loose(entry.get("timestamp")) or datetime.max.replace(
            tzinfo=timezone.utc
        )

    def _structured_summary(
        self,
        repository: Any,
        status: MemoryStatus,
        allowed_scopes: set[Any],
    ) -> str:
        lines: list[str] = []
        used = 0
        for record in repository.current_records(status):
            if record.scope not in allowed_scopes or len(lines) >= self._SUMMARY_MAX_RECORDS:
                continue
            line = f"- [{record.id}] {record.statement} (tags: {', '.join(record.tags)})"
            line = line[: self._SUMMARY_RECORD_MAX_CHARS]
            if used + len(line) > self._SUMMARY_MAX_CHARS:
                break
            lines.append(line)
            used += len(line) + 1
        return "\n".join(lines)

    @staticmethod
    def _history_prompt_line(entry: Mapping[str, Any], preview_chars: int) -> str:
        content = str(entry.get("content", ""))[:preview_chars]
        return f"[history:{entry['cursor']} | {entry.get('timestamp', '')}] {content}"

    @staticmethod
    def _reflection_prompt_line(entry: Mapping[str, Any], preview_chars: int) -> str:
        reflection_id = reflection_evidence_id(entry)
        if reflection_id is None:
            raise ValueError("invalid reflection id")
        content = str(entry.get("lesson") or entry.get("reflection", ""))[:preview_chars]
        return (
            f"[reflection:{reflection_id} | {entry.get('timestamp', '')}] "
            f"({entry.get('trigger', 'unknown')}) {content}"
        )

    def _render_user_prompt(
        self,
        repository: Any,
        history: list[dict[str, Any]],
        reflections: list[dict[str, Any]],
        allowed_scopes: set[Any],
        *,
        history_preview: int,
        reflection_preview: int,
        include_summaries: bool,
    ) -> str:
        from miniunicorn.agent.memory_models import MemoryStatus

        history_lines = [self._history_prompt_line(entry, history_preview) for entry in history]
        reflection_lines = [
            self._reflection_prompt_line(entry, reflection_preview) for entry in reflections
        ]
        history_text = "\n".join(history_lines) if history_lines else "(no new history)"
        reflection_text = "\n".join(reflection_lines) if reflection_lines else "(none)"
        active = (
            self._structured_summary(repository, MemoryStatus.ACTIVE, allowed_scopes)
            if include_summaries
            else ""
        )
        candidates = (
            self._structured_summary(repository, MemoryStatus.CANDIDATE, allowed_scopes)
            if include_summaries
            else ""
        )
        return (
            "## Conversation History\n"
            f"{history_text}\n\n"
            "## Recent Reflections (Lessons Learned)\n"
            f"{reflection_text}\n\n"
            "## Current Active Facts\n"
            f"{active or '(none)'}\n\n"
            "## Current Candidates\n"
            f"{candidates or '(none)'}"
        )

    def _bounded_user_prompt(
        self,
        repository: Any,
        history: list[dict[str, Any]],
        reflections: list[dict[str, Any]],
        allowed_scopes: set[Any],
    ) -> str | None:
        def render(history_preview: int, reflection_preview: int, summaries: bool) -> str:
            return self._render_user_prompt(
                repository,
                history,
                reflections,
                allowed_scopes,
                history_preview=history_preview,
                reflection_preview=reflection_preview,
                include_summaries=summaries,
            )

        prompt = render(
            self._HISTORY_ENTRY_PREVIEW_MAX_CHARS,
            self._REFLECTION_ENTRY_PREVIEW_MAX_CHARS,
            True,
        )
        if self.context_window_tokens is None:
            return prompt
        budget = (
            self.context_window_tokens - self.max_completion_tokens - self._PROMPT_SAFETY_TOKENS
        )
        if budget <= 0:
            return None
        if estimate_message_tokens({"role": "user", "content": prompt}) <= budget:
            return prompt

        prompt = render(
            self._HISTORY_ENTRY_PREVIEW_MAX_CHARS,
            self._REFLECTION_ENTRY_PREVIEW_MAX_CHARS,
            False,
        )
        if estimate_message_tokens({"role": "user", "content": prompt}) <= budget:
            return prompt

        low, high = self._MIN_EVIDENCE_PREVIEW_CHARS, self._HISTORY_ENTRY_PREVIEW_MAX_CHARS
        best = render(
            self._MIN_EVIDENCE_PREVIEW_CHARS,
            self._MIN_EVIDENCE_PREVIEW_CHARS,
            False,
        )
        if estimate_message_tokens({"role": "user", "content": best}) > budget:
            return None
        while low <= high:
            mid = (low + high) // 2
            candidate = render(mid, min(mid, self._REFLECTION_ENTRY_PREVIEW_MAX_CHARS), False)
            if estimate_message_tokens({"role": "user", "content": candidate}) <= budget:
                best = candidate
                low = mid + 1
            else:
                high = mid - 1
        if estimate_message_tokens({"role": "user", "content": best}) <= budget:
            return best
        return None

    def _fit_bounded_batch(
        self,
        repository: Any,
        history: list[dict[str, Any]],
        reflections: list[dict[str, Any]],
        allowed_scopes: set[Any],
        *,
        primary_source: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str] | None:
        """Return the largest selected prefix whose complete prompt fits."""
        fitted_history = list(history)
        fitted_reflections = list(reflections)
        while fitted_history or fitted_reflections:
            prompt = self._bounded_user_prompt(
                repository,
                fitted_history,
                fitted_reflections,
                allowed_scopes,
            )
            if prompt is not None:
                return fitted_history, fitted_reflections, prompt

            # Selection fills the primary source first and the secondary source
            # second. Removing in reverse preserves the exact selected prefix.
            if primary_source == "history":
                if fitted_reflections:
                    fitted_reflections.pop()
                else:
                    fitted_history.pop()
            elif fitted_history:
                fitted_history.pop()
            else:
                fitted_reflections.pop()
        return None

    async def _run_structured_batch(self) -> bool:
        """Extract proposals and ingest them through the lifecycle,
        advance cursors only on full success (fail-closed, idempotent retry).

        On any provider/parse/ingest error, both cursors stay put and the next
        cycle retries the same batch; re-ingest is safe because lifecycle
        deduplicates by source_batch + content hash.
        """
        from miniunicorn.agent.memory_extraction import (
            MemoryExtractionError,
            parse_extraction_batch,
        )
        from miniunicorn.agent.memory_lifecycle import IngestContext
        from miniunicorn.agent.memory_models import (
            ActorKind,
            EvidenceKind,
            EvidenceRef,
            MemoryScope,
            ScopeKind,
        )

        store = self.store
        repository = store.structured_repository
        lifecycle = store.structured_lifecycle
        if repository is None or lifecycle is None:
            logger.warning("memory_dream_batch_failed code=structured_stack_missing")
            return False

        history_entries = store.read_unprocessed_history(since_cursor=store.get_last_dream_cursor())
        reflection_entries = store.read_unprocessed_reflections(
            since_cursor=store.get_last_reflections_cursor()
        )
        if not history_entries and not reflection_entries:
            return False

        first_history = history_entries[0] if history_entries else None
        first_reflection = next(
            (entry for entry in reflection_entries if reflection_evidence_id(entry) is not None),
            None,
        )
        if first_history is None and first_reflection is None:
            store.set_last_reflections_cursor(
                max(entry.get("_line", 0) for entry in reflection_entries)
            )
            store.run_memory_hygiene()
            store._export_audit_pending()
            return True

        if first_reflection is None or (
            first_history is not None
            and self._entry_timestamp(first_history) <= self._entry_timestamp(first_reflection)
        ):
            primary_source = "history"
            primary_entry = first_history
        else:
            primary_source = "reflection"
            primary_entry = first_reflection
        assert primary_entry is not None
        partition = self._partition_identity(primary_entry)

        selected_history: list[dict[str, Any]] = []
        selected_reflections: list[dict[str, Any]] = []
        reflection_advance_line = 0

        def take_history() -> None:
            for entry in history_entries:
                if len(selected_history) + len(selected_reflections) >= self.max_batch_size:
                    break
                if self._partition_identity(entry) != partition:
                    break
                selected_history.append(entry)

        def take_reflections() -> None:
            nonlocal reflection_advance_line
            for entry in reflection_entries:
                if len(selected_history) + len(selected_reflections) >= self.max_batch_size:
                    break
                if reflection_evidence_id(entry) is None:
                    reflection_advance_line = max(
                        reflection_advance_line, int(entry.get("_line", 0))
                    )
                    logger.warning("memory_reflection_skipped code=invalid_reflection_id")
                    continue
                if self._partition_identity(entry) != partition:
                    break
                selected_reflections.append(entry)
                reflection_advance_line = max(reflection_advance_line, int(entry.get("_line", 0)))

        if primary_source == "history":
            take_history()
            take_reflections()
        else:
            take_reflections()
            take_history()

        if not selected_history and not selected_reflections:
            if reflection_advance_line:
                store.set_last_reflections_cursor(reflection_advance_line)
                store.run_memory_hygiene()
                store._export_audit_pending()
                return True
            return False

        scope_by_hint = {
            ScopeKind.PROJECT: MemoryScope(kind=ScopeKind.PROJECT, key=store.project_scope_key),
            ScopeKind.SHARED: MemoryScope(kind=ScopeKind.SHARED, key="shared:*"),
        }
        session_key, user_key = partition
        if session_key is not None:
            scope_by_hint[ScopeKind.SESSION] = MemoryScope(
                kind=ScopeKind.SESSION, key=f"session:{session_key}"
            )
        if user_key is not None:
            scope_by_hint[ScopeKind.USER] = MemoryScope(kind=ScopeKind.USER, key=user_key)

        fitted = self._fit_bounded_batch(
            repository,
            selected_history,
            selected_reflections,
            set(scope_by_hint.values()),
            primary_source=primary_source,
        )
        if fitted is None:
            logger.warning("memory_dream_batch_deferred code=prompt_budget_too_small")
            return False
        selected_history, selected_reflections, user_prompt = fitted

        # A pre-fit scan may have crossed reflections that were later removed
        # from the batch. Recompute the physical cursor as a strict prefix so
        # no valid, unsent reflection can be pruned or skipped.
        selected_reflection_lines = {int(entry.get("_line", 0)) for entry in selected_reflections}
        reflection_advance_line = 0
        for entry in reflection_entries:
            line = int(entry.get("_line", 0))
            if reflection_evidence_id(entry) is None:
                reflection_advance_line = max(reflection_advance_line, line)
                continue
            if line not in selected_reflection_lines:
                break
            reflection_advance_line = max(reflection_advance_line, line)

        evidence_catalog: dict[str, EvidenceRef] = {}
        for entry in selected_history:
            ref = f"history:{entry['cursor']}"
            content = str(entry.get("content", ""))
            evidence_catalog[ref] = EvidenceRef(
                kind=EvidenceKind.HISTORY,
                ref=ref,
                excerpt=content[: self._EVIDENCE_EXCERPT_MAX_CHARS],
                observed_at=_parse_datetime_loose(entry.get("timestamp")),
            )
        for entry in selected_reflections:
            reflection_id = reflection_evidence_id(entry)
            assert reflection_id is not None
            content = str(entry.get("lesson") or entry.get("reflection", ""))
            ref = f"reflection:{reflection_id}"
            evidence_catalog[ref] = EvidenceRef(
                kind=EvidenceKind.REFLECTION,
                ref=ref,
                excerpt=content[: self._EVIDENCE_EXCERPT_MAX_CHARS],
                observed_at=_parse_datetime_loose(entry.get("timestamp")),
            )

        system_prompt = render_template(
            "agent/dream_phase1.md",
            strip=True,
            allowed_scope_hints=", ".join(kind.value for kind in scope_by_hint),
        )
        try:
            async with call_purpose(CallPurpose.MEMORY):
                response = await self.provider.chat_with_retry(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    tools=None,
                    tool_choice=None,
                )
        except Exception:
            logger.exception("memory_dream_batch_failed code=phase1_provider_error")
            return False

        raw = response.content or ""
        try:
            extracted = parse_extraction_batch(
                raw,
                evidence_catalog,
                repository.tag_catalog,
                allowed_scope_hints=set(scope_by_hint),
            )
        except MemoryExtractionError as exc:
            logger.warning("memory_dream_batch_failed code=extraction_error error={}", exc)
            return False

        context = IngestContext(
            actor=ActorKind.DREAM,
            reason="dream batch",
            source_batch=_dream_source_batch(evidence_catalog.keys()),
            scope=scope_by_hint[ScopeKind.PROJECT],
            evidence_catalog=evidence_catalog,
            now=datetime.now(timezone.utc),
        )
        results = []
        try:
            for proposal in sorted(extracted.proposals, key=lambda p: p.proposal_index):
                ctx = dataclasses_replace(context, scope=scope_by_hint[proposal.scope_hint])
                results.append(lifecycle.ingest(proposal, ctx))
        except Exception as exc:
            logger.warning(
                "memory_dream_batch_failed code={} error={}",
                exc.__class__.__name__,
                str(exc),
            )
            return False
        if repository.health.state != "healthy":
            logger.warning("memory_dream_batch_failed code=repository_degraded")
            return False

        if selected_history:
            store.set_last_dream_cursor(selected_history[-1]["cursor"])
        if reflection_advance_line:
            store.set_last_reflections_cursor(reflection_advance_line)
        for result in results:
            logger.info(
                "memory_dream_candidate id={} status={} reason={}",
                result.candidate_id,
                result.final_status.value,
                result.reason_code,
            )
        try:
            if store.git.is_initialized():
                last_entry = selected_history[-1] if selected_history else selected_reflections[-1]
                ts = last_entry.get("timestamp") or datetime.now().strftime("%Y-%m-%d %H:%M")
                sha = store.git.auto_commit(f"dream structured: {ts}, {len(results)} proposal(s)")
                if sha:
                    logger.info("Dream commit: {}", sha)
        except Exception:
            logger.debug("Dream git commit skipped", exc_info=True)
        store.compact_history()
        try:
            store.run_memory_hygiene()
        except Exception:
            logger.debug("File hygiene failed", exc_info=True)
        store._export_audit_pending()
        return True
