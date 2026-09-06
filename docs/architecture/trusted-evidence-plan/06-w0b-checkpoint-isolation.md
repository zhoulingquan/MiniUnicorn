# W0-B：plan_snapshot 审计隔离 + digest 持久化

> 前置依赖：W0-A1 已合并（可与 A2/A3/A4 并行实施，无冲突）。
> 本批小：两处改动。修复"计划快照覆盖崩溃恢复检查点"的数据安全问题。

## 一、问题（现状锚点）

`erza/agent/session_turn.py` 约 46 行：

```python
_AUDIT_ONLY_CHECKPOINT_PHASES = frozenset({"tool_started", "tool_completed", "tool_blocked"})
```

`plan_snapshot` **不在集合内**。而 `PlanningReflectionService.emit_plan_snapshot`（planning.py 约 42-65 行）在每次步骤状态迁移时都会 emit `phase="plan_snapshot"` checkpoint，经 `_set_runtime_checkpoint`（session_turn.py 约 205-210 行）**覆盖** `session.metadata["runtime_checkpoint"]`。

后果：崩溃恢复时 `_restore_runtime_checkpoint`（约 234 行）物化的是最后一次 plan_snapshot 的 payload（无 assistant_message / tool results 可恢复），而不是崩溃前真实的执行断点（awaiting_tools / tools_completed / final_response）。恢复链路被审计数据顶掉。

已核查：`_RUNTIME_CHECKPOINT_KEY` 的读取方只有 `_restore_runtime_checkpoint` 与清理逻辑（session_turn.py 内部）+ loop.py 的常量再导出，无外部消费方依赖"最后一条 checkpoint 恰好是 plan_snapshot"的行为——过滤是安全的。

## 二、改动一：plan_snapshot 加入审计隔离

```python
_AUDIT_ONLY_CHECKPOINT_PHASES = frozenset(
    {"tool_started", "tool_completed", "tool_blocked", "plan_snapshot"}
)
```

效果：plan_snapshot 仍正常走 `emit_checkpoint` 回调（gateway/webui 的观察面不变——观察面收到的 payload 与过滤无关，过滤只作用于 session.metadata 持久化槽位），只是不再写入 runtime_checkpoint 槽。

副带收益：MANAGED 模式每步骤 2 次快照（start + complete）不再触发 session 存盘，高频快照的磁盘写放大消失。

## 三、改动二：PlanSnapshot 增加 digest

`erza/agent/plan_snapshot.py`：

```python
import hashlib, json

@dataclass(frozen=True, slots=True)
class PlanSnapshot:
    ...
    digest: str = ""    # 计划内容的规范化摘要
```

`from_plan` 计算（不含 turn_id/created_at/stop_reason/origin 这些每次都变的字段，只摘要计划本体）：

```python
def _plan_digest(goal: str, steps: list[dict], replan_count: int) -> str:
    payload = json.dumps(
        {"goal": goal, "steps": steps, "replan_count": replan_count},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
```

`to_dict()` 输出 `digest` 字段。

用途：审计侧（gateway 日志 / webui 计划视图）可比对相邻快照 digest 判断"计划内容是否真的变了"还是仅状态迁移，为 W2 的跨轮计划恢复备忘提供基础数据。本批只做持久化，**不新增任何基于 digest 的逻辑分支**。

## 四、测试要求

扩展 `tests/agent/test_session_turn*.py`（以实际文件名为准）或新增 `tests/agent/test_plan_snapshot_checkpoint.py`：

1. **隔离用例（核心）**：先 emit `phase="awaiting_tools"` checkpoint，再 emit `phase="plan_snapshot"` → `session.metadata["runtime_checkpoint"]` 仍是 awaiting_tools 的 payload。
2. 过滤集合回归：tool_started/completed/blocked/plan_snapshot 四个 phase 均不落 runtime_checkpoint 槽。
3. 恢复链路回归（受保护测试如已存在则确认仍绿）：awaiting_tools → tools_completed → 崩溃 → 恢复物化正确。
4. digest 确定性：同一 Plan 两次 from_plan → digest 相同（created_at 不同不影响）。
5. digest 敏感性：步骤状态迁移（PENDING→COMPLETED，steps 序列变化）→ digest 变化；仅 stop_reason/turn_id 变化 → digest 不变。
6. `to_dict` 含 digest 键。

## 五、禁改清单

- `_restore_runtime_checkpoint` 的物化逻辑
- `emit_plan_snapshot` 的调用频率与 payload 结构（phase 名保持 "plan_snapshot"）
- awaiting_tools / tools_completed / final_response 三个恢复性 phase 的写入行为
- 审批门 / tool_blocked 相关（同在受保护区）

## 六、验收自检

- [ ] 全量测试绿，ruff 零告警
- [ ] 用例 1（隔离核心用例）在批次报告中展示断言输出
- [ ] 与本规格的偏差逐条说明
