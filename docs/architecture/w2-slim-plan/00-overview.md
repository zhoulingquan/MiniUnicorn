# W2 精简系列总览：核心减法与攻击面收口

> 状态：方案定稿，待分批实施（Cline）。
> 基线：W0/W1 全部 9 批已合并，`pytest tests/ -q` 4077 passed / 0 failed，ruff 零告警。
> 建议实施前打 tag：`git tag -a baseline-pre-w2 -m "W1 收官基线，W2 精简系列起点"`。

## 一、背景

W0（可信证据管道）与 W1（工具库边界：惰性加载、生命周期归组合根、activate_plan）已完成。W1 收官时的四个备忘候选项，本方案逐一对照**当前代码实况**重新裁决——不是照单全收：两个按原样做（其中标的修正）、两个以更轻的形式做或否决。

## 二、候选项裁决表（本方案的核心决策）

| 备忘候选项 | 裁决 | 理由（基于 2026-08-31 代码核实） |
|---|---|---|
| RuntimeState 白名单化 | **做（W2-1，安全优先）** | 真实标的不是 `tools/mcp.py` 的连接协议（W1-2 已用 McpRuntime 原生解决），而是 **`tools/runtime_state.py` + `tools/self.py` 的 MyTool**：deny-list 模式 + 任意 getattr/setattr 点路径访问宿主对象。已发现**多个活漏洞**：W1-2 新增的 `_mcp_runtime` 不在 BLOCKED 清单，`my set _mcp_runtime <x>` 可整体替换 MCP runtime owner；`cron_service`、`workspace_scopes`、`model_preset`、`_provider_snapshot_loader` 同样可被 `my set` 替换。deny-list 天然随代码演化腐烂，必须翻转为 allow-list |
| TurnController 从 runner.py 抽取 | **改造形式后做（W2-2）** | runner.py 1657 行、`_run_with_ledger` 505 行（485-990）确是维护痛点。但**否决"新类"形式**：抽取 TurnController 需要 10+ 字段构造或 frame 对象，纯抽象税。改为**主循环分段方法化**——遵循代码库既有惯例（"自 run 拆出"的 helper + action 字符串返回值，`_retry_empty_response` / `_handle_fatal_tool_error` 皆此模式），把 escalation 块、工具分支、收尾分支拆为 AgentRunner 上的 phase 方法，零新抽象 |
| ManagedPolicy 抽取 | **否决（残余价值并入 W2-2）** | MANAGED 逻辑已合理分布：`planning_policy.py`（PlanningPolicy.escalate 决策）、`progress_policy.py`（ProgressTracker 熔断）、`execution/planning.py`（PlanningReflectionService 推进）。再抽一层 ManagedPolicy 是为分布本身付费。有价值的残留只有两处：escalation 块 60 行内联在主循环（并入 W2-2 搬家）+ PlanSnapshot origin 重建用 12 字段逐个复制（runner.py:545-557，改为 `with_origin()` 方法，并入 W2-2） |
| ToolContext Any 字段增量收窄 | **否决类型收窄，改为死字段删除（W2-3）** | 项目**无 mypy/pyright**（pyproject.toml 只有 ruff）——纯类型注解收窄无任何静态强制力，是死重。核实发现两个真正的死字段：`timezone`（构造点硬编码 "UTC"，全库零读取）、`workspace_sandbox`（两处构造传入，全库零读取）。删字段+删构造参数是实打实的减法；类型收窄不做（若未来引入类型检查器再议） |

## 三、批次索引

| 批次 | 文件 | 内容 | 规模 | 依赖 |
|---|---|---|---|---|
| W2-1 | 01-w2-1-mytool-whitelist.md | MyTool deny-list → allow-list，关闭 `my set` 替换宿主对象的活漏洞 | 中 | baseline-pre-w2 |
| W2-2 | 02-w2-2-loop-phase-split.md | 主循环分段方法化 + escalation 搬家 + PlanSnapshot.with_origin | **大** | baseline-pre-w2 |
| W2-3 | 03-w2-3-toolcontext-dead-fields.md | ToolContext 死字段删除（timezone、workspace_sandbox） | 小 | baseline-pre-w2 |

三批**互不重叠**（W2-1 只碰 self.py 及其测试；W2-2 只碰 runner.py / planning.py / plan_snapshot.py；W2-3 只碰 context.py / _mcp_lifecycle.py / subagent.py），无顺序依赖。建议顺序 W2-1 → W2-3 → W2-2（安全优先、小批热身、大批殿后）。

## 四、全局约束（继承并扩充）

### 4.1 受保护清单（W0/W1 成果，语义不得触碰）

1. 可信证据管道：ToolObservation / receipts / effective_evidence_level 判定 / observations_digest 缓存键 / verifier 熔断
2. 三态审批门（allow/deny/approval_callback）与 policy 传播
3. tool_blocked 审计 checkpoint 与 runtime_checkpoint 恢复链路（含 plan_snapshot 审计隔离）
4. LazyToolRegistry 的装载语义（register 不触发装载）
5. McpRuntime / SubagentManager 的组合根所有权（W1-2 的注入-回退双路径）
6. activate_plan 的激活器语义（挂载即返回，主循环唯一推进者）
7. verifier 异常 fail-closed 回退

### 4.2 W2 特有红线

1. **MyTool 现有合法功能不缩水**：check 的合法点路径（RESTRICTED 键、exec_config 子路径、_last_usage 等）与 set 的合法路径（RESTRICTED 三键 + scratchpad 存储）行为不变；变的只是"未列出的路径一律拒绝"
2. **主循环分段是纯搬家**：语义逐句对应（代码库既有惯例，见 runner.py 992 行注释"语义与原内联实现逐句对应"），不趁机修 bug、不改行为；全量测试必须零变化通过（本批不允许更新任何既有测试的断言，除非是纯导入路径调整）
3. **不引入新抽象**：无新类（TurnController/ManagedPolicy/LoopFrame 均否决）、无新配置项、无新事件
4. 定位用符号，行号是编写时参考值
5. 单批单 commit，禁止 `git add .`

## 五、验证门（每批统一）

```
.venv\Scripts\python.exe -m pytest tests/ -q        # 0 failed，passed ≥ 4077（新测试只增不减）
.venv\Scripts\python.exe -m ruff check miniunicorn/  # 零输出
.venv\Scripts\python.exe -m ruff format --check miniunicorn/
```

注意：必须用项目 venv 的 Python 3.12（`.venv\Scripts\python.exe`），系统默认 Python 3.10 缺 `typing.Self` 会导致收集错误。

## 六、实施方式

交由 Cline 分批实施，提示词模板见 `cline-prompt.md`（与 trusted-evidence-plan 系列的 WorkBuddy 模板同构）。每批完成以该批任务书"验收自检"清单为准。
