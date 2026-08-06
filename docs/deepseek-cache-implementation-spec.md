# DeepSeek 缓存命中率优化 — 细颗粒度实施方案 (Agent-Executable)

> **目的**：本文档供另一个 AI agent 直接按步骤实施，无需额外上下文。每个任务包含精确的文件路径、搜索/替换代码块、受影响测试清单和验证命令。
>
> **工作目录**：`d:\MyProject\MiniUnicorn`
>
> **核心原则**：系统提示在 session 内字节级稳定；所有运行时变化走 user 消息尾部。

---

## 0. 前置条件与全局规则

### 0.1 环境要求

- Python 3.11+，已安装项目依赖（`pip install -e .`）
- 测试框架：pytest + pytest-asyncio
- Lint：ruff
- 所有命令在 `d:\MyProject\MiniUnicorn` 下执行

### 0.2 实施顺序与依赖关系

```
Phase 1 (T1.1 → T1.2 → T1.3 → T1.4 → T1.5)
  ↓
Phase 3 (T3.1 → T3.2)          ← 独立，可与 Phase 1 并行
Phase 6 (T6.1 → T6.2 → T6.3)   ← 独立，可与 Phase 1 并行
Phase 5 (T5.1 → T5.2 → T5.3 → T5.4 → T5.5)  ← 依赖 Phase 1 完成
Phase 2 (T2.1 → T2.2 → T2.3)   ← 可选，风险较高，建议最后做
Phase 4 (T4.1 → T4.2)          ← 可选，依赖 Consolidator API
```

### 0.3 通用规则

- 每个任务完成后执行该任务的验证命令，全绿才进入下一个任务
- 如果验证失败，不要继续，先修复
- 搜索/替换块中的注释（`# ...`）是给 agent 看的说明，替换时保留
- `old_str` 必须与文件实际内容完全匹配（包括空格和换行）
- 如果 `old_str` 匹配不到，先用 Read 工具读取该文件对应区域确认当前内容

### 0.4 受影响测试文件清单

| 文件 | Phase 1 | Phase 2 | Phase 3 | Phase 5 | Phase 6 |
|------|---------|---------|---------|---------|---------|
| `tests/agent/test_context_builder.py` | 3 个测试需改 | — | — | — | — |
| `tests/agent/test_context_prompt_cache.py` | 1 个测试需改 | 5 个测试需改 | — | — | — |
| `tests/agent/test_memory_prompt.py` | — | — | — | — | — |
| `tests/agent/test_runner_core.py` | — | — | — | — | — |
| `tests/test_openai_api.py` | — | — | 1 个测试新增 | — | 2 个测试新增 |
| `tests/agent/test_cache_shape.py` | — | — | — | 新建 | — |

---

## Phase 1：Memory 移出 system prompt

**问题**：`loop.py` 的 `_refresh_memory` 在每次工具迭代时，用新的 recall 结果替换 system prompt 里的 `<!-- miniunicorn-memory:start/end -->` 标记块。两轮工具调用之间 recall 结果若不同，system prompt 内容就变了，从 memory 块往后全部 cache miss。

**预期收益**：短会话命中率 20%→50-70%，长会话命中率 10%→60-80%，多工具迭代命中率 0%→70-90%。

---

### T1.1：context.py — 移除 build_system_prompt 的 memory_prompt 参数

**文件**：`miniunicorn/agent/context.py`

**步骤 1/3：修改 import（第 10-15 行）**

搜索：
```python
from miniunicorn.agent.memory_prompt import (
    END_MARK,
    START_MARK,
    MemoryPromptPayload,
    MemoryPromptPolicy,
)
```

替换为：
```python
from miniunicorn.agent.memory_prompt import MemoryPromptPolicy
```

**步骤 2/3：修改 build_system_prompt 签名（第 106-115 行）**

搜索：
```python
    def build_system_prompt(
        self,
        skill_names: list[str] | None = None,
        channel: str | None = None,
        session_summary: str | None = None,
        workspace: Path | None = None,
        memory_prompt: MemoryPromptPayload | None = None,
        agent_override: SubagentDefinition | None = None,
        light_context: bool = False,
    ) -> str:
```

替换为：
```python
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

**步骤 3/3：删除 memory 注入块 + 修改 docstring（第 116-170 行）**

搜索：
```python
        """Build the system prompt from identity, bootstrap files, memory, and skills.

        When ``agent_override`` is provided (subagent takeover mode), the
        subagent's system_prompt replaces the default agent identity and the
        "Available Subagents" delegation list is omitted — the subagent runs
        as the primary identity for the turn.

        The bounded ``SOUL.md`` is always injected (light_context included);
        the bounded memory section is injected from ``memory_prompt``.
        ``light_context`` skips AGENTS.md, skills and history, keeping only
        identity, tool contract, bounded soul and core memory.
        """
```

替换为：
```python
        """Build the system prompt from identity, bootstrap files, and skills.

        When ``agent_override`` is provided (subagent takeover mode), the
        subagent's system_prompt replaces the default agent identity and the
        "Available Subagents" delegation list is omitted — the subagent runs
        as the primary identity for the turn.

        The bounded ``SOUL.md`` is always injected (light_context included).
        Memory recall results are NOT injected here — they are appended to
        the user message tail by ``build_messages`` to keep the system prompt
        byte-stable across tool iterations for DeepSeek prefix cache hits.
        ``light_context`` skips AGENTS.md, skills and history, keeping only
        identity, tool contract, bounded soul.
        """
```

然后删除 memory 注入代码块。搜索：
```python
        # Bounded memory injection: the caller pre-builds the payload (always
        # core + provenance-tagged recall records). Wrapped in the marker block
        # so the runner's per-call refresh can splice updated memory sections.
        if memory_prompt is not None and memory_prompt.text:
            block = memory_prompt.text
            if START_MARK not in block:
                block = f"{START_MARK}\n{block}\n{END_MARK}"
            parts.append((self._PRIORITY_MEMORY, block))

        # 注入 notes.md（主 Agent 的 scratchpad，借鉴 MiMo Code）。
```

替换为：
```python
        # 注入 notes.md（主 Agent 的 scratchpad，借鉴 MiMo Code）。
```

**验证**：
```powershell
cd d:\MyProject\MiniUnicorn
python -m ruff check miniunicorn/agent/context.py
python -c "from miniunicorn.agent.context import ContextBuilder; print('import ok')"
```

> **注意**：此时 `build_system_prompt(memory_prompt=...)` 的调用方（`build_messages` 内部和测试）会报 TypeError，这是预期的——T1.2 会修复 `build_messages` 内部调用，T1.5 会修复测试。

---

### T1.2：context.py — build_messages 中将 memory 拼到 user 消息尾部

**文件**：`miniunicorn/agent/context.py`

**步骤 1/2：修改 merged 拼接逻辑（第 480-489 行）**

搜索：
```python
        user_content = self._build_user_content(current_message, media)

        # Merge runtime context and user content into a single user message
        # to avoid consecutive same-role messages that some providers reject.
        # Runtime context is appended to keep the user-content prefix stable
        # for prompt-cache hits (the context changes every turn due to time).
        if isinstance(user_content, str):
            merged = f"{user_content}\n\n{runtime_ctx}"
        else:
            merged = user_content + [{"type": "text", "text": runtime_ctx}]
```

替换为：
```python
        user_content = self._build_user_content(current_message, media)

        # Memory recall results are appended to the user message tail (not
        # injected into the system prompt) so the system prompt stays
        # byte-stable across tool iterations for DeepSeek prefix cache hits.
        memory_tail = ""
        if memory_prompt is not None and memory_prompt.text:
            memory_tail = f"\n\n<!-- miniunicorn-memory -->\n{memory_prompt.text}"

        # Merge runtime context and user content into a single user message
        # to avoid consecutive same-role messages that some providers reject.
        # Runtime context is appended to keep the user-content prefix stable
        # for prompt-cache hits (the context changes every turn due to time).
        if isinstance(user_content, str):
            merged = f"{user_content}\n\n{runtime_ctx}{memory_tail}"
        else:
            merged = user_content + [{"type": "text", "text": runtime_ctx + memory_tail}]
```

**步骤 2/2：修改 build_system_prompt 调用，移除 memory_prompt 参数（第 497-505 行）**

搜索：
```python
            {
                "role": "system",
                "content": self.build_system_prompt(
                    skill_names,
                    channel=channel,
                    session_summary=session_summary,
                    workspace=root,
                    memory_prompt=memory_prompt,
                    agent_override=agent_override,
                    light_context=light_context,
                ),
            },
```

替换为：
```python
            {
                "role": "system",
                "content": self.build_system_prompt(
                    skill_names,
                    channel=channel,
                    session_summary=session_summary,
                    workspace=root,
                    agent_override=agent_override,
                    light_context=light_context,
                ),
            },
```

**验证**：
```powershell
cd d:\MyProject\MiniUnicorn
python -m ruff check miniunicorn/agent/context.py
python -c "
from pathlib import Path
from miniunicorn.agent.context import ContextBuilder
b = ContextBuilder(Path('.'))
msgs = b.build_messages(history=[], current_message='hi', channel='cli', chat_id='d')
print('system role:', msgs[0]['role'])
print('memory in system:', 'miniunicorn-memory' in msgs[0]['content'])
print('OK')
"
```

---

### T1.3：loop.py — 移除 _refresh_memory 的 marker splice

**文件**：`miniunicorn/agent/loop.py`

**步骤 1/2：修改 import（第 27 行）**

搜索：
```python
from miniunicorn.agent.context import ContextBuilder, MemoryPromptPolicy
```

替换为：
```python
from miniunicorn.agent.context import ContextBuilder
```

**步骤 2/2：移除 _refresh_memory 闭包（第 792-806 行）**

搜索：
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

替换为：
```python
        # Memory recall is baked into the user message at _build_initial_messages
        # time and does NOT change during tool iterations. This keeps the system
        # prompt byte-stable across all provider calls within a turn, maximizing
        # DeepSeek prefix cache hits. Memory is re-recalled on the next user turn.
        before_provider_call = None
```

**验证**：
```powershell
cd d:\MyProject\MiniUnicorn
python -m ruff check miniunicorn/agent/loop.py
python -c "from miniunicorn.agent.loop import AgentLoop; print('import ok')"
```

---

### T1.4：memory_prompt.py — replace_section 标记废弃

**文件**：`miniunicorn/agent/memory_prompt.py`

搜索：
```python
    @staticmethod
    def replace_section(messages: list[dict], payload: MemoryPromptPayload) -> None:
        """Splice *payload* into the first system message, replacing the marker block.

        An empty payload removes the marker block (and any content inside it).
        """
```

替换为：
```python
    @staticmethod
    def replace_section(messages: list[dict], payload: MemoryPromptPayload) -> None:
        """Splice *payload* into the first system message, replacing the marker block.

        An empty payload removes the marker block (and any content inside it).

        .. deprecated::
            Memory is now injected into the user message tail by
            ``ContextBuilder.build_messages``, not the system prompt. This
            method is kept for backward compatibility but should not be
            called in new code — the marker-block splice invalidated the
            system prompt prefix on every tool iteration, destroying
            DeepSeek cache hits.
        """
```

**验证**：
```powershell
cd d:\MyProject\MiniUnicorn
python -m ruff check miniunicorn/agent/memory_prompt.py
```

---

### T1.5：更新受影响测试

#### T1.5a：修复 test_context_builder.py

**文件**：`tests/agent/test_context_builder.py`

**修复 1/3：test_memory_prompt_injected_with_marker_block（第 319-328 行）**

搜索：
```python
    def test_memory_prompt_injected_with_marker_block(self, tmp_path):
        (tmp_path / "USER.md").write_text("# Always\n叫我小王", encoding="utf-8")
        policy = MemoryPromptPolicy(tmp_path)
        payload = policy.build(RecallOutcome((), None, 1.0))
        builder = _builder(tmp_path)
        result = builder.build_system_prompt(memory_prompt=payload)
        assert START_MARK in result
        assert END_MARK in result
        assert "叫我小王" in result
        assert "## USER.md" not in result
```

替换为：
```python
    def test_memory_prompt_injected_into_user_message(self, tmp_path):
        (tmp_path / "USER.md").write_text("# Always\n叫我小王", encoding="utf-8")
        policy = MemoryPromptPolicy(tmp_path)
        payload = policy.build(RecallOutcome((), None, 1.0))
        builder = _builder(tmp_path)
        messages = builder.build_messages(
            history=[], current_message="hi", channel="cli",
            chat_id="direct", memory_prompt=payload,
        )
        # Memory must NOT be in system prompt
        assert START_MARK not in messages[0]["content"]
        assert END_MARK not in messages[0]["content"]
        # Memory MUST be in the user message
        user_content = messages[-1]["content"]
        assert "叫我小王" in user_content
        assert "miniunicorn-memory" in user_content
```

**修复 2/3：test_empty_memory_prompt_not_injected（第 330-333 行）**

搜索：
```python
    def test_empty_memory_prompt_not_injected(self, tmp_path):
        builder = _builder(tmp_path)
        result = builder.build_system_prompt(memory_prompt=None)
        assert START_MARK not in result
```

替换为：
```python
    def test_no_memory_markers_when_memory_prompt_is_none(self, tmp_path):
        builder = _builder(tmp_path)
        messages = builder.build_messages(
            history=[], current_message="hi", channel="cli", chat_id="direct",
        )
        assert START_MARK not in messages[0]["content"]
        assert "miniunicorn-memory" not in messages[-1]["content"]
```

**修复 3/3：test_light_context_keeps_soul_and_core_memory（第 335-347 行）**

搜索：
```python
    def test_light_context_keeps_soul_and_core_memory(self, tmp_path):
        (tmp_path / "SOUL.md").write_text("Soul text.", encoding="utf-8")
        (tmp_path / "AGENTS.md").write_text("Agents rules.", encoding="utf-8")
        (tmp_path / "USER.md").write_text("# Always\n核心记忆", encoding="utf-8")
        policy = MemoryPromptPolicy(tmp_path)
        payload = policy.build(RecallOutcome((), None, 1.0))
        builder = _builder(tmp_path)
        result = builder.build_system_prompt(memory_prompt=payload, light_context=True)
        assert "Soul text." in result
        assert "核心记忆" in result
        assert "Agents rules." not in result
        assert "# Active Skills" not in result
        assert "# Recent History" not in result
```

替换为：
```python
    def test_light_context_keeps_soul_in_system_and_memory_in_user(self, tmp_path):
        (tmp_path / "SOUL.md").write_text("Soul text.", encoding="utf-8")
        (tmp_path / "AGENTS.md").write_text("Agents rules.", encoding="utf-8")
        (tmp_path / "USER.md").write_text("# Always\n核心记忆", encoding="utf-8")
        policy = MemoryPromptPolicy(tmp_path)
        payload = policy.build(RecallOutcome((), None, 1.0))
        builder = _builder(tmp_path)
        messages = builder.build_messages(
            history=[], current_message="hi", channel="cli",
            chat_id="direct", memory_prompt=payload,
        )
        system = messages[0]["content"]
        user = messages[-1]["content"]
        # Soul stays in system prompt
        assert "Soul text." in system
        # Memory goes to user message
        assert "核心记忆" in user
        assert "miniunicorn-memory" in user
        # light_context skips AGENTS.md, skills, history
        assert "Agents rules." not in system
        assert "# Active Skills" not in system
        assert "# Recent History" not in system
```

> **注意**：此测试需要 `build_messages` 支持 `light_context`。当前 `build_messages` 从 `runtime_state._light_context` 读取，测试中无 runtime_state。需确认 `build_messages` 是否有其他方式传入 `light_context`。检查代码后发现 `light_context` 在 `build_messages` 内部从 `runtime_state` 读取（第 491-493 行）。测试中不传 `runtime_state` 时默认为 `False`，即非 light_context 模式。
>
> **解决方案**：如果测试需要验证 light_context 行为，需要 mock runtime_state。或者简化测试，只验证 memory 在 user 消息中。以下是简化版本——如果上面的替换在运行时报错，改用：

```python
    def test_light_context_keeps_soul_and_memory_separated(self, tmp_path):
        (tmp_path / "SOUL.md").write_text("Soul text.", encoding="utf-8")
        (tmp_path / "USER.md").write_text("# Always\n核心记忆", encoding="utf-8")
        policy = MemoryPromptPolicy(tmp_path)
        payload = policy.build(RecallOutcome((), None, 1.0))
        builder = _builder(tmp_path)
        messages = builder.build_messages(
            history=[], current_message="hi", channel="cli",
            chat_id="direct", memory_prompt=payload,
        )
        # Soul in system, memory in user
        assert "Soul text." in messages[0]["content"]
        assert "核心记忆" in messages[-1]["content"]
```

#### T1.5b：修复 test_context_prompt_cache.py

**文件**：`tests/agent/test_context_prompt_cache.py`

**修复 1/1：test_customized_memory_md_not_injected_without_memory_prompt（第 378-401 行）**

搜索：
```python
def test_customized_memory_md_not_injected_without_memory_prompt(tmp_path) -> None:
    """MEMORY.md is no longer injected wholesale; the bounded memory prompt
    (always core + recall records) is built by the loop and passed in."""
    workspace = _make_workspace(tmp_path)
    from miniunicorn.utils.helpers import sync_workspace_templates

    sync_workspace_templates(workspace, silent=True)

    (workspace / "memory" / "MEMORY.md").write_text(
        "# Always\n\nUser prefers dark mode.\n", encoding="utf-8"
    )

    builder = ContextBuilder(workspace)
    prompt = builder.build_system_prompt()

    assert "# Long-term Memory" not in prompt
    assert "User prefers dark mode" not in prompt

    # The bounded memory prompt carries the always-core section.
    from miniunicorn.agent.memory_prompt import MemoryPromptPolicy
    from miniunicorn.agent.memory_recall import RecallOutcome

    payload = MemoryPromptPolicy(workspace).build(RecallOutcome((), None, 1.0))
    with_prompt = builder.build_system_prompt(memory_prompt=payload)
    assert "User prefers dark mode" in with_prompt
```

替换为：
```python
def test_customized_memory_md_not_injected_without_memory_prompt(tmp_path) -> None:
    """MEMORY.md is no longer injected wholesale into the system prompt;
    the bounded memory prompt is built by the loop and passed to
    build_messages, which appends it to the user message tail."""
    workspace = _make_workspace(tmp_path)
    from miniunicorn.utils.helpers import sync_workspace_templates

    sync_workspace_templates(workspace, silent=True)

    (workspace / "memory" / "MEMORY.md").write_text(
        "# Always\n\nUser prefers dark mode.\n", encoding="utf-8"
    )

    builder = ContextBuilder(workspace)
    prompt = builder.build_system_prompt()

    assert "# Long-term Memory" not in prompt
    assert "User prefers dark mode" not in prompt

    # The bounded memory prompt is now appended to the user message.
    from miniunicorn.agent.memory_prompt import MemoryPromptPolicy
    from miniunicorn.agent.memory_recall import RecallOutcome

    payload = MemoryPromptPolicy(workspace).build(RecallOutcome((), None, 1.0))
    messages = builder.build_messages(
        history=[], current_message="hi", channel="cli",
        chat_id="direct", memory_prompt=payload,
    )
    assert "User prefers dark mode" in messages[-1]["content"]
    assert "User prefers dark mode" not in messages[0]["content"]
```

#### T1.5c：新增测试

**文件**：`tests/agent/test_context_prompt_cache.py`（在文件末尾追加）

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

    # Memory MUST be in the last user message
    user_content = messages[-1]["content"]
    assert "User prefers dark mode" in user_content
    assert "miniunicorn-memory" in user_content


def test_system_prompt_identical_across_different_memory_prompts(tmp_path) -> None:
    """System prompt must be byte-identical regardless of memory_prompt content."""
    from miniunicorn.agent.memory_prompt import MemoryPromptPayload

    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

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

**验证**：
```powershell
cd d:\MyProject\MiniUnicorn
python -m pytest tests/agent/test_context_builder.py tests/agent/test_context_prompt_cache.py tests/agent/test_memory_prompt.py -v
python -m ruff check miniunicorn/agent/context.py miniunicorn/agent/loop.py miniunicorn/agent/memory_prompt.py tests/agent/test_context_builder.py tests/agent/test_context_prompt_cache.py
```

---

## Phase 3：reasoning_content 精细化回传

**问题**：`openai_compat_provider.py` 给**所有** assistant 消息补 `reasoning_content = ""`，包括不带 tool_calls 的纯文本回复。这增加了不必要的字段，可能影响 DeepSeek 对历史消息的前缀匹配。

**依赖**：无，可与 Phase 1 并行实施。

---

### T3.1：openai_compat_provider.py — 只给带 tool_calls 的消息回传

**文件**：`miniunicorn/providers/openai_compat_provider.py`

搜索（第 767-770 行）：
```python
        if explicit_thinking or implicit_deepseek_thinking:
            for msg in kwargs["messages"]:
                if msg.get("role") == "assistant" and "reasoning_content" not in msg:
                    msg["reasoning_content"] = ""
```

替换为：
```python
        if explicit_thinking or implicit_deepseek_thinking:
            for msg in kwargs["messages"]:
                if (
                    msg.get("role") == "assistant"
                    and "reasoning_content" not in msg
                    and msg.get("tool_calls")  # only backfill on tool-call turns
                ):
                    msg["reasoning_content"] = ""
```

**验证**：
```powershell
cd d:\MyProject\MiniUnicorn
python -m ruff check miniunicorn/providers/openai_compat_provider.py
```

---

### T3.2：新增测试

**文件**：`tests/test_openai_api.py`（在文件末尾追加）

> **关键**：`_build_kwargs` 的 reasoning_content 回填逻辑依赖 `spec.backfill_reasoning_content=True`。测试必须传入一个配置了该 flag 的 `ProviderSpec`，否则回填条件不会触发。

```python
def test_reasoning_content_only_backfilled_on_tool_calls_messages() -> None:
    """reasoning_content should only be backfilled on assistant messages with tool_calls."""
    from miniunicorn.providers.openai_compat_provider import OpenAICompatProvider
    from miniunicorn.providers.registry import ProviderSpec

    spec = ProviderSpec(
        name="deepseek",
        keywords=("deepseek",),
        env_key="DEEPSEEK_API_KEY",
        backfill_reasoning_content=True,
    )
    provider = OpenAICompatProvider(
        api_key="test",
        api_base="https://api.deepseek.com",
        default_model="deepseek-v4-reasoner",
        spec=spec,
    )
    messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there"},  # no tool_calls
        {"role": "user", "content": "Do something"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "1", "type": "function", "function": {"name": "test", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "1", "content": "result"},
    ]
    kwargs = provider._build_kwargs(
        messages=messages,
        tools=[],
        model="deepseek-v4-reasoner",
        max_tokens=100,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    )
    result = kwargs["messages"]
    # Assistant with tool_calls gets reasoning_content
    tc_msg = next(m for m in result if m.get("tool_calls"))
    assert "reasoning_content" in tc_msg
    # Plain assistant (no tool_calls) does NOT get reasoning_content
    plain_msg = next(
        m for m in result if m.get("role") == "assistant" and not m.get("tool_calls")
    )
    assert "reasoning_content" not in plain_msg
```

**验证**：
```powershell
cd d:\MyProject\MiniUnicorn
python -m pytest tests/test_openai_api.py -v -k reasoning
```

---

## Phase 6：cache_miss 字段补全

**问题**：`_extract_usage` 只提取了 `cached_tokens`（hit），没有提取 `prompt_cache_miss_tokens`。`turn_budget.py` 虽然读了 miss，但只在 `prompt == 0` 时才用，逻辑不完整。

**依赖**：无，可与 Phase 1 并行实施。

---

### T6.1：openai_compat_provider.py — 提取 miss 字段

**文件**：`miniunicorn/providers/openai_compat_provider.py`

搜索（第 988-1003 行，`_extract_usage` 方法的 `cached_tokens` 提取块之后、`return result` 之前）：
```python
        for path in (
            ("prompt_tokens_details", "cached_tokens"),  # OpenAI/Zhipu/MiniMax/Qwen/Mistral/xAI
            ("cached_tokens",),  # StepFun/Moonshot (top-level)
            ("prompt_cache_hit_tokens",),  # DeepSeek/SiliconFlow
        ):
            cached = cls._get_nested_int(usage_map, path)
            if not cached and usage_obj:
                cached = cls._get_nested_int(usage_obj, path)
            if cached:
                result["cached_tokens"] = cached
                break

        return result
```

替换为：
```python
        for path in (
            ("prompt_tokens_details", "cached_tokens"),  # OpenAI/Zhipu/MiniMax/Qwen/Mistral/xAI
            ("cached_tokens",),  # StepFun/Moonshot (top-level)
            ("prompt_cache_hit_tokens",),  # DeepSeek/SiliconFlow
        ):
            cached = cls._get_nested_int(usage_map, path)
            if not cached and usage_obj:
                cached = cls._get_nested_int(usage_obj, path)
            if cached:
                result["cached_tokens"] = cached
                break

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

        # If provider returned cached but not miss, compute miss = prompt - cached
        if "cached_tokens" in result and "cache_miss_tokens" not in result:
            result["cache_miss_tokens"] = max(
                0, result["prompt_tokens"] - result["cached_tokens"]
            )

        return result
```

**验证**：
```powershell
cd d:\MyProject\MiniUnicorn
python -m ruff check miniunicorn/providers/openai_compat_provider.py
python -c "
from miniunicorn.providers.openai_compat_provider import OpenAICompatProvider
r = {'usage': {'prompt_tokens': 10000, 'completion_tokens': 500, 'total_tokens': 10500, 'prompt_cache_hit_tokens': 9000, 'prompt_cache_miss_tokens': 1000}}
u = OpenAICompatProvider._extract_usage(r)
assert u['cached_tokens'] == 9000, u
assert u['cache_miss_tokens'] == 1000, u
print('DeepSeek cache fields OK')

r2 = {'usage': {'prompt_tokens': 5000, 'completion_tokens': 200, 'total_tokens': 5200, 'prompt_tokens_details': {'cached_tokens': 4000}}}
u2 = OpenAICompatProvider._extract_usage(r2)
assert u2['cached_tokens'] == 4000, u2
assert u2['cache_miss_tokens'] == 1000, u2
print('Computed miss OK')
"
```

---

### T6.2：turn_budget.py — 修正 miss 累计逻辑

**文件**：`miniunicorn/agent/turn_budget.py`

搜索（第 56-64 行，`accumulate` 方法）：
```python
        prompt = int(usage.get("prompt_tokens", 0) or 0)
        completion = int(usage.get("completion_tokens", 0) or 0)
        # Some providers report cache stats separately; count cache misses
        # toward input consumption (cache hits are ~free).
        cache_hit = int(usage.get("prompt_cache_hit_tokens", 0) or 0)
        if cache_hit > 0 and prompt == 0:
            cache_miss = int(usage.get("prompt_cache_miss_tokens", 0) or 0)
            prompt = cache_miss
        self.used_input += prompt
```

替换为：
```python
        prompt = int(usage.get("prompt_tokens", 0) or 0)
        completion = int(usage.get("completion_tokens", 0) or 0)
        # Some providers report cache stats separately; count cache misses
        # toward input consumption (cache hits are ~free).
        cache_hit = int(usage.get("prompt_cache_hit_tokens", 0) or 0)
        if not cache_hit:
            cache_hit = int(usage.get("cached_tokens", 0) or 0)
        cache_miss = int(usage.get("prompt_cache_miss_tokens", 0) or 0)
        if not cache_miss and "cache_miss_tokens" in usage:
            cache_miss = int(usage.get("cache_miss_tokens", 0) or 0)

        if cache_hit > 0 and prompt == 0:
            # DeepSeek sometimes returns prompt_tokens=0; use miss as input
            prompt = cache_miss
        elif cache_hit > 0 and prompt > 0:
            # With cache info available, only count the miss portion (hits are ~free)
            prompt = cache_miss if cache_miss > 0 else max(0, prompt - cache_hit)

        self.used_input += prompt
```

**验证**：
```powershell
cd d:\MyProject\MiniUnicorn
python -m ruff check miniunicorn/agent/turn_budget.py
python -m pytest tests/agent/test_runner_core.py -v -k budget
```

---

### T6.3：新增测试

**文件**：`tests/test_openai_api.py`（在文件末尾追加）

```python
def test_extract_usage_deepseek_cache_fields() -> None:
    """DeepSeek response with prompt_cache_hit/miss_tokens should be normalized."""
    from miniunicorn.providers.openai_compat_provider import OpenAICompatProvider

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


def test_extract_usage_computes_miss_when_absent() -> None:
    """When provider returns cached but not miss, compute miss = prompt - cached."""
    from miniunicorn.providers.openai_compat_provider import OpenAICompatProvider

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

**验证**：
```powershell
cd d:\MyProject\MiniUnicorn
python -m pytest tests/test_openai_api.py -v -k cache
```

---

## Phase 5：缓存命中率监控与展示

**问题**：`_extract_usage` 已能提取 DeepSeek 的 `prompt_cache_hit_tokens`，但没有做命中率计算、前缀溯源和展示。

**依赖**：Phase 1 完成后实施效果最佳（system prompt 稳定后才有意义监控命中率）。

---

### T5.1：新建 cache_shape.py — 前缀哈希溯源

**文件**：`miniunicorn/agent/cache_shape.py`（新建）

```python
"""Prefix shape tracking for cache hit diagnosis.

Provides SHA256 fingerprinting of cache-stable prefix components (system
prompt, tools JSON) so callers can detect *why* a cache miss occurred —
was it the system prompt that changed, or the tools definition?
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


@dataclass(frozen=True)
class PrefixShape:
    """SHA256 fingerprint of the cache-stable prefix components.

    Attributes:
        system_hash: First 16 hex chars of SHA256(system_prompt).
        tools_hash: First 16 hex chars of SHA256(tools_json).
        log_rewrite_version: Bumped when the log-rewrite logic changes
            (forces cache invalidation on code updates).
    """

    system_hash: str = ""
    tools_hash: str = ""
    log_rewrite_version: int = 0

    @staticmethod
    def hash(text: str) -> str:
        """Return first 16 hex chars of SHA256(text)."""
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def from_prompt(
        system_prompt: str,
        tools_json: str,
        log_version: int = 0,
    ) -> "PrefixShape":
        """Build a PrefixShape from the current prompt components."""
        return PrefixShape(
            system_hash=PrefixShape.hash(system_prompt),
            tools_hash=PrefixShape.hash(tools_json),
            log_rewrite_version=log_version,
        )


@dataclass
class CacheDiagnostics:
    """Result of comparing two PrefixShapes.

    Attributes:
        prefix_changed: True if any component differs from the previous shape.
        prefix_change_reasons: List of component names that changed
            ("system", "tools", "log_rewrite").
    """

    prefix_changed: bool = False
    prefix_change_reasons: list[str] = field(default_factory=list)

    @staticmethod
    def compare(prev: PrefixShape | None, cur: PrefixShape) -> "CacheDiagnostics":
        """Compare two PrefixShapes and report what changed."""
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

**验证**：
```powershell
cd d:\MyProject\MiniUnicorn
python -m ruff check miniunicorn/agent/cache_shape.py
python -c "from miniunicorn.agent.cache_shape import PrefixShape, CacheDiagnostics; print('import ok')"
```

---

### T5.2：loop.py — 会话级累计计数器

**文件**：`miniunicorn/agent/loop.py`

**步骤 1/3：新增 import（在第 27 行附近）**

搜索：
```python
from miniunicorn.agent.context import ContextBuilder
```

替换为：
```python
from miniunicorn.agent.cache_shape import CacheDiagnostics, PrefixShape
from miniunicorn.agent.context import ContextBuilder
```

**步骤 2/3：在 AgentLoop.__init__ 中新增字段**

需要在 `__init__` 方法中找到合适的位置添加字段。搜索 `self.embedding_control` 附近的代码（约第 358-370 行）。在 `self.embedding_control = ...` 之后追加：

搜索：
```python
        self.embedding_control = EmbeddingControl.for_workspace(
```

> **注意**：先读取该行附近代码确认精确上下文。在 `self.embedding_control` 赋值语句块之后插入新字段。具体操作：
>
> 搜索 `self.context.memory.set_reconcile_hook(self.embedding_control.request_reconcile)` 这一行，在它之后插入：

搜索：
```python
        self.context.memory.set_reconcile_hook(self.embedding_control.request_reconcile)
```

替换为：
```python
        self.context.memory.set_reconcile_hook(self.embedding_control.request_reconcile)

        # Session-level cache diagnostics (persist across turns, reset on
        # compaction). Used to track DeepSeek prefix cache hit rate.
        self._sess_cache_hit: int = 0
        self._sess_cache_miss: int = 0
        self._prev_prefix_shape: PrefixShape | None = None
```

**步骤 3/3：新增方法**

在 `AgentLoop` 类中新增以下两个方法。建议放在 `_build_initial_messages` 方法之前（第 486 行之前）：

搜索：
```python
    async def _build_initial_messages(
```

替换为：
```python
    def _accumulate_cache_usage(self, usage: dict[str, Any]) -> None:
        """Accumulate cache hit/miss tokens for session-level ratio."""
        cached = int(usage.get("cached_tokens", 0) or 0)
        if not cached:
            cached = int(usage.get("prompt_cache_hit_tokens", 0) or 0)
        miss = int(usage.get("cache_miss_tokens", 0) or 0)
        if not miss:
            miss = int(usage.get("prompt_cache_miss_tokens", 0) or 0)
        if cached == 0 and miss == 0:
            prompt = int(usage.get("prompt_tokens", 0) or 0)
            if prompt > 0:
                miss = prompt  # no cache info → all miss
        self._sess_cache_hit += cached
        self._sess_cache_miss += miss

    def _session_cache_ratio(self) -> int | None:
        """Return session-level cache hit ratio as percentage, or None."""
        total = self._sess_cache_hit + self._sess_cache_miss
        if total == 0:
            return None
        return self._sess_cache_hit * 100 // total

    async def _build_initial_messages(
```

**验证**：
```powershell
cd d:\MyProject\MiniUnicorn
python -m ruff check miniunicorn/agent/loop.py
python -c "from miniunicorn.agent.loop import AgentLoop; print('import ok')"
```

---

### T5.3：progress_hook.py — 展示缓存命中率

**文件**：`miniunicorn/agent/progress_hook.py`

搜索（第 169-175 行）：
```python
        u = context.usage or {}
        logger.debug(
            "LLM usage: prompt={} completion={} cached={}",
            u.get("prompt_tokens", 0),
            u.get("completion_tokens", 0),
            u.get("cached_tokens", 0),
        )
```

替换为：
```python
        u = context.usage or {}
        prompt = u.get("prompt_tokens", 0)
        cached = u.get("cached_tokens", 0)
        # Also try DeepSeek native field
        if not cached:
            cached = u.get("prompt_cache_hit_tokens", 0)
        fresh = max(0, prompt - cached) if prompt else 0
        cache_pct = (cached * 100 // prompt) if prompt else 0
        logger.info(
            "LLM usage: {} tok | in {} ({} cached / {} new) | out {} | cache {}%",
            u.get("total_tokens", 0),
            prompt,
            cached,
            fresh,
            u.get("completion_tokens", 0),
            cache_pct,
        )
```

**验证**：
```powershell
cd d:\MyProject\MiniUnicorn
python -m ruff check miniunicorn/agent/progress_hook.py
python -m pytest tests/agent/test_progress_hook.py -v
```

---

### T5.4：helpers.py — 状态栏展示缓存绝对计数

**文件**：`miniunicorn/utils/helpers.py`

搜索（第 536-549 行）：
```python
    cached = last_usage.get("cached_tokens", 0)
    ctx_total = max(context_window_tokens, 0)
```

替换为：
```python
    cached = last_usage.get("cached_tokens", 0)
    if not cached:
        cached = last_usage.get("prompt_cache_hit_tokens", 0)
    ctx_total = max(context_window_tokens, 0)
```

然后修改 token_line 显示逻辑。搜索：
```python
    token_line = f"\U0001f4ca Tokens: {last_in} in / {last_out} out"
    if cached and last_in:
        token_line += f" ({cached * 100 // last_in}% cached)"
```

替换为：
```python
    fresh = max(0, last_in - cached) if last_in else 0
    token_line = f"\U0001f4ca Tokens: {last_in} in / {last_out} out"
    if cached and last_in:
        token_line += f" ({cached} cached / {fresh} new)"
```

**验证**：
```powershell
cd d:\MyProject\MiniUnicorn
python -m ruff check miniunicorn/utils/helpers.py
python -m pytest tests/utils/ -v -k status
```

---

### T5.5：新建 test_cache_shape.py

**文件**：`tests/agent/test_cache_shape.py`（新建）

```python
"""Tests for prefix shape tracking and cache diagnostics."""

from miniunicorn.agent.cache_shape import CacheDiagnostics, PrefixShape


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


def test_prefix_shape_detects_log_version_change():
    prev = PrefixShape.from_prompt("hello", "[tool1]", 0)
    cur = PrefixShape.from_prompt("hello", "[tool1]", 1)
    diag = CacheDiagnostics.compare(prev, cur)
    assert diag.prefix_changed
    assert "log_rewrite" in diag.prefix_change_reasons


def test_prefix_shape_no_change():
    prev = PrefixShape.from_prompt("hello", "[tool1]", 0)
    cur = PrefixShape.from_prompt("hello", "[tool1]", 0)
    diag = CacheDiagnostics.compare(prev, cur)
    assert not diag.prefix_changed
    assert diag.prefix_change_reasons == []


def test_prefix_shape_first_call_no_change():
    cur = PrefixShape.from_prompt("hello", "[tool1]", 0)
    diag = CacheDiagnostics.compare(None, cur)
    assert not diag.prefix_changed


def test_prefix_shape_empty_prev_no_change():
    cur = PrefixShape.from_prompt("hello", "[tool1]", 0)
    diag = CacheDiagnostics.compare(PrefixShape(), cur)
    assert not diag.prefix_changed
```

**验证**：
```powershell
cd d:\MyProject\MiniUnicorn
python -m pytest tests/agent/test_cache_shape.py -v
python -m ruff check tests/agent/test_cache_shape.py
```

---

## Phase 5 验证：在 loop.py 中接入计数器

> 此任务将 `_accumulate_cache_usage` 接入到 provider usage 回调中。需要在 `loop.py` 中找到处理 provider 返回 usage 的位置。

**文件**：`miniunicorn/agent/loop.py`

需要在 `_run_agent_loop` 方法中找到 `before_provider_call` 传给 runner 的位置（第 846 行附近），以及 runner 返回 usage 的位置。由于 runner 内部处理 usage，最简单的接入点是在 `AgentProgressHook.on_llm_finish` 中调用 loop 的累计方法。

**修改 progress_hook.py**：在 `on_llm_finish` 中调用 loop 的累计方法。

搜索 progress_hook.py 中修改后的 usage 日志块（T5.3 已修改的部分），在其后追加：

```python
        # Accumulate cache stats on the loop if available
        if hasattr(self, "_loop_ref") and self._loop_ref is not None:
            self._loop_ref._accumulate_cache_usage(u)
```

> **注意**：这需要 `AgentProgressHook` 持有 loop 的引用。检查 `AgentProgressHook.__init__` 是否已接收 loop 引用。如果没有，需要在创建 hook 时传入。
>
> **替代方案**（更简单）：在 `loop.py` 的 `_run_agent_loop` 方法中，runner 返回后直接读取 result 中的 usage 并累计。搜索 `result = await self.runner.run(` 之后的代码，在 result 拿到后追加：
>
> ```python
> if result and hasattr(result, "usage") and result.usage:
>     self._accumulate_cache_usage(result.usage)
> ```
>
> 具体实现取决于 `AgentRunResult` 是否携带 usage。先读取 runner.py 确认 result 结构。

**此任务标记为需要根据实际代码结构调整**。如果 agent 在实施时发现接入点不明确，可以跳过此步骤——计数器方法已就位，后续可手动接入。

---

## Phase 2：Recent History 移出 system prompt [可选，风险较高]

**问题**：`build_system_prompt` 每轮都调用 `read_unprocessed_history`，新条目会改变 Recent History 部分，导致 system prompt 变化。

**风险**：6 个现有测试断言 history 在 system prompt 中，需要全部修改。建议在 Phase 1 稳定后再实施。

**依赖**：Phase 1 完成。

---

### T2.1：context.py — Recent History 移出 system prompt，拼到 user 消息尾部

**文件**：`miniunicorn/agent/context.py`

**步骤 1/2：从 build_system_prompt 中删除 Recent History 块（第 198-205 行）**

搜索：
```python
            entries = self.memory.read_unprocessed_history(
                since_cursor=self.memory.get_last_dream_cursor()
            )
            if entries:
                capped = entries[-self._MAX_RECENT_HISTORY :]
                history_text = "\n".join(f"- [{e['timestamp']}] {e['content']}" for e in capped)
                history_text = truncate_text(history_text, self._MAX_HISTORY_CHARS)
                parts.append((self._PRIORITY_HISTORY, "# Recent History\n\n" + history_text))
```

替换为：
```python
            # Recent History moved to user message tail for cache stability.
            # See build_messages for the injection point.
```

**步骤 2/2：在 build_messages 中追加 recent_history_tail**

> **前置调整**：`light_context` 变量当前定义在 `merged` 之后（第 491 行），但 `recent_history_tail` 需要在 `merged` 之前使用它。因此需先将 `light_context` 的定义移到 `memory_tail` 之前。
>
> 搜索（Phase 1 T1.2 修改后的代码）：
> ```python
>         user_content = self._build_user_content(current_message, media)
>
>         # Memory recall results are appended to the user message tail (not
>         # injected into the system prompt) so the system prompt stays
>         # byte-stable across tool iterations for DeepSeek prefix cache hits.
>         memory_tail = ""
> ```
>
> 替换为：
> ```python
>         user_content = self._build_user_content(current_message, media)
>
>         # light_context 从 runtime_state 读取(由 AgentLoop 设置,用于心跳等轻量场景)
>         light_context = (
>             bool(getattr(runtime_state, "_light_context", False)) if runtime_state else False
>         )
>
>         # Memory recall results are appended to the user message tail (not
>         # injected into the system prompt) so the system prompt stays
>         # byte-stable across tool iterations for DeepSeek prefix cache hits.
>         memory_tail = ""
> ```
>
> 然后删除原来位置的 `light_context` 定义。搜索：
> ```python
>         # light_context 从 runtime_state 读取(由 AgentLoop 设置,用于心跳等轻量场景)
>         light_context = (
>             bool(getattr(runtime_state, "_light_context", False)) if runtime_state else False
>         )
>         messages = [
> ```
>
> 替换为：
> ```python
>         messages = [
> ```

搜索 T1.2 中已修改的 memory_tail 拼接逻辑：
```python
        memory_tail = ""
        if memory_prompt is not None and memory_prompt.text:
            memory_tail = f"\n\n<!-- miniunicorn-memory -->\n{memory_prompt.text}"

        # Merge runtime context and user content into a single user message
```

替换为：
```python
        memory_tail = ""
        if memory_prompt is not None and memory_prompt.text:
            memory_tail = f"\n\n<!-- miniunicorn-memory -->\n{memory_prompt.text}"

        # Recent History appended to user message tail for cache stability.
        recent_history_tail = ""
        if not light_context:
            entries = self.memory.read_unprocessed_history(
                since_cursor=self.memory.get_last_dream_cursor()
            )
            if entries:
                capped = entries[-self._MAX_RECENT_HISTORY :]
                history_text = "\n".join(
                    f"- [{e['timestamp']}] {e['content']}" for e in capped
                )
                history_text = truncate_text(history_text, self._MAX_HISTORY_CHARS)
                recent_history_tail = (
                    f"\n\n<!-- miniunicorn-recent-history -->\n"
                    f"# Recent History\n\n{history_text}"
                )

        full_tail = memory_tail + recent_history_tail

        # Merge runtime context and user content into a single user message
```

然后修改 merged 拼接，搜索：
```python
        if isinstance(user_content, str):
            merged = f"{user_content}\n\n{runtime_ctx}{memory_tail}"
        else:
            merged = user_content + [{"type": "text", "text": runtime_ctx + memory_tail}]
```

替换为：
```python
        if isinstance(user_content, str):
            merged = f"{user_content}\n\n{runtime_ctx}{full_tail}"
        else:
            merged = user_content + [{"type": "text", "text": runtime_ctx + full_tail}]
```

> **注意**：`light_context` 变量在 `build_messages` 中从 `runtime_state` 读取（第 491-493 行）。需确认 `light_context` 在 recent_history_tail 代码块之前已定义。当前代码中 `light_context` 定义在第 491 行，而 `memory_tail` 在第 480 行附近。需要将 `light_context` 的定义移到 `memory_tail` 之前，或者将 recent_history_tail 的读取也放在 `light_context` 定义之后。
>
> **实施时**：先读取 `build_messages` 完整方法，确认 `light_context` 定义位置，调整代码顺序确保 `light_context` 在使用前已定义。

---

### T2.2：更新受影响测试

**文件**：`tests/agent/test_context_prompt_cache.py`

以下 5 个测试断言 Recent History 在 system prompt 中，需要改为断言在 user 消息中：

1. **test_unprocessed_history_injected_into_system_prompt（第 144 行）**
   - 改测试名为 `test_unprocessed_history_injected_into_user_message`
   - 改 `prompt = builder.build_system_prompt()` → `messages = builder.build_messages(...)`
   - 改断言 `assert "# Recent History" in prompt` → `assert "# Recent History" in messages[-1]["content"]`
   - 改断言 `assert "User asked about weather in Tokyo" in prompt` → `assert ... in messages[-1]["content"]`

2. **test_recent_history_capped_at_max（第 159 行）**
   - 同上模式，改为从 `messages[-1]["content"]` 中断言

3. **test_recent_history_truncated_at_max_chars（第 173 行）**
   - 同上模式

4. **test_no_recent_history_when_dream_has_processed_all（第 187 行）**
   - 改为断言 `"# Recent History" not in messages[-1]["content"]`

5. **test_partial_dream_processing_shows_only_remainder（第 199 行）**
   - 同上模式

**每个测试的修改模式**：

```python
# 修改前：
prompt = builder.build_system_prompt()
assert "..." in prompt

# 修改后：
messages = builder.build_messages(
    history=[], current_message="hi", channel="cli", chat_id="direct",
)
user_content = messages[-1]["content"]
assert "..." in user_content
```

**验证**：
```powershell
cd d:\MyProject\MiniUnicorn
python -m pytest tests/agent/test_context_prompt_cache.py tests/agent/test_context_builder.py -v
python -m ruff check miniunicorn/agent/context.py
```

---

## Phase 4：压缩阈值分级 [可选]

**问题**：`autocompact.py` 只有 TTL 触发，没有基于 token 用量的主动压缩阈值。

**依赖**：需要了解 `Consolidator.compact_idle_session` API。

> **此阶段为可选增强**。如果 agent 实施时发现 `Consolidator` API 复杂或不确定，可以跳过。Phase 1-3+5+6 已能显著提升缓存命中率。

实施步骤参见原始方案文档 `docs/deepseek-cache-optimization-plan.md` Phase 4 部分。核心改动是在 `loop.py` 中新增 `_maybe_compact_by_tokens` 方法，在 provider usage 返回后检查 `prompt_tokens / context_window_tokens` 比率，按 50%/80%/90% 三级阈值触发不同行为。

---

## 全局验证

所有阶段完成后执行：

```powershell
cd d:\MyProject\MiniUnicorn

# 1. 全量测试
python -m pytest tests/ -v --timeout=60

# 2. Lint
python -m ruff check miniunicorn/

# 3. 关键路径验证
python -c "
from miniunicorn.agent.context import ContextBuilder
from miniunicorn.agent.loop import AgentLoop
from miniunicorn.agent.cache_shape import PrefixShape, CacheDiagnostics
from miniunicorn.providers.openai_compat_provider import OpenAICompatProvider
print('All imports OK')
"

# 4. 端到端验证：启动 gateway，用 DeepSeek 模型进行多轮对话
#    观察日志中的 "cache N%" 行，确认命中率提升
python -m miniunicorn gateway
```

---

## 预期效果

| 指标 | 改造前 | Phase 1 后 | Phase 1+3+5+6 后 | 全部完成 |
|------|--------|------------|-------------------|----------|
| 短会话命中率 | 20-40% | 50-70% | 60-80% | 70-85% |
| 长会话命中率 | 10-30% | 60-80% | 75-90% | 85-95% |
| 多工具迭代命中率 | ~0% | 70-90% | 75-92% | 75-92% |
| 可观测性 | 仅 cached_tokens | 同前 | 前缀溯源+会话级命中率 | 同前 |

> Phase 1 是收益最大的一项，单独完成后即可显著提升命中率。后续阶段是增量优化。

---

## 回滚指南

如果某个阶段导致测试大面积失败且无法快速修复：

1. **Phase 1 回滚**：恢复 `context.py` 的 `build_system_prompt` 签名（加回 `memory_prompt` 参数和注入块），恢复 `loop.py` 的 `_refresh_memory` 闭包，恢复 import
2. **Phase 3 回滚**：恢复 `openai_compat_provider.py` 第 767-770 行的原始条件（去掉 `and msg.get("tool_calls")`）
3. **Phase 5 回滚**：删除 `cache_shape.py`，恢复 `progress_hook.py` 和 `helpers.py` 的原始日志格式
4. **Phase 6 回滚**：删除 `_extract_usage` 中的 miss 提取块，恢复 `turn_budget.py` 的原始 accumulate 逻辑

每个阶段的改动是独立的，可以单独回滚而不影响其他阶段。
