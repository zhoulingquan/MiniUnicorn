"""deep_research 工具单元测试。

覆盖核心流程:
- Plan/Search/Reflect/Write 四阶段
- reflect_rounds 多轮循环生效
- LLM 失败时的错误处理(Plan/Write 显式报错,Reflect 降级)
- 空 query / 空 results 等边界场景
- _parse_query_list JSON 解析容错
- _truncate / _build_results_block 等纯函数
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from miniunicorn.providers.base import LLMResponse
from miniunicorn.tools.deep_research.config import DeepResearchConfig
from miniunicorn.tools.deep_research.tool import DeepResearchTool
from miniunicorn.tools.web_search.backends.base import BackendResponse, SearchResult
from miniunicorn.tools.web_search.config import WebSearchConfig

# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------


def _make_tool(
    *,
    config: DeepResearchConfig | None = None,
    provider_responses: list[str] | None = None,
    provider_raises: Exception | None = None,
    search_results: list[SearchResult] | None = None,
) -> DeepResearchTool:
    """构造带 mock provider/aggregator 的 DeepResearchTool。

    - provider_responses: 按 _llm_chat 调用顺序依次返回(Plan/Reflect/Write)
    - provider_raises: provider.chat_with_retry 抛指定异常
    - search_results: aggregator.search 固定返回这些结果
    """
    # Disable fetch to avoid real HTTP calls in tests
    cfg = config or DeepResearchConfig(
        max_queries=5,
        initial_queries=3,
        enable_reflect=True,
        reflect_rounds=1,
        overall_timeout_s=30,
        enable_fetch=False,
    )
    ws_cfg = WebSearchConfig()

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    if provider_raises is not None:
        provider.chat_with_retry = AsyncMock(side_effect=provider_raises)
    else:
        # 用迭代器按顺序返回不同响应
        responses = list(provider_responses or [])
        llm_responses = [LLMResponse(content=r) for r in responses]

        async def _chat_with_retry(*args, **kwargs):
            if llm_responses:
                return llm_responses.pop(0)
            return LLMResponse(content="")

        provider.chat_with_retry = AsyncMock(side_effect=_chat_with_retry)

    tool = DeepResearchTool(
        config=cfg,
        web_search_config=ws_cfg,
        provider=provider,
        model="test-model",
    )

    # Mock aggregator.search
    results = (
        search_results
        if search_results is not None
        else [
            SearchResult(
                title=f"Result {i}",
                url=f"https://example.com/{i}",
                snippet=f"Snippet {i}",
                source_backend="test",
            )
            for i in range(3)
        ]
    )

    async def _fake_search(query, count, backend=None):
        return BackendResponse(backend="test", results=results)

    tool.aggregator.search = _fake_search
    return tool


# ---------------------------------------------------------------------------
# execute 入口测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_empty_query_returns_error():
    """空 query 应直接返回错误,不调用 LLM。"""
    tool = _make_tool()
    result = await tool.execute(query="")
    data = json.loads(result)
    assert "error" in data
    assert "query" in data["error"].lower() or "required" in data["error"].lower()


@pytest.mark.asyncio
async def test_execute_no_provider_returns_error():
    """无 provider 应返回明确错误。"""
    tool = DeepResearchTool(config=DeepResearchConfig(), web_search_config=WebSearchConfig())
    result = await tool.execute(query="test topic")
    data = json.loads(result)
    assert "error" in data
    assert "provider" in data["error"].lower()


@pytest.mark.asyncio
async def test_execute_whitespace_query_returns_error():
    """纯空白 query 应返回错误。"""
    tool = _make_tool()
    result = await tool.execute(query="   ")
    data = json.loads(result)
    assert "error" in data


# ---------------------------------------------------------------------------
# 完整流程测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_workflow_success():
    """正常 Plan -> Search -> Reflect(SUFFICIENT) -> Write 流程。"""
    tool = _make_tool(
        provider_responses=[
            json.dumps(["query1", "query2", "query3"]),  # Plan
            "SUFFICIENT",  # Reflect
            "## Report\n\nThis is a test report.",  # Write
        ],
    )
    result = await tool.execute(query="test topic")
    data = json.loads(result)

    assert "error" not in data
    assert data["query"] == "test topic"
    assert len(data["executed_queries"]) == 3
    assert data["total_results"] > 0
    assert "Report" in data["report"]


@pytest.mark.asyncio
async def test_full_workflow_with_reflect_extra_queries():
    """Reflect 补充新查询时,应执行补充搜索。"""
    tool = _make_tool(
        config=DeepResearchConfig(
            max_queries=5,
            initial_queries=2,
            enable_reflect=True,
            reflect_rounds=1,
            overall_timeout_s=30,
            enable_fetch=False,
        ),
        provider_responses=[
            json.dumps(["q1", "q2"]),  # Plan
            json.dumps(["q3", "q4"]),  # Reflect: 需要补充
            "## Final Report",  # Write
        ],
    )
    result = await tool.execute(query="topic")
    data = json.loads(result)

    assert "error" not in data
    assert set(data["executed_queries"]) == {"q1", "q2", "q3", "q4"}


# ---------------------------------------------------------------------------
# reflect_rounds 多轮循环测试(核心修复点)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reflect_rounds_multiple_iterations():
    """reflect_rounds=2 时应最多循环 2 次。

    修复前:if 语句只跑 1 次;修复后:while 循环按 reflect_rounds 跑。
    """
    call_count = {"reflect": 0}

    cfg = DeepResearchConfig(
        max_queries=10,
        initial_queries=2,
        enable_reflect=True,
        reflect_rounds=2,
        overall_timeout_s=30,
        enable_fetch=False,
    )
    ws_cfg = WebSearchConfig()

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    reflect_responses = [
        json.dumps(["extra1"]),  # 第 1 轮 Reflect: 补 1 条
        json.dumps(["extra2"]),  # 第 2 轮 Reflect: 补 1 条
    ]

    async def _chat(*args, **kwargs):
        # Plan
        if not hasattr(_chat, "_plan_done"):
            _chat._plan_done = True
            return LLMResponse(content=json.dumps(["q1", "q2"]))
        # Reflect
        if call_count["reflect"] < len(reflect_responses):
            resp = reflect_responses[call_count["reflect"]]
            call_count["reflect"] += 1
            return LLMResponse(content=resp)
        # Write
        return LLMResponse(content="## Report")

    provider.chat_with_retry = AsyncMock(side_effect=_chat)

    tool = DeepResearchTool(config=cfg, web_search_config=ws_cfg, provider=provider, model="m")

    async def _fake_search(query, count, backend=None):
        return BackendResponse(
            backend="test",
            results=[
                SearchResult(
                    title="R", url=f"https://x.com/{query}", snippet="s", source_backend="test"
                )
            ],
        )

    tool.aggregator.search = _fake_search

    await tool.execute(query="topic")

    # 应该有 2 轮 Reflect 调用
    assert call_count["reflect"] == 2, f"Expected 2 reflect rounds, got {call_count['reflect']}"


@pytest.mark.asyncio
async def test_reflect_rounds_stops_on_sufficient():
    """Reflect 返回 SUFFICIENT 时应立即停止循环,即使 reflect_rounds 未达上限。"""
    call_count = {"reflect": 0}

    cfg = DeepResearchConfig(
        max_queries=10,
        initial_queries=2,
        enable_reflect=True,
        reflect_rounds=3,  # 允许 3 轮
        overall_timeout_s=30,
        enable_fetch=False,
    )
    ws_cfg = WebSearchConfig()

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    async def _chat(*args, **kwargs):
        messages = kwargs.get("messages") or (args[0] if args else [])
        system_content = messages[0]["content"] if messages else ""
        if "planner" in system_content.lower():
            return LLMResponse(content=json.dumps(["q1", "q2"]))
        if "reflector" in system_content.lower():
            call_count["reflect"] += 1
            return LLMResponse(content="SUFFICIENT")  # 第 1 轮就 SUFFICIENT
        # Write
        return LLMResponse(content="## Report")

    provider.chat_with_retry = AsyncMock(side_effect=_chat)

    tool = DeepResearchTool(config=cfg, web_search_config=ws_cfg, provider=provider, model="m")

    async def _fake_search(query, count, backend=None):
        return BackendResponse(
            backend="test",
            results=[
                SearchResult(title="R", url="https://x.com", snippet="s", source_backend="test")
            ],
        )

    tool.aggregator.search = _fake_search

    await tool.execute(query="topic")
    # 只应调 1 次 Reflect
    assert call_count["reflect"] == 1


@pytest.mark.asyncio
async def test_reflect_rounds_stops_on_budget_exhausted():
    """预算用尽时应停止 Reflect 循环。"""
    cfg = DeepResearchConfig(
        max_queries=3,  # 只允许 3 次搜索
        initial_queries=3,  # Plan 用完所有预算
        enable_reflect=True,
        reflect_rounds=2,
        overall_timeout_s=30,
    )
    ws_cfg = WebSearchConfig()

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    reflect_called = {"count": 0}

    async def _chat(*args, **kwargs):
        messages = kwargs.get("messages") or (args[0] if args else [])
        system_content = messages[0]["content"] if messages else ""
        if "planner" in system_content.lower():
            return LLMResponse(content=json.dumps(["q1", "q2", "q3"]))
        if "reflector" in system_content.lower():
            reflect_called["count"] += 1
            return LLMResponse(content=json.dumps(["extra"]))  # 不应被调用
        # Write
        return LLMResponse(content="## Report")

    provider.chat_with_retry = AsyncMock(side_effect=_chat)

    tool = DeepResearchTool(config=cfg, web_search_config=ws_cfg, provider=provider, model="m")

    async def _fake_search(query, count, backend=None):
        return BackendResponse(
            backend="test",
            results=[
                SearchResult(title="R", url="https://x.com", snippet="s", source_backend="test")
            ],
        )

    tool.aggregator.search = _fake_search

    await tool.execute(query="topic")
    # Plan 用完 3 条预算,Reflect 不应被调用(remaining=0)
    assert reflect_called["count"] == 0


# ---------------------------------------------------------------------------
# LLM 失败错误处理测试(核心修复点)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_llm_failure_returns_explicit_error():
    """Plan 阶段 LLM 失败应返回明确错误(而非静默空串)。"""
    tool = _make_tool(provider_raises=RuntimeError("network error"))
    result = await tool.execute(query="topic")
    data = json.loads(result)
    assert "error" in data
    assert "plan" in data["error"].lower()
    assert "network error" in data["error"]


@pytest.mark.asyncio
async def test_write_llm_failure_returns_explicit_error():
    """Write 阶段 LLM 失败应返回明确错误,且包含已执行查询信息。"""
    cfg = DeepResearchConfig(enable_reflect=False, overall_timeout_s=30)
    ws_cfg = WebSearchConfig()

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    write_call_count = {"n": 0}

    async def _chat(*args, **kwargs):
        write_call_count["n"] += 1
        if write_call_count["n"] == 1:
            # Plan 成功
            return LLMResponse(content=json.dumps(["q1", "q2"]))
        # Write 失败
        raise RuntimeError("write API error")

    provider.chat_with_retry = AsyncMock(side_effect=_chat)

    tool = DeepResearchTool(config=cfg, web_search_config=ws_cfg, provider=provider, model="m")

    async def _fake_search(query, count, backend=None):
        return BackendResponse(
            backend="test",
            results=[
                SearchResult(title="R", url="https://x.com", snippet="s", source_backend="test")
            ],
        )

    tool.aggregator.search = _fake_search

    result = await tool.execute(query="topic")
    data = json.loads(result)
    assert "error" in data
    assert "write" in data["error"].lower()
    assert "write API error" in data["error"]
    # 应附带已执行的查询信息
    assert "executed_queries" in data
    assert len(data["executed_queries"]) > 0


@pytest.mark.asyncio
async def test_reflect_llm_failure_does_not_block_write():
    """Reflect 阶段 LLM 失败应降级为"不补充",不阻断 Write。"""
    cfg = DeepResearchConfig(enable_reflect=True, reflect_rounds=1, overall_timeout_s=30)
    ws_cfg = WebSearchConfig()

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    call_count = {"n": 0}

    async def _chat(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return LLMResponse(content=json.dumps(["q1"]))  # Plan
        if call_count["n"] == 2:
            raise RuntimeError("reflect API error")  # Reflect 失败
        return LLMResponse(content="## Report")  # Write

    provider.chat_with_retry = AsyncMock(side_effect=_chat)

    tool = DeepResearchTool(config=cfg, web_search_config=ws_cfg, provider=provider, model="m")

    async def _fake_search(query, count, backend=None):
        return BackendResponse(
            backend="test",
            results=[
                SearchResult(title="R", url="https://x.com", snippet="s", source_backend="test")
            ],
        )

    tool.aggregator.search = _fake_search

    result = await tool.execute(query="topic")
    data = json.loads(result)
    # Reflect 失败不应导致整体失败,Write 应正常执行
    assert "error" not in data
    assert "Report" in data["report"]


@pytest.mark.asyncio
async def test_write_empty_report_returns_error():
    """Write 返回空串应返回明确错误(而非硬编码占位文本)。"""
    tool = _make_tool(
        config=DeepResearchConfig(enable_reflect=False, overall_timeout_s=30),
        provider_responses=[
            json.dumps(["q1"]),  # Plan
            "",  # Write 空串
        ],
    )
    result = await tool.execute(query="topic")
    data = json.loads(result)
    assert "error" in data
    assert "empty" in data["error"].lower()


# ---------------------------------------------------------------------------
# 搜索失败测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_searches_fail_returns_error():
    """所有搜索都失败时应返回错误。"""
    cfg = DeepResearchConfig(enable_reflect=False, overall_timeout_s=30)
    ws_cfg = WebSearchConfig()

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content=json.dumps(["q1", "q2"])))

    tool = DeepResearchTool(config=cfg, web_search_config=ws_cfg, provider=provider, model="m")

    async def _failing_search(query, count, backend=None):
        return BackendResponse(backend="test", error="all backends failed")

    tool.aggregator.search = _failing_search

    result = await tool.execute(query="topic")
    data = json.loads(result)
    assert "error" in data
    assert "no search results" in data["error"].lower()


# ---------------------------------------------------------------------------
# 纯函数测试
# ---------------------------------------------------------------------------


class TestParseQueryList:
    def test_plain_json_array(self):
        result = DeepResearchTool._parse_query_list('["q1", "q2", "q3"]')
        assert result == ["q1", "q2", "q3"]

    def test_code_fence_wrapped(self):
        result = DeepResearchTool._parse_query_list('```json\n["q1", "q2"]\n```')
        assert result == ["q1", "q2"]

    def test_with_prose_around(self):
        """LLM 可能在 JSON 前后加文字,正则应兜底提取。"""
        result = DeepResearchTool._parse_query_list(
            'Here are the queries: ["q1", "q2"] hope this helps'
        )
        assert result == ["q1", "q2"]

    def test_empty_string(self):
        assert DeepResearchTool._parse_query_list("") == []

    def test_none(self):
        assert DeepResearchTool._parse_query_list(None) == []  # type: ignore[arg-type]

    def test_invalid_json(self):
        assert DeepResearchTool._parse_query_list("not json at all") == []

    def test_filters_empty_strings(self):
        result = DeepResearchTool._parse_query_list('["q1", "", "q2"]')
        assert result == ["q1", "q2"]


class TestTruncate:
    def test_short_text_unchanged(self):
        assert DeepResearchTool._truncate("hello", 100) == "hello"

    def test_empty_text(self):
        assert DeepResearchTool._truncate("", 100) == ""

    def test_exact_length(self):
        assert DeepResearchTool._truncate("hello", 5) == "hello"

    def test_long_text_truncated_with_ellipsis(self):
        result = DeepResearchTool._truncate("a" * 100, 10)
        assert len(result) == 10
        assert result.endswith("…")


class TestBuildResultsBlock:
    def test_empty_results(self):
        assert DeepResearchTool._build_results_block([]) == ""

    def test_single_result(self):
        results = [{"query": "q1", "title": "Title", "url": "https://x.com", "snippet": "Snippet"}]
        block = DeepResearchTool._build_results_block(results)
        assert "[1]" in block
        assert "q1" in block
        assert "Title" in block
        assert "https://x.com" in block
        assert "Snippet" in block

    def test_multiple_results_indexed(self):
        results = [
            {"query": "q1", "title": "T1", "url": "u1", "snippet": "s1"},
            {"query": "q2", "title": "T2", "url": "u2", "snippet": "s2"},
        ]
        block = DeepResearchTool._build_results_block(results)
        assert "[1]" in block
        assert "[2]" in block


class TestBuildResultsDigest:
    def test_empty_results(self):
        assert DeepResearchTool._build_results_digest([], 8) == ""

    def test_truncates_to_max_items(self):
        results = [{"title": f"T{i}", "snippet": f"S{i}"} for i in range(20)]
        digest = DeepResearchTool._build_results_digest(results, max_items=5)
        # 应只含 5 条 + 1 条 "more" 提示
        assert digest.count("- ") == 5
        assert "15 more" in digest

    def test_snippet_truncated_to_160_chars(self):
        long_snippet = "x" * 500
        results = [{"title": "T", "snippet": long_snippet}]
        digest = DeepResearchTool._build_results_digest(results, max_items=8)
        # 每行 snippet 应被截断到 160 字符
        assert "x" * 161 not in digest
