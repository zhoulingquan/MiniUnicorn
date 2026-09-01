"""SearXNG 元搜索后端 - 自托管,聚合 100+ 搜索引擎,免 API Key。

SearXNG 是一个免费的开源元搜索引擎,可自托管,一次部署即可聚合
Google/Bing/DuckDuckGo 等 100+ 搜索引擎的结果。
- 项目地址: https://github.com/searxng/searxng
- API 文档: https://docs.searxng.org/dev/search_api.html
- 部署后通过 `format=json` 参数返回结构化 JSON

优势:
- 自托管,零成本零 Key,数据不出本地
- 聚合多引擎结果,覆盖面最广
- 支持分类(通用/图片/新闻/IT 等)和语言过滤

配置:
    config.web_search.backends.searxng.base_url = "http://localhost:8080"
    (可选)config.web_search.backends.searxng.api_key = "..." (若启用了 limiter)
"""

from __future__ import annotations

from loguru import logger

from miniunicorn.tools.web_search.backends.base import (
    BackendResponse,
    SearchBackend,
    SearchResult,
)

# 默认无 base_url,必须用户显式配置(自托管实例地址不固定)
_DEFAULT_BASE_URL = ""
_ENV_VAR = ""  # SearXNG 通常无需 Key;预留环境变量位供 limiter 场景


class SearXngBackend(SearchBackend):
    """SearXNG 自托管元搜索后端。

    需要在 config.web_search.backends.searxng.base_url 配置实例地址。
    若实例启用了 bot protection(限流器),需额外配置 api_key。
    """

    name = "searxng"
    requires_api_key = False  # 通常不需要,仅在实例启用 limiter 时需要
    env_var = _ENV_VAR
    needs_proxy_in_cn = False  # 自托管,通常本地可达
    default_timeout = 10  # 本地/局域网实例响应快,10s 足够;挂了应快速失败让其他后端接管

    async def search(self, query: str, count: int) -> BackendResponse:
        backend_cfg = self.config.get_backend_config(self.name)
        base_url = (backend_cfg.base_url or _DEFAULT_BASE_URL).rstrip("/")
        if not base_url:
            return BackendResponse(
                backend=self.name,
                error=(
                    "searxng: base_url not configured "
                    "(set config.web_search.backends.searxng.base_url to your SearXNG instance)"
                ),
            )

        # SearXNG /search 接口参数
        # - format=json: 返回 JSON(实例必须在 settings.yml 启用 search.formats: [html, json])
        # - pageno: 页码,1 起
        # - categories: general(默认)/images/news/it/files
        # - language: zh-CN/en/all
        params = {
            "q": query,
            "format": "json",
            "pageno": 1,
            "categories": "general",
            "language": "all",
        }
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        # 可选 API Key(实例启用 limiter 时)
        api_key = self.get_api_key()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        try:
            async with self.make_client() as client:
                resp = await client.get(
                    f"{base_url}/search",
                    params=params,
                    headers=headers,
                )
                resp.raise_for_status()
                # 部分实例未启用 json 格式会返回 HTML
                content_type = resp.headers.get("content-type", "")
                if "application/json" not in content_type:
                    return BackendResponse(
                        backend=self.name,
                        error=(
                            "searxng: instance did not return JSON "
                            "(enable 'search.formats: [html, json]' in settings.yml)"
                        ),
                    )
                data = resp.json()
        except Exception as e:
            logger.debug("searxng search failed: {}", e)
            return BackendResponse(
                backend=self.name,
                error=f"searxng fetch failed: {type(e).__name__}: {e}",
            )

        results = self._parse(data, count)
        if not results:
            return BackendResponse(
                backend=self.name,
                error="searxng: no results in response (instance may have no enabled engines)",
            )
        return BackendResponse(backend=self.name, results=results)

    def _parse(self, data: dict, count: int) -> list[SearchResult]:
        """解析 SearXNG JSON 响应。

        响应结构(参考 https://docs.searxng.org/dev/search_api.html):
        {
          "query": "...",
          "results": [
            {
              "url": "...",
              "title": "...",
              "content": "...",        # 摘要
              "engine": "google",      # 来源引擎
              "score": 1.0,
              "category": "general",
              "publishedDate": "..."   # 可选
            },
            ...
          ],
          "number_of_results": 123,
          "unresponsive_engines": []
        }
        """
        items = data.get("results") or []
        results: list[SearchResult] = []
        for item in items[:count]:
            title = str(item.get("title") or "")
            url = str(item.get("url") or "")
            snippet = str(item.get("content") or item.get("snippet") or "")
            # 标记来源引擎,便于调试(如 "searxng:google")
            engine = str(item.get("engine") or "")
            source = f"searxng:{engine}" if engine else "searxng"
            if not title or not url:
                continue
            results.append(
                SearchResult(
                    title=title,
                    url=url,
                    snippet=snippet,
                    source_backend=source,
                )
            )
        return results
