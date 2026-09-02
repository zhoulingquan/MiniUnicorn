"""LLM-powered subagent definition generator.

Given a natural-language description of what the user wants the subagent to
do, this module asks the configured LLM to produce a properly formatted
``.md`` definition file (YAML frontmatter + Markdown body) compatible with
:class:`miniunicorn.agent.subagent_registry.SubagentRegistry`.

The generator only produces the file content; persistence is left to the
caller (the AgentsView HTTP route returns a preview, the ``create_agent``
tool saves to disk).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from loguru import logger

from miniunicorn.ledger import CallPurpose, call_purpose

if TYPE_CHECKING:
    from miniunicorn.providers.base import LLMProvider


# Tool names exposed to the LLM when generating a subagent definition.
# Kept in sync with the built-in tool registry; ``delegate`` and
# ``create_agent`` are excluded because subagents should not recursively
# spawn other agents.
_AVAILABLE_TOOLS = (
    "read_file",
    "write_file",
    "edit_file",
    "grep",
    "find_files",
    "list_dir",
    "exec",
    "run_cli_app",
    "web_fetch",
    "web_search",
    "deep_research",
    "apply_patch",
    "spawn",
    "delegate_plan",
    "cron",
    "message",
    "complete_goal",
    "long_task",
    "list_exec_sessions",
    "write_stdin",
    "my",
)

# Match the frontmatter delimiter the SubagentRegistry expects.
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)

# Extract the ``name:`` field from frontmatter text (first occurrence).
_NAME_LINE_RE = re.compile(r"^\s*name\s*:\s*(\S+)\s*$", re.MULTILINE)


_SYSTEM_PROMPT = """\
You are a subagent definition generator for the miniunicorn AI agent framework.

Your job: given a natural-language description of what a user wants a
subagent to do, produce a single subagent definition file in Markdown with
YAML frontmatter. The output must be ready to save as ``agents/<name>.md``
and parseable by miniunicorn's SubagentRegistry.

## Output format

The response MUST start with the frontmatter delimiter ``---`` and contain
nothing else but the .md file content (no explanations, no code fences,
no leading/trailing prose).

```
---
name: <kebab-case-english-name>
description: <one sentence in the user's language describing WHEN the main
  agent should delegate to this subagent>
tools: <comma-separated subset of the available tools, or empty to allow all>
model: <optional model override, omit if not needed>
---
<Markdown body: the subagent's system prompt. Written in the user's
language. Tells the subagent its role, scope of work, and output format.>
```

Rules:
- ``name`` is required, kebab-case, English ASCII (e.g. ``code-reviewer``).
- ``description`` is required and must describe the delegation trigger
  (when the main agent should hand off to this subagent), not just the
  subagent's role. This is what the main LLM reads to decide delegation.
- ``tools`` is optional. Pick the minimal set the subagent actually needs
  from the available tools list below. Leave empty (or omit the field) to
  grant all tools. Use an empty quoted string ``""`` to grant no tools.
- ``model`` is optional. Omit unless the user explicitly requests a model.
- The body is the subagent's system prompt: concise, role-focused, and
  ends with the expected output format.
- Do not include the ``create_agent`` or ``delegate`` tools in ``tools``;
  subagents must not recursively spawn agents.

## Available tools

The following tool names may be listed in the ``tools`` field:

{tools_block}

## Examples

### Example 1

Input description: "Review my Python code for quality issues."

Output:
---
name: code-reviewer
description: Reviews code for quality, best practices, and potential issues when user asks for code review or quality check
tools: read_file, grep, find_files, list_dir
---
You are a code review expert. Your job is to professionally and rigorously
review the code the user specifies, surface potential issues, and improve
code quality.

## Scope

- Code quality: naming, readability, duplication, complexity, idioms
- Best practices: design patterns, error handling, resource cleanup
- Potential bugs: null references, boundary conditions, concurrency, type errors
- Security: missing input validation, secret leakage (deep audits belong to security-audit)

## Output format

A list of issues sorted by severity (Critical / Warning / Suggestion).
For each issue: file location, description, and fix suggestion. End with
an overall quality score (1-10) and the top 1-2 improvements.

### Example 2

Input description: "Write unit tests for my functions."

Output:
---
name: test-writer
description: Generates unit tests for specified files or functions when user asks for tests
tools: read_file, write_file, edit_file, grep, find_files
---
You are a test-writing expert. Your job is to write comprehensive,
maintainable unit tests for the code the user specifies, ensuring tests
catch regressions effectively.

## Scope

- Use pytest style (functions, fixtures, parametrize, plain asserts)
- Cover: normal paths, boundary conditions, exceptions, branch coverage
- Isolate external dependencies with mock/monkeypatch
- Test names describe "scenario + expected" behavior

## Output format

- Files named ``test_<module>.py`` under ``tests/``
- Each test function starts with ``test_``
- Use ``@pytest.mark.parametrize`` for multi-input coverage
- After writing, summarize: covered functions/branches, intentionally
  uncovered scenarios and why, and the command to run the tests.
"""


class AgentGenerator:
    """Generate a subagent ``.md`` definition from a natural-language description."""

    def __init__(self, provider: "LLMProvider", model: str | None = None):
        self._provider = provider
        self._model = model

    async def generate(self, description: str) -> str:
        """Generate ``.md`` content (frontmatter + body) from *description*.

        Returns the raw .md file content as a string. Raises
        :class:`ValueError` if the LLM output cannot be coerced into the
        expected frontmatter+body shape.
        """
        description = (description or "").strip()
        if not description:
            raise ValueError("description must not be empty")

        tools_block = "\n".join(f"- ``{name}``" for name in _AVAILABLE_TOOLS)
        system_prompt = _SYSTEM_PROMPT.format(tools_block=tools_block)

        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    "Generate a subagent definition (.md file content) for the "
                    "following requirement. Output ONLY the .md content starting "
                    "with the ``---`` frontmatter delimiter.\n\n"
                    f"Requirement:\n{description}"
                ),
            },
        ]

        model = self._model or self._provider.get_default_model()
        async with call_purpose(CallPurpose.TOOL):
            response = await self._provider.chat_with_retry(
                messages=messages,
                tools=None,
                model=model,
                max_tokens=2048,
                temperature=0.3,
            )

        raw = (response.content or "").strip()
        if not raw:
            raise ValueError("LLM returned empty content for agent generation")

        content = _extract_markdown_content(raw)
        if not content:
            raise ValueError("LLM output did not contain a valid frontmatter block (---...---)")

        # Validate that the parsed frontmatter carries a non-empty name and
        # description; SubagentRegistry._parse_file would otherwise skip it.
        match = _FRONTMATTER_RE.match(content)
        if not match:
            raise ValueError("Generated content has no parseable frontmatter")
        fm_text, _body = match.group(1), match.group(2)
        name_match = _NAME_LINE_RE.search(fm_text)
        if not name_match:
            raise ValueError("Generated frontmatter is missing a 'name' field")
        if not _has_field(fm_text, "description"):
            raise ValueError("Generated frontmatter is missing a 'description' field")

        logger.info("Generated subagent definition for name={}", name_match.group(1))
        return content


def _extract_markdown_content(raw: str) -> str | None:
    """Strip code fences / leading prose and return the ``---...`` block.

    LLMs occasionally wrap the answer in a ```markdown fence or prefix it
    with chit-chat. We want the substring starting at the first ``---``
    line and ending at the matching closing ``---``.
    """
    if not raw:
        return None

    text = raw

    # Strip common code fences: ```markdown ... ``` or ``` ... ```
    fence_match = re.search(
        r"```(?:markdown|md|yaml)?\s*\n(.*?)\n```",
        text,
        re.DOTALL,
    )
    if fence_match:
        text = fence_match.group(1).strip()

    # Find the first ``---`` delimiter.
    start = text.find("---")
    if start == -1:
        return None
    # Find the closing ``---`` after the opening one.
    end = text.find("\n---", start + 3)
    if end == -1:
        return None
    # Include everything through the end of the body.
    return text[start:].strip()


def _has_field(frontmatter_text: str, field: str) -> bool:
    """Return True if *frontmatter_text* declares a non-empty ``field:``."""
    pattern = re.compile(rf"^\s*{re.escape(field)}\s*:\s*\S", re.MULTILINE)
    return bool(pattern.search(frontmatter_text))


def extract_name(content: str) -> str | None:
    """Extract the ``name`` field from a generated .md string.

    Returns ``None`` if the content has no parseable frontmatter or no
    ``name`` field. Useful for callers that need to derive a filename
    from LLM-generated content.
    """
    if not content:
        return None
    match = _FRONTMATTER_RE.match(content.strip())
    if not match:
        return None
    name_match = _NAME_LINE_RE.search(match.group(1))
    if not name_match:
        return None
    name = name_match.group(1).strip()
    # Strip surrounding quotes if present.
    if len(name) >= 2 and name[0] in "\"'" and name[-1] == name[0]:
        name = name[1:-1]
    return name or None
