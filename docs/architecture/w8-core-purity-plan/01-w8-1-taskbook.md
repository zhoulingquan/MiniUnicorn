# W8-1 任务书:composition 守护修复 + McpRuntime 归位 tools

> 性质:纯搬家(git mv)+ import 改写 + 守护修复,**零逻辑改动**
> 规模:~8 文件;预计 10 分钟内完成(无全量 pytest 前置依赖,门内仍要跑)

## 0. 红线

1. 只用 `git mv`;`git log --follow` 必须能追溯搬家前历史
2. 零逻辑改动:只改 import 行与守护匹配逻辑;McpRuntime 类体一字不动
3. 不留 shim:旧路径 `miniunicorn.composition.mcp_runtime` 彻底消失
4. 守护修复只影响匹配精度,不得引入新豁免
5. 验证门一过立即 commit

## 1. 手术清单

### 1.1 搬家(1 文件)

```powershell
git mv miniunicorn/composition/mcp_runtime.py miniunicorn/tools/mcp_runtime.py
```

依据:该文件 56 loc,依赖仅 `tools.registry.ToolRegistry` 与
`tools.mcp.connect_missing_servers`(intra-tools),类功能是 MCP 运行时持有/连接/关闭
——纯库代码,归 tools 后 agent→tools 为既有合法方向。

### 1.2 生产消费方改指向(3 文件)

| 文件 | 行 | 改写 |
|---|---|---|
| agent/loop.py | 49 | `from miniunicorn.composition.mcp_runtime import McpRuntime` → `from miniunicorn.tools.mcp_runtime import McpRuntime` |
| agent/loop_builder.py | 39 | 同上 |
| composition/gateway.py | 65 | 同上(composition 可引用一切,仅路径更新) |

### 1.3 测试(2 文件)

| 文件 | 手术 |
|---|---|
| tests/composition/test_mcp_runtime.py | git mv → `tests/tools/test_mcp_runtime.py`,import 行改指向新家 |
| tests/agent/test_loop_init_phase_split.py:21 | import 行改指向新家 |

### 1.4 守护修复(1 文件,唯一逻辑改动)

`tests/architecture/test_dependency_direction.py` 的
`test_business_modules_do_not_import_composition`(行 131–141):

现匹配 `target.split(".")[0] == "composition"` 漏掉 `miniunicorn.` 前缀全限定形式。
修复为同时匹配两种形式(裸 `composition` 与 `miniunicorn.composition`):

```python
if target == "composition" or target.startswith("composition.") \
        or target == "miniunicorn.composition" or target.startswith("miniunicorn.composition."):
    violations.append(...)
```

(以文件内实际代码风格为准;确保 `from miniunicorn.composition.mcp_runtime import X`
迁移后不再命中——迁移后 agent 里已无 composition import,修复后的守护必须全绿。)

### 1.5 文档(1 文件)

`docs/architecture/module-boundaries.md`:

- §2.1 composition:公开 API 清单中移除 McpRuntime(若有列名)
- tools 相关小节(§1/§2 合适位置)登记 `tools/mcp_runtime.py`(McpRuntime,自 composition 归位,W8-1)
- §1.1 第一行"现状"更新:守护修复后为真实 ✅(可注明 W8-1 修复了匹配盲区)

## 2. 验证门

```powershell
# 门 1:零残留
rg -n "composition.mcp_runtime|composition/mcp_runtime" miniunicorn/ tests/
# 期望零命中(退出码 1);注意别匹配到 git mv 后 docs 中合法的历史记录——只查 miniunicorn/ tests/

# 门 2:守护(修复后)必须绿
.venv\Scripts\python.exe -m pytest tests/architecture/test_dependency_direction.py -q

# 门 3:冷导入
.venv\Scripts\python.exe -c "import miniunicorn.agent; import miniunicorn.composition.gateway; print('ok')"

# 门 4:相关测试
.venv\Scripts\python.exe -m pytest tests/tools/test_mcp_runtime.py tests/agent/test_loop_init_phase_split.py -q

# 门 5:全量(后台 Start-Process + 轮询变体)
.venv\Scripts\python.exe -m pytest tests/ -q
# 期望 4146 passed / 0 failed / 29 skipped(纯搬家,数字不变)

# 门 6:双 ruff 零 + 历史追踪
.venv\Scripts\python.exe -m ruff check miniunicorn/ tests/
.venv\Scripts\python.exe -m ruff format --check miniunicorn/ tests/
git log --follow --oneline miniunicorn/tools/mcp_runtime.py   # > 1 条
```

## 3. 提交

```
refactor(tools): home McpRuntime from composition to tools library

McpRuntime (56 loc) depends only on tools.registry/tools.mcp — pure
library code misplaced in the assembly layer. Fixes the composition
import guard's blind spot for miniunicorn.-prefixed imports; agent/loop
and loop_builder re-point, closing the only two business->composition
violations.
```
