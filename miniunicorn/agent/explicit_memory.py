"""Append-only explicit memory revision journal and conflict-aware service.

显式记忆是一等公民：每次“记住”都追加一行 JSONL（never rewrite），
创建、更新、还原均为追加新 revision。读取端对坏行只记录 parse error，
不影响其他 memory ID 的 effective 结果。

``ExplicitMemoryService`` 在 journal 之上提供“记住”触发识别、与现有记忆的
LLM 关系判断，以及冲突确认/保留/分场景的业务状态机。潜在冲突必须先展示
新旧内容并由用户选择，任何解析或 LLM 失败都不写 journal。
"""

import json
import os
import re
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

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


Relation = Literal["duplicate", "supplement", "conflict", "unrelated"]
ProposalAction = Literal[
    "saved",
    "duplicate",
    "confirmation_required",
    "clarification_required",
    "ignored",
    "failed",
]


@dataclass(frozen=True)
class RelationResult:
    label: Relation
    candidate_memory_id: str | None = None
    normalized_fact: str = ""
    scope: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class CaptureIntent:
    kind: Literal["explicit", "ambiguous", "none"]
    fact: str = ""


@dataclass(frozen=True)
class MemoryProposal:
    proposal_id: str
    action: ProposalAction
    raw_text: str
    normalized_fact: str
    candidate_memory_id: str | None
    candidate_revision: int | None
    user_message: str
    created_at: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: object) -> "MemoryProposal":
        if not isinstance(data, dict):
            raise ValueError("pending_memory_proposal must be a dict")
        missing = [key for key in cls.__dataclass_fields__ if key not in data]
        if missing:
            raise ValueError(f"pending_memory_proposal missing keys: {missing}")
        return cls(
            proposal_id=str(data["proposal_id"]),
            action=str(data["action"]),
            raw_text=str(data["raw_text"]),
            normalized_fact=str(data["normalized_fact"]),
            candidate_memory_id=data.get("candidate_memory_id"),
            candidate_revision=data.get("candidate_revision"),
            user_message=str(data["user_message"]),
            created_at=str(data["created_at"]),
        )


@dataclass(frozen=True)
class MemoryResolution:
    kind: Literal["update", "keep", "both", "confirm"]
    old_scope: str | None = None
    new_scope: str | None = None


_TRIGGER_PREFIXES = (
    "/remember ",
    "/remember",
    "/记住 ",
    "/记住",
    "请记住",
    "帮我记一下，",
    "帮我记一下",
    "以后请一直",
    "remember that ",
    "please remember ",
    "save this to memory",
    "记住",
)
_NEGATION_KEYS = ("不要", "别", "do not", "don't", "never")
_HEDGE_KEYS = ("可能", "也许", "有时候", "暂时", "maybe", "sometimes")
_QUOTED_RE = re.compile(r"[“”‘’\"'「」『』]")

_UPDATE_RESOLUTION = {"更新记忆", "update"}
_KEEP_RESOLUTION = {"保留原记忆", "keep", "不用记"}
_CONFIRM_RESOLUTION = {"确认记住"}


def _strip_lead(text: str) -> str:
    return text.lstrip(" 　,，;；、:：.。")

def _fact_after(prefix: str, text: str) -> str:
    return _strip_lead(text[len(prefix):])


def _has_negation(text: str) -> bool:
    head = text[:8]
    lower_head = text[:20].lower()
    return any(key in head or key in lower_head for key in _NEGATION_KEYS)


def _has_quote(text: str) -> bool:
    known_quote_pairs = (("“", "”"), ("‘", "’"), ("『", "』"), ("「", "」"))
    for open_q, close_q in known_quote_pairs:
        start = text.find(open_q)
        if start != -1:
            end = text.find(close_q, start + 1)
            if end != -1:
                inner = text[start + 1:end]
                for prefix in _TRIGGER_PREFIXES:
                    if prefix in inner.lower():
                        return True
    for char in ("\"", "'"):
        start = text.find(char)
        if start != -1:
            end = text.find(char, start + 1)
            if end != -1:
                inner = text[start + 1:end]
                for prefix in _TRIGGER_PREFIXES:
                    if prefix in inner.lower():
                        return True
    return False


def _isa_discussion_prefix(text_lower: str) -> bool:
    for prefix in _TRIGGER_PREFIXES:
        if not text_lower.startswith(prefix.lower()):
            continue
        rest = text_lower[len(prefix):]
        if rest.startswith("？") or rest.startswith("?"):
            return True
    return False


def _has_hedge(text: str) -> bool:
    lower = text.lower()
    return any(key in lower for key in _HEDGE_KEYS)


class ExplicitMemoryService:
    """Semantic layer for capturing and confirming explicit memories."""

    def __init__(self, journal: ExplicitMemoryJournal, *, control=None, provider=None) -> None:
        self.journal = journal
        self.control = control
        self.provider = provider

    # -- capture ----------------------------------------------------------

    @staticmethod
    def detect(text: str) -> CaptureIntent:
        text = (text or "").strip()
        if not text:
            return CaptureIntent("none")
        lower = text.lower()
        if _has_quote(text):
            return CaptureIntent("none")
        if _isa_discussion_prefix(lower):
            return CaptureIntent("none")
        if _has_negation(text):
            return CaptureIntent("none")
        if not lower.startswith("/") and _has_hedge(text):
            return CaptureIntent("ambiguous", text)
        for prefix in sorted(_TRIGGER_PREFIXES, key=len, reverse=True):
            if lower.startswith(prefix.lower()):
                fact = _fact_after(prefix, text)
                if fact:
                    return CaptureIntent("explicit", fact)
                return CaptureIntent("ambiguous", "")
        return CaptureIntent("none")

    # -- candidate selection ----------------------------------------------

    async def _candidate_facts(self, raw_text: str, limit: int = 3) -> list[ExplicitMemoryRevision]:
        effective = self.journal.effective()
        return sorted(effective, key=lambda row: row.created_at, reverse=True)[:limit]

    # -- relation judgment ------------------------------------------------

    async def classify(
        self, raw_text: str, candidates: list[ExplicitMemoryRevision]
    ) -> RelationResult:
        from miniunicorn.utils.prompt_templates import render_template

        if self.provider is None:
            raise RuntimeError("classify requires an LLM provider")
        messages = [
            {
                "role": "system",
                "content": render_template("agent/memory_relation.md", part="system"),
            },
            {
                "role": "user",
                "content": render_template(
                    "agent/memory_relation.md",
                    part="user",
                    raw_text=raw_text,
                    candidates=_format_candidates(candidates),
                ),
            },
        ]
        response = await self.provider.chat_with_retry(
            messages,
            max_tokens=256,
            temperature=0.0,
            retry_mode=None,
        )
        return _parse_relation_json(response.content, candidates)

    # -- propose / resolve state machine -----------------------------------

    async def propose(self, raw_text: str, classifier) -> MemoryProposal:
        candidates = await self._candidate_facts(raw_text, limit=3)
        try:
            relation = await classifier(raw_text, candidates)
        except Exception:
            return self._proposal(
                action="failed",
                raw_text=raw_text,
                normalized_fact=raw_text,
                candidate_memory_id=None,
                candidate_revision=None,
                user_message="暂时无法分析这条记忆，请稍后再试或换个说法。",
            )
        if relation.label == "duplicate":
            return self._duplicate_proposal(raw_text, relation, candidates)
        if relation.label == "conflict":
            return self._confirmation_proposal(raw_text, relation, candidates)
        if relation.label in ("supplement", "unrelated"):
            saved = self.journal.append_new(
                raw_text, relation.normalized_fact or raw_text, relation.scope
            )
            self._request_reconcile()
            message = (
                f"已保存到长期记忆（来源 memory/explicit.jsonl）："
                f"{saved.normalized_fact}"
            )
            return self._proposal(
                action="saved",
                raw_text=raw_text,
                normalized_fact=saved.normalized_fact,
                candidate_memory_id=saved.memory_id,
                candidate_revision=saved.revision,
                user_message=message,
            )
        raise ValueError(f"unsupported relation: {relation.label}")

    def ambiguous(self, intent: CaptureIntent) -> MemoryProposal:
        raw = intent.fact or ""
        return self._proposal(
            action="clarification_required",
            raw_text=raw,
            normalized_fact=raw,
            candidate_memory_id=None,
            candidate_revision=None,
            user_message="你希望我把这条保存为长期记忆吗？请回复“确认记住”或“不用记”。",
        )

    def parse_resolution(self, content: str) -> MemoryResolution | None:
        text = (content or "").strip()
        if not text:
            return None
        lower = text.lower()
        if lower in _UPDATE_RESOLUTION:
            return MemoryResolution("update")
        if lower in _KEEP_RESOLUTION:
            return MemoryResolution("keep")
        if lower in _CONFIRM_RESOLUTION:
            return MemoryResolution("confirm")
        if lower.startswith("分别适用") or lower.startswith("both"):
            return self._parse_both_resolution(text)
        return None

    def _parse_both_resolution(self, text: str) -> MemoryResolution:
        if text.lower().startswith("分别适用"):
            body = text[len("分别适用"):].lstrip("：: ")
        else:
            body = text[len("both"):].lstrip("：: ")
        old_scope = None
        new_scope = None
        for token in body.replace("；", ";").split(";"):
            token = token.strip()
            key, sep, value = token.partition("=")
            key = key.strip().lower()
            if not sep:
                continue
            stripped = value.strip() or None
            if key in {"旧", "old"}:
                old_scope = stripped
            elif key in {"新", "new"}:
                new_scope = stripped
        return MemoryResolution("both", old_scope=old_scope, new_scope=new_scope)

    async def resolve(
        self, proposal: MemoryProposal, resolution: MemoryResolution, classifier=None
    ) -> MemoryProposal:
        if resolution.kind == "keep":
            return self._derive(
                proposal,
                action="ignored",
                user_message=_keep_message(proposal),
            )
        if resolution.kind == "update":
            return await self._resolve_update(proposal)
        if resolution.kind == "both":
            return await self._resolve_both(proposal, resolution)
        if resolution.kind == "confirm":
            if classifier is None:
                return self._derive(
                    proposal,
                    action="clarification_required",
                    user_message="我需要再确认一次。请回复“确认记住”后我会重新判断。",
                )
            return await self.propose(proposal.raw_text, classifier)
        raise ValueError(f"unsupported resolution: {resolution.kind}")

    async def _resolve_update(self, proposal: MemoryProposal) -> MemoryProposal:
        memory_id = proposal.candidate_memory_id
        if not memory_id:
            return self._derive(
                proposal,
                action="clarification_required",
                user_message="没有可更新的现有记忆，无法执行“更新记忆”。请回复“保留原记忆”或“确认记住”。",
            )
        history = self.journal.history(memory_id)
        if not history:
            return self._derive(
                proposal,
                action="failed",
                user_message="没有找到要更新的记忆，未做任何写入。",
            )
        current = history[-1]
        updated = self.journal.append_update(
            memory_id, proposal.raw_text, proposal.normalized_fact, current.scope
        )
        self._request_reconcile()
        return self._derive(
            proposal,
            action="saved",
            candidate_memory_id=memory_id,
            candidate_revision=updated.revision,
            user_message=(
                f"已更新记忆（revision {updated.revision}，来源 memory/explicit.jsonl）：\n"
                f"- 旧：{current.raw_text}\n"
                f"- 新：{updated.raw_text}"
            ),
        )

    async def _resolve_both(
        self, proposal: MemoryProposal, resolution: MemoryResolution
    ) -> MemoryProposal:
        memory_id = proposal.candidate_memory_id
        if not memory_id:
            return self._derive(
                proposal,
                action="clarification_required",
                user_message="没有可用的现有记忆，无法分别适用。请回复“保留原记忆”或“确认记住”。",
            )
        history = self.journal.history(memory_id)
        if not history:
            return self._derive(
                proposal,
                action="failed",
                user_message="没有找到要分别适用的记忆，未做任何写入。",
            )
        if not resolution.old_scope or not resolution.new_scope:
            return self._derive(
                proposal,
                action="clarification_required",
                user_message="请提供新旧记忆各自的适用范围，例如：分别适用：旧=个人; 新=工作。",
            )
        current = history[-1]
        revised_old = self.journal.append_update(
            memory_id, current.raw_text, current.normalized_fact, resolution.old_scope
        )
        new_fact = proposal.normalized_fact or proposal.raw_text
        new_row = self.journal.append_new(
            proposal.raw_text, new_fact, resolution.new_scope
        )
        self._request_reconcile()
        return self._derive(
            proposal,
            action="saved",
            candidate_memory_id=new_row.memory_id,
            candidate_revision=new_row.revision,
            user_message=(
                f"已分别保存（来源 memory/explicit.jsonl）：\n"
                f"- 旧记忆 revision {revised_old.revision} 适用范围：{resolution.old_scope}，内容：{current.raw_text}\n"
                f"- 新记忆 revision {new_row.revision} 适用范围：{resolution.new_scope}，内容：{new_fact}"
            ),
        )

    def _duplicate_proposal(
        self, raw_text: str, relation: RelationResult, candidates: list[ExplicitMemoryRevision]
    ) -> MemoryProposal:
        row = _candidate_row(candidates, relation.candidate_memory_id)
        if row is None:
            return self._proposal(
                action="duplicate",
                raw_text=raw_text,
                normalized_fact=raw_text,
                candidate_memory_id=relation.candidate_memory_id,
                candidate_revision=relation.candidate_memory_id
                and _candidate_row(candidates, relation.candidate_memory_id).revision,
                user_message="这条记忆与现有记忆重复，无需再次保存。",
            )
        return self._proposal(
            action="duplicate",
            raw_text=raw_text,
            normalized_fact=row.normalized_fact,
            candidate_memory_id=row.memory_id,
            candidate_revision=row.revision,
            user_message=_duplicate_message(row),
        )

    def _confirmation_proposal(
        self, raw_text: str, relation: RelationResult, candidates: list[ExplicitMemoryRevision]
    ) -> MemoryProposal:
        row = _candidate_row(candidates, relation.candidate_memory_id)
        if row is None:
            message = (
                f"这条新记忆与现有记忆可能冲突，请确认：{raw_text}\n"
                "请回复“更新记忆”或“保留原记忆”。"
            )
        else:
            source = f"{row.scope or '通用'} · {row.created_at}"
            message = (
                f"检测到与现有记忆冲突，请确认如何处理：\n\n"
                f"- 现有记忆（{source}）：{row.raw_text}\n"
                f"- 你的新内容：{raw_text}\n\n"
                "请回复以下命令之一：\n"
                "1. 更新记忆 —— 用新内容替换现有记忆\n"
                "2. 保留原记忆 —— 不写入，维持现状\n"
                "3. 分别适用：旧=适用范围; 新=适用范围 —— 新旧各自保留"
            )
        return self._proposal(
            action="confirmation_required",
            raw_text=raw_text,
            normalized_fact=relation.normalized_fact or raw_text,
            candidate_memory_id=row.memory_id if row else relation.candidate_memory_id,
            candidate_revision=row.revision if row else None,
            user_message=message,
        )

    def _proposal(
        self,
        *,
        action: ProposalAction,
        raw_text: str,
        normalized_fact: str,
        candidate_memory_id: str | None,
        candidate_revision: int | None,
        user_message: str,
    ) -> MemoryProposal:
        return MemoryProposal(
            proposal_id=str(uuid.uuid4()),
            action=action,
            raw_text=raw_text,
            normalized_fact=normalized_fact,
            candidate_memory_id=candidate_memory_id,
            candidate_revision=candidate_revision,
            user_message=user_message,
            created_at=_utc_now(),
        )

    def _derive(
        self,
        proposal: MemoryProposal,
        *,
        action: ProposalAction,
        user_message: str,
        candidate_memory_id: str | None = None,
        candidate_revision: int | None = None,
    ) -> MemoryProposal:
        return MemoryProposal(
            proposal_id=proposal.proposal_id,
            action=action,
            raw_text=proposal.raw_text,
            normalized_fact=proposal.normalized_fact,
            candidate_memory_id=(
                candidate_memory_id
                if candidate_memory_id is not None
                else proposal.candidate_memory_id
            ),
            candidate_revision=(
                candidate_revision
                if candidate_revision is not None
                else proposal.candidate_revision
            ),
            user_message=user_message,
            created_at=_utc_now(),
        )

    def _request_reconcile(self) -> None:
        control = getattr(self, "control", None)
        if control is not None and hasattr(control, "request_reconcile"):
            control.request_reconcile()


def _candidate_row(
    candidates: list[ExplicitMemoryRevision], memory_id: str | None
) -> ExplicitMemoryRevision | None:
    if not memory_id:
        return candidates[0] if candidates else None
    return next((row for row in candidates if row.memory_id == memory_id), None)


def _duplicate_message(row: ExplicitMemoryRevision) -> str:
    scope = f"适用范围：{row.scope}" if row.scope else "适用范围：通用"
    return (
        f"这条记忆已经记录过了（来源 memory/explicit.jsonl，{scope}，"
        f"更新于 {row.created_at}）：{row.raw_text}\n无需重复保存。"
    )


def _keep_message(proposal: MemoryProposal) -> str:
    if proposal.candidate_memory_id:
        return (
            f"好的，未写入新记忆，保留原记忆：{proposal.raw_text}"
        )
    return f"好的，未写入新记忆（{proposal.raw_text}）。"


def _format_candidates(candidates: list[ExplicitMemoryRevision]) -> str:
    if not candidates:
        return "（无现有记忆）"
    lines: list[str] = []
    for row in candidates:
        lines.append(
            f"- memory_id: {row.memory_id}\n"
            f"  revision: {row.revision}\n"
            f"  text: {row.normalized_fact}\n"
            f"  scope: {row.scope}\n"
            f"  created_at: {row.created_at}\n"
            f"  source_file: memory/explicit.jsonl"
        )
    return "\n".join(lines)


def _parse_relation_json(
    content: str | None, candidates: list[ExplicitMemoryRevision]
) -> RelationResult:
    if not content or not content.strip():
        raise ValueError("classifier returned empty response")
    import json_repair

    try:
        data = json_repair.loads(content)
    except Exception as exc:
        raise ValueError("classifier returned non-JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("classifier returned non-object")
    label = data.get("label")
    if label not in ("duplicate", "supplement", "conflict", "unrelated"):
        raise ValueError(f"invalid relation label: {label!r}")
    candidate_id = data.get("candidate_memory_id")
    known_ids = {row.memory_id for row in candidates}
    if candidate_id not in (None, "null", "") and candidate_id not in known_ids:
        raise ValueError("candidate_memory_id not among input candidates")
    memory_id = None if candidate_id in (None, "null", "") else candidate_id
    return RelationResult(
        label=label,
        candidate_memory_id=memory_id,
        normalized_fact=str(data.get("normalized_fact") or "").strip(),
        scope=_optional(data.get("scope")),
        reason=str(data.get("reason") or ""),
    )
