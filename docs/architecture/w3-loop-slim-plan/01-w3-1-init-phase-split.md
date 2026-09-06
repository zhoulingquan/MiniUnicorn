# W3-1:`AgentLoop.__init__` 装配分段方法化

> 前置依赖:tag `baseline-pre-w3`。W3 系列首批(热身批),建议在 W3-2 之前实施。
> 本批是**纯搬家重构**:语句逐句对应、零行为变更、零新抽象(无新类、无新配置、无新事件)。

## 一、问题(现状锚点)

`erza/agent/loop.py` 共 1346 行,类声明 `class AgentLoop(StateMixin, ProviderSwitchingMixin, McpLifecycleMixin)` 位于 186 行。`AgentLoop.__init__`(336-601)约 265 行线性装配巨方法,混杂八层关注点,构造顺序与所有权不可见。

### 1.1 注入参数(必须保留在 `__init__` 签名,不得移动)

W1-2 组合根注入点:`bus`、`workspace`、`config`、`dispatcher`、`session_turn`、`resources`、`turn_orchestrator`、`response`(以及 `**legacy` 兼容路径 `AgentLoopConfig(**legacy)`)。这些参数的解析与守卫逻辑(351-356 行:cfg 解析、provider 缺失 raise TypeError)保留在 `__init__`。

### 1.2 区段清单(编写时参考行号)

| 区段 | 行号 | 内容 |
|---|---|---|
| A 命令层 | 357-366 | `_tc`/`defaults` 局部量、bus、CommandRouter、CommandApplicationService、LazyToolRegistry(load_hook)、ResponseAssembler 回退、MessageDispatcher 回退、channels_config |
| B Provider 层 | 367-386 | ProviderRegistry、`self.provider=`、snapshot loaders、runtime_model_publisher、provider_signature、default_selection_signature、`self.workspace=`、`self.model=` |
| C 执行限制 | 387-451 | max_iterations 档位选择(FAST/MANAGED)、context_window_tokens 解析 + HF 自动探测(fail-loud)、resolved_context_window_tokens、context_block_limit、max_tool_result tokens/chars 双向派生、provider_retry_mode、tool_hint_max_length |
| D 策略与工作区 | 452-485 | planning_policy 解析(policy 显式优先,否则 from_use_planner)、use_planner/planner_model/planner_max_replans 兼容字段、reflection 字段、逐轮预算上限字段(managed/fast 两组)、tools_config/web_config/exec_config、cron_service、restrict_to_workspace、high_risk_policy、WorkspaceScopeResolver、_start_time、_last_usage、_extra_hooks |
| E 会话层 | 487-509 | ContextBuilder、SessionManager 回退、WebuiTurnCoordinator、`self.__dict__["_session_turn"]`(session_turn 注入或回退)、FileStateStore、AgentRunner |
| F Subagent 层 | 510-539 | SubagentManager(注入或回退构造,18 个实参)、SubagentRegistry + load + 挂到 context、_unified_session、_max_messages、McpRuntime(注入或回退) |
| G 资源层 | 540-569 | RuntimeResourceRegistry(注入或回退)、_default_root、_workspace_helpers_lock、_workspace_consolidators、_workspace_dreams 别名、model_presets、_active_preset + set_model_preset、_runtime_vars、_current_iteration |
| H 编排层 | 570-601 | TurnOrchestrator(注入或回退)+ TurnDeps(15 个晚绑定依赖) |

### 1.3 顺序敏感锚点(纯搬家的核心不变量)

1. **属性 setter 路由**:`self._provider_registry = ProviderRegistry(...)`(372-376)必须先于 `self.provider = cfg.provider`(377)、`self.model = ...`(386)、`self.context_window_tokens = ...`(408 起)——三者是 property setter(loop.py 237-279),写入路由进 `_provider_registry`。B 层内部顺序不得改变。
2. **`self.__dict__["_session_turn"]` 绕过 property**(500 行):绕开 `_session_turn` property(705-709,其 setter 触达 orchestrator)在构造期直写,防止对未就绪对象赋值。**逐字保留,不得改成 `self._session_turn = ...`**。
3. `self.context.subagent_registry = self.subagent_registry`(536)必须在 SubagentRegistry 构造并 load 之后(F 层内部顺序)。
4. `set_model_preset(cfg.model_preset, publish_update=False)`(567)必须在 `_resources` 就绪之后(读 `_resources` 相关状态)。
5. `LazyToolRegistry(load_hook=self._register_default_tools)` 的 load_hook 引用 McpLifecycleMixin 方法——A 层与 Mixin 的既有协作,保持原样。

## 二、改动

### 2.1 目标形态:`__init__` 瘦身为守卫 + 八次有序调用

```python
def __init__(
    self,
    bus: MessageBus,
    workspace: Path,
    *,
    config: AgentLoopConfig | None = None,
    dispatcher: MessageDispatcher | None = None,
    session_turn: SessionTurnService | None = None,
    resources: RuntimeResourceRegistry | None = None,
    turn_orchestrator: TurnOrchestrator | None = None,
    response: ResponseAssembler | None = None,
    **legacy: Any,
):
    from erza.config.schema import ToolsConfig

    cfg = config or AgentLoopConfig(**legacy)
    if cfg.provider is None:
        raise TypeError(...)  # 原文照搬
    _tc = cfg.tools_config or ToolsConfig()
    defaults = AgentDefaults()
    self._init_command_layer(bus, dispatcher, response, cfg)
    self._init_provider_layer(cfg, workspace)
    self._init_execution_limits(cfg, defaults)
    self._init_policy_and_workspace(cfg, workspace, _tc)
    self._init_session_layer(cfg, workspace, session_turn)
    self._init_subagent_layer(cfg, workspace, bus, _tc)
    self._init_resource_layer(cfg, workspace, resources, defaults)
    self._init_turn_orchestrator(turn_orchestrator)
```

### 2.2 八个阶段方法(签名与归属,语句逐句搬入)

```python
def _init_command_layer(self, bus, dispatcher, response, cfg) -> None:
    """消息/命令/工具注册表装配。A 区段。"""

def _init_provider_layer(self, cfg, workspace) -> None:
    """Provider registry 与模型标识装配。B 区段(含 workspace 归属)。"""

def _init_execution_limits(self, cfg, defaults) -> None:
    """迭代/上下文窗口/工具结果预算派生。C 区段。"""

def _init_policy_and_workspace(self, cfg, workspace, _tc) -> None:
    """规划策略、审批策略与工作区解析器装配。D 区段。"""

def _init_session_layer(self, cfg, workspace, session_turn) -> None:
    """上下文构建器、会话存储与单轮服务装配。E 区段。"""

def _init_subagent_layer(self, cfg, workspace, bus, _tc) -> None:
    """Subagent/注册表/MCP runtime(注入-回退双路径)。F 区段。"""

def _init_resource_layer(self, cfg, workspace, resources, defaults) -> None:
    """RuntimeResourceRegistry(注入-回退)与 per-workspace 别名。G 区段。"""

def _init_turn_orchestrator(self, turn_orchestrator) -> None:
    """TurnOrchestrator(注入-回退)与 TurnDeps 晚绑定依赖。H 区段。"""
```

参数注解类型以源码实际类型为准(`cfg: AgentLoopConfig`、`workspace: Path`、`_tc: ToolsConfig`、`defaults: AgentDefaults` 等)。

### 2.3 逐句搬入纪律(人工核对项)

- 每条语句连同其**引导注释**一起搬移(如 367-371 ProviderRegistry 注释、413-419 context window 自动探测注释、506-507 file state tracker 注释、540-544 资源所有权注释、571-573 晚绑定注释等);
- 区段内语句顺序、赋值目标、回退表达式(`x or Y(...)`)一律不变;
- 任何属性不得从赋值改为其他写法,任何 `self.__dict__` 直写不得改为 property 赋值;
- C 区段内 `from erza.cli.models import get_model_context_limit`(421 行,函数内 import)与 `DEFAULT_CONTEXT_LIMIT`(427 行)随语句搬入;
- F 区段 `SubagentManager(...)` 的 14 个实参(含两个 lambda:llm_wall_timeout、turn_budget_factory)逐字保留;
- H 区段 TurnDeps 的 24 个依赖(含 4 个 lambda)逐字保留;
- `AgentDefaults()` 只在 `__init__` 构造一次,以参数传入需要它的层(C、G),不得在各层重复构造。

### 2.4 不移动的内容

- 类属性 `_RUNTIME_CHECKPOINT_KEY` / `_PENDING_USER_TURN_KEY`(333-334)保持在类体;
- `AgentLoopConfig`(119-185)与所有 property(199-334)不动;
- 336-601 以外的所有方法不动(本批与 W3-2 互不重叠)。

## 三、测试要求

**核心纪律:本批不允许修改任何既有测试的断言**(纯搬家,行为零变更;若某测试失败说明搬错了,回退重搬)。允许的例外仅限纯导入路径调整(本批不移动任何符号,理论上不存在)。

新增 `tests/agent/test_loop_init_phase_split.py`:

1. **结构断言**:`AgentLoop.__init__` 源码行数 < 70(`inspect.getsource` 统计,防退化回装配巨方法)。
2. **构造冒烟**:仿 `tests/agent/conftest.py` 的 `make_loop` 直连构造后断言——
   - provider registry 一致性:`loop.provider` / `loop.model` / `loop.context_window_tokens` 与 `loop._provider_registry` 内当前值一致(验证 setter 路由未被破坏);
   - FAST 档(默认)`loop.max_iterations` 取 `AgentDefaults().fast_max_tool_iterations`,MANAGED 档(planning_policy=MANAGED)取 managed 值;
   - `max_tool_result_tokens=1000` 时 `max_tool_result_chars == 4000`(tokens×4 派生);
   - `planning_policy` 显式传入时优先生效。
3. **阶段顺序断言**:monkeypatch 包装八个 `_init_*` 方法记录调用名,断言 `__init__` 按 `_init_command_layer → _init_provider_layer → _init_execution_limits → _init_policy_and_workspace → _init_session_layer → _init_subagent_layer → _init_resource_layer → _init_turn_orchestrator` 顺序调用(装配顺序是本批核心不变量)。
4. **注入点冒烟**:`subagent_manager` / `mcp_runtime` 显式注入时被采纳(回退不触发)——已有同类断言的文件:`tests/agent/test_subagent_manager_ownership.py`、`tests/composition/test_mcp_runtime.py`。若这两处已覆盖注入语义则本批只需一条最小直连构造冒烟(断言 `loop._mcp_runtime is 注入实例`),不重复造测试。

## 四、禁改清单

- 一切行为语义:属性赋值顺序、回退表达式、注入-回退双路径、异常消息文本
- `AgentLoop.__init__` 签名(参数名、默认值、keyword-only 标记、`**legacy`)
- `AgentLoopConfig` 字段与 property 定义
- McpLifecycleMixin / ProviderSwitchingMixin / StateMixin 本体
- `_provider_registry` 的 setter 路由语义
- 不新增类 / 配置 / 事件 / 参数默认值变化

## 五、验收自检

- [ ] 全量测试绿且**零既有测试修改**,ruff 零告警
- [ ] `__init__` 行数 < 70(报告附 `inspect.getsource` 统计值)
- [ ] 八个阶段方法按序调用断言通过
- [ ] 属性 setter 路由冒烟断言通过(provider/model/context_window_tokens 与 registry 一致)
- [ ] 2.3 逐句搬入纪律逐项打勾(人工核对,报告列出)
- [ ] 与本规格的偏差逐条说明
