"""独立 deep_research 模块。

工作流:Plan(拆查询) -> Search(并发搜索) -> Reflect(补查询) -> Write(成报告)
依赖 web_search 的 SearchAggregator(降级链/缓存复用),与 web_fetch 解耦。
"""

from miniunicorn.tools.deep_research.tool import DeepResearchTool

__all__ = ["DeepResearchTool"]
