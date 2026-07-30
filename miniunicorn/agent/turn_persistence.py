"""Turn persistence and recovery collaborator.

Owns session history writes, multimodal sanitization, and subagent follow-up
dedup. The algorithms were moved here mechanically from :class:`AgentLoop`;
the loop retains thin delegates so existing monkeypatches continue to
intercept calls.

Design §29.4 splits this module's responsibilities without duplicating state:

- **Session transcript persistence** — ``persist_user_message_early``,
  ``save_turn``, ``assemble_outbound``, ``sanitize_persisted_blocks``, and
  ``persist_subagent_followup`` remain the transcript authority.
- **Durable checkpoint adapter** — implemented separately in
  :mod:`miniunicorn.runtime.durable_journal` (design §29.4). Used by the
  Worker Adapter.
- **SessionCommitter adapter** — implemented separately in
  :mod:`miniunicorn.runtime.session_committer` (design §17.7).

Legacy ``runtime_checkpoint`` / ``pending_user_turn`` session-metadata
writers and readers were removed in design Task 10. Durable tasks own
recovery through the Runtime Store state machine and
``TurnJournalPort.save_checkpoint()`` (design §6.22, §29.4).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Awaitable, Callable, Protocol

from loguru import logger

from miniunicorn.agent import context as agent_context
from miniunicorn.agent.context import ContextBuilder
from miniunicorn.agent.tools.message import MessageTool
from miniunicorn.bus.events import InboundMessage, OutboundMessage
from miniunicorn.utils.helpers import image_placeholder_text
from miniunicorn.utils.helpers import truncate_text as truncate_text_fn

if TYPE_CHECKING:
    from miniunicorn.agent.tools.registry import ToolRegistry
    from miniunicorn.session.manager import Session, SessionManager


class TurnPersistenceHost(Protocol):
    """Host capabilities required by :class:`TurnPersistence`."""

    sessions: "SessionManager"
    tools: "ToolRegistry"
    context: ContextBuilder
    max_tool_result_chars: int


class TurnPersistence:
    """Single owner of session transcript persistence algorithms."""

    def __init__(self, host: TurnPersistenceHost) -> None:
        self._host = host

    @property
    def host(self) -> TurnPersistenceHost:
        """Read-only diagnostic accessor for the bound host."""
        return self._host

    # ------------------------------------------------------------------
    # Early persistence of the triggering user message
    # ------------------------------------------------------------------

    def persist_user_message_early(
        self,
        msg: InboundMessage,
        session: "Session",
        **kwargs: Any,
    ) -> bool:
        """Persist the triggering user message before the turn starts.

        Returns True if the message was persisted.
        """
        media_paths = [p for p in (msg.media or []) if isinstance(p, str) and p]
        has_text = isinstance(msg.content, str) and msg.content.strip()
        if has_text or media_paths:
            extra: dict[str, Any] = (
                {"media": list(media_paths)} if media_paths else {}
            ) | agent_context.session_extra(msg.metadata)
            extra.update(kwargs)
            text = msg.content if isinstance(msg.content, str) else ""
            session.add_message("user", text, **extra)
            self._host.sessions.save(session)
            return True
        return False

    # ------------------------------------------------------------------
    # Outbound assembly
    # ------------------------------------------------------------------

    def assemble_outbound(
        self,
        msg: InboundMessage,
        final_content: str,
        all_msgs: list[dict[str, Any]],
        stop_reason: str,
        had_injections: bool,
        on_stream: Callable[[str], Awaitable[None]] | None,
        *,
        turn_latency_ms: int | None = None,
    ) -> OutboundMessage | None:
        """Assemble the final outbound message from turn results."""
        # MessageTool suppression
        if (
            (mt := self._host.tools.get("message"))
            and isinstance(mt, MessageTool)
            and mt._sent_in_turn
        ):
            if not had_injections or stop_reason == "empty_final_response":
                return None

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info("Response to {}:{}: {}", msg.channel, msg.sender_id, preview)

        meta = dict(msg.metadata or {})
        if on_stream is not None and stop_reason not in {"error", "tool_error"}:
            meta["_streamed"] = True
        if turn_latency_ms is not None:
            meta["latency_ms"] = int(turn_latency_ms)

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            metadata=meta,
        )

    # ------------------------------------------------------------------
    # Multimodal sanitization
    # ------------------------------------------------------------------

    def sanitize_persisted_blocks(
        self,
        content: list[dict[str, Any]],
        *,
        should_truncate_text: bool = False,
        drop_runtime: bool = False,
    ) -> list[dict[str, Any]]:
        """Strip volatile multimodal payloads before writing session history."""
        filtered: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                filtered.append(block)
                continue

            if (
                drop_runtime
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
                and block["text"].startswith(ContextBuilder._RUNTIME_CONTEXT_TAG)
            ):
                continue

            if block.get("type") == "image_url" and block.get("image_url", {}).get(
                "url", ""
            ).startswith("data:image/"):
                path = (block.get("_meta") or {}).get("path", "")
                filtered.append({"type": "text", "text": image_placeholder_text(path)})
                continue

            if block.get("type") == "text" and isinstance(block.get("text"), str):
                text = block["text"]
                if should_truncate_text and len(text) > self._host.max_tool_result_chars:
                    text = truncate_text_fn(text, self._host.max_tool_result_chars)
                filtered.append({**block, "text": text})
                continue

            filtered.append(block)

        return filtered

    # ------------------------------------------------------------------
    # History save
    # ------------------------------------------------------------------

    def save_turn(
        self,
        session: "Session",
        messages: list[dict],
        skip: int,
        *,
        turn_latency_ms: int | None = None,
    ) -> None:
        """Save new-turn messages into session, truncating large tool results."""
        from datetime import datetime

        last_assistant_idx: int | None = None
        for m in messages[skip:]:
            entry = dict(m)
            role, content = entry.get("role"), entry.get("content")
            if role == "assistant" and not content and not entry.get("tool_calls"):
                continue  # skip empty assistant messages — they poison session context
            if role == "tool":
                if isinstance(content, str) and len(content) > self._host.max_tool_result_chars:
                    entry["content"] = truncate_text_fn(content, self._host.max_tool_result_chars)
                elif isinstance(content, list):
                    filtered = self.sanitize_persisted_blocks(content, should_truncate_text=True)
                    if not filtered:
                        continue
                    entry["content"] = filtered
            elif role == "user":
                if isinstance(content, str) and ContextBuilder._RUNTIME_CONTEXT_TAG in content:
                    # Strip the runtime-context block appended at the end.
                    tag_pos = content.find(ContextBuilder._RUNTIME_CONTEXT_TAG)
                    before = content[:tag_pos].rstrip("\n ")
                    if before:
                        entry["content"] = before
                    else:
                        continue
                if isinstance(content, list):
                    filtered = self.sanitize_persisted_blocks(content, drop_runtime=True)
                    if not filtered:
                        continue
                    entry["content"] = filtered
            entry.setdefault("timestamp", datetime.now().isoformat())
            session.messages.append(entry)
            if role == "assistant":
                last_assistant_idx = len(session.messages) - 1
        if turn_latency_ms is not None and last_assistant_idx is not None:
            session.messages[last_assistant_idx]["latency_ms"] = int(turn_latency_ms)
        session.updated_at = datetime.now()

    # ------------------------------------------------------------------
    # Subagent follow-up persistence
    # ------------------------------------------------------------------

    def persist_subagent_followup(self, session: "Session", msg: InboundMessage) -> bool:
        """Persist subagent follow-ups before prompt assembly so history stays durable.

        Returns True if a new entry was appended; False if the follow-up was
        deduped (same ``subagent_task_id`` already in session) or carries no
        content worth persisting.
        """
        if not msg.content:
            return False
        task_id = msg.metadata.get("subagent_task_id") if isinstance(msg.metadata, dict) else None
        if task_id and any(
            m.get("injected_event") == "subagent_result" and m.get("subagent_task_id") == task_id
            for m in session.messages
        ):
            return False
        session.add_message(
            "assistant",
            msg.content,
            sender_id=msg.sender_id,
            injected_event="subagent_result",
            subagent_task_id=task_id,
        )
        return True

