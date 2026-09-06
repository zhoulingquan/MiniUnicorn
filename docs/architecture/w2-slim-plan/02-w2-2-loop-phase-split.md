# W2-2：主循环分段方法化 + PlanSnapshot.with_origin

> 前置依赖：tag `baseline-pre-w2`。建议在 W2-1、W2-3 之后实施（本批最大，独占 runner.py）。
> 本批是**纯搬家重构**：语义逐句对应、零行为变更、零新抽象（无新类、无新配置、无新事件）。

## 一、问题（现状锚点）

`erza/agent/runner.py` 共 1657 行，`AgentRunner._run_with_ledger`（485-990 行）单个方法约 505 行，是全项目最大的 god-method。内部混合了六类关注点：

| 区段 | 行号（参考） | 内容 |
|---|---|---|
| turn 装配 | 486-502 | ledger / turn_id / deadline / planner init / snapshot / reflection |
| 迭代前置 | 505-602 | deadline 检查、FAST→MANAGED escalation（60 行内联）、governance、步骤指引、预算预检 |
| 模型请求 | 603-637 | request_model、usage 累积、reasoning 提取 |
| 工具分支 | 639-767 | assistant 消息、awaiting_tools checkpoint、execute_tools、观察累积、fatal error replan、tools_completed、injection drain、activate_plan 接管 |
| 收尾分支 | 769-964 | empty retry、length recovery、injection drain、error/empty/final 三种终止、`_complete_plan_step` 验收、progress verdict、REPLAN |
| teardown | 965-990 | max_iterations 收尾、terminal reflection、结果组装 |

另有内联坏味道：escalation 块内（545-557 行）为改一个 `origin` 字段对 PlanSnapshot 做 **12 字段逐个复制重建**。

**已否决的替代方案**（见 00-overview.md 裁决表）：抽取 TurnController 类（需 10+ 字段构造或 frame 对象，抽象税）。本批沿用代码库既有惯例：phase 方法 + action 字符串返回（`_retry_empty_response`、`_handle_length_recovery`、`_handle_fatal_tool_error` 皆此模式，992 行注释明言"自 run 拆出，语义与原内联实现逐句对应"）。

## 二、改动

### 2.1 PlanSnapshot.with_origin（plan_snapshot.py，先做，独立可测）

```python
def with_origin(self, origin: str) -> "PlanSnapshot":
    """Copy of this snapshot with a different origin (e.g. 'escalated')."""
    return PlanSnapshot(
        goal=self.goal, steps=self.steps, replan_count=self.replan_count,
        max_replans=self.max_replans, current_step_id=self.current_step_id,
        turn_id=self.turn_id, created_at=self.created_at,
        stop_reason=self.stop_reason, origin=origin, digest=self.digest,
    )
```

runner.py 545-557 行的 12 字段重建替换为：

```python
state.plan_snapshot = state.plan_snapshot.with_origin("escalated")
```

### 2.2 escalation 块搬家（runner.py 515-575 → 新私有方法）

```python
async def _maybe_escalate_to_managed(
    self,
    spec: AgentRunSpec,
    state: _TurnState,
    planner: Any,
    plan: Any,
    planner_task_text: str | None,
    planner_tools_summary: str | None,
) -> tuple[Any, Any, str | None, str | None]:
    """FAST 停滞检测 → MANAGED 升级。返回 (planner, plan, task_text, tools_summary)。

    条件不满足或升级失败时原样返回入参（plan 不变即无升级）。
    """
```

实现要点：
- 入口条件（plan is None / 未升级过 / planning_policy 存在 / `consecutive_nontool_iterations >= 2`）整体搬入方法；调用点只剩一个 `if` 包裹或直接调用（方法内部自判，返回未变的 plan 时外层无感知）。
- 内部语义逐句对应：`_init_planner` 重试、快照 emit、`with_origin("escalated")`（2.1 产物）、ProgressTracker 重建、`escalated_this_turn`/`consecutive_nontool_iterations` 状态写回（经 state 对象可变引用）、except 分支 logger.warning 后原样返回。
- 返回后主循环的局部变量 `planner / plan / planner_task_text / planner_tools_summary` 用返回值重绑。

### 2.3 工具分支搬家（runner.py 639-767 → 新私有方法）

```python
async def _execute_tool_iteration(
    self,
    spec: AgentRunSpec,
    state: _TurnState,
    hook: AgentHook,
    messages: list[dict[str, Any]],
    context: AgentHookContext,
    plan: Any,
    planner: Any,
    planner_task_text: str | None,
    planner_tools_summary: str | None,
    reflection: Any,
    iteration: int,
    response: Any,
) -> tuple[str, Any]:
    """工具响应迭代。返回 (action, plan)：action ∈ {"continue", "break"}。"""
```

语义逐句对应清单（搬家人工核对项）：
- assistant 消息构建与 append（644-650 行）
- `state.tools_used.extend`（651 行）
- awaiting_tools checkpoint（652-664 行）
- `hook.before_execute_tools`（666 行）
- `execute_tools` + `build_observations` + `tool_events`（668-693 行，**观察累积顺序与注释保留**：先摘观察 pop receipt，再入列事件）
- `context.tool_results` / `context.tool_events` / `build_tool_result_messages`（694-699 行）
- fatal error 分支（700-733 行）：`_handle_fatal_tool_error` 既有调用 + replan 后 `tool_observations.clear()` + 快照重发 + `action == "continue"` / `break` 透传为返回值
- tools_completed checkpoint（734-744 行）
- `empty_content_retries` / `length_recovery_count` 复位（745-746 行）
- injection drain（748-756 行）
- `hook.after_iteration`（757 行）
- `consecutive_nontool_iterations = 0`（759 行）
- activate_plan 接管（762-766 行）：`take_pending_plan()` → `_adopt_activated_plan`，返回的 plan 随方法返回值透传
- 末尾 `continue`

### 2.4 收尾分支搬家（runner.py 769-964 → 新私有方法）

```python
async def _finalize_nontool_iteration(
    self,
    spec: AgentRunSpec,
    state: _TurnState,
    hook: AgentHook,
    messages: list[dict[str, Any]],
    budget: Any,
    context: AgentHookContext,
    plan: Any,
    planner: Any,
    planner_task_text: str | None,
    planner_tools_summary: str | None,
    reflection: Any,
    iteration: int,
    response: Any,
    raw_usage: dict[str, int],
    clean: str,
) -> tuple[str, Any]:
    """非工具响应迭代（含终止与验收）。返回 (action, plan)。"""
```

语义逐句对应清单：
- `finish_reason` 非 tool 的 warning 日志（769-774 行）
- FAST 停滞计数（776-778 行）
- `hook.finalize_content`（780 行）
- `_retry_empty_response` 既有调用与 action 透传（781-797 行）
- length recovery（799-803 行）
- assistant 消息构建（805-811 行）
- injection drain + stream end（813-832 行）
- error 终止分支（834-862 行，含 arrearage 判定、reflection 触发、`_end_turn_with_drain`）
- empty 终止分支（863-878 行）
- final append + final_response checkpoint（880-898 行）
- `_complete_plan_step` 验收 + progress verdict + ABORT/REPLAN 分支（899-959 行，**证据过滤表达式原样保留**：`[o for o in state.tool_observations if o.step_id == plan.current_step.id]`）
- REPLAN 分支的 `planner.replan` / `tool_observations.clear()` / 快照重发（931-949 行）
- final_content 写回与 `break`（960-964 行）

### 2.5 主循环骨架（_run_with_ledger 重写后的形态）

保留：turn 装配（486-502）、deadline 检查（505-513）、escalation 调用（一个语句）、governance/指引/预算预检（577-602）、模型请求与 usage（603-637）、工具/非工具分支的分派（两个 `if` + 元组解包）、max_iterations else 子句（965-966）、teardown 与结果组装（968-990）。

目标形态（示意）：

```python
if response.should_execute_tools:
    ...
    action, plan = await self._execute_tool_iteration(...)
    if action == "break":
        break
    continue

action, plan = await self._finalize_nontool_iteration(...)
if action == "break":
    break
```

拆分后 `_run_with_ledger` 预期约 180-220 行纯编排。**不得**在拆分中改变任何条件顺序、日志内容、checkpoint payload 结构。

## 三、测试要求

**核心纪律：本批不允许修改任何既有测试的断言**（纯搬家，行为零变更；若某测试失败说明搬错了，回退重搬）。允许的例外仅限纯导入路径调整（如测试直接 import 了被移动的私有符号——本批不移动任何符号，理论上不存在）。

新增 `tests/agent/test_runner_phase_split.py`：

1. **escalation 单元**：FAST + `consecutive_nontool_iterations=2` + policy 存在 → `_maybe_escalate_to_managed` 返回新 plan、state.escalated_this_turn=True、快照 origin=="escalated"；已升级过 → 返回原 plan。
2. **escalation 失败**：monkeypatch `_init_planner` 抛错 → 原样返回、warning 日志、无异常逃逸。
3. **with_origin**：改 origin 其余字段（含 digest）逐一相等；原快照不可变。
4. **结构断言**：`_run_with_ledger` 源码行数 < 260（防退化回 god-method；用 `inspect.getsource` 统计）。
5. **集成回归**（复用既有 fake provider 设施）：完整 MANAGED turn（计划 2 步 + 回执验收）行为与拆分前一致——直接依赖既有 4077 个测试的通过即为此证，本条只补一个最小冒烟。

## 四、禁改清单

- 一切行为语义：条件顺序、日志文本、checkpoint payload、异常路径、action 字符串取值
- `_TurnState` 字段与语义
- W0 证据管道相关代码（`build_observations` 调用位置必须在 execute_tools 之后、`tool_events.extend` 之前，注释保留）
- `_complete_plan_step` / `_adopt_activated_plan` / `_handle_fatal_tool_error` 等既有方法本体（只移动调用点，不改方法）
- 不新增类 / 配置 / 事件 / 参数默认值变化

## 五、验收自检

- [ ] 全量测试绿且**零既有测试修改**，ruff 零告警
- [ ] `_run_with_ledger` 行数 < 260（报告附 `inspect.getsource` 统计值）
- [ ] 2.3 / 2.4 的语义逐句对应清单逐项打勾（人工核对，报告列出）
- [ ] with_origin 替换后 origin=="escalated" 的集成断言通过
- [ ] 与本规格的偏差逐条说明
