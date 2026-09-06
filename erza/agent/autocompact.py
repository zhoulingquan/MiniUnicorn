"""Auto compact: proactive compression of idle sessions to reduce token cost and latency."""

from __future__ import annotations

import time
from collections.abc import Collection
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Callable, Coroutine

from loguru import logger

from erza.session.manager import Session, SessionManager

if TYPE_CHECKING:
    from erza.memory import Consolidator


class AutoCompact:
    _RECENT_SUFFIX_MESSAGES = 8
    # list_sessions() 做全目录 glob + 逐文件扫描, 空闲网关下每秒执行一次代价
    # 过高; 节流为每 30 秒最多扫描一次 (主循环仍每秒轮询消息, 不影响响应)。
    _RESCAN_INTERVAL_S = 30.0
    # 内存摘要条目保留窗口。摘要已持久化在 session.metadata["_last_summary"],
    # 内存 dict 只是热路径缓存, 超期条目可安全清理 (重开会话走冷路径读取)。
    _SUMMARY_RETENTION = timedelta(hours=24)

    def __init__(
        self,
        sessions: SessionManager,
        consolidator: Consolidator,
        session_ttl_minutes: int = 0,
        consolidator_for: Callable[[str], Consolidator] | None = None,
    ):
        self.sessions = sessions
        self.consolidator = consolidator
        self.consolidator_for = consolidator_for
        self._ttl = session_ttl_minutes
        self._archiving: set[str] = set()
        self._summaries: dict[str, tuple[str, datetime]] = {}
        self._last_scan_monotonic = 0.0

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
        """Schedule archival for idle sessions, skipping those with in-flight agent tasks."""
        if self._ttl <= 0:
            return
        now_mono = time.monotonic()
        if now_mono - self._last_scan_monotonic < self._RESCAN_INTERVAL_S:
            return
        self._last_scan_monotonic = now_mono
        now = datetime.now()
        self._prune_summaries(now)
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
                    schedule_background(self._archive(key))
                except Exception:
                    self._archiving.discard(key)
                    raise

    def _prune_summaries(self, now: datetime) -> None:
        """清理长期未重新打开的会话摘要条目, 防止 _summaries 无界增长。

        摘要同时持久化在 session.metadata["_last_summary"], 删除内存条目后
        重开会话仍可通过冷路径取回, 无信息丢失。
        """
        expired = [
            key
            for key, (_, last_active) in self._summaries.items()
            if now - last_active > self._SUMMARY_RETENTION
        ]
        for key in expired:
            del self._summaries[key]

    async def _archive(self, key: str) -> None:
        try:
            consolidator = (
                self.consolidator_for(key)
                if self.consolidator_for is not None
                else self.consolidator
            )
            summary = await consolidator.compact_idle_session(
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
