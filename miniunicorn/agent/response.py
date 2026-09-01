"""Response assembly service.

Owns the outbound-response machinery that was formerly spread across the
agent loop and the dispatcher: assembling the final outbound message from a
finished turn, building the streaming segment closures (``on_stream`` /
``on_stream_end``) used to push token deltas to streaming channels, and the
trailing telemetry snapshot (``_last_usage`` / ``_last_call_usage``) plus the
pending-turn-latency map (``_pending_turn_latency_ms``).

Phase 6 convergence: the *turn-scoped* copies of the telemetry moved to
``miniunicorn.agent.turn_telemetry`` (bound per dispatch).  The response
snapshot remains the trailing display surface for ``/status`` and the ``my``
tool, which run inside command turns where the per-turn telemetry is empty.
The pending-turn-latency mechanism is intentionally unchanged.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from miniunicorn.bus.events import InboundMessage, OutboundMessage
from miniunicorn.tools.message import MessageTool

if TYPE_CHECKING:
    from miniunicorn.bus.queue import MessageBus
    from miniunicorn.tools.registry import ToolRegistry


class ResponseAssembler:
    """Assemble and publish the agent's outbound responses."""

    def __init__(self, bus: MessageBus, tools: ToolRegistry) -> None:
        self.bus = bus
        self.tools = tools
        # 尾部快照(跨会话共享的显示语义):/status 与 my 工具运行在命令回合内,
        # 此时 per-turn 遥测为空,故保留快照作为显示来源(Phase 6 收敛后仅剩
        # _last_usage 由 loop 属性对外; _last_call_usage 仅作 turn_end 兜底)。
        self._last_usage: dict[str, int] = {}
        self._last_call_usage: dict[str, int] = {}
        self._pending_turn_latency_ms: dict[str, int] = {}

    # -- final outbound assembly ---------------------------------------------

    def _assemble_outbound(
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
        if (mt := self.tools.get("message")) and isinstance(mt, MessageTool) and mt._sent_in_turn:
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

    # -- streaming closures --------------------------------------------------

    def build_stream_closures(
        self, msg: InboundMessage
    ) -> tuple[
        Callable[[str], Awaitable[None]] | None,
        Callable[..., Awaitable[None]] | None,
    ]:
        """Construct the streaming ``on_stream`` / ``on_stream_end`` closures.

        Returns ``(None, None)`` for messages that did not opt into streaming
        (no ``_wants_stream`` metadata).
        """
        on_stream = on_stream_end = None
        if msg.metadata.get("_wants_stream"):
            # Split one answer into distinct stream segments.
            stream_base_id = f"{msg.session_key}:{time.time_ns()}"
            stream_segment = 0

            def _current_stream_id() -> str:
                return f"{stream_base_id}:{stream_segment}"

            async def on_stream(delta: str) -> None:
                meta = dict(msg.metadata or {})
                meta["_stream_delta"] = True
                meta["_stream_id"] = _current_stream_id()
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=delta,
                        metadata=meta,
                    )
                )

            async def on_stream_end(*, resuming: bool = False) -> None:
                nonlocal stream_segment
                meta = dict(msg.metadata or {})
                meta["_stream_end"] = True
                meta["_resuming"] = resuming
                meta["_stream_id"] = _current_stream_id()
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content="",
                        metadata=meta,
                    )
                )
                stream_segment += 1

        return on_stream, on_stream_end

    # -- trailing telemetry writes ------------------------------------------

    def record_last_usage(self, result: Any) -> None:
        """Record the trailing usage telemetry of a finished runner turn.

        Phase 6 决策项: 见 ``_last_usage`` 属性注释,此处仅迁移写入位置。
        """
        self._last_usage = result.usage
        self._last_call_usage = result.last_call_usage

    def record_pending_turn_latency(self, session_key: str, latency_ms: int) -> None:
        """Stage the wall-clock latency of a websocket turn for its turn_end event."""
        self._pending_turn_latency_ms[session_key] = latency_ms

    def pop_pending_turn_latency(self, session_key: str) -> int | None:
        """Consume the staged latency for a websocket turn, if any."""
        return self._pending_turn_latency_ms.pop(session_key, None)
