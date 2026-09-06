# W1-1：工具惰性加载（LazyToolRegistry）

> 前置依赖：W0 全部批次已合并稳定（A1-A4、B）。
> 目标：Agent Core 构造期不再付出工具库的模块导入与实例化成本；工具库在首次被真正使用时物化。这是"core + tool library"边界在**装配时机**上的落地——核心循环可以在零工具状态下构造与运行管理面。

## 一、现状锚点（为什么现在不是惰性的）

1. `erza/agent/loop.py` 约 360 行：`self.tools = ToolRegistry()`（构造期）。
2. `erza/agent/loop.py` 约 562 行（`AgentLoop.__init__` 内，构造起始于约 333 行）：`self._register_default_tools()` —— **构造期立即执行**。
3. `_mcp_lifecycle.py` 约 34-61 行：`_register_default_tools` 构建 `ToolContext` 并 `ToolLoader().load(ctx, self.tools)`。
4. `tools/loader.py` 的 `discover()`：`pkgutil.iter_modules` + `importlib.import_module` **逐一导入全部工具模块**，再逐类 `tool_cls.create(ctx)` 实例化。

后果：gateway/CLI 只想做状态查询、配置校验、管理面操作时，也被迫付全量工具导入+实例化的成本；工具库与核心的构造期耦合在一起。

## 二、设计

### 2.1 LazyToolRegistry（tools/registry.py，与 ToolRegistry 同文件）

```python
class LazyToolRegistry(ToolRegistry):
    """首次读取时才执行装载钩子的注册表。register 不触发装载。"""

    def __init__(self, load_hook: Callable[[], None]) -> None:
        super().__init__()
        self._load_hook = load_hook
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True      # 先置位防装载过程递归触发
        self._load_hook()

    # 触发装载的四个读入口：
    def get(self, name): self._ensure_loaded(); return super().get(name)
    def has(self, name): self._ensure_loaded(); return super().has(name)
    def get_definitions(self): self._ensure_loaded(); return super().get_definitions()
    def prepare_call(self, name, params): self._ensure_loaded(); return super().prepare_call(name, params)

    # register/unregister 不触发装载：
    # MCP 工具在 connect 时注册、内建工具在首次读取时装载，
    # get_definitions 缓存重建时二者正确合并（顺序无关）。
```

要点：
- `_loaded` 先置位再调钩子：`_register_default_tools` 内部调用 `registry.register()`/`registry.has()` 不会递归触发装载。
- 钩子异常直接传播（loader 内部已有 per-tool try/except，钩子层面异常属装配 bug，应响亮失败）。
- 装载**至多一次**，失败不重置（半装载状态比反复装载可预测）。

### 2.2 loop.py 接线

1. 约 360 行改为：

```python
self.tools: ToolRegistry = LazyToolRegistry(load_hook=self._register_default_tools)
```

2. 删除约 562 行的 `self._register_default_tools()` 直接调用。
3. `self.tools` 保持**普通实例属性**（不是 property）：测试直接赋值 `loop.tools = ToolRegistry()` 覆盖整个注册表的行为不受影响（覆盖者自带规则）。

### 2.3 构造期依赖审计（实施时必做）

`_register_default_tools` 从构造期移到首次使用后，其 ToolContext 引用的属性（`subagents`、`cron_service`、`sessions`、`workspace_scopes`、`subagent_registry`、`_provider_snapshot_loader`）在首次工具使用时必须已就绪。逐项确认：

- 全部在 `__init__` 约 473-526 行之间赋值，任何合法的首次工具访问都发生在 `__init__` 返回之后 → 安全。
- 审计 `__init__` 尾部（约 539-596 行）是否有代码在赋值 `self.tools` 之后**读取**工具（如 `RuntimeResourceRegistry(tools=self.tools)` 只是传引用不算读；若内部 start() 调 get_definitions 则会提前触发装载——**正确性无碍，允许提前装载**，惰性是尽力而为）。
- `rg -n "self\.tools" erza/agent/loop.py` 全量过目并在批次报告列出每一处是否构成"构造期读取"。

### 2.4 MCP / MyTool 注册顺序兼容

- MyTool 在 `_register_default_tools` 内注册（走钩子，无变化）。
- MCP 工具经 `_connect_mcp` → `agent_context.connect_mcp(self, self.tools)` 注册：若发生在首次读取前，MCP 工具先进、内建后装；`get_definitions` 的缓存（`_cached_definitions`）在每次 register 时失效、下次读取重建，两类工具正确合并（builtins 排序在前、`mcp_` 前缀在后，现有逻辑不变）。

## 三、测试要求

新增 `tests/agent/test_lazy_tool_registry.py`：

1. **不读不装**：构造 LazyToolRegistry + spy 钩子 → 钩子未被调用。
2. **读触发且仅一次**：`has("x")` → 钩子调用 1 次；随后 `get`/`get_definitions`/`prepare_call` → 仍共 1 次。
3. **register 不触发**：装载前 `register(fake_tool)` → 钩子 0 次；之后 `get(fake_tool.name)` 触发装载且 fake_tool 仍在。
4. **递归防护**：钩子内部调用 `registry.has(...)`（模拟 _register_default_tools 行为）→ 不死循环。
5. **集成**：`AgentLoop.from_config(...)`（最小配置）构造完成后，monkeypatch/spy 断言 `_register_default_tools` 未执行；随后 `loop.tools.get("write_file")` 返回工具且钩子已执行。
6. **既有全量测试绿**：所有经 get/get_definitions 访问工具的路径自动装载，无需逐一修改。若存在直接访问 `registry._tools` 私有字典或断言构造期已注册的测试，**更新为经公开入口访问**（属测试实现细节修正，报告中列出）。

## 四、性能佐证（批次报告可选项，不阻塞合并）

用 `time.perf_counter()` 粗测两段：AgentLoop 构造耗时、首次 `get_definitions()` 耗时，改动前后对比各跑 3 次取中位数。目的仅是验证收益方向正确（构造变快），不设阈值。

## 五、禁改清单

- `ToolRegistry` 既有公开行为（get/has/register/get_definitions/prepare_call 的语义与缓存策略）
- `ToolLoader.discover` / `load` 逻辑
- `_register_default_tools` 的注册内容与 ToolContext 组装
- 审批门 / 安全策略（SafetyPolicy evaluate 在 run_tool 内，与本批无交集，但不得顺手重构）

## 六、验收自检

- [ ] 全量测试绿，ruff 零告警
- [ ] `self.tools` 构造期读取审计清单入报告
- [ ] 构造后未触发装载的集成断言（测试 5）通过
- [ ] 与本规格的偏差逐条说明
