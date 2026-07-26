"""LRU + TTL 结果缓存。

简单的进程内缓存,避免相同 query+backend 短时间内重复请求。
不持久化,进程重启清空。
"""

from __future__ import annotations

import time
from collections import OrderedDict
from threading import Lock
from typing import Any


class SearchCache:
    """线程安全的 LRU + TTL 缓存。"""

    def __init__(self, max_size: int = 256, ttl: int = 3600) -> None:
        self._max_size = max_size
        self._ttl = ttl
        self._store: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = Lock()

    def _make_key(
        self,
        query: str,
        backend: str,
        count: int,
        user_id: str | None = None,
    ) -> str:
        # 可选 user_id:用于按用户/工作区隔离缓存,避免跨用户命中同一缓存
        # (不同用户可能使用不同后端凭证,缓存结果不应共享)。
        parts = [backend, count, query.strip().lower()]
        if user_id:
            parts.append(user_id)
        return ":".join(str(p) for p in parts)

    def get(
        self,
        query: str,
        backend: str,
        count: int,
        user_id: str | None = None,
    ) -> Any | None:
        key = self._make_key(query, backend, count, user_id=user_id)
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            ts, value = entry
            if time.time() - ts > self._ttl:
                # 过期,删除
                self._store.pop(key, None)
                return None
            # LRU 命中,移到末尾
            self._store.move_to_end(key)
            return value

    def set(
        self,
        query: str,
        backend: str,
        count: int,
        value: Any,
        user_id: str | None = None,
    ) -> None:
        key = self._make_key(query, backend, count, user_id=user_id)
        with self._lock:
            self._store[key] = (time.time(), value)
            self._store.move_to_end(key)
            # 淘汰最旧
            while len(self._store) > self._max_size:
                self._store.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
