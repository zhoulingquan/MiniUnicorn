"""Turn state machine orchestrator.

This module hosts the turn-state enum, the per-turn context dataclass, the
state-trace entry dataclass, the transition table, and the handler methods
that drive a turn through RESTORE → COMPACT → COMMAND → BUILD → RUN → SAVE →
RESPOND → DONE.

Phase 3 of the modular-monolith refactor extracts these from
``miniunicorn.agent.loop`` so the loop stops being the host for the turn
state machine.  Every private attribute the handlers used to read off the
host (sessions, workspace scopes, auto-compact, the per-workspace
consolidator/memory caches, the MCP lifecycle, telemetry, …) is now injected
explicitly through :class:`TurnDeps`.  The loop keeps a thin
:class:`StateMixin` that simply forwards to a :class:`TurnOrchestrator` it
constructs from its own collaborators.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from miniunicorn.agent import turn_telemetry
from miniunicorn.agent.call_ledger import CallLedger, bind_call_ledger
from miniunicorn.agent.tools.message import MessageTool
from miniunicorn.bus.events import InboundMessage, OutboundMessage
from miniunicorn.session.webui_turns import mark_webui_session
from miniunicorn.utils.document import extract_documents, reference_non_image_attachments
from miniunicorn.utils.runtime import EMPTY_FINAL_RESPONSE_MESSAGE

if TYPE_CHECKING:
    from miniunicorn.agent.context import ContextBuilder
    from miniunicorn.agent.hook import AgentHook
    from miniunicorn.agent.response import ResponseAssembler
    from miniunicorn.agent.runner import AgentRunner
    from miniunicorn.agent.runtime_resources import RuntimeResourceRegistry
    from miniunicorn.agent.session_turn import SessionTurnService
    from miniunicorn.agent.subagent_registry import SubagentDefinition
    from miniunicorn.agent.tools.registry import ToolRegistry
    from miniunicorn.agent.turn_budget import TurnBudget
    from miniunicorn.config.schema import ChannelsConfig
    from miniunicorn.security.workspace_access import WorkspaceScope
    from miniunicorn.session.manager import Session, SessionManager
    from miniunicorn.session.webui_turns import WebuiTurnCoordinator
    from miniunicorn.utils.callback_types import ProgressCallback
    from miniunicorn.utils.llm_runtime import LLMRuntime


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
    # Per-turn call ledger for LLM usage accounting and budget enforcement.
    call_ledger: CallLedger | None = None


@dataclass
class TurnDeps:
    """Explicit collaborators injected into the turn state machine.

    The state handlers used to reach into the loop's private attributes
    directly.  This dependency bundle is the single bridge between the state
    machine and the loop: the orchestrator never reads a host attribute and
    never references the loop type.

    Callable fields are late-bound so tests that monkeypatch the loop's
    methods after construction (e.g. ``loop._run_agent_loop = ...``) keep
    taking effect.
    """

    session_turn: SessionTurnService
    resources: RuntimeResourceRegistry
    response: ResponseAssembler
    runner: AgentRunner
    tools: ToolRegistry
    context_builder: ContextBuilder
    commands: Callable[[InboundMessage, Session | None, str, str], Awaitable[OutboundMessage | None]]
    webui_turns: WebuiTurnCoordinator
    sessions: SessionManager
    channels_config: ChannelsConfig | None
    max_messages: int

    run_agent_loop: Callable[..., Awaitable[tuple[str | None, list[str], list[dict], str, bool]]]
    build_bus_progress_callback: Callable[[InboundMessage], Awaitable[ProgressCallback]]
    build_retry_wait_callback: Callable[[InboundMessage], Awaitable[Callable[[str], Awaitable[None]]]]
    assemble_outbound: Callable[..., OutboundMessage | None]
    schedule_background: Callable[[Any], None]
    set_tool_context: Callable[..., None]
    build_initial_messages: Callable[..., Awaitable[list[dict[str, Any]]]]
    replay_token_budget: Callable[[], int]
    llm_runtime: Callable[[], LLMRuntime]
    refresh_provider_snapshot: Callable[[], None]
    resolve_agent_override: Callable[[InboundMessage], SubagentDefinition | None]
    process_system_message: Callable[..., Awaitable[OutboundMessage | None]]
    build_turn_budget: Callable[[], TurnBudget | None]


class TurnOrchestrator:
    """Event-driven per-turn state machine.

    The handlers were lifted out of the agent loop verbatim; every host
    attribute read is now an explicit :class:`TurnDeps` access, so the
    orchestrator can be unit-tested without a loop instance.
    """

    # Handlers return an event string; the driver looks up the next state here.
    _TRANSITIONS: dict[tuple[TurnState, str], TurnState] = {
        (TurnState.RESTORE, "ok"): TurnState.COMPACT,
        (TurnState.COMPACT, "ok"): TurnState.COMMAND,
        (TurnState.COMMAND, "dispatch"): TurnState.BUILD,
        (TurnState.COMMAND, "shortcut"): TurnState.DONE,
        (TurnState.BUILD, "ok"): TurnState.RUN,
        (TurnState.RUN, "ok"): TurnState.SAVE,
        (TurnState.SAVE, "ok"): TurnState.RESPOND,
        (TurnState.RESPOND, "ok"): TurnState.DONE,
    }

    def __init__(self, deps: TurnDeps) -> None:
        self._deps = deps

    # -- driver ---------------------------------------------------------------

    async def process_turn(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: ProgressCallback | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        pending_queue: asyncio.Queue | None = None,
        turn_hooks: list[AgentHook] | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response."""
        self._deps.refresh_provider_snapshot()

        if msg.channel == "system":
            ledger = CallLedger(budget=self._deps.build_turn_budget())
            async with bind_call_ledger(ledger):
                return await self._deps.process_system_message(
                    msg,
                    session_key=session_key,
                    on_progress=on_progress,
                    on_stream=on_stream,
                    on_stream_end=on_stream_end,
                    pending_queue=pending_queue,
                )

        key = session_key or msg.session_key
        ctx = TurnContext(
            msg=msg,
            session=None,
            session_key=key,
            state=TurnState.RESTORE,
            turn_id=f"{key}:{time.time_ns()}",
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            pending_queue=pending_queue,
            agent_override=self._deps.resolve_agent_override(msg),
            turn_hooks=list(turn_hooks or []),
        )

        ledger = CallLedger(budget=self._deps.build_turn_budget())
        ctx.call_ledger = ledger

        async with bind_call_ledger(ledger):
            while ctx.state is not TurnState.DONE:
                handler_name = f"_state_{ctx.state.name.lower()}"
                handler = getattr(self, handler_name, None)
                if handler is None:
                    raise RuntimeError(f"Missing state handler for {ctx.state}")

                t0 = time.perf_counter()
                try:
                    event = await handler(ctx)
                except Exception:
                    duration = (time.perf_counter() - t0) * 1000
                    ctx.trace.append(
                        StateTraceEntry(
                            state=ctx.state,
                            started_at=t0,
                            duration_ms=duration,
                            event="",
                            error="exception",
                        )
                    )
                    raise

                duration = (time.perf_counter() - t0) * 1000
                ctx.trace.append(
                    StateTraceEntry(
                        state=ctx.state,
                        started_at=t0,
                        duration_ms=duration,
                        event=event,
                    )
                )
                logger.debug(
                    "[turn {}] State {} took {:.1f}ms -> event {}",
                    ctx.turn_id,
                    ctx.state.name,
                    duration,
                    event,
                )

                next_state = self._TRANSITIONS.get((ctx.state, event))
                if next_state is None:
                    raise RuntimeError(
                        f"[turn {ctx.turn_id}] No transition from {ctx.state} on event {event!r}"
                    )
                ctx.state = next_state

        logger.debug(
            "[turn {}] Turn completed after {} states",
            ctx.turn_id,
            len(ctx.trace),
        )
        telemetry = turn_telemetry.current()
        if telemetry is not None and telemetry.prompt_components is not None:
            pc = telemetry.prompt_components
            pressure_level = telemetry.governance_pressure.level if telemetry.governance_pressure else "unknown"
            logger.info(
                "prompt telemetry: sys={} tools={} history={} compacted={} total={} pressure={}",
                pc.system_prompt,
                pc.tool_definitions,
                pc.conversation_history,
                pc.compacted_context,
                pc.total_estimated,
                pressure_level,
            )
        return ctx.outbound

    # -- RESTORE --------------------------------------------------------------

    async def _state_restore(self, ctx: TurnContext) -> TurnState:
        """Restore checkpoint / pending user turn; extract documents."""
        msg = ctx.msg

        if msg.media:
            new_content, image_only = self._prepare_message_media(msg.content, msg.media)
            ctx.msg = dataclasses.replace(msg, content=new_content, media=image_only)
            msg = ctx.msg

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info("Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview)

        # Session is already fetched by the driver but ensure it exists in
        # case this handler is invoked independently.
        if ctx.session is None:
            ctx.session = self._deps.sessions.get_or_create(ctx.session_key)
        mark_webui_session(ctx.session, msg.metadata)
        self._deps.resources.workspace_scopes.persist_message_scope(ctx.session, msg)

        if self._deps.session_turn._restore_runtime_checkpoint(ctx.session):
            self._deps.sessions.save(ctx.session)
        if self._deps.session_turn._restore_pending_user_turn(ctx.session):
            self._deps.sessions.save(ctx.session)

        return "ok"

    def _prepare_message_media(
        self, content: str, media: list[str]
    ) -> tuple[str, list[str]]:
        if self._should_extract_document_text():
            return extract_documents(content, media)
        return reference_non_image_attachments(content, media)

    def _should_extract_document_text(self) -> bool:
        if self._deps.channels_config is None:
            return True
        return self._deps.channels_config.extract_document_text

    # -- COMPACT --------------------------------------------------------------

    async def _state_compact(self, ctx: TurnContext) -> str:
        ctx.session, pending = self._deps.resources.auto_compact.prepare_session(
            ctx.session, ctx.session_key
        )
        ctx.pending_summary = pending
        return "ok"

    # -- COMMAND --------------------------------------------------------------

    async def _state_command(self, ctx: TurnContext) -> str:
        raw = ctx.msg.content.strip()
        result = await self._deps.commands(ctx.msg, ctx.session, ctx.session_key, raw)
        if result is not None:
            ctx.outbound = result
            # Shortcut commands skip BUILD and SAVE, so we must persist the
            # turn here so WebUI history hydration after turn end sees the
            # message.  Mark messages with _command so get_history can filter
            # them out of LLM context.  /new is excluded because it
            # intentionally clears the session.
            if raw.lower() != "/new":
                ctx.user_persisted_early = self._deps.session_turn._persist_user_message_early(
                    ctx.msg, ctx.session, _command=True
                )
                ctx.session.add_message("assistant", result.content, _command=True)
                self._deps.sessions.save(ctx.session)
                self._deps.session_turn._clear_pending_user_turn(ctx.session)
            return "shortcut"
        return "dispatch"

    # -- BUILD ----------------------------------------------------------------

    async def _state_build(self, ctx: TurnContext) -> str:
        scope = self._turn_scope(ctx.msg, ctx.session)
        await self._deps.resources._consolidator_for(scope.project_path).maybe_consolidate_by_tokens(
            ctx.session,
            replay_max_messages=self._deps.max_messages,
        )
        self._deps.set_tool_context(
            ctx.msg.channel,
            ctx.msg.chat_id,
            ctx.msg.metadata.get("message_id"),
            ctx.msg.metadata,
            session_key=ctx.session_key,
        )
        if message_tool := self._deps.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        _hist_kwargs: dict[str, Any] = {
            "max_messages": self._deps.max_messages,
            "max_tokens": self._deps.replay_token_budget(),
            "include_timestamps": True,
        }
        ctx.history = ctx.session.get_history(**_hist_kwargs)
        self._deps.webui_turns.capture_title_context(
            ctx.session_key,
            ctx.msg,
            self._deps.llm_runtime(),
        )

        ctx.initial_messages = await self._deps.build_initial_messages(
            ctx.msg,
            ctx.session,
            ctx.history,
            ctx.pending_summary,
            agent_override=ctx.agent_override,
        )
        ctx.user_persisted_early = self._deps.session_turn._persist_user_message_early(
            ctx.msg, ctx.session
        )

        if ctx.on_progress is None:
            ctx.on_progress = await self._deps.build_bus_progress_callback(ctx.msg)
        if ctx.on_retry_wait is None:
            ctx.on_retry_wait = await self._deps.build_retry_wait_callback(ctx.msg)

        return "ok"

    # -- RUN ------------------------------------------------------------------

    async def _state_run(self, ctx: TurnContext) -> str:
        await self._deps.webui_turns.publish_run_status(ctx.msg, "running")
        sender_id = ctx.msg.sender_id
        user_key = (
            f"user:{sender_id}"
            if sender_id and sender_id != "subagent"
            else "user:default"
        )
        result = await self._deps.run_agent_loop(
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

    # -- SAVE -----------------------------------------------------------------

    async def _state_save(self, ctx: TurnContext) -> str:
        if ctx.final_content is None or not ctx.final_content.strip():
            ctx.final_content = EMPTY_FINAL_RESPONSE_MESSAGE

        ctx.save_skip = 1 + len(ctx.history) + (1 if ctx.user_persisted_early else 0)

        ctx.turn_latency_ms = max(0, int((time.time() - ctx.turn_wall_started_at) * 1000))
        self._deps.session_turn._save_turn(
            ctx.session,
            ctx.all_messages,
            ctx.save_skip,
            turn_latency_ms=ctx.turn_latency_ms,
        )
        if ctx.msg.channel == "websocket":
            self._deps.response.record_pending_turn_latency(ctx.session_key, ctx.turn_latency_ms)
        scope = self._turn_scope(ctx.msg, ctx.session)
        ctx.session.enforce_file_cap(
            on_archive=self._deps.resources.memory_for(scope.project_path).raw_archive
        )
        self._deps.session_turn._clear_pending_user_turn(ctx.session)
        self._deps.session_turn._clear_runtime_checkpoint(ctx.session)
        self._deps.sessions.save(ctx.session)
        self._deps.schedule_background(
            self._deps.resources._consolidator_for(scope.project_path).maybe_consolidate_by_tokens(
                ctx.session,
                replay_max_messages=self._deps.max_messages,
            )
        )
        return "ok"

    # -- RESPOND --------------------------------------------------------------

    async def _state_respond(self, ctx: TurnContext) -> str:
        ctx.outbound = self._deps.assemble_outbound(
            ctx.msg,
            ctx.final_content,
            ctx.all_messages,
            ctx.stop_reason,
            ctx.had_injections,
            ctx.on_stream,
            turn_latency_ms=ctx.turn_latency_ms,
        )
        return "ok"

    # -- helpers --------------------------------------------------------------

    def _turn_scope(self, msg: InboundMessage, session: Session) -> WorkspaceScope:
        """Resolve the effective workspace scope for a turn."""
        return self._deps.resources.workspace_scopes.for_turn(
            channel=msg.channel,
            message_metadata=msg.metadata,
            session_metadata=session.metadata,
        )


class StateMixin:
    """Thin delegates for the turn state machine.

    Keeps the loop's historical handler surface (``_state_restore`` …
    ``_state_respond``, ``_prepare_message_media``) intact for commands,
    tools and tests; every call is forwarded to the injected
    :class:`TurnOrchestrator`.
    """

    _turn_orchestrator: TurnOrchestrator

    async def _state_restore(self, ctx: TurnContext) -> TurnState:
        return await self._turn_orchestrator._state_restore(ctx)

    async def _state_compact(self, ctx: TurnContext) -> str:
        return await self._turn_orchestrator._state_compact(ctx)

    async def _state_command(self, ctx: TurnContext) -> str:
        return await self._turn_orchestrator._state_command(ctx)

    async def _state_build(self, ctx: TurnContext) -> str:
        return await self._turn_orchestrator._state_build(ctx)

    async def _state_run(self, ctx: TurnContext) -> str:
        return await self._turn_orchestrator._state_run(ctx)

    async def _state_save(self, ctx: TurnContext) -> str:
        return await self._turn_orchestrator._state_save(ctx)

    async def _state_respond(self, ctx: TurnContext) -> str:
        return await self._turn_orchestrator._state_respond(ctx)

    def _prepare_message_media(
        self, content: str, media: list[str]
    ) -> tuple[str, list[str]]:
        return self._turn_orchestrator._prepare_message_media(content, media)

    def _should_extract_document_text(self) -> bool:
        return self._turn_orchestrator._should_extract_document_text()
