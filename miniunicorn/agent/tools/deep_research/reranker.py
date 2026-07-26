"""搜索结果重排模块 - 基于 query 与 result 的 token 重叠度评分。

借鉴 gpt-researcher 的 relevance scoring 思路,实现一个轻量的、
不依赖 LLM 的结果重排:对每条结果计算其 title+snippet 与查询的
token 重叠分数,按分数降序排列。

设计要点:
- 纯 Python 实现,零外部依赖
- 支持中英文混合(简单分词:英文按空格/标点,中文按字符)
- 可选启用(通过 config.enable_rerank)
- 失败时静默回退到原顺序(不阻断流程)
"""

from __future__ import annotations

import re
from typing import Any

from loguru import logger

# 英文 token:连续字母数字(允许连字符)
_EN_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-]*")
# 停用词(常见无意义词,不计入重叠)
_STOPWORDS: frozenset[str] = frozenset(
    {
        "the", "a", "an", "and", "or", "but", "is", "are", "was", "were",
        "be", "been", "being", "have", "has", "had", "do", "does", "did",
        "will", "would", "could", "should", "may", "might", "must", "can",
        "this", "that", "these", "those", "i", "you", "he", "she", "it",
        "we", "they", "what", "which", "who", "when", "where", "why", "how",
        "of", "in", "on", "at", "to", "for", "with", "by", "from", "as",
        "的", "了", "是", "在", "和", "与", "或", "但", "也", "都", "还",
        "就", "只", "又", "已", "正", "将", "会", "能", "可", "要", "想",
        "这", "那", "一", "个", "些", "么", "什", "怎", "哪", "哪",
    }
)


def tokenize(text: str) -> set[str]:
    """简单分词:英文按词,中文按字符,转小写,去停用词。

    返回 token 集合(用于快速重叠计算)。
    """
    if not text:
        return set()
    tokens: set[str] = set()
    # 英文 token
    for m in _EN_TOKEN_RE.finditer(text):
        tok = m.group(0).lower()
        if len(tok) >= 2 and tok not in _STOPWORDS:
            tokens.add(tok)
    # 中文字符(每个汉字作为一个 token)
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            tokens.add(ch)
    return tokens


def score_result(query_tokens: set[str], result: dict[str, Any]) -> float:
    """计算单条结果与查询的相关性分数(0.0 - 1.0)。

    分数 = overlap_count / max(len(query_tokens), 1)
    即查询 token 在结果(title + snippet)中出现的比例。
    """
    if not query_tokens:
        return 0.0
    title = (result.get("title") or "").strip()
    snippet = (result.get("snippet") or "").strip()
    content = (result.get("content") or "").strip()
    # title 权重最高(×3),snippet 次之(×2),content 辅助(×1)
    title_tokens = tokenize(title)
    snippet_tokens = tokenize(snippet)
    content_tokens = tokenize(content) if content else set()

    # 加权重叠:在 title 出现算 3 分,snippet 2 分,content 1 分
    score = 0.0
    for tok in query_tokens:
        if tok in title_tokens:
            score += 3.0
        elif tok in snippet_tokens:
            score += 2.0
        elif tok in content_tokens:
            score += 1.0
    # 归一化到 [0, 1]:理论最大 = len(query_tokens) * 3
    max_score = max(len(query_tokens) * 3, 1.0)
    return min(score / max_score, 1.0)


def rerank_results(
    results: list[dict[str, Any]],
    query: str,
    *,
    min_score: float = 0.1,
) -> list[dict[str, Any]]:
    """按与 query 的相关性重排结果,过滤低分项。

    - 相同分数保持原顺序(稳定排序)
    - 分数低于 min_score 的结果被过滤
    - 失败时静默返回原列表(不阻断流程)
    """
    if not results:
        return results
    try:
        query_tokens = tokenize(query)
        if not query_tokens:
            # 查询无有效 token(如纯标点),不重排
            return results

        scored: list[tuple[float, int, dict[str, Any]]] = []
        for idx, r in enumerate(results):
            s = score_result(query_tokens, r)
            if s >= min_score:
                scored.append((s, idx, r))

        if not scored:
            # 全部低于阈值,返回原列表(避免空结果)
            logger.debug(
                "rerank: all {} results below min_score={}, returning original order",
                len(results),
                min_score,
            )
            return results

        # 稳定排序:按分数降序,相同分数按原 idx 升序
        scored.sort(key=lambda x: (-x[0], x[1]))
        reranked = [r for _, _, r in scored]

        if len(reranked) < len(results):
            logger.debug(
                "rerank: kept {}/{}, filtered {} low-score results",
                len(reranked),
                len(results),
                len(results) - len(reranked),
            )
        return reranked
    except Exception as e:
        logger.debug("rerank failed, returning original order: {}", e)
        return results


def rerank_per_query(
    results: list[dict[str, Any]],
    *,
    min_score: float = 0.1,
) -> list[dict[str, Any]]:
    """按每条结果自己的 query 字段重排。

    用于 deep_research 中,results 来自多个不同查询,每个 query 的结果
    应独立按相关性重排,而非全局混排。
    """
    if not results:
        return results
    # 按 query 分组,保持组间原顺序
    groups: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    order: list[str] = []  # 记录 query 首次出现顺序
    for idx, r in enumerate(results):
        q = (r.get("query") or "").strip()
        if q not in groups:
            groups[q] = []
            order.append(q)
        groups[q].append((idx, r))

    output: list[dict[str, Any]] = []
    for q in order:
        group = [r for _, r in groups[q]]
        reranked = rerank_results(group, q, min_score=min_score)
        output.extend(reranked)
    return output
