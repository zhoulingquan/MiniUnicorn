from __future__ import annotations

import asyncio

import pytest

from miniunicorn.config.loader import load_config
from miniunicorn.webui.mcp_presets_api import (
    McpPresetError,
    custom_mcp_action,
    mcp_presets_action,
    mcp_presets_payload,
    mcp_presets_test_action,
    normalize_mcp_preset_mentions,
)


def _use_config(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("miniunicorn.config.loader._current_config_path", tmp_path / "config.json")


def test_mcp_presets_payload_lists_supported_cards(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_config(tmp_path, monkeypatch)

    payload = mcp_presets_payload()
    names = {preset["name"] for preset in payload["presets"]}

    assert {
        "playwright",
        "github",
        "context7",
        "sequential-thinking",
        "fetch",
        "filesystem",
        "memory",
        "puppeteer",
        "time",
    }.issubset(names)
    # brave-search / tavily 已移至 web_search backends，不再作为 MCP preset 暴露
    assert "brave-search" not in names
    assert "tavily" not in names


def test_enable_github_writes_scrubbed_config_payload(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch)

    payload = mcp_presets_action(
        "enable",
        {
            "name": ["github"],
            "api_key": ["ghp_test_token_value"],
        },
    )

    assert payload["requires_restart"] is True
    assert payload["last_action"]["ok"] is True
    assert payload["last_action"]["installed"] is True
    assert payload["last_action"]["verification"] == ["config_present"]
    preset = next(row for row in payload["presets"] if row["name"] == "github")
    assert preset["installed"] is True
    assert preset["configured"] is True
    assert "ghp_test_token_value" not in str(payload)
    config = load_config()
    assert config.tools.mcp_servers["github"].env["GITHUB_PERSONAL_ACCESS_TOKEN"] == "ghp_test_token_value"


def test_enable_requires_missing_secret(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_config(tmp_path, monkeypatch)

    with pytest.raises(McpPresetError) as exc:
        mcp_presets_action("enable", {"name": ["github"]})

    assert exc.value.status == 400
    assert "GitHub Personal Access Token" in exc.value.message


def test_enable_context7_optional_api_key_appends_arg(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch)

    payload = mcp_presets_action(
        "enable",
        {
            "name": ["context7"],
            "context7_api_key": ["ctx7_secret"],
        },
    )

    assert "ctx7_secret" not in str(payload)
    row = next(item for item in payload["presets"] if item["name"] == "context7")
    assert row["configured"] is True
    config = load_config()
    assert config.tools.mcp_servers["context7"].args == [
        "-y",
        "@upstash/context7-mcp@latest",
        "--api-key",
        "ctx7_secret",
    ]


def test_enable_stdio_preset_uses_config_scoped_cwd(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch)

    mcp_presets_action("enable", {"name": ["playwright"]})

    config = load_config()
    cwd = config.tools.mcp_servers["playwright"].cwd
    assert cwd == str(tmp_path / "mcp" / "playwright")
    assert (tmp_path / "mcp" / "playwright").is_dir()


def test_remove_mcp_preset_updates_config(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_config(tmp_path, monkeypatch)
    mcp_presets_action("enable", {"name": ["playwright"]})
    managed_cwd = tmp_path / "mcp" / "playwright"
    (managed_cwd / "cache.txt").write_text("managed runtime data", encoding="utf-8")

    payload = mcp_presets_action("remove", {"name": ["playwright"]})

    assert payload["requires_restart"] is True
    assert payload["last_action"]["ok"] is True
    assert payload["last_action"]["removed"] is True
    assert payload["last_action"]["managed_paths_removed"] == ["runtime:mcp/playwright"]
    assert not managed_cwd.exists()
    config = load_config()
    assert "playwright" not in config.tools.mcp_servers


def test_remove_custom_mcp_server_preserves_user_cwd(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_config(tmp_path, monkeypatch)
    user_cwd = tmp_path / "user-cwd"
    user_cwd.mkdir()
    custom_mcp_action(
        "custom",
        {
            "name": ["internal-docs"],
            "transport": ["stdio"],
            "command": ["node"],
            "args": ['["server.js"]'],
            "cwd": [str(user_cwd)],
        },
    )

    payload = mcp_presets_action("remove", {"name": ["internal-docs"]})

    assert payload["last_action"]["ok"] is True
    assert user_cwd.exists()
    config = load_config()
    assert "internal-docs" not in config.tools.mcp_servers


def test_test_mcp_preset_reports_missing_dependency(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch)
    mcp_presets_action("enable", {"name": ["playwright"]})
    monkeypatch.setattr("miniunicorn.webui.mcp_presets_api.shutil.which", lambda _command: None)

    payload = asyncio.run(mcp_presets_test_action({"name": ["playwright"]}))

    assert payload["last_action"]["ok"] is False
    assert "npx" in payload["last_action"]["message"]


def test_test_mcp_preset_connects_and_reports_tools(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch)
    mcp_presets_action("enable", {"name": ["playwright"]})

    class FakeStack:
        async def aclose(self) -> None:
            return None

    async def fake_connect(servers, registry):
        assert list(servers) == ["playwright"]

        class FakeTool:
            name = "mcp_playwright_browser_navigate"

            def to_schema(self):
                return {"name": self.name, "description": "", "parameters": {}}

        registry.register(FakeTool())
        return {"playwright": FakeStack()}

    monkeypatch.setattr("miniunicorn.agent.tools.mcp.connect_mcp_servers", fake_connect)

    payload = asyncio.run(mcp_presets_test_action({"name": ["playwright"]}))

    assert payload["last_action"]["ok"] is True
    assert payload["last_action"]["tool_count"] == 1
    assert payload["last_action"]["tool_names"] == ["mcp_playwright_browser_navigate"]


def test_test_mcp_preset_scrubs_connection_errors(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch)
    custom_mcp_action(
        "custom",
        {
            "name": ["custom-remote"],
            "transport": ["streamableHttp"],
            "url": ["https://example.invalid/mcp?token=bb_live_secret"],
        },
    )

    async def fake_connect(_servers, _registry):
        raise RuntimeError(
            "failed https://example.invalid/mcp?token=bb_live_secret"
        )

    monkeypatch.setattr("miniunicorn.agent.tools.mcp.connect_mcp_servers", fake_connect)

    payload = asyncio.run(mcp_presets_test_action({"name": ["custom-remote"]}))

    assert payload["last_action"]["ok"] is False
    assert "bb_live_secret" not in str(payload)
    assert "<redacted>" in payload["last_action"]["error"]


def test_unlisted_oauth_placeholder_is_not_enabled(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_config(tmp_path, monkeypatch)

    with pytest.raises(McpPresetError) as exc:
        mcp_presets_action("enable", {"name": ["linear"]})

    assert exc.value.status == 404


def test_normalize_mcp_preset_mentions_keeps_known_presets_only() -> None:
    payload = normalize_mcp_preset_mentions(
        [
            {
                "name": "playwright",
                "display_name": "Playwright",
                "transport": "stdio",
                "configured": True,
                "logo_url": "https://example.invalid/logo.svg",
            },
            {"name": "totally-unknown"},
            "bad",
        ]
    )

    assert payload == [
        {
            "name": "playwright",
            "display_name": "Playwright",
            "transport": "stdio",
            "configured": True,
            "logo_url": "https://example.invalid/logo.svg",
        }
    ]


def test_custom_mcp_server_writes_config_and_catalog_row(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch)

    payload = custom_mcp_action(
        "custom",
        {
            "name": ["internal-docs"],
            "transport": ["stdio"],
            "command": ["node"],
            "args": ['["server.js"]'],
            "env": ['{"DOCS_TOKEN":"docs-secret-value"}'],
            "tool_timeout": ["45"],
        },
    )

    assert payload["requires_restart"] is True
    row = next(item for item in payload["presets"] if item["name"] == "internal-docs")
    assert row["source"] == "custom"
    assert row["transport"] == "stdio"
    assert row["connection_summary"] == "node server.js"
    assert row["manifest"]["schema"] == "agent-app.v1"
    assert row["manifest"]["source"] == "mcp-custom"
    assert row["manifest"]["capabilities"][0]["command"] == "node"
    assert "server.js" not in str(row["manifest"])
    assert "docs-secret-value" not in str(payload)
    config = load_config()
    assert config.tools.mcp_servers["internal-docs"].args == ["server.js"]
    assert config.tools.mcp_servers["internal-docs"].env["DOCS_TOKEN"] == "docs-secret-value"


def test_import_mcp_config_and_tool_allowlist(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch)

    payload = custom_mcp_action(
        "import",
        {
            "config": [
                (
                    '{"mcpServers":{'
                    '"docs":{"command":"npx","args":["-y","docs-mcp"],"env":{"API_KEY":"config-secret-value"}},'
                    '"remote-docs":{"transport":"sse","url":"https://example.com/sse"}'
                    "}}"
                )
            ],
        },
    )

    assert payload["last_action"]["message"] == "Imported 2 MCP server(s)."
    config = load_config()
    assert config.tools.mcp_servers["docs"].command == "npx"
    assert config.tools.mcp_servers["docs"].args == ["-y", "docs-mcp"]
    assert config.tools.mcp_servers["remote-docs"].type == "sse"
    assert config.tools.mcp_servers["remote-docs"].url == "https://example.com/sse"
    assert config.tools.mcp_servers["docs"].env["API_KEY"] == "config-secret-value"
    assert "config-secret-value" not in str(payload)

    payload = custom_mcp_action(
        "tools",
        {
            "name": ["docs"],
            "enabled_tools": ['["mcp_docs_search"]'],
        },
    )

    row = next(item for item in payload["presets"] if item["name"] == "docs")
    assert row["enabled_tools"] == ["mcp_docs_search"]
    assert load_config().tools.mcp_servers["docs"].enabled_tools == ["mcp_docs_search"]

    payload = custom_mcp_action(
        "tools",
        {
            "name": ["docs"],
            "enabled_tools": ["[]"],
        },
    )

    row = next(item for item in payload["presets"] if item["name"] == "docs")
    assert row["enabled_tools"] == []
    assert load_config().tools.mcp_servers["docs"].enabled_tools == []


def test_normalize_mcp_preset_mentions_accepts_configured_custom_server(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch)
    custom_mcp_action(
        "custom",
        {
            "name": ["docs"],
            "transport": ["streamableHttp"],
            "url": ["https://example.com/mcp"],
        },
    )

    payload = normalize_mcp_preset_mentions(
        [
            {"name": "docs", "display_name": "Docs", "transport": "streamableHttp"},
        ]
    )

    assert payload == [{"name": "docs", "display_name": "Docs", "transport": "streamableHttp"}]
