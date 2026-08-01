"""Dream 空闲触发器：用户不使用时在后台触发 Dream，不依赖 cron 定时。

解决"用户不 24 小时运行 gateway，凌晨 cron 点大概率关机"的问题。
借鉴 Claude Dreaming 的"会话间空闲自动触发"机制。

与 cron 形成互补：
- cron 保证最低频率（如每天凌晨 3 点保底）
- 空闲触发保证"有数据就尽快整理"（用户停用 5 分钟后即触发）

两者共享同一个 ``Dream.run()``，cursor 机制保证不会重复处理。

Durable maintenance (design §22.3, §29.16):
When ``enqueue_callback`` is set, the trigger enqueues a durable ``DREAM``
internal task instead of owning the work through ``asyncio.create_task``.
This ensures required Dream work survives process restarts.
"""

from __future__ import annotations

import time
from collections.abc import Collection
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from loguru import logger

if TYPE_CHECKING:
    from miniunicorn.agent.memory import Dream


class DreamIdleTrigger:
    """会话空闲时触发 Dream，不依赖 cron 定时。

    触发条件（全部满足才触发）：
    1. 无活跃会话（用户不在使用）
    2. 距上次 dream 超过 ``min_interval_s``（防止高频）
    3. 未处理历史条目数 ≥ ``min_entries``（有足够数据才值得 dream）
    4. 距上次用户活动超过 ``min_idle_seconds``（避免打断工作流）

    触发后异步执行 ``Dream.run()``，不阻塞 agent loop 主循环。

    When ``enqueue_callback`` is provided (durable mode, design §22.3), the
    trigger calls it with a source revision derived from the current dream
    cursor instead of spawning ``asyncio.create_task``. The actual Dream
    execution is then owned by a maintenance Worker that claims the durable
    task. This satisfies WP7 exit criteria: "no required background operation
    is owned only by Task Supervisor" (design §22.3, §29.16, §35.13).
    """

    def __init__(
        self,
        dream: "Dream",
        *,
        enabled: bool = True,
        min_idle_seconds: int = 300,
        min_entries: int = 5,
        min_interval_s: int = 3600,
        enqueue_callback: Callable[[str], Coroutine[Any, Any, str | None]] | None = None,
    ) -> None:
        self.dream = dream
        self.enabled = enabled
        self.min_idle_seconds = min_idle_seconds
        self.min_entries = min_entries
        self.min_interval_s = min_interval_s
        self._last_trigger_ts: float = 0.0
        self._last_user_activity_ts: float = time.monotonic()
        self._running: bool = False  # 防止并发 dream
        # Durable enqueue callback (design §22.3). The trigger enqueues a
        # DREAM task instead of asyncio.create_task. The callback receives
        # the source revision (dream cursor) and returns the task_id.
        self._enqueue_callback = enqueue_callback

    def update_config(
        self,
        *,
        enabled: bool | None = None,
        min_idle_seconds: int | None = None,
        min_entries: int | None = None,
        min_interval_s: int | None = None,
    ) -> None:
        """运行时更新配置（gateway 启动时从 DreamConfig 同步）。"""
        if enabled is not None:
            self.enabled = enabled
        if min_idle_seconds is not None:
            self.min_idle_seconds = min_idle_seconds
        if min_entries is not None:
            self.min_entries = min_entries
        if min_interval_s is not None:
            self.min_interval_s = min_interval_s

    def notify_user_activity(self) -> None:
        """标记用户有新活动（重置空闲计时器）。

        由 AgentLoop 在收到用户消息时调用。
        """
        self._last_user_activity_ts = time.monotonic()

    async def maybe_trigger(
        self,
        active_session_keys: Collection[str] = (),
    ) -> None:
        """检查是否满足空闲触发条件，满足则后台触发 Dream。

        在 AgentLoop.run() 的 timeout 分支中调用（每秒一次）。

        Enqueues a durable DREAM task through ``enqueue_callback`` (design
        §22.3, WP7 hard cutover). The source revision is derived from the
        current dream cursor so repeated submissions within the same cursor
        deduplicate (design §13.1).
        """
        if not self.enabled or self._running:
            return
        # 有活跃会话不触发（用户正在使用）
        if active_session_keys:
            return
        now = time.monotonic()
        # 空闲时间不足不触发
        if now - self._last_user_activity_ts < self.min_idle_seconds:
            return
        # 距上次 dream 间隔不足不触发
        if self._last_trigger_ts and now - self._last_trigger_ts < self.min_interval_s:
            return
        # 检查是否有足够的新数据
        try:
            cursor = self.dream.store.get_last_dream_cursor()
            unprocessed = self.dream.store.read_unprocessed_history(since_cursor=cursor)
        except Exception:
            logger.debug("Dream idle trigger: failed to read unprocessed history", exc_info=True)
            return
        if len(unprocessed) < self.min_entries:
            return
        # 满足所有条件，触发
        self._last_trigger_ts = now
        logger.info(
            "Dream idle trigger: {} unprocessed entries, triggering dream",
            len(unprocessed),
        )
        if self._enqueue_callback is None:
            logger.warning("Dream idle trigger: no enqueue_callback, skipping (WP7 hard cutover)")
            return
        # Durable mode: enqueue a DREAM task (design §22.3).
        # The source revision is the current dream cursor, so repeated
        # submissions deduplicate until the cursor advances.
        source_revision = cursor or "init"
        try:
            task_id = await self._enqueue_callback(source_revision)
            if task_id:
                logger.info(
                    "Dream idle trigger: enqueued durable DREAM task {} (rev={})",
                    task_id,
                    source_revision,
                )
        except Exception:
            logger.exception("Dream idle trigger: enqueue failed")

    @property
    def is_running(self) -> bool:
        """Dream 是否正在执行（用于避免并发触发）。"""
        return self._running
