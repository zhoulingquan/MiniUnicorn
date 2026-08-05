"""Append-only explicit memory revision journal.

显式记忆是一等公民：每次“记住”都追加一行 JSONL（never rewrite），
创建、更新、还原均为追加新 revision。读取端对坏行只记录 parse error，
不影响其他 memory ID 的 effective 结果。
"""

import json
import os
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from miniunicorn.agent.memory_sources import SourceParseError


@dataclass(frozen=True)
class ExplicitMemoryRevision:
    memory_id: str
    revision: int
    raw_text: str
    normalized_fact: str
    scope: str | None
    created_at: str
    supersedes_revision: int | None = None


def _required(value: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError("raw_text/normalized_fact 不能为空")
    return value


def _optional(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _memory_id_valid(memory_id: str) -> bool:
    try:
        return uuid.UUID(memory_id).version == 4
    except (ValueError, AttributeError, TypeError):
        return False


class ExplicitMemoryJournal:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve(strict=False)
        self.path = self.workspace / "memory" / "explicit.jsonl"

    def append_new(self, raw_text: str, normalized_fact: str, scope: str | None) -> ExplicitMemoryRevision:
        row = ExplicitMemoryRevision(
            memory_id=str(uuid.uuid4()),
            revision=1,
            raw_text=_required(raw_text),
            normalized_fact=_required(normalized_fact),
            scope=_optional(scope),
            created_at=_utc_now(),
            supersedes_revision=None,
        )
        self._append(row)
        return row

    def append_update(self, memory_id: str, raw_text: str, normalized_fact: str, scope: str | None) -> ExplicitMemoryRevision:
        history = self.history(memory_id)
        if not history:
            raise KeyError(memory_id)
        current = history[-1]
        row = ExplicitMemoryRevision(
            memory_id=memory_id,
            revision=current.revision + 1,
            raw_text=_required(raw_text),
            normalized_fact=_required(normalized_fact),
            scope=_optional(scope),
            created_at=_utc_now(),
            supersedes_revision=current.revision,
        )
        self._append(row)
        return row

    def restore(self, memory_id: str, revision: int) -> ExplicitMemoryRevision:
        archived = next((row for row in self.history(memory_id) if row.revision == revision), None)
        if archived is None:
            raise KeyError(f"{memory_id}@{revision}")
        return self.append_update(
            memory_id, archived.raw_text, archived.normalized_fact, archived.scope
        )

    def effective(self) -> list[ExplicitMemoryRevision]:
        rows, _ = self._load()
        latest: dict[str, ExplicitMemoryRevision] = {}
        for row in rows:
            latest[row.memory_id] = row
        return list(latest.values())

    def history(self, memory_id: str) -> list[ExplicitMemoryRevision]:
        rows, _ = self._load()
        return [row for row in rows if row.memory_id == memory_id]

    def errors(self) -> list[SourceParseError]:
        _, errors = self._load()
        return errors

    # -- internal ------------------------------------------------------------

    def _append(self, row: ExplicitMemoryRevision) -> None:
        resolved = self.path
        if self.workspace not in resolved.parents:
            raise ValueError(f"path 超出 workspace: {self.path}")
        resolved.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(asdict(row), ensure_ascii=False, separators=(",", ":")) + "\n"
        with resolved.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def _read(self) -> list[ExplicitMemoryRevision]:
        rows, _ = self._load()
        return rows

    def _load(self) -> tuple[list[ExplicitMemoryRevision], list[SourceParseError]]:
        if not self.path.exists():
            return [], []
        errors: list[SourceParseError] = []
        rows: list[ExplicitMemoryRevision] = []
        next_revision: dict[str, int] = {}
        with self.path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    data = json.loads(stripped)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    errors.append(SourceParseError(str(self.path), line_no, "invalid_json", "JSON 解析失败"))
                    continue
                code = self._validate_row(data, line_no)
                if code is not None:
                    continue
                row = self._row_from_data(data)
                memory_id = row.memory_id
                if memory_id in next_revision:
                    if row.revision != next_revision[memory_id]:
                        errors.append(
                            SourceParseError(
                                str(self.path), line_no, "invalid_revision_sequence",
                                f"memory_id={memory_id!r} 的 revision 不连续（期望 {next_revision[memory_id]}）",
                            )
                        )
                        continue
                elif row.revision != 1:
                    errors.append(
                        SourceParseError(
                            str(self.path), line_no, "invalid_revision_sequence",
                            f"memory_id={memory_id!r} 首条 revision 应为 1",
                        )
                    )
                    continue
                next_revision[memory_id] = row.revision + 1
                rows.append(row)
        return rows, errors

    def _validate_row(self, data: object, line_no: int) -> str | None:
        if not isinstance(data, dict):
            return "invalid_shape"
        memory_id = data.get("memory_id")
        if not _memory_id_valid(memory_id):
            return "invalid_memory_id"
        revision = data.get("revision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            return "invalid_revision"
        if not _optional(data.get("raw_text")) or not _optional(data.get("normalized_fact")):
            return "invalid_text"
        supersedes = data.get("supersedes_revision")
        if supersedes is not None and supersedes != revision - 1:
            return "invalid_supersedes"
        return None

    def _row_from_data(self, data: dict) -> ExplicitMemoryRevision:
        revision = data["revision"]
        return ExplicitMemoryRevision(
            memory_id=data["memory_id"],
            revision=revision,
            raw_text=_optional(data.get("raw_text")) or "",
            normalized_fact=_optional(data.get("normalized_fact")) or "",
            scope=_optional(data.get("scope")),
            created_at=str(data.get("created_at") or ""),
            supersedes_revision=data.get("supersedes_revision"),
        )
