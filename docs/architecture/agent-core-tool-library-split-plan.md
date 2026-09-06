# Erza Agent Core + Tool Library 拆分方案

## 1. 文档信息

| 项目 | 内容 |
|---|---|
| 文档类型 | 架构重构实施方案 |
| 适用分支 | 当前 main 分支 |
| 分析依据 | 当前源码、测试和 Git 历史 |
| README | 不作为分析依据 |
| 当前状态 | 方案阶段，尚未实施 |
| 目标 | 保留一个轻量、稳定、长期可维护的 Agent Core |

本方案服务于“小型企业长期使用的业务智能体”目标。系统暂不考虑多用户场景，也不设计多种 Agent 运行档位。

核心原则：

~~~
Agent Core + Tool Library
~~~

- Agent Core 负责稳定的单轮对话执行。
- Tool Library 负责所有可选能力。
- Planning、Cron、MCP、Subagent、Web Search 等都是工具库能力，不是 Agent Core 的运行模式。
- 工具按需注册、按需加载、按需执行。
- 保留兼容门面，分阶段迁移，不进行一次性大规模删除。

---

## 2. 当前源码现状

### 2.1 当前规模

| 模块 | 当前规模 | 主要问题 |
|---|---:|---|
| erza/agent/loop.py | 约 1300 行 | 同时承担组装、分发、运行时管理和 Agent 执行 |
| erza/agent/runner.py | 约 1570 行 | 已拆出多个服务，但仍保留兼容代理和高级模式逻辑 |
| erza/agent/memory.py | 约 2000 行 | 同时包含存储、结构化记忆、整理和 Dream |
| erza/agent/tools/ | 约 11000 行 | 工具种类多，加载和生命周期边界仍不清晰 |
| erza/agent/dispatch.py | 约 550 行 | 消息分发和 Agent 执行入口耦合 |
| erza/agent/context.py | 约 670 行 | 上下文、记忆、技能、MCP 辅助逻辑集中 |

### 2.2 主要源码依据

AgentLoop 构造函数位于：

~~~
erza/agent/loop.py:333
~~~

目前仍由 AgentLoop 接收或创建：

- Provider
- SessionManager
- MemoryStore 相关对象
- ToolRegistry
- SubagentManager
- CronService
- MCP 配置
- Planning 配置
- Reflection 配置
- RuntimeResourceRegistry
- 超时、预算和工具限制

AgentRunner 的主执行入口位于：

~~~
erza/agent/runner.py:469
~~~

其中仍然包含：

- Planner 初始化
- Reflection 初始化
- Planning Policy 判断
- FAST 到 MANAGED 的升级逻辑
- 模型调用
- 工具执行
- 预算判断
- 超时处理
- 注入消息
- 计划步骤推进
- 错误恢复

工具加载入口位于：

~~~
erza/agent/tools/loader.py:43
~~~

现有 Loader 会扫描工具包并导入模块，然后再判断工具是否启用，导致可选工具在启动阶段就产生导入成本。

### 2.3 工作区约束

当前工作区存在大量未提交修改和未跟踪文件，主要集中在：

- erza/agent/
- tests/agent/
- tests/tools/
- docs/
- 临时审计和评估目录

实施前必须建立基线，不能覆盖、重置或批量删除当前工作区内容。

---

## 3. 目标架构

### 3.1 总体依赖关系

~~~
渠道层 / WebUI / CLI / API
             │
             ▼
      AgentLoop 兼容门面
             │
             ▼
        AgentRuntime
             │
             ▼
       TurnController
             │
     ┌───────┼────────┐
     ▼       ▼        ▼
 Context  Model    Tool Gateway
 Manager  Port         │
     │       │         ▼
     ▼       ▼    Tool Registry
 Memory  Provider        │
 Port    Adapter         ▼
                   Tool Library
~~~

### 3.2 Agent Core 负责什么

Agent Core 只负责一次对话的稳定执行：

1. 接收标准化 TurnRequest。
2. 恢复当前 Session。
3. 读取必要的上下文和记忆。
4. 构建模型请求。
5. 请求模型并处理流式响应。
6. 识别模型返回的工具调用。
7. 调用安全策略和预算策略。
8. 通过 Tool Gateway 执行工具。
9. 将工具结果追加回当前对话。
10. 处理超时、取消、停止、重试和错误恢复。
11. 保存 Session、工具结果和运行时检查点。
12. 组装最终响应和遥测数据。

### 3.3 Agent Core 不负责什么

以下内容不得再直接写入 Agent Core：

- Planner 的创建和自动运行
- Reflection 的周期性触发
- Dream 的自动触发
- CronService 的持有和调度
- MCP 连接管理
- SubagentManager 的创建
- Web Search 后端选择
- 图片生成 Provider 选择
- CLI App 发现和加载
- 具体文件、Shell、网络工具实现
- SQLite、JSONL 等记忆存储细节

---

## 4. Tool Library 划分

| 工具库 | 当前主要实现 | 目标职责 |
|---|---|---|
| Workspace | tools/filesystem.py、apply_patch.py、search.py | 文件读写、编辑、搜索 |
| Shell | tools/shell.py、exec_session.py | 命令执行和长任务会话 |
| Web | tools/web.py、tools/web_search/ | 网页访问和搜索 |
| Planning | planner.py、execute_plan.py | 创建和推进计划 |
| Cron | tools/cron.py | 定时任务管理 |
| MCP | tools/mcp.py | MCP 服务和远程工具 |
| Subagent | subagent.py、spawn.py、delegate.py | 子 Agent 调用 |
| Research | tools/deep_research/ | 深度研究 |
| Apps | tools/cli_apps.py、image_generation/ | 外部应用和图片生成 |
| Runtime | tools/message.py、self.py、long_task.py | 消息、运行时状态、长任务 |

不再设计 fast、managed 等运行档位，只保留两类配置：

~~~
Turn 配置：一次对话最多运行多久、多少轮、多少 Token
Tool 配置：哪些工具库可用、哪些工具被禁用
~~~

示意：

~~~
tools:
  enabled:
    - filesystem
    - shell
    - web_search
    - planning
    - cron
    - mcp
    - subagent
~~~

工具是否启用，不代表 Agent 进入不同运行模式。

---

## 5. 核心接口设计

### 5.1 ToolDescriptor

建议新增：

~~~
erza/agent/core/contracts.py
~~~

~~~
@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    name: str
    description: str
    parameters: dict[str, Any]
    provider: str
    aliases: tuple[str, ...] = ()
    read_only: bool = False
    concurrency_safe: bool = False
    exclusive: bool = False
    risk_level: str | None = None
    implementation_ref: str | None = None
~~~

职责：

- 描述工具名称、说明和参数。
- 在启动阶段提供给模型。
- 不要求导入工具实现类。
- 支持真正调用时再加载实现。

### 5.2 ToolProvider

建议新增接口：

~~~
erza/agent/core/ports.py
~~~

~~~
class ToolProvider(Protocol):
    provider_id: str

    def descriptors(self, context: ToolContext) -> list[ToolDescriptor]:
        ...

    async def load_tool(
        self,
        name: str,
        context: ToolContext,
    ) -> ToolHandler:
        ...

    async def startup(self, context: ToolContext) -> None:
        ...

    async def shutdown(self) -> None:
        ...
~~~

Provider 负责：

- 工具描述
- 延迟导入
- 依赖初始化
- 工具执行入口
- 生命周期管理

### 5.3 ToolContext

当前 erza/agent/tools/context.py 中的 ToolContext 含有多个 Any 类型字段。建议逐步改成：

~~~
@dataclass(frozen=True)
class ToolContext:
    workspace: Path
    request: RequestContext
    config: ToolConfig
    services: ToolServices
~~~

~~~
@dataclass(frozen=True)
class ToolServices:
    model: ModelPort | None = None
    memory: MemoryPort | None = None
    session: SessionPort | None = None
    scheduler: SchedulerPort | None = None
    agent: AgentPort | None = None
    mcp: McpPort | None = None
    events: EventPort | None = None
~~~

工具不再直接读取 AgentLoop 的内部属性。

### 5.4 ToolResult

所有工具统一返回：

~~~
@dataclass(slots=True)
class ToolResult:
    content: str | list[dict[str, Any]]
    is_error: bool = False
    retryable: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
~~~

工具负责产生业务结果，Tool Gateway 负责统一错误格式，Agent Core 负责决定是否继续循环。

### 5.5 ModelPort

Agent Core 不直接依赖具体 LLMProvider：

~~~
class ModelPort(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        ...
~~~

现有 Provider 通过适配器接入，适配器可以放在：

~~~
erza/composition/model_gateway.py
~~~

### 5.6 MemoryPort

Agent Core 只依赖记忆接口：

~~~
class MemoryPort(Protocol):
    def recall(self, query: MemoryQuery) -> MemoryContext:
        ...

    def save_turn(self, turn: CompletedTurn) -> None:
        ...

    def create_checkpoint(self, checkpoint: TurnCheckpoint) -> None:
        ...
~~~

MemoryStore、StructuredMemoryRepository、Consolidator 和 Dream 都属于实现层。

---

## 6. 标准执行流程

~~~
收到用户消息
      │
      ▼
标准化 TurnRequest
      │
      ▼
SessionPort 恢复历史
      │
      ▼
MemoryPort 读取必要记忆
      │
      ▼
ContextManager 构建上下文
      │
      ▼
ModelPort 请求模型
      │
      ├── 无工具调用 ──► 保存并返回最终答案
      │
      └── 有工具调用
              │
              ▼
       SafetyController 检查
              │
              ▼
       BudgetController 检查
              │
              ▼
          ToolGateway
              │
              ▼
       ToolProvider 延迟加载
              │
              ▼
          执行工具
              │
              ▼
       追加 ToolResult
              │
              └────────► 再次请求模型
~~~

Agent Core 中只能保留这一条主循环，不再额外存在 Planner 循环、Managed 循环或 Subagent 专用循环。

---

## 7. 分阶段实施计划

## 阶段 0：建立工作区和行为基线

### 目标

确认当前工作区状态，避免重构覆盖用户已有修改。

### 任务

1. 保存 git status --short。
2. 保存 git diff --stat。
3. 分类已修改文件和未跟踪文件。
4. 区分功能修改、架构修改、测试修改和临时文件。
5. 记录当前公共导入路径。
6. 记录当前工具清单和默认启用状态。
7. 记录普通对话、工具调用、Session 恢复和中断恢复行为。

### 建议测试

~~~powershell
python -m pytest tests/agent tests/tools -q --basetemp=.tmp-core-baseline
~~~

### 完成标准

- 当前工作区状态已记录。
- 当前测试结果已记录。
- 未删除或重置任何用户文件。
- 后续每个阶段都有独立回滚点。

---

## 阶段 1：建立 Core 契约

### 新增文件

~~~
erza/agent/core/__init__.py
erza/agent/core/contracts.py
erza/agent/core/ports.py
erza/agent/core/tool_context.py
~~~

### 修改文件

~~~
erza/agent/tools/context.py
erza/agent/tools/base.py
~~~

### 任务

1. 定义 ToolDescriptor。
2. 定义 ToolResult。
3. 定义 ToolContext 和 ToolServices。
4. 定义 ToolProvider。
5. 定义 ModelPort。
6. 定义 MemoryPort。
7. 定义 SessionPort。
8. 定义 AgentPort。
9. 让旧 tools.context.ToolContext 重新导出新类型。
10. 保留现有 Tool 抽象类，暂不重写所有工具。

### 测试文件

~~~
tests/agent/core/test_contracts.py
tests/agent/core/test_ports.py
~~~

### 完成标准

- Core 契约模块可以独立导入。
- Core 契约不导入具体工具实现。
- 现有工具可以继续使用旧接口。

### 回滚点

删除本阶段新增契约，并恢复旧 tools.context 导出方式即可，不影响工具实现。

---

## 阶段 2：重构 ToolLoader 和 ToolRegistry

### 新增文件

~~~
erza/agent/tools/catalog.py
erza/agent/tools/provider.py
erza/agent/tools/adapters.py
~~~

### 修改文件

~~~
erza/agent/tools/loader.py
erza/agent/tools/registry.py
erza/agent/tools/base.py
~~~

### 任务

1. 引入 ToolCatalog。
2. 为每个工具记录 Provider、Schema 和实现引用。
3. 修改 Loader，使启动时只读取描述信息。
4. 修改 Registry，使其支持描述注册和实现延迟加载。
5. 增加 LegacyToolProvider。
6. 使用适配器包装现有 Tool 子类。
7. 保持现有工具别名和名称冲突行为。
8. 禁止通过全量 pkgutil 扫描导入所有工具实现。

### 目标流程

~~~
启动
  └── 读取描述信息

模型调用工具
  └── Registry 找到 Provider
        └── Provider 延迟导入实现
              └── 执行工具
~~~

### 测试文件

~~~
tests/tools/test_tool_loader_lazy.py
tests/tools/test_tool_registry_lazy.py
tests/tools/test_tool_provider_adapter.py
tests/tools/test_tool_import_isolation.py
~~~

### 完成标准

- 禁用工具的实现模块不被导入。
- 未调用的可选工具不被初始化。
- 现有工具仍可执行。
- 原有工具 Schema 保持兼容。

### 回滚点

保留现有 ToolLoader.load() 作为 fallback，Provider 加载失败时回退到 LegacyToolProvider。

---

## 阶段 3：抽取单一 TurnController

### 新增文件

~~~
erza/agent/core/turn_controller.py
erza/agent/core/model_gateway.py
erza/agent/core/tool_gateway.py
erza/agent/core/budget_controller.py
erza/agent/core/safety_controller.py
erza/agent/core/recovery.py
~~~

### 迁移来源

| 新模块 | 当前来源 |
|---|---|
| turn_controller.py | runner.py 主循环 |
| model_gateway.py | execution/model_request.py |
| tool_gateway.py | execution/tool_execution.py |
| budget_controller.py | turn_budget.py 和 runner.py |
| safety_controller.py | safety_policy.py 和 runner.py |
| recovery.py | execution/recovery.py |

### 任务

1. 将 AgentRunner._run_with_ledger() 的主循环迁移到 TurnController。
2. 保留一个模型请求入口。
3. 保留一个工具执行入口。
4. 将工具结果标准化集中到 ToolGateway。
5. 将超时、取消、预算和安全判断集中到 Core 控制器。
6. 将每轮可变状态集中到 TurnState。
7. 删除主循环中的 Planner 初始化。
8. 删除主循环中的 Reflection 初始化。
9. 删除主循环中的 FAST/MANAGED 分支。
10. 保留流式输出、工具审批、检查点和中断恢复行为。

### AgentRunSpec 简化

保留：

- 初始消息
- 工具集合
- 模型
- 最大迭代次数
- Token 限制
- 时间限制
- 工作区
- Session 标识
- Hook
- 取消回调
- 审批回调

移除：

- use_planner
- planner_model
- planner_max_replans
- planning_policy
- enable_reflection
- reflection_interval
- fast / managed 专属字段

### 兼容策略

暂时保留 erza.agent.runner.AgentRunner，但降级为薄门面：

~~~
class AgentRunner:
    async def run(self, spec):
        return await self._controller.run(spec)
~~~

### 测试文件

~~~
tests/agent/core/test_turn_controller.py
tests/agent/core/test_model_gateway.py
tests/agent/core/test_tool_gateway.py
tests/agent/core/test_turn_limits.py
tests/agent/core/test_turn_recovery.py
~~~

### 完成标准

- Core 中只有一个 Agent 执行循环。
- 普通对话不初始化 Planner 和 Reflection。
- 旧 Runner 公共导入路径仍然可用。
- 工具调用、错误恢复、审批和超时行为不变。

---

## 阶段 4：简化 AgentLoop 和运行时组装

### 新增文件

~~~
erza/agent/core/runtime.py
erza/composition/agent_runtime.py
~~~

### 修改文件

~~~
erza/agent/loop.py
erza/agent/loop_builder.py
erza/agent/dispatch.py
erza/composition/gateway.py
~~~

### 任务

1. 将 AgentRuntime 的组装逻辑移到 Composition 层。
2. AgentLoop 只保留消息分发和兼容代理职责。
3. 移除 AgentLoop 对具体 Tool 类的依赖。
4. 移除 AgentLoop 对 SubagentManager 的直接创建。
5. 移除 AgentLoop 对 CronService 的直接持有。
6. 移除 AgentLoop 对 MCP 生命周期的直接管理。
7. 将 Provider、Memory、Session、ToolProviders 统一注入 AgentRuntime。
8. 保留旧构造参数的兼容转换，但不再让构造函数承载业务逻辑。
9. 将 AgentLoopBuilder 的配置收敛为 Core 配置和 Tool 配置。

### 最终关系

~~~
MessageDispatcher
      ↓
AgentLoop 兼容门面
      ↓
AgentRuntime
      ↓
TurnController
~~~

### 目标规模

~~~
loop.py：300～400 行以内
runner.py：400～500 行以内
~~~

### 测试文件

~~~
tests/agent/test_loop_composition.py
tests/agent/test_loop_compatibility.py
tests/agent/test_runtime_dependencies.py
~~~

---

## 阶段 5：迁移 Planning、Cron、MCP、Subagent

### 5.1 Planning Provider

目标目录：

~~~
erza/agent/tools/planning/
├── __init__.py
├── provider.py
├── tool.py
├── state.py
└── models.py
~~~

迁移来源：

~~~
erza/agent/planner.py
erza/agent/planning_policy.py
erza/agent/execution/planning.py
erza/agent/tools/execute_plan.py
erza/agent/plan_snapshot.py
~~~

工具：

~~~
create_plan
get_plan
update_plan
complete_plan_step
replan
~~~

运行逻辑：

~~~
模型判断任务复杂
      ↓
调用 create_plan
      ↓
PlanningProvider 保存计划状态
      ↓
返回计划内容
      ↓
模型继续调用其他工具
~~~

Planning 不再自动触发，也不再产生 FAST → MANAGED 升级。

测试：

~~~
tests/tools/planning/test_planning_provider.py
tests/tools/planning/test_planning_state.py
tests/agent/test_normal_turn_does_not_plan.py
~~~

### 5.2 Reflection 和 Dream

reflection.py 和 Dream 不再由 Agent Core 自动周期性调用。

可选实现：

- 转换成 reflect 工具。
- 转换成 Memory Maintenance 服务。
- 由 Cron 工具定期触发。
- 由用户明确请求时执行。

普通业务对话默认不触发 Reflection 和 Dream。

### 5.3 Cron Provider

目标文件：

~~~
erza/agent/tools/cron/provider.py
~~~

依赖接口：

~~~
SchedulerPort
~~~

任务：

1. 将创建、修改、暂停、删除、查询任务封装为工具。
2. Agent Core 不再持有 cron_service。
3. Cron Provider 自己管理调度器生命周期。
4. 禁用 Cron 时不加载 Cron 服务实现。

### 5.4 MCP Provider

目标目录：

~~~
erza/agent/tools/mcp/
├── provider.py
├── client.py
└── registry.py
~~~

任务：

1. 将 MCP 连接逻辑移入 Provider。
2. 将远程工具转换成 ToolDescriptor。
3. 远程工具调用统一返回 ToolResult。
4. 连接、重连和关闭由 Provider 管理。
5. 从 AgentLoop 删除 MCP 生命周期方法和 Mixin。
6. 将 context.py 中的 MCP 辅助逻辑移出。

### 5.5 Subagent Provider

目标目录：

~~~
erza/agent/tools/subagent/
├── provider.py
├── tool.py
├── registry.py
├── lifecycle.py
└── models.py
~~~

运行逻辑：

~~~
主 Agent 调用 delegate
      ↓
SubagentProvider 创建子任务
      ↓
子任务复用同一个 TurnController
      ↓
子任务使用独立 ToolContext 和 Session
      ↓
返回 ToolResult
~~~

必须保留：

- 最大递归深度
- 最大并发数
- 子任务独立预算
- 子任务独立取消
- 子任务工具白名单
- 子任务结果长度限制

Subagent 不允许重新复制一整套 AgentLoop。

---

## 阶段 6：隔离 Memory、Session 和 Context

### 6.1 Memory 适配层

第一步不移动现有 2000 多行 memory.py，先增加：

~~~
erza/agent/core/memory_port.py
erza/agent/memory_adapter.py
~~~

MemoryAdapter 包装现有 MemoryStore，让 Core 只依赖 MemoryPort。

### 6.2 Memory 实现层

行为稳定后，再逐步迁移为：

~~~
erza/memory/
├── store.py
├── models.py
├── repository.py
├── lifecycle.py
├── recall.py
├── maintenance.py
└── adapters.py
~~~

旧的 erza/agent/memory.py 暂时保留为兼容导出门面。

### 6.3 Session Gateway

新增：

~~~
erza/agent/core/session_gateway.py
~~~

负责：

- 恢复历史消息
- 保存用户消息
- 保存助手消息
- 保存工具调用和结果
- 保存运行时检查点
- 恢复中断任务

当前 SessionTurnService 改造成 SessionPort 的适配器。

### 6.4 Context Manager

新增或迁移为：

~~~
erza/agent/core/context_manager.py
~~~

ContextManager 只接收：

- 历史消息
- 当前消息
- MemoryContext
- ToolDescriptor
- Workspace 信息
- Token 限制

它不直接调用 MCP、Planner、Subagent 或具体工具。

### 6.5 RuntimeResourceRegistry

当前 runtime_resources.py 中的资源创建逻辑迁移到 Composition 层。

它可以继续负责缓存 Workspace 级资源，但不能再成为 Agent Core 的隐式依赖。

---

## 阶段 7：删除运行档位和冗余逻辑

在所有引用清理完成后，删除或降级：

- PlanningMode
- PlanningPolicy
- use_planner
- planner_model
- planner_max_replans
- fast / managed 专属预算
- fast / managed 专属迭代次数
- AgentLoop 中的 Planner 初始化
- AgentLoop 中的 Reflection 初始化
- AgentLoop 中的 MCP 生命周期
- AgentLoop 中的 SubagentManager 创建
- AgentLoop 中的 CronService 持有
- ToolLoader 的全量模块扫描
- Runner 中的高级模式分支
- execution/planning.py 中的 Core 规划逻辑

删除前执行：

~~~powershell
rg -n "PlanningMode|PlanningPolicy|use_planner|enable_reflection|planner_model|SubagentManager|McpLifecycleMixin" erza tests
~~~

确认只有兼容层或工具库引用后，再分别提交删除。

兼容窗口至少保留：

- AgentLoop 旧导入路径
- AgentRunner 旧导入路径
- agent.memory 旧导出路径
- 旧配置字段的转换逻辑

---

## 8. 测试方案

### 8.1 Core 单元测试

~~~
tests/agent/core/test_contracts.py
tests/agent/core/test_turn_controller.py
tests/agent/core/test_model_gateway.py
tests/agent/core/test_tool_gateway.py
tests/agent/core/test_turn_limits.py
tests/agent/core/test_turn_recovery.py
~~~

覆盖：

- 无工具调用直接返回。
- 一次模型调用后执行工具，再次调用模型。
- 多个工具调用的顺序。
- 工具错误是否正确返回模型。
- 超时是否停止。
- 预算超限是否停止。
- 取消是否保存检查点。
- 流式响应是否正常结束。

### 8.2 Tool Library 测试

~~~
tests/tools/test_tool_loader_lazy.py
tests/tools/test_tool_registry_lazy.py
tests/tools/test_tool_provider_adapter.py
tests/tools/test_tool_import_isolation.py
~~~

覆盖：

- 禁用工具不加载实现。
- 未调用工具不初始化依赖。
- 工具描述和工具实现可以分开加载。
- Provider 加载失败不会破坏其他工具。
- 工具错误统一转换成 ToolResult。

### 8.3 高级工具测试

~~~
tests/tools/planning/test_planning_provider.py
tests/tools/cron/test_cron_provider.py
tests/tools/mcp/test_mcp_provider.py
tests/tools/subagent/test_subagent_provider.py
~~~

覆盖：

- Planning 只在模型调用时执行。
- Cron 不影响普通对话。
- MCP 未启用时不建立连接。
- Subagent 使用同一个 Core，不复制执行循环。
- Subagent 递归和预算限制仍然生效。

### 8.4 兼容性测试

~~~
tests/agent/test_loop_compatibility.py
tests/agent/test_runner_compatibility.py
tests/agent/test_session_checkpoint_compatibility.py
tests/agent/test_memory_import_compatibility.py
~~~

---

## 9. 验收指标

### 9.1 架构指标

- Core 不直接导入具体工具实现。
- Core 不直接导入 Planner、MCP、Cron、Subagent 实现。
- Agent 主执行循环只有一个。
- ToolProvider 是高级能力的统一入口。
- ToolLoader 不再全量导入工具模块。

### 9.2 行为指标

- 普通对话不触发 Planning。
- 普通对话不触发 Reflection。
- 普通对话不触发 Dream。
- Session 数据格式不变。
- 中断恢复行为不变。
- 工具审批行为不变。
- 原有公共导入路径保持兼容。

### 9.3 规模指标

| 文件 | 目标 |
|---|---:|
| agent/loop.py | 不超过 300～400 行 |
| agent/runner.py | 不超过 400～500 行 |
| core/turn_controller.py | 不超过 400 行 |
| core/context_manager.py | 不超过 400 行 |
| tools/loader.py | 不超过 250 行 |
| Core 直接依赖具体工具数量 | 0 |
| Agent 主执行循环数量 | 1 |

### 9.4 延迟加载指标

- 未启用 MCP 时不导入 MCP 依赖。
- 未启用图片生成时不导入图片 Provider。
- 未调用 Web Search 时不初始化搜索后端。
- 未调用 Subagent 时不创建 SubagentManager。
- 未调用 Planning 时不创建 Planner。

---

## 10. 风险和应对策略

| 风险 | 影响 | 应对措施 |
|---|---|---|
| 现有测试依赖内部属性 | 重构后测试失败 | 保留兼容属性和门面 |
| ContextVar 并发污染 | 多任务结果错误 | 保留每轮 bind/reset，并增加并发测试 |
| MCP 生命周期遗漏 | 连接泄漏 | 由 McpProvider 统一 startup/shutdown |
| Subagent 递归失控 | 资源耗尽 | 将深度、并发、预算统一放入 AgentPort |
| Session 检查点不兼容 | 中断任务无法恢复 | 先适配、后移动，不改变数据结构 |
| Memory 行为变化 | 历史召回异常 | 第一阶段只增加 MemoryAdapter，不移动实现 |
| 可选依赖导入失败 | 启动失败 | 描述和实现分离，调用时隔离异常 |
| 工作区已有修改被覆盖 | 用户代码丢失 | 阶段 0 建立基线，禁止 reset 和批量删除 |

---

## 11. 推荐提交顺序

每个阶段独立提交：

~~~
1. chore(agent): capture core split baseline
2. refactor(agent): add core contracts and ports
3. refactor(tools): add lazy tool catalog
4. refactor(tools): add legacy tool provider adapter
5. refactor(agent): extract single turn controller
6. refactor(agent): simplify AgentLoop facade
7. refactor(tools): migrate planning provider
8. refactor(tools): migrate cron and mcp providers
9. refactor(tools): migrate subagent provider
10. refactor(agent): isolate memory and session ports
11. refactor(agent): remove planning modes and reflection branches
12. test(agent): add core and lazy-loading regression coverage
~~~

每个提交完成后执行对应测试，不把所有移动、接口变化和删除集中到一个提交中。

---

## 12. 最终结论

本次重构不建议直接删除 Planning、Cron、MCP、Subagent 等功能，而应先完成边界迁移：

~~~
Agent Core   = 稳定的对话执行内核
Tool Library = 所有按需调用的业务能力
~~~

最关键的三个动作是：

1. 把 AgentLoop 降级为兼容门面。
2. 把 AgentRunner 收敛为唯一的 TurnController。
3. 让所有工具通过 ToolProvider 延迟加载和执行。

完成后，普通业务对话只加载必要组件，高级功能仍然可用，但不会污染核心执行路径，能够满足高效、简洁、易维护和长期运行的目标。

