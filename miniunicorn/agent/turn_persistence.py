"""Turn persistence and recovery collaborator.

Owns session history writes, multimodal sanitization, runtime checkpoints,
pending user-turn recovery, and subagent follow-up dedup. The algorithms
were moved here mechanically from :class:`AgentLoop`; the loop retains thin
delegates so existing monkeypatches continue to intercept calls.

Design §29.4 splits this module's responsibilities without duplicating state:

- **Legacy checkpoint reader** — ``restore_runtime_checkpoint`` /
  ``restore_pending_user_turn`` remain for migration (design §31.2) and for
  the legacy non-durable path. They are no-ops for runtime tasks.
- **Durable checkpoint adapter** — implemented separately in
  :mod:`miniunicorn.runtime.durable_journal` (design §29.4). Used by the
  Worker Adapter when ``runtime.enabled=true``.
- **SessionCommitter adapter** — implemented separately in
  :mod:`miniunicorn.runtime.session_committer` (design §17.7).

For runtime tasks (design §29.4):

- do not write ``runtime_checkpoint`` session metadata;
- do not write ``pending_user_turn`` session metadata;
- do not convert pending calls into synthetic errors.
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


# Module-level constants. ``AgentLoop`` keeps class-level aliases so tests
# and extensions that reference ``AgentLoop._RUNTIME_CHECKPOINT_KEY`` keep
# working without importing this module.
RUNTIME_CHECKPOINT_KEY = "runtime_checkpoint"
PENDING_USER_TURN_KEY = "pending_user_turn"

_INTERRUPTED_TOOL_MESSAGE = "Error: Task interrupted before this tool finished."
_INTERRUPTED_RESPONSE_MESSAGE = "Error: Task interrupted before a response was generated."


class TurnPersistenceHost(Protocol):
    """Host capabilities required by :class:`TurnPersistence`."""

    sessions: "SessionManager"
    tools: "ToolRegistry"
    context: ContextBuilder
    max_tool_result_chars: int


class TurnPersistence:
    """Single owner of checkpoint, pending-turn and history-write algorithms."""

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
            self.mark_pending_user_turn(session)
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

    # ------------------------------------------------------------------
    # Checkpoint mark/clear
    # ------------------------------------------------------------------

    def set_runtime_checkpoint(self, session: "Session", payload: dict[str, Any]) -> None:
        """Persist the latest in-flight turn state into session metadata."""
        session.metadata[RUNTIME_CHECKPOINT_KEY] = payload
        self._host.sessions.save(session)

    def mark_pending_user_turn(self, session: "Session") -> None:
        session.metadata[PENDING_USER_TURN_KEY] = True

    def clear_pending_user_turn(self, session: "Session") -> None:
        session.metadata.pop(PENDING_USER_TURN_KEY, None)

    def clear_runtime_checkpoint(self, session: "Session") -> None:
        if RUNTIME_CHECKPOINT_KEY in session.metadata:
            session.metadata.pop(RUNTIME_CHECKPOINT_KEY, None)

    @staticmethod
    def checkpoint_message_key(message: dict[str, Any]) -> tuple[Any, ...]:
        return (
            message.get("role"),
            message.get("content"),
            message.get("tool_call_id"),
            message.get("name"),
            message.get("tool_calls"),
            message.get("reasoning_content"),
            message.get("thinking_blocks"),
        )

    # ------------------------------------------------------------------
    # Checkpoint / pending-turn recovery
    # ------------------------------------------------------------------

    def restore_runtime_checkpoint(self, session: "Session") -> bool:
        """Materialize an unfinished turn into session history before a new request."""
        from datetime import datetime

        checkpoint = session.metadata.get(RUNTIME_CHECKPOINT_KEY)
        if not isinstance(checkpoint, dict):
            return False

        assistant_message = checkpoint.get("assistant_message")
        completed_tool_results = checkpoint.get("completed_tool_results") or []
        pending_tool_calls = checkpoint.get("pending_tool_calls") or []

        restored_messages: list[dict[str, Any]] = []
        if isinstance(assistant_message, dict):
            restored = dict(assistant_message)
            restored.setdefault("timestamp", datetime.now().isoformat())
            restored_messages.append(restored)
        for message in completed_tool_results:
            if isinstance(message, dict):
                restored = dict(message)
                restored.setdefault("timestamp", datetime.now().isoformat())
                restored_messages.append(restored)
        for tool_call in pending_tool_calls:
            if not isinstance(tool_call, dict):
                continue
            tool_id = tool_call.get("id")
            name = ((tool_call.get("function") or {}).get("name")) or "tool"
            restored_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "name": name,
                    "content": _INTERRUPTED_TOOL_MESSAGE,
                    "timestamp": datetime.now().isoformat(),
                }
            )

        overlap = 0
        max_overlap = min(len(session.messages), len(restored_messages))
        for size in range(max_overlap, 0, -1):
            existing = session.messages[-size:]
            restored = restored_messages[:size]
            if all(
                self.checkpoint_message_key(left) == self.checkpoint_message_key(right)
                for left, right in zip(existing, restored)
            ):
                overlap = size
                break
        session.messages.extend(restored_messages[overlap:])

        self.clear_pending_user_turn(session)
        self.clear_runtime_checkpoint(session)
        return True

    def restore_pending_user_turn(self, session: "Session") -> bool:
        """Close a turn that only persisted the user message before crashing."""
        from datetime import datetime

        if not session.metadata.get(PENDING_USER_TURN_KEY):
            return False

        if session.messages and session.messages[-1].get("role") == "user":
            session.messages.append(
                {
                    "role": "assistant",
                    "content": _INTERRUPTED_RESPONSE_MESSAGE,
                    "timestamp": datetime.now().isoformat(),
                }
            )
            session.updated_at = datetime.now()

        self.clear_pending_user_turn(session)
        return True

