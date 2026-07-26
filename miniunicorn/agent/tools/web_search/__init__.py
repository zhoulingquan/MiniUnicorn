"""独立 web_search 模块。

与 web_fetch 解耦,负责关键词搜索,以 SearXNG 为主力聚合多家搜索引擎后端。
- searxng: 自托管元搜索,聚合 100+ 引擎(主力,需配置 base_url)
- tavily: AI Search API,提供 LLM 摘要增强(需 Key)
- bing_cn: 国内免 Key 兜底(SearXNG 不可用时降级)

详见 `tool.py` 的 WebSearchTool。
"""

from miniunicorn.agent.tools.web_search.tool import WebSearchTool

__all__ = ["WebSearchTool"]
