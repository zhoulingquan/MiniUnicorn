"""Session management for conversation history."""

import hashlib
import json
import os
import re
import sys
import threading
import time
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Literal

from loguru import logger

# Platform-specific OS-visible file locking (design §21.2 step 1).
# POSIX uses ``fcntl.flock``; Windows uses ``msvcrt.locking``.
if sys.platform == "win32":  # pragma: no cover - platform branch
    import msvcrt

    _HAS_FCNTL = False
else:  # pragma: no cover - platform branch
    import fcntl

    _HAS_FCNTL = True

from miniunicorn.config.paths import get_legacy_sessions_dir
from miniunicorn.utils.helpers import (
    ensure_dir,
    estimate_message_tokens,
    find_legal_message_start,
    image_placeholder_text,
    safe_filename,
)
from miniunicorn.utils.subagent_channel_display import scrub_subagent_announce_body

FILE_MAX_MESSAGES = 2000
_MESSAGE_TIME_PREFIX_RE = re.compile(r"^\[Message Time: [^\]]+\]\n?")
_LOCAL_IMAGE_BREADCRUMB_RE = re.compile(r"^\[image: (?:/|~)[^\]]+\]\s*$")
_TOOL_CALL_ECHO_RE = re.compile(r"^\s*(?:generate_image|message)\([^)]*\)\s*$")
_SESSION_PREVIEW_MAX_CHARS = 120
_SESSION_LIST_PREVIEW_MAX_RECORDS = 200
_SESSION_LIST_PREVIEW_MAX_CHARS = 1_000_000


def _sanitize_assistant_replay_text(content: str) -> str:
    """Remove internal replay artifacts that the model may have copied before.

    These strings are useful as runtime/session metadata, but when they appear
    in assistant examples they become demonstrations for the model to repeat.
    """
    content = _MESSAGE_TIME_PREFIX_RE.sub("", content, count=1)
    lines = [
        line
        for line in content.splitlines()
        if not _LOCAL_IMAGE_BREADCRUMB_RE.match(line) and not _TOOL_CALL_ECHO_RE.match(line)
    ]
    return "\n".join(lines).strip()


def _text_preview(content: Any) -> str:
    """Return compact display text for session lists."""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                value = block.get("text")
                if isinstance(value, str):
                    parts.append(value)
        text = " ".join(parts)
    else:
        return ""
    text = _sanitize_assistant_replay_text(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > _SESSION_PREVIEW_MAX_CHARS:
        text = text[: _SESSION_PREVIEW_MAX_CHARS - 1].rstrip() + "…"
    return text


def _message_preview_text(message: dict[str, Any]) -> str:
    """Session list preview text; subagent inject blobs are shortened for display."""
    content: Any = message.get("content")
    if message.get("injected_event") == "subagent_result" and isinstance(content, str):
        content = scrub_subagent_announce_body(content)
    return _text_preview(content)


@dataclass
class Session:
    """A conversation session."""

    key: str  # channel:chat_id
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    last_consolidated: int = 0  # Number of messages already consolidated to files
    # generation 用于防止删除后 late save 复活已删除的 session 文件。
    # SessionManager 维护每个 key 的当前 generation;delete_session 时递增,
    # save 时校验 session.generation == manager 当前 generation,不匹配则跳过保存。
    generation: int = 0
    # revision 是单调递增的会话版本号,用于跨进程乐观并发控制 (design §17.7, §21.1)。
    # 每次 commit_turn 成功后递增;legacy 文件无此字段时按 0 加载。
    # save() 不会自动递增 revision,只有 commit_turn() 会。
    revision: int = 0
    # Reserved storage metadata (Task 4 Step 3). Holds the bounded commit
    # index that is written atomically with the session file replacement so
    # a crash after the replace still leaves a recoverable commit-id record
    # (design §21.2 crash recovery). Excluded from Agent-visible metadata,
    # WebUI payloads, and caller-provided ``mutation_metadata_updates``.
    _runtime: dict[str, Any] = field(
        default_factory=lambda: {"format_version": 1, "applied_commits": []}
    )

    @staticmethod
    def _annotate_message_time(message: dict[str, Any], content: Any) -> Any:
        """Expose persisted turn timestamps to the model for relative-date reasoning.

        Annotating *every* assistant turn trains the model (via in-context
        demonstrations) to start its own replies with the same
        ``[Message Time: ...]`` prefix, which leaks metadata back to the user.
        We therefore only annotate user turns. User-side stamps are enough to
        pin adjacent assistant replies for relative-time reasoning, including
        proactive messages the user replies to later.
        """
        timestamp = message.get("timestamp")
        if not timestamp or not isinstance(content, str):
            return content
        role = message.get("role")
        if role != "user":
            return content
        return f"[Message Time: {timestamp}]\n{content}"

    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        """Add a message to the session."""
        msg = {"role": role, "content": content, "timestamp": datetime.now().isoformat(), **kwargs}
        self.messages.append(msg)
        self.updated_at = datetime.now()

    def get_history(
        self,
        max_messages: int = 120,
        *,
        max_tokens: int = 0,
        include_timestamps: bool = False,
    ) -> list[dict[str, Any]]:
        """Return unconsolidated messages for LLM input.

        History is sliced by message count first (``max_messages``), then by
        token budget from the tail (``max_tokens``) when provided.
        """
        unconsolidated = self.messages[self.last_consolidated :]
        max_messages = max_messages if max_messages > 0 else 120
        sliced = unconsolidated[-max_messages:]

        # Avoid starting mid-turn when possible, except for proactive
        # assistant deliveries that the user may be replying to.
        for i, message in enumerate(sliced):
            if message.get("role") == "user":
                start = i
                if i > 0 and sliced[i - 1].get("_channel_delivery"):
                    start = i - 1
                sliced = sliced[start:]
                break

        # Drop orphan tool results at the front.
        start = find_legal_message_start(sliced)
        if start:
            sliced = sliced[start:]

        out: list[dict[str, Any]] = []
        for message in sliced:
            if message.get("_command"):
                continue
            content = message.get("content", "")
            role = message.get("role")
            if role == "assistant" and isinstance(content, str):
                content = _sanitize_assistant_replay_text(content)
            # Synthesize an ``[image: path]`` breadcrumb from the persisted
            # ``media`` kwarg so LLM replay still sees *something* where the
            # image used to be. Without this, an image-only user turn
            # replays as an empty user message — the assistant's reply then
            # looks like it's responding to nothing.
            media = message.get("media")
            if role == "user" and isinstance(media, list) and media and isinstance(content, str):
                breadcrumbs = "\n".join(
                    image_placeholder_text(p) for p in media if isinstance(p, str) and p
                )
                content = f"{content}\n{breadcrumbs}" if content else breadcrumbs
            cli_apps = message.get("cli_apps")
            if (
                role == "user"
                and isinstance(cli_apps, list)
                and cli_apps
                and isinstance(content, str)
            ):
                cli_lines: list[str] = []
                for item in cli_apps[:8]:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("name") or "").strip().lower()
                    if not name:
                        continue
                    entry = str(item.get("entry_point") or "unknown").strip() or "unknown"
                    cli_lines.append(
                        f"[CLI App Attachment: @{name}; tool=run_cli_app; entry_point={entry}; "
                        f"skill=skills/cli-app-{name}/SKILL.md]"
                    )
                if cli_lines:
                    breadcrumbs = "\n".join(cli_lines)
                    content = f"{content}\n{breadcrumbs}" if content else breadcrumbs
            mcp_presets = message.get("mcp_presets")
            if (
                role == "user"
                and isinstance(mcp_presets, list)
                and mcp_presets
                and isinstance(content, str)
            ):
                mcp_lines: list[str] = []
                for item in mcp_presets[:8]:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("name") or "").strip().lower()
                    if not name:
                        continue
                    transport = str(item.get("transport") or "mcp").strip() or "mcp"
                    mcp_lines.append(
                        f"[MCP Preset Attachment: @{name}; tool_prefix=mcp_{name}_; "
                        f"transport={transport}]"
                    )
                if mcp_lines:
                    breadcrumbs = "\n".join(mcp_lines)
                    content = f"{content}\n{breadcrumbs}" if content else breadcrumbs
            if include_timestamps:
                content = self._annotate_message_time(message, content)
            if role == "assistant" and isinstance(content, str) and not content.strip():
                if not any(
                    key in message for key in ("tool_calls", "reasoning_content", "thinking_blocks")
                ):
                    continue
            entry: dict[str, Any] = {"role": message["role"], "content": content}
            for key in (
                "tool_calls",
                "tool_call_id",
                "name",
                "reasoning_content",
                "thinking_blocks",
            ):
                if key in message:
                    entry[key] = message[key]
            out.append(entry)

        if max_tokens > 0 and out:
            kept: list[dict[str, Any]] = []
            used = 0
            for message in reversed(out):
                tokens = estimate_message_tokens(message)
                if kept and used + tokens > max_tokens:
                    break
                kept.append(message)
                used += tokens
            kept.reverse()

            # Keep history aligned to the first visible user turn.
            first_user = next((i for i, m in enumerate(kept) if m.get("role") == "user"), None)
            if first_user is not None:
                kept = kept[first_user:]
            else:
                # Tight token budgets can otherwise leave assistant-only tails.
                # If a user turn exists in the unsliced output, recover the
                # nearest one even if it slightly exceeds the token budget.
                recovered_user = next(
                    (i for i in range(len(out) - 1, -1, -1) if out[i].get("role") == "user"),
                    None,
                )
                if recovered_user is not None:
                    kept = out[recovered_user:]

            # And keep a legal tool-call boundary at the front.
            start = find_legal_message_start(kept)
            if start:
                kept = kept[start:]
            out = kept
        return out

    def clear(self) -> None:
        """Clear all messages and reset session to initial state."""
        self.messages = []
        self.last_consolidated = 0
        self.updated_at = datetime.now()
        self.metadata.pop("_last_summary", None)

    def retain_recent_legal_suffix(self, max_messages: int) -> None:
        """Keep a legal recent suffix constrained by a hard message cap."""
        if max_messages <= 0:
            self.clear()
            return
        if len(self.messages) <= max_messages:
            return

        retained = list(self.messages[-max_messages:])

        # Prefer starting at a user turn when one exists within the tail.
        first_user = next((i for i, m in enumerate(retained) if m.get("role") == "user"), None)
        if first_user is not None:
            retained = retained[first_user:]
        else:
            # If the tail is assistant/tool-only, anchor to the latest user in
            # the full session and take a capped forward window from there.
            latest_user = next(
                (
                    i
                    for i in range(len(self.messages) - 1, -1, -1)
                    if self.messages[i].get("role") == "user"
                ),
                None,
            )
            if latest_user is not None:
                retained = list(self.messages[latest_user : latest_user + max_messages])

        # Mirror get_history(): avoid persisting orphan tool results at the front.
        start = find_legal_message_start(retained)
        if start:
            retained = retained[start:]

        # Hard-cap guarantee: never keep more than max_messages.
        if len(retained) > max_messages:
            retained = retained[-max_messages:]
            start = find_legal_message_start(retained)
            if start:
                retained = retained[start:]

        dropped = len(self.messages) - len(retained)
        self.messages = retained
        self.last_consolidated = max(0, self.last_consolidated - dropped)
        self.updated_at = datetime.now()

    def enforce_file_cap(
        self,
        on_archive: Any = None,
        limit: int = FILE_MAX_MESSAGES,
    ) -> None:
        """Bound session message growth by archiving and trimming old prefixes."""
        if limit <= 0 or len(self.messages) <= limit:
            return

        before = list(self.messages)
        before_last_consolidated = self.last_consolidated
        before_count = len(before)
        self.retain_recent_legal_suffix(limit)
        dropped_count = before_count - len(self.messages)
        if dropped_count <= 0:
            return

        dropped = before[:dropped_count]
        already_consolidated = min(before_last_consolidated, dropped_count)
        archive_chunk = dropped[already_consolidated:]
        if archive_chunk and on_archive:
            on_archive(archive_chunk)
        logger.info(
            "Session file cap hit for {}: dropped {}, raw-archived {}, kept {}",
            self.key,
            dropped_count,
            len(archive_chunk),
            len(self.messages),
        )


# ---------------------------------------------------------------------------
# WP2 — Revision-aware commit types (design §21.1)
# ---------------------------------------------------------------------------

# Maximum number of commit-id entries retained in the per-session sidecar
# (design §21.3). FIFO eviction when exceeded.
_COMMIT_INDEX_MAX = 200


@dataclass(slots=True, frozen=True)
class SessionSnapshot:
    """Read-only snapshot returned by :meth:`SessionManager.load_fresh`.

    Design §21.1. ``applied_commit_ids`` maps ``commit_id -> content_hash``
    for recent commits, bounded by ``_COMMIT_INDEX_MAX``.
    """

    session_key: str
    revision: int
    messages: list[dict[str, Any]]
    applied_commit_ids: dict[str, str]


@dataclass(slots=True, frozen=True)
class SessionCommitOutcome:
    """Outcome of :meth:`SessionManager.commit_turn` (design §21.1).

    Outcomes:
    - ``COMMITTED`` — mutation applied, revision incremented;
    - ``ALREADY_COMMITTED`` — same commit_id + content_hash already on disk;
    - ``REVISION_CONFLICT`` — ``base_revision`` does not match disk;
    - ``IO_FAILURE`` — filesystem error or commit_id hash mismatch.
    """

    state: Literal["COMMITTED", "ALREADY_COMMITTED", "REVISION_CONFLICT", "IO_FAILURE"]
    revision: int
    error: str | None = None


# ---------------------------------------------------------------------------
# OS-visible per-session file lock (design §21.2 step 1)
# ---------------------------------------------------------------------------


class _OSFileLock:
    """Cross-process exclusive file lock using OS primitives.

    On POSIX this uses ``fcntl.flock(LOCK_EX)``; on Windows it uses
    ``msvcrt.locking(LK_LOCK)`` on byte 0. The lock file is a sidecar
    ``<session_path>.lock`` created in the sessions directory.

    The lock is held for the lifetime of the context manager. Acquisition
    blocks until the lock is available (or the OS times out on Windows).
    """

    __slots__ = ("_path", "_fd")

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    def __enter__(self) -> "_OSFileLock":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Open read/write, create if missing. Binary mode for msvcrt compat.
        self._fd = os.open(str(self._path), os.O_RDWR | os.O_CREAT, 0o644)
        if _HAS_FCNTL:
            # POSIX: blocking exclusive lock.
            fcntl.flock(self._fd, fcntl.LOCK_EX)
        else:  # pragma: no cover - Windows branch
            # Windows: lock byte 0 with blocking retry. LK_LOCK blocks for
            # ~10 s then raises OSError if still unavailable.
            os.lseek(self._fd, 0, os.SEEK_SET)
            msvcrt.locking(self._fd, msvcrt.LK_LOCK, 1)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._fd is None:
            return
        try:
            if _HAS_FCNTL:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            else:  # pragma: no cover - Windows branch
                os.lseek(self._fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        finally:
            os.close(self._fd)
            self._fd = None


class SessionManager:
    """
    Manages conversation sessions.

    Sessions are stored as JSONL files in the sessions directory.
    """

    # 进程内缓存上限：超出后 evict 最旧会话（磁盘已有持久化，安全）。
    # 通过 MINIUNICORN_SESSION_CACHE_SIZE 环境变量可调整；<=0 表示无上限（向后兼容）。
    _DEFAULT_CACHE_MAX = 50

    def __init__(self, workspace: Path, *, cache_max: int | None = None):
        self.workspace = workspace
        self.sessions_dir = ensure_dir(self.workspace / "sessions")
        self.legacy_sessions_dir = get_legacy_sessions_dir()
        # OrderedDict 以 LRU 方式淘汰：get_or_create/save 时 move_to_end，
        # 超过 cache_max 时 popitem(last=False) 丢弃最旧。
        self._cache: "OrderedDict[str, Session]" = OrderedDict()
        # 保护 _cache (OrderedDict) 的线程锁，确保多线程访问缓存时不会出现
        # 并发修改导致的字典状态错乱。仅保护同步操作，不要在持锁时 await。
        self._cache_lock = threading.Lock()
        if cache_max is None:
            try:
                env_val = int(
                    os.environ.get("MINIUNICORN_SESSION_CACHE_SIZE", str(self._DEFAULT_CACHE_MAX))
                )
            except ValueError:
                env_val = self._DEFAULT_CACHE_MAX
            cache_max = env_val
        self._cache_max = cache_max  # <=0 表示无上限
        # 当会话从缓存中 evict 时触发，供 AgentLoop 同步清理锁/队列/任务。
        # 回调签名：(session_key: str) -> None
        self._on_evict: Callable[[str], None] = None
        # per-session 保存锁，防止并发 save 同一 session 时 tmp 文件互相覆盖。
        # 使用 threading.Lock (而非 asyncio.Lock) 因为 save() 是同步方法。
        # threading.Lock 不支持 weakref，故用普通 dict；锁数量与会话数成正比，
        # 而会话数受磁盘文件数限制，规模可控。
        self._save_locks: dict[str, threading.Lock] = {}
        self._save_locks_guard = threading.Lock()
        # session 删除 tombstone:key -> 删除时的 generation。
        # save() 时校验 session.generation >= tombstone generation,
        # 小于则说明该 Session 对象是删除前的旧引用,跳过保存以防止复活。
        # get_or_create 创建新 Session 时使用 max(当前 generation, tombstone)。
        self._tombstones: dict[str, int] = {}
        # session generation 当前值:key -> generation。每次 delete_session 递增。
        self._generations: dict[str, int] = {}
        # legacy 迁移索引:legacy_stem -> 已认领该 stem 的 original key。
        # 防止两个不同 key(在旧命名规则下碰撞到同一 stem)都认领同一 legacy 文件,
        # 导致数据被错误复制到另一个会话。
        self._legacy_claims: dict[str, str] = {}
        # Fault injection hook used only by tests (Task 4 Step 1). When set,
        # called with a named fault point immediately after the durable
        # session file replace and before any sidecar write or cache
        # refresh. A raised exception simulates a crash between the atomic
        # session file replacement and the now-removed sidecar write.
        # Production default is ``None`` and adds no behavior.
        self._commit_fault_hook: Callable[[str], None] | None = None

    def set_on_evict(self, cb: Callable[[str], None]) -> None:
        """注册 evict 回调，AgentLoop 在此清理 _session_locks/_pending_queues 等。"""
        self._on_evict = cb

    def _touch(self, key: str) -> None:
        """LRU 更新：把 key 移到末尾（最近使用）。

        调用方必须已持有 ``_cache_lock``。
        """
        self._cache.move_to_end(key)

    def _enforce_max(self) -> None:
        """淘汰最旧条目直到满足 cache_max。

        调用方必须已持有 ``_cache_lock``。
        """
        if self._cache_max <= 0:
            return
        while len(self._cache) > self._cache_max:
            evicted_key, _ = self._cache.popitem(last=False)
            if self._on_evict is not None:
                try:
                    self._on_evict(evicted_key)
                except Exception:
                    logger.exception("Session on_evict callback failed for {}", evicted_key)

    @staticmethod
    def safe_key(key: str) -> str:
        """Public helper — returns the v2 filename stem for *key*.

        v2 格式:``<sanitized-prefix>--<sha256(key) 前 16 位>``
        - 前缀仅用于可读性(sanitized key,截断到 32 字符)。
        - sha256 哈希负责唯一性:不同 key 即使 sanitized 后相同,哈希也不同,
          彻底消除 ``websocket:a:b`` 与 ``websocket:a_b`` 碰撞到同一文件的问题。
        """
        return SessionManager.safe_key_v2(key)

    @staticmethod
    def safe_key_v2(key: str) -> str:
        """v2 filename stem: ``<sanitized-prefix>--<sha256(key)[:16]>``."""
        prefix = safe_filename(key.replace(":", "_"))
        # 截断前缀到 32 字符,避免过长文件名(部分文件系统有 255 字节限制)。
        if len(prefix) > 32:
            prefix = prefix[:32]
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        return f"{prefix}--{digest}"

    @staticmethod
    def safe_key_legacy(key: str) -> str:
        """旧命名规则(仅用于向后兼容查找/迁移)。"""
        return safe_filename(key.replace(":", "_"))

    def _get_session_path(self, key: str) -> Path:
        """Get the v2 file path for a session."""
        return self.sessions_dir / f"{self.safe_key_v2(key)}.jsonl"

    def _get_legacy_session_path(self, key: str) -> Path:
        """Legacy global session path (~/.miniunicorn/sessions/), 旧命名规则。"""
        return self.legacy_sessions_dir / f"{self.safe_key_legacy(key)}.jsonl"

    def _get_workspace_legacy_session_path(self, key: str) -> Path:
        """workspace 内的旧命名规则路径(迁移用)。"""
        return self.sessions_dir / f"{self.safe_key_legacy(key)}.jsonl"

    def get_or_create(self, key: str) -> Session:
        """
        Get an existing session or create a new one.

        Args:
            key: Session key (usually channel:chat_id).

        Returns:
            The session.
        """
        with self._cache_lock:
            if key in self._cache:
                self._touch(key)
                return self._cache[key]

        # _load 涉及磁盘 IO,不持 _cache_lock 避免阻塞其他 cache 访问。
        session = self._load(key)
        if session is None:
            # 新建 session 时使用当前 generation(若已被删除过,使用 tombstone+1)。
            gen = self._generations.get(key, 0)
            tombstone = self._tombstones.get(key)
            if tombstone is not None and tombstone >= gen:
                gen = tombstone + 1
                self._generations[key] = gen
            session = Session(key=key, generation=gen)

        with self._cache_lock:
            # 双重检查:_load 期间另一线程可能已写入同一 key。
            if key in self._cache:
                self._touch(key)
                return self._cache[key]
            self._cache[key] = session
            self._enforce_max()
        return session

    def _load(self, key: str) -> Session | None:
        """Load a session from disk.

        查找顺序:
        1. v2 文件(``<prefix>--<sha256>.jsonl``)。
        2. workspace 内旧命名文件(``<safe_key_legacy>.jsonl``),原子 rename 迁移到 v2。
        3. 全局 legacy 目录(``~/.miniunicorn/sessions/<safe_key_legacy>.jsonl``),
           原子 rename 迁移到 v2。

        迁移歧义处理:旧命名规则下 ``websocket:a:b`` 与 ``websocket:a_b`` 碰撞到同一
        stem。使用 instance 级 ``_legacy_claims`` 索引记录 ``legacy_stem -> original key``。
        若旧 stem 已被另一个 key 认领,保留旧文件,为当前 key 创建独立 v2 文件并记录警告,
        绝不覆盖或复制可能属于其他会话的数据。
        """
        path = self._get_session_path(key)
        if not path.exists():
            # 尝试从 workspace 内旧命名迁移
            ws_legacy = self._get_workspace_legacy_session_path(key)
            if ws_legacy.exists():
                self._migrate_legacy(key, ws_legacy, path)
            else:
                # 尝试从全局 legacy 目录迁移
                legacy_path = self._get_legacy_session_path(key)
                if legacy_path.exists():
                    self._migrate_legacy(key, legacy_path, path)

        if not path.exists():
            return None

        try:
            messages = []
            metadata = {}
            created_at = None
            updated_at = None
            last_consolidated = 0
            revision = 0  # legacy 文件无此字段,按 0 读取 (design §21.1)。
            runtime_meta: dict[str, Any] = {
                "format_version": 1,
                "applied_commits": [],
            }

            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    data = json.loads(line)

                    if data.get("_type") == "metadata":
                        metadata = data.get("metadata", {})
                        created_at = (
                            datetime.fromisoformat(data["created_at"])
                            if data.get("created_at")
                            else None
                        )
                        updated_at = (
                            datetime.fromisoformat(data["updated_at"])
                            if data.get("updated_at")
                            else None
                        )
                        last_consolidated = data.get("last_consolidated", 0)
                        revision = data.get("revision", 0)
                        # Embedded commit index (Task 4 Step 3). Corrupt or
                        # legacy files without ``_runtime`` keep the default
                        # empty index; the sidecar merge in ``load_fresh``
                        # recovers any older entries (Task 4 Step 4).
                        runtime_meta = self._normalize_runtime_meta(data.get("_runtime"))
                    else:
                        messages.append(data)

            # 加载成功后,使用当前 generation(若已被删除过,使用 tombstone+1)。
            gen = self._generations.get(key, 0)
            tombstone = self._tombstones.get(key)
            if tombstone is not None and tombstone >= gen:
                gen = tombstone + 1
                self._generations[key] = gen

            return Session(
                key=key,
                messages=messages,
                created_at=created_at or datetime.now(),
                updated_at=updated_at or datetime.now(),
                metadata=metadata,
                last_consolidated=last_consolidated,
                generation=gen,
                revision=revision,
                _runtime=runtime_meta,
            )
        except Exception as e:
            logger.warning("Failed to load session {}: {}", key, e)
            repaired = self._repair(key)
            if repaired is not None:
                logger.info(
                    "Recovered session {} from corrupt file ({} messages)",
                    key,
                    len(repaired.messages),
                )
            return repaired

    def _migrate_legacy(self, key: str, legacy_path: Path, v2_path: Path) -> None:
        """原子迁移旧命名文件到 v2 路径,处理碰撞歧义。

        - 若旧 stem 未被其他 key 认领:记录 claim,``os.replace`` 原子迁移。
        - 若旧 stem 已被另一个 key 认领:保留旧文件,不迁移,记录警告。
          当前 key 将在 ``_load`` 返回 None 后由 ``get_or_create`` 创建独立 v2 文件。
        """
        legacy_stem = legacy_path.stem
        claimed_by = self._legacy_claims.get(legacy_stem)
        if claimed_by is not None and claimed_by != key:
            logger.warning(
                "Legacy session stem '{}' collision: claimed by '{}', "
                "cannot migrate for '{}'; creating independent v2 file",
                legacy_stem,
                claimed_by,
                key,
            )
            return
        try:
            self._legacy_claims[legacy_stem] = key
            # os.replace 是原子的:目标存在时覆盖,不存在时移动。
            # 但 v2_path 在 _load 调用时已确认不存在,所以这里等价于原子 rename。
            os.replace(str(legacy_path), str(v2_path))
            logger.info("Migrated session {} from legacy path to v2", key)
        except OSError as exc:
            logger.warning("Failed to migrate session {} from legacy to v2: {}", key, exc)

    def _repair(self, key: str) -> Session | None:
        """Attempt to recover a session from a corrupt JSONL file."""
        path = self._get_session_path(key)
        return self._repair_path(path, key)

    def _repair_path(self, path: Path, key: str | None = None) -> Session | None:
        """Attempt to recover a session from a corrupt JSONL file at *path*.

        *key* 用于 Session.key 字段;若为 None,尝试从 metadata 行读取,
        都不可用时用 path.stem 作为最后兜底。
        """
        if not path.exists():
            return None

        try:
            messages: list[dict[str, Any]] = []
            metadata: dict[str, Any] = {}
            created_at: datetime | None = None
            updated_at: datetime | None = None
            last_consolidated = 0
            revision = 0  # legacy 文件无此字段,按 0 读取 (design §21.1)。
            runtime_meta: dict[str, Any] = {
                "format_version": 1,
                "applied_commits": [],
            }
            skipped = 0
            stored_key: str | None = None

            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        skipped += 1
                        continue

                    if data.get("_type") == "metadata":
                        metadata = data.get("metadata", {})
                        if data.get("created_at"):
                            with suppress(ValueError, TypeError):
                                created_at = datetime.fromisoformat(data["created_at"])
                        if data.get("updated_at"):
                            with suppress(ValueError, TypeError):
                                updated_at = datetime.fromisoformat(data["updated_at"])
                        last_consolidated = data.get("last_consolidated", 0)
                        stored_key = data.get("key")
                        revision = data.get("revision", 0)
                        runtime_meta = self._normalize_runtime_meta(data.get("_runtime"))
                    else:
                        messages.append(data)

            resolved_key = key or stored_key or path.stem
            if skipped:
                logger.warning("Skipped {} corrupt lines in session {}", skipped, resolved_key)

            if not messages and not metadata:
                return None

            return Session(
                key=resolved_key,
                messages=messages,
                created_at=created_at or datetime.now(),
                updated_at=updated_at or datetime.now(),
                metadata=metadata,
                last_consolidated=last_consolidated,
                revision=revision,
                _runtime=runtime_meta,
            )
        except Exception as e:
            logger.warning("Repair failed for session {}: {}", resolved_key, e)
            return None

    @staticmethod
    def _session_payload(session: Session) -> dict[str, Any]:
        return {
            "key": session.key,
            "created_at": session.created_at.isoformat(),
            "updated_at": session.updated_at.isoformat(),
            "metadata": session.metadata,
            "messages": session.messages,
        }

    def _tmp_path(self, path: Path) -> Path:
        """使用 session key 哈希生成唯一 tmp 路径,避免并发 save 冲突。

        每个 session 的 tmp 文件路径不同,即使两个线程同时调用 save()
        也不会互相覆盖对方的 tmp 文件 (配合 per-session 锁双重保险)。
        """
        h = hashlib.md5(str(path).encode()).hexdigest()[:8]
        return path.with_suffix(f".{h}.jsonl.tmp")

    def _get_save_lock(self, key: str) -> threading.Lock:
        """获取 (或惰性创建) 指定 session 的 save 锁。

        使用 _save_locks_guard 保护字典本身的多线程并发访问,
        实际保存期间持锁的是返回的 per-session Lock。
        """
        with self._save_locks_guard:
            lock = self._save_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._save_locks[key] = lock
            return lock

    def save(self, session: Session, *, fsync: bool = False) -> None:
        """Save a session to disk atomically.

        When *fsync* is ``True`` the final file and its parent directory are
        explicitly flushed to durable storage.  This is intentionally off by
        default (the OS page-cache is sufficient for normal operation) but
        should be enabled during graceful shutdown so that filesystems with
        write-back caching (e.g. rclone VFS, NFS, FUSE mounts) do not lose
        the most recent writes.

        Note: ``flush_all`` 在优雅关闭时强制 ``fsync=True`` 以确保持久化,
        满足"至少在 flush_all 中强制 fsync=True"的兜底要求;常规 save 保留
        ``fsync=False`` 默认值以避免每次写入都付 fsync 性能开销。

        删除后复活防护:校验 ``session.generation`` 不小于 tombstone generation。
        若 session 是删除前的旧引用(generation < tombstone),跳过保存并记录警告,
        避免 late save 通过 ``os.replace`` 重新创建已删除的文件。
        """
        # 删除后复活防护:旧 Session 引用的 generation 小于 tombstone 时跳过保存。
        tombstone = self._tombstones.get(session.key)
        if tombstone is not None and session.generation < tombstone:
            logger.warning(
                "Skipping save for deleted session {} (generation {} < tombstone {})",
                session.key,
                session.generation,
                tombstone,
            )
            return
        # 使用 per-session 锁串行化同一 session 的并发 save,避免 tmp 文件
        # 互相覆盖导致的数据丢失 (不同 session 之间不互斥,可并行保存)。
        lock = self._get_save_lock(session.key)
        with lock:
            self._save_impl(session, fsync=fsync)

    def _save_impl(self, session: Session, *, fsync: bool) -> None:
        """实际的同步保存逻辑 (调用方需自行加锁)。"""
        path = self._get_session_path(session.key)
        tmp_path = self._tmp_path(path)

        # 清理旧版本残留的 tmp 文件 (向后兼容;旧版使用 .jsonl.tmp 后缀)。
        # 不清理当前 _tmp_path 自身,避免误删正在使用的文件。
        legacy_tmp = path.with_suffix(".jsonl.tmp")
        if legacy_tmp != tmp_path and legacy_tmp.exists():
            with suppress(OSError):
                legacy_tmp.unlink()

        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                metadata_line = {
                    "_type": "metadata",
                    "key": session.key,
                    "created_at": session.created_at.isoformat(),
                    "updated_at": session.updated_at.isoformat(),
                    "metadata": session.metadata,
                    "last_consolidated": session.last_consolidated,
                    # revision 持久化以支持跨进程乐观并发控制 (design §21.1)。
                    # legacy 文件无该字段,_load 按 0 读取。
                    "revision": session.revision,
                    # Reserved storage metadata (Task 4 Step 3): the bounded
                    # commit index embedded in the same atomically-replaced
                    # session file so a crash after the replace still leaves
                    # a recoverable commit-id record.
                    "_runtime": session._runtime,
                }
                f.write(json.dumps(metadata_line, ensure_ascii=False) + "\n")
                for msg in session.messages:
                    f.write(json.dumps(msg, ensure_ascii=False) + "\n")
                if fsync:
                    f.flush()
                    os.fsync(f.fileno())

            os.replace(tmp_path, path)

            if fsync:
                # fsync the directory so the rename is durable.
                # On Windows, opening a directory with O_RDONLY raises
                # PermissionError — skip the dir sync there (NTFS
                # journals metadata synchronously).
                with suppress(PermissionError):
                    fd = os.open(str(path.parent), os.O_RDONLY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

        # 保存即最近使用，更新 LRU 顺序。
        with self._cache_lock:
            if session.key in self._cache:
                self._touch(session.key)
            else:
                self._cache[session.key] = session
                self._enforce_max()

    def flush_all(self) -> int:
        """Re-save every cached session with fsync for durable shutdown.

        Returns the number of sessions flushed.  Errors on individual
        sessions are logged but do not prevent other sessions from being
        flushed.
        """
        # 拍快照避免迭代期间其他线程修改 _cache 导致 RuntimeError。
        with self._cache_lock:
            items = list(self._cache.items())
        flushed = 0
        for key, session in items:
            try:
                self.save(session, fsync=True)
                flushed += 1
            except Exception:
                logger.warning("Failed to flush session {}", key, exc_info=True)
        return flushed

    def invalidate(self, key: str) -> None:
        """Remove a session from the in-memory cache."""
        with self._cache_lock:
            self._cache.pop(key, None)

    # ------------------------------------------------------------------
    # WP2 — Revision-aware commit API (design §21.1, §21.2)
    # ------------------------------------------------------------------

    def _get_lock_path(self, key: str) -> Path:
        """OS-visible per-session lock sidecar path (design §21.2 step 1)."""
        return self._get_session_path(key).with_suffix(".jsonl.lock")

    def _get_commits_path(self, key: str) -> Path:
        """Per-session commit-id index sidecar path (design §21.3)."""
        return self._get_session_path(key).with_suffix(".jsonl.commits")

    @staticmethod
    def _normalize_runtime_meta(raw: Any) -> dict[str, Any]:
        """Return a valid ``_runtime`` dict, ignoring corrupt fields (Task 4 Step 6).

        A corrupt ``_runtime`` (non-dict, missing ``applied_commits``, or
        entries with non-string ``commit_id``) must never lose session
        messages — the bad metadata is replaced with the empty default
        and the sidecar merge in :meth:`load_fresh` recovers valid
        entries when present.
        """
        default: dict[str, Any] = {
            "format_version": 1,
            "applied_commits": [],
        }
        if not isinstance(raw, dict):
            return default
        raw_commits = raw.get("applied_commits")
        if not isinstance(raw_commits, list):
            return default
        cleaned: list[dict[str, Any]] = []
        seen: set[str] = set()
        for entry in raw_commits:
            if not isinstance(entry, dict):
                continue
            cid = entry.get("commit_id")
            if not isinstance(cid, str) or cid in seen:
                continue
            seen.add(cid)
            cleaned.append(
                {
                    "commit_id": cid,
                    "content_hash": str(entry.get("content_hash", "")),
                    "revision": int(entry.get("revision", 0) or 0),
                    "commit_kind": str(entry.get("commit_kind", "")),
                    "committed_at_ms": int(entry.get("committed_at_ms", 0) or 0),
                }
            )
        # FIFO bound (newest retained).
        if len(cleaned) > _COMMIT_INDEX_MAX:
            cleaned = cleaned[-_COMMIT_INDEX_MAX:]
        return {"format_version": 1, "applied_commits": cleaned}

    def _read_commit_index(self, key: str) -> "OrderedDict[str, dict[str, Any]]":
        """Read the bounded commit-id index sidecar.

        Returns an ``OrderedDict`` mapping ``commit_id -> entry`` where each
        entry has ``content_hash``, ``revision``, ``commit_kind``, and
        ``committed_at_ms``. Insertion order reflects commit order. Returns
        an empty dict when the sidecar does not exist or is corrupt.
        """
        path = self._get_commits_path(key)
        if not path.exists():
            return OrderedDict()
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            entries = data.get("commits", [])
            result: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
            for entry in entries:
                cid = entry.get("commit_id")
                if isinstance(cid, str):
                    result[cid] = entry
            return result
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            logger.warning("Failed to read commit index for {}: {}", key, exc)
            return OrderedDict()

    def _write_commit_index(self, key: str, index: "OrderedDict[str, dict[str, Any]]") -> None:
        """Atomically write the commit-id index sidecar (design §21.3).

        The sidecar is bounded to ``_COMMIT_INDEX_MAX`` entries (FIFO
        eviction). Written with ``fsync`` + atomic ``os.replace``.
        """
        path = self._get_commits_path(key)
        tmp_path = self._tmp_path(path)
        # Bound the index (FIFO eviction of oldest entries).
        while len(index) > _COMMIT_INDEX_MAX:
            index.popitem(last=False)
        entries = list(index.values())
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump({"commits": entries}, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
            # fsync parent directory when supported (design §21.2 step 11).
            with suppress(PermissionError):
                fd = os.open(str(path.parent), os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    def load_fresh(self, session_key: str) -> SessionSnapshot:
        """Load session from disk, bypassing the process cache (design §21.1).

        Returns a :class:`SessionSnapshot` with the current revision,
        messages, and applied commit ids. If the session does not exist on
        disk, returns a snapshot with ``revision=0`` and empty messages.

        This is the read path used by the Session Committer before applying
        a mutation. It never returns cached data.

        Commit-index merge (Task 4 Step 4):
        1. Prefer the embedded ``_runtime.applied_commits`` written
           atomically with the session file.
        2. Fall back to the legacy ``.jsonl.commits`` sidecar when the
           embedded index is absent or older (migration from pre-Task-4
           files). Valid unique sidecar entries not present in the embedded
           index are merged in memory; they are persisted into the session
           file on the next successful session write.
        3. The sidecar is NOT deleted in this task.
        """
        # Bypass cache: read directly from disk.
        session = self._load(session_key)
        if session is None:
            return SessionSnapshot(
                session_key=session_key,
                revision=0,
                messages=[],
                applied_commit_ids={},
            )
        # Start with the embedded index (authoritative after Task 4).
        embedded = session._runtime.get("applied_commits", [])
        applied: dict[str, str] = {}
        for entry in embedded:
            cid = entry.get("commit_id")
            if isinstance(cid, str):
                applied[cid] = str(entry.get("content_hash", ""))
        # Merge legacy sidecar entries not already present.
        sidecar = self._read_commit_index(session_key)
        for cid, entry in sidecar.items():
            if cid not in applied:
                applied[cid] = str(entry.get("content_hash", ""))
        return SessionSnapshot(
            session_key=session_key,
            revision=session.revision,
            messages=list(session.messages),
            applied_commit_ids=applied,
        )

    def commit_turn(
        self,
        session_key: str,
        commit_id: str,
        commit_kind: str,
        base_revision: int,
        mutation_messages: list[dict[str, Any]],
        mutation_metadata_updates: dict[str, Any] | None,
        content_hash: str,
    ) -> SessionCommitOutcome:
        """Idempotent, revision-aware session commit (design §21.1, §21.2).

        Implements the 13-step protocol from design §21.2:

        1. acquire OS-visible per-session file lock;
        2. reload current on-disk session, bypassing process cache;
        3. return ``ALREADY_COMMITTED`` if the same commit id and hash exist;
        4. reject reuse of a commit id with a different hash;
        5. compare ``base_revision`` against the reloaded session;
        6. apply the normalized mutation once;
        7. increment revision exactly once;
        8. write a temporary file in the destination directory;
        9. flush and ``fsync`` the file;
        10. atomically replace the destination;
        11. ``fsync`` the parent directory when supported;
        12. refresh or invalidate the process-local cache;
        13. release the lock.

        ``commit_kind`` is ``"INBOUND"`` or ``"FINAL"`` (design §17.7).
        """
        mutation_metadata_updates = mutation_metadata_updates or {}
        lock_path = self._get_lock_path(session_key)

        try:
            with _OSFileLock(lock_path):
                # Step 2: reload from disk, bypassing cache.
                session = self._load(session_key)
                if session is None:
                    session = Session(key=session_key)

                # Steps 3-4: check commit-id idempotency. Prefer the
                # embedded index (authoritative after Task 4); merge the
                # legacy sidecar for migration from pre-Task-4 files.
                embedded_commits = session._runtime.get("applied_commits", [])
                existing_hash: str | None = None
                for entry in embedded_commits:
                    if entry.get("commit_id") == commit_id:
                        existing_hash = str(entry.get("content_hash", ""))
                        break
                if existing_hash is None:
                    sidecar = self._read_commit_index(session_key)
                    sidecar_entry = sidecar.get(commit_id)
                    if sidecar_entry is not None:
                        existing_hash = str(sidecar_entry.get("content_hash", ""))
                if existing_hash is not None:
                    if existing_hash == content_hash:
                        # Step 3: same commit_id + hash → already committed.
                        return SessionCommitOutcome(
                            state="ALREADY_COMMITTED",
                            revision=session.revision,
                        )
                    # Step 4: commit_id reused with a different hash → reject.
                    return SessionCommitOutcome(
                        state="IO_FAILURE",
                        revision=session.revision,
                        error=(
                            f"commit_id {commit_id} already applied with a different content_hash"
                        ),
                    )

                # Step 5: compare base_revision (optimistic concurrency).
                if session.revision != base_revision:
                    return SessionCommitOutcome(
                        state="REVISION_CONFLICT",
                        revision=session.revision,
                    )

                # Step 6: apply the normalized mutation once.
                for msg in mutation_messages:
                    session.messages.append(msg)
                session.metadata.update(mutation_metadata_updates)
                session.updated_at = datetime.now()

                # Step 7: increment revision exactly once.
                session.revision += 1

                # Step 7b (Task 4 Step 3): append the commit entry to the
                # embedded ``_runtime.applied_commits`` index BEFORE the
                # atomic session file replace. The append, message append,
                # and revision increment all occur before the single
                # ``_save_impl(fsync=True)`` call, so the commit identity is
                # crash-atomic with the session content.
                new_entry = {
                    "commit_id": commit_id,
                    "content_hash": content_hash,
                    "revision": session.revision,
                    "commit_kind": commit_kind,
                    "committed_at_ms": int(time.time() * 1000),
                }
                applied_commits = list(embedded_commits)
                applied_commits.append(new_entry)
                # FIFO bound (newest retained).
                if len(applied_commits) > _COMMIT_INDEX_MAX:
                    applied_commits = applied_commits[-_COMMIT_INDEX_MAX:]
                session._runtime = {
                    "format_version": 1,
                    "applied_commits": applied_commits,
                }

                # Steps 8-11: atomic save with fsync (reuse _save_impl).
                self._save_impl(session, fsync=True)

                # Fault injection (Task 4 Step 1): the session file has been
                # durably replaced at this point. If a hook is registered,
                # call it with ``after_session_replace``; a raised exception
                # simulates a crash between the atomic session file
                # replacement and any non-durable post-replace work.
                if self._commit_fault_hook is not None:
                    self._commit_fault_hook("after_session_replace")

                # Step 12: refresh process-local cache. No required
                # persistence operation follows the atomic session replace
                # (Task 4 Step 5) — the commit index is already embedded.
                with self._cache_lock:
                    self._cache[session_key] = session
                    self._touch(session_key)
                    self._enforce_max()

                return SessionCommitOutcome(
                    state="COMMITTED",
                    revision=session.revision,
                )
        except OSError as exc:
            logger.warning("commit_turn IO failure for session {}: {}", session_key, exc)
            return SessionCommitOutcome(
                state="IO_FAILURE",
                revision=base_revision,
                error=str(exc),
            )

    def delete_session(self, key: str) -> bool:
        """Remove a session from disk and the in-memory cache.

        Returns True if a JSONL file was found and unlinked.

        写入 tombstone 并递增 generation:删除后,任何持有旧 Session 引用的
        调用方调用 ``save()`` 都会因为 generation < tombstone 而被跳过,
        避免 late save 通过 ``os.replace`` 重新创建已删除的文件。
        重新 ``get_or_create`` 同一 key 时会使用 tombstone+1 作为新 generation,
        确保新 Session 与旧引用的 generation 不冲突。
        """
        path = self._get_session_path(key)
        # 同时清理 legacy 命名格式的遗留文件。list_sessions 通过 glob 扫描整个
        # sessions_dir,会扫到从未被 get_or_create 加载过(未触发 _migrate_legacy)
        # 的 legacy 文件;若只删 V2 路径,刷新后侧边栏会把这些会话重新列出。
        legacy_workspace_path = self._get_workspace_legacy_session_path(key)
        legacy_global_path = self._get_legacy_session_path(key)
        self.invalidate(key)
        # 写 tombstone:递增 generation,记录删除时刻。
        # _load/_get_or_create 创建新 Session 时使用 max(当前 gen, tombstone+1)。
        current_gen = self._generations.get(key, 0)
        new_gen = current_gen + 1
        self._generations[key] = new_gen
        self._tombstones[key] = new_gen
        removed_any = False
        # Also clean up WP2 sidecars (lock + commit-id index).
        lock_path = self._get_lock_path(key)
        commits_path = self._get_commits_path(key)
        for candidate in (
            path,
            legacy_workspace_path,
            legacy_global_path,
            lock_path,
            commits_path,
        ):
            if not candidate.exists():
                continue
            try:
                candidate.unlink()
                removed_any = True
            except OSError as e:
                logger.warning("Failed to delete session file {}: {}", candidate, e)
        return removed_any

    def rewind_to_user_message(self, key: str, user_message_index: int) -> int:
        """Truncate session messages to remove the N-th user message and everything after.

        Args:
            key: Session key (e.g., ``websocket:<chat_id>``).
            user_message_index: 0-based index of the user message to rewind from.
                Pass ``0`` to clear all messages.

        Returns:
            The number of messages removed. Returns ``0`` when the target user
            message cannot be located.
        """
        if user_message_index < 0:
            return 0
        session = self.get_or_create(key)
        if not session.messages:
            return 0
        user_count = 0
        cutoff = len(session.messages)
        for i, msg in enumerate(session.messages):
            if msg.get("role") == "user":
                if user_count == user_message_index:
                    cutoff = i
                    break
                user_count += 1
        if cutoff >= len(session.messages):
            return 0
        removed = len(session.messages) - cutoff
        session.messages = session.messages[:cutoff]
        # Keep last_consolidated within bounds — consolidated entries are
        # referenced by index relative to session.messages. If a rewind removes
        # consolidated content, reset the pointer so history replay starts fresh.
        if session.last_consolidated > len(session.messages):
            session.last_consolidated = len(session.messages)
        session.updated_at = datetime.now()
        self.save(session)
        return removed

    def read_session_file(self, key: str) -> dict[str, Any] | None:
        """Load a session from disk without caching; intended for read-only HTTP endpoints.

        Returns ``{"key", "created_at", "updated_at", "metadata", "messages"}`` or
        ``None`` when the session file does not exist or fails to parse.
        """
        path = self._get_session_path(key)
        if not path.exists():
            return None
        try:
            messages: list[dict[str, Any]] = []
            metadata: dict[str, Any] = {}
            created_at: str | None = None
            updated_at: str | None = None
            stored_key: str | None = None
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    data = json.loads(line)
                    if data.get("_type") == "metadata":
                        metadata = data.get("metadata", {})
                        created_at = data.get("created_at")
                        updated_at = data.get("updated_at")
                        stored_key = data.get("key")
                    else:
                        messages.append(data)
            return {
                "key": stored_key or key,
                "created_at": created_at,
                "updated_at": updated_at,
                "metadata": metadata,
                "messages": messages,
            }
        except Exception as e:
            logger.warning("Failed to read session {}: {}", key, e)
            repaired = self._repair(key)
            if repaired is not None:
                logger.info("Recovered read-only session view {} from corrupt file", key)
                return self._session_payload(repaired)
            return None

    def list_sessions(self) -> list[dict[str, Any]]:
        """
        List all sessions.

        Returns:
            List of session info dicts.
        """
        sessions = []

        for path in self.sessions_dir.glob("*.jsonl"):
            # v2 文件名格式:``<prefix>--<sha256>.jsonl``。
            # v2 文件的 key 从 metadata 行读取(authoritative);fallback 仅用于
            # metadata 缺失时的 _repair 调用,此时 v2 stem 无法可靠反推 key,
            # 用 stem 原值即可(_repair 会因路径不匹配返回 None,安全跳过)。
            stem = path.stem
            if "--" in stem:
                fallback_key = stem  # v2:无法从 stem 反推 key,用原值
            else:
                fallback_key = stem.replace("_", ":", 1)  # legacy:向后兼容
            try:
                # Read the metadata line and a small preview for WebUI/session lists.
                with open(path, encoding="utf-8") as f:
                    first_line = f.readline().strip()
                    if first_line:
                        data = json.loads(first_line)
                        if data.get("_type") == "metadata":
                            key = data.get("key") or fallback_key
                            metadata = data.get("metadata", {})
                            title = metadata.get("title") if isinstance(metadata, dict) else None
                            preview = ""
                            fallback_preview = ""
                            scanned_records = 0
                            scanned_chars = 0
                            for line in f:
                                if not line.strip():
                                    continue
                                scanned_records += 1
                                scanned_chars += len(line)
                                if (
                                    scanned_records > _SESSION_LIST_PREVIEW_MAX_RECORDS
                                    or scanned_chars > _SESSION_LIST_PREVIEW_MAX_CHARS
                                ):
                                    break
                                item = json.loads(line)
                                if item.get("_type") == "metadata":
                                    continue
                                text = _message_preview_text(item)
                                if not text:
                                    continue
                                if item.get("role") == "user":
                                    preview = text
                                    break
                                if not fallback_preview and item.get("role") == "assistant":
                                    fallback_preview = text
                            preview = preview or fallback_preview
                            sessions.append(
                                {
                                    "key": key,
                                    "created_at": data.get("created_at"),
                                    "updated_at": data.get("updated_at"),
                                    "title": title if isinstance(title, str) else "",
                                    "preview": preview,
                                    "path": str(path),
                                }
                            )
            except Exception:
                # 用 path 直接修复,不依赖 fallback_key 反推(fallback_key 在 v2
                # 命名下无法可靠反推原始 key,但 _repair_path 可从 metadata 读取)。
                repaired = self._repair_path(path)
                if repaired is not None:
                    sessions.append(
                        {
                            "key": repaired.key,
                            "created_at": repaired.created_at.isoformat(),
                            "updated_at": repaired.updated_at.isoformat(),
                            "title": (
                                repaired.metadata.get("title")
                                if isinstance(repaired.metadata.get("title"), str)
                                else ""
                            ),
                            "preview": next(
                                (
                                    text
                                    for msg in repaired.messages
                                    if (text := _message_preview_text(msg))
                                ),
                                "",
                            ),
                            "path": str(path),
                        }
                    )
                continue

        return sorted(sessions, key=lambda x: x.get("updated_at", ""), reverse=True)
