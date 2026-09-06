# Erza 可信证据与工具库化实施计划（总览）

> 本系列文件是多轮红队评审（2026-08-25 ~ 08-29）收敛后的最终实施方案。
> 每份批次文件自包含，可直接作为 Opencode 的任务书，无需对话上下文。

## 一、背景（一段话）

项目定位：小型企业长期使用的业务智能体，单用户。核心问题链：步骤验收依赖模型自述文本（step_acceptance.py 的 done_criteria 子串匹配），模型复述关键词即可伪造完成；工具调用结果从未接入验收（管道断路）；plan_snapshot 会覆盖崩溃恢复检查点。经七轮对抗评审收敛为：**先建可信证据管道（W0），再做工具库化（W1）**，不引入 Port/Adapter 全家桶，不做大规模重写。

## 二、终点架构（三层）

```
Agent Core（治理内核）
  唯一主循环 / TurnState / 上下文组装 / 预算 / 安全审批门 /
  工具网关 / 步骤验收（含证据管道）/ ProgressTracker / 会话检查点
      ↑ 只经收窄的接口
Tool Library（能力四肢）
  filesystem / shell / web / MCP 工具 / cron 工具 / subagent 工具 /
  planning 工具（activate_plan）……
      ↑ 由外层装配注入
Composition/Runtime（后勤组装层）
  创建与销毁：CronService / MCP 连接 / SubagentManager
```

边界铁律：
- 步骤状态唯一写者是系统侧验收器；模型没有任何"标记完成"的工具
- 工具不能在当前调用栈内启动第二个主循环
- 长命服务的生杀权不在 AgentLoop
- 完成判定 = 结构化回执（工具代码在副作用真实发生后生成），不是文本、不是 status=ok、不是关键词命中

## 三、批次索引

| 批次 | 文件 | 内容 | 规模 | 依赖 |
|---|---|---|---|---|
| 阶段 0 | 01-phase0-baseline.md | 提交 52 个受保护文件，打基线 | 无代码改动 | — |
| W0-A1 | 02-w0a1-evidence-pipeline.md | ToolObservation + 跨迭代累积 + 接线（无行为变更） | 中 | 阶段 0 |
| W0-A2 | 03-w0a2-receipt-protocol.md | 结构化回执协议 + 三个契约工具改造（无验收行为变更） | 中 | W0-A1 |
| W0-A3 | 04-w0a3-acceptance-semantics.md | 验收语义切换（本计划的核心行为变更批） | **大** | W0-A2 |
| W0-A4 | 05-w0a4-planner-protocol.md | Planner 模板与解析：evidence_level 字段 | 小 | W0-A3 |
| W0-B | 06-w0b-checkpoint-isolation.md | plan_snapshot 审计隔离 + digest 持久化 | 小 | W0-A1 |
| W1-1 | 07-w1-lazy-loading.md | 工具惰性加载 | 中 | W0 全部 |
| W1-2 | 08-w1-lifecycle-ownership.md | Cron/MCP/Subagent 生命周期归组合根（3 个 commit） | 大 | W1-1 |
| W1-3 | 09-w1-activate-plan.md | activate_plan 激活器工具 | 大 | W0-A3/A4、W1-2 |

依赖关系：W0-A1 → A2 → A3 → A4 严格串行；W0-B 可在 A1 后任意插入；W1 三批在 W0 稳定后串行。W1-1 与 W0-B 无依赖冲突。

## 四、全局约束（每个批次必须遵守）

### 4.1 受保护行为（2026-08-26 修复批次，任何批次不得触碰其语义）

- 高风险工具三态审批门（allow/deny/approval_callback）
- tool_blocked 审计 checkpoint
- runtime_checkpoint 恢复链路（awaiting_tools 物化）
- 子代理预算传播与 high_risk_policy 传播
- verifier 异常 fail-closed 回退（step_acceptance.py 现行为）

对应测试（**修改任何一条须在批次报告中单独说明理由**）：
tests/agent/ 下审批门、tool_blocked、checkpoint 恢复、预算传播、子代理策略传播相关用例。

### 4.2 锚点约定

本系列文件中的行号是**编写时参考值**（HEAD 0e6cd046 + 受保护工作区改动），实施时以**符号名**（类名/函数名）定位为准。若符号找不到，停下来报告，不要猜测。

### 4.3 每批次固定流程

1. 读批次文件全文 → 定位锚点符号 → 确认与文件描述的现状一致
2. 现状不一致时：停止并报告差异，等待澄清
3. 实施（禁改范围绝对不碰）
4. 新增测试逐条实现并跑绿
5. `pytest tests/ -x -q` 全量 ≥ 3970 passed / 0 failed
6. `ruff check erza/ && ruff format --check erza/` 零告警
7. 单批单 commit，禁止 `git add .`，禁止混入 .tmp-* 与临时报告
8. 输出批次报告：改动文件清单、测试结果、与本规格的偏差说明

### 4.4 坚决不做（全系列有效）

- 不引入 ModelPort/MemoryPort/SessionPort/AgentPort/SchedulerPort/McpPort/EventPort
- 不把 Planning 拆成 create_plan/complete_plan_step 等五个模型自主工具
- 不用 token/关键词/status=ok/模型文本作为完成证明
- 不做 weak/provisional accepted
- 不实现跨轮计划恢复（第一阶段崩溃后重新激活）
- 不在工具调用栈内嵌套主循环
- 不删除 FAST/MANAGED 档位
- 不一次性搬迁全部工具文件；不一次性重写 ToolContext 全部 Any 字段
- 不为"架构完整"新建 PlanStateService/ToolCatalog/ToolGateway 等服务

## 五、关键设计裁决速查（评审定稿）

| 议题 | 裁决 |
|---|---|
| 证据等级 | text / tool 两级（none 折叠进 text：规则等价）；effective = max(Planner 声明, static_floor) |
| 可信回执 | ToolReceiptClaim，由工具代码在副作用真实完成后创建；仅 write_file/edit_file/apply_patch；dry_run 不产生 |
| 不可作为回执 | 模型文本 / criteria 关键词 / shell 输出 / web_search 内容 / status=ok / 200 字符 result_summary |
| verifier 权限 | 只能复核 done_criteria_not_met（唯一 rescue 通道）；硬失败永不覆盖；故障 fail-closed 不写缓存 |
| 缓存键 | (step_id, evidence_digest)；evidence_digest = 观察序列的 SHA-256 规范化哈希 |
| verifier 连续故障 | ProgressTracker.verifier_failures ≥ 2 → REPLAN（reason=verifier_unavailable） |
| plan_snapshot | 仅审计：加入 _AUDIT_ONLY_CHECKPOINT_PHASES，不占恢复槽位 |
| activate_plan | 激活器语义：挂载计划立即返回，外层主循环推进；delegate_plan 与 execute_plan 别名原样保留 |
| execute_plan 现状 | 是 delegate_plan 的兼容别名（工具栈内 spawn_and_wait 跑子代理），保持不变 |

## 六、分批实施指引（面向 Opencode 逐批委托）

1. **每次只给一份批次文件**。提示词模板：
   > 请完整阅读 `docs/architecture/trusted-evidence-plan/NN-xxx.md`，严格按其中"现状锚点 → 改动 → 测试要求 → 禁改清单"执行。实施前先核对锚点符号与代码现状是否一致，不一致立即停下报告，不要猜测。完成后输出批次报告。
2. **先读 00-overview.md 第 4 节全局约束**再读批次文件（可合并进同一提示词）。
3. **批次完成标准以各文件"验收自检"为准**，未达标的批次不进入下一批。
4. **中断续接**：Opencode 在批次中途静默挂断时（已知风险），先 `git status` + `git diff` 判断半成品范围，未完成的改动 `git checkout -- <files>` 回退后重新起批，不要在半成品上续写。
5. **规模预估**（按代码接触面，供 token 预算参考）：小批次（05、06）通常一次可完成；中批次（02、03、07）一次或两次；大批次（04、08、09）建议拆成"代码改动"与"测试补齐"两次委托，两次之间跑一次全量测试确认中间态可用。

## 七、W2 展望（已另立方案）

W1 稳定后的候选已裁决并另立为 `docs/architecture/w2-slim-plan/`（三批：MyTool 白名单化、主循环分段方法化、ToolContext 死字段删除；TurnController 类与 ManagedPolicy 抽取经评估否决）。本系列到此收官。
