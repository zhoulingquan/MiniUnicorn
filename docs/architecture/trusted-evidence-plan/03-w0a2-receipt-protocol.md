# W0-A2：结构化回执协议（ToolReceiptClaim + 三工具改造）

> 前置依赖：W0-A1 已合并（ToolObservation 管道可用）。
> 本批**无验收行为变更**：回执只被记录进观察，不参与判定。判定切换在 W0-A3。

## 一、设计裁决（为什么是 contextvar + 事件搭车）

回执通道的三个硬约束：

1. **必须在副作用真实发生后创建**——回执只能在 `fp.write_text(...)` + `_verify_within(fp)` + `record_write(fp)` 全部成功的那条代码路径上产生。错误返回路径、dry-run 路径一律不产生。
2. **必须经任务内 contextvar 传出**——并发批次用 `asyncio.gather`，每个 `run_tool` 协程被包成独立 Task，Task 拷贝上下文。工具 execute 里 `contextvar.set()` 的写只在本 Task 可见，**父协程事后读不到**。因此读取必须发生在 `run_tool` 内部（与工具执行同 Task）。
3. **不能污染模型可见面**——回执不能进 tool result 字符串（模型可读就能伪造预期），也不能持久化进 `tool_events`（外泄到 UI/结果对象）。方案：临时挂在 event dict 上，`build_observations` 读取后**立刻 pop 掉**，事件面恢复干净。

这与 `FileStates` / `current_file_states` 的既有 contextvar 模式同构，不引入新范式。

## 二、新增模块 miniunicorn/agent/tools/receipts.py

```python
"""工具副作用回执：由工具代码在副作用真实完成后创建。"""
from contextvars import ContextVar
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any


def content_digest(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ToolReceiptClaim:
    tool: str                      # "write_file" | "edit_file" | "apply_patch"
    operation: str                 # "write" | "edit" | "patch"
    target: str                    # 解析后的绝对路径（apply_patch 为 None，见 files）
    committed: bool = True         # 恒 True；保留字段以显式表达语义
    digest: str | None = None      # 提交内容 sha256（apply_patch 为 None）
    files: list[dict[str, Any]] = field(default_factory=list)  # apply_patch 多文件明细
    created_at: str                # UTC ISO8601

    def to_dict(self) -> dict[str, Any]: ...


_current_receipt: ContextVar[ToolReceiptClaim | None] = ContextVar(
    "current_tool_receipt", default=None
)


def emit_receipt(claim: ToolReceiptClaim) -> None:
    """工具代码在副作用完成后调用。同一次工具调用内重复 emit 保留最后一条。"""
    _current_receipt.set(claim)


def take_receipt() -> ToolReceiptClaim | None:
    """run_tool 在工具执行返回后调用：读取并复位。"""
    claim = _current_receipt.get()
    _current_receipt.set(None)
    return claim
```

要点：
- `created_at` 在 `emit_receipt` 调用点生成（不要放默认值，冻结 dataclass 不便）。
- `digest` 用**提交后的真实内容**计算（`_hash_file(fp)` 或对写入文本直接 `content_digest`，二者等价时优先前者——写后读盘，防"算了没写"）。

## 三、run_tool 读取回执（tool_execution.py）

`run_tool` 中 `result, event, error = await self._run_tool_impl(...)`（约 220 行）之后：

```python
claim = take_receipt()
if claim is not None:
    event["receipt"] = claim.to_dict()
```

- 只在回执存在时挂键；不判断 status（回执只会在成功路径产生，error 路径本来就没有）。
- event 的类型注解从 `dict[str, str]` 放宽为 `dict[str, Any]`（本文件内全部 event 相关注解同步放宽；`AgentHookContext.tool_events`、`AgentRunResult.tool_events`、`_TurnState.tool_events` 的注解同步）。
- **必须在 blocked 早返回路径之后**（blocked 不执行工具，天然无回执）。

`build_observations`（W0-A1 新增）改为：

```python
receipt = event.pop("receipt", None)
obs = ToolObservation(..., receipt=receipt)
```

**pop 而非 get**——保证 receipt 键不会随 event 进入 `state.tool_events` / `AgentRunResult.tool_events` / 任何 UI 消费面。

## 四、三个契约工具的改造（miniunicorn/agent/tools/）

### 4.1 WriteFileTool.execute（filesystem.py 约 478-495 行）

成功路径 `fp.write_text(content)` → `self._verify_within(fp)` → `self._file_states.record_write(fp)` 之后、return 之前：

```python
from miniunicorn.agent.tools.receipts import ToolReceiptClaim, emit_receipt
emit_receipt(ToolReceiptClaim(
    tool="write_file", operation="write", target=str(fp),
    digest=<写后内容摘要>, created_at=...,
))
```

错误路径（PermissionError / Exception 分支）不动。

### 4.2 EditFileTool.execute（filesystem.py 约 839 行起）

该方法的成功写入点**有多处**（old_text="" 创建文件、空文件覆写、常规替换、replace_all 等），判据是：**每一处 `self._file_states.record_write(fp)` 调用点之后**都要 emit 回执。实施时以 `rg -n "record_write" miniunicorn/agent/tools/filesystem.py` 全量定位，逐处添加，`operation="edit"`，`detail` 携带实际替换次数（若有）。

write_file 与 edit_file 的 `record_write` 调用点总数以代码为准（当前核实 filesystem.py 内共约 4-5 处，含 create 分支），**漏一处就是验收漏洞**——批次报告需列出每处调用的行号。

### 4.3 ApplyPatchTool.execute（apply_patch.py 约 146-307 行）

- **dry_run 分支（约 282 行）提前 return，天然不产生回执——加一条测试锁死此行为。**
- 提交块（备份→写入→异常回滚，约 287-303 行）之后、`for path in writes: self._file_states.record_write(path)` 循环（约 305 行）之后 emit **一条**多文件回执：

```python
emit_receipt(ToolReceiptClaim(
    tool="apply_patch", operation="patch",
    target="", digest=None,
    files=[
        {"path": str(p), "digest": <写后内容摘要>,
         "added": <对应 summary.added>, "deleted": <对应 summary.deleted>}
        for p in writes
    ],
    created_at=...,
))
```

- 回滚路径（except 分支恢复备份后 raise）不 emit——raise 后 run_tool 拿到 error result，contextvar 未设置，安全。
- 摘要明细（added/deleted）从 `summaries` 列表取，与 path 按 `writes` 键序对应。

### 4.4 明确不产生回执的工具（写进 receipts.py 模块 docstring）

`shell`、`web_search`、`read_file`、`list_files`、一切 MCP 工具。原因：输出可被模型文本诱导（echo 关键词、搜索结果含目标词），不构成副作用证明。回执是白名单制，不是通用机制。

## 五、测试要求（新增）

新文件 `tests/agent/test_tool_receipts.py`：

1. **write_file 成功** → 观察含 receipt，digest == sha256(落盘内容)。
2. **write_file 失败**（路径越界 / 权限错误）→ receipt 为 None。
3. **edit_file 每条成功路径**（创建、空文件、常规替换）→ 各自产生 receipt（参数化用例）。
4. **apply_patch 提交** → 单条 receipt，files 数量 == 写入文件数，每文件 digest 正确。
5. **apply_patch dry_run=True** → receipt 为 None（关键防伪造用例）。
6. **apply_patch 回滚**（构造写入中途失败，如 monkeypatch write_text 第二次抛错）→ receipt 为 None，备份恢复。
7. **并发批次隔离**：`concurrent_tools=True` 下同一批两个 write_file（不同文件）→ 两条观察各含各的 receipt，无串扰（验证 contextvar 任务隔离假设）。
8. **事件面干净**：执行后 `event` dict 无 "receipt" 键；`AgentRunResult.tool_events` 序列化无 receipt；`state.tool_events` 无 receipt。
9. **take_receipt 复位**：连续两次 take，第二次为 None。
10. **MCP/其他工具不 emit**：FakeTool 返回成功 → receipt None。

## 六、禁改清单

- `StepAcceptancePolicy` 全部判定逻辑（回执在本批不参与任何判定）
- `run_tool` 的审批门 / blocked / checkpoint 逻辑（只在 `_run_tool_impl` 返回后追加读取）
- 工具的既有返回字符串格式（模型可见面不变）
- `Plan` / `PlanStep` / planner 模板

## 七、验收自检

- [ ] 全量测试绿；`ruff` 零告警
- [ ] `record_write` 全量调用点清单（行号）写入批次报告，逐处确认 emit
- [ ] receipt 键不出现在任何持久化/外泄面（测试 8 覆盖）
- [ ] 与本规格的偏差逐条说明
