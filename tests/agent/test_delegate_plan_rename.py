"""Tests for delegate_plan tool rename with execute_plan alias compatibility."""

from __future__ import annotations

from miniunicorn.agent.tools.execute_plan import ExecutePlanTool
from miniunicorn.agent.tools.registry import ToolRegistry


def test_tool_primary_name() -> None:
    """ExecutePlanTool primary name should be delegate_plan."""
    tool = ExecutePlanTool()
    assert tool.name == "delegate_plan"


def test_legacy_alias_resolves() -> None:
    """Registry should resolve execute_plan alias to the same delegate_plan tool instance."""
    registry = ToolRegistry()
    tool = ExecutePlanTool()
    registry.register(tool)

    delegate_tool = registry.get("delegate_plan")
    alias_tool = registry.get("execute_plan")

    assert delegate_tool is not None
    assert alias_tool is delegate_tool


def test_definitions_not_duplicated() -> None:
    """get_definitions() should only include delegate_plan once, not the alias."""
    registry = ToolRegistry()
    tool = ExecutePlanTool()
    registry.register(tool)

    definitions = registry.get_definitions()
    names = [d["function"]["name"] for d in definitions]

    assert names.count("delegate_plan") == 1
    assert "execute_plan" not in names


def test_description_mentions_delegation_not_planning() -> None:
    """Tool description should mention delegation/parallel, not Planner."""
    tool = ExecutePlanTool()
    desc = tool.description

    assert "Delegate" in desc or "delegate" in desc
    assert "parallel" in desc.lower()
    assert "Planner" not in desc
    assert "planner" not in desc.lower()
