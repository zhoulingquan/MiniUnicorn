"""MemoryStore: pure file-I/O memory store layer (extracted from memory.py)."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from contextlib import suppress
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

from filelock import FileLock
from loguru import logger

from erza.memory.jsonl_import import (
    LegacyJournalImportError,
    migrate_legacy_journal,
)
from erza.utils.gitstore import GOVERNED_MEMORY_TRACKED_FILES, GitStore
from erza.utils.helpers import (
    atomic_rewrite_lines,
    ensure_dir,
    strip_think,
    truncate_text,
)

if TYPE_CHECKING:
    from erza.config.schema import StructuredMemoryConfig
    from erza.memory.models import RecallQuery, RecallResult
    from erza.memory.repository import StructuredMemoryRepository


class MemoryStore:
    """File-backed governed memory, history, reflections, and scratch notes."""

    _DEFAULT_MAX_HISTORY = 1000

    # Single-Writer 路径白名单（借鉴 MiMo Code）：
    # 每个文件只有一个允许的 writer 角色，其他角色不应直接写入。
    # 这是文档化的不变量（invariant），MemoryStore 自身的写入方法已通过
    # __init__ 固定路径天然满足；此处记录用于审计、未来工具层强制校验、
    # 以及防止 Consolidator/Dream/主 Agent 之间职责混淆。
    #
    # 角色说明：
    #   - "main_agent": 主 Agent 通过 EditFileTool 间接写入（唯一允许的文件是 notes.md）
    #   - "consolidator": Consolidator 归档时写入 history.jsonl + 清空 notes.md
    #   - "dream": Dream 提炼结构化候选并推进消费 cursor
    #   - "memory_store": MemoryStore internal maintenance
    #
    # 旧 journal 已不是运行时事实源（唯一事实库是 memory.db，design §1），
    # 因此不再授予任何写角色；audit/ 与 backups/ 是目录项，其子路径通过
    # containment/prefix 规则校验（_assert_writer_allowed）。
    _WRITER_WHITELIST: dict[str, set[str]] = {
        "notes.md": {"main_agent", "consolidator"},
        "SOUL.md": {"memory_store"},
        "memory/history.jsonl": {"consolidator", "memory_store"},
        "memory/.cursor": {"consolidator", "memory_store"},
        "memory/.dream_cursor": {"dream", "memory_store"},
        "memory/.reflections_cursor": {"dream", "memory_store"},
        "memory/reflections.jsonl": {"dream", "reflection", "memory_store"},
        "memory/shared/POLICY.md": {"memory_store"},
        "memory/structured/memory.db": {"memory_store"},
        "memory/structured/memory.db-wal": {"memory_store"},
        "memory/structured/memory.db-shm": {"memory_store"},
        "memory/structured/storage-migration-v2.json": {"memory_store"},
        "memory/structured/audit": {"memory_store"},
        "memory/structured/backups": {"memory_store"},
        "memory/structured/recovery": {"memory_store"},
        "memory/structured/memory-maintenance.lock": {"memory_store"},
        "memory/structured/*.importing-*": {"memory_store"},
        "memory/structured/tags.json": {"memory_store"},
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
        self.recall_audit_file = self.memory_dir / "structured" / "recall-audit.jsonl"
        self.recall_audit_lock_file = self.memory_dir / "structured" / "recall-audit.lock"
        self.history_file = self.memory_dir / "history.jsonl"
        self.soul_file = workspace / "SOUL.md"
        # notes.md: 主 Agent 唯一被允许的持久化写入通道（借鉴 MiMo Code）。
        # 主 Agent 用 write_file/edit_file 往这里 append 零散发现，Consolidator
        # 在每次归档时读取内容路由到 summary、然后清空文件。这样主 Agent
        # 不需要自己维护结构化记忆，但仍能记录跨 turn 的临时笔记。
        self.notes_file = workspace / "notes.md"
        self._cursor_file = self.memory_dir / ".cursor"
        self._dream_cursor_file = self.memory_dir / ".dream_cursor"
        self._reflections_cursor_file = self.memory_dir / ".reflections_cursor"
        self.shared_dir = ensure_dir(workspace / "memory" / "shared")
        self._corruption_logged = False  # rate-limit non-int cursor warning
        self._oversize_logged = False  # rate-limit oversized-entry warning
        # 文件内容缓存：key=Path, value=(st_mtime_ns, st_size, content)。
        # SOUL.md and notes.md may be read repeatedly during prompt construction.
        # 通过 mtime+size 校验避免重复磁盘 IO。写入时调用 _invalidate_cache。
        self._file_cache: dict[Path, tuple[int, int, str]] = {}
        self._git = GitStore(
            workspace,
            tracked_files=list(GOVERNED_MEMORY_TRACKED_FILES),
        )
        # Governed structured memory: always on; journal-backed facts + lifecycle + recall.
        from erza.config.schema import StructuredMemoryConfig

        self.structured_config = structured_config or StructuredMemoryConfig()
        self._ensure_structured_bundled_files()
        self._build_structured_stack()
        logger.info(
            "structured_memory_initialized health={}",
            self.structured_repository.health.state,
        )
        self._export_audit_pending()

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

    def restore_memory_version(self, commit: str):
        """Git journal restore is removed (design section 13).

        Structured facts live only in ``memory.db``; restoring them through
        Git would silently leave post-migration transactions behind while a
        journal-restore looks successful. Use the database backup API instead
        (``/memory-restore``). Kept as a deletion point until the caller
        replacement lands.
        """
        raise NotImplementedError("use memory backup restore")

    # ------------------------------------------------------------------
    # Governed structured memory (C2) — repository/lifecycle/recall wiring
    # ------------------------------------------------------------------

    @staticmethod
    def _bundled_template_path(name: str) -> Path:
        return Path(__file__).resolve().parent.parent / "templates" / "memory" / name

    def _ensure_structured_bundled_files(self) -> None:
        """Install canonical structured templates without overwriting user files."""
        canonical_tags = self.workspace / "memory" / "structured" / "tags.json"
        targets = {
            "TAGS.json": canonical_tags,
            "POLICY.md": self.workspace / "memory" / "shared" / "POLICY.md",
        }
        for name, target in targets.items():
            if target.exists():
                continue
            ensure_dir(target.parent)
            with (
                self._bundled_template_path(name).open("r", encoding="utf-8") as source,
                target.open("w", encoding="utf-8", newline="\n") as dest,
            ):
                dest.write(source.read())

    def _build_structured_stack(self) -> StructuredMemoryRepository:
        """Construct the repository -> lifecycle -> recall stack.

        The startup decision (open the existing database, migrate a legacy
        journal once, or fail closed on a lost migrated database) runs before
        repository construction, so the stack never replays the legacy
        journal at runtime (design section 11).
        """
        from erza.memory.audit_export import MemoryAuditExporter
        from erza.memory.lifecycle import (
            LifecyclePolicy,
            StructuredMemoryLifecycle,
        )
        from erza.memory.recall import StructuredMemoryRecall
        from erza.memory.repository import StructuredMemoryRepository

        self._run_startup_decision()
        repository = StructuredMemoryRepository(
            self.workspace, lock_timeout_s=self.structured_config.lock_timeout_s
        )
        policy = LifecyclePolicy(
            auto_promote_verified=self.structured_config.auto_promote_verified,
            min_repeated_evidence=self.structured_config.min_repeated_evidence,
            candidate_ttl_days=self.structured_config.candidate_ttl_days,
        )
        self.structured_repository = repository
        self.audit_exporter = MemoryAuditExporter(repository)
        self.structured_lifecycle = StructuredMemoryLifecycle(repository, policy)
        self.structured_recall = StructuredMemoryRecall(repository, repository.tag_catalog)
        return repository

    def _run_startup_decision(self) -> None:
        """Bring the SQLite fact database up before repository construction.

        Startup matrix (design section 11): an existing database is opened
        directly and the legacy journal is never read; a missing database is
        migrated from a non-empty legacy journal exactly once; a completed
        migration manifest without a database is left untouched so the
        repository fails closed with ``migration_database_lost`` instead of
        re-importing a journal that lacks post-migration facts.
        """
        structured_dir = self.workspace / "memory" / "structured"
        database_path = structured_dir / "memory.db"
        if database_path.exists():
            return
        if self._migration_manifest_completed(structured_dir):
            return
        try:
            migrate_legacy_journal(self.workspace, self.structured_config.lock_timeout_s)
        except (LegacyJournalImportError, OSError):
            # Fail closed: leave the failed migration to the repository, which
            # degrades health instead of raising through startup or creating
            # an empty database over the untouched journal.
            logger.exception("memory_storage_migration_failed")

    @staticmethod
    def _migration_manifest_completed(structured_dir: Path) -> bool:
        """True when the migration manifest records a completed migration."""
        manifest_path = structured_dir / "storage-migration-v2.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return manifest.get("status") == "completed"

    def _export_audit_pending(self) -> None:
        """Best-effort export of pending audit rows after committed facts.

        Fired on a successful Dream batch, on explicit memory modification
        commands before they return, and at startup when a lag is discovered
        (design section 12). Export failures never turn a committed memory
        command into a failure: they only raise ``audit_lag`` (visible via
        ``/memory-status``) and log ``memory_audit_export_failed``, because
        the JSONL audit is a rebuildable derivation of the fact database.
        """
        if self.structured_repository.health.state != "healthy":
            return
        try:
            result = self.audit_exporter.export_pending()
            if result.exported_rows:
                logger.info(
                    "memory_audit_export_completed rows={} sealed_segments={} lag={}",
                    result.exported_rows,
                    result.sealed_segments,
                    result.lag,
                )
        except Exception:
            logger.exception("memory_audit_export_failed")

    def _structured_stack_or_build(self) -> StructuredMemoryRepository:
        """Return the always-initialized structured repository."""
        return self.structured_repository

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
        lock_timeout = self.structured_config.lock_timeout_s
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
        目录项（如 ``memory/structured/audit``）按 containment/prefix 规则
        覆盖其全部子路径：*relative_path* 以目录项 + ``/`` 开头时，按该
        目录项的 allowed roles 校验，从而拒绝主 Agent 写 audit/ 下任何文件。
        若 *relative_path* 不在白名单中，则视为 unrestricted（兼容新文件）。
        若 *role* 不在白名单中，记录 warning 但不阻塞（避免破坏现有流程）。
        """
        allowed_roles = cls._WRITER_WHITELIST.get(relative_path)
        if allowed_roles is None:
            for key, roles in cls._WRITER_WHITELIST.items():
                if relative_path.startswith(f"{key}/") or fnmatchcase(relative_path, key):
                    allowed_roles = roles
                    break
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

    def run_memory_hygiene(self, now: datetime | None = None) -> dict[str, int]:
        """Prune consumed reflections and expire due structured records."""
        result: dict[str, int] = {
            "reflections": self.prune_reflections_after_cursor(),
            "structured_expired": 0,
        }
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
                lines = f.readlines()
                # Rotation/pruning can only safely retain a physical cursor
                # within the current file. A larger cursor is stale, so replay
                # all current lines rather than permanently hiding them.
                effective_cursor = since_cursor if since_cursor <= len(lines) else 0
                for idx, line in enumerate(lines, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    entry["_line"] = idx
                    if idx > effective_cursor:
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
        if cursor > len(lines):
            # The physical-line cursor is stale; no current line is proven
            # consumed. Reset and retain all entries for safe reprocessing.
            atomic_rewrite_lines(self._reflections_cursor_file, ["0\n"])
            return 0
        # cursor 是 1-based 行号，保留 cursor 之后（未处理）的行
        kept = lines[cursor:]
        if len(kept) == len(lines):
            # 没有可截断的（cursor 超出文件范围等）
            return 0
        # Reset before renumbering. A failed file rewrite may cause harmless
        # duplicate processing, while the reverse order can permanently skip
        # unconsumed entries under the old physical-line cursor.
        if not atomic_rewrite_lines(self._reflections_cursor_file, ["0\n"]):
            return 0
        if not atomic_rewrite_lines(rf, kept):
            return 0
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

        Prompt construction may read SOUL.md and notes.md repeatedly. These
        files remain stable within a turn. A cache hit avoids disk I/O; an
        mtime or size change reloads the file.
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

    # -- SOUL.md -------------------------------------------------------------

    def read_soul(self) -> str:
        return self._cached_read(self.soul_file)

    def write_soul(self, content: str) -> None:
        self._assert_path_in_workspace(self.soul_file)
        self._assert_writer_allowed("memory_store", "SOUL.md")
        self.soul_file.write_text(content, encoding="utf-8")
        self._invalidate_cache(self.soul_file)

    # -- notes.md (主 Agent scratchpad，借鉴 MiMo Code) ---------------------
    # notes.md is the main Agent's only direct durable scratch channel.
    # 主 Agent 用 write_file/edit_file 往这里 append
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
        # 原子写：中途崩溃不得损坏/截断游标文件（损坏会被 get_last_dream_cursor
        # suppress 后回退为 0，导致 dream 从头重扫全部历史）。
        if not atomic_rewrite_lines(self._dream_cursor_file, [str(cursor)]):
            # 失败方向是安全的（重复处理而非跳过），与 reflections 游标语义一致，
            # 仅留痕不抛异常。
            logger.warning(
                "Failed to persist dream cursor {} to {} (safe: entries will be reprocessed)",
                cursor,
                self._dream_cursor_file,
            )

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
            str(message["session_key"]) for message in messages if message.get("session_key")
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


class WorkspaceMemoryRegistry:
    """Deterministic per-workspace ``MemoryStore`` cache.

    Resolves an effective workspace path to a single governed ``MemoryStore``
    instance and reuses it across prompts/turns. Creation is guarded by a lock
    so concurrent turns cannot construct duplicate stores for one resolved
    path. The default workspace's store (passed in at construction) is always
    reused for the default resolved path.
    """

    def __init__(
        self,
        default_workspace: Path,
        default_store: MemoryStore,
        *,
        structured_config: StructuredMemoryConfig | None = None,
    ) -> None:
        self._default_root = str(Path(default_workspace).expanduser().resolve())
        self._stores: dict[str, MemoryStore] = {self._default_root: default_store}
        self._lock = threading.Lock()
        self._structured_config = structured_config

    def memory_for(self, workspace: Path | str) -> MemoryStore:
        """Return the governed store for a resolved workspace path."""
        root = str(Path(workspace).expanduser().resolve())
        store = self._stores.get(root)
        if store is not None:
            return store
        with self._lock:
            store = self._stores.get(root)
            if store is None:
                store = MemoryStore(Path(root), structured_config=self._structured_config)
                self._stores[root] = store
        return store

    def known_stores(self) -> list[MemoryStore]:
        """Return the default store plus every lazily-created workspace store."""
        with self._lock:
            stores = list(self._stores.values())
        return stores


# Individual history.jsonl writers cap their own payloads tightly; the
# _HISTORY_ENTRY_HARD_CAP at append_history() is a belt-and-suspenders default
# that catches any new caller that forgot to set its own cap.
_RAW_ARCHIVE_MAX_CHARS = 16_000  # fallback dump (LLM failed)
_HISTORY_ENTRY_HARD_CAP = 64_000  # emergency cap in append_history
