---
name: memory
description: Always-on governed, journal-backed structured memory.
always: true
---

# Memory

## Structure

- `SOUL.md` — Bot personality and communication style; bootstrap guidance, not a fact store.
- `memory/history.jsonl` — append-only JSONL, not loaded into context. Prefer the built-in `grep` tool to search it.
- `memory/reflections.jsonl` — strict lessons used as Dream evidence.
- `memory/shared/POLICY.md` — explicitly authored cross-session policy.
- `memory/structured/journal.jsonl` — append-only governed record revisions. **Never edit directly.**
- `memory/structured/tags.json` — controlled tag catalog. **Never edit during a turn.**

Only active records returned by deterministic exact-scope recall enter context. Candidates never enter context. Refer to records by their stable `mem_...` IDs and use `/memory-show`, `/memory-promote`, `/memory-revoke`, or `/memory-correct` for management. `/memory-correct` requires non-empty subject, slot and statement fields.

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

- **Do NOT edit any file under `memory/structured/`.** Use the memory commands for governed records.
- Correct outdated facts explicitly with `/memory-correct`; Dream only proposes facts from history and reflections.
- If `/memory-status` is degraded, do not infer missing facts or edit the journal. Report the diagnostic and use backup/memory Git history for recovery.
