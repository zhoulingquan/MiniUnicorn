# Single-Path Structured Memory Design

## Goal

Erza is still under development and has no production population that needs runtime compatibility modes. Replace the `legacy` / `shadow` / `governed` product modes with one always-on governed structured-memory path.

## Decisions

1. `StructuredMemoryConfig` remains the home of operational tuning, but its public `mode` field is removed. Supplying `mode`, including an old value such as `"governed"`, is a configuration error instead of being silently ignored.
2. `MemoryStore` always constructs the journal repository, lifecycle, and deterministic recall stack during initialization.
3. Normal prompts always use governed semantics: inject customized shared policy and deterministic active-memory recall; never inject `USER.md`, `memory/MEMORY.md`, or `memory/shared/MEMORY_SHARED.md` wholesale.
4. Reflection always uses strict structured JSON (`{"lesson": "..."}`), assigns its stable ID in application code, and persists the structured fields.
5. Dream always extracts proposals into the lifecycle. Its old direct fact-file editing path is removed from runtime behavior.
6. The legacy scanner and `/memory-migrate --dry-run|--apply` remain as explicit developer import tooling. They are not a runtime mode and do not gate startup.
7. A fresh or empty workspace starts immediately with an empty structured journal. Existing legacy files do not block startup; they remain inert until explicitly imported.
8. `/memory-status` reports the architecture as `governed` without reading a configurable mode. It continues reporting journal health, record counts, and optional migration/import state.
9. Recall failure remains fail-closed: no memory facts are injected and a content-free diagnostic is emitted. Journal corruption continues disabling structured writes.
10. No embeddings, vector database, or new dependency is introduced.

## Architecture and Data Flow

```text
history.jsonl + structured reflections
                 |
                 v
          Dream proposals
                 |
                 v
      deterministic lifecycle
      candidate -> active/etc.
                 |
                 v
  append-only structured journal
                 |
                 v
 deterministic scoped lexical recall
                 |
                 v
       Agent prompt injection
```

There is no runtime branch around this flow. Configuration changes budgets and policy thresholds only.

## Public API Changes

- Remove `StructuredMemoryConfig.mode`.
- Remove `AgentRunSpec.structured_memory_mode`.
- Remove mode propagation through `AgentLoop`, `AgentLoopBuilder`, and `Reflection`.
- Keep `structured_memory_config` injection where it carries tuning values, but treat absence as `StructuredMemoryConfig()` rather than an opt-out.
- Remove helpers whose sole purpose is resolving or auditing shadow mode.

This is an intentional development-stage breaking change. Backward-compatible parsing of the three old values would preserve ambiguity and is therefore explicitly rejected.

## Context Rules

For every normal, non-light turn:

- load agent identity/bootstrap content but exclude `USER.md`;
- always include customized `memory/shared/POLICY.md`;
- build a recall query using exact session, project, user, and shared scopes;
- inject only active recall hits within hit/token budgets;
- never inject candidates or entire legacy fact files.

Light/heartbeat contexts continue skipping recall. Subagents continue inheriting the parent session and user identity.

## Migration Semantics

Migration becomes an optional import operation:

- startup never requires a migration manifest;
- `/memory-migrate --dry-run` remains zero-write;
- `/memory-migrate --apply` remains locked, resumable, idempotent, and fail-closed;
- legacy sources remain untouched after import;
- the manifest continues documenting import progress, but is not a feature flag.

## Diagnostics

Recall auditing stays controlled by `recallAuditEnabled`. It records redacted metadata for the single governed path. Shadow-only log events and code are removed. `/memory-status` uses a constant architecture label and must not imply that users can switch modes.

## Testing and Acceptance

The change is accepted when:

1. Configuration defaults contain no mode and reject any supplied `mode` key.
2. A fresh loop starts without migration and initializes a healthy structured stack.
3. A workspace containing legacy files also starts without migration and does not inject those files.
4. Active scoped facts are recalled and injected; candidates are not.
5. Reflection rejects free text and persists a program-generated stable ID for valid JSON.
6. Dream takes only the structured proposal/lifecycle route.
7. Explicit migration remains dry-run safe, idempotent, resumable, and concurrent-safe.
8. No runtime references to the three mode literals remain outside historical design documents or tests explicitly checking rejection.
9. Focused tests, the full agent/config suite, and Ruff pass, apart from independently reproduced pre-existing platform/network failures documented with commands and output.

## Non-Goals

- Deleting legacy source scanners or import code.
- Adding semantic/vector retrieval.
- Redesigning lifecycle policy, evidence ranking, journal format, or recall scoring.
- Automatically importing legacy files at startup.
