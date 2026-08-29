# W0-A3：验收语义切换（核心行为变更批）

> 前置依赖：W0-A2 已合并（回执已进 ToolObservation.receipt）。
> **这是整个 W0 唯一改变验收行为的批次**。此前 text 判定漏洞（模型复述 done_criteria 关键词即通过、shell echo 伪造、dry-run 骗过验收）在本批关闭。

## 一、语义总则

| 概念 | 定义 |
|---|---|
| 证据等级 | `text` / `tool` 两级。规则：`none` 折叠进 `text`（"无要求"与"文本即可"判定等价） |
| effective level | `effective = max(Planner 声明等级, 静态下限)`，`tool > text` |
| 静态下限 | `step.tool_hint ∈ {write_file, edit_file, apply_patch}` → 下限为 `tool` |
| tool 级判定 | 观察序列中存在 **≥1 条 `receipt is not None 且 receipt["committed"] is True`** → 通过；文本完全不参与 |
| text 级判定 | 现行逻辑不变（final_content 非空 + done_criteria 子串命中） |
| verifier rescue | **仅**复核 `done_criteria_not_met` 一种拒绝原因，且只允许从拒绝改判通过。硬失败（无回执、空内容）永不 rescue |
| verifier 故障 | 异常 → fail-closed 回退规则判定结果（现行为，受保护），**不写缓存** |
| 缓存键 | `(step.id, evidence_digest)`；digest = 观察序列规范化 SHA-256 |

## 二、PlanStep 增加字段（planner.py）

```python
evidence_level: str = "text"   # "text" | "tool"，默认 text
```

- `to_dict()` 输出该字段。
- 解析端归一化在 W0-A4；本批只加字段与辅助函数。
- 新增模块级函数（planner.py）：

```python
RECEIPT_TOOLS = frozenset({"write_file", "edit_file", "apply_patch"})

def effective_evidence_level(step: PlanStep) -> str:
    if step.evidence_level == "tool" or (step.tool_hint or "") in RECEIPT_TOOLS:
        return "tool"
    return "text"
```

## 三、StepAcceptancePolicy 改造（step_acceptance.py）

### 3.1 新增证据摘要函数（模块级）

```python
def observations_digest(observations: list[ToolObservation]) -> str:
    """观察序列规范化哈希：只取影响判定的稳定字段。"""
    items = sorted(
        (o.tool_name, o.status,
         o.receipt.get("digest") if o.receipt else None,
         o.receipt.get("target") if o.receipt else None)
        for o in observations
    )
    return sha256(json.dumps(items, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
```

- 排序后哈希：同一组观察不同到达顺序 → 同 digest（并发批次顺序不保证稳定）。
- 不含 `result_excerpt` / `occurred_at` / `arguments` 全文：这些不稳定字段进 digest 会让缓存永远失效；它们仍留在 StepEvidence 里供审计与 verifier prompt。

### 3.2 判定主体 `_is_accepted` 重写

```python
def _is_accepted(self, step, observations, final_content) -> bool:
    level = effective_evidence_level(step)
    if level == "tool":
        return any(
            o.receipt is not None and o.receipt.get("committed") is True
            for o in observations
        )
    # text 级：现行为原样保留
    if final_content and final_content.strip():
        if step.done_criteria:
            return step.done_criteria.lower() in final_content.lower()
        return True
    return False
```

### 3.3 拒绝原因 `_rejection_reason` 重写

```python
def _rejection_reason(self, step, observations, final_content) -> str:
    level = effective_evidence_level(step)
    if level == "tool":
        if not observations:
            return "no_tool_receipt"          # 一步工具都没调
        if not any(o.receipt for o in observations):
            return "no_tool_receipt"          # 调了工具但没有可信副作用
        return "done_criteria_not_met"        # 有回执但文本未复述完成标准 → 可 rescue
    # text 级：现有四类原因保留（empty_content_no_tools /
    # empty_content_with_tools / done_criteria_not_met / unknown）
    ...
```

tool 级的三类拒绝里，只有第三类（有回执但文本未达标准）允许 verifier rescue——回执已证明"真实做了事"，verifier 判断"做的事够不够"。

### 3.4 evaluate / evaluate_with_verifier 签名与缓存

- 二者的 `tool_calls` / `tool_results` 参数**删除**（自 W0-A1 起恒为空列表，属死参数；调用方 planning.py 同步删除传参）。签名变为：

```python
def evaluate(self, step, observations, final_content, iterations_used) -> StepEvidence

async def evaluate_with_verifier(
    self, step, observations, final_content, iterations_used, *,
    provider, model, enable_verifier,
    step_evidence_cache: dict[tuple[int, str], dict[str, Any]] | None = None,
) -> StepEvidence
```

- **缓存键**：`plan._verifier_cache` 的键从 `step.id` 改为 `(step.id, observations_digest(observations))`。效果：步骤被拒后模型继续干活 → 新观察 → 新 digest → 重新评估（旧键下二次评估直接命中缓存永不重判，这是现存缺陷）。
- **缓存写入条件收紧**：verifier 返回了有效裁决（accepted 字段存在）或规则直接通过 → 写缓存；verifier 故障回退（`verifier_verdict = {"error": "verifier_failed", ...}`）→ **不写缓存**。故障 fail-closed 本身（回退到规则结果）是受保护行为，保留。
- 缓存值结构不变：`{"accepted", "rejection_reason", "verifier_verdict"}`。

### 3.5 verifier rescue 通道收紧（evaluate_with_verifier 内）

调用 `_call_verifier_llm` 的前置条件从 `规则拒绝 且 enable_verifier` 收紧为：

```python
rule_rejected and enable_verifier and rule_rejection == "done_criteria_not_met"
```

其余拒绝原因（`no_tool_receipt` / `empty_content_*`）直接返回规则结果，**不发起 LLM 调用**——这是堵死"无回执但靠 verifier 放水"的通道。

### 3.6 verifier prompt 升级（_build_verifier_prompt）

prompt 需呈现真实证据而非只有文本摘要：

```
Step: {action}
Done criteria: {done_criteria}
Evidence level: {effective level}
Tool observations ({n}):
  - write_file(target=..., committed=True, digest=abc123...)
  - shell(status=ok, no receipt)   ← 不可信证据如实标注
Final content: {final_content 或 (empty)}

Question: the step HAS produced verified side effects (receipts above).
Judge ONLY whether those actions + content satisfy the done criteria.
Respond with JSON: {"accepted": true|false, "reason": "..."}
```

实现要点：观察逐条一行（tool_name + receipt target/digest 或 "no receipt"），receipt 的 files 明细展开为一行一文件。不传完整 arguments / result_excerpt（prompt 膨胀且注入面大）。

### 3.7 StepEvidence 扩展字段

```python
evidence_level: str = "text"        # 本次评定的 effective level
evidence_digest: str | None = None  # observations_digest 结果
```

`to_dict()` 输出二者；`observations` 已在 A1 就位。

## 四、verifier 连续故障熔断（progress_policy.py + planning.py）

### 4.1 ProgressTracker 增加计数

```python
# __init__
self._verifier_failures: int = 0

def record_verifier_failure(self) -> None:
    self._verifier_failures += 1

def record_verifier_success(self) -> None:
    self._verifier_failures = 0
```

### 4.2 check_step_progress 增加熔断检查（放在迭代上限检查之后）

```python
if self._verifier_failures >= 2:
    return ProgressVerdict(ProgressAction.REPLAN, "verifier_unavailable")
```

语义：verifier 连续 2 次故障 → 当前步骤的证据判定不可信 → 触发 REPLAN（换步骤路径），而非无限烧 verifier 重试。

### 4.3 计数写入点（planning.py evaluate_step_progress）

`PlanningReflectionService.evaluate_step_progress`（约 330 行）在调用 `tracker.check_step_progress` 之前：

```python
verdict_dict = evidence.verifier_verdict if evidence else None
if verdict_dict and verdict_dict.get("error") == "verifier_failed":
    tracker.record_verifier_failure()
elif verdict_dict is not None:
    tracker.record_verifier_success()
```

（规则直接通过/拒绝时 verifier_verdict 为 None，不计数。）

## 五、调用方同步（planning.py / runner.py）

- `planning.complete_plan_step`：删除 `tool_calls` / `tool_results` 形参与传参；`observations` 已在 A1 就位，本批真正传入判定。`plan._verifier_cache` 初始化逻辑保持（键类型变化对 `hasattr` 检查透明）。
- runner 调用点（约 873 行）不传 tool_calls/tool_results（现状本来就不传）。
- `_TurnState.tool_observations` 在步骤完成时不清空（跨迭代审计保留），只在 replan 时清空（A1 已实现）。

## 六、受影响既有测试的处置预案

本批是**有意的行为变更**，以下既有测试预期需要更新（每处修改在批次报告中列出并说明）：

1. 任何以 `tool_hint="write_file"`（或另两个契约工具）构造步骤、靠 final_content 文本通过验收的测试 → 改为提供含 receipt 的观察，或改 tool_hint。
2. 任何断言 verifier 缓存按 step.id 生效的测试 → 改为 (step_id, digest) 键。
3. 任何断言"规则拒绝即触发 verifier LLM 调用"的测试 → 改为仅 done_criteria_not_met 触发。

**禁止修改**的测试（受保护清单，见 00-overview.md §4.1）：审批门、tool_blocked、checkpoint 恢复、预算传播、子代理策略传播、verifier 异常 fail-closed 回退行为本身。

## 七、测试要求（新增）

新文件 `tests/agent/test_acceptance_semantics.py`：

**tool 级判定矩阵**（参数化）：

| 场景 | 预期 |
|---|---|
| 观察 = [write_file receipt committed] | accepted |
| 观察 = []，final_content 复述标准 | **rejected** `no_tool_receipt`（核心防伪用例） |
| 观察 = [shell status=ok 无 receipt]，文本命中 | rejected `no_tool_receipt` |
| 观察 = [apply_patch dry_run（无 receipt）] | rejected `no_tool_receipt` |
| 观察 = [receipt]，文本为空 | accepted（tool 级不看文本） |
| 观察 = [receipt]，done_criteria 未复述 | rejected `done_criteria_not_met`，verifier（enable 时）可 rescue 为 accepted |

**text 级回归**：无 tool_hint、evidence_level 缺省 → 现行四类判定结果逐一不变。

**静态下限**：evidence_level="text" 但 tool_hint="edit_file" → effective="tool"。

**verifier rescue 边界**：
- done_criteria_not_met + verifier 返回 accepted=true → 通过，reason 清空
- no_tool_receipt + enable_verifier → **不发起 LLM 调用**（spy 断言 chat_with_retry 未被调用）
- verifier 抛异常 → 回退规则结果 rejected，`verifier_verdict["error"]=="verifier_failed"`，**缓存中无该键**

**缓存语义**：
- 同 digest 二次评估命中缓存，无第二次 LLM 调用
- 拒绝后追加新观察（digest 变化）→ 重新评估
- 观察顺序重排（sorted 稳定性）→ digest 相同

**熔断**：
- 连续 2 次 verifier_failed → `check_step_progress` 返回 REPLAN `verifier_unavailable`
- 中间一次成功 → 计数归零

**集成**：完整 managed turn（fake write_file 工具）→ 步骤 1 真实写入后被验收通过；模型只发文本不调工具 → 步骤保持 IN_PROGRESS。

## 八、禁改清单

- 审批门 / tool_blocked / checkpoint / 预算 / 子代理策略（受保护区）
- `ToolObservation` / `build_observations` / receipts 模块（A1/A2 成果）
- `Plan` 的步骤推进状态机（current_step/all_done/pending_steps 语义）
- text 级判定逻辑本体

## 九、验收自检

- [ ] 全量测试绿（含按第六节更新的既有测试，逐条列出）
- [ ] ruff 零告警
- [ ] 防伪三用例（文本复述 / shell echo / dry-run）全部 rejected —— 在报告中单独展示这三条的输出
- [ ] 与本规格的偏差逐条说明
