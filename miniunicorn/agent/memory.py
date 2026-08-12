"""Memory system: pure file I/O store, lightweight Consolidator, and Dream processor."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import weakref
from contextlib import suppress
from dataclasses import replace as dataclasses_replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterator

import tiktoken
from filelock import FileLock
from loguru import logger

from miniunicorn.agent.runner import AgentRunner, AgentRunSpec
from miniunicorn.agent.tools.registry import ToolRegistry
from miniunicorn.session.manager import Session
from miniunicorn.utils.gitstore import GitStore
from miniunicorn.utils.helpers import (
    ensure_dir,
    estimate_message_tokens,
    estimate_prompt_tokens_chain,
    find_legal_message_start,
    strip_think,
    truncate_text,
)
from miniunicorn.utils.prompt_templates import render_template

if TYPE_CHECKING:
    from miniunicorn.agent.memory_lifecycle import StructuredMemoryLifecycle
    from miniunicorn.agent.memory_models import MemoryStatus, RecallQuery, RecallResult
    from miniunicorn.agent.memory_recall import StructuredMemoryRecall
    from miniunicorn.agent.memory_repository import StructuredMemoryRepository
    from miniunicorn.config.schema import StructuredMemoryConfig
    from miniunicorn.providers.base import LLMProvider
    from miniunicorn.session.manager import SessionManager


# ---------------------------------------------------------------------------
# MemoryStore — pure file I/O layer
# ---------------------------------------------------------------------------


def _parse_datetime_loose(value: str | None) -> datetime | None:
    """Parse a history/reflection timestamp into an aware datetime.

    Naive timestamps are interpreted as local time so the result always
    carries a timezone (required by ``EvidenceRef.observed_at``).
    """
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.astimezone()
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.astimezone()
    except ValueError:
        return None


def reflection_evidence_id(entry: Mapping[str, Any]) -> str:
    """Return a stable evidence id for a reflection entry.

    New entries carry a program-generated ``rfl_<32 hex>`` id and are used
    verbatim. Legacy ``R<number>`` or missing ids are not trustworthy (they
    depend on mutable line numbers), so a deterministic legacy id is derived
    from the canonicalized entry content. ``_line`` is cursor bookkeeping and
    never included in the digest.
    """
    raw = str(entry.get("reflection_id") or "")
    if re.fullmatch(r"rfl_[0-9a-f]{32}", raw):
        return raw
    canonical = json.dumps(
        {key: value for key, value in entry.items() if key != "_line"},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return f"rfl_legacy_{digest}"


class MemoryStore:
    """Pure file I/O for memory files: MEMORY.md, history.jsonl, SOUL.md, USER.md."""

    _DEFAULT_MAX_HISTORY = 1000
    # Episodic/procedural/reflections 文件条数上限：超出时截断最旧条目。
    # episodic 是事件流（按时间），procedural 是教训（Dream 提炼），
    # reflections 是一句话反思（Reflection 写入，Dream 消费）。
    # 这些文件只增不减会导致长期使用后磁盘膨胀，故设上限。
    _MAX_EPISODIC_ENTRIES = 500
    _MAX_PROCEDURAL_ENTRIES = 300
    _MAX_SHARED_PROCEDURAL_ENTRIES = 200
    _LEGACY_ENTRY_START_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}[^\]]*)\]\s*")
    _LEGACY_TIMESTAMP_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\]\s*")
    _LEGACY_RAW_MESSAGE_RE = re.compile(
        r"^\[\d{4}-\d{2}-\d{2}[^\]]*\]\s+[A-Z][A-Z0-9_]*(?:\s+\[tools:\s*[^\]]+\])?:"
    )

    # Single-Writer 路径白名单（借鉴 MiMo Code）：
    # 每个文件只有一个允许的 writer 角色，其他角色不应直接写入。
    # 这是文档化的不变量（invariant），MemoryStore 自身的写入方法已通过
    # __init__ 固定路径天然满足；此处记录用于审计、未来工具层强制校验、
    # 以及防止 Consolidator/Dream/主 Agent 之间职责混淆。
    #
    # 角色说明：
    #   - "main_agent": 主 Agent 通过 EditFileTool 间接写入（唯一允许的文件是 notes.md）
    #   - "consolidator": Consolidator 归档时写入 history.jsonl + 清空 notes.md
    #   - "dream": Dream 提炼后通过 EditFileTool 写入 MEMORY/SOUL/USER + 推进 cursor
    #   - "memory_store": MemoryStore 内部迁移/截断逻辑（_maybe_migrate_legacy_history 等）
    _WRITER_WHITELIST: dict[str, set[str]] = {
        "notes.md": {"main_agent", "consolidator"},
        "memory/MEMORY.md": {"dream", "memory_store"},
        "SOUL.md": {"dream", "memory_store"},
        "USER.md": {"dream", "memory_store"},
        "memory/history.jsonl": {"consolidator", "memory_store"},
        "memory/.cursor": {"consolidator", "memory_store"},
        "memory/.dream_cursor": {"dream", "memory_store"},
        "memory/.reflections_cursor": {"dream", "memory_store"},
        "memory/episodic.jsonl": {"dream", "memory_store"},
        "memory/procedural.jsonl": {"dream", "memory_store"},
        "memory/reflections.jsonl": {"dream", "reflection", "memory_store"},
        "memory/shared/MEMORY_SHARED.md": {"dream", "memory_store"},
        "memory/shared/procedural_shared.jsonl": {"dream", "memory_store"},
        "memory/shared/POLICY.md": {"memory_store"},
        "memory/structured/journal.jsonl": {"memory_store"},
        "memory/structured/tags.json": {"memory_store"},
        "memory/structured/migration-v1.json": {"memory_store"},
    }

    def __init__(
        self,
        workspace: Path,
        max_history_entries: int = _DEFAULT_MAX_HISTORY,
        structured_config: StructuredMemoryConfig | None = None,
    ):
        self.workspace = workspace
        self.max_history_entries = max_history_entries
        self.memory_dir = ensure_dir(workspace / "memory")
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.recall_audit_file = self.memory_dir / "structured" / "recall-audit.jsonl"
        self.recall_audit_lock_file = self.memory_dir / "structured" / "recall-audit.lock"
        self.history_file = self.memory_dir / "history.jsonl"
        self.legacy_history_file = self.memory_dir / "HISTORY.md"
        self.soul_file = workspace / "SOUL.md"
        self.user_file = workspace / "USER.md"
        # notes.md: 主 Agent 唯一被允许的持久化写入通道（借鉴 MiMo Code）。
        # 主 Agent 用 write_file/edit_file 往这里 append 零散发现，Consolidator
        # 在每次归档时读取内容路由到 summary、然后清空文件。这样主 Agent
        # 不需要自己维护结构化记忆，但仍能记录跨 turn 的临时笔记。
        self.notes_file = workspace / "notes.md"
        self._cursor_file = self.memory_dir / ".cursor"
        self._dream_cursor_file = self.memory_dir / ".dream_cursor"
        # Layered memory stores (P1-1): episodic events and procedural lessons.
        # semantic memory remains MEMORY.md (existing). Both new files are
        # append-only JSONL, mirroring history.jsonl's on-disk format.
        self._episodic_file = self.memory_dir / "episodic.jsonl"
        self.procedural_file = self.memory_dir / "procedural.jsonl"
        self._reflections_cursor_file = self.memory_dir / ".reflections_cursor"
        # Cross-session shared layer (P2-2): global facts and lessons that apply
        # to every session rather than just the current one. Lives under
        # ``memory/shared/`` so it stays separate from per-session stores.
        self.shared_dir = ensure_dir(workspace / "memory" / "shared")
        self.shared_memory_file = self.shared_dir / "MEMORY_SHARED.md"
        self.shared_procedural_file = self.shared_dir / "procedural_shared.jsonl"
        self._corruption_logged = False  # rate-limit non-int cursor warning
        self._oversize_logged = False  # rate-limit oversized-entry warning
        # 文件内容缓存：key=Path, value=(st_mtime_ns, st_size, content)。
        # build_system_prompt 每次 turn 都会读取 MEMORY.md/SOUL.md/USER.md，
        # 这些文件在单次 turn 内不会变化（只有 Dream 会改写，且 Dream 独占运行）。
        # 通过 mtime+size 校验避免重复磁盘 IO。写入时调用 _invalidate_cache。
        self._file_cache: dict[Path, tuple[int, int, str]] = {}
        self._git = GitStore(
            workspace,
            tracked_files=[
                "SOUL.md",
                "USER.md",
                "notes.md",
                "memory/MEMORY.md",
                "memory/.dream_cursor",
                "memory/episodic.jsonl",
                "memory/procedural.jsonl",
                "memory/shared/MEMORY_SHARED.md",
                "memory/shared/procedural_shared.jsonl",
                "memory/structured/journal.jsonl",
                "memory/structured/tags.json",
                "memory/shared/POLICY.md",
                "memory/structured/migration-v1.json",
            ],
        )
        self._maybe_migrate_legacy_history()
        # Governed structured memory (C2): journal-backed facts + lifecycle + recall.
        self.structured_config = structured_config
        self.structured_repository: StructuredMemoryRepository | None = None
        self.structured_lifecycle: StructuredMemoryLifecycle | None = None
        self.structured_recall: StructuredMemoryRecall | None = None
        mode = structured_config.mode if structured_config is not None else "legacy"
        if mode in ("shadow", "governed"):
            self._ensure_structured_bundled_files()
            self._build_structured_stack()
            logger.info(
                "structured_memory_initialized mode={} health={}",
                mode,
                self.structured_repository.health.state,  # type: ignore[union-attr]
            )

    @property
    def project_scope_key(self) -> str:
        """Stable project scope key for this workspace (deterministic)."""
        import hashlib

        root = str(self.workspace.resolve()).casefold()
        digest = hashlib.sha256(root.encode("utf-8")).hexdigest()[:12]
        return f"project:{digest}"

    @property
    def git(self) -> GitStore:
        return self._git

    # ------------------------------------------------------------------
    # Governed structured memory (C2) — repository/lifecycle/recall wiring
    # ------------------------------------------------------------------

    @staticmethod
    def _bundled_template_path(name: str) -> Path:
        return Path(__file__).resolve().parent.parent / "templates" / "memory" / name

    def _ensure_structured_bundled_files(self) -> None:
        """Install canonical structured templates without overwriting user files."""
        canonical_tags = self.workspace / "memory" / "structured" / "tags.json"
        legacy_tags = canonical_tags.with_name("TAGS.json")
        if not canonical_tags.exists() and legacy_tags.exists():
            ensure_dir(canonical_tags.parent)
            canonical_tags.write_text(legacy_tags.read_text(encoding="utf-8"), encoding="utf-8")
        targets = {
            "TAGS.json": canonical_tags,
            "POLICY.md": self.workspace / "memory" / "shared" / "POLICY.md",
        }
        for name, target in targets.items():
            if target.exists():
                continue
            ensure_dir(target.parent)
            with self._bundled_template_path(name).open("r", encoding="utf-8") as source, target.open(
                "w", encoding="utf-8", newline="\n"
            ) as dest:
                dest.write(source.read())

    def _build_structured_stack(self) -> None:
        """Construct repository -> lifecycle -> recall from the structured config.

        ``None`` config means the caller opted into ``legacy`` mode; the stack
        is then built lazily on the first structured command.
        """
        if self.structured_config is None:
            from miniunicorn.agent.memory_models import MemoryWriteError

            raise MemoryWriteError("structured stack requires a structured config")
        from miniunicorn.agent.memory_lifecycle import (
            LifecyclePolicy,
            StructuredMemoryLifecycle,
        )
        from miniunicorn.agent.memory_recall import StructuredMemoryRecall
        from miniunicorn.agent.memory_repository import StructuredMemoryRepository

        repository = StructuredMemoryRepository(
            self.workspace, lock_timeout_s=self.structured_config.lock_timeout_s
        )
        policy = LifecyclePolicy(
            auto_promote_verified=self.structured_config.auto_promote_verified,
            min_repeated_evidence=self.structured_config.min_repeated_evidence,
            candidate_ttl_days=self.structured_config.candidate_ttl_days,
        )
        self.structured_repository = repository
        self.structured_lifecycle = StructuredMemoryLifecycle(repository, policy)
        self.structured_recall = StructuredMemoryRecall(repository, repository.tag_catalog)

    def _structured_stack_or_build(self) -> StructuredMemoryRepository:
        """Lazily materialize the structured stack (legacy deferral)."""
        if self.structured_repository is None:
            if self.structured_config is None:
                from miniunicorn.config.schema import StructuredMemoryConfig

                self.structured_config = StructuredMemoryConfig(mode="legacy")
            self._ensure_structured_bundled_files()
            self._build_structured_stack()
        return self.structured_repository  # type: ignore[return-value]

    def read_shared_policy(self) -> str:
        """Return shared POLICY.md body, or "" when missing or still the bundled
        template (a template holds only Markdown comments, never instructions)."""
        policy = self.workspace / "memory" / "shared" / "POLICY.md"
        if not policy.exists():
            return ""
        body = policy.read_text(encoding="utf-8")
        try:
            if body == self._bundled_template_path("POLICY.md").read_text(encoding="utf-8"):
                return ""
        except OSError:
            pass
        return body

    def recall_structured(self, query: RecallQuery) -> RecallResult:
        """Run deterministic recall, building the structured stack on demand."""
        self._structured_stack_or_build()
        return self.structured_recall.recall(query)  # type: ignore[union-attr]

    def write_recall_audit(self, query: RecallQuery, result: RecallResult) -> None:
        """Append a content-free recall audit and retain only the latest 1000 rows."""
        self._assert_path_in_workspace(self.recall_audit_file)
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "scope_hashes": [
                hashlib.sha256(scope.key.encode("utf-8")).hexdigest()
                for scope in query.allowed_scopes
            ],
            "hits": [
                {
                    "id": hit.record.id,
                    "score": hit.score,
                    "reason_codes": [self._recall_reason_code(reason) for reason in hit.reasons],
                }
                for hit in result.hits
            ],
            "candidates": result.candidates,
            "filtered": result.filtered,
            "excluded_by_budget": result.excluded_by_budget,
            "tokens_used": result.tokens_used,
            "degraded": result.degraded,
            "error_code": result.error_code,
        }
        ensure_dir(self.recall_audit_file.parent)
        self._assert_path_in_workspace(self.recall_audit_lock_file)
        lock_timeout = self.structured_config.lock_timeout_s if self.structured_config else 5.0
        with FileLock(str(self.recall_audit_lock_file), timeout=lock_timeout):
            existing: list[str] = []
            with suppress(FileNotFoundError, OSError):
                existing = self.recall_audit_file.read_text(encoding="utf-8").splitlines()
            lines = [
                *existing,
                json.dumps(record, ensure_ascii=False, separators=(",", ":")),
            ][-1000:]
            temp_path = self.recall_audit_file.with_suffix(".jsonl.tmp")
            self._assert_path_in_workspace(temp_path)
            try:
                with temp_path.open("w", encoding="utf-8", newline="\n") as stream:
                    stream.write("\n".join(lines) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_path, self.recall_audit_file)
            finally:
                with suppress(FileNotFoundError):
                    temp_path.unlink()

    @staticmethod
    def _recall_reason_code(reason: str) -> str:
        """Reduce a rendered Why reason to a non-content-bearing category."""
        return reason.split("=", 1)[0].split("(", 1)[0]

    # ------------------------------------------------------------------
    # Legacy migration (C2 §14): deterministic, idempotent, no-LLM.
    # ------------------------------------------------------------------

    def migration_plan(self) -> tuple[list[Any], list[Any]]:
        """Scan legacy sources (read-only). Returns (items, issues)."""
        from miniunicorn.agent.memory_migration import scan_legacy_memory

        return scan_legacy_memory(self.workspace)

    def run_migration(self, *, dry_run: bool = False) -> Any:
        """Run the legacy -> structured migration (dry-run or apply).

        dry-run performs zero writes; apply imports through the journal-backed
        lifecycle and writes ``memory/structured/migration-v1.json`` progress, setting
        ``completed_at`` after the full source scan finished.
        """
        from miniunicorn.agent.memory_migration import MemoryMigration

        self._structured_stack_or_build()
        migration = MemoryMigration(
            self.workspace,
            self.structured_repository,
            self.structured_lifecycle,
            self.project_scope_key,
        )
        return migration.dry_run() if dry_run else migration.apply()

    def migration_completed(self) -> bool:
        """True when migration-v1.json records a completed_at (spec §14.3)."""
        from miniunicorn.agent.memory_migration import MIGRATION_STATE_FILE, MigrationState

        return MigrationState.load(self.workspace / MIGRATION_STATE_FILE).completed_at is not None

    # -- Single-Writer 路径校验（借鉴 MiMo Code 的 path whitelist）----------
    # 防御 path traversal：所有写入方法的路径必须解析后仍在 workspace 内。
    # 这层校验是 defense-in-depth —— MemoryStore 的写入路径在 __init__ 固化，
    # 但 workspace 本身可能被配置成符号链接或包含 ../ 等危险路径。
    # _assert_path_in_workspace 在每次写入前做 resolve() + 边界检查。

    def _assert_path_in_workspace(self, path: Path) -> None:
        """断言 *path* 解析后仍在 workspace 内，防御 path traversal / symlink 攻击。

        借鉴 MiMo Code 的 single-writer path whitelist：所有持久化写入必须
        落在 workspace 边界内，避免恶意/误操作写到 workspace 之外的敏感文件。
        """
        try:
            resolved = path.resolve()
            ws = self.workspace.resolve()
            # 检查 resolved 是否等于 ws 或在 ws 之下
            if resolved != ws and ws not in resolved.parents:
                raise PermissionError(
                    f"MemoryStore write blocked: path {path} resolves to {resolved}, "
                    f"outside workspace {ws}"
                )
        except (OSError, RuntimeError) as e:
            # resolve 失败（broken symlink 等）也拒绝写入
            raise PermissionError(f"MemoryStore write blocked: cannot resolve {path}: {e}") from e

    @classmethod
    def _assert_writer_allowed(cls, role: str, relative_path: str) -> None:
        """断言 *role* 角色被允许写入 *relative_path*（single-writer invariant）。

        借鉴 MiMo Code：每个文件只有一个允许的 writer 角色。这里只做
        文档化校验（不抛异常），用于审计和未来在工具层强制执行。
        若 *relative_path* 不在白名单中，则视为 unrestricted（兼容新文件）。
        若 *role* 不在白名单中，记录 warning 但不阻塞（避免破坏现有流程）。
        """
        allowed_roles = cls._WRITER_WHITELIST.get(relative_path)
        if allowed_roles is None:
            # 不在白名单中的文件视为 unrestricted
            return
        if role not in allowed_roles:
            logger.warning(
                "Single-Writer invariant violation: role '{}' is not in allowed writers {} "
                "for path '{}' (allowed: {})",
                role,
                allowed_roles,
                relative_path,
                allowed_roles,
            )

    # -- layered memory: episodic / procedural (P1-1) -----------------------

    def append_episodic(self, session_key: str, content: str) -> int | None:
        """Append an episodic memory entry (event with timestamp/session).

        Returns the 1-based line number, or None on failure.
        """
        if not content:
            return None
        self._assert_path_in_workspace(self._episodic_file)
        self._assert_writer_allowed("dream", "memory/episodic.jsonl")
        try:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M")
            entry = {
                "timestamp": ts,
                "session_key": session_key,
                "content": content,
            }
            with open(self._episodic_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return self._count_lines(self._episodic_file)
        except Exception:
            logger.exception("append_episodic failed")
            return None

    def read_episodic(self, since_timestamp: str | None = None) -> list[dict[str, Any]]:
        """Read episodic entries newer than *since_timestamp*."""
        return self._read_jsonl(self._episodic_file, since_timestamp)

    def append_procedural(self, lesson: str, source_reflection: str | None = None) -> int:
        """Append a procedural lesson (from reflections). Returns cursor.

        Each entry is a JSONL line with ``cursor``, ``timestamp``, ``content``,
        and ``source`` keys — mirroring history.jsonl's on-disk format so the
        same read/dedup tooling applies.
        """
        self._assert_path_in_workspace(self.procedural_file)
        self._assert_writer_allowed("dream", "memory/procedural.jsonl")
        cursor = self._next_procedural_cursor()
        entry = {
            "cursor": cursor,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "content": lesson,
            "source": source_reflection,
        }
        try:
            with open(self.procedural_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return cursor
        except Exception:
            logger.exception("append_procedural failed")
            return cursor

    def _next_procedural_cursor(self) -> int:
        """Get next cursor for procedural file (1-based, monotonic)."""
        try:
            text = self.procedural_file.read_text(encoding="utf-8")
            lines = [line for line in text.strip().split("\n") if line.strip()]
            if not lines:
                return 1
            last = json.loads(lines[-1])
            return last.get("cursor", 0) + 1
        # 移除冗余的 Exception，明确列出可能的异常类型
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            return 1

    def read_procedural(self, limit: int = 100) -> list[dict[str, Any]]:
        """Read procedural lessons, returning the last *limit* entries."""
        if not self.procedural_file.exists():
            return []
        entries: list[dict[str, Any]] = []
        try:
            with open(self.procedural_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except Exception:
            return []
        return entries[-limit:] if limit > 0 else entries

    # -- cross-session shared layer (P2-2) -----------------------------------

    def read_shared_memory(self) -> str:
        """Read global shared semantic memory (cross-session facts)."""
        return self._cached_read(self.shared_memory_file)

    def read_shared_procedural(self, limit: int = 50) -> list[dict[str, Any]]:
        """Read global shared procedural lessons (cross-session experience).

        Same JSONL on-disk format as :meth:`read_procedural`, but stored under
        ``memory/shared/procedural_shared.jsonl`` so lessons that Dream
        promoted as globally applicable stay separate from the per-session
        procedural log.
        """
        if not self.shared_procedural_file.exists():
            return []
        entries: list[dict[str, Any]] = []
        try:
            with open(self.shared_procedural_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except Exception:
            return []
        return entries[-limit:] if limit > 0 else entries

    # -- JSONL 文件截断（防膨胀） -------------------------------------------
    # episodic/procedural/shared_procedural 都是 append-only JSONL，长期使用
    # 会无限增长。这些方法按条数上限截断最旧条目，在 Dream 末尾调用。

    @staticmethod
    def _truncate_jsonl_tail(path: Path, max_entries: int) -> int:
        """截断 JSONL 文件，只保留最后 *max_entries* 行。

        原子写入（tmp + os.replace），失败时返回 0 且不修改文件。
        返回截断的行数。
        """
        if max_entries <= 0 or not path.exists():
            return 0
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception:
            return 0
        if len(lines) <= max_entries:
            return 0
        kept = lines[-max_entries:]
        tmp = path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(kept)
            os.replace(tmp, path)
        except Exception:
            logger.exception("_truncate_jsonl_tail write failed for {}", path)
            return 0
        pruned = len(lines) - len(kept)
        logger.info(
            "Pruned {} old entries from {}, {} remaining",
            pruned,
            path.name,
            len(kept),
        )
        return pruned

    def prune_episodic_if_needed(self) -> int:
        """截断 episodic.jsonl 到 _MAX_EPISODIC_ENTRIES 条。"""
        return self._truncate_jsonl_tail(self._episodic_file, self._MAX_EPISODIC_ENTRIES)

    def prune_procedural_if_needed(self) -> int:
        """截断 procedural.jsonl 到 _MAX_PROCEDURAL_ENTRIES 条。"""
        return self._truncate_jsonl_tail(self.procedural_file, self._MAX_PROCEDURAL_ENTRIES)

    def prune_shared_procedural_if_needed(self) -> int:
        """截断 shared/procedural_shared.jsonl 到 _MAX_SHARED_PROCEDURAL_ENTRIES 条。"""
        return self._truncate_jsonl_tail(
            self.shared_procedural_file, self._MAX_SHARED_PROCEDURAL_ENTRIES
        )

    def run_memory_hygiene(self, now: datetime | None = None) -> dict[str, int]:
        """执行全部文件层清理，返回各部分清理统计。

        在 Dream.run() 末尾调用，也可由 Consolidator 在归档后节流调用。
        包含：
        - reflections.jsonl 截断已处理条目
        - episodic/procedural/shared_procedural 按上限截断
        """
        result: dict[str, int] = {
            "episodic": self.prune_episodic_if_needed(),
            "procedural": self.prune_procedural_if_needed(),
            "reflections": self.prune_reflections_after_cursor(),
            "shared_procedural": self.prune_shared_procedural_if_needed(),
            "structured_expired": 0,
        }
        if self.structured_lifecycle is not None:
            expired = self.structured_lifecycle.expire_due(now or datetime.now(timezone.utc))
            result["structured_expired"] = len(expired)
        return result

    def get_last_reflections_cursor(self) -> int:
        """Get the last processed reflection cursor (line number)."""
        try:
            return int(self._reflections_cursor_file.read_text(encoding="utf-8").strip())
        except (FileNotFoundError, ValueError):
            return 0
        except Exception:
            return 0

    def set_last_reflections_cursor(self, cursor: int) -> None:
        """Set the reflections cursor (line number of last processed entry)."""
        self._assert_path_in_workspace(self._reflections_cursor_file)
        self._assert_writer_allowed("dream", "memory/.reflections_cursor")
        try:
            self._reflections_cursor_file.write_text(str(cursor), encoding="utf-8")
        except Exception:
            logger.exception("set_last_reflections_cursor failed")

    def read_unprocessed_reflections(self, since_cursor: int = 0) -> list[dict[str, Any]]:
        """Read reflections newer than *since_cursor* (for Dream integration).

        Reflections live in ``memory/reflections.jsonl``, each line a JSON
        object with ``timestamp``, ``trigger``, ``iteration``, ``context``,
        ``reflection``, and ``session_key``. The 1-based line number is
        attached as ``_line`` so callers can advance a cursor after processing.
        Returns entries in chronological (file) order.
        """
        rf = self.memory_dir / "reflections.jsonl"
        if not rf.exists():
            return []
        results: list[dict[str, Any]] = []
        try:
            with open(rf, "r", encoding="utf-8") as f:
                for idx, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    entry["_line"] = idx
                    if idx > since_cursor:
                        results.append(entry)
        except Exception:
            return []
        return results

    def prune_reflections_after_cursor(self) -> int:
        """截断已被 Dream 处理的 reflections 条目。

        Dream 成功处理 reflections 后调用。删除 cursor 之前的所有行（已消费），
        并将 cursor 重置为 0。截断后文件只剩未处理条目，行号从 1 重新开始。
        这样 reflections.jsonl 不会无限增长（Reflection 写入 + Dream 消费）。

        返回截断的行数。失败时返回 0 且不修改文件。
        """
        rf = self.memory_dir / "reflections.jsonl"
        cursor = self.get_last_reflections_cursor()
        if cursor <= 0 or not rf.exists():
            return 0
        try:
            with open(rf, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception:
            return 0
        # cursor 是 1-based 行号，保留 cursor 之后（未处理）的行
        kept = lines[cursor:]
        if len(kept) == len(lines):
            # 没有可截断的（cursor 超出文件范围等）
            return 0
        tmp = rf.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(kept)
            os.replace(tmp, rf)
        except Exception:
            logger.exception("prune_reflections_after_cursor write failed")
            return 0
        # 重置 cursor：截断后未处理条目从行 1 开始
        self.set_last_reflections_cursor(0)
        pruned = len(lines) - len(kept)
        logger.info(
            "Pruned {} processed reflection(s), {} remaining",
            pruned,
            len(kept),
        )
        return pruned

    @staticmethod
    def _count_lines(path: Path) -> int:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return sum(1 for _ in f)
        except FileNotFoundError:
            return 0

    @staticmethod
    def _read_jsonl(path: Path, since_timestamp: str | None = None) -> list[dict[str, Any]]:
        """Read a JSONL file, optionally filtering entries newer than *since_timestamp*."""
        if not path.exists():
            return []
        results: list[dict[str, Any]] = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if since_timestamp is not None:
                        ts = entry.get("timestamp", "")
                        if ts <= since_timestamp:
                            continue
                    results.append(entry)
        except Exception:
            logger.exception("_read_jsonl failed for {}", path)
        return results

    # -- generic helpers -----------------------------------------------------

    @staticmethod
    def read_file(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def _cached_read(self, path: Path) -> str:
        """带 mtime+size 校验的缓存读取。

        build_system_prompt 每次 turn 都会读 MEMORY/SOUL/USER.md 等文件，
        这些文件在 turn 内不变（Dream 独占运行时才会改写）。命中缓存时
        避免一次磁盘 IO；未命中或 mtime/size 变化时回源读取。
        """
        try:
            st = path.stat()
        except (FileNotFoundError, OSError):
            # 文件不存在时清掉旧缓存（防止"曾经存在"的残留），返回空。
            self._file_cache.pop(path, None)
            return ""
        key = (st.st_mtime_ns, st.st_size)
        cached = self._file_cache.get(path)
        if cached is not None and (cached[0], cached[1]) == key:
            return cached[2]
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return ""
        self._file_cache[path] = (st.st_mtime_ns, st.st_size, content)
        return content

    def _invalidate_cache(self, path: Path | None = None) -> None:
        """写入后调用，清除单文件或全部缓存。"""
        if path is None:
            self._file_cache.clear()
        else:
            self._file_cache.pop(path, None)

    def _maybe_migrate_legacy_history(self) -> None:
        """One-time upgrade from legacy HISTORY.md to history.jsonl.

        The migration is best-effort and prioritizes preserving as much content
        as possible over perfect parsing.
        """
        if not self.legacy_history_file.exists():
            return
        if self.history_file.exists() and self.history_file.stat().st_size > 0:
            return

        try:
            legacy_text = self.legacy_history_file.read_text(
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            logger.exception("Failed to read legacy HISTORY.md for migration")
            return

        entries = self._parse_legacy_history(legacy_text)
        try:
            if entries:
                self._write_entries(entries)
                last_cursor = entries[-1]["cursor"]
                self._cursor_file.write_text(str(last_cursor), encoding="utf-8")
                # Default to "already processed" so upgrades do not replay the
                # user's entire historical archive into Dream on first start.
                self._dream_cursor_file.write_text(str(last_cursor), encoding="utf-8")

            backup_path = self._next_legacy_backup_path()
            self.legacy_history_file.replace(backup_path)
            logger.info(
                "Migrated legacy HISTORY.md to history.jsonl ({} entries)",
                len(entries),
            )
        except Exception:
            logger.exception("Failed to migrate legacy HISTORY.md")

    def _parse_legacy_history(self, text: str) -> list[dict[str, Any]]:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized:
            return []

        fallback_timestamp = self._legacy_fallback_timestamp()
        entries: list[dict[str, Any]] = []
        chunks = self._split_legacy_history_chunks(normalized)

        for cursor, chunk in enumerate(chunks, start=1):
            timestamp = fallback_timestamp
            content = chunk
            match = self._LEGACY_TIMESTAMP_RE.match(chunk)
            if match:
                timestamp = match.group(1)
                remainder = chunk[match.end() :].lstrip()
                if remainder:
                    content = remainder

            entries.append(
                {
                    "cursor": cursor,
                    "timestamp": timestamp,
                    "content": content,
                }
            )
        return entries

    def _split_legacy_history_chunks(self, text: str) -> list[str]:
        lines = text.split("\n")
        chunks: list[str] = []
        current: list[str] = []
        saw_blank_separator = False

        for line in lines:
            if saw_blank_separator and line.strip() and current:
                chunks.append("\n".join(current).strip())
                current = [line]
                saw_blank_separator = False
                continue
            if self._should_start_new_legacy_chunk(line, current):
                chunks.append("\n".join(current).strip())
                current = [line]
                saw_blank_separator = False
                continue
            current.append(line)
            saw_blank_separator = not line.strip()

        if current:
            chunks.append("\n".join(current).strip())
        return [chunk for chunk in chunks if chunk]

    def _should_start_new_legacy_chunk(self, line: str, current: list[str]) -> bool:
        if not current:
            return False
        if not self._LEGACY_ENTRY_START_RE.match(line):
            return False
        if self._is_raw_legacy_chunk(current) and self._LEGACY_RAW_MESSAGE_RE.match(line):
            return False
        return True

    def _is_raw_legacy_chunk(self, lines: list[str]) -> bool:
        first_nonempty = next((line for line in lines if line.strip()), "")
        match = self._LEGACY_TIMESTAMP_RE.match(first_nonempty)
        if not match:
            return False
        return first_nonempty[match.end() :].lstrip().startswith("[RAW]")

    def _legacy_fallback_timestamp(self) -> str:
        try:
            return datetime.fromtimestamp(
                self.legacy_history_file.stat().st_mtime,
            ).strftime("%Y-%m-%d %H:%M")
        except OSError:
            return datetime.now().strftime("%Y-%m-%d %H:%M")

    def _next_legacy_backup_path(self) -> Path:
        candidate = self.memory_dir / "HISTORY.md.bak"
        suffix = 2
        while candidate.exists():
            candidate = self.memory_dir / f"HISTORY.md.bak.{suffix}"
            suffix += 1
        return candidate

    # -- MEMORY.md (long-term facts) -----------------------------------------

    def read_memory(self) -> str:
        return self._cached_read(self.memory_file)

    def write_memory(self, content: str) -> None:
        self._assert_path_in_workspace(self.memory_file)
        self._assert_writer_allowed("memory_store", "memory/MEMORY.md")
        self.memory_file.write_text(content, encoding="utf-8")
        self._invalidate_cache(self.memory_file)

    # -- SOUL.md -------------------------------------------------------------

    def read_soul(self) -> str:
        return self._cached_read(self.soul_file)

    def write_soul(self, content: str) -> None:
        self._assert_path_in_workspace(self.soul_file)
        self._assert_writer_allowed("memory_store", "SOUL.md")
        self.soul_file.write_text(content, encoding="utf-8")
        self._invalidate_cache(self.soul_file)

    # -- USER.md -------------------------------------------------------------

    def read_user(self) -> str:
        return self._cached_read(self.user_file)

    def write_user(self, content: str) -> None:
        self._assert_path_in_workspace(self.user_file)
        self._assert_writer_allowed("memory_store", "USER.md")
        self.user_file.write_text(content, encoding="utf-8")
        self._invalidate_cache(self.user_file)

    # -- notes.md (主 Agent scratchpad，借鉴 MiMo Code) ---------------------
    # 主 Agent 对其他结构化文件（MEMORY/SOUL/USER）只有读权限，但 notes.md
    # 是唯一允许的写入通道。主 Agent 用 write_file/edit_file 往这里 append
    # 零散发现，Consolidator 在归档时读取并路由到 summary、然后清空。
    # 这避免了"让正在调 bug 的模型同时维护结构化日志"的双任务冲突。

    def read_notes(self) -> str:
        """读取 notes.md 全部内容（不存在时返回空串）。"""
        return self._cached_read(self.notes_file)

    def append_notes(self, content: str) -> None:
        """追加一行到 notes.md。主 Agent 调用。

        自动加时间戳前缀，便于 Consolidator 路由和审计。
        空内容不写入。
        """
        if not content or not content.strip():
            return
        # Single-Writer 路径校验（防御 path traversal）
        self._assert_path_in_workspace(self.notes_file)
        self._assert_writer_allowed("main_agent", "notes.md")
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        line = f"- [{ts}] {content.rstrip()}\n"
        try:
            with open(self.notes_file, "a", encoding="utf-8") as f:
                f.write(line)
            self._invalidate_cache(self.notes_file)
        except OSError:
            logger.exception("append_notes failed")

    def clear_notes(self) -> str:
        """清空 notes.md 并返回清空前的内容。

        由 Consolidator 在归档后调用：先把 notes 内容路由到 summary，
        再清空文件，释放主 Agent 的 scratchpad 空间。
        """
        content = self.read_notes()
        if not content:
            return ""
        # Single-Writer 路径校验
        self._assert_path_in_workspace(self.notes_file)
        self._assert_writer_allowed("consolidator", "notes.md")
        try:
            self.notes_file.write_text("", encoding="utf-8")
            self._invalidate_cache(self.notes_file)
        except OSError:
            logger.exception("clear_notes failed")
        return content

    # -- context injection (used by context.py) ------------------------------

    def get_memory_context(self) -> str:
        long_term = self.read_memory()
        return f"## Long-term Memory\n{long_term}" if long_term else ""

    # -- history.jsonl — append-only, JSONL format ---------------------------

    def append_history(
        self,
        entry: str,
        *,
        max_chars: int | None = None,
        session_key: str | None = None,
        user_key: str | None = None,
    ) -> int:
        """Append *entry* to history.jsonl and return its auto-incrementing cursor.

        Entries are passed through `strip_think` to drop template-level leaks
        (e.g. unclosed `<think` prefixes, `<channel|>` markers) before being
        persisted. If the cleaned content is empty but the raw entry wasn't,
        the record is persisted with an empty string rather than falling back
        to the raw leak — otherwise `strip_think`'s guarantees would be
        undone by history replay / consolidation downstream.

        A defensive cap (*max_chars*, default ``_HISTORY_ENTRY_HARD_CAP``) is
        applied as a final safety net: individual callers should cap their own
        content more tightly; this default only exists to catch unintentional
        large writes (e.g. an LLM echoing its input back as a "summary").
        """
        limit = max_chars if max_chars is not None else _HISTORY_ENTRY_HARD_CAP
        cursor = self._next_cursor()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        raw = entry.rstrip()
        if len(raw) > limit:
            if not self._oversize_logged:
                self._oversize_logged = True
                logger.warning(
                    "history entry exceeds {} chars ({}); truncating. "
                    "Usually means a caller forgot its own cap; "
                    "further occurrences suppressed.",
                    limit,
                    len(raw),
                )
            raw = truncate_text(raw, limit)
        content = strip_think(raw)
        if raw and not content:
            logger.debug(
                "history entry {} stripped to empty (likely template leak); "
                "persisting empty content to avoid re-polluting context",
                cursor,
            )
        record = {"cursor": cursor, "timestamp": ts, "content": content}
        if session_key:
            record["session_key"] = session_key
        if user_key:
            record["user_key"] = user_key
        # Single-Writer 路径校验
        self._assert_path_in_workspace(self.history_file)
        self._assert_path_in_workspace(self._cursor_file)
        self._assert_writer_allowed("consolidator", "memory/history.jsonl")
        self._assert_writer_allowed("consolidator", "memory/.cursor")
        with open(self.history_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._cursor_file.write_text(str(cursor), encoding="utf-8")
        return cursor

    @staticmethod
    def _valid_cursor(value: Any) -> int | None:
        """Int cursors only — reject bool (``isinstance(True, int)`` is True)."""
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value

    def _iter_valid_entries(self) -> Iterator[tuple[dict[str, Any], int]]:
        """Yield ``(entry, cursor)`` for entries with int cursors; warn once on corruption."""
        poisoned: Any = None
        for entry in self._read_entries():
            raw = entry.get("cursor")
            if raw is None:
                continue
            cursor = self._valid_cursor(raw)
            if cursor is None:
                poisoned = raw
                continue
            yield entry, cursor
        if poisoned is not None and not self._corruption_logged:
            self._corruption_logged = True
            logger.warning(
                "history.jsonl contains a non-int cursor ({!r}); dropping it. "
                "Usually caused by an external writer; further occurrences suppressed.",
                poisoned,
            )

    def _next_cursor(self) -> int:
        """Read the current cursor counter and return the next value."""
        if self._cursor_file.exists():
            with suppress(ValueError, OSError):
                return int(self._cursor_file.read_text(encoding="utf-8").strip()) + 1
        # Fast path: trust the tail when intact.  Otherwise scan the whole
        # file and take ``max`` — that stays correct even if the monotonic
        # invariant was broken by external writes.
        last = self._read_last_entry() or {}
        cursor = self._valid_cursor(last.get("cursor"))
        if cursor is not None:
            return cursor + 1
        return max((c for _, c in self._iter_valid_entries()), default=0) + 1

    def read_unprocessed_history(self, since_cursor: int) -> list[dict[str, Any]]:
        """Return history entries with a valid cursor > *since_cursor*."""
        return [e for e, c in self._iter_valid_entries() if c > since_cursor]

    def compact_history(self) -> None:
        """Drop oldest entries if the file exceeds *max_history_entries*."""
        if self.max_history_entries <= 0:
            return
        entries = self._read_entries()
        if len(entries) <= self.max_history_entries:
            return
        kept = entries[-self.max_history_entries :]
        self._write_entries(kept)

    # -- JSONL helpers -------------------------------------------------------

    def _read_entries(self) -> list[dict[str, Any]]:
        """Read all entries from history.jsonl."""
        entries: list[dict[str, Any]] = []
        with suppress(FileNotFoundError):
            with open(self.history_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue

        return entries

    def _read_last_entry(self) -> dict[str, Any] | None:
        """Read the last entry from the JSONL file efficiently."""
        try:
            with open(self.history_file, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                if size == 0:
                    return None
                read_size = min(size, 4096)
                f.seek(size - read_size)
                data = f.read().decode("utf-8")
                lines = [line for line in data.split("\n") if line.strip()]
                if not lines:
                    return None
                return json.loads(lines[-1])
        except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
            return None

    def _write_entries(self, entries: list[dict[str, Any]]) -> None:
        """Overwrite history.jsonl with the given entries (atomic write)."""
        tmp_path = self.history_file.with_suffix(self.history_file.suffix + ".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                for entry in entries:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.history_file)

            # fsync the directory so the rename is durable.
            # On Windows, opening a directory with O_RDONLY raises
            # PermissionError — skip the dir sync there (NTFS
            # journals metadata synchronously).
            # 扩展 suppress 范围，覆盖目录不存在/已非目录等边缘情况
            with suppress(PermissionError, FileNotFoundError, NotADirectoryError, OSError):
                fd = os.open(str(self.history_file.parent), os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        # 使用 Exception 而非 BaseException，避免吞掉 KeyboardInterrupt 等系统级中断
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    # -- dream cursor --------------------------------------------------------

    def get_last_dream_cursor(self) -> int:
        if self._dream_cursor_file.exists():
            with suppress(ValueError, OSError):
                return int(self._dream_cursor_file.read_text(encoding="utf-8").strip())
        return 0

    def set_last_dream_cursor(self, cursor: int) -> None:
        self._assert_path_in_workspace(self._dream_cursor_file)
        self._assert_writer_allowed("dream", "memory/.dream_cursor")
        self._dream_cursor_file.write_text(str(cursor), encoding="utf-8")

    # -- message formatting utility ------------------------------------------

    @staticmethod
    def _format_messages(messages: list[dict]) -> str:
        lines = []
        for message in messages:
            if not message.get("content"):
                continue
            tools = (
                f" [tools: {', '.join(message['tools_used'])}]" if message.get("tools_used") else ""
            )
            lines.append(
                f"[{message.get('timestamp', '?')[:16]}] {message['role'].upper()}{tools}: {message['content']}"
            )
        return "\n".join(lines)

    def raw_archive(self, messages: list[dict], *, max_chars: int | None = None) -> None:
        """Fallback: dump raw messages to history.jsonl without LLM summarization."""
        limit = max_chars if max_chars is not None else _RAW_ARCHIVE_MAX_CHARS
        formatted = truncate_text(self._format_messages(messages), limit)
        session_key, user_key = self._archive_identity(messages)
        self.append_history(
            f"[RAW] {len(messages)} messages\n{formatted}",
            session_key=session_key,
            user_key=user_key,
        )
        logger.warning("Memory consolidation degraded: raw-archived {} messages", len(messages))

    @staticmethod
    def _archive_identity(messages: list[dict]) -> tuple[str | None, str | None]:
        """Return exact identity only when the archive batch is unambiguous."""
        sessions = {
            str(message["session_key"])
            for message in messages
            if message.get("session_key")
        }
        senders = {
            str(message["sender_id"])
            for message in messages
            if message.get("role") == "user"
            and message.get("sender_id")
            and message.get("sender_id") != "subagent"
        }
        session_key = next(iter(sessions)) if len(sessions) == 1 else None
        sender_id = next(iter(senders)) if len(senders) == 1 else None
        return session_key, f"user:{sender_id}" if sender_id else None


# ---------------------------------------------------------------------------
# Consolidator — lightweight token-budget triggered consolidation
# ---------------------------------------------------------------------------


# Individual history.jsonl writers cap their own payloads tightly; the
# _HISTORY_ENTRY_HARD_CAP at append_history() is a belt-and-suspenders default
# that catches any new caller that forgot to set its own cap.
_RAW_ARCHIVE_MAX_CHARS = 16_000  # fallback dump (LLM failed)
_ARCHIVE_SUMMARY_MAX_CHARS = 8_000  # LLM-produced consolidation summary
_HISTORY_ENTRY_HARD_CAP = 64_000  # emergency cap in append_history


class Consolidator:
    """Lightweight consolidation: summarizes evicted messages into history.jsonl."""

    _MAX_CONSOLIDATION_ROUNDS = 5

    _SAFETY_BUFFER = 1024  # extra headroom for tokenizer estimation drift

    # 每 N 次归档后触发一次 memory hygiene（结构化 JSONL 文件截断）。
    # 这样即使 Dream 关闭，append-only 记忆文件也能定期按上限清理。
    # 20 次 ≈ 每隔数十轮对话清理一次，开销可忽略。
    _HYGIENE_THROTTLE = 20

    # 提前 checkpoint 触发比例（借鉴 MiMo Code 的"提前提取"思想）。
    # 旧逻辑：estimated >= budget（100%）才触发，此时模型能力已因
    # "lost in the middle" 衰减，压缩质量下降。
    # 新行为：estimated >= budget * checkpoint_ratio（默认 70%）即触发，
    # 在模型仍有充足注意力时完成归档。rebuild target 仍由 consolidation_ratio
    # 控制（默认压到 50%），循环逻辑不变。
    _CHECKPOINT_RATIO = 0.7

    def __init__(
        self,
        store: MemoryStore,
        provider: LLMProvider,
        model: str,
        sessions: SessionManager,
        context_window_tokens: int,
        build_messages: Callable[..., list[dict[str, Any]]],
        get_tool_definitions: Callable[[], list[dict[str, Any]]],
        max_completion_tokens: int = 4096,
        consolidation_ratio: float = 0.5,
        checkpoint_ratio: float | None = None,
    ):
        self.store = store
        self.provider = provider
        self.model = model
        self.sessions = sessions
        self.context_window_tokens = context_window_tokens
        self.max_completion_tokens = max_completion_tokens
        self.consolidation_ratio = consolidation_ratio
        # checkpoint_ratio=None 走类默认 _CHECKPOINT_RATIO；显式传入可覆盖。
        # 传 1.0 可恢复旧行为（只在 100% 时触发）。
        self.checkpoint_ratio = (
            checkpoint_ratio if checkpoint_ratio is not None else self._CHECKPOINT_RATIO
        )
        # 校验 ratio 范围，避免无效配置导致压缩/检查点逻辑异常
        assert 0 < consolidation_ratio <= 1.0, "consolidation_ratio 必须在 (0, 1.0]"
        assert 0 < self.checkpoint_ratio <= 1.0, "checkpoint_ratio 必须在 (0, 1.0]"
        self._build_messages = build_messages
        self._get_tool_definitions = get_tool_definitions
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        # 归档计数器：每 _HYGIENE_THROTTLE 次归档触发一次 memory hygiene。
        self._archive_count_since_hygiene = 0

    def set_provider(
        self,
        provider: LLMProvider,
        model: str,
        context_window_tokens: int,
    ) -> None:
        self.provider = provider
        self.model = model
        self.context_window_tokens = context_window_tokens
        self.max_completion_tokens = provider.generation.max_tokens

    def get_lock(self, session_key: str) -> asyncio.Lock:
        """Return the shared consolidation lock for one session."""
        return self._locks.setdefault(session_key, asyncio.Lock())

    def pick_consolidation_boundary(
        self,
        session: Session,
        tokens_to_remove: int,
    ) -> tuple[int, int] | None:
        """Pick a user-turn boundary that removes enough old prompt tokens."""
        start = session.last_consolidated
        if start >= len(session.messages) or tokens_to_remove <= 0:
            return None

        removed_tokens = 0
        last_boundary: tuple[int, int] | None = None
        for idx in range(start, len(session.messages)):
            message = session.messages[idx]
            if idx > start and message.get("role") == "user":
                last_boundary = (idx, removed_tokens)
                if removed_tokens >= tokens_to_remove:
                    return last_boundary
            removed_tokens += estimate_message_tokens(message)

        return last_boundary

    @staticmethod
    def _full_unconsolidated_history(
        session: Session,
        *,
        include_timestamps: bool = False,
    ) -> list[dict[str, Any]]:
        """Return the whole unconsolidated tail for consolidation decisions."""
        unconsolidated_count = len(session.messages) - session.last_consolidated
        if unconsolidated_count <= 0:
            return []
        return session.get_history(
            max_messages=unconsolidated_count,
            include_timestamps=include_timestamps,
        )

    @staticmethod
    def _replay_overflow_boundary(
        session: Session,
        replay_max_messages: int | None,
    ) -> int | None:
        if not replay_max_messages or replay_max_messages <= 0:
            return None
        tail = list(
            enumerate(session.messages[session.last_consolidated :], session.last_consolidated)
        )
        if len(tail) <= replay_max_messages:
            return None

        sliced = tail[-replay_max_messages:]
        for i, (_idx, message) in enumerate(sliced):
            if message.get("role") == "user":
                start = i
                if i > 0 and sliced[i - 1][1].get("_channel_delivery"):
                    start = i - 1
                sliced = sliced[start:]
                break

        legal_start = find_legal_message_start([message for _idx, message in sliced])
        if legal_start:
            sliced = sliced[legal_start:]
        if not sliced:
            return len(session.messages)

        first_visible_idx = sliced[0][0]
        if first_visible_idx <= session.last_consolidated:
            return None
        return first_visible_idx

    async def _consolidate_replay_overflow(
        self,
        session: Session,
        replay_max_messages: int | None,
    ) -> str | None:
        """Archive messages that would be hidden by the replay message window."""
        end_idx = self._replay_overflow_boundary(session, replay_max_messages)
        if end_idx is None:
            return None
        chunk = session.messages[session.last_consolidated : end_idx]
        if not chunk:
            return None
        logger.info(
            "Replay-window consolidation for {}: chunk={} msgs, replay_max={}",
            session.key,
            len(chunk),
            replay_max_messages,
        )
        summary = await self.archive(chunk)
        session.last_consolidated = end_idx
        self.sessions.save(session)
        return summary

    # 逐字切片保留的最近用户消息条数（借鉴 MiMo Code 的 rebuild 注入设计）。
    # Consolidator 生成的 summary 是 LLM 改写后的自由文本，可能偏离用户原意。
    # 保留最近 N 条用户消息原文，在 AutoCompact 注入时与 summary 拼接，
    # 让主 Agent 能直接看到用户最近的真实表述，防止 writer 误读意图。
    _VERBATIM_RECENT_USER_MSGS = 2

    def _extract_verbatim_recent(self, session: Session) -> list[str]:
        """提取最近 N 条用户消息的原文（用于防 summary 改写偏离）。

        从 session 当前消息尾部向前扫描，只取 role=user 的 content，
        跳过空内容和工具注入的 runtime context 块。
        """
        result: list[str] = []
        for msg in reversed(session.messages):
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not content:
                continue
            # content 可能是 str 或 list[block]；统一取文本
            if isinstance(content, list):
                text = " ".join(
                    b.get("text", "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            else:
                text = str(content)
            text = text.strip()
            if not text:
                continue
            # 跳过纯 runtime context 标记的消息（无实际用户输入）
            if text.startswith("[Runtime Context"):
                continue
            result.append(text)
            if len(result) >= self._VERBATIM_RECENT_USER_MSGS:
                break
        # 反转回时间顺序
        return list(reversed(result))

    def _persist_last_summary(self, session: Session, summary: str | None) -> None:
        if summary and summary != "(nothing)":
            session.metadata["_last_summary"] = {
                "text": summary,
                "last_active": session.updated_at.isoformat(),
                # 逐字切片：保留最近几条用户消息原文，防止 summary 改写偏离。
                # AutoCompact 注入时会拼接在 summary 之后，让主 Agent 能
                # 直接看到用户最近的真实表述。
                "verbatim_recent": self._extract_verbatim_recent(session),
            }
            self.sessions.save(session)

    def estimate_session_prompt_tokens(
        self,
        session: Session,
    ) -> tuple[int, str]:
        """Estimate prompt size from the full unconsolidated session tail."""
        history = self._full_unconsolidated_history(session, include_timestamps=True)
        channel, chat_id = session.key.split(":", 1) if ":" in session.key else (None, None)
        # Include archived summary in estimation so the budget accounts for it.
        meta = session.metadata.get("_last_summary")
        summary = (
            meta.get("text")
            if isinstance(meta, dict)
            else (meta if isinstance(meta, str) else None)
        )
        probe_messages = self._build_messages(
            history=history,
            current_message="[token-probe]",
            channel=channel,
            chat_id=chat_id,
            sender_id=None,
            session_summary=summary,
            session_metadata=session.metadata,
        )
        return estimate_prompt_tokens_chain(
            self.provider,
            self.model,
            probe_messages,
            self._get_tool_definitions(),
        )

    @property
    def _input_token_budget(self) -> int:
        """Available input token budget for consolidation LLM."""
        return self.context_window_tokens - self.max_completion_tokens - self._SAFETY_BUFFER

    def _truncate_to_token_budget(self, text: str) -> str:
        """Truncate text so it fits within the consolidation LLM's token budget."""
        budget = self._input_token_budget
        if budget <= 0:
            return truncate_text(text, _RAW_ARCHIVE_MAX_CHARS)
        try:
            enc = tiktoken.get_encoding("cl100k_base")
            tokens = enc.encode(text)
            if len(tokens) <= budget:
                return text
            return enc.decode(tokens[:budget]) + "\n... (truncated)"
        except Exception:
            return truncate_text(text, budget * 4)

    async def archive(self, messages: list[dict]) -> str | None:
        """Summarize messages via LLM and append to history.jsonl.

        Returns the summary text on success, None if nothing to archive.

        归档时会一并读取 notes.md（主 Agent 的 scratchpad 笔记），将其作为
        额外上下文喂给 LLM，让 summary 能整合主 Agent 主动记录的零散发现。
        归档成功后清空 notes.md，释放 scratchpad 空间（借鉴 MiMo Code 的
        notes.md 设计：主 Agent 唯一写入通道，Consolidator 定期路由+清空）。
        """
        if not messages:
            return None
        try:
            formatted = MemoryStore._format_messages(messages)
            # 读取主 Agent 的 scratchpad 笔记，附加到归档输入。
            # 这样 LLM 生成 summary 时能整合主 Agent 主动记录的发现，
            # 而不只是被动压缩对话历史。
            notes_content = self.store.read_notes()
            if notes_content:
                formatted = (
                    f"{formatted}\n\n## Agent Scratchpad Notes (from notes.md)\n{notes_content}"
                )
            formatted = self._truncate_to_token_budget(formatted)
            response = await self.provider.chat_with_retry(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": render_template(
                            "agent/consolidator_archive.md",
                            strip=True,
                        ),
                    },
                    {"role": "user", "content": formatted},
                ],
                tools=None,
                tool_choice=None,
            )
            if response.finish_reason == "error":
                raise RuntimeError(f"LLM returned error: {response.content}")
            summary = response.content or "[no summary]"
            session_key, user_key = self.store._archive_identity(messages)
            self.store.append_history(
                summary,
                max_chars=_ARCHIVE_SUMMARY_MAX_CHARS,
                session_key=session_key,
                user_key=user_key,
            )
            # 归档成功后清空 notes.md：内容已被 LLM 整合进 summary，
            # 释放主 Agent 的 scratchpad 空间供下一轮使用。
            if notes_content:
                self.store.clear_notes()
            # 节流触发 memory hygiene：每 _HYGIENE_THROTTLE 次归档后清理一次
            # 文件截断，避免 Dream 关闭时 append-only 文件无限膨胀。
            self._archive_count_since_hygiene += 1
            if self._archive_count_since_hygiene >= self._HYGIENE_THROTTLE:
                self._archive_count_since_hygiene = 0
                try:
                    pruned = self.store.run_memory_hygiene()
                    if any(v > 0 for v in pruned.values()):
                        logger.debug("Consolidator throttled hygiene: {}", pruned)
                except Exception:
                    logger.debug("Consolidator hygiene failed", exc_info=True)
            return summary
        except Exception:
            logger.warning("Consolidation LLM call failed, raw-dumping to history")
            self.store.raw_archive(messages)
            return None

    @property
    def _checkpoint_threshold(self) -> int:
        """提前 checkpoint 触发阈值。

        低于此值时不触发归档；达到或超过时进入归档循环。
        默认为输入预算的 70%（_CHECKPOINT_RATIO），比旧行为（100%）
        更早介入，避免模型在高利用率下能力衰减时做关键压缩。
        """
        # 用 max(0, ...) 防御预算为负或 ratio 为负时产生负阈值
        return max(0, int(self._input_token_budget * self.checkpoint_ratio))

    async def maybe_consolidate_by_tokens(
        self,
        session: Session,
        *,
        replay_max_messages: int | None = None,
    ) -> None:
        """Loop: archive old messages until prompt fits within safe budget.

        The budget reserves space for completion tokens and a safety buffer
        so the LLM request never exceeds the context window.

        触发阈值由 ``checkpoint_ratio`` 控制（默认 0.7），即当估算 token
        达到输入预算的 70% 时就提前归档，而非等到 100% 满载。这是借鉴
        MiMo Code 的"提前提取"思想：模型在高利用率下能力衰减，不应
        在它压缩能力最差时让它做最关键的压缩。
        """
        if self.context_window_tokens <= 0:
            return

        lock = self.get_lock(session.key)
        async with lock:
            # Refresh session reference: AutoCompact may have replaced it.
            fresh = self.sessions.get_or_create(session.key)
            if fresh is not session:
                session = fresh
            if not session.messages:
                return

            budget = self._input_token_budget
            target = int(budget * self.consolidation_ratio)
            checkpoint_threshold = self._checkpoint_threshold
            last_summary = await self._consolidate_replay_overflow(
                session,
                replay_max_messages,
            )
            try:
                estimated, source = self.estimate_session_prompt_tokens(
                    session,
                )
            except Exception:
                logger.exception("Token estimation failed for {}", session.key)
                estimated, source = 0, "error"
            if estimated <= 0:
                self._persist_last_summary(session, last_summary)
                return
            if estimated < checkpoint_threshold:
                unconsolidated_count = len(session.messages) - session.last_consolidated
                logger.debug(
                    "Token consolidation idle {}: {}/{} (checkpoint@{}%, {}) msgs={}",
                    session.key,
                    estimated,
                    self.context_window_tokens,
                    int(self.checkpoint_ratio * 100),
                    source,
                    unconsolidated_count,
                )
                self._persist_last_summary(session, last_summary)
                return

            for round_num in range(self._MAX_CONSOLIDATION_ROUNDS):
                if estimated <= target:
                    break

                boundary = self.pick_consolidation_boundary(session, max(1, estimated - target))
                if boundary is None:
                    logger.debug(
                        "Token consolidation: no safe boundary for {} (round {})",
                        session.key,
                        round_num,
                    )
                    break

                end_idx = boundary[0]

                chunk = session.messages[session.last_consolidated : end_idx]
                if not chunk:
                    break

                logger.info(
                    "Token consolidation round {} for {}: {}/{} via {}, chunk={} msgs",
                    round_num,
                    session.key,
                    estimated,
                    self.context_window_tokens,
                    source,
                    len(chunk),
                )
                summary = await self.archive(chunk)
                # Advance the cursor either way: on success the chunk was
                # summarized; on failure archive() already raw-archived it as
                # a breadcrumb. Re-archiving the same chunk on the next call
                # would just emit duplicate [RAW] entries.
                if summary:
                    last_summary = summary
                session.last_consolidated = end_idx
                self.sessions.save(session)
                if not summary:
                    # LLM is degraded — stop hammering it this call;
                    # the next invocation can retry a fresh chunk.
                    break

                try:
                    estimated, source = self.estimate_session_prompt_tokens(
                        session,
                    )
                except Exception:
                    logger.exception("Token estimation failed for {}", session.key)
                    estimated, source = 0, "error"
                if estimated <= 0:
                    break

            # Persist the last summary to session metadata so it can be injected
            # into the runtime context on the next prepare_session() call, aligning
            # the summary injection strategy with AutoCompact._archive().
            self._persist_last_summary(session, last_summary)

    async def compact_idle_session(
        self,
        session_key: str,
        max_suffix: int = 8,
    ) -> str | None:
        """Hard-truncate an idle session under the consolidation lock.

        Used by AutoCompact so all session mutation goes through a single
        lock-protected path.  Returns the summary text on success, ``None``
        if the LLM failed (raw_archive fallback), or ``""`` if there was
        nothing to archive.
        """
        lock = self.get_lock(session_key)
        async with lock:
            self.sessions.invalidate(session_key)
            session = self.sessions.get_or_create(session_key)

            tail = list(session.messages[session.last_consolidated :])
            if not tail:
                session.updated_at = datetime.now()
                self.sessions.save(session)
                return ""

            probe = Session(
                key=session.key,
                messages=tail.copy(),
                created_at=session.created_at,
                updated_at=session.updated_at,
                metadata={},
                last_consolidated=0,
            )
            probe.retain_recent_legal_suffix(max_suffix)
            kept = probe.messages
            cut = len(tail) - len(kept)
            archive_msgs = tail[:cut]

            if not archive_msgs and not kept:
                session.updated_at = datetime.now()
                self.sessions.save(session)
                return ""

            last_active = session.updated_at
            summary: str | None = ""
            if archive_msgs:
                summary = await self.archive(archive_msgs)

            if summary and summary != "(nothing)":
                # 在清空 messages 前提取 verbatim_recent（基于当前 session.messages），
                # 这样保留的是归档前的最近用户消息原文。
                session.metadata["_last_summary"] = {
                    "text": summary,
                    "last_active": last_active.isoformat(),
                    "verbatim_recent": self._extract_verbatim_recent(session),
                }

            session.messages = kept
            session.last_consolidated = 0
            session.updated_at = datetime.now()
            self.sessions.save(session)

            if archive_msgs:
                logger.info(
                    "Idle-session compact for {}: archived={}, kept={}, summary={}",
                    session_key,
                    len(archive_msgs),
                    len(kept),
                    bool(summary),
                )

            return summary


# ---------------------------------------------------------------------------
# Dream — heavyweight cron-scheduled memory consolidation
# ---------------------------------------------------------------------------


# Single source of truth for the staleness threshold used in _annotate_with_ages
# *and* in the Phase 1 prompt template (passed as `stale_threshold_days`).
# Keep code and prompt aligned — if you bump this, the LLM's instruction string
# updates automatically.
_STALE_THRESHOLD_DAYS = 14


class Dream:
    """Two-phase memory processor: analyze history.jsonl, then edit files via AgentRunner.

    Phase 1 produces an analysis summary (plain LLM call).
    Phase 2 delegates to AgentRunner with read_file / edit_file tools so the
    LLM can make targeted, incremental edits instead of replacing entire files.
    """

    # Caps on prompt-bound inputs so Dream's LLM calls never exceed the model's
    # context window just because a file (or a legacy large history entry) grew
    # unexpectedly. Each file still appears in full via read_file when the agent
    # needs it in Phase 2 — these caps only bound the Phase 1/2 prompt preview.
    _MEMORY_FILE_MAX_CHARS = 32_000
    _SOUL_FILE_MAX_CHARS = 16_000
    _USER_FILE_MAX_CHARS = 16_000
    _HISTORY_ENTRY_PREVIEW_MAX_CHARS = 4_000

    def __init__(
        self,
        store: MemoryStore,
        provider: LLMProvider,
        model: str,
        max_batch_size: int = 20,
        max_iterations: int = 10,
        max_tool_result_chars: int = 16_000,
        annotate_line_ages: bool = True,
    ):
        self.store = store
        self.provider = provider
        self.model = model
        self.max_batch_size = max_batch_size
        self.max_iterations = max_iterations
        self.max_tool_result_chars = max_tool_result_chars
        # Kill switch for the git-blame-based per-line age annotation in Phase 1.
        # Default True keeps the #3212 behavior; set False to feed MEMORY.md raw
        # (e.g. if a specific LLM reacts poorly to the `← Nd` suffix).
        self.annotate_line_ages = annotate_line_ages
        self._runner = AgentRunner(provider)
        self._tools = self._build_tools()

    def set_provider(self, provider: LLMProvider, model: str) -> None:
        self.provider = provider
        self.model = model
        self._runner.provider = provider

    # -- tool registry -------------------------------------------------------

    def _build_tools(self) -> ToolRegistry:
        """Build a minimal tool registry for the Dream agent."""
        from miniunicorn.agent.skills import BUILTIN_SKILLS_DIR
        from miniunicorn.agent.tools.file_state import FileStates
        from miniunicorn.agent.tools.filesystem import EditFileTool, ReadFileTool, WriteFileTool

        tools = ToolRegistry()
        workspace = self.store.workspace
        # Allow reading builtin skills for reference during skill creation
        extra_read = [BUILTIN_SKILLS_DIR] if BUILTIN_SKILLS_DIR.exists() else None
        # Dream gets its own FileStates so its caches stay isolated from the
        # main loop's sessions (issue #3571).
        file_states = FileStates()
        tools.register(
            ReadFileTool(
                workspace=workspace,
                allowed_dir=workspace,
                extra_allowed_dirs=extra_read,
                file_states=file_states,
            )
        )
        structured = self.store.structured_config
        structured_mode = structured is not None and structured.mode in ("shadow", "governed")
        if not structured_mode:
            # Structured mode removes fact-file editing entirely: facts flow
            # only through lifecycle candidates, never through edit_file.
            tools.register(
                EditFileTool(workspace=workspace, allowed_dir=workspace, file_states=file_states)
            )
        # write_file resolves relative paths from workspace root, but can only
        # write under skills/ so the prompt can safely use skills/<name>/SKILL.md.
        skills_dir = workspace / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)
        tools.register(
            WriteFileTool(workspace=workspace, allowed_dir=skills_dir, file_states=file_states)
        )
        return tools

    # -- skill listing --------------------------------------------------------

    def _list_existing_skills(self) -> list[str]:
        """List existing skills as 'name — description' for dedup context."""
        import re as _re

        from miniunicorn.agent.skills import BUILTIN_SKILLS_DIR

        desc_re = _re.compile(r"^description:\s*(.+)$", _re.MULTILINE | _re.IGNORECASE)
        entries: dict[str, str] = {}
        for base in (self.store.workspace / "skills", BUILTIN_SKILLS_DIR):
            if not base.exists():
                continue
            for d in base.iterdir():
                if not d.is_dir():
                    continue
                skill_md = d / "SKILL.md"
                if not skill_md.exists():
                    continue
                # Prefer workspace skills over builtin (same name)
                if d.name in entries and base == BUILTIN_SKILLS_DIR:
                    continue
                content = skill_md.read_text(encoding="utf-8")[:500]
                m = desc_re.search(content)
                desc = m.group(1).strip() if m else "(no description)"
                entries[d.name] = desc
        return [f"{name} — {desc}" for name, desc in sorted(entries.items())]

    # -- main entry ----------------------------------------------------------

    def _annotate_with_ages(self, content: str) -> str:
        """Append per-line age suffixes to MEMORY.md content.

        Each non-blank line whose age exceeds ``_STALE_THRESHOLD_DAYS`` gets a
        suffix like ``← 30d`` indicating days since last modification.
        Returns the original content unchanged if git is unavailable,
        annotate fails, or the line count doesn't match the age count
        (which can happen with an uncommitted working-tree edit — better to
        skip annotation than to tag the wrong line).
        SOUL.md and USER.md are never annotated.
        """
        file_path = "memory/MEMORY.md"
        try:
            ages = self.store.git.line_ages(file_path)
        except Exception:
            logger.debug("line_ages failed for {}", file_path)
            return content
        if not ages:
            return content

        had_trailing = content.endswith("\n")
        lines = content.splitlines()
        # If HEAD-blob line count disagrees with the working-tree content we
        # received, ages would be assigned to the wrong lines — skip entirely
        # and feed the LLM un-annotated content rather than misleading data.
        if len(lines) != len(ages):
            logger.debug(
                "line_ages length mismatch for {} (lines={}, ages={}); skipping annotation",
                file_path,
                len(lines),
                len(ages),
            )
            return content

        annotated: list[str] = []
        for line, age in zip(lines, ages):
            if not line.strip():
                annotated.append(line)
                continue
            if age.age_days > _STALE_THRESHOLD_DAYS:
                annotated.append(f"{line}  \u2190 {age.age_days}d")
            else:
                annotated.append(line)
        result = "\n".join(annotated)
        if had_trailing:
            result += "\n"
        return result

    async def run(self) -> bool:
        """Process unprocessed history entries. Returns True if work was done."""
        structured = self.store.structured_config
        if structured is not None and structured.mode in ("shadow", "governed"):
            return await self._run_structured_batch()
        from miniunicorn.agent.skills import BUILTIN_SKILLS_DIR

        last_cursor = self.store.get_last_dream_cursor()
        entries = self.store.read_unprocessed_history(since_cursor=last_cursor)

        # Read unprocessed reflections (from P2 Reflection mechanism)
        last_refl_cursor = self.store.get_last_reflections_cursor()
        reflections = self.store.read_unprocessed_reflections(since_cursor=last_refl_cursor)

        if not entries and not reflections:
            return False

        # If only reflections (no new history), still process them
        if not entries and reflections:
            logger.info(
                "Dream: processing {} reflections only (no new history)",
                len(reflections),
            )

        batch = entries[: self.max_batch_size] if entries else []
        if batch:
            logger.info(
                "Dream: processing {} entries (cursor {}→{}), batch={}",
                len(entries),
                last_cursor,
                batch[-1]["cursor"],
                len(batch),
            )

        # Build history text for LLM — cap each entry so a legacy oversized
        # record (e.g. pre-#3412 raw_archive dump) can't blow up the prompt.
        history_text = (
            "\n".join(
                f"[{e['timestamp']}] "
                f"{truncate_text(e['content'], self._HISTORY_ENTRY_PREVIEW_MAX_CHARS)}"
                for e in batch
            )
            if batch
            else "(no new history)"
        )

        # Current file contents + per-line age annotations (MEMORY.md only).
        # Each file is capped in the *prompt preview* only; Phase 2 still sees
        # the full file via the read_file tool.
        current_date = datetime.now().strftime("%Y-%m-%d")
        raw_memory = self.store.read_memory() or "(empty)"
        annotated_memory = (
            self._annotate_with_ages(raw_memory) if self.annotate_line_ages else raw_memory
        )
        current_memory = truncate_text(annotated_memory, self._MEMORY_FILE_MAX_CHARS)
        current_soul = truncate_text(
            self.store.read_soul() or "(empty)",
            self._SOUL_FILE_MAX_CHARS,
        )
        current_user = truncate_text(
            self.store.read_user() or "(empty)",
            self._USER_FILE_MAX_CHARS,
        )

        file_context = (
            f"## Current Date\n{current_date}\n\n"
            f"## Current MEMORY.md ({len(current_memory)} chars)\n{current_memory}\n\n"
            f"## Current SOUL.md ({len(current_soul)} chars)\n{current_soul}\n\n"
            f"## Current USER.md ({len(current_user)} chars)\n{current_user}"
        )

        # Build reflections context for Phase 1 — lessons learned from
        # failures/mistakes that Dream should consolidate into procedural.jsonl.
        reflections_text = ""
        if reflections:
            reflections_text = "\n\n## Recent Reflections (Lessons Learned)\n"
            for r in reflections:
                trigger = r.get("trigger", "unknown")
                reflection = r.get("reflection", "")
                ts = r.get("timestamp", "")
                reflections_text += f"- [{ts}] ({trigger}) {reflection}\n"

            # Show current procedural lessons so the LLM can dedup
            current_procedural = self.store.read_procedural(limit=50)
            if current_procedural:
                reflections_text += "\n## Current Procedural Lessons\n"
                for p in current_procedural:
                    reflections_text += f"- [{p.get('timestamp', '')}] {p.get('content', '')}\n"

        # Phase 1: Analyze (no skills list — dedup is Phase 2's job)
        phase1_prompt = (
            f"## Conversation History\n{history_text}\n\n{file_context}{reflections_text}"
        )

        try:
            phase1_response = await self.provider.chat_with_retry(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": render_template(
                            "agent/dream_phase1.md",
                            strip=True,
                            stale_threshold_days=_STALE_THRESHOLD_DAYS,
                        ),
                    },
                    {"role": "user", "content": phase1_prompt},
                ],
                tools=None,
                tool_choice=None,
            )
            analysis = phase1_response.content or ""
            logger.debug("Dream Phase 1 analysis ({} chars): {}", len(analysis), analysis[:500])
        except Exception:
            logger.exception("Dream Phase 1 failed")
            return False

        # Phase 2: Delegate to AgentRunner with read_file / edit_file
        existing_skills = self._list_existing_skills()
        skills_section = ""
        if existing_skills:
            skills_section = "\n\n## Existing Skills\n" + "\n".join(
                f"- {s}" for s in existing_skills
            )
        phase2_prompt = f"## Analysis Result\n{analysis}\n\n{file_context}{skills_section}"

        tools = self._tools
        skill_creator_path = BUILTIN_SKILLS_DIR / "skill-creator" / "SKILL.md"
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": render_template(
                    "agent/dream_phase2.md",
                    strip=True,
                    skill_creator_path=str(skill_creator_path),
                ),
            },
            {"role": "user", "content": phase2_prompt},
        ]

        try:
            result = await self._runner.run(
                AgentRunSpec(
                    initial_messages=messages,
                    tools=tools,
                    model=self.model,
                    max_iterations=self.max_iterations,
                    max_tool_result_chars=self.max_tool_result_chars,
                    fail_on_tool_error=False,
                )
            )
            logger.debug(
                "Dream Phase 2 complete: stop_reason={}, tool_events={}",
                result.stop_reason,
                len(result.tool_events),
            )
            for ev in result.tool_events or []:
                logger.info(
                    "Dream tool_event: name={}, status={}, detail={}",
                    ev.get("name"),
                    ev.get("status"),
                    ev.get("detail", "")[:200],
                )
        except Exception:
            logger.exception("Dream Phase 2 failed")
            result = None

        # Build changelog from tool events
        changelog: list[str] = []
        if result and result.tool_events:
            for event in result.tool_events:
                if event["status"] == "ok":
                    changelog.append(f"{event['name']}: {event['detail']}")

        # Only advance cursor on successful completion to prevent silent loss
        if result and result.stop_reason == "completed":
            if batch:
                new_cursor = batch[-1]["cursor"]
                self.store.set_last_dream_cursor(new_cursor)
                logger.info(
                    "Dream done: {} change(s), cursor advanced to {}",
                    len(changelog),
                    new_cursor,
                )
            else:
                logger.info(
                    "Dream done: {} change(s) (reflections only)",
                    len(changelog),
                )
            # Advance reflections cursor after successful processing
            if reflections:
                last_line = max(r.get("_line", 0) for r in reflections)
                self.store.set_last_reflections_cursor(last_line)
        else:
            reason = result.stop_reason if result else "exception"
            logger.warning(
                "Dream incomplete ({}): cursor NOT advanced, will retry next cron cycle",
                reason,
            )

        self.store.compact_history()

        # 文件层清理：截断 episodic/procedural/shared_procedural/reflections
        # 中已处理或过旧的条目，避免 append-only 文件无限增长。
        # 放在 compact_history 之后、git commit 之前，这样截断也会被 commit 记录。
        try:
            pruned = self.store.run_memory_hygiene()
            if any(v > 0 for v in pruned.values()):
                logger.info("Dream file hygiene: {}", pruned)
        except Exception:
            logger.debug("File hygiene failed", exc_info=True)

        # Git auto-commit (only when there are actual changes)
        if changelog and self.store.git.is_initialized():
            ts = batch[-1]["timestamp"] if batch else datetime.now().strftime("%Y-%m-%d %H:%M")
            summary = f"dream: {ts}, {len(changelog)} change(s)"
            commit_msg = f"{summary}\n\n{analysis.strip()}"
            sha = self.store.git.auto_commit(commit_msg)
            if sha:
                logger.info("Dream commit: {}", sha)
                # 本次 Dream 产生了新 commit，顺带做 GC 回收 loose objects，
                # 避免 .git/objects 长期累积膨胀。失败不影响 Dream 主流程。
                try:
                    self.store.git.gc()
                except Exception:
                    logger.debug("Dream gc skipped", exc_info=True)

        return True

    # -- structured mode: strict extraction -> lifecycle candidates -------------

    def _structured_summary(self, repository: Any, status: MemoryStatus) -> str:
        lines = []
        for record in repository.current_records(status):
            lines.append(f"- [{record.id}] {record.statement} (tags: {', '.join(record.tags)})")
        return "\n".join(lines)

    async def _run_structured_batch(self) -> bool:
        """Structured-mode Dream: extract proposals, ingest through lifecycle,
        advance cursors only on full success (fail-closed, idempotent retry).

        On any provider/parse/ingest error, both cursors stay put and the next
        cycle retries the same batch; re-ingest is safe because lifecycle
        deduplicates by source_batch + content hash.
        """
        from miniunicorn.agent.memory_extraction import (
            MemoryExtractionError,
            parse_extraction_batch,
        )
        from miniunicorn.agent.memory_lifecycle import IngestContext
        from miniunicorn.agent.memory_models import (
            ActorKind,
            EvidenceKind,
            EvidenceRef,
            MemoryScope,
            MemoryStatus,
            ScopeKind,
        )

        store = self.store
        repository = store.structured_repository
        lifecycle = store.structured_lifecycle
        if repository is None or lifecycle is None:
            logger.warning("memory_dream_batch_failed code=structured_stack_missing")
            return False

        last_cursor = store.get_last_dream_cursor()
        entries = store.read_unprocessed_history(since_cursor=last_cursor)
        last_refl_cursor = store.get_last_reflections_cursor()
        reflections = store.read_unprocessed_reflections(since_cursor=last_refl_cursor)
        if not entries and not reflections:
            return False
        batch = entries[: self.max_batch_size] if entries else []

        evidence_catalog: dict[str, EvidenceRef] = {}
        history_lines: list[str] = []
        for entry in batch:
            ref = f"history:{entry['cursor']}"
            content = entry.get("content", "")
            evidence_catalog[ref] = EvidenceRef(
                kind=EvidenceKind.HISTORY,
                ref=ref,
                excerpt=content,
                observed_at=_parse_datetime_loose(entry.get("timestamp")),
            )
            history_lines.append(
                f"[{entry.get('timestamp', '')}] "
                f"{truncate_text(content, self._HISTORY_ENTRY_PREVIEW_MAX_CHARS)}"
            )
        reflection_lines: list[str] = []
        for entry in reflections:
            line = int(entry.get("_line", 0))
            ref = f"reflection:{line}"
            content = entry.get("lesson") or entry.get("reflection", "")
            evidence_catalog[ref] = EvidenceRef(
                kind=EvidenceKind.REFLECTION,
                ref=ref,
                excerpt=content,
                observed_at=_parse_datetime_loose(entry.get("timestamp")),
            )
            reflection_lines.append(
                f"- [{entry.get('timestamp', '')}] ({entry.get('trigger', 'unknown')}) {content}"
            )

        newline = "\n"
        user_prompt = (
            f"## Conversation History\n"
            f"{newline.join(history_lines) if history_lines else '(no new history)'}\n\n"
            f"## Recent Reflections (Lessons Learned)\n"
            f"{newline.join(reflection_lines) if reflection_lines else '(none)'}\n\n"
            f"## Current Active Facts\n"
            f"{self._structured_summary(repository, MemoryStatus.ACTIVE) or '(none)'}\n\n"
            f"## Current Candidates\n"
            f"{self._structured_summary(repository, MemoryStatus.CANDIDATE) or '(none)'}"
        )
        system_prompt = render_template(
            "agent/dream_phase1.md",
            strip=True,
            stale_threshold_days=_STALE_THRESHOLD_DAYS,
            structured_mode=True,
        )
        try:
            response = await self.provider.chat_with_retry(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                tools=None,
                tool_choice=None,
            )
        except Exception:
            logger.exception("memory_dream_batch_failed code=phase1_provider_error")
            return False

        raw = response.content or ""
        scope_by_hint = {
            ScopeKind.PROJECT: MemoryScope(kind=ScopeKind.PROJECT, key=store.project_scope_key),
            ScopeKind.SHARED: MemoryScope(kind=ScopeKind.SHARED, key="shared:*"),
        }
        session_keys = {str(entry["session_key"]) for entry in batch if entry.get("session_key")}
        user_keys = {str(entry["user_key"]) for entry in batch if entry.get("user_key")}
        if batch and len(session_keys) == 1 and all(entry.get("session_key") for entry in batch):
            session_key = next(iter(session_keys)).split("#", 1)[0]
            scope_by_hint[ScopeKind.SESSION] = MemoryScope(
                kind=ScopeKind.SESSION, key=f"session:{session_key}"
            )
        if batch and len(user_keys) == 1 and all(entry.get("user_key") for entry in batch):
            scope_by_hint[ScopeKind.USER] = MemoryScope(
                kind=ScopeKind.USER, key=next(iter(user_keys))
            )
        try:
            extracted = parse_extraction_batch(
                raw,
                evidence_catalog,
                repository.tag_catalog,
                allowed_scope_hints=set(scope_by_hint),
            )
        except MemoryExtractionError as exc:
            logger.warning("memory_dream_batch_failed code=extraction_error error={}", exc)
            return False

        context = IngestContext(
            actor=ActorKind.DREAM,
            reason="dream batch",
            source_batch=f"dream:{last_cursor}-{last_refl_cursor}",
            scope=scope_by_hint[ScopeKind.PROJECT],
            evidence_catalog=evidence_catalog,
            now=datetime.now(timezone.utc),
        )
        results = []
        try:
            for proposal in sorted(extracted.proposals, key=lambda p: p.proposal_index):
                ctx = dataclasses_replace(context, scope=scope_by_hint[proposal.scope_hint])
                results.append(lifecycle.ingest(proposal, ctx))
        except Exception as exc:
            logger.warning(
                "memory_dream_batch_failed code={} error={}",
                exc.__class__.__name__,
                str(exc),
            )
            return False
        if repository.health.state != "healthy":
            logger.warning("memory_dream_batch_failed code=repository_degraded")
            return False

        if batch:
            store.set_last_dream_cursor(batch[-1]["cursor"])
        if reflections:
            store.set_last_reflections_cursor(max(r.get("_line", 0) for r in reflections))
        for result in results:
            logger.info(
                "memory_dream_candidate id={} status={} reason={}",
                result.candidate_id,
                result.final_status.value,
                result.reason_code,
            )
        try:
            if store.git.is_initialized():
                ts = batch[-1]["timestamp"] if batch else datetime.now().strftime("%Y-%m-%d %H:%M")
                sha = store.git.auto_commit(f"dream structured: {ts}, {len(results)} proposal(s)")
                if sha:
                    logger.info("Dream commit: {}", sha)
        except Exception:
            logger.debug("Dream git commit skipped", exc_info=True)
        store.compact_history()
        try:
            store.run_memory_hygiene()
        except Exception:
            logger.debug("File hygiene failed", exc_info=True)
        return True
