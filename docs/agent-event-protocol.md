# Agent Event Protocol

MiniUnicorn's WebSocket channel emits typed events from the backend to
connected clients (WebUI and custom integrations). This document defines
the versioning policy, compatibility rules, and regeneration workflow.

## Source of Truth

The Python Pydantic models in
[`miniunicorn/bus/agent_events.py`](../miniunicorn/bus/agent_events.py) are
authoritative. Every outbound frame is constructed as one of the
`AgentEvent` models and serialized through `serialize_agent_event`, which
guarantees:

- `protocol_version` is always present (currently `1`).
- Unknown fields are rejected (`extra="forbid"`).
- The `event` discriminator selects the concrete model.
- Optional fields that are `None` are dropped from the wire payload.

## Versioning Policy

| Change type | Version bump? | Migration required? |
|-------------|---------------|---------------------|
| Additive optional field | No | No |
| Rename or remove an existing field | Yes | Yes — one release with dual-write |
| Semantic change to an existing field | Yes | Yes — one release with dual-write |
| New event type (new `event` discriminator value) | No | No |

`protocol_version` starts at `1`. Additive optional fields do not increment
the version. Renames, removals, or semantic changes to existing fields
require a new version and a migration period.

## Legacy Compatibility

Legacy `OutboundMessage.metadata` flags (`_progress`, `_turn_end`,
`_goal_state`, …) remain supported for one release so non-WebUI channels
and older tests keep working. New code should construct the Pydantic models
directly and attach them via `OUTBOUND_META_AGENT_EVENT`.

> **Batch 3 constraint:** Legacy metadata flags must NOT be removed in
> Batch 3. They are scheduled for deprecation after the migration window
> closes.

## Regeneration

When the Python models change, regenerate the frontend TypeScript types:

```bash
# Python → JSON Schema (writes webui/src/generated/agent-events.schema.json)
uv run python scripts/export_agent_event_schema.py

# JSON Schema → TypeScript types + runtime guards
cd webui
bun run generate:protocol
```

To verify generated files are current without writing:

```bash
uv run python scripts/export_agent_event_schema.py --check
cd webui
bun run check:protocol
```

Both checks run in CI: the `lint` job runs `--check` for the schema, and
the `frontend` job runs `check:protocol`.
