# W8-3b 任务书:死壳清除 + usage 直连 + 13 个承重别名退役

> 性质:删除死委托壳 + 调用点改写(含 runner 方法体内调用点),行为零变化
> 规模:~6 文件 · 前置:W8-3a(4658d45e)已完成 23 个零引用别名删除
> 调用点数据已程序化核验(2026-09-03 快照,行号以内容匹配为准)

## 0. 红线

1. 只删方法壳与改写调用点;服务本体(ModelRequestExecutor /
   ToolExecutionService / ContextGovernanceService)一字不动
2. 改写仅限 ``self._x(`` → ``self.x(`` 与等价重指向,不改参数、不改语义
3. 验证门一过立即 commit
4. 测试一律 ``.venv\Scripts\python.exe``

## 1. 手术清单

### 1.1 删除 5 个死委托壳(runner.py)

生产调用点已全部直连服务或经服务自身,壳上仅剩测试:

| 方法 | 行(快照) | 壳上测试调用点(需先改指向) |
|---|---|---|
| run_tool | 1523 附近 | 无(全仓零调用方) |
| normalize_tool_result | 1697 附近 | 无 |
| apply_tool_result_budget | 1708 附近 | 无 |
| snip_history | 1718 附近 | test_runner_governance.py ×4(115/175/716/764) |
| partition_tool_batches | 1728 附近 | 无 |

**execute_tools 保留**(runner.py:753 编排核心内部在调,活壳)。

### 1.2 usage 三件套直连(runner.py + recovery.py)

删除 runner.py 的 usage_dict/accumulate_usage/merge_usage(1439-1454,纯转发
ModelRequestExecutor 静态方法):

- runner.py 内部 2 处(557/561 附近)改 ``ModelRequestExecutor.usage_dict(...)``
  / ``ModelRequestExecutor.accumulate_usage(...)``
- execution/recovery.py 3 处(227/228/245 附近)``self._runner.usage_dict`` 等
  改为直接调用 ModelRequestExecutor(recovery.py 若无该导入则补一行,
  与既有 model_request 相关 import 合并排序)

### 1.3 13 个承重别名退役(runner.py + 2 测试文件)

改写 runner.py 内部 20 处 ``self._x(`` → ``self.x(``(方法名照抄,仅去前缀):

```
_init_planner(2处:484/682) _init_reflection(490) _govern_messages(518)
_apply_plan_step_guidance(523) _handle_fatal_tool_error(783)
_end_turn_with_drain(952/968) _fire_terminal_reflection(622)
_retry_empty_response(877) _handle_length_recovery(896)
_complete_plan_step(998) _emit_plan_snapshot(6处:486/685/800/807/1041/1370)
_finalize_max_iterations(619) _append_final_message(963)
```

然后删除 13 行 ``_name = name  # compat alias`` 类级绑定(1072/1084/1099/
1118/1165/1186/1218/1253/1282/1315/1334/1389/1647 附近,内容匹配)。
**__init__ 内 5 行实例级别名保留不动。**

测试侧:

- tests/agent/test_runner_phase_split.py:4 处 monkeypatch.setattr 的目标字符串
  ``"_init_planner"`` → ``"init_planner"``,``
  ``"_emit_plan_snapshot"`` → ``"emit_plan_snapshot"``(101/102/136/161);
  fake 函数本体不动(实例属性 patch 机制不变)
- tests/agent/test_runner_governance.py:4 处 ``runner.snip_history(`` →
  ``runner.context_governance.snip_history(``(115/175/716/764)

### 1.4 文档(module-boundaries.md)

runner 条目若有方法清单/别名说明,同步(如无则跳过,如实记录)。

## 2. 验证门

```powershell
# 门 1:壳与别名零残留
rg -n -e "compat alias" erza/agent/runner.py
# 期望:仅 5 行实例级(self._x = self.x)
rg -n -e "def run_tool" -e "def normalize_tool_result" -e "def partition_tool_batches" erza/agent/runner.py
# 期望:零命中(方法壳已删)

# 门 2:定向测试
.venv\Scripts\python.exe -m pytest tests/agent/test_runner_governance.py tests/agent/test_runner_phase_split.py tests/agent/test_runner_injections.py tests/agent/test_runner_tool_execution.py tests/agent/test_runner_receipts.py tests/agent/test_planner_results.py tests/agent/test_progress_policy.py tests/agent/test_replan_semantics.py -q

# 门 3:全量(后台 Start-Process + 轮询变体)
.venv\Scripts\python.exe -m pytest tests/ -q
# 期望 4147 passed / 0 failed / 29 skipped(无测试增删,数字不变)

# 门 4:双 ruff 零
.venv\Scripts\python.exe -m ruff check erza/ tests/
.venv\Scripts\python.exe -m ruff format --check erza/ tests/
```

## 3. 提交

```
refactor(agent): retire runner delegation shells and load-bearing aliases

Drop five zero-caller delegation shells (run_tool, normalize_tool_result,
apply_tool_result_budget, snip_history, partition_tool_batches) and the
usage trio forwards (direct ModelRequestExecutor). Rewrite the 20 internal
underscore call sites to public names, retiring the 13 load-bearing compat
aliases; phase_split monkeypatch targets and governance test call sites
re-pointed. Behavior unchanged.
```
