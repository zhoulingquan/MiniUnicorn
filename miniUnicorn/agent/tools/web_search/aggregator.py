"""搜索聚合器 - 首成功返回 + 后台补缓存 + 单后端兜底。

核心职责:
1. auto 模式: 并发调用所有已注册后端,首个成功即返回(方案A),
   剩余后端后台跑完写入缓存供下次命中。
   - searxng(主力,需配置) + tavily(AI 摘要,需 Key) + bing_cn(免 Key 兜底)
   - 海外后端通过系统代理或 config.proxy 走代理;无代理时自然失败被跳过
2. 单后端模式: provider != "auto" 时只调一个后端
3. 缓存: 命中即返回,失败不缓存(单后端模式下生效)
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit, urlunsplit

from loguru import logger

from miniUnicorn.agent.tools.web_search.backends import BACKEND_REGISTRY
from miniUnicorn.agent.tools.web_search.backends.base import (
    BackendResponse,
    SearchBackend,
    SearchResult,
)
from miniUnicorn.agent.tools.web_search.cache import SearchCache
from miniUnicorn.agent.tools.web_search.config import DEFAULT_CACHE_TTL_S, WebSearchConfig
from miniUnicorn.security.workspace_access import current_workspace_scope

# 后端结果合并优先级(按单条结果质量从高到低)。
# 并发模式下,所有后端同时跑;合并去重时排在前面的后端结果优先占位。
# 精简为以 SearXNG 为主力的三层架构:
# 1. searxng: 自托管元搜索,聚合 100+ 引擎,稳定零成本(主力,需配置 base_url)
# 2. tavily: AI Search API,含 LLM 摘要 answer 字段,snippet 最相关(需 Key)
# 3. bing_cn: Bing RSS,国内免 Key 兜底,SearXNG 不可用时降级
BACKEND_PRIORITY: tuple[str, ...] = (
    "searxng",  # 主力:自托管元搜索,聚合 100+ 引擎(需配置 base_url)
    "tavily",   # AI 摘要增强:含 LLM summary,snippet 质量最高(需 Key)
    "bing_cn",  # 国内免 Key 兜底:Bing RSS,SearXNG 不可用时降级
)


class SearchAggregator:
    """搜索聚合器。"""

    def __init__(self, config: WebSearchConfig) -> None:
        self.config = config
        # 缓存始终启用,TTL 由内部常量控制(不暴露给 UI)
        self.cache = SearchCache(ttl=DEFAULT_CACHE_TTL_S)
        # 后端实例缓存: name -> SearchBackend
        self._backend_instances: dict[str, SearchBackend] = {}

    def _get_backend(self, name: str) -> SearchBackend | None:
        """获取后端实例(单例)。"""
        if name in self._backend_instances:
            return self._backend_instances[name]
        cls = BACKEND_REGISTRY.get(name)
        if cls is None:
            logger.warning("Unknown search backend: {}", name)
            return None
        try:
            instance = cls(self.config)
            self._backend_instances[name] = instance
            return instance
        except Exception:
            logger.exception("Failed to instantiate backend: {}", name)
            return None

    async def search(
        self,
        query: str,
        count: int | None = None,
        backend: str | None = None,
    ) -> BackendResponse:
        """执行搜索。

        - backend 显式指定且非 auto 时,只调这一个后端
        - backend 为 None / "auto" 时,并发调用所有已注册后端,合并去重
        """
        n = min(max(count or self.config.max_results, 1), 10)
        provider = (backend or self.config.provider or "auto").strip().lower()

        # 单后端模式
        if provider not in ("", "auto"):
            return await self._search_single(query, n, provider)

        # 并发聚合模式
        return await self._search_concurrent(query, n)

    async def _search_single(
        self,
        query: str,
        count: int,
        backend_name: str,
    ) -> BackendResponse:
        """调用单个后端(带缓存)。"""
        # 取当前工作区标识用于缓存隔离(若无可用的 workspace scope 则返回 None,
        # 保持原有跨工作区共享缓存的行为,向后兼容)。
        user_id = self._current_cache_user_id()

        # 1. 命中缓存
        if self.cache is not None:
            cached = self.cache.get(query, backend_name, count, user_id=user_id)
            if cached is not None:
                return BackendResponse(
                    backend=backend_name,
                    results=cached,
                    from_cache=True,
                )

        # 2. 调用后端(经熔断器包装)
        backend = self._get_backend(backend_name)
        if backend is None:
            return BackendResponse(
                backend=backend_name,
                error=f"backend '{backend_name}' not registered",
            )
        resp = await backend.safe_search(query, count)

        # 3. 成功才缓存
        if resp.ok and self.cache is not None:
            self.cache.set(query, backend_name, count, resp.results, user_id=user_id)
        return resp

    @staticmethod
    def _current_cache_user_id() -> str | None:
        """获取当前工作区标识,用作缓存隔离的 user_id。

        从 ``current_workspace_scope()`` 取 ``project_name`` 作为隔离键:
        不同工作区(可能对应不同用户/项目)的搜索缓存互不污染,
        避免跨工作区命中同一缓存导致的凭证/结果泄露。
        若无可用 scope(例如未绑定上下文),返回 None 保持向后兼容。
        """
        scope = current_workspace_scope()
        if scope is None:
            return None
        try:
            return scope.project_name
        except Exception:
            return None

    async def _search_concurrent(
        self,
        query: str,
        count: int,
    ) -> BackendResponse:
        """并发调用所有已注册后端,按 aggregate_mode 策略返回。

        - "fast" (默认): 首个成功后端返回,其余后台补缓存(低延迟)
        - "full": 等所有后端返回或超时,全量合并去重(高质量)
        - "hybrid": 首成功返回初步结果,后台继续聚合,下次查询返回增强结果
          (语义等价于 fast,显式列出便于将来扩展)

        - 任一后端失败被跳过(不影响其他后端)
        - 全部失败时返回聚合错误
        - 按 BACKEND_PRIORITY 顺序合并去重,截取 count 条
        """
        mode = (self.config.aggregate_mode or "fast").strip().lower()
        if mode == "full":
            return await self._search_full(query, count)
        # fast / hybrid / 未知值 均走首成功返回逻辑(hybrid 与 fast 行为一致,
        # 区别仅在语义:hybrid 暗示"将来会增强",当前实现相同)
        return await self._search_first_success(query, count)

    async def _search_first_success(
        self,
        query: str,
        count: int,
    ) -> BackendResponse:
        """fast/hybrid 模式:首个成功后端返回,其余后台补缓存。"""
        backend_names = list(BACKEND_REGISTRY.keys())
        tasks: dict[str, asyncio.Future] = {
            name: asyncio.ensure_future(self._search_single(query, count, name))
            for name in backend_names
        }
        ok_responses: dict[str, BackendResponse] = {}
        failed: list[str] = []
        pending: set[asyncio.Future] = set(tasks.values())

        try:
            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    name = next(n for n, t in tasks.items() if t is task)
                    try:
                        resp = task.result()
                    # 使用 Exception 而非 BaseException，避免吞掉 CancelledError
                    except Exception as exc:
                        failed.append(f"{name}: {type(exc).__name__}: {exc}")
                        continue
                    if resp.ok and resp.results:
                        ok_responses[name] = resp
                        break
                    failed.append(f"{name}: {resp.error or 'no results'}")
                else:
                    # 内层 for 正常结束(无成功响应),继续等下一批
                    continue
                # 内层 for 被 break 打断(已拿到首个成功响应)
                break

            # 剩余后端后台跑完写入缓存(不阻塞当前请求)
            if ok_responses and pending:
                asyncio.ensure_future(
                    self._drain_pending(tasks, pending, query, count)
                )

            if not ok_responses:
                return BackendResponse(
                    backend="auto",
                    error=f"all backends failed: {'; '.join(failed)}",
                )

            merged = _merge_dedupe(ok_responses, count)

            if failed:
                logger.debug(
                    "web_search concurrent: first-success={}, {} backends failed: {}",
                    list(ok_responses.keys()),
                    len(failed),
                    "; ".join(failed),
                )

            return BackendResponse(
                backend="auto",
                results=merged,
                from_cache=any(r.from_cache for r in ok_responses.values()),
            )
        except Exception:
            for task in tasks.values():
                if not task.done():
                    task.cancel()
            raise

    async def _search_full(
        self,
        query: str,
        count: int,
    ) -> BackendResponse:
        """full 模式:等所有后端返回或超时,全量合并去重。

        适合 deep_research 等对结果完整性要求高的场景。
        超时控制复用 config.timeout(单个后端超时),整体等待时间不超过
        max(backend_timeout) + 1s 缓冲。
        """
        backend_names = list(BACKEND_REGISTRY.keys())
        # 整体超时:单个后端超时 + 1s 缓冲(防止慢后端拖死)
        overall_timeout = float(self.config.timeout) + 1.0
        tasks: dict[str, asyncio.Future] = {
            name: asyncio.ensure_future(self._search_single(query, count, name))
            for name in backend_names
        }
        ok_responses: dict[str, BackendResponse] = {}
        failed: list[str] = []

        try:
            # 等所有任务完成(或整体超时)
            done, pending = await asyncio.wait(
                set(tasks.values()),
                timeout=overall_timeout,
                return_when=asyncio.ALL_COMPLETED,
            )
            # 超时未完成的任务取消
            for task in pending:
                task.cancel()
                name = next(n for n, t in tasks.items() if t is task)
                failed.append(f"{name}: timeout")

            for task in done:
                name = next(n for n, t in tasks.items() if t is task)
                try:
                    resp = task.result()
                # 使用 Exception 而非 BaseException，避免吞掉 CancelledError
                except Exception as exc:
                    failed.append(f"{name}: {type(exc).__name__}: {exc}")
                    continue
                if resp.ok and resp.results:
                    ok_responses[name] = resp
                else:
                    failed.append(f"{name}: {resp.error or 'no results'}")

            if not ok_responses:
                return BackendResponse(
                    backend="auto",
                    error=f"all backends failed: {'; '.join(failed)}",
                )

            merged = _merge_dedupe(ok_responses, count)

            if failed:
                logger.debug(
                    "web_search full aggregate: {} ok, {} failed: {}",
                    list(ok_responses.keys()),
                    len(failed),
                    "; ".join(failed),
                )

            return BackendResponse(
                backend="auto",
                results=merged,
                from_cache=any(r.from_cache for r in ok_responses.values()),
            )
        except Exception:
            for task in tasks.values():
                if not task.done():
                    task.cancel()
            raise

    async def _drain_pending(
        self,
        tasks: dict[str, asyncio.Future],
        pending: set[asyncio.Future],
        query: str,
        count: int,
    ) -> None:
        """后台等待剩余后端任务完成,结果通过 _search_single 自动写入缓存。"""
        try:
            await asyncio.gather(*pending, return_exceptions=True)
        except Exception:
            logger.debug("web_search background drain interrupted")

    def invalidate_cache(self) -> None:
        """清空缓存。"""
        if self.cache is not None:
            self.cache.clear()


def _normalize_url(url: str) -> str:
    """URL 标准化用于去重:去 fragment、去 trailing slash、转小写 host。

    保留 query string,因为同一 URL 不同 query 可能指向不同内容;
    但去掉 fragment(锚点不影响内容)。
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()
    if not parts.netloc:
        return url.strip().lower()
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            path,
            parts.query,
            "",  # 丢弃 fragment
        )
    )


def _merge_dedupe(
    responses: dict[str, BackendResponse],
    count: int,
) -> list[SearchResult]:
    """按 BACKEND_PRIORITY 顺序合并多个后端的结果,URL 去重,截取 count 条。

    - 同一 URL 只保留首次出现(优先级高的后端优先)
    - 单个后端内部原本就按相关性排序,保留其相对顺序
    """
    seen: set[str] = set()
    merged: list[SearchResult] = []

    # 优先级顺序,未在 BACKEND_PRIORITY 中的后端追加在末尾(按响应字典插入顺序)
    ordered_names = [n for n in BACKEND_PRIORITY if n in responses]
    ordered_names += [n for n in responses if n not in BACKEND_PRIORITY]

    for name in ordered_names:
        resp = responses[name]
        for result in resp.results:
            key = _normalize_url(result.url)
            if not key or key in seen:
                continue
            seen.add(key)
            # 标记来源后端(便于调试和展示)
            if not result.source_backend:
                result.source_backend = name
            merged.append(result)
            if len(merged) >= count:
                return merged
    return merged
