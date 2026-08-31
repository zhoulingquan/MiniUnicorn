# W2-3：ToolContext 死字段删除

> 前置依赖：tag `baseline-pre-w2`。独立小批，可与 W2-1 并行实施。

## 一、问题（现状锚点，已全库核实）

`miniunicorn/agent/tools/context.py`（48-60 行）的 ToolContext 有 11 个字段，其中 9 个标 `Any`。逐字段全库核查消费情况（含 `getattr(ctx, "...")` 形式）：

| 字段 | 消费方 | 结论 |
|---|---|---|
| config | 几乎所有工具的 create() | 保留 |
| workspace | 广泛 | 保留 |
| bus | deep_research 等 | 保留 |
| subagent_manager | delegate / create_agent | 保留 |
| cron_service | cron 工具 | 保留 |
| sessions | long_task.py（117/123/194/200 行，getattr 形式） | 保留 |
| file_state_store | 文件系统工具 | 保留 |
| provider_snapshot_loader | image_generation | 保留 |
| subagent_registry | delegate.py:60 / create_agent.py:77（getattr 形式） | 保留 |
| **timezone** | **零消费**（唯一出现是构造点硬编码 `"UTC"`） | **删除** |
| **workspace_sandbox** | **零消费**（构造点传入，全库无 `ctx.workspace_sandbox` 读取；self.py / webui 读的是别处的同名概念） | **删除** |

生产构造点（全库仅两处）：

1. `miniunicorn/agent/_mcp_lifecycle.py` 40-49 行：`ToolContext(..., sessions=..., timezone="UTC", workspace_sandbox=self.workspace_scopes.sandbox_status, subagent_registry=...)`
2. `miniunicorn/agent/subagent.py` 255-263 行：`ToolContext(config=..., workspace=..., file_state_store=..., workspace_sandbox=workspace_sandbox_status(...))`

> 附带裁决记录：**类型收窄不做**。项目无 mypy/pyright（pyproject.toml 仅 ruff 配置），`Any → 具体类型` 无静态强制力，纯文档价值不抵循环导入处理成本。若未来引入类型检查器再议。

## 二、改动

### 2.1 context.py

删除两个字段：

```python
@dataclass
class ToolContext:
    config: Any
    workspace: str
    bus: Any | None = None
    subagent_manager: Any | None = None
    cron_service: Any | None = None
    sessions: Any | None = None
    file_state_store: Any = field(default=None)
    provider_snapshot_loader: Callable[[], Any] | None = None
    subagent_registry: Any = None
```

### 2.2 _mcp_lifecycle.py

构造点删除 `timezone="UTC",` 与 `workspace_sandbox=self.workspace_scopes.sandbox_status,` 两行。

### 2.3 subagent.py

`_build_tools`（246-265 行）构造点删除 `workspace_sandbox=workspace_sandbox_status(...)` 实参（259-262 行）。随后核查 `workspace_sandbox_status` 的 import（本文件头部）：若删除后无其他使用则同步删除 import；若仍有其他使用（如 `_filter_tools` 附近逻辑）则保留——以 `rg -n "workspace_sandbox_status" miniunicorn/agent/subagent.py` 结果为准，报告记录。

### 2.4 测试构造点核查

`rg -n "ToolContext\(" tests/ -g "*.py"` 全量过目：凡测试显式传 `timezone=` 或 `workspace_sandbox=` 的构造，删除该实参。预期数量很少（两字段本就无行为），逐处列出。

## 三、测试要求

1. **字段面回归锁**：新增 `tests/agent/tools/test_tool_context_fields.py`：

```python
def test_tool_context_field_set_is_exact():
    import dataclasses
    from miniunicorn.agent.tools.context import ToolContext

    names = {f.name for f in dataclasses.fields(ToolContext)}
    assert names == {
        "config", "workspace", "bus", "subagent_manager", "cron_service",
        "sessions", "file_state_store", "provider_snapshot_loader",
        "subagent_registry",
    }
```

用途：防止字段面再无声增长（新字段必须先证明有消费方再进这个集合）。

2. **消费方回归**：long_task / delegate / create_agent 的既有测试全绿（字段删除不影响它们读取的键）。
3. 子代理工具构建回归：既有 subagent 工具相关测试全绿。

## 四、禁改清单

- ToolContext 其余 9 个字段的名称、类型、默认值
- `RequestContext` / `ContextAware` / contextvar 机制（同文件，无关）
- `_build_tools` 的其余逻辑（scope 过滤、ToolsConfig 解析）
- self.py 的 `workspace_sandbox` 检查键（MyTool 的 INSPECTABLE 里有 `workspace_sandbox`——那是 AgentLoop 属性 `workspace_scopes.sandbox_status` 的展示名，与 ToolContext 字段同名但不同物，**不受本批影响**；若 W2-1 已实施，勿从 INSPECTABLE 中删除该键）

## 五、验收自检

- [ ] 全量测试绿，ruff 零告警
- [ ] `rg -n "timezone|workspace_sandbox" miniunicorn/agent/tools/context.py` 零命中
- [ ] 两处生产构造点 + 全部测试构造点已清理（报告列出每处）
- [ ] 字段面回归锁测试通过
- [ ] 与本规格的偏差逐条说明
