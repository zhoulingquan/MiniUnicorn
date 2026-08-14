# Adaptive Three-Tier Task Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用始终启用的确定性三级路由替换全局 `usePlanner` 开关，使简单任务直接 ReAct、中等任务使用轻量 guided checklist、复杂或运行中升级的任务使用 Planner + ReAct。

**Architecture:** 新增纯本地 `TaskRouter` 负责初始评分和单向动态升级；新增 Tool domain/risk 元数据提供运行时真实信号。Runner 保留唯一 ReAct 执行循环，GUIDED 只注入非持久化固定指导，PLANNED 复用现有 Planner 和 step guidance。路由不调用分类 LLM、不自动创建子 Agent，也不替代权限审批。

**Tech Stack:** Python 3.11+、dataclasses/Enum/re 标准库、现有 Pydantic 2 配置、现有 AgentRunner/Planner/ToolRegistry、pytest、Ruff。

## Global Constraints

- 设计规格：`docs/superpowers/specs/2026-08-14-adaptive-three-tier-task-routing-design.md`。
- 基线：`main@4e0553db`；实施在独立 worktree/feature branch 中完成。
- 顶层 Router 始终启用；不得新增 enabled/mode/strategy 或保留 `usePlanner` 双路径。
- DIRECT 分数0..2，GUIDED分数3..5，PLANNED分数>=6；权重和阈值本阶段不配置化。
- 初始分类不得调用 LLM、网络、记忆或外部服务，最多分析规范化后的前16,000字符。
- 动态路由只能 `DIRECT -> GUIDED -> PLANNED`，每 turn 最多一次动态正式计划尝试。
- HIGH risk 实际工具调用必须在执行前完成 PLANNED 升级；被丢弃调用不得持久化。
- Route 不授予工具权限，也不替代付款、删除、外发等操作的确认机制。
- GUIDED 不调用 Planner，不持久化指导文字。
- PLANNED 不自动调用 `execute_plan`，每个计划步骤继续由当前 ReAct loop 执行。
- 子 Agent 最多使用 GUIDED，不创建嵌套正式 Plan。
- Planner model 输出和工具结果不得原样写入 routing 日志。
- 保持 ContextGovernor、TurnBudget、Reflection、checkpoint、injection 和工具安全 guard 的现有语义。
- 每个 Task 使用 TDD，单独提交，不做无关重构。

---

## File Map

### 新建

- `miniunicorn/agent/task_router.py`：路由枚举、reason、policy、runtime state、初始评分和升级判定。
- `tests/agent/test_task_router.py`：中英文评分、边界、规范化和升级规则。
- `tests/agent/test_tool_risk_profiles.py`：内置/MCP工具 domain 和风险画像。
- `tests/agent/test_planner.py`：Planner 严格解析和显式 fallback 结果。
- `tests/agent/test_runner_task_routing.py`：DIRECT/GUIDED/PLANNED 与动态升级集成。
- `scripts/benchmark_task_router.py`：纯分类微基准，不设共享 CI 时间硬门槛。

### 修改

- `miniunicorn/agent/tools/base.py`：`ToolRisk`、`Tool.domain`、`Tool.risk_for()`。
- `miniunicorn/agent/tools/registry.py`：安全解析工具调用画像。
- `miniunicorn/agent/tools/filesystem.py`
- `miniunicorn/agent/tools/apply_patch.py`
- `miniunicorn/agent/tools/shell.py`
- `miniunicorn/agent/tools/message.py`
- `miniunicorn/agent/tools/cron.py`
- `miniunicorn/agent/tools/spawn.py`
- `miniunicorn/agent/tools/delegate.py`
- `miniunicorn/agent/tools/execute_plan.py`
- `miniunicorn/agent/tools/self.py`
- `miniunicorn/agent/tools/mcp.py`
- `miniunicorn/agent/planner.py`
- `miniunicorn/agent/runner.py`
- `miniunicorn/agent/loop.py`
- `miniunicorn/agent/loop_builder.py`
- `miniunicorn/agent/subagent.py`
- `miniunicorn/config/schema.py`
- `tests/config/test_config_boundaries.py`
- `tests/agent/test_loop_runner_integration.py`
- `tests/agent/test_runner_core.py`
- `tests/agent/test_runner_persistence.py`
- `tests/agent/test_runner_governance.py`
- `tests/agent/test_runner_reflection.py`
- `tests/agent/test_runner_injections.py`
- `docs/configuration.md`
- `docs/task-routing.md`
- `README.md`

---

### Task 1: 实现确定性初始评分模型

**Files:**
- Create: `miniunicorn/agent/task_router.py`
- Create: `tests/agent/test_task_router.py`

**Interfaces:**
- Produces: `TaskRoute`、`RouteReason`、`TaskRouteDecision`、`RoutingRuntimeState`、`TaskRoutingPolicy`、`TaskRouter.classify(task)`。
- Consumes: 后续 Runner 只使用公开模型和方法，不读取 router 私有正则。

- [ ] **Step 1: 写路由模型和分数边界失败测试**

```python
@pytest.mark.parametrize(
    ("task", "route", "score"),
    [
        ("查询今天的订单", TaskRoute.DIRECT, 0),
        ("批量列出所有逾期订单", TaskRoute.DIRECT, 1),
        ("查找资料并对比三个供应商", TaskRoute.DIRECT, 2),
        ("批量处理订单，完成后等待我确认", TaskRoute.GUIDED, 3),
        ("调研并对比来源，然后根据结果逐个处理并并行核验", TaskRoute.PLANNED, 6),
    ],
)
def test_initial_route_boundaries(task, route, score):
    decision = TaskRouter().classify(task)
    assert decision.route is route
    assert decision.score == score
```

每个测试句只命中表中对应的固定信号；实现不得用未写入规格的隐含关键词改变分数。

- [ ] **Step 2: 写强制规划、否定和规范化测试**

覆盖：

```python
def test_explicit_plan_forces_planned() -> None:
    result = TaskRouter().classify("请先制定计划，再开始执行")
    assert result.route is TaskRoute.PLANNED
    assert result.forced is True
    assert result.reasons == (RouteReason.EXPLICIT_PLAN,)


@pytest.mark.parametrize("task", ["解释如何删除订单", "不要发送邮件", "模拟付款流程"])
def test_discussion_negation_and_simulation_do_not_force_high_risk(task: str) -> None:
    result = TaskRouter().classify(task)
    assert RouteReason.HIGH_RISK_INTENT not in result.reasons
```

再断言全角标点、大小写、连续空白和 NFKC 等价输入得到完全相同 decision。

- [ ] **Step 3: 运行并确认失败**

Run: `pytest tests/agent/test_task_router.py -q`
Expected: FAIL，模块不存在。

- [ ] **Step 4: 实现公开数据模型**

```python
class TaskRoute(str, Enum):
    DIRECT = "direct"
    GUIDED = "guided"
    PLANNED = "planned"


@dataclass(frozen=True, slots=True)
class TaskRouteDecision:
    route: TaskRoute
    score: int
    reasons: tuple[RouteReason, ...]
    forced: bool = False
    upgraded_from: TaskRoute | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "route": self.route.value,
            "score": self.score,
            "reasons": [reason.value for reason in self.reasons],
            "forced": self.forced,
            "upgraded_from": self.upgraded_from.value if self.upgraded_from else None,
        }
```

`TaskRoutingPolicy` 固定阈值并带 `planner_model`、`planner_max_replans`、`allow_planned`；`subagent()` 返回 `allow_planned=False`。当 policy 禁止 PLANNED 时，初始和动态 PLANNED 都明确 clamp 为 GUIDED，保留 score/reasons、设置 `forced=False`，并用测试证明 Planner 零构造。

- [ ] **Step 5: 实现规范化和规则表**

使用预编译 regex 常量，规则函数每类最多返回一个 reason。最终 score 按规格 §7 固定；reasons 按 `RouteReason` 定义顺序输出。任务先执行：

```python
normalized = unicodedata.normalize("NFKC", task).casefold()
normalized = re.sub(r"\s+", " ", normalized).strip()[:16_000]
```

高风险意图必须同时命中 action regex，且不命中讨论/否定窗口；把这些模式作为命名常量和参数化测试维护，不在 `classify()` 中堆砌匿名字符串。

- [ ] **Step 6: 写空任务和超长任务测试**

空/纯空白返回 DIRECT 0分；20,000字符输入只分析前16,000字符；不得抛异常或产生网络调用。

- [ ] **Step 7: 运行测试和 Ruff**

Run: `pytest tests/agent/test_task_router.py -q`
Expected: PASS。
Run: `ruff check miniunicorn/agent/task_router.py tests/agent/test_task_router.py`
Expected: PASS。

- [ ] **Step 8: 提交**

```bash
git add miniunicorn/agent/task_router.py tests/agent/test_task_router.py
git commit -m "feat(agent): add deterministic three-tier task classifier"
```

---

### Task 2: 增加工具 domain 和风险画像

**Files:**
- Modify: `miniunicorn/agent/tools/base.py`
- Modify: `miniunicorn/agent/tools/registry.py`
- Modify: `miniunicorn/agent/tools/filesystem.py`
- Modify: `miniunicorn/agent/tools/apply_patch.py`
- Modify: `miniunicorn/agent/tools/shell.py`
- Modify: `miniunicorn/agent/tools/message.py`
- Modify: `miniunicorn/agent/tools/cron.py`
- Modify: `miniunicorn/agent/tools/spawn.py`
- Modify: `miniunicorn/agent/tools/delegate.py`
- Modify: `miniunicorn/agent/tools/execute_plan.py`
- Modify: `miniunicorn/agent/tools/self.py`
- Modify: `miniunicorn/agent/tools/mcp.py`
- Create: `tests/agent/test_tool_risk_profiles.py`

**Interfaces:**
- Produces: `ToolRisk`、`ToolCallProfile`、`Tool.domain`、`Tool.risk_for(params)`、`ToolRegistry.profile_call(name, params)`。
- Consumes: Task 5/7 在执行前使用 profile；画像不得调用 tool.execute。

- [ ] **Step 1: 写默认画像失败测试**

```python
def test_default_tool_risk_uses_read_only() -> None:
    assert ReadOnlyFakeTool().risk_for({}) is ToolRisk.LOW
    assert MutatingFakeTool().risk_for({}) is ToolRisk.MEDIUM


def test_registry_profiles_unknown_tool_without_execution() -> None:
    registry = ToolRegistry()
    profile = registry.profile_call("missing", {"secret": "not-logged"})
    assert profile.name == "missing"
    assert profile.domain == "unknown"
    assert profile.risk is ToolRisk.MEDIUM
    assert profile.known is False
```

- [ ] **Step 2: 写内置工具矩阵失败测试**

至少覆盖规格 §9 表中的每一类。明确断言 `cron list=LOW`、`cron add/remove=HIGH`、message=HIGH、write/edit=MEDIUM、read/search=LOW、spawn/delegate=MEDIUM。

- [ ] **Step 3: 写 shell 和 MCP 参数化风险测试**

shell 只做风险画像，不改变现有 guard：普通 `Get-ChildItem`/`ls` 为 MEDIUM；包含仓库外递归删除、权限修改或生产部署动词的命令为 HIGH。MCP annotation：`readOnlyHint=True` 为 LOW，`destructiveHint=True` 为 HIGH，缺失为 MEDIUM；不得把参数值写进 profile repr/log。

- [ ] **Step 4: 运行并确认失败**

Run: `pytest tests/agent/test_tool_risk_profiles.py -q`
Expected: FAIL，缺少 `ToolRisk/profile_call`。

- [ ] **Step 5: 实现基础接口**

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


class Tool(ABC):
    @property
    def domain(self) -> str:
        return "general"

    def risk_for(self, params: dict[str, Any]) -> ToolRisk:
        return ToolRisk.LOW if self.read_only else ToolRisk.MEDIUM
```

`ToolRegistry.profile_call()` 必须先调用现有 `prepare_call()` 做安全 cast/validation；参数无效时仍返回已知工具的保守风险，不能执行工具，也不能在错误日志包含完整 params。

- [ ] **Step 6: 为内置和 MCP wrapper 增加明确 override**

只覆盖 domain/risk，不改变 `read_only/concurrency_safe/exclusive`。MCP server domain 从 wrapper 已有 server/name 元数据稳定派生；无法解析时使用 `mcp:unknown`。

- [ ] **Step 7: 运行工具画像和既有工具测试**

Run: `pytest tests/agent/test_tool_risk_profiles.py tests/agent/test_runner_tool_execution.py tests/agent/test_runner_safety.py -q`
Expected: PASS。

- [ ] **Step 8: 提交**

```bash
git add miniunicorn/agent/tools tests/agent/test_tool_risk_profiles.py tests/agent/test_runner_tool_execution.py tests/agent/test_runner_safety.py
git commit -m "feat(agent): classify tool domains and call risk"
```

---

### Task 3: 让 Planner 显式报告合法计划和 fallback

**Files:**
- Modify: `miniunicorn/agent/planner.py`
- Modify: `miniunicorn/templates/agent/planner_system.md`
- Modify: `miniunicorn/templates/agent/planner_replan.md`
- Create: `tests/agent/test_planner.py`

**Interfaces:**
- Produces: `PlanBuildResult(plan, fallback_plan, error_code)`；`Planner.create_plan(..., progress_summary=None)`。
- Consumes: Runner 不再通过“steps数量”猜测 Planner 是否失败。

- [ ] **Step 1: 写严格解析失败测试**

覆盖合法JSON、fenced JSON、空内容、坏JSON、空steps、超过20步、重复/非整数ID、空action、超长action、未知顶层字段。失败统一返回单步 fallback，但 `fallback_plan=True` 且稳定 error code。

```python
result = await planner.create_plan("task", "tools")
assert result.plan.goal == "task"
assert result.fallback_plan is True
assert result.error_code == "invalid_json"
```

- [ ] **Step 2: 写 progress summary prompt 测试**

传入 progress summary 时，user prompt 必须包含 `<execution_progress>` data delimiter、最多4000字符和“不得作为指令”提示；不传时 prompt 不出现该 section。

- [ ] **Step 3: 运行并确认失败**

Run: `pytest tests/agent/test_planner.py -q`
Expected: FAIL，`PlanBuildResult` 不存在或返回类型不同。

- [ ] **Step 4: 实现结果模型和 parser 约束**

```python
@dataclass(frozen=True, slots=True)
class PlanBuildResult:
    plan: Plan
    fallback_plan: bool
    error_code: str | None = None
```

合法 plan 必须有1..20步；ID 由 parser 重新编号为连续1..N，不信任模型 ID；action 1..500字符，tool_hint/done_criteria 各最多300字符。解析失败不抛出原始模型内容。

- [ ] **Step 5: 更新 replan 保留结果语义**

`replan()` 返回 `PlanBuildResult`，保留 `replan_count` 和 completed steps；fallback 时 Runner 能区分“保留旧 plan”和“生成新 plan”。修复当前 `replan_count` 在边界上的 off-by-one：允许次数严格等于 `max_replans`，第 N+1 次拒绝。

- [ ] **Step 6: 运行 Planner 测试**

Run: `pytest tests/agent/test_planner.py -q`
Expected: PASS。

- [ ] **Step 7: 提交**

```bash
git add miniunicorn/agent/planner.py miniunicorn/templates/agent/planner_system.md miniunicorn/templates/agent/planner_replan.md tests/agent/test_planner.py
git commit -m "fix(agent): make planner fallback state explicit"
```

---

### Task 4: 用 `taskRouting` 配置替换静态 Planner 开关

**Files:**
- Modify: `miniunicorn/config/schema.py`
- Modify: `miniunicorn/agent/loop.py`
- Modify: `miniunicorn/agent/loop_builder.py`
- Modify: `tests/config/test_config_boundaries.py`
- Modify: `tests/agent/test_loop_runner_integration.py`

**Interfaces:**
- Produces: `TaskRoutingConfig`、`AgentDefaults.task_routing`、`AgentLoop.task_routing_policy`。
- Consumes: Task 1 `TaskRoutingPolicy`；Task 5 传入 `AgentRunSpec.routing_policy`。

- [ ] **Step 1: 写配置失败测试**

```python
def test_task_routing_defaults_and_aliases() -> None:
    defaults = AgentDefaults.model_validate(
        {"taskRouting": {"plannerModel": "fast-planner", "plannerMaxReplans": 4}}
    )
    assert defaults.task_routing.planner_model == "fast-planner"
    assert defaults.task_routing.planner_max_replans == 4


@pytest.mark.parametrize("old", [
    {"usePlanner": True},
    {"plannerModel": "x"},
    {"plannerMaxReplans": 3},
])
def test_old_top_level_planner_fields_are_rejected(old) -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        AgentDefaults.model_validate(old)
```

再覆盖 max replans -1/11 和 taskRouting 未知字段。

- [ ] **Step 2: 运行并确认失败**

Run: `pytest tests/config/test_config_boundaries.py -q`
Expected: FAIL，`task_routing` 不存在或旧字段仍被接受。

- [ ] **Step 3: 实现配置模型并删除旧字段**

```python
class TaskRoutingConfig(Base):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    planner_model: str | None = None
    planner_max_replans: int = Field(default=3, ge=0, le=10)


class AgentDefaults(Base):
    task_routing: TaskRoutingConfig = Field(default_factory=TaskRoutingConfig)
```

删除 `use_planner/planner_model/planner_max_replans` 顶层字段，不写兼容迁移。

- [ ] **Step 4: 显式接入 Builder 和 Loop**

`AgentLoop.__init__` 增加 `task_routing_config: TaskRoutingConfig | None = None`，构造：

```python
config = task_routing_config or TaskRoutingConfig()
self.task_routing_policy = TaskRoutingPolicy(
    planner_model=config.planner_model,
    planner_max_replans=config.planner_max_replans,
)
```

`AgentLoopBuilder` 增加 `with_task_routing_config()`，`from_config()` 必须调用 `builder.with_task_routing_config(defaults.task_routing)`。删除 Loop 内用新 `AgentDefaults()` 读取 Planner 设置的代码。

- [ ] **Step 5: 写 Builder 真实配置传递测试**

用非默认 planner model/replans 构建 Loop，断言 policy 收到实际值；这项测试必须失败于旧实现的“配置模型存在但 builder 未传递”问题。

- [ ] **Step 6: 运行配置和 Loop 接线测试**

Run: `pytest tests/config/test_config_boundaries.py tests/agent/test_loop_runner_integration.py -q`
Expected: PASS。

- [ ] **Step 7: 提交**

```bash
git add miniunicorn/config/schema.py miniunicorn/agent/loop.py miniunicorn/agent/loop_builder.py tests/config/test_config_boundaries.py tests/agent/test_loop_runner_integration.py
git commit -m "refactor(agent): replace planner toggle with adaptive routing config"
```

---

### Task 5: 接入初始 DIRECT/PLANNED 路由

**Files:**
- Modify: `miniunicorn/agent/runner.py`
- Create: `tests/agent/test_runner_task_routing.py`
- Modify: `tests/agent/test_runner_core.py`
- Modify: `tests/agent/test_runner_persistence.py`

**Interfaces:**
- Consumes: `TaskRouter`、`TaskRoutingPolicy`、`PlanBuildResult`。
- Produces: `AgentRunSpec.routing_policy`、`AgentRunResult.routing`；DIRECT零 Planner，PLANNED复用现有 step loop。

- [ ] **Step 1: 写 DIRECT 零开销失败测试**

构造简单 user task、policy 和 mocked provider：

```python
result = await runner.run(
    AgentRunSpec(
        initial_messages=[{"role": "user", "content": "查询今天订单"}],
        tools=registry,
        model="test-model",
        max_iterations=3,
        max_tool_result_chars=1000,
        routing_policy=TaskRoutingPolicy(),
    )
)
assert result.routing.route is TaskRoute.DIRECT
assert provider.chat_with_retry.await_count == 1
assert all("Execution mode" not in m.get("content", "") for m in provider_calls())
```

Planner 用 patch 断言从未构造。

- [ ] **Step 2: 写初始 PLANNED 失败测试**

明确规划请求必须先调用 Planner，再按 step guidance 执行；断言 Planner 只创建一次，`result.plan` 和 `result.routing` 都存在，最终输出来自最后步骤。

- [ ] **Step 3: 写初始 Planner fallback 矩阵测试**

- explicit plan + fallback => `stop_reason="routing_failed"`，主工具零执行；
- score>=6 + fallback => route 更新为 GUIDED，继续一次普通模型调用；
- 合法单步 plan => 仍是 PLANNED，不当成失败。

- [ ] **Step 4: 运行并确认失败**

Run: `pytest tests/agent/test_runner_task_routing.py -q -x`
Expected: FAIL，`routing_policy/routing` 不存在。

- [ ] **Step 5: 修改 run spec/result 和初始化顺序**

```python
@dataclass(slots=True)
class AgentRunSpec:
    # existing fields...
    routing_policy: TaskRoutingPolicy | None = None


@dataclass(slots=True)
class AgentRunResult:
    # existing fields...
    routing: TaskRouteDecision | None = None
```

`run()` 在任何 Planner/Reflection 初始化前：提取 task；policy 为 None 时创建 DIRECT decision 但保持旧单元测试零额外行为；policy 非 None 时调用 `TaskRouter(policy).classify(task)`。创建 `RoutingRuntimeState`，后续所有返回路径携带同一个最终 decision。

- [ ] **Step 6: 抽取 `_build_initial_plan()`**

不要继续把 Planner 初始化堆在 `run()` 主体。新增：

```python
async def _build_initial_plan(
    self,
    spec: AgentRunSpec,
    decision: TaskRouteDecision,
    task: str,
    tools_summary: str,
) -> tuple[Planner | None, Plan | None, str | None]:
    ...
```

第三项是稳定 failure code。只在 PLANNED 时构造 Planner。根据 explicit/score fallback 矩阵返回 routing failure 或 GUIDED downgrade；不得捕获后无条件静默 ReAct。

- [ ] **Step 7: 保持旧 Plan step 执行但适配 `PlanBuildResult`**

现有 step guidance、mark completed、replan 逻辑继续使用；replan fallback 必须保留失败步骤和 stop reason，不能把 fallback 单步误报为成功重规划。

- [ ] **Step 8: 运行路由、核心和持久化测试**

Run: `pytest tests/agent/test_runner_task_routing.py tests/agent/test_runner_core.py tests/agent/test_runner_persistence.py -q`
Expected: PASS；所有未传 policy 的旧 tests 结果保持不变。

- [ ] **Step 9: 提交**

```bash
git add miniunicorn/agent/runner.py tests/agent/test_runner_task_routing.py tests/agent/test_runner_core.py tests/agent/test_runner_persistence.py
git commit -m "feat(agent): route simple and complex turns automatically"
```

---

### Task 6: 实现 GUIDED 非持久化微计划路径

**Files:**
- Modify: `miniunicorn/agent/runner.py`
- Modify: `tests/agent/test_runner_task_routing.py`
- Modify: `tests/agent/test_runner_governance.py`
- Modify: `tests/agent/test_runner_injections.py`

**Interfaces:**
- Produces: `_inject_guided_guidance(messages)`；GUIDED 只影响本次 `messages_for_model`。
- Consumes: ContextGovernor 输出；不得修改持久 `messages`。

- [ ] **Step 1: 写 GUIDED 零 Planner 测试**

中等任务 route=GUIDED；patch Planner 构造器抛异常以证明它不被调用；provider 收到固定指导且模型调用总数与同等 ReAct 迭代一致。

- [ ] **Step 2: 写不持久化和不重复测试**

模型连续两轮调用工具，捕获两次 provider messages：每次最后 user message中指导恰好出现一次；`AgentRunResult.messages`、session append 和 checkpoint assistant/tool messages 中指导出现0次。

- [ ] **Step 3: 写 ContextGovernor/injection 交互测试**

governor 删除/压缩历史后，guided guidance 追加在治理后的最后 user message；pending injection 到来后下一 iteration 仍只出现一次；不得修改 injected message 原文。

- [ ] **Step 4: 运行并确认失败**

Run: `pytest tests/agent/test_runner_task_routing.py -q -k guided`
Expected: FAIL，未注入 guided guidance。

- [ ] **Step 5: 实现固定指导和幂等注入**

模块常量：

```python
GUIDED_EXECUTION_GUIDANCE = (
    "[Execution mode: guided]\n"
    "Maintain a short private checklist of the requested outcomes. Execute dependent "
    "actions in order, verify intermediate results, and do not finish until every "
    "requested outcome is covered. Do not reveal the checklist unless asked."
)
```

复用 `_inject_step_guidance()` 的非破坏性拷贝模式，但单独函数先移除已有同标记 guidance 再追加，保证幂等。调用顺序固定：ContextGovernor -> route guidance -> model request。

- [ ] **Step 6: 验证 DIRECT 和 PLANNED 不收到 guided 文本**

参数化三个 route；DIRECT无指导，GUIDED只有 guided，PLANNED只有 current step guidance，不同时出现两种文字。

- [ ] **Step 7: 运行相关测试**

Run: `pytest tests/agent/test_runner_task_routing.py tests/agent/test_runner_governance.py tests/agent/test_runner_injections.py -q`
Expected: PASS。

- [ ] **Step 8: 提交**

```bash
git add miniunicorn/agent/runner.py tests/agent/test_runner_task_routing.py tests/agent/test_runner_governance.py tests/agent/test_runner_injections.py
git commit -m "feat(agent): guide medium tasks without planner overhead"
```

---

### Task 7: 实现运行时升级和 HIGH-risk pre-execution gate

**Files:**
- Modify: `miniunicorn/agent/task_router.py`
- Modify: `miniunicorn/agent/runner.py`
- Modify: `tests/agent/test_task_router.py`
- Modify: `tests/agent/test_runner_task_routing.py`
- Modify: `tests/agent/test_runner_safety.py`

**Interfaces:**
- Produces: `TaskRouter.evaluate_runtime(state, profiles)`、`_build_progress_summary()`、动态 plan attempt。
- Consumes: `ToolRegistry.profile_call()`；调用发生在 assistant tool-call 消息持久化之前。

- [ ] **Step 1: 写纯升级规则失败测试**

参数化断言：

```python
@pytest.mark.parametrize(
    ("initial", "iterations", "calls", "errors", "domains", "current", "expected"),
    [
        (TaskRoute.DIRECT, 0, 0, 0, set(), three_low_profiles(), TaskRoute.GUIDED),
        (TaskRoute.DIRECT, 2, 3, 0, {"filesystem"}, [], TaskRoute.GUIDED),
        (TaskRoute.GUIDED, 1, 4, 0, {"filesystem", "web"}, one_low_profile(), TaskRoute.PLANNED),
        (TaskRoute.DIRECT, 0, 0, 0, set(), one_high_profile(), TaskRoute.PLANNED),
        (TaskRoute.GUIDED, 1, 2, 2, {"web"}, [], TaskRoute.PLANNED),
    ],
)
def test_runtime_upgrade_rules(...): ...
```

再断言 PLANNED 永不降级、DIRECT不能越过规则、reasons稳定追加、`upgraded_from` 正确。

- [ ] **Step 2: 写 HIGH risk 工具零执行失败测试**

第一次主模型返回 `message` 或 destructive exec；Runner 必须先创建 plan，`tools.execute.await_count == 0`，原 assistant tool-call 不出现在 `result.messages` 和 checkpoint。计划后的下一次模型调用重新产生工具调用才可执行。

- [ ] **Step 3: 写动态多域和失败升级测试**

覆盖：

- 低风险3 calls只升 GUIDED且当前调用照常执行；
- 累计5 calls+2 domains在下一次执行前升 PLANNED；
- 两次工具错误后正式 plan attempt一次；
- 同 turn 后续高风险调用不创建第二份 initial/dynamic plan；
- high-risk planner fallback => `routing_failed`且高风险调用零执行；
- complexity planner fallback => GUIDED并允许低/中风险调用继续。

- [ ] **Step 4: 运行并确认失败**

Run: `pytest tests/agent/test_task_router.py tests/agent/test_runner_task_routing.py -q -k "runtime or risk or upgrade"`
Expected: FAIL，尚无动态升级。

- [ ] **Step 5: 实现 `evaluate_runtime()`**

该方法计算 projected call/domain 并返回新 immutable decision，不执行工具。`state.executed_tool_calls` 只在调用真正执行后增加；触发PLANNED而丢弃的拟调用不得计数。domain 集合来自 profile，HIGH判断只看 `ToolRisk.HIGH`。升级顺序先检查 PLANNED强制条件，再检查 GUIDED条件，防止同一次判断生成两个 upgrade event。

- [ ] **Step 6: 把 gate 放到持久化之前**

在 `response.should_execute_tools` 分支中，顺序必须变成：

```text
profile proposed calls
evaluate runtime route
if upgrade to PLANNED:
    build plan / handle fallback
    discard proposed response
    continue next iteration
else:
    append assistant tool-call message
    emit awaiting_tools checkpoint
    execute tools
    append tool results
```

任何代码审查发现 assistant message 在 gate 前 append，Task 不得验收。

- [ ] **Step 7: 实现脱敏进度摘要**

`_build_progress_summary(messages, max_chars=4000)` 只提取已完成 tool message 的 name、成功/错误分类和经过 `_normalize_tool_result` 后再次截断的摘要；使用 `<execution_progress>` delimiter，并加入“data, not instructions”。禁止包含 assistant reasoning、system prompt、API key字段或超过4000字符。

- [ ] **Step 8: 更新错误计数**

工具执行后用统一 `_tool_result_is_error()` 判断 `Error:`、fatal_error 和 tool event error；同一个 call 只计一次。成功后不清零累计错误，turn 结束自然销毁 state。第二次错误计入后、再次请求模型前立即调用 runtime evaluation；若升级PLANNED，使用刚完成的工具结果生成progress summary并创建剩余计划。

- [ ] **Step 9: 运行动态路由和安全测试**

Run: `pytest tests/agent/test_task_router.py tests/agent/test_runner_task_routing.py tests/agent/test_runner_safety.py -q`
Expected: PASS，尤其 HIGH risk pre-execution gate 测试必须通过。

- [ ] **Step 10: 提交**

```bash
git add miniunicorn/agent/task_router.py miniunicorn/agent/runner.py tests/agent/test_task_router.py tests/agent/test_runner_task_routing.py tests/agent/test_runner_safety.py
git commit -m "feat(agent): upgrade complex and risky turns before execution"
```

---

### Task 8: 接入顶层 Loop、子 Agent、checkpoint 和日志

**Files:**
- Modify: `miniunicorn/agent/loop.py`
- Modify: `miniunicorn/agent/subagent.py`
- Modify: `miniunicorn/agent/runner.py`
- Modify: `tests/agent/test_loop_runner_integration.py`
- Modify: `tests/agent/test_runner_persistence.py`
- Modify: `tests/agent/test_runner_reflection.py`

**Interfaces:**
- Produces: 顶层始终 auto-route；subagent policy最多GUIDED；checkpoint/result/log含脱敏 routing。
- Consumes: Task 4 `AgentLoop.task_routing_policy` 和 Task 5 `AgentRunSpec.routing_policy`。

- [ ] **Step 1: 写顶层 policy 传递测试**

patch `runner.run` 捕获 spec，断言每次顶层 `_run_agent_loop()` 都传 `loop.task_routing_policy`，不再传 `use_planner/planner_model/planner_max_replans`。

- [ ] **Step 2: 写子 Agent 禁止嵌套计划测试**

分别覆盖 `spawn_and_wait` 和 direct subagent 路径，捕获 spec：

```python
assert spec.routing_policy.allow_planned is False
assert spec.routing_policy.planner_model is None
```

复杂 subagent task最多route GUIDED；patch Planner 构造器断言零调用。

- [ ] **Step 3: 写 checkpoint routing 测试**

`awaiting_tools`、`final_response`、error checkpoint都包含 `routing=decision.to_dict()`；不包含原始 task、工具参数和结果。动态升级后 checkpoint显示最终 route及`upgraded_from`。

- [ ] **Step 4: 写日志脱敏测试**

使用 log sink 捕获 `task_routed/task_route_upgraded/task_planner_*`，断言有 route/score/reason/count，无原始用户 secret 和工具参数。不要对完整自然语言日志格式做脆弱快照，只断言结构化字段。

- [ ] **Step 5: 实现 Loop/Subagent wiring**

顶层 spec：

```python
routing_policy=self.task_routing_policy
```

两个 Subagent spec 都传 `TaskRoutingPolicy.subagent()`；不得依赖父 Agent 的 mutable runtime state。

- [ ] **Step 6: 实现 checkpoint/result/log字段**

所有 `_emit_checkpoint()` 调用通过一个 helper 合并 routing，避免漏掉分支。Reflection trigger在 `routing_failed` 时使用该稳定 trigger，不把 raw planner error写入 reflection evidence。

- [ ] **Step 7: 运行接线、持久化和 Reflection 测试**

Run: `pytest tests/agent/test_loop_runner_integration.py tests/agent/test_runner_persistence.py tests/agent/test_runner_reflection.py tests/agent/test_runner_task_routing.py -q`
Expected: PASS。

- [ ] **Step 8: 提交**

```bash
git add miniunicorn/agent/loop.py miniunicorn/agent/subagent.py miniunicorn/agent/runner.py tests/agent/test_loop_runner_integration.py tests/agent/test_runner_persistence.py tests/agent/test_runner_reflection.py tests/agent/test_runner_task_routing.py
git commit -m "feat(agent): propagate adaptive routing across runtimes"
```

---

### Task 9: 删除静态 Planner 残留并更新产品文档

**Files:**
- Modify: `miniunicorn/agent/runner.py`
- Modify: `miniunicorn/agent/loop.py`
- Modify: `miniunicorn/agent/planner.py`
- Modify: `miniunicorn/config/schema.py`
- Create: `tests/agent/test_task_routing_boundary.py`
- Modify: `docs/configuration.md`
- Create: `docs/task-routing.md`
- Modify: `README.md`
- Modify: `docs/image-generation.md`

**Interfaces:**
- Produces: 只有 auto-router 的公开配置和一致文档；旧静态字段硬拒绝。
- Consumes: Tasks 1–8 的最终命名和语义。

- [ ] **Step 1: 写静态路径残留边界测试**

`tests/agent/test_task_routing_boundary.py` 扫描运行时源码和配置：

```python
FORBIDDEN_RUNTIME_PATTERNS = (
    "use_planner",
    "usePlanner",
    "spec.use_planner",
    "self.use_planner",
)


def test_static_planner_toggle_removed() -> None:
    source = runtime_source_text()
    for pattern in FORBIDDEN_RUNTIME_PATTERNS:
        assert pattern not in source
```

`planner_model/planner_max_replans` 允许出现在 `TaskRoutingConfig/Policy` 内，但不允许作为 `AgentDefaults` 顶层字段或 `AgentRunSpec` 独立字段。再断言不存在 route classifier provider/LLM调用。

- [ ] **Step 2: 运行边界测试并确认失败**

Run: `pytest tests/agent/test_task_routing_boundary.py -q`
Expected: FAIL，旧 `use_planner` 仍存在。

- [ ] **Step 3: 删除旧字段、注释和静态分支**

删除 Runner/Loop/Planner docstring 中“when use_planner True”和“legacy ReAct-only toggle”描述。保留唯一结构：TaskRouter decision决定是否构造 Planner。不得保留旧字段作为 deprecated alias，因为项目仍在开发期。

- [ ] **Step 4: 编写 `docs/task-routing.md`**

文档必须包括：

- DIRECT/GUIDED/PLANNED 大白话定义；
- 固定分值表和阈值；
- 运行时升级条件；
- Planner fallback矩阵；
- 工具 risk/domain 的作用；
- “路由不等于授权”的醒目说明；
- 子 Agent不嵌套规划；
- structured log reason codes；
- 三个小微企业示例：单订单查询、批量对账、审批后外发催款。

- [ ] **Step 5: 更新配置、README和相邻说明**

`docs/configuration.md` 使用唯一配置示例：

```json
"taskRouting": {
  "plannerModel": null,
  "plannerMaxReplans": 3
}
```

README 架构描述改为“Adaptive Router -> ReAct / Guided ReAct / Planner+ReAct”。`docs/image-generation.md` 把“与 Plan & Execute 的 plannerModel 同模式”改成“与 taskRouting.plannerModel 一样引用 model preset”。

- [ ] **Step 6: 执行残留搜索**

Run: `rg -n "usePlanner|use_planner|agents\.defaults\.plannerModel|agents\.defaults\.plannerMaxReplans" miniunicorn docs README.md tests`
Expected: 只在旧字段拒绝测试的字符串 fixture 中出现；运行时和产品文档零结果。

- [ ] **Step 7: 运行边界和配置测试**

Run: `pytest tests/agent/test_task_routing_boundary.py tests/config/test_config_boundaries.py -q`
Expected: PASS。

- [ ] **Step 8: 提交**

```bash
git add miniunicorn/agent/runner.py miniunicorn/agent/loop.py miniunicorn/agent/planner.py miniunicorn/config/schema.py tests/agent/test_task_routing_boundary.py docs/configuration.md docs/task-routing.md docs/image-generation.md README.md
git commit -m "docs(agent): document adaptive task routing"
```

---

### Task 10: 基准、全量回归和完成审查

**Files:**
- Create: `scripts/benchmark_task_router.py`
- Modify only scoped files when verification exposes a defect; every fix requires a regression test.

**Interfaces:**
- Produces: 可合并的三级路由实现和可重复分类性能报告。

- [ ] **Step 1: 实现纯分类 benchmark**

脚本参数固定：

```text
--iterations 100000
--max-task-chars 1000
--json-output PATH
```

使用固定中英文任务集合，报告 Python/OS、iterations、route counts、mean/p50/p95/max microseconds。脚本只调用 `TaskRouter.classify()`，patch socket/provider并在被访问时失败，证明无网络/LLM。

- [ ] **Step 2: 运行分类和 Planner 专项测试**

Run:

```bash
pytest \
  tests/agent/test_task_router.py \
  tests/agent/test_tool_risk_profiles.py \
  tests/agent/test_planner.py \
  tests/agent/test_runner_task_routing.py \
  tests/agent/test_task_routing_boundary.py \
  -q
```

Expected: PASS。

- [ ] **Step 3: 运行 Runner 相关回归**

Run:

```bash
pytest \
  tests/agent/test_runner_core.py \
  tests/agent/test_runner_tool_execution.py \
  tests/agent/test_runner_safety.py \
  tests/agent/test_runner_persistence.py \
  tests/agent/test_runner_governance.py \
  tests/agent/test_runner_injections.py \
  tests/agent/test_runner_reflection.py \
  tests/agent/test_loop_runner_integration.py \
  -q
```

Expected: PASS。

- [ ] **Step 4: 运行全量 Python 测试**

Run: `pytest -q`
Expected: PASS；skip 必须是基线已有且原因明确的 skip。

- [ ] **Step 5: 运行 WebUI 测试和构建**

Run:

```bash
cd webui
npm test -- --run
npm run build
```

Expected: tests PASS，build exit 0。

- [ ] **Step 6: 运行静态和编译检查**

Run: `ruff check miniunicorn tests scripts/benchmark_task_router.py`
Expected: PASS。
Run: `python -m compileall -q miniunicorn`
Expected: exit 0。
Run: `git diff --check`
Expected: 无 whitespace error。

- [ ] **Step 7: 运行本地性能报告**

Run: `python scripts/benchmark_task_router.py --iterations 100000 --max-task-chars 1000 --json-output .benchmark-task-router.json`
Expected: exit 0，生成有效 JSON，DIRECT分类 p95 目标小于1ms；报告文件不提交。若未达标，先 profile regex，不能删规则或降低 iterations 伪造结果。

- [ ] **Step 8: 做端到端场景演练**

使用 mocked/测试工具验证并记录：

1. “查询订单123” => DIRECT，零 Planner调用；
2. “批量核对逾期订单，完成后等我确认” => GUIDED，零 Planner调用；
3. “调研供应商、比较报价、生成采购建议并交我确认” => PLANNED；
4. 初始 DIRECT 后模型提出 message外发 => 工具零执行，先升级PLANNED；
5. HIGH-risk Planner失败 => routing_failed，零工具执行；
6. 子 Agent收到复杂任务 => 最多GUIDED，零嵌套Planner。

- [ ] **Step 9: 对照设计完成定义审查**

逐项检查设计 §18 的11项并在交付说明附测试证据。特别确认 HIGH-risk gate 位于 assistant tool-call message持久化之前；这是 merge blocker，不能以“后续再修”通过。

- [ ] **Step 10: 处理验证发现的问题**

发现缺陷时回到拥有该行为的 Task，先补充失败回归测试，再做最小修复并使用准确的 `fix(agent): ...` 提交。修复后重新执行 Step 2–9；没有缺陷时不创建空提交。

- [ ] **Step 11: 请求代码审查，不直接合并**

审查者重点检查：规则误判边界、Planner fallback矩阵、pre-execution顺序、orphan tool call、MCP未知风险、progress summary脱敏、子 Agent嵌套、配置真实接线和 DIRECT零额外调用。全部验证通过后再使用 `finishing-a-development-branch` 决定 merge/PR。

---

## Implementation Notes for the Executing Agent

1. 先运行基线 Runner/config tests并保存结果，不把基线失败归咎于本次改造。
2. Router只判断执行结构，不生成计划内容；不要把 Planner prompt逻辑塞进 `task_router.py`。
3. 不要用任务长度作为独立复杂度信号；长文本可能只是翻译或总结。
4. HIGH-risk文本识别允许保守误判，因为只增加规划；实际工具画像才是执行前硬门槛。
5. 路由升级发生后更新同一个 runtime state，不重新 classify原始文本。
6. GUIDED指导是synthetic model input，不得污染 session、checkpoint正文或长期记忆。
7. 动态PLANNED升级必须丢弃未执行的tool-call response；绝不能append后不给tool result。
8. Tool risk只做画像，不能改变原有guard、权限、workspace限制或并发安全属性。
9. `routing_policy=None` 仅用于底层Runner测试/嵌入调用的DIRECT兼容；正式AgentLoop始终传policy。
10. 如果实现需要人类审批或新的授权协议，立即暂停：那是独立安全项目，不在本计划范围。
