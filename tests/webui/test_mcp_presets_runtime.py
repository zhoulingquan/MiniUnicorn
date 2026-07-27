from __future__ import annotations

from types import SimpleNamespace

from miniunicorn.agent.tools import mcp as mcp_presets_runtime


def test_mcp_preset_runtime_lines_describe_tool_prefix() -> None:
    msg = SimpleNamespace(
        content="use @playwright",
        metadata={
            "mcp_presets": [
                {
                    "name": "playwright",
                    "display_name": "Playwright",
                    "transport": "streamableHttp",
                }
            ],
        },
    )

    lines = mcp_presets_runtime.runtime_lines(
        msg,
        configured_server_names={"playwright"},
        connected_server_names={"playwright"},
    )

    assert lines
    assert "@playwright" in lines[0]
    assert "mcp_playwright_" in lines[0]
    assert "shell commands" in lines[0]


def test_mcp_preset_runtime_lines_warn_when_restart_needed() -> None:
    msg = SimpleNamespace(
        content="use @playwright",
        metadata={
            "mcp_presets": [
                {
                    "name": "playwright",
                    "display_name": "Playwright",
                    "transport": "streamableHttp",
                }
            ],
        },
    )

    lines = mcp_presets_runtime.runtime_lines(
        msg,
        configured_server_names=set(),
        connected_server_names=set(),
    )

    assert lines
    assert "has not loaded the latest MCP settings" in lines[0]


def test_mcp_preset_runtime_lines_warn_when_connection_not_live() -> None:
    msg = SimpleNamespace(
        content="use @playwright",
        metadata={
            "mcp_presets": [
                {
                    "name": "playwright",
                    "display_name": "Playwright",
                    "transport": "streamableHttp",
                }
            ],
        },
    )

    lines = mcp_presets_runtime.runtime_lines(
        msg,
        configured_server_names={"playwright"},
        connected_server_names=set(),
    )

    assert lines
    assert "connection is not currently live" in lines[0]


def test_mcp_preset_session_extra_only_persists_structured_mentions() -> None:
    assert mcp_presets_runtime.session_extra({}) == {}
    assert mcp_presets_runtime.session_extra(
        {
            "mcp_presets": [{"name": "playwright"}],
        }
    ) == {"mcp_presets": [{"name": "playwright"}]}
