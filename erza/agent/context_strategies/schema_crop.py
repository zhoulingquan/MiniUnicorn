"""Schema cropping under RED context pressure (P2-T4).

When context pressure is RED, verbose tool definitions can consume a
significant portion of the prompt. ``SchemaCropStrategy`` truncates long
descriptions and drops optional parameters that carry defaults (the tool
implementation applies the same default when the model omits the field),
keeping required fields and their types so call semantics never change.

Cropped definitions are stashed on ``spec.effective_tool_definitions``;
``ModelRequestService`` prefers them over the raw registry definitions.
Messages themselves pass through untouched.
"""

from __future__ import annotations

from typing import Any

from erza.agent.context_governor import GovernanceContext, PressureLevel

MAX_DESCRIPTION_CHARS = 200


def crop_tool_definitions(
    definitions: list[dict[str, Any]],
    *,
    max_description_chars: int = MAX_DESCRIPTION_CHARS,
) -> list[dict[str, Any]]:
    """Crop verbose tool schemas while preserving call semantics."""
    return [_crop_schema(schema, max_description_chars) for schema in definitions]


def _crop_schema(schema: dict[str, Any], max_description_chars: int) -> dict[str, Any]:
    updated = dict(schema)
    fn = updated.get("function")
    if isinstance(fn, dict):
        updated["function"] = _crop_function(fn, max_description_chars)
        return updated
    if isinstance(updated.get("parameters"), dict) or isinstance(updated.get("description"), str):
        return _crop_function(updated, max_description_chars)
    return updated


def _crop_function(fn: dict[str, Any], max_description_chars: int) -> dict[str, Any]:
    updated = dict(fn)
    description = updated.get("description")
    if isinstance(description, str) and len(description) > max_description_chars:
        updated["description"] = description[:max_description_chars] + "…"
    params = updated.get("parameters")
    if isinstance(params, dict):
        updated["parameters"] = _crop_parameters(params)
    return updated


def _crop_parameters(params: dict[str, Any]) -> dict[str, Any]:
    required = params.get("required")
    required_set = set(required) if isinstance(required, list) else set()
    props = params.get("properties")
    if not isinstance(props, dict) or not props:
        return params
    updated_props: dict[str, Any] = {}
    for name, prop in props.items():
        if name in required_set:
            updated_props[name] = prop
            continue
        if isinstance(prop, dict) and "default" in prop:
            continue
        updated_props[name] = prop
    if len(updated_props) == len(props):
        return params
    updated = dict(params)
    updated["properties"] = updated_props
    return updated


class SchemaCropStrategy:
    """Crop tool schemas under RED pressure; no-op otherwise."""

    name = "schema_crop"

    def apply(
        self,
        messages: list[dict[str, Any]],
        ctx: GovernanceContext,
    ) -> list[dict[str, Any]]:
        pressure = getattr(ctx, "pressure", None)
        if pressure is None or pressure.level is not PressureLevel.RED:
            return messages
        spec = ctx.spec
        try:
            definitions = spec.tools.get_definitions()
        except Exception:
            return messages
        if not definitions:
            return messages
        spec.effective_tool_definitions = crop_tool_definitions(definitions)
        return messages
