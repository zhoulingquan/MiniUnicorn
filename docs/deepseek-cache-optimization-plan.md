# MiniUnicorn DeepSeek 缓存命中率优化方案

> **目标**：将 DeepSeek 前缀缓存命中率从当前水平提升至 85%+，重点优化长会话场景。
>
> **核心原则**：系统提示在 session 内字节级稳定；所有运行时变化走 user 消息尾部。
>
> **参考**：DeepSeek-Reasonix 的六层缓存架构 + DeepSeek 官方 Context Caching 文档。

---

## 总览：6 个阶段，按优先级排列

| 阶段 | 名称 | 涉及文件 | 预期收益 | 工作量 |
|------|------|----------|----------|--------|
| Phase 1 | Memory 移出 system prompt | `context.py` `loop.py` `memory_prompt.py` | 极高 | 中 |
| Phase 2 | System prompt session 级缓存 | `loop.py` `context.py` | 高 | 小 |
| Phase 3 | reasoning_content 精细化回传 | `openai_compat_provider.py` | 中 | 小 |
| Phase 4 | 压缩阈值分级 | `loop.py` `autocompact.py` | 中 | 中 |
| Phase 5 | 缓存命中率监控与展示 | `loop.py` 新建 `cache_shape.py` `progress_hook.py` | 中 | 中 |
| Phase 6 | cache_miss 字段补全 | `openai_compat_provider.py` `turn_budget.py` | 低 | 小 |

**每个阶段可独立实施、独立测试、独立合并。建议按顺序执行。**

---

## Phase 1：Memory 移出 system prompt

### 问题

`loop.py:800-803` 的 `_refresh_memory` 在每次工具迭代时，用新的 recall 结果替换 system prompt 里的 `<!-- miniunicorn-memory:start/end -->` 标记块。两轮工具调用之间 recall 结果若不同，system prompt 内容就变了，从 memory 块往后全部 cache miss。

### 改动清单

#### 1.1 `miniunicorn/agent/context.py` — `build_system_prompt` 移除 memory 注入

**位置**：`build_system_prompt` 方法，约第 166-170 行

**删除以下代码**：

```python
# 删除这整段（约第 166-170 行）：
if memory_prompt is not None and memory_prompt.text:
    block = memory_prompt.text
    if START_MARK not in block:
        block = f"{START_MARK}\n{block}\n{END_MARK}"
    parts.append((self._PRIORITY_MEMORY, block))
```

**同时删除 `build_system_prompt` 签名中的 `memory_prompt` 参数**（约第 112 行）：

```python
# 修改前：
def build_system_prompt(
    self,
    skill_names: list[str] | None = None,
    channel: str | None = None,
    session_summary: str | None = None,
    workspace: Path | None = None,
    memory_prompt: MemoryPromptPayload | None = None,  # ← 删除这行
    agent_override: SubagentDefinition | None = None,
    light_context: bool = False,
) -> str:

# 修改后：
def build_system_prompt(
    self,
    skill_names: list[str] | None = None,
    channel: str | None = None,
    session_summary: str | None = None,
    workspace: Path | None = None,
    agent_override: SubagentDefinition | None = None,
    light_context: bool = False,
) -> str:
```

**删除 docstring 中关于 memory_prompt 的描述**（约第 124 行 "the bounded memory section is injected from ``memory_prompt``." 那句）。

**移除不再需要的 import**（约第 10-15 行）：

```python
# 保留 MemoryPromptPolicy（bounded_soul 仍在用），移除 START_MARK, END_MARK, MemoryPromptPayload
# 修改前：
from miniunicorn.agent.memory_prompt import (
    END_MARK,
    START_MARK,
    MemoryPromptPayload,
    MemoryPromptPolicy,
)

# 修改后：
from miniunicorn.agent.memory_prompt import MemoryPromptPolicy
```

#### 1.2 `miniunicorn/agent/context.py` — `build_messages` 将 memory 拼到 user 尾巴

**位置**：`build_messages` 方法，约第 459-515 行

**修改签名**：保留 `memory_prompt` 参数（它现在拼到 user 消息而不是 system 消息）。

**修改 `build_messages` 内部**，在拼接 `merged` 之后、构造 messages 之前，追加 memory 尾巴：

```python
# 约第 480-489 行，修改 merged 拼接逻辑：

# 修改前：
user_content = self._build_user_content(current_message, media)
if isinstance(user_content, str):
    merged = f"{user_content}\n\n{runtime_ctx}"
else:
    merged = user_content + [{"type": "text", "text": runtime_ctx}]

# 修改后：
user_content = self._build_user_content(current_message, media)
# Memory recall 结果拼到 user 消息尾部，不进 system prompt
memory_tail = ""
if memory_prompt is not None and memory_prompt.text:
    memory_tail = f"\n\n<!-- miniunicorn-memory -->\n{memory_prompt.text}"

if isinstance(user_content, str):
    merged = f"{user_content}\n\n{runtime_ctx}{memory_tail}"
else:
    extra_blocks = [{"type": "text", "text": runtime_ctx + memory_tail}]
    merged = user_content + extra_blocks
```

**修改 `build_system_prompt` 调用**（约第 497-505 行），移除 `memory_prompt` 参数：

```python
# 修改前：
"content": self.build_system_prompt(
    skill_names,
    channel=channel,
    session_summary=session_summary,
    workspace=root,
    memory_prompt=memory_prompt,
    agent_override=agent_override,
    light_context=light_context,
),

# 修改后：
"content": self.build_system_prompt(
    skill_names,
    channel=channel,
    session_summary=session_summary,
    workspace=root,
    agent_override=agent_override,
    light_context=light_context,
),
```

#### 1.3 `miniunicorn/agent/loop.py` — 移除 `_refresh_memory` 的 marker splice

**位置**：约第 792-806 行

**修改前**：

```python
# Per-call memory refresh for the main dialogue: every real chat-provider
# call (including tool iterations and finalization retries) re-reads the
# index using the turn's original user query and splices only the marked
# memory section, so the prompt is refreshed, never duplicated.
before_provider_call = None
if turn_query is not None:
    control = self.embedding_control

    async def _refresh_memory(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        recall = await control.recall_for_turn(turn_query)
        payload = control.prompt_policy.build(recall)
        MemoryPromptPolicy.replace_section(messages, payload)
        return messages

    before_provider_call = _refresh_memory
```

**修改后**：

```python
# Memory recall is now baked into the user message at _build_initial_messages
# time and does NOT change during tool iterations. This keeps the system
# prompt byte-stable across all provider calls within a turn, maximizing
# DeepSeek prefix cache hits. Memory is re-recalled on the next user turn.
before_provider_call = None
```

**移除不再需要的 import**（约第 27 行）：

```python
# 修改前：
from miniunicorn.agent.context import ContextBuilder, MemoryPromptPolicy

# 修改后：
from miniunicorn.agent.context import ContextBuilder
```

#### 1.4 `miniunicorn/agent/memory_prompt.py` — `replace_section` 标记为废弃

**位置**：`replace_section` 静态方法，约第 119 行

**在 docstring 中添加废弃说明**（方法体保留，避免破坏存量调用，但新代码不应使用）：

```python
@staticmethod
def replace_section(messages: list[dict], payload: MemoryPromptPayload) -> None:
    """Splice *payload* into the first system message, replacing the marker block.

    .. deprecated::
        Memory is now injected into the user message tail, not the system
        prompt. This method is kept for backward compatibility but should
        not be called in new code. The marker-block splice invalidated the
        system prompt prefix on every tool iteration, destroying DeepSeek
        cache hits.
    """
```

`START_MARK` / `END_MARK` 常量保留不动（`build_messages` 不再用它们，但保留以避免破坏外部引用）。

#### 1.5 测试更新

**文件**：`tests/agent/test_context_prompt_cache.py`

**修改 `test_runtime_context_is_separate_untrusted_user_message`**（约第 64-87 行）：该测试断言 `messages[0]["role"] == "system"` 且 `"## Current Session" not in messages[0]["content"]`。这些断言仍然成立（memory 本来就不在 system 里注入了）。但需要新增断言：**memory_prompt 传入时，memory 文本出现在 user 消息而非 system 消息中**。

新增测试：

```python
def test_memory_prompt_injected_into_user_message_not_system(tmp_path) -> None:
    """Memory recall results must appear in the user message, not the system prompt."""
    from miniunicorn.agent.memory_prompt import MemoryPromptPayload

    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    payload = MemoryPromptPayload(
        text="## 核心记忆 [user]\nUser prefers dark mode.",
        mode="vector",
        token_count=10,
        diagnostic="",
    )

    messages = builder.build_messages(
        history=[],
        current_message="hello",
        channel="cli",
        chat_id="direct",
        memory_prompt=payload,
    )

    # Memory must NOT be in system prompt
    system_content = messages[0]["content"]
    assert "User prefers dark mode" not in system_content
    assert START_MARK not in system_content
    assert END_MARK not in system_content

    # Memory MUST be in the last user message
    user_content = messages[-1]["content"]
    assert "User prefers dark mode" in user_content
    assert "miniunicorn-memory" in user_content
```

**修改 `test_system_prompt_reflects_current_dream_memory_contract`**（约第 51-60 行）：该测试断言 system prompt 包含 `"memory/history.jsonl"` 和 `"automatically managed by Dream"`。这些来自 `tool_contract.md` 模板，不在 memory_prompt 里，仍然成立。无需修改。

**新增测试**：验证工具迭代期间 system prompt 不变：

```python
def test_system_prompt_identical_across_tool_iterations(tmp_path) -> None:
    """System prompt must be byte-identical across multiple build_messages calls
    when only the user message changes (simulating tool iterations)."""
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    from miniunicorn.agent.memory_prompt import MemoryPromptPayload

    payload1 = MemoryPromptPayload(
        text="## 核心记忆\nMemory A", mode="vector", token_count=5, diagnostic=""
    )
    payload2 = MemoryPromptPayload(
        text="## 核心记忆\nMemory B", mode="vector", token_count=5, diagnostic=""
    )

    msgs1 = builder.build_messages(
        history=[], current_message="turn 1", channel="cli",
        chat_id="direct", memory_prompt=payload1,
    )
    msgs2 = builder.build_messages(
        history=[{"role": "assistant", "content": "ok"}],
        current_message="turn 2", channel="cli",
        chat_id="direct", memory_prompt=payload2,
    )

    # System prompt must be identical regardless of memory_prompt content
    assert msgs1[0]["content"] == msgs2[0]["content"]
```

### 验证命令

```powershell
cd d:\MyProject\MiniUnicorn
python -m pytest tests/agent/test_context_prompt_cache.py -v
python -m pytest tests/agent/test_context_builder.py -v
python -m pytest tests/agent/test_memory_prompt.py -v
python -m ruff check miniunicorn/agent/context.py miniunicorn/agent/loop.py miniunicorn/agent/memory_prompt.py
```

---

## Phase 2：System prompt session 级缓存

### 问题

`build_messages` 每轮都调用 `build_system_prompt`，重新拼接字符串。虽然 bootstrap 文件有 mtime 缓存，但 `read_unprocessed_history` 每次都读 `history.jsonl`，新条目会改变 Recent History 部分，导致 system prompt 变化。

### 改动清单

#### 2.1 `miniunicorn/agent/context.py` — Recent History 移出 system prompt

**位置**：`build_system_prompt` 方法，约第 198-205 行

**删除以下代码**：

```python
# 删除这整段（约第 198-205 行）：
entries = self.memory.read_unprocessed_history(
    since_cursor=self.memory.get_last_dream_cursor()
)
if entries:
    capped = entries[-self._MAX_RECENT_HISTORY :]
    history_text = "\n".join(f"- [{e['timestamp']}] {e['content']}" for e in capped)
    history_text = truncate_text(history_text, self._MAX_HISTORY_CHARS)
    parts.append((self._PRIORITY_HISTORY, "# Recent History\n\n" + history_text))
```

**在 `build_messages` 中将 Recent History 拼到 user 消息尾部**（和 memory_tail 类似）：

```python
# build_messages 中，在 memory_tail 之后追加：
recent_history_tail = ""
if not light_context:
    entries = self.memory.read_unprocessed_history(
        since_cursor=self.memory.get_last_dream_cursor()
    )
    if entries:
        capped = entries[-self._MAX_RECENT_HISTORY:]
        history_text = "\n".join(
            f"- [{e['timestamp']}] {e['content']}" for e in capped
        )
        history_text = truncate_text(history_text, self._MAX_HISTORY_CHARS)
        recent_history_tail = f"\n\n<!-- miniunicorn-recent-history -->\n# Recent History\n\n{history_text}"

# 合并到 merged：
full_tail = memory_tail + recent_history_tail
if isinstance(user_content, str):
    merged = f"{user_content}\n\n{runtime_ctx}{full_tail}"
else:
    merged = user_content + [{"type": "text", "text": runtime_ctx + full_tail}]
```

#### 2.2 `miniunicorn/agent/loop.py` — AgentLoop 缓存 system prompt

**位置**：`AgentLoop.__init__` 或类属性中新增缓存字段

```python
# AgentLoop 类中新增：
self._cached_system_prompt: str | None = None
self._system_prompt_key: tuple | None = None
```

**新增方法**：

```python
def _get_system_prompt(
    self,
    skill_names: list[str] | None = None,
    channel: str | None = None,
    session_summary: str | None = None,
    workspace: Path | None = None,
    agent_override: SubagentDefinition | None = None,
    light_context: bool = False,
) -> str:
    """Return cached system prompt, rebuilding only when key inputs change."""
    key = (
        channel or "",
        str(workspace or self.workspace),
        session_summary or "",
        agent_override.name if agent_override else None,
        light_context,
    )
    if self._system_prompt_key == key and self._cached_system_prompt is not None:
        return self._cached_system_prompt
    self._cached_system_prompt = self.context.build_system_prompt(
        skill_names=skill_names,
        channel=channel,
        session_summary=session_summary,
        workspace=workspace,
        agent_override=agent_override,
        light_context=light_context,
    )
    self._system_prompt_key = key
    return self._cached_system_prompt
```

**修改 `context.py` 的 `build_messages`**，让它接受可选的预构建 system prompt：

```python
# build_messages 签名新增参数：
def build_messages(
    self,
    history: list[dict[str, Any]],
    current_message: str,
    ...
    system_prompt_override: str | None = None,  # 新增
    ...
) -> list[dict[str, Any]]:

# 内部使用：
system_content = system_prompt_override or self.build_system_prompt(
    skill_names, channel=channel, ...
)
```

**修改 `loop.py` 的 `_build_initial_messages`**，传入缓存的 system prompt：

```python
# _build_initial_messages 中：
system_prompt = self._get_system_prompt(
    channel=msg.channel,
    session_summary=pending_summary,
    workspace=scope.project_path,
    agent_override=agent_override,
)
return self.context.build_messages(
    history=history,
    current_message=msg.content,
    ...
    system_prompt_override=system_prompt,
    ...
)
```

#### 2.3 测试更新

**新增测试**：验证 system prompt 在相同参数下被缓存：

```python
def test_system_prompt_cached_across_turns(tmp_path) -> None:
    """build_system_prompt should return identical content for same inputs."""
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    prompt1 = builder.build_system_prompt(channel="cli")
    prompt2 = builder.build_system_prompt(channel="cli")
    assert prompt1 == prompt2
```

### 验证命令

```powershell
cd d:\MyProject\MiniUnicorn
python -m pytest tests/agent/test_context_prompt_cache.py tests/agent/test_context_builder.py -v
python -m ruff check miniunicorn/agent/context.py miniunicorn/agent/loop.py
```

---

## Phase 3：reasoning_content 精细化回传

### 问题

`openai_compat_provider.py` 第 752-770 行给**所有** assistant 消息补 `reasoning_content = ""`，包括不带 tool_calls 的纯文本回复。这增加了不必要的字段，可能影响 DeepSeek 对历史消息的前缀匹配。

### 改动清单

#### 3.1 `miniunicorn/providers/openai_compat_provider.py` — 只给带 tool_calls 的消息回传

**位置**：`_build_kwargs` 方法，约第 752-770 行

**修改前**：

```python
if explicit_thinking or implicit_deepseek_thinking:
    for msg in kwargs["messages"]:
        if msg.get("role") == "assistant" and "reasoning_content" not in msg:
            msg["reasoning_content"] = ""
```

**修改后**：

```python
if explicit_thinking or implicit_deepseek_thinking:
    for msg in kwargs["messages"]:
        if (
            msg.get("role") == "assistant"
            and "reasoning_content" not in msg
            and msg.get("tool_calls")  # 只给带 tool_calls 的消息补
        ):
            msg["reasoning_content"] = ""
```

#### 3.2 测试更新

**文件**：`tests/providers/test_openai_api.py`（或相关 provider 测试）

**新增测试**：

```python
def test_reasoning_content_only_backfilled_on_tool_calls_messages() -> None:
    """reasoning_content should only be backfilled on assistant messages with tool_calls."""
    provider = OpenAICompatProvider(
        api_key="test",
        api_base="https://api.deepseek.com",
        default_model="deepseek-v4-reasoner",
    )
    messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there"},  # 无 tool_calls
        {"role": "user", "content": "Do something"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "type": "function", "function": {"name": "test", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "1", "content": "result"},
    ]
    kwargs = provider._build_kwargs(
        messages=messages, tools=[], model="deepseek-v4-reasoner",
        max_tokens=100, temperature=0.7, reasoning_effort=None, tool_choice=None,
    )
    result = kwargs["messages"]
    # 带 tool_calls 的 assistant 消息应有 reasoning_content
    tc_msg = next(m for m in result if m.get("tool_calls"))
    assert "reasoning_content" in tc_msg
    # 不带 tool_calls 的 assistant 消息不应有 reasoning_content
    plain_msg = next(m for m in result if m.get("role") == "assistant" and not m.get("tool_calls"))
    assert "reasoning_content" not in plain_msg
```

### 验证命令

```powershell
cd d:\MyProject\MiniUnicorn
python -m pytest tests/providers/test_openai_api.py -v -k reasoning
python -m ruff check miniunicorn/providers/openai_compat_provider.py
```

---

## Phase 4：压缩阈值分级

### 问题

`autocompact.py` 只有 TTL 触发（session 空闲超时），没有基于 token 用量的主动压缩阈值。长会话会一直增长直到撑满上下文窗口。

### 改动清单

#### 4.1 `miniunicorn/agent/loop.py` — 增加 token 阈值检查

**在 `AgentLoop` 类中新增常量和字段**：

```python
# 类级别常量：
_SOFT_COMPACT_RATIO = 0.50   # 50% 窗口：只通知，不压缩
_TRIGGER_COMPACT_RATIO = 0.80  # 80%：触发压缩
_FORCE_COMPACT_RATIO = 0.90   # 90%：强制压缩

# __init__ 中新增：
self._soft_compact_notified = False
```

**新增方法**：

```python
async def _maybe_compact_by_tokens(
    self,
    usage: dict[str, Any],
    session: Session,
    key: str,
) -> None:
    """Check token usage ratio and trigger compaction if needed.

    Three-tier threshold (inspired by Reasonix):
    - 50%: soft notify (log only, cache preserved)
    - 80%: trigger compaction (cache reset unavoidable)
    - 90%: force compaction
    """
    prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
    if prompt_tokens == 0 or self.context_window_tokens == 0:
        return

    ratio = prompt_tokens / self.context_window_tokens

    if ratio >= self._FORCE_COMPACT_RATIO:
        logger.warning(
            "Force compaction at {:.0%} context usage ({} / {})",
            ratio, prompt_tokens, self.context_window_tokens,
        )
        await self._do_token_compact(session, key, force=True)
    elif ratio >= self._TRIGGER_COMPACT_RATIO:
        logger.info(
            "Triggering compaction at {:.0%} context usage",
            ratio,
        )
        await self._do_token_compact(session, key, force=False)
    elif ratio >= self._SOFT_COMPACT_RATIO and not self._soft_compact_notified:
        self._soft_compact_notified = True
        logger.info(
            "Context at {:.0%} ({} tokens), approaching compaction threshold. "
            "Cache preserved — no compaction yet.",
            ratio, prompt_tokens,
        )

async def _do_token_compact(self, session: Session, key: str, *, force: bool) -> None:
    """Execute token-based compaction via Consolidator."""
    try:
        summary = await self.consolidator.compact_idle_session(
            key,
            AutoCompact._RECENT_SUFFIX_MESSAGES,
        )
        if summary and summary != "(nothing)":
            logger.info("Token compaction completed for session {}", key)
    except Exception:
        logger.exception("Token compaction failed for session {}", key)
```

**在 provider 返回后调用**（在 `_run_agent_loop` 或 `process_direct` 的 usage 回调中）：

```python
# 在收到 provider usage 后调用：
if usage and isinstance(usage, dict):
    await self._maybe_compact_by_tokens(usage, session, key)
```

#### 4.2 `miniunicorn/agent/autocompact.py` — 重置 soft notify 标记

**在 `prepare_session` 方法中，当 session 重新加载时重置标记**：

```python
# prepare_session 中，return 之前：
def prepare_session(self, session: Session, key: str) -> tuple[Session, str | None]:
    # ... 现有逻辑 ...
    # 重置 soft compact 通知标记（新 session 或 reload 后重新通知）
    # 注意：这个标记在 AgentLoop 上，不在 AutoCompact 上
    # 实际实现时由 AgentLoop 在 _build_initial_messages 前重置
    return session, summary
```

实际实现时，在 `AgentLoop._build_initial_messages` 开头重置：

```python
async def _build_initial_messages(self, ...):
    self._soft_compact_notified = False  # 新 turn 重置
    # ...
```

#### 4.3 测试更新

**文件**：`tests/agent/test_auto_compact.py`

**新增测试**：

```python
async def test_soft_compact_does_not_trigger_at_50pct(...):
    """At 50% context usage, should only log, not compact."""

async def test_trigger_compact_at_80pct(...):
    """At 80% context usage, should trigger compaction."""

async def test_force_compact_at_90pct(...):
    """At 90% context usage, should force compaction."""
```

### 验证命令

```powershell
cd d:\MyProject\MiniUnicorn
python -m pytest tests/agent/test_auto_compact.py -v
python -m ruff check miniunicorn/agent/loop.py miniunicorn/agent/autocompact.py
```

---

## Phase 5：缓存命中率监控与展示

### 问题

`_extract_usage` 已能提取 DeepSeek 的 `prompt_cache_hit_tokens`，但没有做命中率计算、前缀溯源和展示。

### 改动清单

#### 5.1 新建 `miniunicorn/agent/cache_shape.py` — 前缀哈希溯源

```python
"""Prefix shape tracking for cache hit diagnosis."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


@dataclass(frozen=True)
class PrefixShape:
    """SHA256 fingerprint of the cache-stable prefix components."""
    system_hash: str = ""
    tools_hash: str = ""
    log_rewrite_version: int = 0

    @staticmethod
    def hash(text: str) -> str:
        """Return first 16 hex chars of SHA256(text)."""
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def from_prompt(system_prompt: str, tools_json: str, log_version: int = 0) -> "PrefixShape":
        return PrefixShape(
            system_hash=PrefixShape.hash(system_prompt),
            tools_hash=PrefixShape.hash(tools_json),
            log_rewrite_version=log_version,
        )


@dataclass
class CacheDiagnostics:
    """Result of comparing two PrefixShapes."""
    prefix_changed: bool = False
    prefix_change_reasons: list[str] = field(default_factory=list)

    @staticmethod
    def compare(prev: PrefixShape | None, cur: PrefixShape) -> "CacheDiagnostics":
        if prev is None or not prev.system_hash:
            return CacheDiagnostics(prefix_changed=False)
        reasons: list[str] = []
        if prev.system_hash != cur.system_hash:
            reasons.append("system")
        if prev.tools_hash != cur.tools_hash:
            reasons.append("tools")
        if prev.log_rewrite_version != cur.log_rewrite_version:
            reasons.append("log_rewrite")
        return CacheDiagnostics(
            prefix_changed=bool(reasons),
            prefix_change_reasons=reasons,
        )
```

#### 5.2 `miniunicorn/agent/loop.py` — 会话级累计计数器

**在 `AgentLoop.__init__` 中新增**：

```python
# 会话级缓存累计计数器（压缩时不重置）
self._sess_cache_hit: int = 0
self._sess_cache_miss: int = 0
self._prev_prefix_shape: PrefixShape | None = None
```

**新增方法**：

```python
def _accumulate_cache_usage(self, usage: dict[str, Any]) -> None:
    """Accumulate cache hit/miss tokens for session-level ratio."""
    cached = int(usage.get("cached_tokens", 0) or 0)
    prompt = int(usage.get("prompt_tokens", 0) or 0)
    # DeepSeek 可能直接返回 prompt_cache_hit_tokens / prompt_cache_miss_tokens
    if cached == 0:
        cached = int(usage.get("prompt_cache_hit_tokens", 0) or 0)
    miss = int(usage.get("prompt_cache_miss_tokens", 0) or 0)
    if cached == 0 and miss == 0 and prompt > 0:
        miss = prompt  # 无缓存信息时全部算 miss
    self._sess_cache_hit += cached
    self._sess_cache_miss += miss

def _session_cache_ratio(self) -> int | None:
    """Return session-level cache hit ratio as percentage, or None."""
    total = self._sess_cache_hit + self._sess_cache_miss
    if total == 0:
        return None
    return self._sess_cache_hit * 100 // total

def _turn_cache_ratio(self, usage: dict[str, Any]) -> int | None:
    """Return single-turn cache hit ratio as percentage, or None."""
    cached = int(usage.get("cached_tokens", 0) or 0)
    prompt = int(usage.get("prompt_tokens", 0) or 0)
    if cached == 0:
        cached = int(usage.get("prompt_cache_hit_tokens", 0) or 0)
    if prompt == 0:
        miss = int(usage.get("prompt_cache_miss_tokens", 0) or 0)
        prompt = cached + miss
    if prompt == 0:
        return None
    return cached * 100 // prompt
```

**在 provider usage 回调中调用**（在 `progress_hook.py` 或 `loop.py` 的 usage 处理处）：

```python
# 收到 usage 后：
self._accumulate_cache_usage(usage)
turn_ratio = self._turn_cache_ratio(usage)
sess_ratio = self._session_cache_ratio()
if turn_ratio is not None:
    logger.info(
        "cache: turn {}% | session {}% (hit={} miss={})",
        turn_ratio, sess_ratio or 0,
        self._sess_cache_hit, self._sess_cache_miss,
    )
```

#### 5.3 `miniunicorn/agent/progress_hook.py` — 展示绝对计数

**位置**：约第 169-175 行

**修改前**：

```python
u = context.usage or {}
logger.debug(
    "LLM usage: prompt={} completion={} cached={}",
    u.get("prompt_tokens", 0),
    u.get("completion_tokens", 0),
    u.get("cached_tokens", 0),
)
```

**修改后**：

```python
u = context.usage or {}
prompt = u.get("prompt_tokens", 0)
cached = u.get("cached_tokens", 0)
# 也尝试 DeepSeek 原生字段
if not cached:
    cached = u.get("prompt_cache_hit_tokens", 0)
fresh = max(0, prompt - cached) if prompt else 0
logger.info(
    "LLM usage: {} tok | in {} ({} cached / {} new) | out {} | cache {}%",
    u.get("total_tokens", 0),
    prompt,
    cached,
    fresh,
    u.get("completion_tokens", 0),
    (cached * 100 // prompt) if prompt else 0,
)
```

#### 5.4 `miniunicorn/utils/helpers.py` — 状态栏展示缓存率

**位置**：`build_status_text` 函数，约第 536-549 行

**修改前**：

```python
cached = last_usage.get("cached_tokens", 0)
# ...
token_line = f"\U0001f4ca Tokens: {last_in} in / {last_out} out"
if cached and last_in:
    token_line += f" ({cached * 100 // last_in}% cached)"
```

**修改后**：

```python
cached = last_usage.get("cached_tokens", 0)
if not cached:
    cached = last_usage.get("prompt_cache_hit_tokens", 0)
fresh = max(0, last_in - cached) if last_in else 0
# ...
token_line = f"\U0001f4ca Tokens: {last_in} in / {last_out} out"
if cached and last_in:
    token_line += f" ({cached} cached / {fresh} new)"
```

#### 5.5 测试更新

**新建**：`tests/agent/test_cache_shape.py`

```python
def test_prefix_shape_stable_for_same_input():
    s1 = PrefixShape.from_prompt("hello", "[tool1]", 0)
    s2 = PrefixShape.from_prompt("hello", "[tool1]", 0)
    assert s1 == s2

def test_prefix_shape_detects_system_change():
    prev = PrefixShape.from_prompt("hello", "[tool1]", 0)
    cur = PrefixShape.from_prompt("hello world", "[tool1]", 0)
    diag = CacheDiagnostics.compare(prev, cur)
    assert diag.prefix_changed
    assert "system" in diag.prefix_change_reasons

def test_prefix_shape_detects_tools_change():
    prev = PrefixShape.from_prompt("hello", "[tool1]", 0)
    cur = PrefixShape.from_prompt("hello", "[tool2]", 0)
    diag = CacheDiagnostics.compare(prev, cur)
    assert diag.prefix_changed
    assert "tools" in diag.prefix_change_reasons

def test_prefix_shape_no_change():
    prev = PrefixShape.from_prompt("hello", "[tool1]", 0)
    cur = PrefixShape.from_prompt("hello", "[tool1]", 0)
    diag = CacheDiagnostics.compare(prev, cur)
    assert not diag.prefix_changed
```

### 验证命令

```powershell
cd d:\MyProject\MiniUnicorn
python -m pytest tests/agent/test_cache_shape.py tests/agent/test_progress_hook.py -v
python -m ruff check miniunicorn/agent/cache_shape.py miniunicorn/agent/progress_hook.py miniunicorn/utils/helpers.py
```

---

## Phase 6：cache_miss 字段补全

### 问题

`_extract_usage` 只提取了 `cached_tokens`（hit），没有提取 `prompt_cache_miss_tokens`。`turn_budget.py` 虽然读了 miss，但只在 `prompt == 0` 时才用，逻辑不完整。

### 改动清单

#### 6.1 `miniunicorn/providers/openai_compat_provider.py` — 提取 miss 字段

**位置**：`_extract_usage` 方法，约第 988-1003 行

**在 `cached_tokens` 提取逻辑之后，追加 miss 提取**：

```python
# 在 result["cached_tokens"] = cached 之后追加：

# --- cache_miss_tokens (DeepSeek-specific) ---
for miss_path in (
    ("prompt_cache_miss_tokens",),  # DeepSeek/SiliconFlow
):
    miss = cls._get_nested_int(usage_map, miss_path)
    if not miss and usage_obj:
        miss = cls._get_nested_int(usage_obj, miss_path)
    if miss:
        result["cache_miss_tokens"] = miss
        break

# 如果 provider 没返回 miss 但有 cached，用 prompt - cached 计算
if "cached_tokens" in result and "cache_miss_tokens" not in result:
    result["cache_miss_tokens"] = max(
        0, result["prompt_tokens"] - result["cached_tokens"]
    )
```

#### 6.2 `miniunicorn/agent/turn_budget.py` — 修正 miss 累计逻辑

**位置**：`accumulate` 方法，约第 56-64 行

**修改前**：

```python
prompt = int(usage.get("prompt_tokens", 0) or 0)
completion = int(usage.get("completion_tokens", 0) or 0)
cache_hit = int(usage.get("prompt_cache_hit_tokens", 0) or 0)
if cache_hit > 0 and prompt == 0:
    cache_miss = int(usage.get("prompt_cache_miss_tokens", 0) or 0)
    prompt = cache_miss
self.used_input += prompt
```

**修改后**：

```python
prompt = int(usage.get("prompt_tokens", 0) or 0)
completion = int(usage.get("completion_tokens", 0) or 0)
# 优先用 provider 返回的 cache 字段
cache_hit = int(usage.get("prompt_cache_hit_tokens", 0) or 0)
if not cache_hit:
    cache_hit = int(usage.get("cached_tokens", 0) or 0)
cache_miss = int(usage.get("prompt_cache_miss_tokens", 0) or 0)
if not cache_miss and "cache_miss_tokens" in usage:
    cache_miss = int(usage.get("cache_miss_tokens", 0) or 0)

if cache_hit > 0 and prompt == 0:
    # DeepSeek 有时 prompt_tokens=0，用 miss 作为 input
    prompt = cache_miss
elif cache_hit > 0 and prompt > 0:
    # 有 cached 信息时，只计 miss 部分（hit 近似免费）
    prompt = cache_miss if cache_miss > 0 else max(0, prompt - cache_hit)

self.used_input += prompt
```

#### 6.3 测试更新

**文件**：`tests/providers/test_openai_api.py`

**新增测试**：

```python
def test_extract_usage_deepseek_cache_fields():
    """DeepSeek response with prompt_cache_hit/miss_tokens should be normalized."""
    response = {
        "usage": {
            "prompt_tokens": 10000,
            "completion_tokens": 500,
            "total_tokens": 10500,
            "prompt_cache_hit_tokens": 9000,
            "prompt_cache_miss_tokens": 1000,
        }
    }
    usage = OpenAICompatProvider._extract_usage(response)
    assert usage["cached_tokens"] == 9000
    assert usage["cache_miss_tokens"] == 1000
    assert usage["prompt_tokens"] == 10000

def test_extract_usage_computes_miss_when_absent():
    """When provider returns cached but not miss, compute miss = prompt - cached."""
    response = {
        "usage": {
            "prompt_tokens": 5000,
            "completion_tokens": 200,
            "total_tokens": 5200,
            "prompt_tokens_details": {"cached_tokens": 4000},
        }
    }
    usage = OpenAICompatProvider._extract_usage(response)
    assert usage["cached_tokens"] == 4000
    assert usage["cache_miss_tokens"] == 1000
```

### 验证命令

```powershell
cd d:\MyProject\MiniUnicorn
python -m pytest tests/providers/test_openai_api.py -v -k cache
python -m pytest tests/agent/test_runner_core.py -v -k budget
python -m ruff check miniunicorn/providers/openai_compat_provider.py miniunicorn/agent/turn_budget.py
```

---

## 全局验证

所有阶段完成后，执行完整验证：

```powershell
cd d:\MyProject\MiniUnicorn

# 1. 全量测试
python -m pytest tests/ -v --timeout=60

# 2. Lint
python -m ruff check miniunicorn/

# 3. 类型检查（如果项目使用 mypy）
python -m mypy miniunicorn/agent/context.py miniunicorn/agent/loop.py miniunicorn/providers/openai_compat_provider.py

# 4. 端到端验证：启动 gateway，用 DeepSeek 模型进行多轮对话，观察日志中的缓存命中率
python -m miniunicorn gateway
# 在另一个终端运行 reasonix 或直接用 API 测试多轮对话
```

---

## 预期效果

| 指标 | 改造前（估计） | Phase 1 后 | Phase 1-2 后 | Phase 1-6 后 |
|------|----------------|------------|--------------|--------------|
| 短会话命中率 | 20%-40% | 50%-70% | 60%-80% | 70%-85% |
| 长会话命中率 | 10%-30% | 60%-80% | 75%-90% | 85%-95% |
| 多工具迭代命中率 | 近 0% | 70%-90% | 70%-90% | 75%-92% |
| 可观测性 | 仅 cached_tokens | 同前 | 同前 | 前缀溯源+会话级命中率 |

> **注意**：Phase 1 是收益最大的一项，单独完成后即可显著提升命中率。后续阶段是增量优化。
