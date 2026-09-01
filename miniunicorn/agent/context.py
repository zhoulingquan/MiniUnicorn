"""Context builder for assembling agent prompts."""

import base64
import mimetypes
import platform
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from loguru import logger

from miniunicorn.agent.memory import MemoryStore, WorkspaceMemoryRegistry
from miniunicorn.agent.memory_models import MemoryScope, RecallQuery, ScopeKind
from miniunicorn.agent.persist_tags import RUNTIME_CONTEXT_TAG
from miniunicorn.agent.runtime_view import RuntimeStateView
from miniunicorn.agent.skills import SkillsLoader
from miniunicorn.agent.subagent_registry import SubagentDefinition
from miniunicorn.apps.cli import utils as cli_app_utils
from miniunicorn.bus.events import InboundMessage
from miniunicorn.config.schema import StructuredMemoryConfig
from miniunicorn.session.goal_state import goal_state_runtime_lines
from miniunicorn.tools import mcp as mcp_tools
from miniunicorn.tools.registry import ToolRegistry
from miniunicorn.utils.helpers import (
    current_time_str,
    detect_image_mime,
    load_bundled_template,
    truncate_text,
)
from miniunicorn.utils.prompt_templates import render_template


def session_extra(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return persisted kwargs for turn-attached capabilities."""
    return cli_app_utils.session_extra(metadata) | mcp_tools.session_extra(metadata)


def runtime_lines(
    state: RuntimeStateView,
    msg: Any,
    workspace: Path,
    *,
    skip: bool = False,
    cli_apps_enabled: bool = True,
) -> list[str]:
    """Return model-visible runtime annotations for turn-attached capabilities.

    ``cli_apps_enabled=False`` (the default config) skips the CLI Apps
    helpers entirely so the optional ``apps`` service is never touched on
    the gateway main flow.
    """
    cli_lines = cli_app_utils.runtime_lines(msg, workspace, skip=skip) if cli_apps_enabled else []
    return [
        *cli_lines,
        *mcp_tools.runtime_lines(
            msg,
            configured_server_names=set(state.mcp_servers),
            connected_server_names=set(state.mcp_stacks),
            skip=skip,
        ),
    ]


async def connect_mcp(state: mcp_tools.RuntimeState, tools: ToolRegistry) -> None:
    await mcp_tools.connect_missing_servers(state, tools)


async def handle_runtime_control(
    state: mcp_tools.RuntimeState, msg: InboundMessage, tools: ToolRegistry
) -> bool:
    return await mcp_tools.handle_runtime_control(state, msg, tools)


class ContextBuilder:
    """Builds the context (system prompt + messages) for the agent."""

    BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md"]
    _RUNTIME_CONTEXT_TAG = RUNTIME_CONTEXT_TAG
    _MAX_RECENT_HISTORY = 50
    _MAX_HISTORY_CHARS = 32_000  # hard cap on recent history section size
    _RUNTIME_CONTEXT_END = "[/Runtime Context]"

    # 总注入预算上限（借鉴 MiMo Code 的 65K rebuild budget）。
    # system prompt 各部分（identity/bootstrap/memory/skills/history/notes 等）
    # 加起来不应超过此值，否则会挤占 user message 与 model 输出空间。
    # 超出时按优先级丢弃/截断：notes → skills list → history → memory → ...
    _MAX_INJECTION_TOKENS = 65_000

    # 优先级：数字越小越重要，越不容易被丢弃。
    # CRITICAL（身份/bootstrap/tool_contract）永不丢弃；
    # SUMMARY（归档 summary）高优先级保留——这是上下文连续性的关键；
    # NOTES 最低优先级（临时 scratchpad，丢了能重建）。
    _PRIORITY_CRITICAL = 0
    _PRIORITY_SUMMARY = 1
    _PRIORITY_MEMORY = 2
    _PRIORITY_SHARED_MEMORY = 2
    _PRIORITY_HISTORY = 3
    _PRIORITY_SKILLS_ACTIVE = 4
    _PRIORITY_SKILLS_LIST = 5
    _PRIORITY_SUBAGENT = 5
    _PRIORITY_NOTES = 6

    def __init__(
        self,
        workspace: Path,
        timezone: str | None = None,
        disabled_skills: list[str] | None = None,
        subagent_registry: Any = None,
        structured_memory_config: StructuredMemoryConfig | None = None,
        cli_apps_enabled: bool = True,
    ):
        self.workspace = workspace
        self.timezone = None
        self.cli_apps_enabled = cli_apps_enabled
        self.memory = MemoryStore(workspace, structured_config=structured_memory_config)
        self.memory_registry = WorkspaceMemoryRegistry(
            workspace,
            self.memory,
            structured_config=structured_memory_config,
        )
        self.skills = SkillsLoader(
            workspace, disabled_skills=set(disabled_skills) if disabled_skills else None
        )
        self.subagent_registry = subagent_registry
        # Bootstrap file (AGENTS.md/SOUL.md) mtime+size cache.
        # build_system_prompt 每次 turn 都会读取，turn 内不变；外部修改（Dream
        # 或用户）通过 mtime 自动失效。key=Path, value=(mtime_ns, size, content)。
        self._bootstrap_cache: dict[Path, tuple[int, int, str]] = {}

    # -- governed structured memory (C2) --------------------------------------

    def memory_for(self, workspace: Path | None = None) -> MemoryStore:
        """Resolve (and cache) the governed ``MemoryStore`` for an effective workspace.

        ``None`` resolves the default workspace's store (``self.memory``),
        preserving compatibility with callers that build memory without an
        explicit effective workspace. Repeated/concurrent resolution for the
        same resolved path returns the same store instance.
        """
        root = workspace or self.workspace
        return self.memory_registry.memory_for(root)

    def _build_recall_query(
        self,
        query_text: str,
        *,
        session_key: str | None = None,
        user_key: str | None = None,
        store: MemoryStore | None = None,
    ) -> RecallQuery:
        """Build the deterministic recall query for the current workspace."""
        store = store or self.memory
        structured = store.structured_config
        allowed_scopes = []
        if session_key:
            allowed_scopes.append(
                MemoryScope(kind=ScopeKind.SESSION, key=f"session:{session_key.split('#', 1)[0]}")
            )
        allowed_scopes.extend(
            (
                MemoryScope(kind=ScopeKind.PROJECT, key=store.project_scope_key),
                MemoryScope(kind=ScopeKind.USER, key=user_key or "user:default"),
                MemoryScope(kind=ScopeKind.SHARED, key="shared:*"),
            )
        )
        return RecallQuery(
            query_text=query_text,
            allowed_scopes=tuple(allowed_scopes),
            now=datetime.now(timezone.utc),
            token_budget=structured.recall_token_budget,
            max_hits=structured.max_recall_hits,
        )

    def _recall_section(
        self,
        query_text: str,
        *,
        session_key: str | None = None,
        user_key: str | None = None,
        store: MemoryStore | None = None,
    ) -> str:
        """Run deterministic recall and render its prompt section."""
        store = store or self.memory
        query = self._build_recall_query(
            query_text, session_key=session_key, user_key=user_key, store=store
        )
        try:
            result = store.recall_structured(query)
        except Exception:
            logger.exception("structured recall failed")
            return self._recall_degraded_diagnostic("recall_error")
        structured = store.structured_config
        if structured.recall_audit_enabled:
            try:
                store.write_recall_audit(query, result)
            except Exception:
                logger.exception("structured recall audit failed")
        recall = store.structured_recall
        if result.degraded:
            return self._recall_degraded_diagnostic(result.error_code)
        return recall.render_prompt(result)

    @staticmethod
    def _recall_degraded_diagnostic(error_code: str | None) -> str:
        code = re.sub(r"[^a-zA-Z0-9_.-]", "_", error_code or "unknown")[:80]
        return (
            "# Structured Memory Diagnostic\n\n"
            f"Structured memory recall is unavailable (code: `{code}`). "
            "No structured memory facts were injected."
        )

    def build_system_prompt(
        self,
        skill_names: list[str] | None = None,
        channel: str | None = None,
        session_summary: str | None = None,
        workspace: Path | None = None,
        agent_override: SubagentDefinition | None = None,
        light_context: bool = False,
        recall_query: str | None = None,
        recall_session_key: str | None = None,
        recall_user_key: str | None = None,
    ) -> str:
        """Build the system prompt from identity, bootstrap files, memory, and skills.

        When ``agent_override`` is provided (subagent takeover mode), the
        subagent's system_prompt replaces the default agent identity and the
        "Available Subagents" delegation list is omitted — the subagent runs
        as the primary identity for the turn.

        Only POLICY.md plus the deterministic recall result
        (when ``recall_query`` is given) are injected as durable memory.
        """
        # parts: list of (priority, content) tuples。
        # priority 数字越小越重要，越不容易被预算控制丢弃。
        # 最后通过 _enforce_injection_budget 按优先级截断/丢弃。
        parts: list[tuple[int, str]] = []

        root = workspace or self.workspace
        store = self.memory_for(root)
        if agent_override is not None:
            # Subagent takeover mode: user selected a subagent via @. Run as
            # that subagent's identity instead of the default agent identity.
            header = (
                f"You are running as the '{agent_override.name}' agent. "
                f"{agent_override.description}"
            )
            parts.append((self._PRIORITY_CRITICAL, header))
            parts.append((self._PRIORITY_CRITICAL, agent_override.system_prompt))
        else:
            parts.append(
                (self._PRIORITY_CRITICAL, self._get_identity(channel=channel, workspace=root))
            )

        # light_context 模式跳过 bootstrap 文件(AGENTS.md/SOUL.md),
        # 仅保留身份+工具契约+记忆,显著降低 token 消耗。用于心跳等轻量巡检场景。
        if not light_context:
            bootstrap = self._load_bootstrap_files(root)
            if bootstrap:
                parts.append((self._PRIORITY_CRITICAL, bootstrap))

        parts.append((self._PRIORITY_CRITICAL, render_template("agent/tool_contract.md")))

        # Cross-session policy is the only always-injected memory layer.
        # read_shared_policy already returns "" for missing/bundled-template content.
        policy = store.read_shared_policy()
        if policy and policy.strip():
            if self._estimate_tokens(policy) > self._MAX_INJECTION_TOKENS:
                logger.warning("shared policy exceeds injection budget; not truncated")
            parts.append((self._PRIORITY_CRITICAL, f"# Shared Policy (Cross-Session)\n\n{policy}"))

        if recall_query and not light_context:
            recall_text = self._recall_section(
                truncate_text(recall_query, 2000),
                session_key=recall_session_key,
                user_key=recall_user_key,
                store=store,
            )
            if recall_text:
                parts.append((self._PRIORITY_MEMORY, recall_text))

        # 注入 notes.md（主 Agent 的 scratchpad，借鉴 MiMo Code）。
        # 主 Agent 用 write_file/edit_file 往 notes.md append 零散发现，
        # Consolidator 在归档时读取并清空。注入让主 Agent 能看到自己之前
        # 记的笔记，支持跨 turn 的临时记忆。文件不存在或为空时跳过。
        notes = store.read_notes()
        if notes and notes.strip():
            parts.append((self._PRIORITY_NOTES, f"# Scratchpad Notes (notes.md)\n\n{notes}"))

        always_skills = self.skills.get_always_skills()
        if always_skills:
            always_content = self.skills.load_skills_for_context(always_skills)
            if always_content:
                parts.append((self._PRIORITY_SKILLS_ACTIVE, f"# Active Skills\n\n{always_content}"))

        skills_summary = self.skills.build_skills_summary(exclude=set(always_skills))
        if skills_summary:
            parts.append(
                (
                    self._PRIORITY_SKILLS_LIST,
                    render_template("agent/skills_section.md", skills_summary=skills_summary),
                )
            )

        entries = store.read_unprocessed_history(since_cursor=store.get_last_dream_cursor())
        if entries:
            capped = entries[-self._MAX_RECENT_HISTORY :]
            history_text = "\n".join(
                f"- [{entry['timestamp']}] {entry['content']}" for entry in capped
            )
            history_text = truncate_text(history_text, self._MAX_HISTORY_CHARS)
            parts.append((self._PRIORITY_HISTORY, "# Recent History\n\n" + history_text))

        if session_summary:
            parts.append(
                (self._PRIORITY_SUMMARY, f"[Archived Context Summary]\n\n{session_summary}")
            )

        # Inject available declarative subagents (TRAE-style auto-delegation).
        # Skip in takeover mode — a subagent running as the primary identity
        # should not delegate to other subagents.
        if agent_override is None and self.subagent_registry:
            subagent_section = self.subagent_registry.build_prompt_section()
            if subagent_section:
                parts.append((self._PRIORITY_SUBAGENT, subagent_section))

        # 按优先级分配 65K token 注入预算（借鉴 MiMo Code 的 rebuild budget）。
        # 超出预算时按优先级从低到高丢弃/截断：NOTES → SKILLS_LIST/SUBAGENT →
        # HISTORY → MEMORY → SUMMARY → CRITICAL（CRITICAL 永不丢弃）。
        parts = self._enforce_injection_budget(parts)

        return "\n\n---\n\n".join(p[1] for p in parts)

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """粗略估算 token 数（chars/4 启发式）。

        精确 tiktoken 估算每次 build 都跑代价太大，这里用启发式。
        实际 LLM tokenizer 对中文 1 字 ≈ 1-2 token，英文 4 字符 ≈ 1 token，
        综合下来 chars/4 是个合理的下界估计，足以做预算控制。
        """
        return max(1, len(text) // 4)

    @classmethod
    def _enforce_injection_budget(
        cls,
        parts: list[tuple[int, str]],
    ) -> list[tuple[int, str]]:
        """按优先级分配 65K token 注入预算。

        策略：
        1. 计算总 token 估计，若未超预算直接返回。
        2. 超预算时，从最低优先级开始处理：
           - 优先级 >= _PRIORITY_NOTES（6）：直接丢弃（notes/subagent 列表）
           - 优先级 == _PRIORITY_SKILLS_LIST（5）：直接丢弃（skills 列表/subagent）
           - 优先级 == _PRIORITY_HISTORY（3）：截断到剩余预算的 50%
           - 优先级 == _PRIORITY_MEMORY（2）：截断到剩余预算的 30%
           - 优先级 == _PRIORITY_SUMMARY（1）：截断到剩余预算的 80%
           - 优先级 == _PRIORITY_CRITICAL（0）：永不丢弃
        3. 每次处理后重新计算总量，达标即停。
        """
        budget = cls._MAX_INJECTION_TOKENS

        def total_tokens() -> int:
            return sum(cls._estimate_tokens(p[1]) for p in parts)

        if total_tokens() <= budget:
            return parts

        # 按优先级从低到高处理（数字大的先处理）
        # 第 1 步：丢弃所有 NOTES（最低优先级，临时内容）
        parts = [p for p in parts if p[0] != cls._PRIORITY_NOTES]
        if total_tokens() <= budget:
            return parts

        # 第 2 步：丢弃所有 SKILLS_LIST 和 SUBAGENT（列表性质，可重建）
        parts = [
            p for p in parts if p[0] not in (cls._PRIORITY_SKILLS_LIST, cls._PRIORITY_SUBAGENT)
        ]
        if total_tokens() <= budget:
            return parts

        # 第 3 步：截断 HISTORY 到剩余预算的 50%
        remaining = budget - sum(
            cls._estimate_tokens(p[1]) for p in parts if p[0] != cls._PRIORITY_HISTORY
        )
        history_quota = max(2000, remaining // 2)
        new_parts: list[tuple[int, str]] = []
        for p in parts:
            if p[0] == cls._PRIORITY_HISTORY:
                truncated = truncate_text(p[1], history_quota * 4)
                new_parts.append((p[0], truncated))
            else:
                new_parts.append(p)
        parts = new_parts
        if total_tokens() <= budget:
            return parts

        # 第 4 步：截断 MEMORY/SHARED_MEMORY 到剩余预算的 30%
        remaining = budget - sum(
            cls._estimate_tokens(p[1])
            for p in parts
            if p[0] not in (cls._PRIORITY_MEMORY, cls._PRIORITY_SHARED_MEMORY)
        )
        memory_quota = max(1500, (remaining * 3) // 10)
        new_parts = []
        for p in parts:
            if p[0] in (cls._PRIORITY_MEMORY, cls._PRIORITY_SHARED_MEMORY):
                truncated = truncate_text(p[1], memory_quota * 4)
                new_parts.append((p[0], truncated))
            else:
                new_parts.append(p)
        parts = new_parts
        if total_tokens() <= budget:
            return parts

        # 第 5 步：截断 SKILLS_ACTIVE 到剩余预算的 30%
        remaining = budget - sum(
            cls._estimate_tokens(p[1]) for p in parts if p[0] != cls._PRIORITY_SKILLS_ACTIVE
        )
        skills_quota = max(1000, (remaining * 3) // 10)
        new_parts = []
        for p in parts:
            if p[0] == cls._PRIORITY_SKILLS_ACTIVE:
                truncated = truncate_text(p[1], skills_quota * 4)
                new_parts.append((p[0], truncated))
            else:
                new_parts.append(p)
        parts = new_parts
        if total_tokens() <= budget:
            return parts

        # 第 6 步：截断 SUMMARY 到剩余预算的 80%（summary 是上下文连续性关键）
        remaining = budget - sum(
            cls._estimate_tokens(p[1]) for p in parts if p[0] != cls._PRIORITY_SUMMARY
        )
        summary_quota = max(1000, (remaining * 4) // 5)
        new_parts = []
        for p in parts:
            if p[0] == cls._PRIORITY_SUMMARY:
                truncated = truncate_text(p[1], summary_quota * 4)
                new_parts.append((p[0], truncated))
            else:
                new_parts.append(p)
        parts = new_parts
        # CRITICAL 永不丢弃；若仍超预算只能接受（说明 bootstrap 文件本身就超大，
        # 用户应自行精简 SOUL.md/AGENTS.md）
        return parts

    def _get_identity(self, channel: str | None = None, workspace: Path | None = None) -> str:
        """Get the core identity section."""
        root = workspace or self.workspace
        workspace_path = str(root.expanduser().resolve())
        system = platform.system()
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}"

        return render_template(
            "agent/identity.md",
            workspace_path=workspace_path,
            runtime=runtime,
            platform_policy=render_template("agent/platform_policy.md", system=system),
            channel=channel or "",
        )

    @staticmethod
    def _build_runtime_context(
        channel: str | None,
        chat_id: str | None,
        timezone: str | None = None,
        sender_id: str | None = None,
        supplemental_lines: Sequence[str] | None = None,
    ) -> str:
        """Build untrusted runtime metadata block appended after user content."""
        lines = [f"Current Time: {current_time_str()}"]
        if channel and chat_id:
            lines += [f"Channel: {channel}", f"Chat ID: {chat_id}"]
        if sender_id:
            lines += [f"Sender ID: {sender_id}"]
        if supplemental_lines:
            lines.extend(supplemental_lines)
        return (
            ContextBuilder._RUNTIME_CONTEXT_TAG
            + "\n"
            + "\n".join(lines)
            + "\n"
            + ContextBuilder._RUNTIME_CONTEXT_END
        )

    @staticmethod
    def _merge_message_content(left: Any, right: Any) -> str | list[dict[str, Any]]:
        if isinstance(left, str) and isinstance(right, str):
            return f"{left}\n\n{right}" if left else right

        def _to_blocks(value: Any) -> list[dict[str, Any]]:
            if isinstance(value, list):
                return [
                    item if isinstance(item, dict) else {"type": "text", "text": str(item)}
                    for item in value
                ]
            if value is None:
                return []
            return [{"type": "text", "text": str(value)}]

        return _to_blocks(left) + _to_blocks(right)

    def _load_bootstrap_files(self, workspace: Path | None = None) -> str:
        """Load all instruction and identity bootstrap files."""
        parts = []
        root = workspace or self.workspace

        for filename in self.BOOTSTRAP_FILES:
            file_path = root / filename
            content = self._cached_read_bootstrap(file_path)
            if content:
                parts.append(f"## {filename}\n\n{content}")

        return "\n\n".join(parts) if parts else ""

    def _cached_read_bootstrap(self, path: Path) -> str:
        """带 mtime+size 校验的缓存读取 bootstrap 文件。

        AGENTS.md/SOUL.md 在 turn 内不变，外部修改后通过 mtime
        自动失效。避免每次 build_system_prompt 都做磁盘 IO。
        """
        try:
            st = path.stat()
        except (FileNotFoundError, OSError):
            self._bootstrap_cache.pop(path, None)
            return ""
        key = (st.st_mtime_ns, st.st_size)
        cached = self._bootstrap_cache.get(path)
        if cached is not None and (cached[0], cached[1]) == key:
            return cached[2]
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return ""
        self._bootstrap_cache[path] = (st.st_mtime_ns, st.st_size, content)
        return content

    @staticmethod
    def _is_template_content(content: str, template_path: str) -> bool:
        """Check if *content* is identical to the bundled template (user hasn't customized it)."""
        tpl = load_bundled_template(template_path)
        if tpl is not None:
            return content.strip() == tpl.strip()
        return False

    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        skill_names: list[str] | None = None,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        current_role: str = "user",
        sender_id: str | None = None,
        session_key: str | None = None,
        memory_user_key: str | None = None,
        session_summary: str | None = None,
        session_metadata: Mapping[str, Any] | None = None,
        current_runtime_lines: Sequence[str] | None = None,
        workspace: Path | None = None,
        runtime_state: mcp_tools.RuntimeState | None = None,
        inbound_message: Any | None = None,
        skip_runtime_lines: bool = False,
        agent_override: SubagentDefinition | None = None,
    ) -> list[dict[str, Any]]:
        """Build the complete message list for an LLM call."""
        root = workspace or self.workspace
        extra = [
            *goal_state_runtime_lines(session_metadata),
        ]
        if runtime_state is not None and inbound_message is not None:
            extra.extend(
                runtime_lines(
                    runtime_state,
                    inbound_message,
                    root,
                    skip=skip_runtime_lines,
                    cli_apps_enabled=self.cli_apps_enabled,
                )
            )
        if current_runtime_lines:
            extra.extend(line for line in current_runtime_lines if line)
        runtime_ctx = self._build_runtime_context(
            channel,
            chat_id,
            self.timezone,
            sender_id=sender_id,
            supplemental_lines=extra or None,
        )
        user_content = self._build_user_content(current_message, media)

        # Merge runtime context and user content into a single user message
        # to avoid consecutive same-role messages that some providers reject.
        # Runtime context is appended to keep the user-content prefix stable
        # for prompt-cache hits (the context changes every turn due to time).
        if isinstance(user_content, str):
            merged = f"{user_content}\n\n{runtime_ctx}"
        else:
            merged = user_content + [{"type": "text", "text": runtime_ctx}]
        # light_context 从 runtime_state 读取(由 AgentLoop 设置,用于心跳等轻量场景)
        light_context = (
            bool(getattr(runtime_state, "_light_context", False)) if runtime_state else False
        )
        # Structured recall is keyed on the current user
        # message; light/heartbeat turns skip recall entirely.
        recall_query = None if light_context else current_message
        if memory_user_key:
            recall_user_key = memory_user_key
        else:
            recall_user_id = sender_id if sender_id != "subagent" else None
            recall_user_key = f"user:{recall_user_id}" if recall_user_id else "user:default"
        messages = [
            {
                "role": "system",
                "content": self.build_system_prompt(
                    skill_names,
                    channel=channel,
                    session_summary=session_summary,
                    workspace=root,
                    agent_override=agent_override,
                    light_context=light_context,
                    recall_query=recall_query,
                    recall_session_key=session_key,
                    recall_user_key=recall_user_key,
                ),
            },
            *history,
        ]
        if messages[-1].get("role") == current_role:
            last = dict(messages[-1])
            last["content"] = self._merge_message_content(last.get("content"), merged)
            messages[-1] = last
            return messages
        messages.append({"role": current_role, "content": merged})
        return messages

    def _build_user_content(self, text: str, media: list[str] | None) -> str | list[dict[str, Any]]:
        """Build user message content with optional base64-encoded images."""
        if not media:
            return text

        images = []
        for path in media:
            p = Path(path)
            if not p.is_file():
                continue
            raw = p.read_bytes()
            mime = detect_image_mime(raw) or mimetypes.guess_type(path)[0]
            if not mime or not mime.startswith("image/"):
                continue
            b64 = base64.b64encode(raw).decode()
            images.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"},
                    "_meta": {"path": str(p)},
                }
            )

        if not images:
            return text
        return images + [{"type": "text", "text": text}]
