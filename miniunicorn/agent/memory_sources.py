"""Catalog authoritative memory files into normalized source records.

Markdown files (``USER.md``, ``memory/MEMORY.md``) are split on ATX heading
boundaries and then into bounded chunks. JSONL files preserve their cursor or
event identity. Invalid records are reported in the scan without preventing
other sources from indexing. ``SOUL.md`` is deliberately excluded — it stays
a bounded part of the chat system prompt.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from miniunicorn.utils.helpers import load_bundled_template

_SEP = os.sep
_HEADING_RE = re.compile(r"^(?P<level>#{1,6})\s+(?P<title>.+?)\s*$")
_SENTENCE_ENDINGS = ("。", "！", "？", "；", ". ", "? ", "! ", ";\n", ".\n")

#: Authoritative markdown sources with their retrieval importance.
_MARKDOWN_SOURCES = (
    ("USER.md", "user", 0.9),
    ("memory/MEMORY.md", "memory", 0.8),
)

#: Authoritative JSONL sources with their retrieval importance.
_JSONL_SOURCES = (
    ("memory/history.jsonl", "history", 0.5),
    ("memory/episodic.jsonl", "episodic", 0.6),
    ("memory/procedural.jsonl", "procedural", 0.8),
)


@dataclass(frozen=True)
class MemorySourceRecord:
    source_id: str
    source_type: str
    source_file: str
    source_revision: str
    content_hash: str
    text: str
    importance: float
    active: bool = True
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceParseError:
    source_file: str
    line: int | None
    code: str
    message: str


@dataclass(frozen=True)
class SourceScan:
    records: tuple[MemorySourceRecord, ...]
    errors: tuple[SourceParseError, ...]


def _normalize_text(text: str) -> str:
    """Normalize newlines and strip trailing whitespace per line."""
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _slugify(heading: str) -> str:
    slug = re.sub(r"[^0-9a-zA-Z]+", "-", heading.casefold()).strip("-")
    return slug or "body"


def _chunk_text(text: str, max_chars: int) -> list[str]:
    """Split *text* into bounded chunks preferring blank lines, then sentence ends."""
    text = text.strip("\n")
    if len(text) <= max_chars:
        return [text] if text.strip() else []

    chunks: list[str] = []
    rest = text
    while rest:
        if len(rest) <= max_chars:
            if rest.strip():
                chunks.append(rest)
            break
        head = rest[:max_chars]
        cut = _find_cut(head, max_chars)
        piece = head[:cut].rstrip()
        if piece.strip():
            chunks.append(piece)
        rest = rest[cut:].lstrip("\n")
    return chunks


def _find_cut(head: str, max_chars: int) -> int:
    """Find a chunk boundary inside *head* (a prefix of max length)."""
    blank = head.rfind("\n\n")
    if blank >= max_chars // 2:
        return blank + 2
    for marker in _SENTENCE_ENDINGS:
        pos = head.rfind(marker)
        if pos >= max_chars // 2:
            return pos + len(marker)
    return len(head)


class MemorySourceCatalog:
    """Scan authoritative workspace files into normalized source records."""

    def __init__(self, workspace: Path, *, max_chunk_chars: int = 2400) -> None:
        self.workspace = workspace.resolve(strict=False)
        self.max_chunk_chars = max_chunk_chars

    def scan(self) -> SourceScan:
        records: list[MemorySourceRecord] = []
        errors: list[SourceParseError] = []
        for rel_path, source_type, importance in _MARKDOWN_SOURCES:
            records.extend(self._scan_markdown(rel_path, source_type, importance, errors))
        for rel_path, source_type, importance in _JSONL_SOURCES:
            valid, invalid = self._scan_jsonl(rel_path, source_type, importance)
            records.extend(valid)
            errors.extend(invalid)
        return SourceScan(tuple(records), tuple(errors))

    # ------------------------------------------------------------- path safety

    def _resolve_within_workspace(self, rel_path: str) -> Path | None:
        raw = self.workspace / rel_path
        try:
            resolved = raw.resolve(strict=False)
        except OSError:
            return None
        workspace = self.workspace
        if resolved != workspace and not str(resolved).startswith(
            str(workspace) + _SEP
        ):
            return None
        return resolved

    def _read_text(self, path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return None
        except OSError:
            return None

    # -------------------------------------------------------------- markdown

    def _scan_markdown(
        self,
        rel_path: str,
        source_type: str,
        importance: float,
        errors: list[SourceParseError],
    ) -> list[MemorySourceRecord]:
        path = self._resolve_within_workspace(rel_path)
        if path is None or not path.is_file():
            return []
        content = self._read_text(path)
        if content is None:
            errors.append(
                SourceParseError(rel_path, None, "decode_error", "文件不是有效的 UTF-8 文本")
            )
            return []
        if self._is_bundled_template(rel_path, content):
            return []

        records: list[MemorySourceRecord] = []
        heading_path = "body"
        buffer: list[str] = []
        normalized = _normalize_text(content)
        ident = _content_hash(normalized)
        revision = f"file:{ident[:12]}"
        for line in normalized.split("\n"):
            match = _HEADING_RE.match(line)
            if match:
                self._flush_markdown_section(
                    records, source_type, heading_path, buffer, rel_path, revision, importance
                )
                heading_path = _slugify(match.group("title"))
                buffer = []
            else:
                buffer.append(line)
        self._flush_markdown_section(
            records, source_type, heading_path, buffer, rel_path, revision, importance
        )
        return records

    def _is_bundled_template(self, rel_path: str, content: str) -> bool:
        template = load_bundled_template(rel_path)
        if template is None:
            return False
        return content.strip() == template.strip()

    def _flush_markdown_section(
        self,
        records: list[MemorySourceRecord],
        source_type: str,
        heading_path: str,
        buffer: list[str],
        rel_path: str,
        revision: str,
        importance: float,
    ) -> None:
        body = _normalize_text("\n".join(buffer))
        if not body:
            return
        heading_text = f"## {heading_path}" if heading_path else ""
        for ordinal, chunk in enumerate(_chunk_text(body, self.max_chunk_chars), start=1):
            if not chunk:
                continue
            text = heading_text + "\n\n" + chunk if heading_text else chunk
            normalized = _normalize_text(text)
            records.append(
                MemorySourceRecord(
                    source_id=f"{source_type}:{heading_path}:{ordinal}",
                    source_type=source_type,
                    source_file=rel_path,
                    source_revision=revision,
                    content_hash=_content_hash(normalized),
                    text=normalized,
                    importance=importance,
                    metadata={"heading_path": heading_path, "chunk_index": ordinal},
                )
            )

    # ---------------------------------------------------------------- jsonl

    def _scan_jsonl(
        self,
        rel_path: str,
        source_type: str,
        importance: float,
    ) -> tuple[list[MemorySourceRecord], list[SourceParseError]]:
        path = self._resolve_within_workspace(rel_path)
        if path is None or not path.is_file():
            return [], []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return [], [SourceParseError(rel_path, None, "decode_error", "无法读取 JSONL 文件")]

        records: list[MemorySourceRecord] = []
        errors: list[SourceParseError] = []
        for line_number, raw_line in enumerate(lines, start=1):
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except (ValueError, TypeError):
                errors.append(
                    SourceParseError(rel_path, line_number, "invalid_json", "JSON 解析失败")
                )
                continue
            if not isinstance(row, dict):
                errors.append(
                    SourceParseError(rel_path, line_number, "invalid_shape", "行不是对象")
                )
                continue

            if source_type in ("history", "procedural"):
                cursor = row.get("cursor")
                if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor <= 0:
                    errors.append(
                        SourceParseError(
                            rel_path, line_number, "invalid_cursor", "cursor 缺失或不是正整数"
                        )
                    )
                    continue
                source_id = f"{source_type}:{cursor}"
            else:
                event_id = row.get("event_id")
                if isinstance(event_id, str) and event_id.strip():
                    source_id = f"episodic:{event_id}"
                else:
                    content_hash = _content_hash(_normalize_text(_first_text(row)))
                    source_id = f"episodic:legacy:{line_number}:{content_hash[:12]}"

            text = _normalize_text(_first_text(row))
            if not text:
                errors.append(
                    SourceParseError(rel_path, line_number, "empty_text", "行没有可索引文本")
                )
                continue
            revision = _first_nonempty(row, "revision", "updated_at", "timestamp")
            if revision is None:
                revision = f"line:{line_number}:{_content_hash(text)[:12]}"
            metadata = {
                key: value
                for key, value in row.items()
                if key not in ("content", "summary", "text", "revision", "updated_at", "timestamp")
            }
            records.append(
                MemorySourceRecord(
                    source_id=source_id,
                    source_type=source_type,
                    source_file=rel_path,
                    source_revision=str(revision),
                    content_hash=_content_hash(text),
                    text=text,
                    importance=importance,
                    metadata=metadata,
                )
            )
        return records, errors


def _first_text(row: dict) -> str:
    for key in ("content", "summary", "text"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _first_nonempty(row: dict, *keys: str) -> str | None:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return None
