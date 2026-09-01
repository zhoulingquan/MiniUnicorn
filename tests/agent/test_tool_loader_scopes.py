import pytest

from miniunicorn.config.schema import Config
from miniunicorn.tools.base import Tool
from miniunicorn.tools.context import ToolContext
from miniunicorn.tools.loader import ToolLoader
from miniunicorn.tools.registry import ToolRegistry


class _CoreOnlyTool(Tool):
    _scopes = {"core"}

    @property
    def name(self):
        return "core_only"

    @property
    def description(self):
        return "..."

    @property
    def parameters(self):
        return {"type": "object"}

    async def execute(self, **_):
        return "ok"


class _SubagentOnlyTool(Tool):
    _scopes = {"subagent"}

    @property
    def name(self):
        return "sub_only"

    @property
    def description(self):
        return "..."

    @property
    def parameters(self):
        return {"type": "object"}

    async def execute(self, **_):
        return "ok"


class _UniversalTool(Tool):
    _scopes = {"core", "subagent", "memory"}

    @property
    def name(self):
        return "universal"

    @property
    def description(self):
        return "..."

    @property
    def parameters(self):
        return {"type": "object"}

    async def execute(self, **_):
        return "ok"


@pytest.mark.asyncio
async def test_loader_filters_by_scope():
    loader = ToolLoader(test_classes=[_CoreOnlyTool, _SubagentOnlyTool, _UniversalTool])

    registry = ToolRegistry()
    ctx = ToolContext(config={}, workspace="/tmp")
    loader.load(ctx, registry, scope="core")

    assert registry.has("core_only")
    assert not registry.has("sub_only")
    assert registry.has("universal")


def test_tool_loader_scope_memory_only_returns_memory_tools():
    loader = ToolLoader()
    registry = ToolRegistry()
    ctx = ToolContext(config=Config().tools, workspace="/tmp")
    loader.load(ctx, registry, scope="memory")

    names = set(registry.tool_names)
    assert "read_file" in names
    assert "edit_file" in names
    assert "write_file" in names
    assert "list_dir" not in names
    assert "exec" not in names
    assert "message" not in names
