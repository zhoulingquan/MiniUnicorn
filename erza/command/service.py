"""Command application service.

Owns the application-level decision of what happens when an inbound message
is (or might be) a slash command: classifying the message against the command
router tiers, executing the matched handler, and publishing the produced
outbound payload to the bus.

Commands are classified into three categories:

* 运行时命令 (runtime commands): ``/stop``、``/restart`` (及未来可能的 ``/reload``)。
  注册在 router 的 priority 层级,由 dispatcher 的 ``run()`` 调度循环在进入
  per-session 派发锁之前就地执行,保证高延迟回合中仍可即时中断。
* 会话命令 (session commands): ``/new``、``/model``、``/history``、``/goal``、
  ``/dream`` 等(设计上含 ``/rewind``、``/compact`` 一类操作会话历史/上下文的命令)。
  注册在 exact / prefix 层级,在派发锁内与普通消息一样执行。
* 查询命令 (query commands): ``/status``、``/help``。读取当前运行时/会话状态并
  立即返回,无需进入完整 turn 状态机。

``CommandRouter`` 与内置命令(builtin)的行为和输出逐字节不变;本服务只负责
"何时判定为命令、如何就地执行并发布结果"。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from erza.bus.events import InboundMessage, OutboundMessage
from erza.command.router import CommandContext, CommandRouter

if TYPE_CHECKING:
    from erza.bus.queue import MessageBus


class CommandApplicationService:
    """Application service for slash-command execution.

    The service is constructed with the outbound ``bus`` (where command
    results are published) and the ``CommandRouter`` holding the registered
    handlers.  The host agent loop is passed per-call so ``CommandContext.loop``
    continues to reference the live loop while the service itself stays
    decoupled from any particular host.
    """

    def __init__(self, bus: MessageBus, router: CommandRouter) -> None:
        self.bus = bus
        self.router = router

    def is_priority_command(self, raw: str) -> bool:
        """Check whether *raw* matches the priority (runtime-command) tier."""
        return self.router.is_priority(raw)

    def is_dispatchable_command(self, raw: str) -> bool:
        """Check whether *raw* matches any non-priority command tier."""
        return self.router.is_dispatchable_command(raw)

    async def dispatch_priority_inline(
        self,
        host: Any,
        msg: InboundMessage,
        key: str,
        raw: str,
    ) -> None:
        """Dispatch a priority (runtime) command from the dispatcher run() loop."""
        await self._dispatch_inline(host, msg, key, raw, self.router.dispatch_priority)

    async def dispatch_inline(
        self,
        host: Any,
        msg: InboundMessage,
        key: str,
        raw: str,
    ) -> None:
        """Dispatch a non-priority command while a session has an active task."""
        await self._dispatch_inline(host, msg, key, raw, self.router.dispatch)

    async def _dispatch_inline(
        self,
        host: Any,
        msg: InboundMessage,
        key: str,
        raw: str,
        dispatch_fn: Callable[[CommandContext], Awaitable[OutboundMessage | None]],
    ) -> None:
        """Build the command context, run the handler, and publish the result."""
        ctx = CommandContext(msg=msg, session=None, key=key, raw=raw, loop=host)
        result = await dispatch_fn(ctx)
        if result:
            await self.bus.publish_outbound(result)
        else:
            logger.warning("Command '{}' matched but dispatch returned None", raw)
