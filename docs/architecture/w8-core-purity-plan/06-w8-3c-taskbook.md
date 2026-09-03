# W8-3c 任务书:violation 分类族归位 tool_execution,planning 专属 static 归位 planning

> 性质:真逻辑搬家(方法 → 模块函数),行为零变化 · 规模:~4 文件
> 前置:W8-3a(4658d45e)、W8-3b(6ef389e9)已完成
> 调用点数据已程序化核验(2026-09-03,W8-3b 后行号快照;执行时以内容匹配为准)

## 0. 红线

1. 函数体逐字搬(仅去 ``self``/``cls`` 前缀与 ``@staticmethod``/``@classmethod`` 装饰,其余一字不动)
2. 不改签名参数(工具族函数签名里的关键字参数照旧)
3. 验证门一过立即 commit
4. 测试一律 ``.venv\Scripts\python.exe``

## 1. 手术清单

### 1.1 violation 族 → execution/tool_execution.py 模块函数

从 runner.py 搬出(现 1485-1569 行附近):

| runner 成员 | 新家形态 |
|---|---|
| ``_SSRF_BOUNDARY_NOTE`` 类常量(1485) | 模块级常量 ``_SSRF_BOUNDARY_NOTE``(tool_execution.py 顶部常量区) |
| ``_is_ssrf_violation`` classmethod(1505) | 公开模块函数 ``is_ssrf_violation``(测试直测,公开名) |
| ``_is_workspace_violation`` classmethod(1512) | 公开模块函数 ``is_workspace_violation``(对称公开) |
| ``classify_violation`` 实例方法(1521) | 公开模块函数 ``classify_violation``(去 ``self``,体内 ``self._x`` → 模块函数直呼) |
| ``_ssrf_soft_payload`` classmethod(1562) | 私有模块函数 ``_ssrf_soft_payload``(去 cls) |
| ``_event_detail`` staticmethod(1567) | 私有模块函数 ``_event_detail`` |

依赖(tool_execution.py 均已有导入,只补一个名字):
- ``repeated_workspace_violation_error`` ← utils.runtime 导入块追加(tool_execution.py:41 附近)
- ``ToolCallRequest`` ← providers.base(已有)
- ``logger`` / ``Any``(已有)

改 3 处调用点(292/356/384 附近):``self._runner.classify_violation(...)``
→ ``classify_violation(...)`` 模块直呼。

更新 tool_execution.py 头部 docstring(第 9 行与 54 行附近)提到
``_classify_violation`` remains on the host 的过时表述——改为说明分类逻辑已归位本模块。

### 1.2 planning 族 → execution/planning.py 模块函数

从 runner.py 搬出(现 288-327 行附近):

| runner 成员 | 新家形态 |
|---|---|
| ``extract_task_from_messages`` staticmethod(288) | 公开模块函数(planning.py) |
| ``inject_step_guidance`` staticmethod(302) | 公开模块函数(planning.py) |

两者均完全自包含(不依赖 ``_merge_message_content``——该函数属排水族,
被 ``_append_injected_messages``(354 行)使用,**留在 runner.py 不动**)。

改 2 处调用点(planning.py 98/162):
``self._runner.extract_task_from_messages(...)`` → 模块直呼;
``self._runner.inject_step_guidance(...)`` → 模块直呼。

### 1.3 测试改指(1 文件)

tests/agent/test_runner_safety.py:5 处 ``AgentRunner._is_ssrf_violation(...)``
(77/79/87/92/94 行附近)→ ``from miniunicorn.agent.execution.tool_execution import
is_ssrf_violation`` 后直呼 ``is_ssrf_violation(...)``。

### 1.4 文档(module-boundaries.md)

runner/execution 服务描述若提及 classify_violation 归属,同步(如无则跳过,如实记录)。

## 2. 验证门

```powershell
# 门 1:runner 零残留
rg -n -e "classify_violation" -e "ssrf_violation" -e "SSRF_BOUNDARY_NOTE" -e "extract_task_from_messages" -e "inject_step_guidance" miniunicorn/agent/runner.py
# 期望:零命中

# 门 2:定向测试
.venv\Scripts\python.exe -m pytest tests/agent/test_runner_safety.py tests/agent/test_tool_execution.py tests/agent/test_planning_policy.py tests/agent/test_planner_results.py -q

# 门 3:全量(后台 Start-Process + 轮询变体)
.venv\Scripts\python.exe -m pytest tests/ -q
# 期望 4147 passed / 0 failed / 29 skipped(无测试增删)

# 门 4:双 ruff 零
.venv\Scripts\python.exe -m ruff check miniunicorn/ tests/
.venv\Scripts\python.exe -m ruff format --check miniunicorn/ tests/

# 门 5:冷导入
.venv\Scripts\python.exe -c "import miniunicorn.agent.runner, miniunicorn.agent.execution.tool_execution, miniunicorn.agent.execution.planning; print('ok')"
```

## 3. 提交

```
refactor(agent): home violation classification to tool_execution

The five-member violation-classification family (classify_violation and
helpers, 67 loc) had zero state coupling and its only production callers
were three sites inside ToolExecutionService reaching back into the
orchestrator - a textbook inversion. Now module functions in
tool_execution.py. The two planning-only statics
(extract_task_from_messages, inject_step_guidance) similarly home to
planning.py. Bodies moved verbatim; behavior unchanged.
```
