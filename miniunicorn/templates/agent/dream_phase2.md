Update memory files based on the analysis below.
- [FILE] entries: add the described content to the appropriate file
- [FILE-REMOVE] entries: delete the corresponding content from memory files
- [SKILL] entries: create a new skill under skills/<name>/SKILL.md using write_file

## File paths (relative to workspace root)
- SOUL.md
- USER.md
- memory/MEMORY.md
- memory/procedural.jsonl (for procedural lessons only)
- skills/<name>/SKILL.md (for [SKILL] entries only)

Do NOT guess paths.

## Editing rules
- Edit directly — file contents provided below, no read_file needed
- Use exact text as old_text, include surrounding blank lines for unique match
- Batch changes to the same file into one edit_file call
- For deletions: section header + all bullets as old_text, new_text empty
- Surgical edits only — never rewrite entire files
- If nothing to update, stop without calling tools

## Skill creation rules (for [SKILL] entries)
- Use write_file to create skills/<name>/SKILL.md
- Before writing, read_file `{{ skill_creator_path }}` for format reference (frontmatter structure, naming conventions, quality standards)
- **Dedup check**: read existing skills listed below to verify the new skill is not functionally redundant. Skip creation if an existing skill already covers the same workflow.
- Include YAML frontmatter with name and description fields
- Keep SKILL.md under 2000 words — concise and actionable
- Include: when to use, steps, output format, at least one example
- Do NOT overwrite existing skills — skip if the skill directory already exists
- Reference specific tools the agent has access to (read_file, write_file, exec, web_fetch, etc.)
- Skills are instruction sets, not code — do not include implementation code

## Quality
- Every line must carry standalone value
- Concise bullets under clear headers
- When reducing (not deleting): keep essential facts, drop verbose details
- If uncertain whether to delete, keep but add "(verify currency)"

## Procedural Memory (Lessons Learned)

If the Analysis Result contains a "## Recent Reflections" section with lessons learned from failures or mistakes:
1. Extract each distinct lesson as a concise one-liner (actionable principle, not incident detail)
2. Use `read_file` on `memory/procedural.jsonl` to check existing lessons (this file is NOT in the prompt — you must read it first)
3. Use `edit_file` to append new lessons at the end of `memory/procedural.jsonl` — one JSON object per line
4. Format each line as JSON: `{"content": "<lesson>", "source": "reflection"}`
5. Only add lessons that are NOT already present (deduplicate by meaning)
6. If no new lessons are needed, skip — do not write duplicate entries

Example procedural lessons:
- "When grep returns no matches, verify the file path exists with list_dir first"
- "apply_patch requires exact context; re-read the file if it was edited since last read"
- "Complex refactors should be planned step-by-step, not attempted in one edit"
