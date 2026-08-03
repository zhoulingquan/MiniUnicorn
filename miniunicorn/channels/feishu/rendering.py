"""Pure Feishu rendering helpers (no SDK, no network, no I/O).

These functions convert markdown into the Feishu card / post / text payloads.
They are intentionally side-effect free and do not import ``lark-oapi`` so they
can be unit-tested without the optional SDK installed.
"""

from __future__ import annotations

import json
import re

# ── Regex patterns ───────────────────────────────────────────────────────────

# Markdown tables (header + separator + data rows)
_TABLE_RE = re.compile(
    r"((?:^[ \t]*\|.+\|[ \t]*\n)(?:^[ \t]*\|[-:\s|]+\|[ \t]*\n)(?:^[ \t]*\|.+\|[ \t]*\n?)+)",
    re.MULTILINE,
)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)

_CODE_BLOCK_RE = re.compile(r"(```[\s\S]*?```)", re.MULTILINE)

# Markdown formatting markers stripped from plain-text surfaces like table
# cells and heading text.
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MD_BOLD_UNDERSCORE_RE = re.compile(r"__(.+?)__")
_MD_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_MD_STRIKE_RE = re.compile(r"~~(.+?)~~")

# Patterns that indicate "complex" markdown needing card rendering
_COMPLEX_MD_RE = re.compile(
    r"```"  # fenced code block
    r"|^\|.+\|.*\n\s*\|[-:\s|]+\|"  # markdown table (header + separator)
    r"|^#{1,6}\s+",  # headings
    re.MULTILINE,
)

# Simple markdown patterns (bold, italic, strikethrough)
_SIMPLE_MD_RE = re.compile(
    r"\*\*.+?\*\*"  # **bold**
    r"|__.+?__"  # __bold__
    r"|(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)"  # *italic* (single *)
    r"|~~.+?~~",  # ~~strikethrough~~
    re.DOTALL,
)

# Markdown link: [text](url)
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\)]+)\)")

# Unordered list items
_LIST_RE = re.compile(r"^[\s]*[-*+]\s+", re.MULTILINE)

# Ordered list items
_OLIST_RE = re.compile(r"^[\s]*\d+\.\s+", re.MULTILINE)

# Max length for plain text format
_TEXT_MAX_LEN = 200

# Max length for post (rich text) format; beyond this, use card
_POST_MAX_LEN = 2000

__all__ = [
    "strip_markdown_formatting",
    "parse_markdown_table",
    "build_card_elements",
    "split_elements_by_table_limit",
    "split_headings",
    "detect_message_format",
    "markdown_to_post",
    "fallback_text_chunks",
    "format_tool_hint_lines",
    "format_tool_hint_delta",
]


def strip_markdown_formatting(text: str) -> str:
    """Strip markdown formatting markers from text for plain display.

    Feishu table cells do not support markdown rendering, so we remove
    the formatting markers to keep the text readable.
    """
    # Remove bold markers
    text = _MD_BOLD_RE.sub(r"\1", text)
    text = _MD_BOLD_UNDERSCORE_RE.sub(r"\1", text)
    # Remove italic markers
    text = _MD_ITALIC_RE.sub(r"\1", text)
    # Remove strikethrough markers
    text = _MD_STRIKE_RE.sub(r"\1", text)
    return text


def parse_markdown_table(table_text: str) -> dict | None:
    """Parse a markdown table into a Feishu table element."""
    lines = [_line.strip() for _line in table_text.strip().split("\n") if _line.strip()]
    if len(lines) < 3:
        return None

    def split(_line: str) -> list[str]:
        return [c.strip() for c in _line.strip("|").split("|")]

    headers = [strip_markdown_formatting(h) for h in split(lines[0])]
    rows = [[strip_markdown_formatting(c) for c in split(_line)] for _line in lines[2:]]
    columns = [
        {"tag": "column", "name": f"c{i}", "display_name": h, "width": "auto"}
        for i, h in enumerate(headers)
    ]
    return {
        "tag": "table",
        "page_size": len(rows) + 1,
        "columns": columns,
        "rows": [{f"c{i}": r[i] if i < len(r) else "" for i in range(len(headers))} for r in rows],
    }


def build_card_elements(content: str) -> list[dict]:
    """Split content into div/markdown + table elements for Feishu card."""
    elements, last_end = [], 0
    for m in _TABLE_RE.finditer(content):
        before = content[last_end : m.start()]
        if before.strip():
            elements.extend(split_headings(before))
        elements.append(
            parse_markdown_table(m.group(1)) or {"tag": "markdown", "content": m.group(1)}
        )
        last_end = m.end()
    remaining = content[last_end:]
    if remaining.strip():
        elements.extend(split_headings(remaining))
    return elements or [{"tag": "markdown", "content": content}]


def split_elements_by_table_limit(elements: list[dict], max_tables: int = 1) -> list[list[dict]]:
    """Split card elements into groups with at most *max_tables* table elements each.

    Feishu cards have a hard limit of one table per card (API error 11310).
    When the rendered content contains multiple markdown tables each table is
    placed in a separate card message so every table reaches the user.
    """
    if not elements:
        return [[]]
    groups: list[list[dict]] = []
    current: list[dict] = []
    table_count = 0
    for el in elements:
        if el.get("tag") == "table":
            if table_count >= max_tables:
                if current:
                    groups.append(current)
                current = []
                table_count = 0
            current.append(el)
            table_count += 1
        else:
            current.append(el)
    if current:
        groups.append(current)
    return groups or [[]]


def split_headings(content: str) -> list[dict]:
    """Split content by headings, converting headings to div elements."""
    protected = content
    code_blocks = []
    for m in _CODE_BLOCK_RE.finditer(content):
        code_blocks.append(m.group(1))
        protected = protected.replace(m.group(1), f"\x00CODE{len(code_blocks) - 1}\x00", 1)

    elements = []
    last_end = 0
    for m in _HEADING_RE.finditer(protected):
        before = protected[last_end : m.start()].strip()
        if before:
            elements.append({"tag": "markdown", "content": before})
        text = strip_markdown_formatting(m.group(2).strip())
        display_text = f"**{text}**" if text else ""
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": display_text,
                },
            }
        )
        last_end = m.end()
    remaining = protected[last_end:].strip()
    if remaining:
        elements.append({"tag": "markdown", "content": remaining})

    for i, cb in enumerate(code_blocks):
        for el in elements:
            if el.get("tag") == "markdown":
                el["content"] = el["content"].replace(f"\x00CODE{i}\x00", cb)

    return elements or [{"tag": "markdown", "content": content}]


# ── Smart format detection ───────────────────────────────────────────────────


def detect_message_format(content: str) -> str:
    """Determine the optimal Feishu message format for *content*.

    Returns one of:
    - ``"text"``        – plain text, short and no markdown
    - ``"post"``        – rich text (links only, moderate length)
    - ``"interactive"`` – card with full markdown rendering
    """
    stripped = content.strip()

    # Complex markdown (code blocks, tables, headings) → always card
    if _COMPLEX_MD_RE.search(stripped):
        return "interactive"

    # Long content → card (better readability with card layout)
    if len(stripped) > _POST_MAX_LEN:
        return "interactive"

    # Has bold/italic/strikethrough → card (post format can't render these)
    if _SIMPLE_MD_RE.search(stripped):
        return "interactive"

    # Has list items → card (post format can't render list bullets well)
    if _LIST_RE.search(stripped) or _OLIST_RE.search(stripped):
        return "interactive"

    # Has links → post format (supports <a> tags)
    if _MD_LINK_RE.search(stripped):
        return "post"

    # Short plain text → text format
    if len(stripped) <= _TEXT_MAX_LEN:
        return "text"

    # Medium plain text without any formatting → post format
    return "post"


def markdown_to_post(content: str) -> str:
    """Convert markdown content to Feishu post message JSON.

    Handles links ``[text](url)`` as ``a`` tags; everything else as ``text`` tags.
    Each line becomes a paragraph (row) in the post body.
    """
    lines = content.strip().split("\n")
    paragraphs: list[list[dict]] = []

    for line in lines:
        elements: list[dict] = []
        last_end = 0

        for m in _MD_LINK_RE.finditer(line):
            # Text before this link
            before = line[last_end : m.start()]
            if before:
                elements.append({"tag": "text", "text": before})
            elements.append(
                {
                    "tag": "a",
                    "text": m.group(1),
                    "href": m.group(2),
                }
            )
            last_end = m.end()

        # Remaining text after last link
        remaining = line[last_end:]
        if remaining:
            elements.append({"tag": "text", "text": remaining})

        # Empty line → empty paragraph for spacing
        if not elements:
            elements.append({"tag": "text", "text": ""})

        paragraphs.append(elements)

    post_body = {
        "zh_cn": {
            "content": paragraphs,
        }
    }
    return json.dumps(post_body, ensure_ascii=False)


def fallback_text_chunks(text: str, limit: int = 3500) -> list[str]:
    """Split a long text into chunks no larger than *limit* characters."""
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    return [chunk for chunk in chunks if chunk]


def format_tool_hint_lines(tool_hint: str) -> str:
    """Split tool hints across lines on top-level call separators only."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    in_string = False
    quote_char = ""
    escaped = False

    for i, ch in enumerate(tool_hint):
        buf.append(ch)

        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote_char:
                in_string = False
            continue

        if ch in {'"', "'"}:
            in_string = True
            quote_char = ch
            continue

        if ch == "(":
            depth += 1
            continue

        if ch == ")" and depth > 0:
            depth -= 1
            continue

        if ch == "," and depth == 0:
            next_char = tool_hint[i + 1] if i + 1 < len(tool_hint) else ""
            if next_char == " ":
                parts.append("".join(buf).rstrip())
                buf = []

    if buf:
        parts.append("".join(buf).strip())

    return "\n".join(part for part in parts if part)


def format_tool_hint_delta(tool_hint: str, prefix: str) -> str:
    """Format a tool hint string with the *prefix* for each line."""
    lines = format_tool_hint_lines(tool_hint).split("\n")
    return "\n".join(f"{prefix} {ln}" for ln in lines if ln.strip())
