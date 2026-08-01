"""Auto compact: proactive compression of idle sessions to reduce token cost and latency."""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from loguru import logger

from miniunicorn.session.manager import Session, SessionManager

if TYPE_CHECKING:
    from miniunicorn.agent.memory import Consolidator


class AutoCompact:
    _RECENT_SUFFIX_MESSAGES = 8

    def __init__(
        self, sessions: SessionManager, consolidator: Consolidator, session_ttl_minutes: int = 0
    ):
        self.sessions = sessions
        self.consolidator = consolidator
        self._ttl = session_ttl_minutes
        self._archiving: set[str] = set()
        self._summaries: dict[str, tuple[str, datetime]] = {}
        # Durable enqueue callback (design §22.3). When set, check_expired
        # enqueues a MEMORY_CONSOLIDATION task instead of owning the work
        # through asyncio.create_task. The callback receives the session key
        # (used as source revision) and returns the task_id or None.
        self._enqueue_callback: (
            Callable[[str], Coroutine[Any, Any, str | None]] | None
        ) = None

    def set_enqueue_callback(
        self,
        callback: Callable[[str], Coroutine[Any, Any, str | None]] | None,
    ) -> None:
        """Set the durable enqueue callback (design §22.3, §29.16).

        When set, ``check_expired`` enqueues a durable
        ``MEMORY_CONSOLIDATION`` task for each expired session instead of
        scheduling ``_archive`` via ``asyncio.create_task``. The actual
        archival is then owned by a maintenance Worker.
        """
        self._enqueue_callback = callback

    def _is_expired(self, ts: datetime | str | None, now: datetime | None = None) -> bool:
        if self._ttl <= 0 or not ts:
            return False
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        return ((now or datetime.now()) - ts).total_seconds() >= self._ttl * 60

    @staticmethod
    def _format_summary(
        text: str,
        last_active: datetime,
        verbatim: list[str] | None = None,
    ) -> str:
        base = f"Previous conversation summary (last active {last_active.isoformat()}):\n{text}"
        if not verbatim:
            return base
        # 拼接最近用户消息原文(防改写偏离),每条截断到 500 字符
        lines = ["Recent user messages (verbatim):"]
        for msg in verbatim:
            if len(msg) > 500:
                lines.append(msg[:500] + "...")
            else:
                lines.append(msg)
        return base + "\n" + "\n".join(lines)

    def check_expired(
        self,
        schedule_background: Callable[[Coroutine], None],
        active_session_keys: Collection[str] = (),
    ) -> None:
        """Schedule archival for idle sessions, skipping those with in-flight agent tasks.

        When ``enqueue_callback`` is set (durable mode, design §22.3), each
        expired session enqueues a ``MEMORY_CONSOLIDATION`` task instead of
        calling ``schedule_background``. The source revision is the session
        key so repeated submissions deduplicate (design §13.1).
        """
        now = datetime.now()
        for info in self.sessions.list_sessions():
            key = info.get("key", "")
            if not key or key in self._archiving:
                continue
            if key in active_session_keys:
                continue
            if self._is_expired(info.get("updated_at"), now):
                self._archiving.add(key)
                # 先标记再调度，调度失败需回滚标记，避免任务集泄漏
                try:
                    if self._enqueue_callback is not None:
                        # Durable mode: enqueue a MEMORY_CONSOLIDATION task.
                        # Use asyncio.ensure_future to fire-and-forget the
                        # coroutine; errors are logged inside the callback.
                        import asyncio

                        async def _enqueue():
                            try:
                                await self._enqueue_callback(key)
                            except Exception:
                                logger.exception(
                                    "Auto-compact: enqueue failed for {}", key
                                )
                            finally:
                                self._archiving.discard(key)

                        asyncio.ensure_future(_enqueue())
                    else:
                        schedule_background(self._archive(key))
                except Exception:
                    self._archiving.discard(key)
                    raise

    async def _archive(self, key: str) -> None:
        try:
            summary = await self.consolidator.compact_idle_session(
                key,
                self._RECENT_SUFFIX_MESSAGES,
            )
            if summary and summary != "(nothing)":
                session = self.sessions.get_or_create(key)
                meta = session.metadata.get("_last_summary")
                if isinstance(meta, dict):
                    self._summaries[key] = (
                        meta["text"],
                        datetime.fromisoformat(meta["last_active"]),
                    )
        except Exception:
            logger.exception("Auto-compact: failed for {}", key)
        finally:
            self._archiving.discard(key)

    def prepare_session(self, session: Session, key: str) -> tuple[Session, str | None]:
        if key in self._archiving or self._is_expired(session.updated_at):
            logger.info(
                "Auto-compact: reloading session {} (archiving={})", key, key in self._archiving
            )
            session = self.sessions.get_or_create(key)
        # Hot path: summary from in-memory dict (process hasn't restarted).
        entry = self._summaries.pop(key, None)
        if entry:
            return session, self._format_summary(entry[0], entry[1])
        # Cold path: summary persisted in session metadata (process restarted).
        meta = session.metadata.get("_last_summary")
        if isinstance(meta, dict):
            return session, self._format_summary(
                meta["text"], datetime.fromisoformat(meta["last_active"])
            )
        return session, None
