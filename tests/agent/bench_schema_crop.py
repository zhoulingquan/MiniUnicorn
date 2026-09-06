"""P2-T4 benchmark: schema cropping on a large tool registry.

Builds 50+ synthetic tool definitions with realistic verbose descriptions
and optional defaulted parameters, then measures the token estimate before
and after cropping under RED pressure. Asserts at least 30% reduction.
"""

from __future__ import annotations

import json
from typing import Any

from erza.agent.context_strategies.schema_crop import crop_tool_definitions


def _make_definitions(count: int = 50) -> list[dict[str, Any]]:
    definitions: list[dict[str, Any]] = []
    for i in range(count):
        definitions.append(
            {
                "type": "function",
                "function": {
                    "name": f"tool_{i}",
                    "description": (
                        f"Tool number {i} performs a wide variety of nuanced operations "
                        "including fetching remote resources, transforming payloads, "
                        "validating schemas, and retrying transient failures with "
                        "exponential backoff. " * 8
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "target": {
                                "type": "string",
                                "description": "The target to operate on",
                            },
                            "verbose": {"type": "boolean", "default": False},
                            "timeout_s": {"type": "number", "default": 30.0},
                            "mode": {"type": "string", "default": "standard"},
                            "retries": {"type": "integer", "default": 3},
                        },
                        "required": ["target"],
                    },
                },
            }
        )
    return definitions


def _token_estimate(definitions: list[dict[str, Any]]) -> int:
    return len(json.dumps(definitions, ensure_ascii=False)) // 4


def test_benchmark_schema_crop_reduces_tool_tokens_30_percent() -> None:
    definitions = _make_definitions(50)
    assert len(definitions) >= 50

    raw_tokens = _token_estimate(definitions)
    assert raw_tokens > 0

    cropped = crop_tool_definitions(definitions)
    cropped_tokens = _token_estimate(cropped)

    reduction = 1.0 - (cropped_tokens / raw_tokens)
    assert reduction >= 0.30, (
        f"schema cropping only reduced tool tokens by {reduction:.1%}; "
        f"expected >= 30% ({raw_tokens} -> {cropped_tokens} tokens)"
    )


def test_benchmark_preserves_required_fields_for_all_tools() -> None:
    definitions = _make_definitions(50)
    cropped = crop_tool_definitions(definitions)
    for original, cropped_schema in zip(definitions, cropped):
        assert cropped_schema["function"]["parameters"]["required"] == ["target"]
        assert cropped_schema["function"]["parameters"]["properties"]["target"]["type"] == "string"
        assert original["function"]["name"] == cropped_schema["function"]["name"]
