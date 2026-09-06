# W0-A4：Planner 协议升级（evidence_level 字段）

> 前置依赖：W0-A3 已合并（PlanStep.evidence_level 字段已存在且参与判定）。
> 本批小而独立：让 Planner LLM 能声明步骤的证据等级，闭合"声明端"协议。无判定逻辑改动。

## 一、改动一：planner_system.md 模板

`erza/templates/agent/planner_system.md` 的 JSON schema 中，每个 step 增加一个字段：

```
"evidence_level": "tool" 或 "text"
```

Rules 段追加两条（保持模板简短，planner 是低能力预算场景）：

```
- evidence_level: "tool" when the step's completion means files were actually
  written or edited (write_file / edit_file / apply_patch); otherwise "text".
- done_criteria describes the observable outcome, not the keywords to say.
```

同时把 done_criteria 的示例含义向"可观察结果"倾斜（现有措辞已可，不强制改）。

注意：模板是给 LLM 的指令，措辞改动会影响 planner 行为，**不要顺手重写其他段落**。

## 二、改动二：planner.py 解析归一化

`Planner` 的 JSON 解析循环（planner.py 约 283-299 行，`raw.get("done_criteria")` 所在的 steps 构造段）：

```python
raw_level = raw.get("evidence_level")
steps.append(
    PlanStep(
        id=raw.get("id", next_id),
        action=action,
        tool_hint=raw.get("tool_hint"),
        done_criteria=raw.get("done_criteria"),
        evidence_level=_normalize_evidence_level(raw_level),
    )
)
```

新增辅助函数（planner.py 模块级）：

```python
def _normalize_evidence_level(value: Any) -> str:
    return "tool" if str(value or "").strip().lower() == "tool" else "text"
```

归一化规则：任何非 "tool" 的值（缺失 / "none" / 拼写错误 / 非 str）一律折叠为 "text"。**不做报错**——静态下限（tool_hint ∈ RECEIPT_TOOLS）仍会把写文件类步骤抬到 tool 级，planner 漏声明不产生验收漏洞。

`PlanStep.to_dict()` 在 W0-A3 已输出 evidence_level，本批无需再动。

## 三、改动三：fallback plan 与既有构造点核查

`Planner._fallback_plan`（planner.py 约 311 行）构造的兜底单步计划不声明等级（默认 "text"）——**保持现状**。兜底计划本就走 ReAct-only 或 text 级验收，声明 tool 反而抬高等级导致永远无法通过。

核查项（只查不改）：`rg -n "PlanStep(" erza/ -g "*.py"` 全量过目，确认无其他生产代码路径需要同步声明 evidence_level（测试构造点按需补字段，属测试自由）。

## 四、测试要求

扩展既有 planner 测试文件（`tests/agent/test_planner*.py`，以实际文件名为准）：

1. 解析含 `"evidence_level": "tool"` 的步骤 → PlanStep.evidence_level == "tool"。
2. 缺失该字段 → "text"。
3. `"none"` / `"TOOL"` 大小写 / `"tool "` 带空白 → 归一化正确（"none"→"text"，"TOOL"→"tool"）。
4. 非 str 值（如 `true`）→ "text" 不抛异常。
5. 模板文件内容断言：`planner_system.md` 含 "evidence_level" 字样（防止模板被回退）。
6. 端到端：planner 返回 tool 级步骤 + runner 验收（复用 W0-A3 集成测试设施，fake provider 返回带 evidence_level 的 plan JSON）→ effective level 为 tool。

## 五、禁改清单

- StepAcceptancePolicy 任何逻辑
- Plan / Plan 模型推进状态机
- 模板其他段落（identity/tool_contract/reflection 等）

## 六、验收自检

- [ ] 全量测试绿，ruff 零告警
- [ ] 模板 diff 仅含 evidence_level 相关增改
- [ ] 与本规格的偏差逐条说明
