"""Pure-rendering parity tests for the Feishu rendering service.

These tests import directly from ``miniunicorn.channels.feishu.rendering`` so the
pure helpers can be exercised without the optional ``lark-oapi`` SDK installed.
The assertions mirror the byte-for-byte payloads produced by the legacy class
methods on ``FeishuChannel`` to guarantee extraction does not change output.
"""

import json

from miniunicorn.channels.feishu.rendering import (
    build_card_elements,
    detect_message_format,
    fallback_text_chunks,
    format_tool_hint_delta,
    format_tool_hint_lines,
    markdown_to_post,
    parse_markdown_table,
    split_elements_by_table_limit,
    split_headings,
    strip_markdown_formatting,
)

# ── strip_markdown_formatting ────────────────────────────────────────────────


def test_strip_markdown_formatting_removes_bold_italic_strike() -> None:
    assert strip_markdown_formatting("**bold** __u__ *i* ~~s~~") == "bold u i s"


# ── parse_markdown_table ─────────────────────────────────────────────────────


def test_parse_markdown_table_strips_markdown_formatting_in_headers_and_cells() -> None:
    table = parse_markdown_table(
        """
| **Name** | __Status__ | *Notes* | ~~State~~ |
| --- | --- | --- | --- |
| **Alice** | __Ready__ | *Fast* | ~~Old~~ |
"""
    )

    assert table is not None
    assert [col["display_name"] for col in table["columns"]] == [
        "Name",
        "Status",
        "Notes",
        "State",
    ]
    assert table["rows"] == [{"c0": "Alice", "c1": "Ready", "c2": "Fast", "c3": "Old"}]


# ── split_headings (headings + code fences) ──────────────────────────────────


def test_split_headings_strips_embedded_markdown_before_bolding() -> None:
    elements = split_headings("# **Important** *status* ~~update~~")

    assert elements == [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "**Important status update**",
            },
        }
    ]


def test_split_headings_keeps_markdown_body_and_code_blocks_intact() -> None:
    elements = split_headings(
        "# **Heading**\n\nBody with **bold** text.\n\n```python\nprint('hi')\n```"
    )

    assert elements[0] == {
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": "**Heading**",
        },
    }
    assert elements[1]["tag"] == "markdown"
    assert "Body with **bold** text." in elements[1]["content"]
    assert "```python\nprint('hi')\n```" in elements[1]["content"]


# ── build_card_elements ──────────────────────────────────────────────────────


def test_build_card_elements_combines_headings_markdown_and_table() -> None:
    content = "# Title\n\nIntro text.\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n"

    elements = build_card_elements(content)

    assert elements[0] == {
        "tag": "div",
        "text": {"tag": "lark_md", "content": "**Title**"},
    }
    tables = [el for el in elements if el.get("tag") == "table"]
    assert len(tables) == 1
    assert [col["display_name"] for col in tables[0]["columns"]] == ["A", "B"]
    assert tables[0]["rows"] == [{"c0": "1", "c1": "2"}]


# ── split_elements_by_table_limit (tables at platform limit) ─────────────────


def test_split_elements_by_table_limit_empty_returns_single_empty_group() -> None:
    assert split_elements_by_table_limit([]) == [[]]


def test_split_elements_by_table_limit_splits_multiple_tables() -> None:
    t1 = {"tag": "table", "columns": [], "rows": [{"c0": "table-one"}], "page_size": 1}
    t2 = {"tag": "table", "columns": [], "rows": [{"c0": "table-two"}], "page_size": 1}
    md = {"tag": "markdown", "content": "between"}
    els = [md, t1, md, t2, md]

    result = split_elements_by_table_limit(els)

    assert len(result) == 2
    assert t1 in result[0]
    assert t2 not in result[0]
    assert t2 in result[1]
    assert t1 not in result[1]


# ── detect_message_format ────────────────────────────────────────────────────


def test_detect_message_format_short_plain_text_is_text() -> None:
    assert detect_message_format("hello world") == "text"


def test_detect_message_format_code_fence_is_interactive() -> None:
    assert detect_message_format("```python\nprint(1)\n```") == "interactive"


def test_detect_message_format_long_text_is_interactive() -> None:
    assert detect_message_format("x" * 2001) == "interactive"


def test_detect_message_format_link_is_post() -> None:
    assert detect_message_format("See [docs](https://example.com)") == "post"


# ── markdown_to_post (post payload + mentions) ───────────────────────────────


def test_markdown_to_post_link_payload() -> None:
    body = json.loads(markdown_to_post("See [docs](https://example.com)"))

    assert body == {
        "zh_cn": {
            "content": [
                [
                    {"tag": "text", "text": "See "},
                    {"tag": "a", "text": "docs", "href": "https://example.com"},
                ]
            ]
        }
    }


def test_markdown_to_post_preserves_mention_placeholder_as_text() -> None:
    body = json.loads(markdown_to_post("@_user_1 hello"))

    assert body == {"zh_cn": {"content": [[{"tag": "text", "text": "@_user_1 hello"}]]}}


def test_detect_message_format_mention_text_is_text() -> None:
    assert detect_message_format("@_user_1 hello") == "text"


# ── fallback_text_chunks (interactive fallback chunks) ────────────────────────


def test_fallback_text_chunks_empty_returns_empty() -> None:
    assert fallback_text_chunks("") == []


def test_fallback_text_chunks_short_returns_single_chunk() -> None:
    assert fallback_text_chunks("short") == ["short"]


def test_fallback_text_chunks_splits_long_text_on_newlines() -> None:
    text = "line1\nline2\nline3"

    assert fallback_text_chunks(text, limit=10) == ["line1", "line2", "line3"]


# ── tool hint formatting (tool hints containing code blocks / commas) ────────


def test_format_tool_hint_lines_keeps_commas_inside_arguments() -> None:
    # The top-level separator comma is retained on the preceding line (the
    # char is buffered before the split) — this mirrors legacy output exactly.
    result = format_tool_hint_lines('list_dir("foo, bar"), read_file("/path/to/file")')

    assert result == 'list_dir("foo, bar"),\nread_file("/path/to/file")'


def test_format_tool_hint_delta_prefixes_each_line() -> None:
    result = format_tool_hint_delta('read src/main.py, grep "TODO"', "\U0001f527")

    assert result == '\U0001f527 read src/main.py,\n\U0001f527 grep "TODO"'


def test_format_tool_hint_delta_handles_folding() -> None:
    result = format_tool_hint_delta('read path × 3, grep "pattern"', "\U0001f527")

    assert "\u00d7 3" in result
    assert 'grep "pattern"' in result
