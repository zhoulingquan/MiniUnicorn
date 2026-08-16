"""AgentLoop state-machine mixin.

This module hosts the turn-state enum, the per-turn context dataclass, the
state-trace entry dataclass, and the small handler methods that drive the
AgentLoop through its RESTORE → COMPACT → COMMAND → BUILD → RUN → SAVE →
RESPOND → DONE state machine.

The methods here are extracted from ``miniunicorn.agent.loop.AgentLoop`` purely
to keep that module focused on orchestration; ``AgentLoop`` re-combines them
through multiple inheritance (see ``StateMixin``).
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from miniunicorn.agent.subagent_registry import SubagentDefinition
from miniunicorn.agent.tools.message import MessageTool
from miniunicorn.bus.events import InboundMessage, OutboundMessage
from miniunicorn.command import CommandContext
from miniunicorn.session.webui_turns import mark_webui_session
from miniunicorn.utils.callback_types import ProgressCallback
from miniunicorn.utils.document import extract_documents, reference_non_image_attachments
from miniunicorn.utils.runtime import EMPTY_FINAL_RESPONSE_MESSAGE

if TYPE_CHECKING:
    from miniunicorn.agent.hook import AgentHook
    from miniunicorn.agent.loop import AgentLoop
    from miniunicorn.session.manager import Session


class TurnState(Enum):
    RESTORE = auto()
    COMPACT = auto()
    COMMAND = auto()
    BUILD = auto()
    RUN = auto()
    SAVE = auto()
    RESPOND = auto()
    DONE = auto()


@dataclass
class StateTraceEntry:
    state: TurnState
    started_at: float
    duration_ms: float
    event: str
    error: str | None = None


@dataclass
class TurnContext:
    msg: InboundMessage
    session_key: str
    state: TurnState
    turn_id: str
    session: Session | None = None

    history: list[dict[str, Any]] = field(default_factory=list)
    initial_messages: list[dict[str, Any]] = field(default_factory=list)

    final_content: str | None = None
    tools_used: list[str] = field(default_factory=list)
    all_messages: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str = ""
    had_injections: bool = False

    user_persisted_early: bool = False
    save_skip: int = 0

    outbound: OutboundMessage | None = None

    on_progress: ProgressCallback | None = None
    on_stream: Callable[[str], Awaitable[None]] | None = None
    on_stream_end: Callable[..., Awaitable[None]] | None = None
    on_retry_wait: Callable[[str], Awaitable[None]] | None = None

    pending_queue: asyncio.Queue | None = None
    pending_summary: str | None = None
    turn_wall_started_at: float = field(default_factory=time.time)
    turn_latency_ms: int | None = None

    trace: list[StateTraceEntry] = field(default_factory=list)
    # Subagent takeover: when set, the turn runs as this subagent's identity
    # (system prompt, tools whitelist, model) instead of the default agent.
    agent_override: SubagentDefinition | None = None
    # Per-turn hooks bound to this single dispatch (e.g. SDK capture hook).
    # ``_run_agent_loop`` combines these with the loop-level ``_extra_hooks``
    # so the SDK no longer needs to mutate shared state for concurrent runs.
    turn_hooks: list["AgentHook"] = field(default_factory=list)


class StateMixin:
    """State-machine handlers for :class:`AgentLoop`.

    Provides the per-turn state handlers (RESTORE/COMPACT/COMMAND/BUILD/RUN/
    SAVE/RESPOND) plus a couple of small media helpers. The orchestration
    driver lives in :class:`AgentLoop`; this mixin only contributes the leaf
    handlers so they can be unit-tested in isolation.
    """

    async def _state_restore(self: "AgentLoop", ctx: TurnContext) -> TurnState:
        """Restore checkpoint / pending user turn; extract documents."""
        msg = ctx.msg

        if msg.media:
            new_content, image_only = self._prepare_message_media(msg.content, msg.media)
            ctx.msg = dataclasses.replace(msg, content=new_content, media=image_only)
            msg = ctx.msg

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info("Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview)

        # Session is already fetched by the caller (_process_message) but
        # ensure it exists in case this handler is invoked independently.
        if ctx.session is None:
            ctx.session = self.sessions.get_or_create(ctx.session_key)
        mark_webui_session(ctx.session, msg.metadata)
        self.workspace_scopes.persist_message_scope(ctx.session, msg)

        if self._restore_runtime_checkpoint(ctx.session):
            self.sessions.save(ctx.session)
        if self._restore_pending_user_turn(ctx.session):
            self.sessions.save(ctx.session)

        return "ok"

    def _prepare_message_media(
        self: "AgentLoop", content: str, media: list[str]
    ) -> tuple[str, list[str]]:
        if self._should_extract_document_text():
            return extract_documents(content, media)
        return reference_non_image_attachments(content, media)

    def _should_extract_document_text(self: "AgentLoop") -> bool:
        if self.channels_config is None:
            return True
        return self.channels_config.extract_document_text

    async def _state_compact(self: "AgentLoop", ctx: TurnContext) -> str:
        ctx.session, pending = self.auto_compact.prepare_session(ctx.session, ctx.session_key)
        ctx.pending_summary = pending
        return "ok"

    async def _state_command(self: "AgentLoop", ctx: TurnContext) -> str:
        raw = ctx.msg.content.strip()
        cmd_ctx = CommandContext(
            msg=ctx.msg, session=ctx.session, key=ctx.session_key, raw=raw, loop=self
        )
        result = await self.commands.dispatch(cmd_ctx)
        if result is not None:
            ctx.outbound = result
            # Shortcut commands skip BUILD and SAVE, so we must persist the
            # turn here so WebUI history hydration after _turn_end sees the
            # message.  Mark messages with _command so get_history can filter
            # them out of LLM context.  /new is excluded because it
            # intentionally clears the session.
            if raw.lower() != "/new":
                ctx.user_persisted_early = self._persist_user_message_early(
                    ctx.msg, ctx.session, _command=True
                )
                ctx.session.add_message("assistant", result.content, _command=True)
                self.sessions.save(ctx.session)
                self._clear_pending_user_turn(ctx.session)
            return "shortcut"
        return "dispatch"

    async def _state_build(self: "AgentLoop", ctx: TurnContext) -> str:
        scope = self._turn_scope(ctx.msg, ctx.session)
        await self._consolidator_for(scope.project_path).maybe_consolidate_by_tokens(
            ctx.session,
            replay_max_messages=self._max_messages,
        )
        self._set_tool_context(
            ctx.msg.channel,
            ctx.msg.chat_id,
            ctx.msg.metadata.get("message_id"),
            ctx.msg.metadata,
            session_key=ctx.session_key,
        )
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        _hist_kwargs: dict[str, Any] = {
            "max_messages": self._max_messages,
            "max_tokens": self._replay_token_budget(),
            "include_timestamps": True,
        }
        ctx.history = ctx.session.get_history(**_hist_kwargs)
        self._webui_turns.capture_title_context(
            ctx.session_key,
            ctx.msg,
            self.llm_runtime(),
        )

        ctx.initial_messages = await self._build_initial_messages(
            ctx.msg,
            ctx.session,
            ctx.history,
            ctx.pending_summary,
            agent_override=ctx.agent_override,
        )
        ctx.user_persisted_early = self._persist_user_message_early(ctx.msg, ctx.session)

        if ctx.on_progress is None:
            ctx.on_progress = await self._build_bus_progress_callback(ctx.msg)
        if ctx.on_retry_wait is None:
            ctx.on_retry_wait = await self._build_retry_wait_callback(ctx.msg)

        return "ok"

    async def _state_run(self: "AgentLoop", ctx: TurnContext) -> str:
        await self._webui_turns.publish_run_status(ctx.msg, "running")
        sender_id = ctx.msg.sender_id
        user_key = (
            f"user:{sender_id}"
            if sender_id and sender_id != "subagent"
            else "user:default"
        )
        result = await self._run_agent_loop(
            ctx.initial_messages,
            on_progress=ctx.on_progress,
            on_stream=ctx.on_stream,
            on_stream_end=ctx.on_stream_end,
            on_retry_wait=ctx.on_retry_wait,
            session=ctx.session,
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            message_id=ctx.msg.metadata.get("message_id"),
            metadata=ctx.msg.metadata,
            session_key=ctx.session_key,
            user_key=user_key,
            pending_queue=ctx.pending_queue,
            agent_override=ctx.agent_override,
            turn_hooks=ctx.turn_hooks,
        )
        final_content, tools_used, all_msgs, stop_reason, had_injections = result
        ctx.final_content = final_content
        ctx.tools_used = tools_used
        ctx.all_messages = all_msgs
        ctx.stop_reason = stop_reason
        ctx.had_injections = had_injections
        return "ok"

    async def _state_save(self: "AgentLoop", ctx: TurnContext) -> str:
        if ctx.final_content is None or not ctx.final_content.strip():
            ctx.final_content = EMPTY_FINAL_RESPONSE_MESSAGE

        ctx.save_skip = 1 + len(ctx.history) + (1 if ctx.user_persisted_early else 0)

        ctx.turn_latency_ms = max(0, int((time.time() - ctx.turn_wall_started_at) * 1000))
        self._save_turn(
            ctx.session,
            ctx.all_messages,
            ctx.save_skip,
            turn_latency_ms=ctx.turn_latency_ms,
        )
        if ctx.msg.channel == "websocket":
            self._pending_turn_latency_ms[ctx.session_key] = ctx.turn_latency_ms
        scope = self._turn_scope(ctx.msg, ctx.session)
        ctx.session.enforce_file_cap(
            on_archive=self.memory_for(scope.project_path).raw_archive
        )
        self._clear_pending_user_turn(ctx.session)
        self._clear_runtime_checkpoint(ctx.session)
        self.sessions.save(ctx.session)
        self._schedule_background(
            self._consolidator_for(scope.project_path).maybe_consolidate_by_tokens(
                ctx.session,
                replay_max_messages=self._max_messages,
            )
        )
        return "ok"

    async def _state_respond(self: "AgentLoop", ctx: TurnContext) -> str:
        ctx.outbound = self._assemble_outbound(
            ctx.msg,
            ctx.final_content,
            ctx.all_messages,
            ctx.stop_reason,
            ctx.had_injections,
            ctx.on_stream,
            turn_latency_ms=ctx.turn_latency_ms,
        )
        return "ok"
