# W1-3：activate_plan 激活器工具

> 前置依赖：W0-A3/A4 已合并（evidence_level 语义就位）；W1-2 已合并（生命周期边界清晰）。
> 目标：模型可以在**运行中**挂载一份计划，交由外层唯一主循环推进——这是"Planning 属于工具库"的最后一块：计划的**产生与激活**是工具能力，计划的**推进与验收**永远是核心主循环的职权。

## 一、语义定义（激活器，不是执行器）

| 属性 | 裁决 |
|---|---|
| 工具名 | `activate_plan`（新工具，`miniunicorn/agent/tools/activate_plan.py`） |
| 语义 | 挂载计划 → 立即返回。**不执行任何步骤、不 spawn 子代理、不嵌套循环** |
| 推进者 | 外层主循环既有机制：`apply_plan_step_guidance`（runner.py 约 575 行每迭代注入步骤指引）+ `complete_plan_step` 验收 |
| 风险级 | `RiskLevel.MEDIUM`（turn 内可逆、无不可逆副作用；HIGH 会挡住无审批回调的 headless/cron 场景） |
| scopes | `{"core"}`——子代理不可用，杜绝嵌套挂载歧义 |
| 既有别名 | `delegate_plan` / `execute_plan`（tools/execute_plan.py）**原样保留不动**（spawn_and_wait 并行委托语义，与本工具互补） |

## 二、工具实现（新文件 tools/activate_plan.py）

### 2.1 参数 schema

```python
@tool_parameters(
    tool_parameters_schema(
        plan=StringSchema(
            'A JSON plan: {"goal": "...", "steps": [{"id": 1, "action": "...", '
            '"tool_hint": "...", "done_criteria": "...", "evidence_level": "text"|"tool"}]}'
        ),
        required=["plan"],
    )
)
```

### 2.2 解析与校验（复用 planner 的模型，不调 LLM）

- `json.loads` 失败 / 非 dict / steps 缺失或空 → 返回 `"Error: ..."`（工具层错误约定，与 delegate_plan 一致）。
- steps 数量上限 **8**（防巨型计划挂死主循环；planner 自身是 2-6，激活器放宽到 8）。
- 每步构造 `PlanStep`：`evidence_level` 走 planner.py 的 `_normalize_evidence_level`（W0-A4 产物，直接 import 复用）；`tool_hint` / `done_criteria` 可选。
- 不做 LLM 校验——结构合法即接受，质量由验收器兜底（这正是 W0 的设计意图）。

### 2.3 激活通道（contextvar，与 receipts 同模式）

新模块内私有 contextvar：

```python
_pending_plan: ContextVar[Plan | None] = ContextVar("pending_plan_activation", default=None)

def take_pending_plan() -> Plan | None:
    plan = _pending_plan.get()
    _pending_plan.set(None)
    return plan
```

工具 execute 尾部（校验全过后）：

```python
_pending_plan.set(plan)
return (
    f"Plan activated: {len(plan.steps)} steps for goal: {plan.goal}\n"
    f"Step 1: {plan.steps[0].action}\n"
    "The system will guide you through each step; complete them in order."
)
```

返回文本只描述结构，**不**声称任何步骤已完成。

## 三、主循环接线（runner.py）

读取点：工具批次执行完成后、`tools_completed` checkpoint（约 712-722 行）之后、`continue`（约 738 行）之前：

```python
activated = take_pending_plan()
if activated is not None:
    plan = self._adopt_activated_plan(spec, state, plan, activated)
```

`_adopt_activated_plan`（runner 新私有方法）职责，按序：

1. **replan 预算**：若已有活跃 plan → `activated.replan_count = plan.replan_count + 1`，且 `activated.max_replans = plan.max_replans`；若 `plan.can_replan` 为 False（预算耗尽）→ **拒绝激活**，记 log，保持旧 plan，工具结果已返回（下一迭代模型会看到步骤指引未变，自然感知失败）。首次激活 → replan_count=0。
   - 注：拒绝发生在工具返回之后，属事后否决——批次报告中说明此取舍（替代方案是工具内预检预算，但工具拿不到当前 plan 状态，不做反向注入）。
2. **挂载**：`plan = activated`；`state.tool_observations.clear()`（W0-A1 的 replan 清空语义，防旧观察串新步骤 id）。
3. **progress_tracker 补建**：`state.progress_tracker is None` 时按 MANAGED 初始化路径构造（`ProgressTracker(ProgressPolicy())`，与既有初始化同参）。
4. **快照**：`state.plan_snapshot = await self._emit_plan_snapshot(spec, plan, state.turn_id)`，`origin="activated"`（PlanSnapshot.origin 已有该枚举值位，planning.py `emit_plan_snapshot` 传 `origin=` 参数即可）。
5. 返回新 plan。

下一个迭代自动生效：runner 约 575 行 `apply_plan_step_guidance` 发现 `plan.current_step` → 注入步骤指引。无需新增推进代码。

## 四、边界与守护

1. **单激活一计划**：contextvar 每次读取即复位；模型在同一批工具调用里 activate 两次 → 后者覆盖前者（与 receipts"保留最后一条"同语义），log warning。
2. **FAST 模式兼容**：FAST turn（planning_policy 非 MANAGED、plan 为 None）同样可激活——激活即该 turn 转入受治理的计划推进，这是特性不是漏洞（模型主动请求结构化执行）。`use_planner=False` 的 legacy 配置同理由此获益。
3. **不给模型完成标记权**：activate_plan 只创建 PENDING 状态计划；步骤状态唯一写者仍是 `complete_plan_step`（验收器）。工具 schema 与返回文本不得出现"complete_step / mark_done"类能力。
4. **turn 结束即失效**：`_pending_plan` 未被消费（模型 activate 后直接收尾文本）→ 计划随 turn 丢弃，不跨轮（跨轮恢复属 W2 备忘，明令不做）。

## 五、测试要求

新文件 `tests/agent/test_activate_plan.py`：

**工具层：**
1. 合法 JSON → 返回 "Plan activated"，contextvar 内有 Plan，步骤数为声明的数量。
2. 非法 JSON / 空 steps / 9 步超限 / 非 dict → "Error:" 开头，contextvar 保持 None。
3. evidence_level 归一化复用：`"tool"` / 缺失 / `"none"` → 正确落位。
4. `_scopes = {"core"}`：子代理工具白名单过滤后不含 activate_plan（复用既有 scope 过滤测试设施）。
5. take_pending_plan 复位语义。

**主循环集成（fake provider 驱动完整 turn）：**
6. 激活后下一迭代收到步骤指引（messages 中出现 `[Current Plan Step 1/...`）。
7. 步骤 1 经 fake write_file（真回执）→ 验收通过 → 指引切到步骤 2（W0 管道 + 本工具的端到端闭环）。
8. 模型只发文本不调工具 → 步骤保持 IN_PROGRESS（激活不豁免验收）。
9. 已有 plan 且 `can_replan=False` → 激活被拒，旧 plan 继续推进。
10. 已有 plan 且可 replan → 新 plan 挂载，replan_count 递增，plan_snapshot 以 `origin="activated"` 发出。
11. FAST 模式 turn 激活 → progress_tracker 被补建，步骤推进正常。
12. 激活后旧观察清空：激活前迭代的工具观察不出现在新步骤的验收输入中。

**守护回归：**
13. `delegate_plan` / `execute_plan` 行为与既有测试完全一致（别名未被顺手改动）。

## 六、禁改清单

- `apply_plan_step_guidance` / `complete_plan_step` / 验收器（激活器只产出计划，不碰推进与验收）
- `tools/execute_plan.py`（delegate_plan 语义冻结）
- 主循环的嵌套结构（不得为激活引入第二个循环或回调式步骤执行）
- 审批门 / checkpoint / 预算管道

## 七、验收自检

- [ ] 全量测试绿，ruff 零告警
- [ ] 端到端用例 7（激活 → 回执验收 → 步骤推进）在批次报告中展示执行轨迹摘要
- [ ] 用例 9（replan 预算拒激活）与用例 13（别名回归）通过
- [ ] 与本规格的偏差逐条说明
