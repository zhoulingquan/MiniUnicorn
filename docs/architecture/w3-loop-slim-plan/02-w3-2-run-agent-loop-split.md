# W3-2:`_run_agent_loop` 分段方法化 + 闭包提取

> 前置依赖:W3-1 已合并(`6a02fb8e`,`pytest tests/ -q` 4104 passed / 0 failed)。本批是 W3 系列末批。
> 行号锚点已按 W3-1 合并后的 loop.py(共 1398 行)刷新;**行号仅为编写时参考值,定位以符号为准**。
> 本批是**纯搬家重构**:语义逐句对应、零行为变更、零新抽象(`functools.partial` 回调绑定不算新抽象)。

## 一、问题(现状锚点)

`miniunicorn/agent/loop.py` 的 `AgentLoop._run_agent_loop`(1077-1308,约 232 行)是 loop.py 内最大方法,混杂七类关注点:

| 区段 | 行号(参考) | 内容 |
|---|---|---|
| hook 装配 | 1109-1128 | `_sync_subagent_runtime_limits`、AgentProgressHook 构造(11 个实参,含 on_iteration lambda)、turn_hooks 与 `_extra_hooks` 合并顺序(turn_hooks 在后)、CompositeHook |
| 闭包×2 | 1130-1188 | `_checkpoint`(1130-1133,绑 session);`_drain_pending`(1135-1188,**约 54 行**,含 subagent 阻塞等待、media 预处理、limit 上限、超时 warning) |
| 上下文绑定 | 1190-1213 | active_session_key、`workspace_scopes.for_turn`、RequestContext、file_state/request/workspace 三个 token 绑定、telemetry 复用或新建 |
| override 解析 | 1214-1224 | agent_override 工具白名单过滤(`_filter_tools_for_override`)与模型选择 |
| goal 提示 | 1225-1237 | `goal_state_runtime_lines` → 持续目标继续提示或 `SUSTAINED_GOAL_CONTINUE_PROMPT` |
| spec 构建 | 1238-1287 | try: `runner.run(AgentRunSpec(...))`(30 个字段,含 llm_timeout_s 的 `runner_wall_llm_timeout_s`、goal_active_predicate lambda);finally: 三 token 复位 + telemetry 复位 |
| 收尾 | 1288-1308 | `record_last_usage`、telemetry usage 写回、max_iterations 流式补推、error 日志、五元组返回 |

### 1.1 回调契约锚点(本批最关键的不变量)

runner.py 438-449 对 `spec.injection_callback` 做 `inspect.signature` 检查:`"limit" in signature.parameters` 为真时以 `injection_callback(limit=_MAX_INJECTIONS_PER_TURN)` 调用,否则无参调用。原 `_drain_pending` 闭包签名 `(*, limit=...)` 走 limit 路径。提取为方法后用 `functools.partial` 绑定 `pending_queue` / `session`,**`inspect.signature(partial)` 解析后 `limit` 必须仍是参数名**,否则注入语义静默退化为无参调用(每次只取一条而非批量)。

## 二、改动

### 2.1 提取一:`_build_turn_hook`(hook 装配,1109-1128)

```python
def _build_turn_hook(
    self,
    on_progress, on_stream, on_stream_end, on_retry_wait,
    channel: str, chat_id: str, message_id: str | None,
    metadata: dict[str, Any] | None, session_key: str | None,
    turn_hooks: list[AgentHook] | None,
) -> AgentHook:
    """单轮 hook 装配。turn_hooks 在 _extra_hooks 之后(per-run 优先)。"""
```

要点:`on_iteration=lambda iteration: setattr(self, "_current_iteration", iteration)` lambda 保留在方法内;`extra = list(self._extra_hooks) + list(turn_hooks or [])` 合并顺序与注释(1124-1126)照搬;无 extra 时直接返回 `loop_hook`。

### 2.2 提取二:`_drain_pending_messages`(约 54 行闭包 → 方法,1135-1188)

```python
async def _drain_pending_messages(
    self,
    pending_queue: asyncio.Queue | None,
    session: Session | None,
    *,
    limit: int = _MAX_INJECTIONS_PER_TURN,
) -> list[dict[str, Any]]:
    """从 pending 队列取出后续消息(subagent 在跑且队列空时阻塞等待)。"""
```

要点:
- 闭包体内的 `_to_user_message` 内嵌函数随方法整体搬入(含 `self._prepare_message_media` 与 `self.context._build_user_content` 调用、media 预处理顺序);
- 阻塞分支(1166-1186):`get_running_count_by_session > 0` 判定、`asyncio.wait_for(pending_queue.get(), timeout=_SUBAGENT_DRAIN_WAIT_S)`、TimeoutError warning(含 session.key 文本)、二次 get_nowait 循环,逐句照搬;
- `pending_queue is None` 早退保留。

### 2.3 提取三:`_emit_turn_checkpoint`(1130-1133)

```python
async def _emit_turn_checkpoint(
    self, session: Session | None, payload: dict[str, Any]
) -> None:
```

原闭包逻辑:`session is None` 早退 + `self._set_runtime_checkpoint(session, payload)`。

### 2.4 提取四:`_resolve_override_runtime`(1214-1224)

```python
def _resolve_override_runtime(
    self, agent_override: SubagentDefinition | None
) -> tuple[ToolRegistry, str]:
    """agent_override 工具白名单与模型选择。返回 (tools, run_model)。"""
```

### 2.5 提取五:`_build_goal_continue`(1225-1237)

```python
def _build_goal_continue(self, session: Session | None) -> str:
```

`goal_state_runtime_lines(session.metadata ...)` 判定、f-string 拼接、`SUSTAINED_GOAL_CONTINUE_PROMPT` 回退,逐句照搬。

### 2.6 提取六:`_build_agent_run_spec`(1240-1280)

```python
def _build_agent_run_spec(
    self,
    initial_messages: list[dict],
    tools: ToolRegistry,
    run_model: str,
    hook: AgentHook,
    session: Session | None,
    session_key: str | None,
    user_key: str | None,
    effective_scope,  # workspace_scopes.for_turn 返回类型,以源码为准
    goal_continue: str,
    on_progress, on_stream, on_retry_wait,
    checkpoint_callback, injection_callback,
) -> AgentRunSpec:
```

要点:
- 30 个 spec 字段逐一照搬,含两条注释(1261-1262 持续目标超时说明、1272 Plan-and-Execute 说明);
- `runner_wall_llm_timeout_s(self.sessions, ...)` 在方法内调用(经 `self.sessions`);
- `goal_active_predicate` lambda 逐字保留;
- 回调参数以形参传入,不在方法内构造 partial。

### 2.7 提取七:`_record_turn_outcome`(1288-1301)

```python
def _record_turn_outcome(self, result, on_stream, on_stream_end) -> None:
```

要点:`self._response.record_last_usage(result)`、`turn_telemetry.current()` 读取与 usage/last_call_usage 写回、max_iterations 分支(含流式补推与注释)、error 分支日志(含 `[:200]` 截断),逐句照搬。

### 2.8 主方法骨架(拆分后形态)

保留:签名(19 行)与 docstring、`_sync_subagent_runtime_limits`、hook/闭包替代品调用、上下文绑定区段(1190-1213,核心编排)、telemetry 绑定、try/finally 结构。目标形态:

```python
self._sync_subagent_runtime_limits()
hook = self._build_turn_hook(...)

injection_callback = functools.partial(self._drain_pending_messages, pending_queue, session)
checkpoint_callback = functools.partial(self._emit_turn_checkpoint, session)

# ...1190-1213 上下文绑定与 telemetry 原样保留...
tools, run_model = self._resolve_override_runtime(agent_override)
goal_continue = self._build_goal_continue(session)
try:
    result = await self.runner.run(
        self._build_agent_run_spec(
            initial_messages, tools, run_model, hook, session, session_key,
            user_key, effective_scope, goal_continue,
            on_progress, on_stream, on_retry_wait,
            checkpoint_callback, injection_callback,
        )
    )
finally:
    # 三个 token 复位 + telemetry 复位,逐句照搬
    ...
self._record_turn_outcome(result, on_stream, on_stream_end)
return (result.final_content, result.tools_used, result.messages, result.stop_reason, result.had_injections)
```

拆分后 `_run_agent_loop` 预期约 90-110 行。**不得**改变任何条件、日志文本、绑定/复位顺序、返回值结构。

## 三、测试要求

**核心纪律:本批不允许修改任何既有测试的断言**(纯搬家;若某测试失败说明搬错了,回退重搬)。

新增 `tests/agent/test_loop_run_phase_split.py`:

1. **`_drain_pending_messages` 单元**(用 conftest `make_loop` 构造):
   - `pending_queue=None` → `[]`;
   - 队列预置 2 条 InboundMessage、`limit=2` → 2 条 user 消息;`limit=1` → 只取 1 条(上限生效);
   - 队列空 + 无运行 subagent → `[]`;
   - 队列空 + `get_running_count_by_session > 0`(monkeypatch)+ `asyncio.wait_for` 超时(monkeypatch 抛 TimeoutError)→ 返回 `[]` 且 warning 日志包含 session.key。
2. **回调契约**:`"limit" in inspect.signature(functools.partial(loop._drain_pending_messages, queue, session)).parameters` 为真(与 runner.py 438-449 的判定兼容——这是本批最关键的回归锁)。
3. **结构断言**:`_run_agent_loop` 源码行数 < 130(`inspect.getsource` 统计)。
4. **hook 合并顺序**:`turn_hooks` 在 `_extra_hooks` 之后(CompositeHook 展开 or monkeypatch 记录顺序,二选一)。
5. **集成回归**:既有 4104 个测试通过即为此证(尤其 `tests/agent/test_runner_injections.py`),本条只补一个最小冒烟:直连 `_run_agent_loop` 或走既有 fake provider 设施完成一次带 pending_queue 的回合。

## 四、禁改清单

- 一切行为语义:条件顺序、日志文本、绑定/复位顺序、返回五元组结构
- `_drain_pending` 的 subagent 阻塞语义与 `_SUBAGENT_DRAIN_WAIT_S` 超时值
- AgentRunSpec 的 30 个字段值与取值表达式
- W1-2 注入-回退双路径、W2-2 主循环分段成果(runner.py 本批零改动)
- `_filter_tools_for_override` / `_set_tool_context` / `_build_turn_budget` 等既有方法本体(只移动调用点)
- 不新增类 / 配置 / 事件 / 参数默认值变化

## 五、验收自检

- [ ] 全量测试绿且**零既有测试修改**,ruff 零告警
- [ ] `_run_agent_loop` 行数 < 130(报告附 `inspect.getsource` 统计值)
- [ ] 回调契约断言通过(`inspect.signature(partial(...))` 含 `limit` 参数)
- [ ] `_drain_pending_messages` 四条单元断言通过
- [ ] 2.1-2.7 各提取的语义逐句对应清单逐项打勾(人工核对,报告列出)
- [ ] 与本规格的偏差逐条说明
