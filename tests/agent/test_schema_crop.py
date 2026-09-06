"""P2-T4: Schema cropping under RED context pressure.

``SchemaCropStrategy`` activates only under RED pressure, truncates verbose
tool descriptions, drops optional fields carrying defaults, and keeps
required fields and their types. Cropped definitions are stashed on the run
spec so the model request path uses them.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

from erza.agent.context_governor import (
    ContextGovernor,
    GovernanceContext,
    PressureLevel,
    PressureSignal,
)
from erza.agent.context_strategies.schema_crop import (
    SchemaCropStrategy,
    crop_tool_definitions,
)


def _definitions() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "d" * 600,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "file path"},
                        "encoding": {"type": "string", "default": "utf-8"},
                        "offset": {"type": "integer", "default": 0},
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "name": "flat_tool",
            "description": "f" * 300,
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                },
                "required": ["query"],
            },
        },
    ]


def _spec(definitions: list[dict[str, Any]]) -> Any:
    spec = MagicMock()
    tools = MagicMock()
    tools.get_definitions = MagicMock(return_value=definitions)
    spec.tools = tools
    spec.effective_tool_definitions = None
    return spec


def _ctx(
    spec: Any,
    pressure: PressureSignal | None,
) -> GovernanceContext:
    ctx = GovernanceContext(
        spec=spec,
        tools=spec.tools,
        provider=MagicMock(),
        iteration=0,
        runner=None,
        pressure=pressure,
    )
    return ctx


def _red() -> PressureSignal:
    return PressureSignal(850, 1000, 0.85, PressureLevel.RED)


def _green() -> PressureSignal:
    return PressureSignal(100, 1000, 0.1, PressureLevel.GREEN)


def test_inactive_outside_red_pressure() -> None:
    spec = _spec(_definitions())
    for pressure in (None, _green()):
        ctx = _ctx(spec, pressure)
        result = SchemaCropStrategy().apply([], ctx)
        assert result == []
        assert spec.effective_tool_definitions is None


def test_activates_on_red_pressure() -> None:
    spec = _spec(_definitions())
    ctx = _ctx(spec, _red())
    result = SchemaCropStrategy().apply([{"role": "user", "content": "hi"}], ctx)
    # Messages pass through untouched; the crop lands on the spec.
    assert result == [{"role": "user", "content": "hi"}]
    assert spec.effective_tool_definitions is not None
    assert len(spec.effective_tool_definitions) == 2


def test_descriptions_truncated_to_limit() -> None:
    cropped = crop_tool_definitions(_definitions(), max_description_chars=200)
    for schema in cropped:
        fn = schema.get("function", schema)
        assert len(fn["description"]) <= 201  # 200 chars + ellipsis


def test_optional_fields_with_defaults_removed() -> None:
    spec = _spec(_definitions())
    ctx = _ctx(spec, _red())
    SchemaCropStrategy().apply([], ctx)
    cropped = spec.effective_tool_definitions
    props = cropped[0]["function"]["parameters"]["properties"]
    assert "path" in props  # required field kept
    assert "encoding" not in props  # optional with default dropped
    assert "offset" not in props


def test_required_fields_and_types_preserved() -> None:
    cropped = crop_tool_definitions(_definitions(), max_description_chars=200)
    params = cropped[0]["function"]["parameters"]
    assert params["required"] == ["path"]
    assert params["properties"]["path"]["type"] == "string"
    # Flat schema form is cropped too.
    flat = cropped[1]
    assert flat["parameters"]["properties"]["query"]["type"] == "string"


def test_no_crop_when_nothing_to_trim() -> None:
    defs = [
        {
            "type": "function",
            "function": {
                "name": "t",
                "description": "short",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
    ]
    assert crop_tool_definitions(defs, max_description_chars=200) == defs


def test_builtin_pipeline_places_schema_crop_after_snip() -> None:
    pipeline = list(ContextGovernor.BUILTIN_PIPELINE)
    assert "schema_crop" in pipeline
    assert pipeline.index("schema_crop") > pipeline.index("snip_history")
    # Cleanup pass still runs last.
    assert pipeline[-2:] == ["drop_orphan_tool_results", "backfill_missing_tool_results"]


def test_default_governor_includes_schema_crop_strategy(monkeypatch) -> None:
    monkeypatch.setattr("erza.agent.context_governor.entry_points", lambda **_kw: [])
    governor = ContextGovernor()
    names = [s.name for s in governor._strategies]
    assert "schema_crop" in names


def test_snip_history_still_skips_on_green_with_new_pipeline(monkeypatch) -> None:
    """Pipeline order change must not break GREEN skip behavior."""
    monkeypatch.setattr("erza.agent.context_governor.entry_points", lambda **_kw: [])
    governor = ContextGovernor()
    spec = _spec(_definitions())
    ctx = _ctx(spec, _green())
    messages = [{"role": "user", "content": "hi"}]
    result = governor.govern(messages, ctx)
    assert result == messages
    assert spec.effective_tool_definitions is None


def test_tokens_estimate_of_cropped_definitions() -> None:
    defs = _definitions()
    cropped = crop_tool_definitions(defs, max_description_chars=200)
    raw_tokens = len(json.dumps(defs, ensure_ascii=False)) // 4
    cropped_tokens = len(json.dumps(cropped, ensure_ascii=False)) // 4
    assert cropped_tokens < raw_tokens
    assert cropped_tokens <= raw_tokens * 0.7
