# W8-3a 任务书:删除 runner.py 的 41 个 compat 别名

> 性质:纯删除,零逻辑改动 · 规模:8 文件 · 前置评估:03-w8-3-runner-eval.md
> 沿革:41 个 `_name = name  # compat alias` 类级绑定,生产零使用,仅 7 个测试文件纯调用

## 0. 红线

1. 只删别名行与改测试调用点;方法本体一字不动
2. 不改任何方法行为、签名、docstring
3. 验证门一过立即 commit
4. 测试一律 `.venv\Scripts\python.exe`

## 1. 手术清单

### 1.1 runner.py:删除 41 行

删除所有含 `# compat alias` 的**类级**绑定行(如 `_execute_tools = execute_tools  # compat alias`)。
注意区分:`__init__` 里的 5 行实例级别名(224/226/231/233/238 行附近,如
`self._tool_execution = self.tool_execution`)是**运行时属性别名,保留不动**——
只删模块顶层类体内 `_name = name` 形式的行。

精确清单(41 行,行号为评估时快照,以内容匹配为准):

272/289/305/333/428/474/1084/1096/1111/1130/1141/1179/1200/1218/1234/1269/
1298/1331/1350/1405/1416/1427/1436/1442/1448/1454/1525/1544/1563/1648/1668/
1685/1706/1716/1726/1735(以上为 `_name = name` 类级,36 行)
+ 1442/1448/1454 是 usage 三件套的(重叠计入上述)
若实测少于 41 行,以内容匹配 `# compat alias` 且行首是 `_` 开头的类级绑定为准,如实记录数量。

### 1.2 测试调用点改名(7 文件)

下划线形态改公开形态(纯调用,无 monkeypatch):

| 文件 | 形态 |
|---|---|
| tests/agent/test_planner_results.py | `runner._init_planner` → `runner.init_planner` |
| tests/agent/test_planning_policy.py | `runner._init_planner` → `runner.init_planner` |
| tests/agent/test_progress_policy.py | `runner._handle_fatal_tool_error` → `runner.handle_fatal_tool_error` |
| tests/agent/test_replan_semantics.py | `runner._handle_fatal_tool_error` → `runner.handle_fatal_tool_error` |
| tests/agent/test_runner_governance.py | `runner._snip_history` → `runner.snip_history` |
| tests/agent/test_runner_injections.py | `runner._drain_injections` → `runner.drain_injections` |
| tests/agent/test_runner_tool_execution.py | `runner._execute_tools` → `runner.execute_tools` |

注意测试文件内可能有**变量名/fixture 名**恰好含这些词(如 `test_drain_injections_...`
函数名)——函数名、测试名、变量名一律不动,只改 `runner._method(` 的属性访问形态。

## 2. 验证门

```powershell
# 门 1:别名零残留
rg -n "compat alias" miniunicorn/agent/runner.py
# 期望:仅剩 __init__ 内 5 行实例级(若全部为类级则零命中)——如实记录实际剩余数并说明性质

# 门 2:相关测试全绿
.venv\Scripts\python.exe -m pytest tests/agent/test_planner_results.py tests/agent/test_planning_policy.py tests/agent/test_progress_policy.py tests/agent/test_replan_semantics.py tests/agent/test_runner_governance.py tests/agent/test_runner_injections.py tests/agent/test_runner_tool_execution.py -q

# 门 3:全量(后台 Start-Process + 轮询变体)
.venv\Scripts\python.exe -m pytest tests/ -q
# 期望 4147 passed / 0 failed / 29 skipped(纯删除,数字不变)

# 门 4:双 ruff 零
.venv\Scripts\python.exe -m ruff check miniunicorn/ tests/
.venv\Scripts\python.exe -m ruff format --check miniunicorn/ tests/
```

## 3. 提交

```
refactor(agent): drop 41 compat aliases from runner

The _name = name class-level bindings were left over from the
execution-service migration. Production code uses public names only;
seven test files had plain call sites on the underscore forms, now
re-pointed. Pure deletion, no logic change.
```
