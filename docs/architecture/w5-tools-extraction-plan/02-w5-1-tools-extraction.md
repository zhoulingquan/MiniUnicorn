# W5-1:agent/tools → erza/tools 外置(目录移动 + 全量前缀重写)

> 前置依赖:W5-0 已合并(循环导入阻塞已解除)。基线 passed 数以 W5-0 合并后为准(W5-0 前 4129 + 2 新增 = 4131)。
> 本批为**纯搬家**:目录整体迁移 + 单一前缀字符串替换 + 一项授权守护更新。包内文件除 import 前缀外零改动。

## 一、变更方案(按序)

### 1.1 目录移动

```
git mv erza/agent/tools erza/tools
```

必须用 `git mv`(保历史)。移动后 `erza/tools/` 结构与原 `erza/agent/tools/` 完全一致(web_search/、deep_research/、image_generation/ 子包原样)。

### 1.2 全局前缀重写

对整个仓库(`erza/` + `tests/`)执行字符串替换:

```
erza.agent.tools  →  erza.tools
```

覆盖四类引用,**一个都不能漏**:

| 引用类型 | 位置 | 量级 |
|---|---|---|
| import 语句 | 包内 55 文件自引用 + 外部消费者约 20 文件(agent 核心 13:loop/runner/context/dispatch/response/subagent/turn_orchestrator/runtime_resources/safety_policy(TYPE_CHECKING)/_mcp_lifecycle/context_governor/runner_strategies/execution\tool_execution;channels/websocket/channel;cli/_gateway_runner;composition/gateway + mcp_runtime;config/schema;webui/image_generation_api + mcp_presets_api) | ~150 处 |
| 字符串字面量-懒加载 | `config/schema.py` 8 处 `_lazy_default("erza.agent.tools.web", ...)` 等默认工厂 | 8 处 |
| 字符串字面量-patch 目标 | 63 个测试文件中的 `patch("erza.agent.tools.shell._IS_WINDOWS", ...)`、`monkeypatch.setattr("erza.agent.tools.mcp.connect_mcp_servers", ...)` 等 | ~70 处 |
| 函数内默认包导入 | `tools/loader.py:35` `import erza.agent.tools as _pkg` | 1 处 |

注释中的旧路径同步重写:已知 `webui/web_search_api.py:23` 与 `tools/web_search/circuit_breaker.py:3`(移动后路径)两处注释引用旧路径。

**推荐做法**:写一次性脚本对 `erza/` 与 `tests/` 下全部 .py 做前缀替换(确定性、完备),跑完后删除脚本;或用编辑器全局替换。无论哪种方式,以 1.4 的零残留扫描为准。

**loader 自适应项(勿动)**:`loader.py:50` `pkgutil.iter_modules(self._package.__path__)`、`loader.py:54` `importlib.import_module(f".{module_name}", self._package.__name__)`、entry_points 组名 `erza.tools`——三者天然适配新位置。

### 1.3 授权守护更新(仅此一项)

`tests/architecture/test_dependency_direction.py` 的 `AGENT_IMPORT_EXEMPTIONS` 删除条目:

```python
("channels/websocket/channel", "erza.agent.tools.mcp"),
```

原因:channels/websocket/channel.py 重写后导入 `erza.tools.mcp`,不再以任何形式导入 agent——该过渡期豁免自然消解(测试的 stale 检查会强制要求删除,这是预期收益而非规避)。**同时同步 `docs/architecture/module-boundaries.md`** 中对应的豁免清单段落(§channels→agent 例外说明),注明 tools 外置(W5)后该依赖消解。

除上述两处外,守护测试与 module-boundaries.md 的其他内容零改动。

### 1.4 新测试(新建 `tests/tools/test_tools_package_split.py`)

1. `test_tools_package_location`:`erza/agent/tools` 路径不存在;`erza/tools/__init__.py` 存在(以 `Path` 断言,root 取 `Path(erza.__file__).parent`)
2. `test_no_legacy_agent_tools_references`:扫描 `erza/` 下全部 .py 文本(排除 `__pycache__`),断言零处含 `"erza" + ".agent" + ".tools"` 拼接串——固化零残留
3. `test_cold_import_regression`:`subprocess.run([sys.executable, "-c", "import erza.tools.message; import erza.tools.self"])` 断言 returncode == 0——冷导入回归(W5-0+W5-1 前此测试必失败,是本系列的核心回归防护)
4. `test_registry_identity_via_new_path`:`from erza.tools import ToolRegistry` 可导入且 `erza.tools.registry.ToolRegistry is ToolRegistry`(门面 re-export 冒烟)

## 二、不可触碰清单

- 包内 55 文件除前缀替换外零改动(loader 发现顺序、`_SKIP_MODULES`、注册逻辑、全部类与函数体)
- 消费者与测试只改 import/patch 字符串,断言与逻辑零修改
- `erza/agent/` 下任何文件的结构性改动(除前缀替换)
- W5-0 成果(security/risk.py、safety_policy re-export)零改动
- pyproject.toml 零改动(扁平打包自动含新子包)
- 守护测试除 1.3 授权项外零改动;任何其他守护失败=停下报告

## 三、验收清单

- [ ] 全量测试绿(passed ≥ W5-0 合并后基线 + 新增 4)且既有断言零修改;ruff 零告警
- [ ] `git diff --stat` 不含 pyproject.toml、composition/ 结构性改动
- [ ] 零残留:`rg "erza\.agent\.tools" erza/ tests/` 仅守护授权删除后无命中(docs/ 历史文档除外)
- [ ] 冷导入回归测试通过(message/self 作为进程首个 erza 导入)
- [ ] `git log --follow erza/tools/filesystem.py` 可追溯至旧路径(历史未断)
- [ ] 偏差逐条说明

## 四、风险与对策

| 风险 | 对策 |
|---|---|
| 字符串字面量漏改(config 懒加载、测试 patch 目标) | 全局前缀替换脚本 + 零残留扫描测试兜底 |
| 循环导入回归 | W5-0 前置 + 冷导入回归测试 |
| 守护 stale 检查误报 | 豁免删除是授权项,任务书已列明 |
| Windows 路径/编码问题 | 替换脚本统一 utf-8 读写;`utf-8-sig` 容错 |
| 全量测试耗时长 | Start-Process 后台跑 + 轮询日志(提示词已写入) |
