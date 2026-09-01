"""国内免 Key 搜索后端的通用 HTML 解析工具。"""

from __future__ import annotations

import html
import re
from urllib.parse import quote_plus, urljoin


def strip_tags(text: str) -> str:
    """移除 HTML 标签并解码实体。"""
    text = re.sub(r"<script[\s\S]*?</script>", "", text, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", "", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def normalize_text(text: str) -> str:
    """规整空白。"""
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def clean_snippet(text: str, max_len: int = 200) -> str:
    """清理摘要文本,限制长度。"""
    text = strip_tags(text)
    text = normalize_text(text)
    if len(text) > max_len:
        text = text[: max_len - 1] + "…"
    return text


def make_absolute_url(base: str, url: str) -> str:
    """将相对 URL 转为绝对 URL。"""
    if not url:
        return ""
    if url.startswith(("http://", "https://")):
        return url
    return urljoin(base, url)


def encode_query(query: str) -> str:
    """URL 编码查询字符串。"""
    return quote_plus(query.strip())
