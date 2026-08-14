# 自适应三级任务路由设计

**状态：** 待实施
**日期：** 2026-08-14
**适用仓库：** MiniUnicorn
**前置基线：** `main@4e0553db`

## 1. 决策结论

MiniUnicorn 将当前全局静态 `usePlanner` 开关替换为始终启用的、确定性优先的三级任务路由：

```text
用户任务
  ├─ DIRECT：直接进入现有 ReAct
  ├─ GUIDED：注入轻量执行清单指导，再进入现有 ReAct
  └─ PLANNED：先由 Planner 生成正式 Plan，每一步由现有 ReAct 执行
```

初始路由由纯本地规则产生，不增加每轮分类 LLM 调用。执行过程中根据真实工具调用数量、工具域、风险和失败情况只允许向上升级：`DIRECT -> GUIDED -> PLANNED`。不得自动降级，也不得在同一 turn 重复创建多个初始计划。

路由只决定“是否需要规划”，不决定工具是否获得授权。付款、删除、外部发送等操作仍必须服从现有权限和未来独立的人类审批机制。

## 2. 当前问题

当前实现有以下问题：

1. `usePlanner=False` 时所有任务使用 ReAct；`True` 时所有任务都先调用 Planner。
2. 简单问答开启 Planner 会增加一次模型调用、延迟和 token。
3. 复杂任务关闭 Planner 时容易边做边改，没有显式步骤和失败重规划。
4. 任务在执行中变复杂时，Runner 无法动态升级。
5. 工具只有 `read_only/exclusive`，没有统一的风险和业务域元数据。
6. Planner 解析失败会静默生成单步计划，调用者无法区分真正计划和 fallback。
7. `AgentLoopBuilder.from_config()` 没有显式把现有 Planner 配置传入 Loop，配置路径容易出现“配置存在但运行时未使用”。

## 3. 目标

1. 简单任务不增加额外 Planner 模型调用。
2. 中等任务不创建正式 Plan，但获得简短清单、逐步执行和中间验证指导。
3. 复杂任务自动使用现有 Planner + ReAct。
4. 高风险实际工具调用发生前必须先升级到 PLANNED。
5. 初始判定完全确定、可测试、可解释，同一输入得到同一路由和 reason codes。
6. 执行过程中可以基于真实复杂度单向升级。
7. Planner 失败时根据升级原因明确 fail closed 或降级 GUIDED。
8. 子 Agent 不递归创建正式 Plan，避免规划嵌套和成本失控。
9. 路由结果进入结构化日志和 `AgentRunResult`，不记录完整用户原文。
10. 保持现有 ReAct、ContextGovernor、TurnBudget、Reflection、checkpoint 和工具执行语义。

## 4. 非目标

- 不实现付款、删除、发送等操作的人类审批工作流。
- 不用 LLM 对每条用户消息做复杂度分类。
- 不让 TaskRouter 生成业务步骤或调用工具。
- 不自动调用 `execute_plan` 或自动创建子 Agent。
- 不改变 Planner 每一步复用 ReAct 的架构。
- 不根据用户身份、公司规模或历史成功率偷偷调整阈值。
- 不引入可学习分类器、embedding 或向量数据库。
- 不保留 `usePlanner=true/false` 两套长期行为。

## 5. 方案对比

### 方案 A：保持全局布尔开关

实现最简单，但用户必须理解内部架构并手动选择；不能按任务切换，也不能动态升级。

### 方案 B：每轮调用一个 LLM 分类器

语义判断更灵活，但每个简单请求都增加延迟、成本和新的模型失败点；同一任务可能因模型随机性得到不同路由。

### 方案 C：确定性初始评分 + 运行时真实信号升级（采用）

简单任务零额外模型调用；复杂任务才支付 Planner 成本；判定可解释且可通过真实工具行为纠正初始低估。

## 6. 核心模型

新增 `miniunicorn/agent/task_router.py`：

```python
class TaskRoute(str, Enum):
    DIRECT = "direct"
    GUIDED = "guided"
    PLANNED = "planned"


class RouteReason(str, Enum):
    EXPLICIT_PLAN = "explicit_plan"
    MULTI_STEP = "multi_step"
    DEPENDENCY_CHAIN = "dependency_chain"
    RESEARCH_COMPARE = "research_compare"
    BATCH_OPERATION = "batch_operation"
    APPROVAL_OR_WAIT = "approval_or_wait"
    PARALLEL_WORK = "parallel_work"
    LONG_RUNNING = "long_running"
    HIGH_RISK_INTENT = "high_risk_intent"
    MANY_TOOL_CALLS = "many_tool_calls"
    MULTI_DOMAIN = "multi_domain"
    REPEATED_FAILURE = "repeated_failure"
    HIGH_RISK_TOOL = "high_risk_tool"


@dataclass(frozen=True, slots=True)
class TaskRouteDecision:
    route: TaskRoute
    score: int
    reasons: tuple[RouteReason, ...]
    forced: bool = False
    upgraded_from: TaskRoute | None = None
```

`reasons` 按枚举固定顺序去重，日志和测试不得依赖正则命中顺序。

运行时状态：

```python
@dataclass(slots=True)
class RoutingRuntimeState:
    decision: TaskRouteDecision
    iterations: int = 0
    executed_tool_calls: int = 0
    tool_errors: int = 0
    tool_domains: set[str] = field(default_factory=set)
    dynamic_plan_attempted: bool = False
```

## 7. 初始评分

TaskRouter 从最后一条非空 user message 提取信号。输入先做 NFKC、casefold 和空白折叠；不读取记忆、不调用模型、不访问网络。

| 信号 | 分值 | 说明 |
|---|---:|---|
| 明确要求先规划/分步骤执行 | 强制 PLANNED | `先规划`、`制定计划`、`plan first` 等 |
| 三项以上编号/项目动作 | +2 | `1.`、`2.`、`3.` 或连续动作条目 |
| 两个以上依赖连接 | +2 | `然后`、`之后`、`基于结果`、`if...then` 等 |
| 搜索并比较多个来源 | +2 | 调研、对比、交叉验证、综合多个来源 |
| 批量/全部/逐个处理 | +1 | 不因单独出现“所有”就判高风险 |
| 等待、审批、确认后继续 | +2 | 说明存在阶段边界 |
| 明确要求并行处理 | +1 | 并行、同时分别处理 |
| 监控、定时、持续运行 | +2 | 长时间状态变化 |
| 高风险动作意图 | +3 | 付款、退款、删除、外发、发布、部署、权限、客户数据导出 |

高风险意图只有在动作语境中计分。`解释如何删除`、`不要发送`、`模拟付款流程` 等否定、讨论或模拟语境不得作为强制条件；即使文本漏判，真实高风险工具调用仍会在执行前强制升级。

阈值固定：

```text
0..2  -> DIRECT
3..5  -> GUIDED
>=6   -> PLANNED
```

阈值和权重本阶段不暴露为用户配置，避免产生难以支持的规则组合。只允许配置 Planner 模型和最大 replan 次数。

## 8. 三条执行路径

### 8.1 DIRECT

完全复用当前 ReAct 循环，不注入额外提示，不调用 Planner。简单问答和单工具查询的 prompt、调用次数和结果保持基线兼容。

### 8.2 GUIDED

不调用 Planner，不创建 `Plan`。Context governance 完成后，Runner 非持久化地向当前 user message 追加固定指导：

```text
[Execution mode: guided]
Maintain a short private checklist of the requested outcomes. Execute dependent
actions in order, verify intermediate results, and do not finish until every
requested outcome is covered. Do not reveal the checklist unless asked.
```

该文字不得写入 session history，只进入本次模型输入。每个 iteration 可以注入，但必须保证只出现一次。

### 8.3 PLANNED

复用当前 `Planner.create_plan()`、step guidance、失败 replan 和 ReAct 工具循环。TaskRouter 不自动调用 `execute_plan`；是否委托子 Agent仍由模型和现有工具规则决定。

Planner 结果必须显式区分：

- `valid_plan`：解析出合法的1..20个原子步骤；
- `fallback_plan`：模型失败、JSON 无效或步骤非法时的单步 fallback；
- `error_code`：稳定错误类别，不包含原始模型内容。

公开返回类型固定为：

```python
@dataclass(frozen=True, slots=True)
class PlanBuildResult:
    plan: Plan
    fallback_plan: bool
    error_code: str | None = None
```

`Planner.create_plan()` 和 `Planner.replan()` 都返回 `PlanBuildResult`，Runner 不得再通过步骤数量猜测是否发生 fallback。

## 9. 工具调用画像

在 `Tool` 基类增加：

```python
class ToolRisk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class ToolCallProfile:
    name: str
    domain: str
    risk: ToolRisk
    known: bool


@property
def domain(self) -> str:
    return "general"


def risk_for(self, params: dict[str, Any]) -> ToolRisk:
    return ToolRisk.LOW if self.read_only else ToolRisk.MEDIUM
```

首批内置工具覆盖：

| 工具 | domain | 风险规则 |
|---|---|---|
| read/list/grep/find/web search/fetch | filesystem/web | LOW |
| write/edit/apply_patch/image generation | filesystem/media | MEDIUM |
| exec | shell | destructive guard 命中 HIGH，否则 MEDIUM |
| message | communication | HIGH |
| cron list | scheduling | LOW |
| cron add/remove | scheduling | HIGH |
| spawn/delegate/execute_plan | delegation | MEDIUM |
| self/config 修改 | configuration | HIGH |
| MCP readOnlyHint=true | `mcp:<server>` | LOW |
| MCP destructiveHint=true | `mcp:<server>` | HIGH |
| MCP 未声明 | `mcp:<server>` | MEDIUM |

工具风险元数据只供路由和日志使用；不得绕过 Tool 自己的安全 guard。

## 10. 动态升级规则

Runner 在执行工具前调用 `TaskRouter.evaluate_runtime()`。升级只允许向上：

### DIRECT -> GUIDED

满足任一条件：

- 当前响应提出至少3个工具调用；
- 已执行工具调用达到3次且已进入第3个 iteration；
- 当前/累计涉及至少2个工具 domain。

升级到 GUIDED 不丢弃当前工具调用；工具正常执行，下一次模型调用开始注入 guided 指导。

### DIRECT/GUIDED -> PLANNED

满足任一条件：

- 当前待执行工具中存在 HIGH risk；
- 已执行工具调用加当前拟执行调用达到5次，且涉及至少2个 domain；
- 同一 turn 出现2次工具错误；
- 初始 route 已是 PLANNED。

动态升级发生在工具执行前。HIGH risk 导致升级时，当前工具调用不得执行或写入持久消息；Runner 先创建“剩余任务计划”，下一 iteration 重新让模型决定工具调用。

`executed_tool_calls` 只在工具真正执行后增加。触发 PLANNED 而被丢弃的拟调用不计入累计数。工具结果产生第二次错误时，Runner 在再次请求模型之前立即评估 `REPEATED_FAILURE` 并尝试创建剩余任务计划。

从已执行进度升级时，Planner 输入增加最多4000字符的确定性进度摘要：已用工具名、成功/失败状态和截断结果摘要；摘要标记为数据，不作为指令。不得把完整敏感工具输出放进 Planner prompt。

每个 turn 最多尝试一次动态创建正式 Plan。Plan 创建成功后由现有 `Plan.current_step` 驱动，不再重新做初始分类。

## 11. Planner 失败策略

| 进入 PLANNED 原因 | Planner 失败后的行为 |
|---|---|
| 用户明确要求规划 | `routing_failed`，不偷偷直接执行 |
| HIGH risk 实际工具调用 | `routing_failed`，高风险调用不执行 |
| 初始分数 >=6 | 降级 GUIDED，记录 `planner_fallback` |
| 多工具/多域动态升级 | 降级 GUIDED，继续 ReAct |
| 重复工具失败升级 | `plan_failed`，避免继续盲目重试 |

Planner 返回合法单步计划不算失败；只有 `fallback_plan=True` 才触发上表。

## 12. 配置和运行时接线

删除：

- `AgentDefaults.use_planner`
- 顶层 `planner_model`
- 顶层 `planner_max_replans`
- `AgentLoop.use_planner`
- `AgentRunSpec.use_planner`

新增：

```python
class TaskRoutingConfig(Base):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    planner_model: str | None = None
    planner_max_replans: int = Field(default=3, ge=0, le=10)
```

配置：

```json
{
  "agents": {
    "defaults": {
      "taskRouting": {
        "plannerModel": null,
        "plannerMaxReplans": 3
      }
    }
  }
}
```

Router 始终启用，没有 `enabled/mode/strategy`。`AgentLoopBuilder.from_config()` 必须显式传递 `defaults.task_routing`，不能在 Loop 内重新构造 `AgentDefaults()` 读取用户设置。

`AgentRunSpec` 新增 `routing_policy: TaskRoutingPolicy | None = None`。顶层 AgentLoop 始终传 policy；独立 Runner 单元测试的 `None` 保持 DIRECT；SubagentManager 传 `TaskRoutingPolicy.subagent()`，把任何初始或动态 PLANNED 决定明确 clamp 为 GUIDED，保留原 score/reasons 但 `forced=False`，不创建嵌套 Plan。

## 13. Checkpoint、结果和可观测性

`AgentRunResult` 增加：

```python
routing: TaskRouteDecision | None = None
```

checkpoint payload 增加：

```json
{
  "routing": {
    "route": "guided",
    "score": 4,
    "reasons": ["multi_step", "batch_operation"],
    "forced": false,
    "upgraded_from": "direct"
  }
}
```

结构化日志：

- `task_routed`
- `task_route_upgraded`
- `task_planner_started/completed/failed`
- `task_routing_failed`

日志只保存 route、score、reason codes、计数、模型和耗时；不保存完整用户请求或工具结果。

## 14. 安全边界

1. Route 不是授权。PLANNED 不意味着可以执行高风险工具。
2. HIGH risk 调用必须在执行前完成升级；不能“先执行再补计划”。
3. 被丢弃的 pre-upgrade tool call 不得进入持久消息，防止 orphan tool call。
4. Planner progress summary 中的工具结果使用 data delimiters，并限制4000字符。
5. 未知 MCP 写工具默认 MEDIUM；明确 destructive 才 HIGH。
6. Router 规则不得读取用户长期记忆来推断风险，避免不同用户同一任务得到不可解释结果。
7. Router 失败时默认 DIRECT；但已识别 HIGH risk 或明确规划请求时 fail closed。

## 15. 测试矩阵

### 15.1 初始分类

- 中英文简单查询为 DIRECT；
- 三项步骤、依赖、研究、批量、审批、并行、长期任务分别计分；
- 边界分数2/3/5/6准确；
- 明确规划强制 PLANNED；
- 否定、解释、模拟高风险语句不误强制；
- 规范化后结果稳定；reason 顺序稳定。

### 15.2 工具画像

- read-only 默认 LOW，未知 mutation 默认 MEDIUM；
- message、cron add/remove、破坏性 shell 为 HIGH；
- MCP annotations 正确映射；
- 画像不改变工具原有执行和并发行为。

### 15.3 Runner

- DIRECT 与旧 ReAct prompt/调用数一致；
- GUIDED 零 Planner 调用，指导不持久化且不重复；
- PLANNED 创建一次 Plan并逐步执行；
- 动态升级单向且每 turn 最多一次正式计划尝试；
- HIGH risk 调用在计划前零执行；
- 丢弃调用不产生 orphan message；
- Planner 失败矩阵逐项验证；
- TurnBudget、ContextGovernor、Reflection 和 injection 继续生效。

### 15.4 接线和配置

- `taskRouting` camelCase 解析；未知字段拒绝；
- 旧 `usePlanner/plannerModel/plannerMaxReplans` 顶层字段硬报错；
- builder 把实际配置传入 Loop；
- 顶层 Agent 自动路由；子 Agent 不创建嵌套 Plan；
- checkpoint 和结果包含脱敏 routing 数据。

## 16. 性能目标

- DIRECT 分类 p95 小于1ms（1000字符以内任务，普通开发机）；
- DIRECT 不增加 LLM 调用和网络调用；
- GUIDED 不增加 LLM 调用，只增加固定短提示；
- 只有 PLANNED 增加 Planner 调用；
- 规则匹配复杂度与任务文本长度线性相关，输入最多分析前16,000字符；
- 路由 reason 日志不得显著增加 checkpoint 体积。

时间指标放入 benchmark，不作为共享 CI 的硬 wall-clock 断言。

## 17. 发布策略

1. 单路径直接替换静态 `usePlanner`；项目仍在开发阶段，不保留新老模式。
2. 先完成纯分类和工具画像，不接入 Runner。
3. 再接入初始路由，验证 DIRECT 基线完全不变。
4. 接入 GUIDED，然后接入 PLANNED 和动态升级。
5. 最后删除旧开关和旧注释，更新文档。
6. 若 HIGH risk pre-execution gate 无法证明，禁止合并。

## 18. 完成定义

1. 顶层 Agent 每 turn 产生且只产生一个初始路由决定。
2. 简单任务保持纯 ReAct 且零额外 Planner 调用。
3. 中等任务使用非持久化 guided 指导。
4. 复杂任务使用正式 Plan + 每步 ReAct。
5. 动态升级只能 DIRECT -> GUIDED -> PLANNED。
6. HIGH risk 工具在计划完成前绝不执行。
7. Planner 失败按原因执行明确矩阵，不静默误报成功。
8. 子 Agent 不产生嵌套正式 Plan。
9. `usePlanner` 旧开关和运行时代码全部删除。
10. 配置、日志、checkpoint 和文档一致。
11. 全量 Python、WebUI、Ruff、compileall 和 diff check 通过。
