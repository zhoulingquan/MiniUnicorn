"""Bing 搜索后端 - 免 API Key,使用 Bing 官方 RSS 输出。

Bing 提供 `format=rss` 参数返回结构化 XML,比解析 HTML 稳定得多,
不受 Bing 页面改版影响。国内外都能访问(www.bing.com 在中国有服务器)。
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

from loguru import logger

from miniunicorn.tools.web_search.backends._html_utils import (
    encode_query,
    strip_tags,
)
from miniunicorn.tools.web_search.backends.base import (
    BackendResponse,
    SearchBackend,
    SearchResult,
)

_SEARCH_URL = (
    "https://www.bing.com/search?format=rss&q={query}&count={count}&mkt=zh-CN&setlang=zh-CN"
)


class BingCnBackend(SearchBackend):
    """Bing RSS 后端(国内外通用,免 Key)。"""

    name = "bing_cn"
    requires_api_key = False
    needs_proxy_in_cn = False

    async def search(self, query: str, count: int) -> BackendResponse:
        url = _SEARCH_URL.format(query=encode_query(query), count=min(count * 2, 30))
        try:
            async with self.make_client() as client:
                resp = await client.get(
                    url,
                    headers={
                        "User-Agent": self.user_agent,
                        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                        "Accept": "application/rss+xml, text/xml, application/xml, */*",
                    },
                )
                resp.raise_for_status()
                text = resp.text
        except Exception as e:
            logger.debug("bing_cn search failed: {}", e)
            return BackendResponse(
                backend=self.name, error=f"bing_cn fetch failed: {type(e).__name__}: {e}"
            )

        results = self._parse(text, count)
        if not results:
            return BackendResponse(
                backend=self.name,
                error="bing_cn parse failed: no results (may be blocked or empty response)",
            )
        return BackendResponse(backend=self.name, results=results)

    def _parse(self, xml_text: str, count: int) -> list[SearchResult]:
        """解析 Bing RSS XML。

        结构: <rss><channel><item><title/><link/><description/><pubDate/></item>...</channel></rss>
        """
        results: list[SearchResult] = []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            logger.debug("bing_cn XML parse failed: {}", e)
            return results

        # RSS 2.0: channel/item
        for item in root.iter("item"):
            title_el = item.find("title")
            link_el = item.find("link")
            desc_el = item.find("description")
            title = strip_tags(title_el.text or "") if title_el is not None else ""
            url = (link_el.text or "").strip() if link_el is not None else ""
            snippet = strip_tags(desc_el.text or "") if desc_el is not None else ""
            if not title or not url:
                continue
            results.append(
                SearchResult(
                    title=title,
                    url=url,
                    snippet=snippet,
                    source_backend=self.name,
                )
            )
            if len(results) >= count:
                break
        return results
