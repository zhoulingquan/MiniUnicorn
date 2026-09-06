"""ToolContext field-set regression lock: the field surface must not grow silently."""

import dataclasses

from erza.tools.context import ToolContext


def test_tool_context_field_set_is_exact():
    names = {f.name for f in dataclasses.fields(ToolContext)}
    assert names == {
        "config",
        "workspace",
        "bus",
        "subagent_manager",
        "cron_service",
        "sessions",
        "file_state_store",
        "provider_snapshot_loader",
        "subagent_registry",
    }
