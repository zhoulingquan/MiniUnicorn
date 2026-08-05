"""Bounded memory prompt section: always-core first, bounded soul, fallback.

The memory prompt keeps a small always-on core (the ``# Always`` sections of
``USER.md`` / ``memory/MEMORY.md``) plus provenance-tagged recall records,
bounded by token budgets. ``SOUL.md`` stays a bounded part of the chat system
prompt and is never injected here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from miniunicorn.agent.memory_recall import RecallOutcome, RecallRecord, _count_tokens

#: Token budget for the injected ``SOUL.md`` content.
SOUL_TOKEN_BUDGET = 4000
#: Token budget shared by the always-core section and the recall records.
CORE_TOKEN_BUDGET = 1200
#: Token budget for the file-fallback mode.
FILE_FALLBACK_TOKEN_BUDGET = 2400
#: Absolute ceiling for the whole memory section in any mode.
TOTAL_TOKEN_BUDGET = 5200

#: Marker block the prompt builder can splice the memory section into.
START_MARK = "<!-- miniunicorn-memory:start -->"
END_MARK = "<!-- miniunicorn-memory:end -->"

_ALWAYS_HEADING_RE = re.compile(r"^#{1,6}\s*Always\s*$")
_ANY_HEADING_RE = re.compile(r"^#{1,6}\s+.+$")

#: Authoritative files whose ``# Always`` sections form the always-core.
_CORE_SOURCES = (("USER.md", "user"), ("memory/MEMORY.md", "memory"))


@dataclass(frozen=True)
class MemoryPromptPayload:
    text: str
    mode: str
    token_count: int
    diagnostic: str


def _truncate_by_tokens(text: str, budget: int) -> str:
    """Return the longest prefix of *text* within *budget* tokens."""
    if _count_tokens(text) <= budget:
        return text
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if _count_tokens(text[:mid]) <= budget:
            low = mid
        else:
            high = mid - 1
    return text[:low].rstrip()


def extract_always_core(markdown: str, budget: int) -> str:
    """Return the ``# Always`` section, truncated to *budget* tokens.

    Without an Always heading, the leading content is used instead so the
    caller still gets a bounded, useful core.
    """
    lines = markdown.split("\n")
    section: list[str] = []
    collecting = False
    for line in lines:
        if _ALWAYS_HEADING_RE.match(line):
            collecting = True
            continue
        if collecting and _ANY_HEADING_RE.match(line):
            break
        if collecting:
            section.append(line)
    body = "\n".join(section).strip()
    if not body:
        body = markdown
    return _truncate_by_tokens(body, budget)


class MemoryPromptPolicy:
    """Build the bounded memory prompt section for one turn."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace)

    def build(self, recall: RecallOutcome) -> MemoryPromptPayload:
        if recall.fallback_reason == "disabled":
            return MemoryPromptPayload(
                text="", mode="disabled", token_count=0, diagnostic="disabled"
            )
        if recall.fallback_reason is None:
            return self._build_vector(recall)
        return self._build_file_fallback(recall.fallback_reason)

    def bounded_soul(self) -> str:
        """Return the bounded ``SOUL.md`` content (empty when absent)."""
        path = self.workspace / "SOUL.md"
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ""
        return _truncate_by_tokens(text, SOUL_TOKEN_BUDGET)

    def core_texts(self) -> list[str]:
        """Return the always-core texts so recall can de-dup against them."""
        texts: list[str] = []
        for rel_path, _kind in _CORE_SOURCES:
            content = self._read_source(rel_path)
            if content is None:
                continue
            core = extract_always_core(content, CORE_TOKEN_BUDGET)
            if core.strip():
                texts.append(core)
        return texts

    @staticmethod
    def replace_section(messages: list[dict], payload: MemoryPromptPayload) -> None:
        """Splice *payload* into the first system message, replacing the marker block.

        An empty payload removes the marker block (and any content inside it).
        """
        if payload.text:
            block = (
                payload.text
                if START_MARK in payload.text
                else f"{START_MARK}\n{payload.text}\n{END_MARK}"
            )
            for message in messages:
                if MemoryPromptPolicy._has_block(message):
                    start = message["content"].find(START_MARK)
                    end = message["content"].find(END_MARK, start)
                    if end == -1:
                        message["content"] = message["content"][:start].rstrip() + "\n\n" + block
                    else:
                        message["content"] = (
                            message["content"][:start]
                            + block
                            + message["content"][end + len(END_MARK) :]
                        )
                    return
            for message in messages:
                if message.get("role") == "system" and isinstance(
                    message.get("content"), str
                ):
                    message["content"] = message["content"].rstrip("\n") + "\n\n" + block
                    return
            return
        for message in messages:
            if not MemoryPromptPolicy._has_block(message):
                continue
            start = message["content"].find(START_MARK)
            end = message["content"].find(END_MARK, start)
            if end == -1:
                message["content"] = message["content"][:start].rstrip("\n")
            else:
                message["content"] = (
                    message["content"][:start] + message["content"][end + len(END_MARK) :]
                )
            return

    @staticmethod
    def _has_block(message: dict) -> bool:
        return (
            message.get("role") == "system"
            and isinstance(message.get("content"), str)
            and START_MARK in message["content"]
        )

    # --------------------------------------------------------------- builders

    def _build_vector(self, recall: RecallOutcome) -> MemoryPromptPayload:
        sections = [self._core_section(), self._records_section(recall.records)]
        text = "\n\n".join(part for part in sections if part.strip())
        return MemoryPromptPayload(
            text=text, mode="vector", token_count=_count_tokens(text), diagnostic=""
        )

    def _core_section(self) -> str:
        parts: list[str] = []
        for rel_path, kind in _CORE_SOURCES:
            content = self._read_source(rel_path)
            if content is None:
                continue
            core = extract_always_core(content, CORE_TOKEN_BUDGET)
            if core.strip():
                parts.append(f"## 核心记忆 [{kind}]\n{core}")
        return "\n\n".join(parts)

    def _records_section(self, records: tuple[RecallRecord, ...]) -> str:
        if not records:
            return ""
        parts: list[str] = []
        used = 0
        for record in records:
            if used + record.token_count > CORE_TOKEN_BUDGET:
                break
            used += record.token_count
            parts.append(
                f"## 相关记忆 [{record.source_id}] (相似度 {record.similarity:.2f})\n"
                f"{record.text}"
            )
        return "\n\n".join(parts)

    def _build_file_fallback(self, reason: str) -> MemoryPromptPayload:
        parts: list[str] = []
        for rel_path, _kind in _CORE_SOURCES:
            content = self._read_source(rel_path)
            if content is None:
                continue
            section = extract_always_core(content, FILE_FALLBACK_TOKEN_BUDGET)
            if section.strip():
                parts.append(f"## 文件记忆 [{rel_path}]\n{section}")
        combined = _truncate_by_tokens("\n\n".join(parts), FILE_FALLBACK_TOKEN_BUDGET)
        return MemoryPromptPayload(
            text=combined,
            mode="file_fallback",
            token_count=_count_tokens(combined),
            diagnostic=reason,
        )

    def _read_source(self, rel_path: str) -> str | None:
        try:
            return (self.workspace / rel_path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
