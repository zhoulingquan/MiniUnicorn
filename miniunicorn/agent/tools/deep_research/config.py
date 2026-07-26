"""deep_research 独立配置 schema。

挂在 ToolsConfig.deep_research,与 web_search 平级,便于独立升级维护。
"""

from __future__ import annotations

from pydantic import Field

from miniunicorn.config.schema import Base


class DeepResearchConfig(Base):
    """deep_research 工具配置。"""

    enable: bool = True
    # 单次研究最多发起多少次搜索(Plan + Reflect 补充查询合计上限)
    max_queries: int = Field(default=5, ge=1, le=10)
    # 每次搜索返回多少条结果
    per_query_results: int = Field(default=5, ge=1, le=10)
    # Plan 阶段一次性生成多少条候选查询(<= max_queries)
    initial_queries: int = Field(default=3, ge=1, le=10)
    # 是否启用 Reflect 阶段(根据已有结果决定是否补充搜索)
    enable_reflect: bool = True
    # Reflect 阶段最多追加多少次搜索
    reflect_rounds: int = Field(default=1, ge=0, le=3)
    # 最终报告的最大 token 数
    write_max_tokens: int = Field(default=4096, ge=512, le=16384)
    # Plan/Reflect 步骤的 LLM 最大 token 数
    plan_max_tokens: int = Field(default=1024, ge=256, le=4096)
    # LLM 温度
    temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    # 单次搜索结果包含的 snippet 最大字符数(超出截断)
    snippet_max_chars: int = Field(default=400, ge=50, le=2000)
    # 单次研究的超时秒数(整体)
    overall_timeout_s: int = Field(default=180, ge=30, le=600)
    # ====== Phase 2: 全文抓取增强 ======
    # 是否在搜索后抓取 top-N 结果的正文(基于 web_fetch + Jina Reader)
    enable_fetch: bool = Field(default=True)
    # 每次搜索后抓取多少条结果的正文(取前 N 条,按搜索排序)
    max_fetch_per_query: int = Field(default=3, ge=0, le=10)
    # 单条正文最大字符数(超出截断,避免 tokens 爆炸)
    content_max_chars: int = Field(default=2000, ge=200, le=8000)
    # 抓取并发数(同时抓多少个 URL)
    fetch_concurrency: int = Field(default=5, ge=1, le=10)
    # 单次抓取超时秒数
    fetch_timeout_s: int = Field(default=20, ge=5, le=60)
    # ====== Phase 3: 结果重排 ======
    # 是否对搜索结果按 query 相关性重排(基于 token 重叠,无需 LLM)
    enable_rerank: bool = Field(default=False)
    # 重排最低分数阈值(0-1,低于此分数的结果被过滤)
    rerank_min_score: float = Field(default=0.1, ge=0.0, le=1.0)
    # ====== Phase 4: 多 agent 协作(预留) ======
    # 是否在 Reflect 阶段对独立子主题派发子 agent 并行研究
    # 当前 deep_research 已通过 _search_batch(asyncio.gather)实现查询级并发,
    # 等价于"多查询并行搜索"。真正的子 agent 派发(各子 agent 独立跑
    # search+fetch+summarize)适合在上层 Plan & Execute 编排中实现,
    # 避免在工具内部嵌套 agent 调度。此开关预留,默认关闭。
    enable_subagent_research: bool = Field(default=False)
