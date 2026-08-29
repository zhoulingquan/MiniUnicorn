# W1-2：长命服务生命周期归组合根（Cron / Subagent / MCP）

> 前置依赖：W1-1 已合并。
> 目标架构裁决（00-overview.md §二）：**长命服务的生杀权不在 AgentLoop**。组合根（`miniunicorn/composition/`）负责创建与销毁，AgentLoop 只持有引用消费能力。
> 本批 3 个独立 commit，每个 commit 单独跑全量测试。

## 现状盘点（已核实）

| 服务 | 创建 | 启动 | 关闭 | 归属判定 |
|---|---|---|---|---|
| CronService | 组合根（gateway.py:103 / agent_app.py:39） | gateway.py:395 `await self.cron.start()` | gateway.py:434 `self.cron.stop()` | **已达标**，需补守护测试 |
| SubagentManager | **AgentLoop.__init__**（loop.py:507-522） | 无显式启动 | 无显式关闭（任务自管理） | **违规**：核心内创建 |
| MCP 连接栈 | **AgentLoop.__init__**（loop.py:530-533 `_mcp_servers/_mcp_stacks`） | McpLifecycleMixin `_connect_mcp` | `close_mcp`（gateway.py:429 经 agent 调用） | **违规**：核心持有连接栈 |

---

## Commit 1：Cron 所有权守护（小）

Cron 已经在组合根，本 commit 只做两件事：

1. **审计 agent_app 路径**：`build_agent_application`（agent_app.py:20-42）构造 CronService 后**从不 start/stop**。核实 `cli agent`（一次性）与 `cli serve` 的语义：若 serve 是长驻入口而 cron 未启动，属既有行为，**不擅自加 start**——在 agent_app.py docstring 写明"CronService 在此路径仅构造注入，生命周期由调用方负责；当前仅 gateway 启动它"。
2. **守护测试**：新增 `tests/composition/test_cron_ownership.py`——
   - monkeypatch `CronService.start`/`stop` 为 spy，构造 AgentLoop 并跑一个最小 turn → 二者从未被调用；
   - gateway 路径（复用既有 gateway 测试设施）→ start 在启动序列中被调用。
   
   防止后续批次把 cron 生杀权悄悄搬回 AgentLoop。

---

## Commit 2：SubagentManager 创建上移（中）

### 改动

1. `AgentLoopConfig`（loop_builder.py，含 `with_cron_service` 的同一构建器）增加：
   ```python
   subagent_manager: "SubagentManager | None" = None
   def with_subagent_manager(self, manager): ...   # 与 with_cron_service 同型
   ```
2. `loop.py` 约 507 行改为：
   ```python
   self.subagents = (
       cfg.subagent_manager
       if cfg.subagent_manager is not None
       else SubagentManager(
           provider=self.provider, workspace=workspace, bus=bus,
           model=self.model, tools_config=_tc,
           max_tool_result_chars=self.max_tool_result_chars,
           restrict_to_workspace=cfg.restrict_to_workspace,
           disabled_skills=cfg.disabled_skills,
           max_iterations=self.max_iterations,
           max_concurrent_subagents=cfg.max_concurrent_subagents,
           llm_wall_timeout_for_session=lambda sk: runner_wall_llm_timeout_s(self.sessions, sk),
           max_subagent_recursion_depth=cfg.max_subagent_recursion_depth,
           turn_budget_factory=self._build_turn_budget,
           high_risk_policy=self.high_risk_policy,
       )
   )
   ```
   参数清单逐字保持现状（含 `provider=self.provider` 快照语义——provider 热切换对本批是既有行为，**不趁机改**）。
   `cfg.subagent_manager is None` 分支 = 直接构造路径（大量测试与 SDK 用法），保留为**合法构造形态**而非兼容垫片：注入则组合根所有，未注入则循环自建（单进程简易用法）。
3. `composition/gateway.py`：组装时经 builder `with_subagent_manager(SubagentManager(...))` 注入，参数与 loop.py 原构造逐项一致（gateway 需先取得 provider/model/sessions 等依赖——按 gateway.py 既有组装顺序，sessions 在 agent 构造前由 gateway 创建的路径若不存在，则本 commit 仅注入可组装的部分，其余依赖在批次报告中列出缺口，**不强拆**）。

### 附带审计项（只查不改，报告输出结论）

- SubagentManager 无显式 stop/aclose（已核实无此方法）。gateway `stop()`（gateway.py:416-445）对在飞子代理任务的处置：追 `SubagentManager.spawn_and_wait` 的任务跟踪集合在 gateway 关闭时的命运（被事件循环取消 or 悬挂），结论写进报告，处置方案留给后续批次。

### 测试

- `tests/agent/test_loop_builder*.py` 扩展：`with_subagent_manager` 注入的实例成为 `loop.subagents`（identity 断言）。
- 未注入路径：`loop.subagents` 仍是新建实例（既有测试已隐式覆盖，补一条 identity-not-None）。
- 守护：注入的 manager 不被 loop 重新包装/替换。

---

## Commit 3：MCP 连接栈上移 McpRuntime（大）

### 设计

新建 `miniunicorn/composition/mcp_runtime.py`：

```python
class McpRuntime:
    """MCP 连接栈的所有者。满足 tools/mcp.py 的 RuntimeState 协议。"""

    def __init__(self, servers: dict[str, Any] | None = None) -> None:
        self._mcp_servers: dict[str, Any] = dict(servers or {})
        self._mcp_stacks: dict[str, AsyncExitStack] = {}
        self._mcp_connected = False
        self._mcp_connecting = False

    def _set_mcp_servers(self, servers: dict[str, Any]) -> None: ...

    async def connect_missing(self, registry: ToolRegistry) -> None:
        from miniunicorn.agent.tools.mcp import connect_missing_servers
        await connect_missing_servers(self, registry)

    async def close_all(self) -> None:
        for name, stack in self._mcp_stacks.items():
            try:
                await stack.aclose()
            except Exception:
                logger.debug("MCP server '{}' cleanup error (can be ignored)", name)
        self._mcp_stacks.clear()
        self._mcp_connected = False
```

要点：
- `McpRuntime` 原生满足 `tools/mcp.py` 的 `RuntimeState` 协议（约 63-76 行：`_mcp_servers/_mcp_stacks/_mcp_connecting/_mcp_connected/_set_mcp_servers`），`connect_missing_servers(state, registry)` 直接以它为 state，**tools/mcp.py 零改动**。
- 生命周期方法体从 `McpLifecycleMixin.close_mcp`（_mcp_lifecycle.py:77-88）平移；`_background_tasks` 排空留在 loop（是 loop 的后台任务账本，不是 MCP 连接）。

### loop.py 接线

1. `AgentLoopConfig` 增加 `mcp_runtime: McpRuntime | None = None` + builder `with_mcp_runtime()`。
2. `__init__` 约 530-533 行：
   ```python
   self._mcp_runtime = cfg.mcp_runtime or McpRuntime(cfg.mcp_servers or {})
   ```
   loop 原有私有属性改为**委托属性**（保持 tools/mcp.py、webui/mcp_presets_api.py、RuntimeState 协议消费面零改动）：
   ```python
   @property
   def _mcp_servers(self): return self._mcp_runtime._mcp_servers
   @property
   def _mcp_stacks(self): return self._mcp_runtime._mcp_stacks
   ```
   `_mcp_connected/_mcp_connecting` 同型委托（读）。注意：`__init__` 里对这几个名字的**写**全部移除（初始状态由 McpRuntime 自带）。
3. `McpLifecycleMixin._connect_mcp` 改为 `await self._mcp_runtime.connect_missing(self.tools)`；`close_mcp` 改为"排空 `_background_tasks` + `await self._mcp_runtime.close_all()`"，保留方法签名（gateway.py:429 与 webui 调用面不破）。

### gateway.py 接线

1. 组装：`self.mcp_runtime = McpRuntime(config 配置的 servers 表)` → `with_mcp_runtime(self.mcp_runtime)`。
2. `stop()`（约 416-445 行）：MCP 段改为先 `await self.agent.close_mcp()`（排空 loop 后台任务，保留）再无需额外调用（close_mcp 已委托 runtime）；或直接 `await self.mcp_runtime.close_all()` + 排空——**二选一，以"排空必须发生在连接关闭前"为准**，批次报告说明所选路径。

### 测试

- `tests/composition/test_mcp_runtime.py`：
  1. McpRuntime 满足 RuntimeState 协议（isinstance 鸭子断言 + connect_missing_servers 可接受它）。
  2. close_all 关闭全部栈并清空；异常栈被吞（日志 debug）不中断其余关闭。
  3. 空栈 close_all 幂等。
- loop 集成：注入的 runtime 实例的 `_mcp_stacks` 与 `loop._mcp_stacks` 是同一对象（委托不拷贝）；未注入路径自建。
- 既有 MCP 测试全绿（tools/mcp.py 未动，webui 热重载路径经委托属性照常工作——若热重载路径对 `state._mcp_stacks` 有**写**操作（如 update/clear），委托属性返回的是 runtime 的真实 dict 引用，写仍然生效，逐处确认后写入报告）。

---

## 全批禁改清单

- SubagentManager 构造参数语义（含 provider 快照行为）
- `connect_missing_servers` / `connect_mcp_servers` / 热重载 reconcile 逻辑
- gateway stop() 的次序骨架（channels → MCP → cron → bus，只替换实现归属）
- 审批门 / checkpoint / 验收管道（W0 成果）
- 不为"架构完整"引入 McpManagerFactory 之类的额外层级——McpRuntime 就是全部

## 验收自检（每 commit 一报）

- [ ] 每 commit 全量测试绿 + ruff 零告警，独立可回滚
- [ ] Commit 2/3 的注入-回退双路径 identity 断言通过
- [ ] 两个审计项（cron serve 语义、子代理在飞任务关闭命运）结论入报告
- [ ] 与本规格的偏差逐条说明
