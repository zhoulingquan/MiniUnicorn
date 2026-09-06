# W0-A1：证据管道（ToolObservation + 跨迭代累积 + 接线）

> 前置依赖：阶段 0 基线已提交（tag `baseline-pre-w0`）。
> 本批**无验收行为变更**：验收判定逻辑一行不动，只铺设数据管道。全量测试在改动前后结果必须一致（含既有失败集为空）。

## 一、问题（现状锚点）

工具结果从未到达验收器，管道断路：

1. `erza/agent/runner.py` 主循环中 `await self.execute_tools(...)`（约 661 行）拿到 `results, new_events, fatal_error`，但 `context.tool_results = list(results)`（约 673 行）**每个迭代被覆盖**，不跨迭代累积。
2. 步骤完成点在非工具响应分支：`await self._complete_plan_step(plan, context, hook, clean, state.stop_reason, spec=spec, turn_id=state.turn_id)`（约 873 行）——**没有传 `tool_calls` / `tool_results`**，`planning.complete_plan_step` 里二者默认 `[]`。
3. 结果：`StepAcceptancePolicy.evaluate` 收到的 `tool_calls=[]`、`tool_results=[]`，`StepEvidence.tool_calls/tool_results` 永远为空，验收实际只看 `final_content` 文本。

## 二、本批改动

### 2.1 新增 ToolObservation（放在 step_acceptance.py，与 StepEvidence 同域同文件）

`erza/agent/step_acceptance.py` 追加：

```python
@dataclass(frozen=True, slots=True)
class ToolObservation:
    """单次工具调用的结构化观察，验收证据的最小单元。"""
    tool_name: str
    arguments: dict[str, Any]          # 深拷贝后的调用参数（含 dry_run 等）
    status: str                        # event["status"]，"ok"/"error"/"blocked"
    result_excerpt: str                # str(result) 截断到 200 字符
    step_id: int | None = None         # 执行时的当前计划步骤 id
    receipt: dict[str, Any] | None = None  # W0-A2 填充，本批恒为 None
    occurred_at: str                   # datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict[str, Any]: ...
```

注意：
- `arguments` 需深拷贝（`copy.deepcopy` 或 json 往返），防止后续可变引用污染观察记录。
- `result_excerpt` 与 checkpoint 的 200 字符截断口径一致。
- `StepEvidence` 增加 `observations: list[dict[str, Any]] = field(default_factory=list)`（slots dataclass 需重新声明默认值），`to_dict()` 在非空时输出。既有字段（tool_calls/tool_results）保留不动。

### 2.2 观察构建器（tool_execution.py，纯函数，不改任何签名）

`erza/agent/execution/tool_execution.py` 的 `ToolExecutionService` 增加方法：

```python
def build_observations(
    self,
    tool_calls: list[ToolCallRequest],
    results: list[Any],
    events: list[dict[str, Any]],
    *,
    step_id: int | None = None,
) -> list[ToolObservation]:
```

实现要点：
- 按索引 zip 三者（`execute_tools` 保证 results/events 与 tool_calls 顺序一致：批内 gather 保序、批间顺序追加）。
- 长度不一致时按最短截断并 `logger.warning`（防御性，正常不应发生）。
- `status = event.get("status", "ok")`。
- 本方法**不读 contextvar、不产生副作用**（回执读取属于 W0-A2，且必须在 run_tool 任务内完成，见 03 号文件）。

### 2.3 跨迭代累积（runner.py）

`_TurnState`（约 167 行）增加字段：

```python
tool_observations: list[ToolObservation] = field(default_factory=list)
```

主循环接线（约 661-674 行区域），在 `state.tool_events.extend(new_events)` 附近追加：

```python
state.tool_observations.extend(
    self.tool_execution.build_observations(
        response.tool_calls, results, new_events,
        step_id=(plan.current_step.id if plan is not None and plan.current_step is not None else None),
    )
)
```

（`step_id` 的取值复用约 666-670 行 execute_tools 已有的同款表达式。）

### 2.4 传入验收器（runner.py + planning.py + step_acceptance.py）

1. runner.py 约 873 行调用点增加实参：
   ```python
   tool_observations=[o for o in state.tool_observations if o.step_id == plan.current_step.id]
   ```
   （此时 `plan.current_step` 非 None 已由外层 `if` 保证。）
2. `runner.complete_plan_step`（约 1188 行）与 `PlanningReflectionService.complete_plan_step`（planning.py 约 237 行）签名各加一个 kwarg：
   ```python
   tool_observations: list[ToolObservation] | None = None
   ```
   默认 None → `[]`，与现有 tool_calls/tool_results 的默认语义一致。
3. `StepAcceptancePolicy.evaluate` / `evaluate_with_verifier` 各加 kwarg `observations: list[ToolObservation] | None = None`，原样存入返回的 `StepEvidence.observations`。**判定逻辑（`_is_accepted` / `_rejection_reason`）一行不改**，`_build_verifier_prompt` 本批不改。
4. verifier 缓存键本批不动（仍 `step.id`），缓存内容增加 `observations` 序列化结果以便审计（仅入 dict，不影响判定路径）。

### 2.5 REPLAN 时清空累积（防步骤 id 复用串扰）

计划被替换的两处位置，在替换后追加 `state.tool_observations.clear()`：

1. `_handle_fatal_tool_error` 返回新 plan 后（约 695-699 行，`plan is not plan_before_error` 分支）。
2. REPLAN verdict 分支拿到新 plan 后（约 899 行起，具体符号以 runner.py 中 `ProgressAction.REPLAN` 处理段为准）。

原因：replan 后新计划的步骤 id 从 1 重新编号，与旧计划的观察按 step_id 过滤会串台。

### 2.6 AgentRunResult 审计面

`AgentRunResult`（runner.py 约 148 行）增加 `tool_observations: list[dict[str, Any]] = field(default_factory=list)`；runner 组装结果处（约 951 行 `tool_events=state.tool_events` 附近）同步填充 `[o.to_dict() for o in state.tool_observations]`。只增不改。

## 三、测试要求（新增，全部要写）

新文件 `tests/agent/test_tool_observation.py`：

1. `build_observations` 顺序配对：3 个调用（含 1 个 error）→ 3 条观察，status 正确。
2. `arguments` 深拷贝：构建后修改原始参数 dict，观察记录不变。
3. `result_excerpt` 截断到 200 字符。
4. 长度不齐时告警截断不抛异常。
5. 集成（可用既有 fake tool 测试设施）：多迭代 turn 中，迭代 1 调工具、迭代 2 纯文本收尾 → `StepEvidence.observations` 含迭代 1 的观察（monkeypatch `StepAcceptancePolicy.evaluate` 断言收到的 observations）。
6. replan 后旧观察被清空（模拟 fatal error replan 路径或直接断言 clear 调用点行为）。
7. `StepEvidence.to_dict` 含 observations 且空时不输出该键。

既有测试全部保持绿（本批不改判定，任何既有验收测试失败都说明改错了范围）。

## 四、禁改清单（本批红线）

- `StepAcceptancePolicy._is_accepted` / `_rejection_reason` / `_call_verifier_llm` / `_build_verifier_prompt`
- `execute_tools` / `run_tool` / `_run_tool_impl` 的签名与逻辑
- 审批门、tool_blocked checkpoint、runtime_checkpoint 链路
- `Plan` / `PlanStep` 模型（evidence_level 属于 W0-A3/W0-A4）
- 工具层任何文件（receipts 属于 W0-A2）

## 五、验收自检（批次报告须包含）

- [ ] `pytest tests/ -q` 全绿，数量与基线一致
- [ ] `ruff check erza/ && ruff format --check erza/` 零告警
- [ ] 改动文件清单 ≤ 5 个（step_acceptance.py、tool_execution.py、runner.py、planning.py、测试文件）
- [ ] 与本规格的偏差逐条说明
