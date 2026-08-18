<!--
# Shared Policy

This file holds explicit cross-session policy for the agent: hard rules,
prohibitions, and long-term behavioral constraints that must always be
injected into the model context.

Rules for editing:
- Add only normative policy statements, never facts or preferences.
- Facts belong in governed structured memory records (memory/structured/memory.db); journal.jsonl is legacy migration input only.
- This file is edited by the user or an explicit management command; Dream
  never promotes facts into policy automatically.
-->
