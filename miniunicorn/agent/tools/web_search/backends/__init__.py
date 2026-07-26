"""搜索后端实现。

精简为以 SearXNG 为主力的三层架构:
- searxng: 自托管元搜索,聚合 Google/Bing/DDG 等 100+ 引擎(主力)
- tavily: AI Search API,提供 LLM 摘要增强(deep_research 用)
- bing_cn: 国内免 Key 兜底(SearXNG 实例不可用时降级)
"""

from miniunicorn.agent.tools.web_search.backends.base import SearchBackend, SearchResult
from miniunicorn.agent.tools.web_search.backends.bing_cn import BingCnBackend
from miniunicorn.agent.tools.web_search.backends.searxng import SearXngBackend
from miniunicorn.agent.tools.web_search.backends.tavily import TavilyBackend

# 后端注册表: name -> Backend 类
# 注册顺序即默认发现顺序,合并优先级由 aggregator.BACKEND_PRIORITY 控制
BACKEND_REGISTRY: dict[str, type[SearchBackend]] = {
    # 主力:自托管元搜索,聚合 100+ 引擎,稳定且零成本(需配置 base_url)
    "searxng": SearXngBackend,
    # AI 摘要增强:返回 LLM 生成的 answer 字段,snippet 质量最高(需 Key)
    "tavily": TavilyBackend,
    # 国内免 Key 兜底:SearXNG 实例不可用时降级使用
    "bing_cn": BingCnBackend,
}

__all__ = [
    "SearchResult",
    "SearchBackend",
    "BACKEND_REGISTRY",
    "SearXngBackend",
    "TavilyBackend",
    "BingCnBackend",
]
