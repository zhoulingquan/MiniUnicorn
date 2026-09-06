# Erza Agent Harness 评估报告

**报告日期：** 2026-08-25  
**评估基线：** `HEAD 035770e3`  
**评估对象：** `D:\MyProject\Erza`  
**用途：** 交由另一 Agent 进行独立复核、补证据和挑战结论

## 1. 执行摘要

Erza 已经不是只有一个 ReAct 循环的实验项目，而是一个包含 Agent 核心、Provider、会话持久化、消息总线、工具生态、WebUI/API、频道适配和 Docker 部署的模块化单体 Agent Harness。

总体判断：

> **核心 Agent 能力较完整，单用户/可信内部环境可用；平台治理、强安全审批、多租户隔离、可回放评估和生产运维能力仍不完整。综合成熟度为 Alpha+，约 3.5/5。**

这不是行业统一的“全部 Agent 组成部分”清单。本报告采用三层评估模型：

1. Agent 认知—行动核心闭环：README 中列出的 12 项。
2. Harness 平台层：模型、运行时、身份安全、扩展、可观测性和评估。
3. 产品与生产层：入口、部署、运维、质量和文档治理。

## 2. 评估范围与方法

### 2.1 纳入范围

- 最近 P0–P4 提交涉及的 `CallLedger`、Managed 执行、上下文治理、`SafetyPolicy`、checkpoint、step acceptance、wall-clock timeout 和 prompt telemetry。
- `erza/agent/`、`providers/`、`session/`、`security/`、`pairing/`、`bus/`、`composition/`、`api/`、`channels/`、`webui/`。
- Docker、Compose、CI、架构文档、测试和静态检查配置。

### 2.2 判断标准

- **强：** 代码路径清晰，有测试覆盖，并且能力在正常路径中真正生效。
- **较强：** 主流程完整，但边界、隔离或生产运维仍有限。
- **部分：** 有实现或接口，但保证不完整，不能直接等价为生产能力。
- **缺失：** 未发现可承担该职责的完整实现。

“代码中存在文件”不等于“能力完整”。尤其需要区分风险分类与执行前审批、日志与分布式 Trace、单用户 API Key 与 RBAC、多进程部署与高可用。

## 3. 第一层：Agent 核心 12 项

12 项清单及项目自述见 [README.md:42](D:/MyProject/Erza/README.md:42)。

| # | 组成部分 | 主要实现 | 评价 | 复核重点 |
|---:|---|---|---|---|
| 1 | 编排循环 | `AgentLoop`、`AgentRunner`、`TurnOrchestrator` | 强 | 确认所有入口是否都经过统一 turn 状态机 |
| 2 | 工具系统 | 内置工具、MCP、CLI App、entry-point 插件 | 强 | 工具 schema、并发安全和权限是否逐工具生效 |
| 3 | 记忆系统 | 会话、历史摘要、结构化记忆、Dream、GitStore | 强 | 记忆作用域、候选事实和 active 事实边界 |
| 4 | 上下文管理 | `ContextGovernor`、压缩、裁剪、工具结果治理 | 强 | 压力触发、消息结构修复和插件策略顺序 |
| 5 | Prompt 构建 | 系统指令、项目指令、工具、Skills、记忆分层组装 | 较强 | 作用域隔离和不可信内容注入风险 |
| 6 | 输出解析 | Function Calling、JSON 修复、Provider 适配 | 较强 | refusal/error finish reason 下是否阻止工具执行 |
| 7 | 状态管理 | 原子 Session 持久化、checkpoint、goal、GitStore | 较强 | 崩溃恢复、并发写入和跨进程一致性 |
| 8 | 错误处理 | Provider retry/fallback、工具错误、Reflection | 较强 | fallback 后的成本、重试次数和幂等性 |
| 9 | 安全防护 | workspace、SSRF、Shell sandbox、DM pairing | 部分 | 是否存在严格的执行前审批和默认 fail-closed |
| 10 | 验证循环 | Planner、Replan、Reflection、Step Acceptance | 部分 | done criteria 未满足时是否仍接受步骤 |
| 11 | 子 Agent 编排 | `spawn`、`delegate`、`create_agent`、并发隔离 | 较强 | 子任务取消、预算、权限和结果合并 |
| 12 | 终止条件 | 迭代、token/cost budget、wall timeout、用户中断 | 较强 | 所有后台任务是否遵守同一预算和取消语义 |

### 3.1 核心代码证据

- 状态机和 turn 依赖束：[`turn_orchestrator.py:55`](D:/MyProject/Erza/erza/agent/turn_orchestrator.py:55)。
- LLM 调用计账和 per-turn budget：[`call_ledger.py:67`](D:/MyProject/Erza/erza/agent/call_ledger.py:67)。
- 上下文治理管线：[`context_governor.py:89`](D:/MyProject/Erza/erza/agent/context_governor.py:89)。
- 记忆主实现：[`memory.py:100`](D:/MyProject/Erza/erza/agent/memory.py:100)。
- turn 级 prompt/usage telemetry：[`turn_telemetry.py:21`](D:/MyProject/Erza/erza/agent/turn_telemetry.py:21)。

### 3.2 核心层关键风险

#### F-001：高风险工具尚未形成严格的执行前审批（P0，待复核）

`SafetyPolicy` 目前负责风险分类和是否需要 checkpoint 的判断：[`safety_policy.py:21`](D:/MyProject/Erza/erza/agent/safety_policy.py:21)。但 `ToolExecutionCoordinator.run_tool()` 先调用 `_run_tool_impl()`，之后才通过 `checkpoint_callback` 发出 `tool_completed` checkpoint：[`tool_execution.py:141`](D:/MyProject/Erza/erza/agent/execution/tool_execution.py:141)。

静态阅读结果意味着：如果没有隐藏在下游工具中的审批机制，高风险写入、删除或执行动作可能在用户确认前已经发生。复核 Agent 应验证：

- `checkpoint_callback` 是否有任何同步阻塞/拒绝语义；
- HIGH 风险工具是否存在另一条执行前拦截路径；
- 默认配置是否允许无沙箱执行。

#### F-002：Step acceptance 可能对未满足 criteria 的步骤给出正向接受（P0，待复核）

[`step_acceptance.py:242`](D:/MyProject/Erza/erza/agent/step_acceptance.py:242) 的 `_is_accepted()` 在 `done_criteria` 未出现在最终文本中时，只要存在 `tool_calls` 仍可能返回 `True`。这可能把“执行过工具”误判成“步骤已完成”。复核 Agent 应沿完整调用链确认该分支是否实际用于 `delegate_plan` 的继续/终止决策，以及 verifier 是否总会覆盖它。

## 4. 第二层：Harness 平台能力

| 能力域 | 当前实现 | 评价 |
|---|---|---|
| 模型与 Provider | Provider 抽象、OpenAI 兼容、Responses API、retry、fallback、provider registry、usage/cost budget | 较强 |
| 运行时与消息总线 | `MessageBus`、Composition Root、Agent/Channel 解耦、并发 gate、取消 | 较强 |
| 会话与后台生命周期 | Session 原子写、Cron、Dream、heartbeat、目标状态、逆序 shutdown | 较强 |
| 配置与热切换 | Pydantic 配置、环境变量、model preset、Provider snapshot/hot switch | 较强 |
| 身份认证 | DM pairing、API Bearer Key | 部分 |
| 授权与多租户 | 未发现完整 RBAC、用户身份模型、租户资源隔离和租户级审计 | 缺失/短板 |
| 文件/网络/进程隔离 | workspace scope、SSRF 防护、可选 bwrap、Docker 限制 | 部分 |
| Secrets 管理 | 配置/API key 支持，但未见独立 secret manager 或租户级 secret 生命周期 | 部分 |
| MCP/插件/Skills/外部环境 | MCP、多工具插件入口、Skills、CLI App、频道插件 | 较强 |
| 可观测性 | Loguru、turn telemetry、prompt telemetry、turn_end 字段、usage | 部分 |
| Trace 与回放 | 有事件、session 和部分 checkpoint，但未形成统一 trace/span/replay 产品能力 | 偏弱 |
| 评估体系 | 大量单元/集成测试、架构守卫、memory benchmark | 部分 |
| Agent 行为评测 | 未发现完整 golden task、模型输出质量评分、离线回归集 | 缺失 |

API 认证目前是单一 Bearer Key 语义，见 [`api/server.py:476`](D:/MyProject/Erza/erza/api/server.py:476)，这能保护开发 API，但不能替代多用户授权系统。

## 5. 第三层：产品与生产能力

| 能力域 | 当前实现 | 评价 |
|---|---|---|
| 用户入口 | CLI、WebUI、OpenAI API、WebSocket、多个 IM channel | 较强 |
| 部署 | Docker 多阶段构建、非 root 用户、healthcheck、Compose 资源限制 | 单机较强 |
| CI | Python 多版本/多 OS、前端 lint/test/build | 较强 |
| 高可用与扩展 | 单进程/单机模型，无集群协调、分布式队列或 leader 选举 | 缺失 |
| 运维 | healthcheck、容器日志、重启策略 | 部分 |
| 指标与告警 | 有应用级 telemetry，但未见完整 metrics backend、告警和 SLO | 偏弱 |
| 备份与恢复 | Session/memory 有局部恢复能力，但未形成全系统备份/恢复流程 | 部分 |
| 架构治理 | Composition Root、依赖方向测试、私有访问豁免清单 | 较强 |
| 文档一致性 | README 完整，但架构文档存在旧文件名/路径漂移 | 部分 |
| 测试工程质量 | 测试面广，但当前环境全量结果和格式检查未完全 clean | 部分 |

Docker 和 CI 证据：

- [`Dockerfile`](D:/MyProject/Erza/Dockerfile)
- [`docker-compose.yml`](D:/MyProject/Erza/docker-compose.yml)
- [`.github/workflows/ci.yml:17`](D:/MyProject/Erza/.github/workflows/ci.yml:17)

文档路径漂移示例见 [`module-boundaries.md:233`](D:/MyProject/Erza/docs/architecture/module-boundaries.md:233)：文档引用 `safety.py`、`planning.py`、`delegation.py`，当前实际文件包括 `safety_policy.py`、`planning_policy.py` 和 `tools/execute_plan.py` 等。需要确认这是文档未更新、兼容别名，还是架构登记表已经失真。

## 6. 综合成熟度

| 维度 | 评级 | 说明 |
|---|---:|---|
| Agent 核心闭环 | 4.0/5 | 主流程完整，最近提交显著增强了计账、预算、上下文和终止控制 |
| Harness 平台 | 3.4/5 | Provider、运行时、扩展和持久化较成熟 |
| 安全与治理 | 2.8/5 | 有安全边界，但审批、身份、租户和强隔离不足 |
| 可观测与评估 | 2.8/5 | 有 telemetry 和大量测试，缺完整 Trace、回放和行为评测 |
| 产品与单机部署 | 3.8/5 | CLI/WebUI/API/Docker/CI 完整，适合个人和单机 |
| 综合 | **3.5/5** | **Alpha+** |

适用场景判断：

- **个人 AI 助手：** 基本适合。
- **开发辅助：** 适合，但应启用 workspace 限制和沙箱。
- **可信内部工具：** 可以试运行，需补审计和审批。
- **多用户团队平台：** 需要先补身份、权限、隔离和审计。
- **公网多租户 SaaS：** 当前不建议直接上线。
- **高风险自动执行：** 需要先解决 F-001，并验证默认 fail-closed 行为。

## 7. 现有验证结果

此前针对最近 P0–P4 的定向测试子集结果为 **117 passed**，架构依赖测试为 **3 passed**。

此前全量测试无法判定为 green：

- `969 passed`
- `13 failed`
- `878 errors`

已知主要干扰因素是本机默认 `~/.erza` 和 pytest 临时目录权限；此外 `.venv` 缺少 `pytest-timeout`，但 `pyproject.toml` 配置了 timeout 选项，产生 unknown config warning。因此这组结果不能直接等价为生产代码有 878 个真实缺陷，但也不能宣称全量通过。

静态检查此前结果：

- `ruff check`：1 个错误。
- `ruff format --check`：32 个文件未格式化。

复核 Agent 应在干净临时 HOME、可写 pytest 临时目录和完整 dev 依赖下重新执行，区分环境错误与代码错误。

## 8. 建议优先级

### P0：安全和正确性

1. 把 HIGH/CRITICAL 工具的审批放到实际执行前，并明确 allow/deny/approve 三态。
2. 修正 step acceptance：工具调用不能自动等价于 done criteria 满足。
3. 对删除、写文件、Shell、外发消息、网络调用建立可审计的风险策略和撤销/补偿语义。

### P1：平台治理

1. 增加用户身份、RBAC、workspace/tenant 隔离和租户级审计。
2. 将 Secret 与普通配置分离，避免 API key 只依赖文件/环境变量管理。
3. 建立统一 Trace：turn → model call → tool call → subagent → checkpoint → final response。
4. 增加 golden tasks、离线 replay、模型升级回归和成本/成功率/安全拒绝率指标。

### P2：工程质量

1. 在 CI 中固定 `pytest-timeout` 或移除无效 timeout 配置。
2. 清理 ruff error 和 format drift。
3. 同步架构文档中的文件路径、类名和兼容别名说明。
4. 增加单进程以外的恢复、备份和升级演练文档。

## 9. 给复核 Agent 的任务清单

请独立验证以下问题，不要只复述本报告：

1. F-001 是否存在真实的“执行前审批缺失”；列出完整调用链和可绕过路径。
2. F-002 是否会影响实际 Managed plan 的 step 完成与 replan；补一个最小失败测试。
3. `restrict_to_workspace`、Shell sandbox、API auth 的默认值是否 fail-closed。
4. 子 Agent 是否继承主 Agent 的预算、权限、workspace scope 和取消信号。
5. full test 中的 failed/error 是否由环境权限导致；在隔离临时目录中给出真实结果。
6. Provider retry/fallback 是否会重复副作用、重复计费或重复记账。
7. 文档中的旧路径是否存在兼容模块，还是纯粹的文档漂移。
8. 基于代码证据，重新给出核心层、平台层、生产层三个分数，并明确任何与本报告不同的结论。

## 10. 建议复核命令

```powershell
git rev-parse HEAD
uv run pytest tests/agent tests/providers tests/security tests/session tests/architecture -q
uv run ruff check erza
uv run ruff format --check erza
rg -n "checkpoint_callback|requires_checkpoint|_is_accepted|done_criteria" erza tests
rg -n "tenant|rbac|role|permission|trace|span|replay|evaluation|golden" erza tests docs
```

## 11. 限制与免责声明

本报告是静态代码和已有测试结果驱动的架构评估，不是渗透测试、形式化安全证明或真实 Provider 压测。报告中的 F-001、F-002 标记为“待复核”，是为了明确高风险判断的证据位置和验证要求，而不是在未完成独立运行验证前把它们当作最终缺陷结论。
