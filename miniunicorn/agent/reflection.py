"""Reflection mechanism for AgentRunner.

When enabled via AgentRunSpec.enable_reflection=True, the runner periodically
asks the LLM to produce a one-sentence "lesson learned" from the current
turn 鈥?triggered on failure (tool error, LLM error, max_iterations) or every
N iterations (reflection_interval). Reflections are appended to
``memory/reflections.jsonl`` for Dream to extract governed memory proposals.

The goal is cross-turn learning: avoid repeating the same mistakes. This
module is self-contained and does not modify the existing ReAct loop.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from miniunicorn.ledger import CallPurpose, call_purpose
from miniunicorn.utils.prompt_templates import render_template

# Hard cap on reflection text length to keep reflections.jsonl compact.
_REFLECTION_MAX_CHARS = 500

# File rotation: if reflections.jsonl exceeds this many entries, oldest are dropped.
_MAX_REFLECTIONS = 500

_REFLECTION_ID_RE = re.compile(r"^rfl_[0-9a-f]{32}$")


def new_reflection_id() -> str:
    """Return a program-generated stable reflection id (never line-number based)."""
    return f"rfl_{uuid.uuid4().hex}"


def _atomic_rewrite_lines(path: Path, lines: list[str]) -> bool:
    """Rewrite a text file with a unique sibling temp, fsync, and atomic replace.

    Returns True only when the canonical file was durably replaced.
    """
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(lines)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except Exception:
        logger.exception("Atomic rewrite failed for {}", path)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return False


class Reflection:
    """Produces and persists short "lesson learned" entries.

    The Reflection instance is stateless across turns; it just knows how to
    ask the LLM for a reflection and append it to the JSONL file. Callers
    (AgentRunner) decide when to trigger.
    """

    def __init__(
        self,
        provider: Any,
        model: str,
        workspace: Path | None,
    ):
        self.provider = provider
        self.model = model
        self.workspace = workspace
        self._reflections_dir = workspace / "memory" if workspace is not None else None
        self._reflections_file = (
            self._reflections_dir / "reflections.jsonl"
            if self._reflections_dir is not None
            else None
        )

    def _parse_structured_response(self, text: str) -> str | None:
        """Parse the strict JSON reflection; return the lesson text.

        Only a JSON object whose exact key set is ``{"lesson"}`` with a
        non-empty trimmed string value is accepted. Invalid JSON, arrays,
        null, missing/extra keys, empty strings, and non-string values return
        None. The stable ``reflection_id`` is assigned by the application
        after parsing and never by the model.
        """
        if not text:
            return None
        try:
            obj = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(obj, dict) or set(obj) != {"lesson"}:
            return None
        lesson = obj.get("lesson")
        if not isinstance(lesson, str) or not lesson.strip():
            return None
        return lesson.strip()

    async def reflect(
        self,
        trigger: str,
        iteration: int,
        context_summary: str,
        messages: list[dict[str, Any]],
        session_key: str | None = None,
        user_key: str | None = None,
    ) -> str | None:
        """Ask the LLM for a one-sentence lesson; persist to JSONL.

        Args:
            trigger: What triggered the reflection ("tool_error", "llm_error",
                "max_iterations", "periodic", "plan_failed").
            iteration: Current iteration index when triggered.
            context_summary: Short description of what happened (e.g. error message).
            messages: The current message list (used as context for the LLM).
            session_key: Optional session identifier for logging.
            user_key: Optional governed user identity (e.g. "user:alice").
                Persisted on the reflection row only when provided, so Dream
                can partition evidence by the exact identity tuple.

        Returns:
            The reflection text, or None on failure.
        """
        if self._reflections_file is None:
            logger.debug("Reflection: no workspace; skipping")
            return None
        try:
            # Build a compact context from recent messages (last 6)
            recent = self._format_recent_messages(messages[-6:])
            async with call_purpose(CallPurpose.REFLECTION):
                response = await self.provider.chat_with_retry(
                    model=self.model,
                    messages=[
                        {
                            "role": "system",
                            "content": render_template(
                                "agent/reflection_system.md",
                                strip=True,
                            ),
                        },
                        {
                            "role": "user",
                            "content": (
                                f"## Trigger\n{trigger} (iteration {iteration})\n\n"
                                f"## What Happened\n{context_summary[:500]}\n\n"
                                f"## Recent Conversation\n{recent}"
                            ),
                        },
                    ],
                    tools=None,
                    tool_choice=None,
                )
            reflection_text = (response.content or "").strip()
            reflection_text = self._parse_structured_response(reflection_text)
            # Truncate to keep file compact
            if reflection_text is not None and len(reflection_text) > _REFLECTION_MAX_CHARS:
                reflection_text = reflection_text[:_REFLECTION_MAX_CHARS] + "..."
            if not reflection_text:
                return None
            entry: dict[str, Any] = {
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "trigger": trigger,
                "iteration": iteration,
                "context": context_summary[:200],
                "reflection": reflection_text,
                "session_key": session_key,
            }
            if user_key:
                entry["user_key"] = user_key
            # The stable id is assigned here, in application code, and is only
            # reported on success once the entry is durably appended.
            entry["reflection_id"] = new_reflection_id()
            entry["lesson"] = reflection_text
            if not self._append_reflection(entry):
                logger.error("Reflection persistence failed; not reporting success")
                return None
            logger.info(
                "Reflection ({}@{}): {}",
                trigger,
                iteration,
                reflection_text[:100],
            )
            return reflection_text
        except Exception:
            logger.exception("Reflection generation failed")
            return None

    def _format_recent_messages(self, messages: list[dict[str, Any]]) -> str:
        """Format a compact view of recent messages for the reflection LLM."""
        lines: list[str] = []
        for msg in messages:
            role = msg.get("role", "?")
            content = msg.get("content", "")
            if isinstance(content, list):
                # Extract text from content blocks
                text_parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                content = " ".join(text_parts)
            elif not isinstance(content, str):
                content = str(content)
            # Truncate each message
            if len(content) > 200:
                content = content[:200] + "..."
            lines.append(f"[{role}] {content}")
        return "\n".join(lines) if lines else "(empty)"

    def _append_reflection(self, entry: dict[str, Any]) -> bool:
        """Append a reflection entry to reflections.jsonl.

        Returns True only when the entry was durably written.
        """
        assert self._reflections_file is not None
        try:
            self._reflections_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._reflections_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            # Rotate if too large
            self._maybe_rotate()
            return True
        except Exception:
            logger.exception("Failed to append reflection")
            return False

    def _maybe_rotate(self) -> None:
        """Prune consumed prefix (cursor-safe); never drop unconsumed entries.

        When the file exceeds ``_MAX_REFLECTIONS`` lines, exactly the lines
        consumed by Dream (up to the sibling ``.reflections_cursor``) are
        atomically pruned and the cursor reset to 0. Unconsumed entries are
        never discarded merely to satisfy the cap: losslessness is secondary
        only to correctness, so a large backlog is retained until consumed.
        """
        assert self._reflections_file is not None
        try:
            with open(self._reflections_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) <= _MAX_REFLECTIONS:
                return
            cursor_path = self._reflections_dir / ".reflections_cursor"
            try:
                cursor = int(cursor_path.read_text(encoding="utf-8").strip())
            except (FileNotFoundError, ValueError):
                cursor = 0
            if cursor <= 0:
                # All entries are unconsumed: retain them until Dream consumes.
                return
            if cursor > len(lines):
                # A stale physical-line cursor cannot safely prove that any
                # current entry was consumed. Reset it and retain everything.
                _atomic_rewrite_lines(cursor_path, ["0\n"])
                return
            kept = lines[cursor:]
            if len(kept) == len(lines):
                return
            # Reset the physical-line cursor before renumbering the canonical
            # file. If the subsequent file rewrite fails, Dream may process
            # already-consumed entries again, but no unconsumed entry can be
            # skipped. The opposite order can permanently skip the new prefix
            # when the cursor reset fails after a successful file rewrite.
            if not _atomic_rewrite_lines(cursor_path, ["0\n"]):
                return
            _atomic_rewrite_lines(self._reflections_file, kept)
        except Exception:
            logger.exception("Reflection rotation failed")

    def read_unprocessed(self, since_timestamp: str | None = None) -> list[dict[str, Any]]:
        """Read reflections newer than *since_timestamp* (for Dream integration).

        Returns entries in chronological order.
        """
        if self._reflections_file is None or not self._reflections_file.exists():
            return []
        results: list[dict[str, Any]] = []
        try:
            with open(self._reflections_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = entry.get("timestamp", "")
                    if since_timestamp is None or ts > since_timestamp:
                        results.append(entry)
        except Exception:
            logger.exception("Failed to read reflections")
        return results
