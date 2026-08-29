# MiniUnicorn 复核问题修复报告

**修复日期：** 2026-08-26
**基线：** `035770e3`（与[评估报告](./agent-harness-evaluation-report-2026-08-25.md)和[复核报告](./agent-harness-evaluation-review-2026-08-25.md)一致）
**执行方式：** Opencode 分 5 批实施（每批独立任务说明 + 人工验证），TRAE 复核全程监督
**最终状态：** 全量测试 **3961 passed / 0 failed**（6:47 完整跑通），ruff **0 错误 / 294 文件全部格式化**

---

## 修复总览

| # | 问题（复核报告编号） | 严重级 | 修复批次 | 状态 |
|---|---|---|---|---|
| 1 | F-002 step acceptance 误接受 | P0 | 批 1 | ✅ 已修复 |
| 2 | F-001 高风险工具无执行前审批 | P0 | 批 2 | ✅ 已修复 |
| 3 | 子代理预算逃逸 | P0 | 批 3 | ✅ 已修复 |
| 4 | requires_checkpoint 死字段 | P0 | 批 2 | ✅ 已修复 |
| 5 | 全量测试挂死（mcp_probe） | P1 | 批 4 | ✅ 已修复 |
| 6 | pytest-timeout 缺失/配置无效 | P1 | 批 4 | ✅ 已修复 |
| 7 | deep_research 测试真实网络抓取 | P1 | 批 4 | ✅ 已修复 |
| 8 | mcp_presets_api 测试假失败 | P1 | 批 4 | ✅ 已修复 |
| 9 | 文档路径漂移（含失真的"别名兼容"） | P2 | 批 5 | ✅ 已修复 |
| 10 | ruff 1 错误 + 32 文件未格式化 | P2 | 批 5 | ✅ 已修复 |

## 各项修复详情

### 批 1 — F-002：StepAcceptancePolicy 误接受（P0）

**改动：** `miniunicorn/agent/step_acceptance.py` 的 `_is_accepted()` 删除 `if tool_calls: return True` 分支——done_criteria 存在时，接受当且仅当 criteria 匹配最终文本，与是否执行过工具无关。

**配套：** 更新 `tests/agent/test_step_acceptance.py`（旧行为用例反转 + 新增 6b/6c/6d 回归测试）、`tests/agent/test_step_acceptance_verifier.py`。修复后"规则接受但 criteria 未匹配"不可能发生，verifier 的"规则拒绝→LLM 兜底"路径自动覆盖过严误拒场景。

**验证：** 定向 41 passed（含复核时编写的最小失败测试，修复前红、修复后绿）。

### 批 2 — F-001：高风险工具执行前审批门（P0）

**改动：**
- `runner.py`：AgentRunSpec 新增 `high_risk_policy`（默认 `"allow"`）与 `approval_callback`（异步回调 → bool）
- `tool_execution.py`：`run_tool()` 在 `_run_tool_impl()` **之前**插入三态门：
  - `policy="deny"` → HIGH 工具一律拒绝（静态 fail-closed 开关）；
  - `approval_callback` 存在 → 必须获批才执行（回调异常按拒绝处理，fail-closed）；
  - 默认 `"allow"` → 保持兼容，但新增**执行前** `tool_started` 审计 checkpoint——消费了 `requires_checkpoint` 死字段，审计时点从"执行后"提前到"执行前"。
- `config/schema.py`：`ToolsConfig.high_risk_policy: Literal["allow","deny"] = "allow"`
- `loop.py` / `loop_builder.py`：完整接线（config → AgentLoop → AgentRunSpec）

**拒绝路径：** 返回模型可见的明确错误（含"hard policy boundary, do not retry"提示），与既有 `repeated_external_lookup_error` 模式一致。

**配套：** 新建 `tests/agent/test_tool_approval.py`（6 用例：默认放行+checkpoint 顺序断言、回调拒绝、回调放行、deny 优先于回调、LOW 工具不触发门、回调异常按拒绝）。

**验证：** 定向 23 passed；tests/agent 全量 1865 passed（除已知 deep_research）。

### 批 3 — 子代理预算逃逸（P0）

**改动：**
- `subagent.py`：`SubagentManager` 新增 `turn_budget_factory` 参数；`_run_subagent_task` 与 `_run_subagent_direct` 两处 AgentRunSpec 均传入工厂产物——每个子代理获得**独立新鲜 TurnBudget**（不共享，避免并发竞态）
- `loop.py`：构造 SubagentManager 时注入 `turn_budget_factory=self._build_turn_budget`（复用主循环含 tiered 解析的预算工厂）

**效果：** spawn/delegate 派生的 LLM 调用现在受 token/cost 预算约束，主 turn 派生 N 个子代理不再能绕过预算消耗 N 倍资源。

**配套：** `tests/agent/test_subagent_lifecycle.py` 新增 TestTurnBudgetFactory（4 用例：两路径注入、向后兼容、并发独立实例）。

**验证：** 定向 65 passed。

### 批 4 — 测试基建四项（P1）

| 项 | 根因（复核已定位） | 修复 |
|---|---|---|
| mcp_probe 挂死 | Python 3.12 `Server.wait_closed()` 语义变更（gh-104344），handler `lambda r,w: None` 不关 writer → 清理永久等待 | handler 改为关闭连接；`import asyncio` 移至文件顶部 |
| pytest-timeout 缺失 | pyproject 配置 `timeout=600` 但依赖组未声明该包，配置完全无效 | 依赖组声明 `pytest-timeout>=2.3.0,<3.0.0` 并安装（2.4.0） |
| deep_research 超时失败 | 3 个测试的自定义 config 未设 `enable_fetch=False`（默认 True），对 example.com 发起真实 HTTP 抓取 | 3 处 config 补 `enable_fetch=False` |
| mcp_presets_api 失败 | **非网络依赖**（复核报告判断有误，Opencode 调查修正）：FakeTool 缺 `aliases` 属性，`ToolRegistry.register()` 遍历 `tool.aliases` 抛 AttributeError 被 API 吞掉 → `ok: False` | FakeTool 补 `aliases: tuple[str, ...] = ()` |

**验证：** mcp_probe 6 passed 6.78s（60s 内、无挂起）；deep_research + mcp_presets 45 passed；tests/tools 全量 494 passed 21 skipped。

### 批 5 — 文档修正 + ruff 清理（P2）

**文档：** `docs/architecture/module-boundaries.md` 三处路径漂移修正（`safety.py`→`safety_policy.py`、`planning.py`→`planning_policy.py`、`delegation.py`→`tools/execute_plan.py`）；§2.17 按实际实现（`ExecutePlanTool`、并行委派语义）整节重写，删除失真的"别名兼容"说明。

**ruff：** schema.py:525 N805 手工修复（metaclass `__call__` 的 `cls`→`self`，未用全局 `--fix`）；`ruff format` 格式化 32 文件（含 .md 内嵌代码块的引号规范化）；执行 noqa 防回归检查（无任何带 noqa 的行被删除，compat re-export 完整保留）。

**验证：** `ruff check` 零错误；`ruff format --check` 294 文件全部通过；tests/config+composition 75 passed；核心新测试 80 passed。

---

## 最终全量回归（修复前 → 修复后）

| 指标 | 修复前（复核报告记录） | 修复后 |
|---|---|---|
| 全量测试 | 无限挂死，强杀后 969 passed / 13 failed / 878 errors | **3961 passed / 0 failed / 29 skipped / 1 xfailed**，6:47 完整跑通 |
| 挂起源 | test_mcp_probe.py（Python 3.12 wait_closed 语义变更） | 已修复，6.78s 通过 |
| pytest-timeout | 未安装，timeout 配置无效（unknown config warning） | 已安装并声明，配置生效，警告消失 |
| ruff check | 1 error | **All checks passed** |
| ruff format | 32 文件未格式化 | **294 文件全部格式化** |
| F-001/F-002/预算逃逸 | 确认成立 | 均已修复并有回归测试锁定 |

## 改动统计

39 个文件：+584 / −307 行（其中约 30 个文件为 ruff format 空白/引号重排；实质改动集中在 13 个文件）。未提交（保留在工作区待用户审阅）。

## 遗留事项（未在本轮范围内）

1. `approval_callback` 目前是 SDK/嵌入层能力字段，loop 层未构造默认审批回调（无审批 UI）——接入 WebUI/频道审批流是后续工作；
2. 文档 §2.15/§2.16 的 API 方法描述与实际实现仍有漂移（如 `assess_risk` vs 实际 `evaluate`），本轮只修了路径与 §2.17，完整文档审计未做；
3. shell 沙箱 `sandbox_required` 默认 False（fail-open）与 API 空 key 本地全开放——复核报告判定为"有意的向后兼容取舍"，未改动默认值，如需 fail-closed 请显式配置。
