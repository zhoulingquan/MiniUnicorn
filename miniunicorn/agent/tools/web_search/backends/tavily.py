"""Tavily AI Search API 后端 - 专为 AI agent 设计的搜索引擎。

Tavily 是专为 LLM/agent 设计的搜索 API,返回结构化结果并附带 LLM 生成的
摘要(answer 字段)和可选的正文(raw_content),非常适合 deep_research。
- 官网: https://tavily.com
- API 文档: https://docs.tavily.com
- MCP 实现: https://github.com/tavily-ai/tavily-mcp
- 免费额度: 1000 次搜索/月

优势:
- 返回 LLM 生成的 answer 字段(自然语言摘要,可直接喂给下游 LLM)
- 支持 search_depth="advanced"(深度搜索,质量更高但耗时长)
- 可选 include_raw_content(返回正文,供 deep_research 使用)
- 结果含 score 字段(0-1 相关性评分)

配置:
    config.web_search.backends.tavily.api_key = "tvly-..."
    (可选)config.web_search.backends.tavily.base_url = "https://api.tavily.com"  # 自定义代理
"""

from __future__ import annotations

from loguru import logger

from miniunicorn.agent.tools.web_search.backends.base import (
    BackendResponse,
    SearchBackend,
    SearchResult,
)

_DEFAULT_BASE_URL = "https://api.tavily.com"
_SEARCH_PATH = "/search"
_ENV_VAR = "TAVILY_API_KEY"


class TavilyBackend(SearchBackend):
    """Tavily AI Search API 后端。

    需要在 config.web_search.backends.tavily.api_key 配置 API Key
    (或设置环境变量 TAVILY_API_KEY)。
    """

    name = "tavily"
    requires_api_key = True
    env_var = _ENV_VAR
    needs_proxy_in_cn = False  # API 直连,通常无需代理
    default_timeout = 15  # 云端 API 偶有抖动,15s 给合理缓冲;超过基本是出问题了

    async def search(self, query: str, count: int) -> BackendResponse:
        api_key = self.get_api_key()
        if not api_key:
            return BackendResponse(
                backend=self.name,
                error=f"tavily: api_key not set (config.web_search.backends.tavily.api_key or env {_ENV_VAR})",
            )

        backend_cfg = self.config.get_backend_config(self.name)
        base_url = (backend_cfg.base_url or _DEFAULT_BASE_URL).rstrip("/")

        # Tavily /search 接口参数
        # - search_depth: "basic"(默认,快)或 "advanced"(深度,质量高但慢)
        # - include_answer: 返回 LLM 生成的自然语言摘要
        # - include_raw_content: 返回正文(deep_research 可用,默认关闭以节省 tokens)
        # - topic: "general"(默认)或 "news"
        payload = {
            "api_key": api_key,
            "query": query,
            "max_results": count,
            "search_depth": "basic",
            "include_answer": True,
            "include_raw_content": False,
            "topic": "general",
        }

        try:
            async with self.make_client() as client:
                resp = await client.post(
                    f"{base_url}{_SEARCH_PATH}",
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            logger.debug("tavily search failed: {}", e)
            return BackendResponse(
                backend=self.name,
                error=f"tavily fetch failed: {type(e).__name__}: {e}",
            )

        results = self._parse(data, count)
        if not results:
            return BackendResponse(
                backend=self.name,
                error="tavily: no results in response",
            )
        return BackendResponse(backend=self.name, results=results)

    def _parse(self, data: dict, count: int) -> list[SearchResult]:
        """解析 Tavily API 响应。

        响应结构(参考 https://docs.tavily.com/api-reference/endpoint/search):
        {
          "query": "...",
          "answer": "...",               # LLM 生成的自然语言摘要(可选)
          "results": [
            {
              "title": "...",
              "url": "...",
              "content": "...",          # 摘要(已为 LLM 优化)
              "raw_content": "...",       # 正文(仅 include_raw_content=true 时)
              "score": 0.95,              # 相关性评分 0-1
              "published_date": "..."     # 可选
            },
            ...
          ],
          "auto_parameters": {...}
        }

        注:answer 字段(若存在)被拼到首条结果的 snippet 前,作为 LLM 摘要优先呈现。
        """
        items = data.get("results") or []
        answer = str(data.get("answer") or "").strip()
        results: list[SearchResult] = []
        for idx, item in enumerate(items[:count]):
            title = str(item.get("title") or "")
            url = str(item.get("url") or "")
            snippet = str(item.get("content") or "")
            if not title or not url:
                continue
            # 首条结果前置 answer 摘要(若有)
            if idx == 0 and answer:
                snippet = f"[AI Summary] {answer}\n\n{snippet}" if snippet else f"[AI Summary] {answer}"
            results.append(
                SearchResult(
                    title=title,
                    url=url,
                    snippet=snippet,
                    source_backend=self.name,
                )
            )
        return results
