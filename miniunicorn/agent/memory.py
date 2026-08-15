"""Memory system: pure file I/O store, lightweight Consolidator, and Dream processor."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
import weakref
from contextlib import suppress
from dataclasses import replace as dataclasses_replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Iterator, Mapping

import tiktoken
from filelock import FileLock
from filelock import Timeout as FileLockTimeout
from loguru import logger

from miniunicorn.agent.reflection import _atomic_rewrite_lines
from miniunicorn.bus.events import session_key_base
from miniunicorn.session.manager import Session
from miniunicorn.utils.gitstore import GOVERNED_MEMORY_TRACKED_FILES, GitStore
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
    from miniunicorn.agent.memory_models import MemoryStatus, RecallQuery, RecallResult
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


def reflection_evidence_id(entry: Mapping[str, Any]) -> str | None:
    """Return the governed evidence ID, or ``None`` for an invalid entry."""
    raw = str(entry.get("reflection_id") or "")
    if re.fullmatch(r"rfl_[0-9a-f]{32}", raw):
        return raw
    return None


def _dream_source_batch(evidence_refs: Iterable[str]) -> str:
    """Derive the Dream source batch from the actual evidence ref set.

    The same input retried yields the same batch id; a different evidence set
    yields a different id.
    """
    canonical = "\n".join(sorted(set(evidence_refs)))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return f"dream:{digest}"


def count_pending_dream_entries(store: "MemoryStore") -> int:
    """Count cursor-visible history and reflection rows without mutating them."""
    history = store.read_unprocessed_history(since_cursor=store.get_last_dream_cursor())
    reflections = store.read_unprocessed_reflections(
        since_cursor=store.get_last_reflections_cursor()
    )
    return len(history) + len(reflections)


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
    _WRITER_WHITELIST: dict[str, set[str]] = {
        "notes.md": {"main_agent", "consolidator"},
        "SOUL.md": {"memory_store"},
        "memory/history.jsonl": {"consolidator", "memory_store"},
        "memory/.cursor": {"consolidator", "memory_store"},
        "memory/.dream_cursor": {"dream", "memory_store"},
        "memory/.reflections_cursor": {"dream", "memory_store"},
        "memory/reflections.jsonl": {"dream", "reflection", "memory_store"},
        "memory/shared/POLICY.md": {"memory_store"},
        "memory/structured/journal.jsonl": {"memory_store"},
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
        from miniunicorn.config.schema import StructuredMemoryConfig

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
        """Revert a memory commit and immediately rebuild governed indexes."""
        try:
            with FileLock(
                str(self.structured_repository.lock_path),
                timeout=self.structured_config.lock_timeout_s,
            ):
                new_sha = self._git.revert(commit)
                if new_sha is None:
                    return None, self.structured_repository.health
                health = self.structured_repository.rebuild()
        except FileLockTimeout:
            logger.warning("memory_restore_failed code=journal_lock_timeout")
            return None, self.structured_repository.health
        from miniunicorn.agent.memory_recall import StructuredMemoryRecall

        self.structured_recall = StructuredMemoryRecall(
            self.structured_repository,
            self.structured_repository.tag_catalog,
        )
        self._file_cache.clear()
        return new_sha, health

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
            with self._bundled_template_path(name).open("r", encoding="utf-8") as source, target.open(
                "w", encoding="utf-8", newline="\n"
            ) as dest:
                dest.write(source.read())

    def _build_structured_stack(self) -> StructuredMemoryRepository:
        """Construct the repository -> lifecycle -> recall stack."""
        from miniunicorn.agent.memory_audit_export import MemoryAuditExporter
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
        self.audit_exporter = MemoryAuditExporter(repository)
        self.structured_lifecycle = StructuredMemoryLifecycle(repository, policy)
        self.structured_recall = StructuredMemoryRecall(repository, repository.tag_catalog)
        return repository

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
            _atomic_rewrite_lines(self._reflections_cursor_file, ["0\n"])
            return 0
        # cursor 是 1-based 行号，保留 cursor 之后（未处理）的行
        kept = lines[cursor:]
        if len(kept) == len(lines):
            # 没有可截断的（cursor 超出文件范围等）
            return 0
        # Reset before renumbering. A failed file rewrite may cause harmless
        # duplicate processing, while the reverse order can permanently skip
        # unconsumed entries under the old physical-line cursor.
        if not _atomic_rewrite_lines(self._reflections_cursor_file, ["0\n"]):
            return 0
        if not _atomic_rewrite_lines(rf, kept):
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
            workspace=self.store.workspace,
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


class Dream:
    """Extract journal-backed memory proposals from history and reflections."""

    _HISTORY_ENTRY_PREVIEW_MAX_CHARS = 4_000
    _REFLECTION_ENTRY_PREVIEW_MAX_CHARS = 1_000
    _MIN_EVIDENCE_PREVIEW_CHARS = 128
    _EVIDENCE_EXCERPT_MAX_CHARS = 1_000
    _SUMMARY_MAX_RECORDS = 40
    _SUMMARY_RECORD_MAX_CHARS = 500
    _SUMMARY_MAX_CHARS = 8_000
    _PROMPT_SAFETY_TOKENS = 1_024

    def __init__(
        self,
        store: MemoryStore,
        provider: LLMProvider,
        model: str,
        max_batch_size: int = 20,
        context_window_tokens: int | None = None,
        max_completion_tokens: int | None = None,
    ):
        self.store = store
        self.provider = provider
        self.model = model
        self.max_batch_size = max_batch_size
        self.context_window_tokens = context_window_tokens
        provider_max_tokens = getattr(getattr(provider, "generation", None), "max_tokens", None)
        self.max_completion_tokens = (
            max_completion_tokens
            if max_completion_tokens is not None
            else provider_max_tokens
            if isinstance(provider_max_tokens, int)
            else 4_096
        )

    def set_provider(
        self,
        provider: LLMProvider,
        model: str,
        context_window_tokens: int | None = None,
        max_completion_tokens: int | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        if context_window_tokens is not None:
            self.context_window_tokens = context_window_tokens
        provider_max_tokens = getattr(getattr(provider, "generation", None), "max_tokens", None)
        resolved_max_tokens = (
            max_completion_tokens
            if max_completion_tokens is not None
            else provider_max_tokens
            if isinstance(provider_max_tokens, int)
            else None
        )
        if resolved_max_tokens is not None:
            self.max_completion_tokens = resolved_max_tokens

    # -- main entry ----------------------------------------------------------

    async def run(self) -> bool:
        """Process unprocessed history entries. Returns True if work was done."""
        return await self._run_structured_batch()

    # -- strict extraction -> lifecycle candidates --------------------------

    @staticmethod
    def _partition_identity(entry: Mapping[str, Any]) -> tuple[str | None, str | None]:
        raw_session = entry.get("session_key")
        session_key = session_key_base(str(raw_session)) if raw_session else None
        raw_user = entry.get("user_key")
        user_key = str(raw_user) if raw_user else None
        return session_key, user_key

    @staticmethod
    def _entry_timestamp(entry: Mapping[str, Any]) -> datetime:
        return _parse_datetime_loose(entry.get("timestamp")) or datetime.max.replace(
            tzinfo=timezone.utc
        )

    def _structured_summary(
        self,
        repository: Any,
        status: MemoryStatus,
        allowed_scopes: set[Any],
    ) -> str:
        lines: list[str] = []
        used = 0
        for record in repository.current_records(status):
            if record.scope not in allowed_scopes or len(lines) >= self._SUMMARY_MAX_RECORDS:
                continue
            line = f"- [{record.id}] {record.statement} (tags: {', '.join(record.tags)})"
            line = line[: self._SUMMARY_RECORD_MAX_CHARS]
            if used + len(line) > self._SUMMARY_MAX_CHARS:
                break
            lines.append(line)
            used += len(line) + 1
        return "\n".join(lines)

    @staticmethod
    def _history_prompt_line(entry: Mapping[str, Any], preview_chars: int) -> str:
        content = str(entry.get("content", ""))[:preview_chars]
        return f"[history:{entry['cursor']} | {entry.get('timestamp', '')}] {content}"

    @staticmethod
    def _reflection_prompt_line(entry: Mapping[str, Any], preview_chars: int) -> str:
        reflection_id = reflection_evidence_id(entry)
        if reflection_id is None:
            raise ValueError("invalid reflection id")
        content = str(entry.get("lesson") or entry.get("reflection", ""))[:preview_chars]
        return (
            f"[reflection:{reflection_id} | {entry.get('timestamp', '')}] "
            f"({entry.get('trigger', 'unknown')}) {content}"
        )

    def _render_user_prompt(
        self,
        repository: Any,
        history: list[dict[str, Any]],
        reflections: list[dict[str, Any]],
        allowed_scopes: set[Any],
        *,
        history_preview: int,
        reflection_preview: int,
        include_summaries: bool,
    ) -> str:
        from miniunicorn.agent.memory_models import MemoryStatus

        history_lines = [self._history_prompt_line(entry, history_preview) for entry in history]
        reflection_lines = [
            self._reflection_prompt_line(entry, reflection_preview) for entry in reflections
        ]
        history_text = "\n".join(history_lines) if history_lines else "(no new history)"
        reflection_text = "\n".join(reflection_lines) if reflection_lines else "(none)"
        active = (
            self._structured_summary(repository, MemoryStatus.ACTIVE, allowed_scopes)
            if include_summaries
            else ""
        )
        candidates = (
            self._structured_summary(repository, MemoryStatus.CANDIDATE, allowed_scopes)
            if include_summaries
            else ""
        )
        return (
            "## Conversation History\n"
            f"{history_text}\n\n"
            "## Recent Reflections (Lessons Learned)\n"
            f"{reflection_text}\n\n"
            "## Current Active Facts\n"
            f"{active or '(none)'}\n\n"
            "## Current Candidates\n"
            f"{candidates or '(none)'}"
        )

    def _bounded_user_prompt(
        self,
        repository: Any,
        history: list[dict[str, Any]],
        reflections: list[dict[str, Any]],
        allowed_scopes: set[Any],
    ) -> str | None:
        def render(history_preview: int, reflection_preview: int, summaries: bool) -> str:
            return self._render_user_prompt(
                repository,
                history,
                reflections,
                allowed_scopes,
                history_preview=history_preview,
                reflection_preview=reflection_preview,
                include_summaries=summaries,
            )

        prompt = render(
            self._HISTORY_ENTRY_PREVIEW_MAX_CHARS,
            self._REFLECTION_ENTRY_PREVIEW_MAX_CHARS,
            True,
        )
        if self.context_window_tokens is None:
            return prompt
        budget = (
            self.context_window_tokens
            - self.max_completion_tokens
            - self._PROMPT_SAFETY_TOKENS
        )
        if budget <= 0:
            return None
        if estimate_message_tokens({"role": "user", "content": prompt}) <= budget:
            return prompt

        prompt = render(
            self._HISTORY_ENTRY_PREVIEW_MAX_CHARS,
            self._REFLECTION_ENTRY_PREVIEW_MAX_CHARS,
            False,
        )
        if estimate_message_tokens({"role": "user", "content": prompt}) <= budget:
            return prompt

        low, high = self._MIN_EVIDENCE_PREVIEW_CHARS, self._HISTORY_ENTRY_PREVIEW_MAX_CHARS
        best = render(
            self._MIN_EVIDENCE_PREVIEW_CHARS,
            self._MIN_EVIDENCE_PREVIEW_CHARS,
            False,
        )
        if estimate_message_tokens({"role": "user", "content": best}) > budget:
            return None
        while low <= high:
            mid = (low + high) // 2
            candidate = render(mid, min(mid, self._REFLECTION_ENTRY_PREVIEW_MAX_CHARS), False)
            if estimate_message_tokens({"role": "user", "content": candidate}) <= budget:
                best = candidate
                low = mid + 1
            else:
                high = mid - 1
        if estimate_message_tokens({"role": "user", "content": best}) <= budget:
            return best
        return None

    def _fit_bounded_batch(
        self,
        repository: Any,
        history: list[dict[str, Any]],
        reflections: list[dict[str, Any]],
        allowed_scopes: set[Any],
        *,
        primary_source: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str] | None:
        """Return the largest selected prefix whose complete prompt fits."""
        fitted_history = list(history)
        fitted_reflections = list(reflections)
        while fitted_history or fitted_reflections:
            prompt = self._bounded_user_prompt(
                repository,
                fitted_history,
                fitted_reflections,
                allowed_scopes,
            )
            if prompt is not None:
                return fitted_history, fitted_reflections, prompt

            # Selection fills the primary source first and the secondary source
            # second. Removing in reverse preserves the exact selected prefix.
            if primary_source == "history":
                if fitted_reflections:
                    fitted_reflections.pop()
                else:
                    fitted_history.pop()
            elif fitted_history:
                fitted_history.pop()
            else:
                fitted_reflections.pop()
        return None

    async def _run_structured_batch(self) -> bool:
        """Extract proposals and ingest them through the lifecycle,
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
            ScopeKind,
        )

        store = self.store
        repository = store.structured_repository
        lifecycle = store.structured_lifecycle
        if repository is None or lifecycle is None:
            logger.warning("memory_dream_batch_failed code=structured_stack_missing")
            return False

        history_entries = store.read_unprocessed_history(
            since_cursor=store.get_last_dream_cursor()
        )
        reflection_entries = store.read_unprocessed_reflections(
            since_cursor=store.get_last_reflections_cursor()
        )
        if not history_entries and not reflection_entries:
            return False

        first_history = history_entries[0] if history_entries else None
        first_reflection = next(
            (entry for entry in reflection_entries if reflection_evidence_id(entry) is not None),
            None,
        )
        if first_history is None and first_reflection is None:
            store.set_last_reflections_cursor(max(entry.get("_line", 0) for entry in reflection_entries))
            store.run_memory_hygiene()
            store._export_audit_pending()
            return True

        if first_reflection is None or (
            first_history is not None
            and self._entry_timestamp(first_history) <= self._entry_timestamp(first_reflection)
        ):
            primary_source = "history"
            primary_entry = first_history
        else:
            primary_source = "reflection"
            primary_entry = first_reflection
        assert primary_entry is not None
        partition = self._partition_identity(primary_entry)

        selected_history: list[dict[str, Any]] = []
        selected_reflections: list[dict[str, Any]] = []
        reflection_advance_line = 0

        def take_history() -> None:
            for entry in history_entries:
                if len(selected_history) + len(selected_reflections) >= self.max_batch_size:
                    break
                if self._partition_identity(entry) != partition:
                    break
                selected_history.append(entry)

        def take_reflections() -> None:
            nonlocal reflection_advance_line
            for entry in reflection_entries:
                if len(selected_history) + len(selected_reflections) >= self.max_batch_size:
                    break
                if reflection_evidence_id(entry) is None:
                    reflection_advance_line = max(
                        reflection_advance_line, int(entry.get("_line", 0))
                    )
                    logger.warning("memory_reflection_skipped code=invalid_reflection_id")
                    continue
                if self._partition_identity(entry) != partition:
                    break
                selected_reflections.append(entry)
                reflection_advance_line = max(
                    reflection_advance_line, int(entry.get("_line", 0))
                )

        if primary_source == "history":
            take_history()
            take_reflections()
        else:
            take_reflections()
            take_history()

        if not selected_history and not selected_reflections:
            if reflection_advance_line:
                store.set_last_reflections_cursor(reflection_advance_line)
                store.run_memory_hygiene()
                store._export_audit_pending()
                return True
            return False

        scope_by_hint = {
            ScopeKind.PROJECT: MemoryScope(kind=ScopeKind.PROJECT, key=store.project_scope_key),
            ScopeKind.SHARED: MemoryScope(kind=ScopeKind.SHARED, key="shared:*"),
        }
        session_key, user_key = partition
        if session_key is not None:
            scope_by_hint[ScopeKind.SESSION] = MemoryScope(
                kind=ScopeKind.SESSION, key=f"session:{session_key}"
            )
        if user_key is not None:
            scope_by_hint[ScopeKind.USER] = MemoryScope(
                kind=ScopeKind.USER, key=user_key
            )

        fitted = self._fit_bounded_batch(
            repository,
            selected_history,
            selected_reflections,
            set(scope_by_hint.values()),
            primary_source=primary_source,
        )
        if fitted is None:
            logger.warning("memory_dream_batch_deferred code=prompt_budget_too_small")
            return False
        selected_history, selected_reflections, user_prompt = fitted

        # A pre-fit scan may have crossed reflections that were later removed
        # from the batch. Recompute the physical cursor as a strict prefix so
        # no valid, unsent reflection can be pruned or skipped.
        selected_reflection_lines = {
            int(entry.get("_line", 0)) for entry in selected_reflections
        }
        reflection_advance_line = 0
        for entry in reflection_entries:
            line = int(entry.get("_line", 0))
            if reflection_evidence_id(entry) is None:
                reflection_advance_line = max(reflection_advance_line, line)
                continue
            if line not in selected_reflection_lines:
                break
            reflection_advance_line = max(reflection_advance_line, line)

        evidence_catalog: dict[str, EvidenceRef] = {}
        for entry in selected_history:
            ref = f"history:{entry['cursor']}"
            content = str(entry.get("content", ""))
            evidence_catalog[ref] = EvidenceRef(
                kind=EvidenceKind.HISTORY,
                ref=ref,
                excerpt=content[: self._EVIDENCE_EXCERPT_MAX_CHARS],
                observed_at=_parse_datetime_loose(entry.get("timestamp")),
            )
        for entry in selected_reflections:
            reflection_id = reflection_evidence_id(entry)
            assert reflection_id is not None
            content = str(entry.get("lesson") or entry.get("reflection", ""))
            ref = f"reflection:{reflection_id}"
            evidence_catalog[ref] = EvidenceRef(
                kind=EvidenceKind.REFLECTION,
                ref=ref,
                excerpt=content[: self._EVIDENCE_EXCERPT_MAX_CHARS],
                observed_at=_parse_datetime_loose(entry.get("timestamp")),
            )

        system_prompt = render_template(
            "agent/dream_phase1.md",
            strip=True,
            allowed_scope_hints=", ".join(kind.value for kind in scope_by_hint),
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
            source_batch=_dream_source_batch(evidence_catalog.keys()),
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

        if selected_history:
            store.set_last_dream_cursor(selected_history[-1]["cursor"])
        if reflection_advance_line:
            store.set_last_reflections_cursor(reflection_advance_line)
        for result in results:
            logger.info(
                "memory_dream_candidate id={} status={} reason={}",
                result.candidate_id,
                result.final_status.value,
                result.reason_code,
            )
        try:
            if store.git.is_initialized():
                last_entry = (
                    selected_history[-1]
                    if selected_history
                    else selected_reflections[-1]
                )
                ts = last_entry.get("timestamp") or datetime.now().strftime("%Y-%m-%d %H:%M")
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
        store._export_audit_pending()
        return True
