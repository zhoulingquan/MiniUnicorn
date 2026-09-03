# W8-3 评估报告:runner.py 拆分裁决

> 日期:2026-09-03 · 数据:方法级地图 + 状态耦合分析 + 精确调用点扫描 + 内部调用扫描(全部程序化)

## 结论:不做大拆分,做残留清理

runner.py(1735 loc)的"大"由三种成分构成,只有第三种是债:

1. **编排核心 ~600 loc,健康,保留**:run / _run_with_ledger(174)/
   _maybe_escalate_to_managed(64)/ _execute_tool_iteration(135)/
   _finalize_nontool_iteration(214)+ spec/result/state 数据类(~130)。
   这是真正的转轮编排,W2 裁决(否决 TurnController 抽象税、主循环方法化)在此适用。

2. **共享词汇 ~330 loc,保留**:注入排水族(184,被 recovery/planning/核心三方调用)、
   handle_budget_exceeded(71,内部 ×2 + recovery ×1)、append_final_message(17 static,
   内部 ×2 + recovery ×3)、build_tool_result_messages(29,内部 ×1 + tool_execution)。
   这些是 runner 与 execution 模块之间的真实共享词汇,搬家只会制造新的间接层。

3. **残留 ~150 loc,是债,分三批清**。

## 关键发现(勘察超出 W8 立项预期的部分)

### 发现一:41 个 compat 别名

runner.py 内有 41 行 `_name = name  # compat alias` 类级绑定(36 类级 + 5 实例级)。
**W8-3a 执行时修正**:评估的首轮扫描(仅匹配公开名)漏掉了编排核心内 20 处 `self._x(` 下划线调用,
13 个类级别名实为承重墙,归 W8-3b 随内部调用点改公开名后删除;实际零引用 23 个已在 W8-3a 删除。
测试侧:7 文件纯调用(已改公开名)+ test_runner_phase_split.py 4 处实例级 monkeypatch(需随 W8-3b 改 patch 目标名)。

### 发现二:四个"死"委托壳(生产已绕行)

normalize_tool_result(11)/ apply_tool_result_budget(10)/ snip_history(10)/
partition_tool_batches(8)这四个 runner 方法是 PR-5b 等历史迁移留下的转发壳,
真逻辑在 ToolExecutionService / ContextGovernanceService。
生产调用点已全部直连服务(如 runner_strategies.py:212 经
`runner.context_governance.apply_tool_result_budget` 直达);
context_governance.py:190 经 `runner.tool_execution.normalize_tool_result` 直达。
runner 级壳仅剩测试调用者(如 test_runner_governance.py 的 `_snip_history`)。

### 发现三:usage 三件套是纯转发

usage_dict/accumulate_usage/merge_usage(17 loc)转发 ModelRequestExecutor 静态方法。
调用方:runner 内部 ×2(_run_with_ledger 557/561)+ recovery.py ×3。
直连 ModelRequestExecutor 可消一层间接,同时删 3 个壳 + 3 个别名。

### 发现四:violation 分类族放错了家(真逻辑错位)

_is_ssrf_violation/_is_workspace_violation/classify_violation/_ssrf_soft_payload/
_event_detail(67 loc)是无状态分类族,状态耦合分析:零 self 属性访问,仅缝内互调。
唯一生产消费方是 **tool_execution.py ×3**(`self._runner.classify_violation(...)`),
runner 内部零调用——执行层服务反向伸进编排器取自己的分类逻辑,典型倒置。
归位 tool_execution.py 模块级函数即可(不需要类,不需要新文件)。

### 发现五:两个 planning 专属 static

extract_task_from_messages(16)/ inject_step_guidance(28)static 方法,
唯一消费方是 planning.py(98/162),内部零调用。与发现四同性质,可顺带归位。

## 批次计划

| 批次 | 内容 | 量级 | 性质 |
|---|---|---|---|
| W8-3a | 41 个 compat 别名删除 + 7 个测试文件改名(~15 处调用点) | −41 loc | 纯删除,机械 |
| W8-3b | 4 个死壳 + usage 三件套直连(内部调用点与 recovery.py 改直连 ModelRequestExecutor,测试改指 context_governance) | −60 loc | 纯删除 + 改写调用 |
| W8-3c | classify_violation 族 67 loc → tool_execution.py 模块函数;extract_task_from_messages/inject_step_guidance 44 loc → planning.py | −111 loc(净,逻辑搬家) | 真逻辑归位,消倒置 |

预期终态:runner.py ~1500 loc,零别名、零死壳、零倒置;
编排核心与共享词汇原样不动。

## 明确否决项(留档防翻烧饼)

- **拆 runner/ 包**(runner.py + drain.py + violations.py …):否决。
  排水族被三方共享,拆出只加间接层;W2 已裁决同类抽象税。
- **drainage / handle_budget_exceeded / append_final_message 搬家**:否决。
  共享词汇,非错位。
- **handle_budget_exceeded(71 loc)抽策略类**:否决。内部 ×2 + recovery ×1,
  单一实现无多态需求,抽类 = 抽象税。

## 风险与边界

- 别名删除涉及测试改名,无逻辑改动;7 文件 15 处,风险低
- W8-3b 中 recovery.py 需新增 ModelRequestExecutor 导入(或经已有导入直连),
  注意与现有 model_request 导入合并
- W8-3c 搬家后 test_runner_safety.py 的 `_is_ssrf_violation` 需改指新家
- 三批可独立验证独立提交;顺序 a → b → c(从纯机械到真搬家)

