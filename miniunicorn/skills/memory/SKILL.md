---
name: memory
description: Legacy Dream files plus governed, journal-backed structured memory.
always: true
---

# Memory

## Structure

- `SOUL.md` — Bot personality and communication style. **Managed by Dream.** Do NOT edit.
- `USER.md` — User profile and preferences. **Managed by Dream.** Do NOT edit.
- `memory/MEMORY.md` — Long-term facts (project context, important events). **Managed by Dream.** Do NOT edit.
- `memory/history.jsonl` — append-only JSONL, not loaded into context. Prefer the built-in `grep` tool to search it.
- `memory/structured/journal.jsonl` — append-only governed record revisions. **Never edit directly.**
- `memory/structured/tags.json` — controlled tag catalog. **Never edit during a turn.**

In governed mode, only active records returned by deterministic exact-scope recall enter context. Candidates never enter context, and USER.md/MEMORY.md are not injected wholesale. Refer to records by their stable `mem_...` IDs and use `/memory-show`, `/memory-promote`, `/memory-revoke`, or `/memory-correct` for management. `/memory-correct` requires non-empty subject, slot and statement fields.

## Search Past Events

`memory/history.jsonl` is JSONL format — each line is a JSON object with `cursor`, `timestamp`, `content`.

- For broad searches, start with `grep(..., path="memory", glob="*.jsonl", output_mode="count")` or the default `files_with_matches` mode before expanding to full content
- Use `output_mode="content"` plus `context_before` / `context_after` when you need the exact matching lines
- Use `fixed_strings=true` for literal timestamps or JSON fragments
- Use `head_limit` / `offset` to page through long histories
- Use `exec` only as a last-resort fallback when the built-in search cannot express what you need

Examples (replace `keyword`):
- `grep(pattern="keyword", path="memory/history.jsonl", case_insensitive=true)`
- `grep(pattern="2026-04-02 10:00", path="memory/history.jsonl", fixed_strings=true)`
- `grep(pattern="keyword", path="memory", glob="*.jsonl", output_mode="count", case_insensitive=true)`
- `grep(pattern="oauth|token", path="memory", glob="*.jsonl", output_mode="content", case_insensitive=true)`

## Important

- **Do NOT edit SOUL.md, USER.md, MEMORY.md, or any file under `memory/structured/`.** Use the memory commands for governed records.
- If you notice outdated information, it will be corrected when Dream runs next.
- Users can view Dream's activity with the `/dream-log` command.
- If `/memory-status` is degraded, do not infer missing facts or edit the journal. Report the diagnostic and use backup/memory Git history for recovery.
