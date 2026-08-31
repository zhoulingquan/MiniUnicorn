"""Memory system: pure file I/O store, lightweight Consolidator, and Dream processor."""

from __future__ import annotations

import asyncio
import hashlib
import re
import weakref
from dataclasses import replace as dataclasses_replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping

import tiktoken
from loguru import logger

from miniunicorn.agent.call_ledger import CallPurpose, call_purpose
from miniunicorn.bus.events import session_key_base
from miniunicorn.session.manager import Session
from miniunicorn.utils.helpers import (
    estimate_message_tokens,
    estimate_prompt_tokens_chain,
    find_legal_message_start,
    truncate_text,
)
from miniunicorn.utils.prompt_templates import render_template

if TYPE_CHECKING:
    from miniunicorn.agent.memory_models import MemoryStatus
    from miniunicorn.providers.base import LLMProvider
    from miniunicorn.session.manager import SessionManager
from miniunicorn.agent.memory_store import (  # noqa: F401
    _HISTORY_ENTRY_HARD_CAP,
    _RAW_ARCHIVE_MAX_CHARS,
    MemoryStore,
    WorkspaceMemoryRegistry,
)

# ---------------------------------------------------------------------------
# MemoryStore — pure file I/O layer
# ---------------------------------------------------------------------------


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
# Consolidator — lightweight token-budget triggered consolidation
# ---------------------------------------------------------------------------


_ARCHIVE_SUMMARY_MAX_CHARS = 8_000  # LLM-produced consolidation summary


class Consolidator:
    """Lightweight consolidation: summarizes evicted messages into history.jsonl."""

    _MAX_CONSOLIDATION_ROUNDS = 5

    _SAFETY_BUFFER = 1024  # extra headroom for tokenizer estimation drift

    # 每 N 次归档后触发一次 memory hygiene（结构化 JSONL 文件截断）。
    # 这样即使 Dream 关闭，append-only 记忆文件也能定期按上限清理。
    # 20 次 ≈ 每隔数十轮对话清理一次，开销可忽略。
    _HYGIENE_THROTTLE = 20

    # 提前 checkpoint 触发比例（借鉴 MiMo Code 的"提前提取"思想）。
    # 旧逻辑：estimated >= budget（100%）才触发，此时模型能力已因
    # "lost in the middle" 衰减，压缩质量下降。
    # 新行为：estimated >= budget * checkpoint_ratio（默认 70%）即触发，
    # 在模型仍有充足注意力时完成归档。rebuild target 仍由 consolidation_ratio
    # 控制（默认压到 50%），循环逻辑不变。
    _CHECKPOINT_RATIO = 0.7

    def __init__(
        self,
        store: MemoryStore,
        provider: LLMProvider,
        model: str,
        sessions: SessionManager,
        context_window_tokens: int,
        build_messages: Callable[..., list[dict[str, Any]]],
        get_tool_definitions: Callable[[], list[dict[str, Any]]],
        max_completion_tokens: int = 4096,
        consolidation_ratio: float = 0.5,
        checkpoint_ratio: float | None = None,
    ):
        self.store = store
        self.provider = provider
        self.model = model
        self.sessions = sessions
        self.context_window_tokens = context_window_tokens
        self.max_completion_tokens = max_completion_tokens
        self.consolidation_ratio = consolidation_ratio
        # checkpoint_ratio=None 走类默认 _CHECKPOINT_RATIO；显式传入可覆盖。
        # 传 1.0 可恢复旧行为（只在 100% 时触发）。
        self.checkpoint_ratio = (
            checkpoint_ratio if checkpoint_ratio is not None else self._CHECKPOINT_RATIO
        )
        # 校验 ratio 范围，避免无效配置导致压缩/检查点逻辑异常
        assert 0 < consolidation_ratio <= 1.0, "consolidation_ratio 必须在 (0, 1.0]"
        assert 0 < self.checkpoint_ratio <= 1.0, "checkpoint_ratio 必须在 (0, 1.0]"
        self._build_messages = build_messages
        self._get_tool_definitions = get_tool_definitions
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        # 归档计数器：每 _HYGIENE_THROTTLE 次归档触发一次 memory hygiene。
        self._archive_count_since_hygiene = 0

    def set_provider(
        self,
        provider: LLMProvider,
        model: str,
        context_window_tokens: int,
    ) -> None:
        self.provider = provider
        self.model = model
        self.context_window_tokens = context_window_tokens
        self.max_completion_tokens = provider.generation.max_tokens

    def get_lock(self, session_key: str) -> asyncio.Lock:
        """Return the shared consolidation lock for one session."""
        return self._locks.setdefault(session_key, asyncio.Lock())

    def pick_consolidation_boundary(
        self,
        session: Session,
        tokens_to_remove: int,
    ) -> tuple[int, int] | None:
        """Pick a user-turn boundary that removes enough old prompt tokens."""
        start = session.last_consolidated
        if start >= len(session.messages) or tokens_to_remove <= 0:
            return None

        removed_tokens = 0
        last_boundary: tuple[int, int] | None = None
        for idx in range(start, len(session.messages)):
            message = session.messages[idx]
            if idx > start and message.get("role") == "user":
                last_boundary = (idx, removed_tokens)
                if removed_tokens >= tokens_to_remove:
                    return last_boundary
            removed_tokens += estimate_message_tokens(message)

        return last_boundary

    @staticmethod
    def _full_unconsolidated_history(
        session: Session,
        *,
        include_timestamps: bool = False,
    ) -> list[dict[str, Any]]:
        """Return the whole unconsolidated tail for consolidation decisions."""
        unconsolidated_count = len(session.messages) - session.last_consolidated
        if unconsolidated_count <= 0:
            return []
        return session.get_history(
            max_messages=unconsolidated_count,
            include_timestamps=include_timestamps,
        )

    @staticmethod
    def _replay_overflow_boundary(
        session: Session,
        replay_max_messages: int | None,
    ) -> int | None:
        if not replay_max_messages or replay_max_messages <= 0:
            return None
        tail = list(
            enumerate(session.messages[session.last_consolidated :], session.last_consolidated)
        )
        if len(tail) <= replay_max_messages:
            return None

        sliced = tail[-replay_max_messages:]
        for i, (_idx, message) in enumerate(sliced):
            if message.get("role") == "user":
                start = i
                if i > 0 and sliced[i - 1][1].get("_channel_delivery"):
                    start = i - 1
                sliced = sliced[start:]
                break

        legal_start = find_legal_message_start([message for _idx, message in sliced])
        if legal_start:
            sliced = sliced[legal_start:]
        if not sliced:
            return len(session.messages)

        first_visible_idx = sliced[0][0]
        if first_visible_idx <= session.last_consolidated:
            return None
        return first_visible_idx

    async def _consolidate_replay_overflow(
        self,
        session: Session,
        replay_max_messages: int | None,
    ) -> str | None:
        """Archive messages that would be hidden by the replay message window."""
        end_idx = self._replay_overflow_boundary(session, replay_max_messages)
        if end_idx is None:
            return None
        chunk = session.messages[session.last_consolidated : end_idx]
        if not chunk:
            return None
        logger.info(
            "Replay-window consolidation for {}: chunk={} msgs, replay_max={}",
            session.key,
            len(chunk),
            replay_max_messages,
        )
        summary = await self.archive(chunk)
        session.last_consolidated = end_idx
        self.sessions.save(session)
        return summary

    # 逐字切片保留的最近用户消息条数（借鉴 MiMo Code 的 rebuild 注入设计）。
    # Consolidator 生成的 summary 是 LLM 改写后的自由文本，可能偏离用户原意。
    # 保留最近 N 条用户消息原文，在 AutoCompact 注入时与 summary 拼接，
    # 让主 Agent 能直接看到用户最近的真实表述，防止 writer 误读意图。
    _VERBATIM_RECENT_USER_MSGS = 2

    def _extract_verbatim_recent(self, session: Session) -> list[str]:
        """提取最近 N 条用户消息的原文（用于防 summary 改写偏离）。

        从 session 当前消息尾部向前扫描，只取 role=user 的 content，
        跳过空内容和工具注入的 runtime context 块。
        """
        result: list[str] = []
        for msg in reversed(session.messages):
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not content:
                continue
            # content 可能是 str 或 list[block]；统一取文本
            if isinstance(content, list):
                text = " ".join(
                    b.get("text", "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            else:
                text = str(content)
            text = text.strip()
            if not text:
                continue
            # 跳过纯 runtime context 标记的消息（无实际用户输入）
            if text.startswith("[Runtime Context"):
                continue
            result.append(text)
            if len(result) >= self._VERBATIM_RECENT_USER_MSGS:
                break
        # 反转回时间顺序
        return list(reversed(result))

    def _persist_last_summary(self, session: Session, summary: str | None) -> None:
        if summary and summary != "(nothing)":
            session.metadata["_last_summary"] = {
                "text": summary,
                "last_active": session.updated_at.isoformat(),
                # 逐字切片：保留最近几条用户消息原文，防止 summary 改写偏离。
                # AutoCompact 注入时会拼接在 summary 之后，让主 Agent 能
                # 直接看到用户最近的真实表述。
                "verbatim_recent": self._extract_verbatim_recent(session),
            }
            self.sessions.save(session)

    def estimate_session_prompt_tokens(
        self,
        session: Session,
    ) -> tuple[int, str]:
        """Estimate prompt size from the full unconsolidated session tail."""
        history = self._full_unconsolidated_history(session, include_timestamps=True)
        channel, chat_id = session.key.split(":", 1) if ":" in session.key else (None, None)
        # Include archived summary in estimation so the budget accounts for it.
        meta = session.metadata.get("_last_summary")
        summary = (
            meta.get("text")
            if isinstance(meta, dict)
            else (meta if isinstance(meta, str) else None)
        )
        probe_messages = self._build_messages(
            history=history,
            current_message="[token-probe]",
            channel=channel,
            chat_id=chat_id,
            sender_id=None,
            session_summary=summary,
            session_metadata=session.metadata,
            workspace=self.store.workspace,
        )
        return estimate_prompt_tokens_chain(
            self.provider,
            self.model,
            probe_messages,
            self._get_tool_definitions(),
        )

    @property
    def _input_token_budget(self) -> int:
        """Available input token budget for consolidation LLM."""
        return self.context_window_tokens - self.max_completion_tokens - self._SAFETY_BUFFER

    def _truncate_to_token_budget(self, text: str) -> str:
        """Truncate text so it fits within the consolidation LLM's token budget."""
        budget = self._input_token_budget
        if budget <= 0:
            return truncate_text(text, _RAW_ARCHIVE_MAX_CHARS)
        try:
            enc = tiktoken.get_encoding("cl100k_base")
            tokens = enc.encode(text)
            if len(tokens) <= budget:
                return text
            return enc.decode(tokens[:budget]) + "\n... (truncated)"
        except Exception:
            return truncate_text(text, budget * 4)

    async def archive(self, messages: list[dict]) -> str | None:
        """Summarize messages via LLM and append to history.jsonl.

        Returns the summary text on success, None if nothing to archive.

        归档时会一并读取 notes.md（主 Agent 的 scratchpad 笔记），将其作为
        额外上下文喂给 LLM，让 summary 能整合主 Agent 主动记录的零散发现。
        归档成功后清空 notes.md，释放 scratchpad 空间（借鉴 MiMo Code 的
        notes.md 设计：主 Agent 唯一写入通道，Consolidator 定期路由+清空）。
        """
        if not messages:
            return None
        try:
            formatted = MemoryStore._format_messages(messages)
            # 读取主 Agent 的 scratchpad 笔记，附加到归档输入。
            # 这样 LLM 生成 summary 时能整合主 Agent 主动记录的发现，
            # 而不只是被动压缩对话历史。
            notes_content = self.store.read_notes()
            if notes_content:
                formatted = (
                    f"{formatted}\n\n## Agent Scratchpad Notes (from notes.md)\n{notes_content}"
                )
            formatted = self._truncate_to_token_budget(formatted)
            async with call_purpose(CallPurpose.COMPACT):
                response = await self.provider.chat_with_retry(
                    model=self.model,
                    messages=[
                        {
                            "role": "system",
                            "content": render_template(
                                "agent/consolidator_archive.md",
                                strip=True,
                            ),
                        },
                        {"role": "user", "content": formatted},
                    ],
                    tools=None,
                    tool_choice=None,
                )
            if response.finish_reason == "error":
                raise RuntimeError(f"LLM returned error: {response.content}")
            summary = response.content or "[no summary]"
            session_key, user_key = self.store._archive_identity(messages)
            self.store.append_history(
                summary,
                max_chars=_ARCHIVE_SUMMARY_MAX_CHARS,
                session_key=session_key,
                user_key=user_key,
            )
            # 归档成功后清空 notes.md：内容已被 LLM 整合进 summary，
            # 释放主 Agent 的 scratchpad 空间供下一轮使用。
            if notes_content:
                self.store.clear_notes()
            # 节流触发 memory hygiene：每 _HYGIENE_THROTTLE 次归档后清理一次
            # 文件截断，避免 Dream 关闭时 append-only 文件无限膨胀。
            self._archive_count_since_hygiene += 1
            if self._archive_count_since_hygiene >= self._HYGIENE_THROTTLE:
                self._archive_count_since_hygiene = 0
                try:
                    pruned = self.store.run_memory_hygiene()
                    if any(v > 0 for v in pruned.values()):
                        logger.debug("Consolidator throttled hygiene: {}", pruned)
                except Exception:
                    logger.debug("Consolidator hygiene failed", exc_info=True)
            return summary
        except Exception:
            logger.warning("Consolidation LLM call failed, raw-dumping to history")
            self.store.raw_archive(messages)
            return None

    @property
    def _checkpoint_threshold(self) -> int:
        """提前 checkpoint 触发阈值。

        低于此值时不触发归档；达到或超过时进入归档循环。
        默认为输入预算的 70%（_CHECKPOINT_RATIO），比旧行为（100%）
        更早介入，避免模型在高利用率下能力衰减时做关键压缩。
        """
        # 用 max(0, ...) 防御预算为负或 ratio 为负时产生负阈值
        return max(0, int(self._input_token_budget * self.checkpoint_ratio))

    async def maybe_consolidate_by_tokens(
        self,
        session: Session,
        *,
        replay_max_messages: int | None = None,
    ) -> None:
        """Loop: archive old messages until prompt fits within safe budget.

        The budget reserves space for completion tokens and a safety buffer
        so the LLM request never exceeds the context window.

        触发阈值由 ``checkpoint_ratio`` 控制（默认 0.7），即当估算 token
        达到输入预算的 70% 时就提前归档，而非等到 100% 满载。这是借鉴
        MiMo Code 的"提前提取"思想：模型在高利用率下能力衰减，不应
        在它压缩能力最差时让它做最关键的压缩。
        """
        if self.context_window_tokens <= 0:
            return

        lock = self.get_lock(session.key)
        async with lock:
            # Refresh session reference: AutoCompact may have replaced it.
            fresh = self.sessions.get_or_create(session.key)
            if fresh is not session:
                session = fresh
            if not session.messages:
                return

            budget = self._input_token_budget
            target = int(budget * self.consolidation_ratio)
            checkpoint_threshold = self._checkpoint_threshold
            last_summary = await self._consolidate_replay_overflow(
                session,
                replay_max_messages,
            )
            try:
                estimated, source = self.estimate_session_prompt_tokens(
                    session,
                )
            except Exception:
                logger.exception("Token estimation failed for {}", session.key)
                estimated, source = 0, "error"
            if estimated <= 0:
                self._persist_last_summary(session, last_summary)
                return
            if estimated < checkpoint_threshold:
                unconsolidated_count = len(session.messages) - session.last_consolidated
                logger.debug(
                    "Token consolidation idle {}: {}/{} (checkpoint@{}%, {}) msgs={}",
                    session.key,
                    estimated,
                    self.context_window_tokens,
                    int(self.checkpoint_ratio * 100),
                    source,
                    unconsolidated_count,
                )
                self._persist_last_summary(session, last_summary)
                return

            for round_num in range(self._MAX_CONSOLIDATION_ROUNDS):
                if estimated <= target:
                    break

                boundary = self.pick_consolidation_boundary(session, max(1, estimated - target))
                if boundary is None:
                    logger.debug(
                        "Token consolidation: no safe boundary for {} (round {})",
                        session.key,
                        round_num,
                    )
                    break

                end_idx = boundary[0]

                chunk = session.messages[session.last_consolidated : end_idx]
                if not chunk:
                    break

                logger.info(
                    "Token consolidation round {} for {}: {}/{} via {}, chunk={} msgs",
                    round_num,
                    session.key,
                    estimated,
                    self.context_window_tokens,
                    source,
                    len(chunk),
                )
                summary = await self.archive(chunk)
                # Advance the cursor either way: on success the chunk was
                # summarized; on failure archive() already raw-archived it as
                # a breadcrumb. Re-archiving the same chunk on the next call
                # would just emit duplicate [RAW] entries.
                if summary:
                    last_summary = summary
                session.last_consolidated = end_idx
                self.sessions.save(session)
                if not summary:
                    # LLM is degraded — stop hammering it this call;
                    # the next invocation can retry a fresh chunk.
                    break

                try:
                    estimated, source = self.estimate_session_prompt_tokens(
                        session,
                    )
                except Exception:
                    logger.exception("Token estimation failed for {}", session.key)
                    estimated, source = 0, "error"
                if estimated <= 0:
                    break

            # Persist the last summary to session metadata so it can be injected
            # into the runtime context on the next prepare_session() call, aligning
            # the summary injection strategy with AutoCompact._archive().
            self._persist_last_summary(session, last_summary)

    async def compact_idle_session(
        self,
        session_key: str,
        max_suffix: int = 8,
    ) -> str | None:
        """Hard-truncate an idle session under the consolidation lock.

        Used by AutoCompact so all session mutation goes through a single
        lock-protected path.  Returns the summary text on success, ``None``
        if the LLM failed (raw_archive fallback), or ``""`` if there was
        nothing to archive.
        """
        lock = self.get_lock(session_key)
        async with lock:
            self.sessions.invalidate(session_key)
            session = self.sessions.get_or_create(session_key)

            tail = list(session.messages[session.last_consolidated :])
            if not tail:
                session.updated_at = datetime.now()
                self.sessions.save(session)
                return ""

            probe = Session(
                key=session.key,
                messages=tail.copy(),
                created_at=session.created_at,
                updated_at=session.updated_at,
                metadata={},
                last_consolidated=0,
            )
            probe.retain_recent_legal_suffix(max_suffix)
            kept = probe.messages
            cut = len(tail) - len(kept)
            archive_msgs = tail[:cut]

            if not archive_msgs and not kept:
                session.updated_at = datetime.now()
                self.sessions.save(session)
                return ""

            last_active = session.updated_at
            summary: str | None = ""
            if archive_msgs:
                summary = await self.archive(archive_msgs)

            if summary and summary != "(nothing)":
                # 在清空 messages 前提取 verbatim_recent（基于当前 session.messages），
                # 这样保留的是归档前的最近用户消息原文。
                session.metadata["_last_summary"] = {
                    "text": summary,
                    "last_active": last_active.isoformat(),
                    "verbatim_recent": self._extract_verbatim_recent(session),
                }

            session.messages = kept
            session.last_consolidated = 0
            session.updated_at = datetime.now()
            self.sessions.save(session)

            if archive_msgs:
                logger.info(
                    "Idle-session compact for {}: archived={}, kept={}, summary={}",
                    session_key,
                    len(archive_msgs),
                    len(kept),
                    bool(summary),
                )

            return summary


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
