"""DeepResearchTool - deep_research 工具入口。

工作流:Plan(拆查询) -> Search(并发搜索) -> Fetch(抓正文,Phase 2) -> Reflect(补查询) -> Write(成报告)。

设计原则:
- 独立模块,与 web_fetch / web_search 解耦,便于升级维护。
- 复用 web_search 的 SearchAggregator(降级链、缓存、后端选择)。
- LLM 调用复用主对话 provider(通过 provider_snapshot_loader)。
- Phase 2 起:搜索后对 top-N 结果调用 web_fetch 抓取正文,大幅提升报告信息密度。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import TYPE_CHECKING, Any

from loguru import logger

from miniunicorn.agent.call_ledger import CallPurpose, call_purpose
from miniunicorn.agent.tools.base import Tool, tool_parameters
from miniunicorn.agent.tools.deep_research.config import DeepResearchConfig
from miniunicorn.agent.tools.deep_research.prompts import (
    PLAN_SYSTEM,
    PLAN_USER,
    REFLECT_SYSTEM,
    REFLECT_USER,
    WRITE_SYSTEM,
    WRITE_USER,
)
from miniunicorn.agent.tools.deep_research.reranker import rerank_per_query
from miniunicorn.agent.tools.schema import IntegerSchema, StringSchema, tool_parameters_schema
from miniunicorn.agent.tools.web import WebFetchTool
from miniunicorn.agent.tools.web_search.aggregator import SearchAggregator
from miniunicorn.agent.tools.web_search.config import WebSearchConfig

if TYPE_CHECKING:
    from miniunicorn.providers.base import LLMProvider


# 提取 LLM 返回的 JSON 数组(容忍前后多余文本)
_JSON_ARRAY_RE = re.compile(r"\[\s*(?:\".*?\"\s*,?\s*)+\s*\]", re.DOTALL)

# 单条查询长度上限(字符):超出则截断,避免单条 query 携带过多内容挤占上下文
_MAX_QUERY_LEN = 200
# 解析出的查询总数上限:超出部分丢弃,防止 LLM 生成过量查询拖慢研究流程
_MAX_QUERY_COUNT = 10


class LLMCallError(RuntimeError):
    """LLM 调用失败(网络/鉴权/模型错误等)。"""


@tool_parameters(
    tool_parameters_schema(
        query=StringSchema("Research topic or question to investigate"),
        max_queries=IntegerSchema(
            5,
            minimum=1,
            maximum=10,
            description="Max number of web searches to perform (default 5).",
        ),
        required=["query"],
    )
)
class DeepResearchTool(Tool):
    """Plan -> Search -> Reflect -> Write workflow for in-depth research."""

    _scopes = {"core", "subagent"}

    name = "deep_research"
    description = (
        "Run a multi-step research workflow on a topic: plan search queries, "
        "execute them in parallel via web_search, reflect on whether more "
        "queries are needed, then write a structured Markdown report with "
        "inline citations and a Sources list. Use this for complex questions "
        "that need synthesis across multiple sources; for a single lookup use "
        "web_search directly."
    )

    config_key = "deep_research"

    @classmethod
    def config_cls(cls):
        return DeepResearchConfig

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        cfg = getattr(ctx.config, "deep_research", None)
        if cfg is not None and not cfg.enable:
            return False
        # web_search 必须可用(否则无搜索能力)
        ws_cfg = getattr(ctx.config, "web_search", None)
        return ws_cfg is None or ws_cfg.enable

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        deep_cfg = getattr(ctx.config, "deep_research", None) or DeepResearchConfig()
        ws_cfg = getattr(ctx.config, "web_search", None) or WebSearchConfig()

        provider = None
        model = None
        loader = getattr(ctx, "provider_snapshot_loader", None)
        if callable(loader):
            try:
                snapshot = loader()
            except Exception:
                snapshot = None
            if snapshot is not None:
                provider = getattr(snapshot, "provider", None)
                model = getattr(snapshot, "model", None)

        # 复用已注册的 WebFetchTool 实例(若 ctx.tool_registry 可用)
        web_fetch_tool = None
        tool_registry = getattr(ctx, "tool_registry", None)
        if tool_registry is not None:
            try:
                web_fetch_tool = tool_registry.get("web_fetch")
            except Exception:
                web_fetch_tool = None

        return cls(
            config=deep_cfg,
            web_search_config=ws_cfg,
            provider=provider,
            model=model,
            web_fetch=web_fetch_tool,
        )

    def __init__(
        self,
        config: DeepResearchConfig | None = None,
        web_search_config: WebSearchConfig | None = None,
        provider: "LLMProvider | None" = None,
        model: str | None = None,
        web_fetch: WebFetchTool | None = None,
    ) -> None:
        self.config = config or DeepResearchConfig()
        self.web_search_config = web_search_config or WebSearchConfig()
        self._provider = provider
        self._model = model
        # 复用同一 aggregator:共享缓存与降级链
        self.aggregator = SearchAggregator(self.web_search_config)
        # web_fetch 工具实例(用于 Phase 2 全文抓取)
        # 若未传入,延迟初始化(避免循环依赖,且仅在 enable_fetch=True 时需要)
        self._web_fetch = web_fetch

    @property
    def read_only(self) -> bool:
        return True

    @property
    def compactable(self) -> bool:
        # 报告内容较长且含引用,不应被压缩
        return False

    @property
    def cacheable(self) -> bool:
        # 每次研究的查询都可能不同,且 LLM 输出有随机性,不做结果缓存
        return False

    @property
    def exclusive(self) -> bool:
        # 内部已并发执行搜索;且占满 LLM 调度,串行避免资源争抢
        return True

    async def execute(
        self,
        query: str,
        max_queries: int | None = None,
        **kwargs: Any,
    ) -> str:
        if not query or not query.strip():
            return json.dumps({"error": "query is required"}, ensure_ascii=False)
        if self._provider is None:
            return json.dumps(
                {"error": "LLM provider not available", "query": query},
                ensure_ascii=False,
            )

        # 覆盖配置:运行时传入的 max_queries 优先(受 config.max_queries 上限)
        budget = self.config.max_queries
        if max_queries is not None:
            budget = min(max(1, int(max_queries)), self.config.max_queries)

        topic = query.strip()
        try:
            return await asyncio.wait_for(
                self._run_research(topic, budget),
                timeout=self.config.overall_timeout_s,
            )
        except asyncio.TimeoutError:
            return json.dumps(
                {
                    "error": f"research timed out after {self.config.overall_timeout_s}s",
                    "query": topic,
                },
                ensure_ascii=False,
            )
        except Exception as e:
            logger.exception("deep_research failed for query: {}", topic)
            return json.dumps(
                {
                    "error": f"research failed: {type(e).__name__}: {e}",
                    "query": topic,
                },
                ensure_ascii=False,
            )

    async def _run_research(self, topic: str, budget: int) -> str:
        """执行完整 Plan -> Search -> Reflect -> Write 流程。"""
        # 1. Plan
        initial_n = min(self.config.initial_queries, budget)
        try:
            plan_queries = await self._plan(topic, initial_n)
        except LLMCallError as e:
            return json.dumps(
                {"error": f"plan step LLM failed: {e}", "query": topic},
                ensure_ascii=False,
            )
        if not plan_queries:
            return json.dumps(
                {"error": "plan step produced no queries", "query": topic},
                ensure_ascii=False,
            )

        # 2. Search
        executed: list[str] = []
        results: list[dict[str, Any]] = []
        await self._search_batch(plan_queries, executed, results, budget)

        # 2.5 Fetch(可选,Phase 2): 抓取 top-N 结果正文,提升报告信息密度
        if self.config.enable_fetch and self.config.max_fetch_per_query > 0:
            await self._fetch_top_results(results)

        # 3. Reflect (可选,多轮循环,受 reflect_rounds 与 budget 双重限制)
        if self.config.enable_reflect and self.config.reflect_rounds > 0:
            rounds_done = 0
            while rounds_done < self.config.reflect_rounds:
                remaining = budget - len(executed)
                if remaining <= 0:
                    break
                extra = await self._reflect(topic, executed, results, remaining)
                if not extra:
                    # SUFFICIENT 或 LLM 失败,停止反思
                    break
                await self._search_batch(extra, executed, results, budget)
                rounds_done += 1

        if not results:
            return json.dumps(
                {
                    "error": "no search results obtained",
                    "query": topic,
                    "executed_queries": executed,
                },
                ensure_ascii=False,
            )

        # 3.5 Rerank(可选): 按 query 相关性重排,过滤低分结果
        if self.config.enable_rerank:
            before_count = len(results)
            results = rerank_per_query(
                results,
                min_score=self.config.rerank_min_score,
            )
            if len(results) < before_count:
                logger.info(
                    "deep_research rerank: {}/{} results kept after rerank",
                    len(results),
                    before_count,
                )

        # 4. Write
        try:
            report = await self._write(topic, results)
        except LLMCallError as e:
            return json.dumps(
                {
                    "error": f"write step LLM failed: {e}",
                    "query": topic,
                    "executed_queries": executed,
                    "total_results": len(results),
                },
                ensure_ascii=False,
            )
        if not report:
            return json.dumps(
                {
                    "error": "write step produced empty report",
                    "query": topic,
                    "executed_queries": executed,
                    "total_results": len(results),
                },
                ensure_ascii=False,
            )

        # 附带执行元信息,便于排查
        meta = {
            "query": topic,
            "executed_queries": executed,
            "total_results": len(results),
            "report": report,
        }
        return json.dumps(meta, ensure_ascii=False)

    async def _plan(self, topic: str, n: int) -> list[str]:
        """让 LLM 把主题拆成 n 条具体查询。失败抛 LLMCallError。"""
        system = PLAN_SYSTEM.format(N=n)
        user = PLAN_USER.format(topic=topic, N=n)
        raw = await self._llm_chat(system, user, max_tokens=self.config.plan_max_tokens)
        queries = self._parse_query_list(raw)
        if not queries:
            logger.warning(
                "deep_research plan: LLM returned no parseable queries for topic: {}",
                topic,
            )
        return queries

    async def _reflect(
        self,
        topic: str,
        executed: list[str],
        results: list[dict[str, Any]],
        extra_budget: int,
    ) -> list[str]:
        """让 LLM 看已有结果,决定是否补充查询。

        LLM 失败时返回空列表(不阻断后续 Write 流程,用已有材料写报告)。
        """
        if extra_budget <= 0:
            return []
        system = REFLECT_SYSTEM.format(extra=extra_budget)
        digest = self._build_results_digest(results, max_items=8)
        user = REFLECT_USER.format(
            topic=topic,
            executed_queries="\n".join(f"- {q}" for q in executed) or "(none)",
            results_digest=digest or "(no results yet)",
            extra=extra_budget,
        )
        try:
            raw = await self._llm_chat(system, user, max_tokens=self.config.plan_max_tokens)
        except LLMCallError as e:
            logger.warning("deep_research reflect: LLM failed, skip reflection: {}", e)
            return []
        if not raw:
            return []
        if "SUFFICIENT" in raw.upper():
            return []
        queries = self._parse_query_list(raw)
        # 去重:不重复已执行过的
        seen = set(executed)
        deduped: list[str] = []
        for q in queries:
            if q and q not in seen:
                seen.add(q)
                deduped.append(q)
            if len(deduped) >= extra_budget:
                break
        return deduped

    async def _write(self, topic: str, results: list[dict[str, Any]]) -> str:
        """基于所有搜索结果生成 Markdown 报告。失败抛 LLMCallError。"""
        block = self._build_results_block(results)
        user = WRITE_USER.format(topic=topic, results_block=block)
        report = await self._llm_chat(
            WRITE_SYSTEM,
            user,
            max_tokens=self.config.write_max_tokens,
        )
        if not report:
            logger.warning("deep_research write: LLM produced empty report for topic: {}", topic)
        return report

    async def _search_batch(
        self,
        queries: list[str],
        executed: list[str],
        results: list[dict[str, Any]],
        budget: int,
    ) -> None:
        """并发执行一批查询,把结果汇总到 results 里(受 budget 限制)。"""
        # 受 budget 限制:已执行 + 待执行 不超过 budget
        remaining = budget - len(executed)
        if remaining <= 0:
            return
        todo = queries[:remaining]
        if not todo:
            return

        tasks = [self.aggregator.search(q, self.config.per_query_results) for q in todo]
        responses = await asyncio.gather(*tasks, return_exceptions=False)

        for q, resp in zip(todo, responses):
            executed.append(q)
            if not resp.ok:
                logger.warning("deep_research search failed: {} -> {}", q, resp.error)
                continue
            for r in resp.results:
                results.append(
                    {
                        "query": q,
                        "title": r.title,
                        "url": r.url,
                        "snippet": self._truncate(r.snippet, self.config.snippet_max_chars),
                    }
                )

    async def _fetch_top_results(self, results: list[dict[str, Any]]) -> None:
        """对每个查询的 top-N 结果抓取正文,填充到 result["content"] 字段。

        - 仅抓取每个 query 的前 N 条(N=config.max_fetch_per_query)
        - 并发抓取,复用 WebFetchTool.execute_batch
        - 失败的结果保留原 snippet,不阻断流程
        - 抓取到的正文截断到 content_max_chars
        """
        fetch_tool = self._get_web_fetch()
        if fetch_tool is None:
            logger.debug("deep_research fetch: web_fetch tool unavailable, skip fetching")
            return

        # 按 query 分组,每组取前 N 条
        per_query_count: dict[str, int] = {}
        to_fetch: list[tuple[int, dict[str, Any]]] = []  # (idx, result)
        for idx, r in enumerate(results):
            q = r.get("query") or ""
            count = per_query_count.get(q, 0)
            if count >= self.config.max_fetch_per_query:
                continue
            per_query_count[q] = count + 1
            url = (r.get("url") or "").strip()
            if url:
                to_fetch.append((idx, r))

        if not to_fetch:
            return

        # 复用 WebFetchTool.execute_batch 批量抓取
        urls = [r["url"] for _, r in to_fetch]
        fetched = await fetch_tool.execute_batch(
            urls,
            max_chars=self.config.content_max_chars,
            concurrency=self.config.fetch_concurrency,
            timeout_s=float(self.config.fetch_timeout_s),
        )

        # 回填到 results(fetched 顺序与 urls 一致)
        fetched_count = 0
        for (idx, _), (url, content) in zip(to_fetch, fetched):
            if content:
                results[idx]["content"] = self._truncate(content, self.config.content_max_chars)
                fetched_count += 1
        logger.info(
            "deep_research fetch: {}/{} URLs fetched successfully",
            fetched_count,
            len(to_fetch),
        )

    def _get_web_fetch(self) -> WebFetchTool | None:
        """获取 WebFetchTool 实例(延迟初始化)。"""
        if self._web_fetch is not None:
            return self._web_fetch
        # 若未通过 ctx 注入,尝试直接实例化(用默认配置)
        try:
            self._web_fetch = WebFetchTool()
            return self._web_fetch
        except Exception as e:
            logger.warning("deep_research: failed to init WebFetchTool: {}", e)
            return None

    async def _llm_chat(self, system: str, user: str, max_tokens: int) -> str:
        """调用 LLM 返回纯文本。

        失败时抛 LLMCallError,上层捕获后决定是否中止流程。
        这样可区分"LLM 真返回空串"与"调用异常"。
        """
        if self._provider is None:
            raise LLMCallError("LLM provider not available")
        try:
            model = self._model or self._provider.get_default_model()
            async with call_purpose(CallPurpose.TOOL):
                response = await self._provider.chat_with_retry(
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    tools=None,
                    model=model,
                    max_tokens=max_tokens,
                    temperature=self.config.temperature,
                )
            return (response.content or "").strip()
        except Exception as e:
            logger.exception("deep_research LLM call failed")
            raise LLMCallError(f"{type(e).__name__}: {e}") from e

    @staticmethod
    def _truncate(text: str, max_chars: int) -> str:
        if not text:
            return ""
        if len(text) <= max_chars:
            return text
        return text[: max_chars - 1].rstrip() + "…"

    @staticmethod
    def _parse_query_list(raw: str) -> list[str]:
        """从 LLM 输出中提取查询列表。

        优先尝试整段 JSON 解析,失败则用正则找第一个 JSON 数组。

        安全限制:
        - 每条 query 长度超过 ``_MAX_QUERY_LEN`` 时截断;
        - 列表总数超过 ``_MAX_QUERY_COUNT`` 时丢弃超出部分。
        """
        if not raw:
            return []
        text = raw.strip()
        # 去掉常见代码围栏
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
            text = re.sub(r"\n?```$", "", text)
            text = text.strip()
        queries: list[str] = []
        # 尝试直接解析
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                queries = [str(x).strip() for x in parsed if str(x).strip()]
        except json.JSONDecodeError:
            pass
        # 正则兜底
        if not queries:
            match = _JSON_ARRAY_RE.search(text)
            if match:
                try:
                    parsed = json.loads(match.group(0))
                    if isinstance(parsed, list):
                        queries = [str(x).strip() for x in parsed if str(x).strip()]
                except json.JSONDecodeError:
                    pass
        if not queries:
            return []
        # 单条 query 长度截断,超出 _MAX_QUERY_LEN 的部分截掉
        truncated = [(q if len(q) <= _MAX_QUERY_LEN else q[:_MAX_QUERY_LEN]) for q in queries]
        # 列表总数上限:只保留前 _MAX_QUERY_COUNT 条
        return truncated[:_MAX_QUERY_COUNT]

    @staticmethod
    def _build_results_digest(results: list[dict[str, Any]], max_items: int) -> str:
        """给 Reflect 阶段用的精简结果摘要。"""
        if not results:
            return ""
        lines: list[str] = []
        for r in results[:max_items]:
            title = (r.get("title") or "").strip()
            snippet = (r.get("snippet") or "").strip()[:160]
            lines.append(f"- {title}: {snippet}")
        if len(results) > max_items:
            lines.append(f"... and {len(results) - max_items} more results")
        return "\n".join(lines)

    @staticmethod
    def _build_results_block(results: list[dict[str, Any]]) -> str:
        """给 Write 阶段用的完整结果列表(带 idx)。

        若结果含 content 字段(Phase 2 全文抓取),优先使用 content;
        否则回退到 snippet。
        """
        lines: list[str] = []
        for idx, r in enumerate(results, start=1):
            q = (r.get("query") or "").strip()
            title = (r.get("title") or "").strip()
            url = (r.get("url") or "").strip()
            # 优先 content(全文),回退 snippet(摘要)
            content = (r.get("content") or "").strip()
            snippet = (r.get("snippet") or "").strip()
            body = content or snippet
            if content and snippet:
                # 两者都有时,snippet 作为简短描述,content 作为正文
                lines.append(
                    f"[{idx}] {q} | {title} | {url}\n  Summary: {snippet}\n  Content: {body}"
                )
            else:
                lines.append(f"[{idx}] {q} | {title} | {url} | {body}")
        return "\n".join(lines)
