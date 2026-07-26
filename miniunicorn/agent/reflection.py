"""Reflection mechanism for AgentRunner.

When enabled via AgentRunSpec.enable_reflection=True, the runner periodically
asks the LLM to produce a one-sentence "lesson learned" from the current
turn — triggered on failure (tool error, LLM error, max_iterations) or every
N iterations (reflection_interval). Reflections are appended to
``memory/reflections.jsonl`` for Dream to consolidate into MEMORY.md.

The goal is cross-turn learning: avoid repeating the same mistakes. This
module is self-contained and does not modify the existing ReAct loop.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from miniunicorn.utils.prompt_templates import render_template

# Hard cap on reflection text length to keep reflections.jsonl compact.
_REFLECTION_MAX_CHARS = 500

# File rotation: if reflections.jsonl exceeds this many entries, oldest are dropped.
_MAX_REFLECTIONS = 500


class Reflection:
    """Produces and persists short "lesson learned" entries.

    The Reflection instance is stateless across turns; it just knows how to
    ask the LLM for a reflection and append it to the JSONL file. Callers
    (AgentRunner) decide when to trigger.
    """

    def __init__(self, provider: Any, model: str, workspace: Path | None):
        self.provider = provider
        self.model = model
        self.workspace = workspace
        self._reflections_dir = (
            workspace / "memory" if workspace is not None else None
        )
        self._reflections_file = (
            self._reflections_dir / "reflections.jsonl"
            if self._reflections_dir is not None
            else None
        )

    async def reflect(
        self,
        trigger: str,
        iteration: int,
        context_summary: str,
        messages: list[dict[str, Any]],
        session_key: str | None = None,
    ) -> str | None:
        """Ask the LLM for a one-sentence lesson; persist to JSONL.

        Args:
            trigger: What triggered the reflection ("tool_error", "llm_error",
                "max_iterations", "periodic", "plan_failed").
            iteration: Current iteration index when triggered.
            context_summary: Short description of what happened (e.g. error message).
            messages: The current message list (used as context for the LLM).
            session_key: Optional session identifier for logging.

        Returns:
            The reflection text, or None on failure.
        """
        if self._reflections_file is None:
            logger.debug("Reflection: no workspace; skipping")
            return None
        try:
            # Build a compact context from recent messages (last 6)
            recent = self._format_recent_messages(messages[-6:])
            response = await self.provider.chat_with_retry(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": render_template(
                            "agent/reflection_system.md", strip=True,
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
            # Truncate to keep file compact
            if len(reflection_text) > _REFLECTION_MAX_CHARS:
                reflection_text = reflection_text[:_REFLECTION_MAX_CHARS] + "..."
            if not reflection_text:
                return None
            self._append_reflection({
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "trigger": trigger,
                "iteration": iteration,
                "context": context_summary[:200],
                "reflection": reflection_text,
                "session_key": session_key,
            })
            logger.info(
                "Reflection ({}@{}): {}",
                trigger, iteration, reflection_text[:100],
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

    def _append_reflection(self, entry: dict[str, Any]) -> None:
        """Append a reflection entry to reflections.jsonl."""
        assert self._reflections_file is not None
        try:
            self._reflections_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._reflections_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            # Rotate if too large
            self._maybe_rotate()
        except Exception:
            logger.exception("Failed to append reflection")

    def _maybe_rotate(self) -> None:
        """Drop oldest entries if file exceeds _MAX_REFLECTIONS lines."""
        assert self._reflections_file is not None
        try:
            with open(self._reflections_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) <= _MAX_REFLECTIONS:
                return
            kept = lines[-_MAX_REFLECTIONS:]
            tmp = self._reflections_file.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(kept)
            os.replace(tmp, self._reflections_file)
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
