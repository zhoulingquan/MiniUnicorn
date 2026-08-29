# MiniUnicorn 面向小型企业长期使用的业务智能体源码分析报告

> 报告日期：2026-08-26  
> 分析基线：当前工作区源码  
> 分析原则：以 `miniunicorn/` 源码中的类、函数、调用关系、配置默认值和持久化实现为主要依据；不以 README 作为架构事实来源。  
> 目标场景：单用户、小型企业、长期运行的业务智能体，暂不考虑多用户平台化。

## 1. 执行摘要

MiniUnicorn 当前已经具备较完整的通用 Agent Harness 能力：消息接收、模型调用、工具执行、会话持久化、记忆、上下文治理、Provider 切换、失败恢复、子 Agent、定时任务、WebUI、API 和多渠道接入均已有代码实现。

从源码结构看，它现在更像一个“通用个人 Agent 运行底盘”，还不是完整的“企业业务智能体产品”。当前最明显的缺口不是 Agent 循环本身，而是企业业务领域层：客户、订单、产品、合同、库存、财务、销售任务、业务审批和权威数据连接器尚未形成独立模块。

本报告继续使用 12 个分析维度，但每个维度的模块归属、工作逻辑和问题判断均来自源码调用关系。

总体判断如下：

| 组成部分 | 当前完成度 | 对小型企业长期使用的判断 |
|---|---|---|
| 1. 编排循环 | 较强 | 需要从“单轮对话”扩展到“可恢复业务任务” |
| 2. 工具系统 | 较强 | 通用工具完善，但缺企业业务工具 |
| 3. 记忆系统 | 较强 | 记忆治理较好，但不能替代业务数据库 |
| 4. 上下文管理 | 较强 | 基础治理完整，需要业务优先级 |
| 5. Prompt 构建 | 中上 | 模板化较好，需要企业规则和版本管理 |
| 6. 输出解析 | 中上 | Provider 兼容较好，需要业务输出契约 |
| 7. 状态管理 | 中上 | 单机可用，需要独立业务状态和事务记录 |
| 8. 错误处理 | 中上 | 通用恢复较好，需要业务错误和补偿机制 |
| 9. 安全防护 | 部分完成 | 是进入真实业务数据环境前的重点 |
| 10. 验证循环 | 部分完成 | 需要从文本判断升级为真实业务证据验证 |
| 11. 子 Agent 编排 | 较强 | 需要父任务总预算和更严格的业务写权限 |
| 12. 终止条件 | 较强 | 需要任务级、日级成本和 SLA 控制 |

## 2. 当前源码体现出的整体运行链路

### 2.1 启动和装配

主要代码位于：

- `miniunicorn/composition/gateway.py`
- `miniunicorn/composition/agent_app.py`
- `miniunicorn/config/schema.py`

GatewayApplication 按以下顺序创建运行对象：

```text
Config
  ↓
MessageBus
  ↓
ProviderSnapshot
  ↓
SessionManager
  ↓
CronService
  ↓
AgentLoop
  ↓
ChannelManager
  ↓
Dream / Heartbeat / Cron system jobs
```

`composition` 是装配层，负责把 Agent、Provider、Session、Cron、Channel 和 MessageBus 连接起来。业务模块不应该反向依赖装配层，这种依赖方向在 `docs/architecture/module-boundaries.md` 中有记录，但本报告以源码实际引用关系为准。

### 2.2 一次普通消息

```text
CLI / WebUI / API / 聊天渠道
          ↓
MessageBus.publish_inbound
          ↓
MessageDispatcher.run
          ↓
会话锁和 pending queue
          ↓
TurnOrchestrator.process_turn
          ↓
RESTORE → COMPACT → COMMAND → BUILD → RUN → SAVE → RESPOND
          ↓
AgentRunner.run
          ↓
Provider → 工具执行 → 工具结果回传
          ↓
SessionManager.save
          ↓
MessageBus.publish_outbound
```

### 2.3 与企业业务目标的关系

当前链路已经能很好地完成：

```text
读取资料 → 分析 → 调用通用工具 → 生成回复
```

但企业长期任务通常是：

```text
创建业务任务
→ 查询权威数据
→ 执行业务动作
→ 等待外部状态变化
→ 触发提醒
→ 请求人工审批
→ 更新业务系统
→ 验证结果
→ 关闭任务
```

第二种流程需要独立的业务任务状态、业务数据模型、审批状态和动作幂等性，不能只依靠聊天 Session 和自然语言回复。

## 3. 12 个组成部分详细分析

## 3.1 编排循环

主要代码：

- `miniunicorn/agent/loop.py`
- `miniunicorn/agent/runner.py`
- `miniunicorn/agent/turn_orchestrator.py`
- `miniunicorn/agent/dispatch.py`
- `miniunicorn/agent/turn_telemetry.py`

### 工作逻辑

`AgentLoop` 是兼容性外观和核心运行入口，内部组合了多个服务：

- `MessageDispatcher`：消费入站消息、创建任务、维护会话锁。
- `TurnOrchestrator`：执行一次 turn 的状态机。
- `AgentRunner`：执行模型调用和工具调用循环。
- `RuntimeResourceRegistry`：管理 Memory、Consolidator、Dream 等运行资源。
- `SessionTurnService`：处理用户消息、保存 turn 和恢复 checkpoint。
- `ResponseAssembler`：生成出站消息和使用量信息。
- `ProviderRegistry`：管理当前模型、Provider 和上下文窗口。

`TurnOrchestrator` 的实际状态为：

```text
RESTORE
→ COMPACT
→ COMMAND
→ BUILD
→ RUN
→ SAVE
→ RESPOND
→ DONE
```

`AgentRunner` 在 RUN 阶段持续执行：

```text
模型请求
→ 解析文本或工具调用
→ 执行工具
→ 把工具结果加入消息
→ 再次请求模型
→ 完成或触发停止条件
```

### 当前已有能力

- 会话级锁。
- 会话内消息排队。
- 并发 gate。
- 用户主动停止。
- 中途子 Agent 结果注入。
- Provider 热切换。
- turn 级 telemetry。
- 每轮调用计账。
- 中断后的 checkpoint 恢复。

### 当前不足

核心编排对象围绕“对话轮次”组织，而不是围绕“业务任务”组织。长业务流程如果跨越多个小时或多个外部事件，当前主要依靠 Cron、Session 和部分 goal 状态拼接，缺少统一的业务任务状态机。

### 建议完善

增加独立的业务任务模型：

```text
BusinessTask
- task_id
- task_type
- status
- current_step
- deadline
- retry_count
- approval_status
- source_records
- last_error
```

同时增加：

- 任务暂停、恢复和取消。
- 任务级 trace_id。
- 任务级幂等键。
- 重启后自动恢复。
- 业务任务和聊天 Session 分离。

对单用户小企业，不需要立即引入分布式工作流系统，SQLite 任务表加 Cron 即可满足初期需求。

## 3.2 工具系统

主要代码：

- `miniunicorn/agent/tools/base.py`
- `miniunicorn/agent/tools/registry.py`
- `miniunicorn/agent/tools/loader.py`
- `miniunicorn/agent/execution/tool_execution.py`

### 工作逻辑

模型返回工具调用后，系统依次执行：

```text
查找工具
→ 类型转换
→ JSON Schema 校验
→ 风险判断
→ 并发分组
→ 执行
→ 结果规范化
→ 结果截断或持久化
→ 返回模型
```

工具基类支持：

- 工具名称和描述。
- 参数 Schema。
- 参数类型转换。
- 参数校验。
- 只读标志。
- 并发安全标志。
- 独占执行标志。
- 结果可压缩标志。
- 缓存标志。
- 风险等级。
- 别名。
- 工具作用域。

当前工具主要包括：

- 文件读取、写入、编辑和删除。
- Shell 和执行会话。
- 文件搜索和文本搜索。
- Web 搜索和网页抓取。
- 深度研究。
- MCP。
- CLI 应用。
- Cron。
- 长任务。
- 计划执行。
- 子 Agent。
- 消息发送。
- 图片生成。
- Agent 自省。

### 当前优点

- 工具接口统一。
- 参数契约明确。
- 支持自动发现。
- 支持插件 entry point。
- 支持 MCP。
- 支持工具别名。
- 支持工具并发。
- 支持结果压缩。
- 支持风险策略。

### 当前不足

当前工具主要解决“操作文件、网络和程序”的问题，缺少企业业务工具。源码中尚未形成独立的客户、订单、库存、合同、财务和销售业务模块。

### 建议完善

建议先建立一套业务连接器和业务工具：

```text
客户工具：查询、创建、跟进、客户历史
订单工具：查询、创建、修改状态、发货查询
财务工具：应收、付款、对账
合同工具：查询、版本、到期提醒
知识库工具：制度、产品、合同条款查询
```

业务工具必须具备：

- 强类型输入。
- 字段白名单。
- 数据来源说明。
- 幂等键。
- 预览模式。
- 审批状态。
- 操作前后状态。
- 结构化返回值。
- 可回读验证。

## 3.3 记忆系统

主要代码：

- `miniunicorn/agent/memory.py`
- `miniunicorn/agent/memory_models.py`
- `miniunicorn/agent/memory_repository.py`
- `miniunicorn/agent/memory_lifecycle.py`
- `miniunicorn/agent/memory_recall.py`
- `miniunicorn/agent/memory_extraction.py`
- `miniunicorn/agent/dream_trigger.py`
- `miniunicorn/agent/reflection.py`

### 工作逻辑

当前记忆系统由多个层次组成：

1. Session：保存当前对话和工具调用。
2. 历史归档：保存压缩后的旧消息。
3. Scratchpad：保存临时笔记。
4. 结构化记忆：SQLite 中的正式记忆记录。
5. Reflection：保存失败经验和反思。
6. Dream：从历史和反思中提取候选事实。
7. GitStore：保存部分文件和长期记忆的版本历史。

结构化记忆具有：

- 记忆状态。
- 记忆类型。
- 作用域。
- 标签。
- 证据。
- 冲突键。
- 修订记录。
- 过期时间。
- 事务记录。

正常流程是：

```text
对话和工具结果
→ 历史压缩
→ Dream 提取候选事实
→ 证据和冲突检查
→ 记忆生命周期处理
→ active 记忆
→ 后续 Prompt 召回
```

### 当前优点

- 有结构化记忆，而不是只有文本文件。
- 有候选、激活、撤销和过期状态。
- 有证据引用。
- 有冲突处理。
- 有 SQLite WAL。
- 记忆损坏时可以 fail-closed。
- 支持项目、会话、用户和共享作用域。

### 当前不足

Agent Memory 和企业业务数据还没有严格分层。

以下内容可以作为 Agent Memory：

- 用户偏好。
- 企业工作习惯。
- 常用回复格式。
- 过去失败经验。
- 长期业务规则。

以下内容不应该只依赖 Memory：

- 客户余额。
- 订单状态。
- 库存数量。
- 合同正式版本。
- 财务账目。

这些数据必须从 CRM、ERP、数据库或权威文件源实时读取。

### 建议完善

- 在记忆记录中增加来源系统和原始记录 ID。
- 保存数据更新时间和过期时间。
- 显示记忆证据和来源。
- 为企业文档增加全文检索或混合检索。
- 增加记忆查看、修改、撤销和恢复界面。
- 增加完整备份和恢复流程。
- 对敏感记忆脱敏或加密。
- 对业务事实设置更短的有效期。

## 3.4 上下文管理

主要代码：

- `miniunicorn/agent/context_governor.py`
- `miniunicorn/agent/runner_strategies.py`
- `miniunicorn/agent/context_strategies/schema_crop.py`
- `miniunicorn/agent/autocompact.py`

### 工作逻辑

上下文治理负责限制发送给模型的信息量，当前包括：

- 裁剪旧历史。
- 压缩旧工具结果。
- 删除孤立工具结果。
- 修复工具消息顺序。
- 裁剪工具 Schema。
- 自动压缩空闲会话。
- 限制工具结果 token。
- 限制每轮输入 token。
- 根据压力调整上下文内容。

系统不会简单地把所有历史消息都传给模型，而是按优先级处理内容。

### 当前优点

- 有 ContextGovernor。
- 有内置策略管线。
- 有上下文压力概念。
- 有 Prompt telemetry。
- 能处理复杂工具调用消息。
- 能避免大工具结果无限占用上下文。

### 当前不足

当前优先级主要是通用 Agent 优先级，还不是企业业务优先级。

企业场景应优先保留：

```text
当前业务任务
> 当前订单或客户
> 最新权威数据
> 当前审批信息
> 相关制度和合同
> 历史对话
> 无关工具输出
```

### 建议完善

- 根据业务对象设置上下文优先级。
- 金额、日期、数量和订单号不能被普通截断策略破坏。
- 工具结果按字段裁剪，而不是只按字符裁剪。
- 注入数据时保留来源和更新时间。
- 最终回复显示使用了哪些业务数据。
- 为不同业务任务设置不同上下文预算。

## 3.5 Prompt 构建

主要代码：

- `miniunicorn/agent/context.py`
- `miniunicorn/agent/skills.py`
- `miniunicorn/templates/agent/`

### 工作逻辑

`ContextBuilder.build_system_prompt()` 会组合：

- Agent 身份。
- 操作系统和工作区信息。
- `AGENTS.md`、`SOUL.md`。
- 工具契约。
- 共享政策。
- 结构化记忆。
- 临时笔记。
- Skills。
- 最近历史。
- 会话摘要。
- Subagent 信息。
- 当前渠道信息。

内容按照优先级进入 Prompt，超过预算后低优先级内容会先被裁剪。

### 当前优点

- Prompt 模板化。
- 身份、工具、记忆和技能分层。
- Skill 可以动态加载。
- 支持禁用 Skill。
- 支持主 Agent 和子 Agent 不同身份。
- 支持轻量上下文模式。
- 结构化记忆按查询结果注入。

### 当前不足

缺少面向具体企业的规则层。当前 Prompt 还没有形成完整的：

```text
企业目标
→ 数据来源优先级
→ 业务术语
→ 可执行动作
→ 禁止动作
→ 审批规则
→ 敏感信息规则
→ 业务成功标准
```

### 建议完善

将 Prompt 拆成：

```text
平台安全规则
→ 企业通用规则
→ 业务领域规则
→ 当前流程规则
→ 当前业务数据
→ 用户临时要求
```

同时增加：

- Prompt 版本号。
- Prompt 变更记录。
- Prompt 回归测试。
- 可信指令和不可信资料的区分。
- 外部文档中的 prompt injection 防护。
- 敏感信息不直接写入普通 Prompt 文件。

## 3.6 输出解析

主要代码：

- `miniunicorn/providers/base.py`
- `miniunicorn/providers/openai_compat_provider.py`
- `miniunicorn/providers/openai_responses/parsing.py`
- `miniunicorn/agent/execution/model_request.py`

### 工作逻辑

Provider 层将不同模型的返回统一为：

```text
LLMResponse
- content
- tool_calls
- finish_reason
- usage
- reasoning_content
- error metadata
```

工具调用统一为：

```text
ToolCallRequest
- id
- name
- arguments
```

当前处理了：

- Chat Completions。
- Responses API。
- SSE 流式输出。
- 工具参数 JSON 修复。
- reasoning 内容。
- 工具调用 ID。
- Provider 错误。
- refusal、content filter 和 error 状态。

只有合法的 finish reason 才会进入工具执行。

### 当前评价

Provider 兼容能力较强，可以降低不同模型供应商对核心 Agent 的影响。

### 建议完善

企业动作应该返回结构化结果，例如：

```json
{
  "status": "success",
  "action": "update_customer",
  "record_id": "C001",
  "changed_fields": ["level"],
  "source": "crm",
  "requires_approval": false
}
```

建议增加：

- 业务结果 Schema。
- 结果版本号。
- 必填字段完整性校验。
- 工具调用幂等键。
- 外部系统回读。
- “模型说成功”和“业务系统成功”的明确区分。

## 3.7 状态管理

主要代码：

- `miniunicorn/session/manager.py`
- `miniunicorn/session/writes.py`
- `miniunicorn/agent/session_turn.py`
- `miniunicorn/session/goal_state.py`
- `miniunicorn/utils/gitstore.py`

### 工作逻辑

SessionManager 负责：

- 创建和读取会话。
- 保存消息。
- 保存 turn。
- 管理历史窗口。
- 保存 checkpoint。
- 恢复未完成的 turn。
- 保存目标状态。
- 关闭时 flush 缓存。

写入采用临时文件、flush、fsync 和替换方式，降低进程崩溃导致数据损坏的风险。

### 当前评价

对于单用户、单进程、单机部署，当前状态管理已经具备较好的基础：

- 会话原子写入。
- checkpoint。
- 目标状态。
- 历史裁剪。
- Git 版本记录。
- 关闭时刷新。

### 当前不足

聊天 Session、长期 Agent 状态和企业业务状态仍然需要进一步分离。

建议新增：

```text
business_tasks
business_events
business_actions
business_approvals
```

每次业务写入应记录：

```text
操作对象
操作前状态
操作后状态
操作时间
触发来源
审批记录
外部系统响应
幂等键
```

还需要数据库迁移、备份恢复、数据版本和业务冲突处理。

## 3.8 错误处理

主要代码：

- `miniunicorn/providers/base.py`
- `miniunicorn/providers/fallback_provider.py`
- `miniunicorn/agent/execution/recovery.py`
- `miniunicorn/agent/reflection.py`

### 工作逻辑

模型请求失败时，系统可以：

- 识别超时、连接错误和限流。
- 进行延迟重试。
- 使用备用 Provider。
- 处理空响应。
- 处理输出过长。
- 处理工具执行错误。
- 处理工作区和 SSRF 边界错误。
- 处理用户取消。
- 生成 Reflection。

工具错误通常会变成模型可见的工具结果，让模型尝试修正。

### 当前评价

通用错误处理较完整，具备 Provider retry、fallback、超时和恢复策略。

### 建议完善

企业业务错误必须分类：

```text
技术错误
业务错误
权限错误
审批错误
数据错误
模型错误
```

例如：

- 网络超时可以重试。
- 库存不足不应无限重试。
- 订单已创建不能再次创建。
- 未审批不能更换工具绕过。
- 数据冲突应进入人工处理。

建议增加：

- 失败任务队列。
- 死信任务。
- 业务补偿操作。
- 外部系统熔断。
- 失败通知。
- 副作用状态记录。
- 失败请求的成本和使用量归因。

## 3.9 安全防护

主要代码：

- `miniunicorn/agent/safety_policy.py`
- `miniunicorn/agent/execution/tool_execution.py`
- `miniunicorn/security/workspace_access.py`
- `miniunicorn/security/network.py`
- `miniunicorn/agent/tools/sandbox.py`
- `miniunicorn/pairing/store.py`

### 当前已有能力

- 默认工作区限制。
- 文件路径越权检查。
- Shell 工作目录限制。
- SSRF 检查。
- 内网和云元数据地址拦截。
- DNS rebinding 防护。
- bwrap 沙箱支持。
- LOW、MEDIUM、HIGH 风险等级。
- 高风险拒绝策略。
- 高风险审批回调。
- `tool_started`、`tool_completed`、`tool_blocked` 检查点。
- 聊天发送者配对。
- API Bearer Key。

### 当前主要问题

当前高风险审批虽然已有代码入口，但还没有完整的用户审批流程：

```text
生成审批请求
→ 展示操作详情
→ 用户批准或拒绝
→ 继续执行或终止
→ 保存审批证据
```

目前 `approval_callback` 更接近 SDK/嵌入层接口，WebUI 和聊天渠道还需要补审批交互。

此外，高风险策略默认偏向兼容性，Shell 沙箱也不是所有环境都强制启用。

### 建议完善

企业内部使用前，应至少完成：

1. WebUI 审批页面。
2. `/approve` 和 `/deny` 命令。
3. 审批超时。
4. 业务写操作审计。
5. 外发消息审批。
6. 删除操作审批。
7. 生产环境强制沙箱。
8. Secret 与普通配置分离。
9. 紧急停止。
10. 不可篡改的操作日志。

即使暂不考虑多用户，也不能省略审批和审计，因为单用户环境同样可能发生模型误操作。

## 3.10 验证循环

主要代码：

- `miniunicorn/agent/planner.py`
- `miniunicorn/agent/planning_policy.py`
- `miniunicorn/agent/step_acceptance.py`
- `miniunicorn/agent/progress_policy.py`
- `miniunicorn/agent/execution/planning.py`

### 工作逻辑

当前支持 FAST 和 MANAGED 两类执行方式。

FAST 模式：

```text
理解任务 → 调用工具 → 查看结果 → 继续执行
```

MANAGED 模式：

```text
生成计划
→ 执行步骤
→ 检查完成条件
→ 完成则进入下一步
→ 失败则重规划
```

Planner 支持：

- 生成计划。
- 解析计划。
- 回退到单步计划。
- 失败重规划。
- 最大重规划次数。
- 计划快照。
- Step Acceptance。
- No-progress 检测。
- 可选 LLM verifier。

当前 Step Acceptance 已经不会仅因为“执行过工具”就接受带有完成条件的步骤，但完成条件仍然主要依赖最终文本是否包含指定内容。

### 当前不足

例如：

```text
完成条件：report.xlsx 已创建
```

只检查模型回复中是否出现这句话，并不能证明文件真的存在、内容正确或已经写入目标系统。

### 建议完善

增加业务领域验证器：

```text
文件验证：文件存在、大小、哈希
数据库验证：记录存在、字段值正确
订单验证：重新读取订单状态
财务验证：金额、币种、日期一致
消息验证：第三方系统返回消息 ID
审批验证：审批记录已经保存
```

对于串行业务流程，某一步失败后不应默认继续下一步，应支持：

- 失败即停止。
- 请求人工确认。
- 补偿操作。
- 回滚。
- 业务依赖关系。
- 任务恢复。

## 3.11 子 Agent 编排

主要代码：

- `miniunicorn/agent/subagent.py`
- `miniunicorn/agent/subagent_registry.py`
- `miniunicorn/agent/tools/execute_plan.py`
- `miniunicorn/agent/tools/spawn.py`
- `miniunicorn/agent/tools/delegate.py`

### 工作逻辑

当前支持：

- 创建子 Agent。
- 等待子 Agent。
- 并行任务。
- 串行任务。
- 工具白名单。
- 工作区作用域继承。
- 模型覆盖。
- 最大并发数。
- 最大递归深度。
- 子 Agent 取消。
- 子 Agent 状态和活动流。
- 独立 Session。
- 独立 turn budget。

`ExecutePlanTool` 会根据步骤之间的依赖关系选择并行或串行。

### 当前优点

- 有递归限制。
- 有并发限制。
- 有工作区范围继承。
- 有预算工厂。
- 有结果回传。
- 有子 Agent 活动状态。

### 建议完善

企业写操作不应默认交给子 Agent。

适合并行：

```text
分析多份独立报告
查询多个独立数据源
检查多个独立文件
```

不适合并行：

```text
创建订单
扣减库存
发送合同
修改客户状态
```

建议增加：

- 父任务总预算。
- 父子任务统一 trace。
- 子 Agent 结果 Schema。
- 子 Agent 失败后的取消传播。
- 子 Agent 默认只读。
- 业务写权限默认只允许主 Agent 使用。
- 显式依赖图。

## 3.12 终止条件

主要代码：

- `miniunicorn/agent/turn_budget.py`
- `miniunicorn/agent/call_ledger.py`
- `miniunicorn/agent/execution/recovery.py`
- `miniunicorn/agent/execution/model_request.py`

### 当前工作逻辑

Agent 可以因为以下原因停止：

- 生成最终回复。
- 达到最大工具迭代次数。
- 达到输入 token 上限。
- 达到成本上限。
- 达到 turn wall-time。
- Provider 错误。
- 工具错误。
- 用户 `/stop`。
- 任务取消。
- 计划完成。
- 重规划次数耗尽。
- 连续没有进展。
- 子 Agent 递归深度耗尽。

`CallLedger` 会记录一次 turn 内不同目的的模型调用，例如 Planner、Replan、Reflection、Memory、Compact 和普通模型请求。

### 当前评价

终止控制较完善，已经考虑：

- 无限循环。
- 工具迭代过多。
- token 超限。
- 成本超限。
- Provider 卡死。
- 用户中断。
- 子 Agent 失控。

### 建议完善

企业环境还需要：

```text
单轮预算
→ 单个业务任务预算
→ 每日预算
→ 每月预算
→ 单个外部系统调用预算
```

同时增加：

- 工具级超时。
- 业务任务 deadline。
- Cron 最大重试次数。
- 超预算前提醒。
- 超预算后的摘要结果。
- 子 Agent 总预算。
- 长任务暂停和恢复。
- 失败任务自动挂起。

## 4. 面向小型企业的关键缺口

### 4.1 缺少企业业务领域层

当前源码主要由以下部分组成：

```text
Agent
Provider
Tools
Memory
Session
Channels
WebUI
API
Cron
Security
```

还没有独立的：

```text
Customer
Order
Product
Inventory
Contract
Finance
SalesTask
BusinessWorkflow
```

因此现在的 Agent 能够“操作文件和调用工具”，但还不能稳定地理解企业业务对象和业务状态。

### 4.2 缺少权威数据连接器

企业 Agent 必须知道：

- 数据来自哪个系统。
- 数据什么时候更新。
- 哪个系统是权威来源。
- 数据冲突如何处理。
- 哪些数据可以写回。
- 写入后如何验证。

建议先设计统一连接器接口，再实现第一个实际连接器：

```python
class BusinessConnector:
    async def health(self): ...
    async def get(self, record_id): ...
    async def search(self, query): ...
    async def execute(self, action): ...
```

### 4.3 缺少业务级审批和审计

当前底层已经具备风险等级和审批回调，但还没有形成完整产品流程。

企业写操作应形成：

```text
准备动作
→ 展示影响范围
→ 用户批准
→ 执行
→ 回读结果
→ 保存审计
```

### 4.4 缺少业务级验证

Agent 不应因为“自己说完成了”就结束，而应通过文件、数据库、接口或第三方系统的真实结果判断完成。

### 4.5 缺少完整运维闭环

长期运行还需要：

- 健康检查。
- 错误告警。
- 任务失败通知。
- 数据备份。
- 恢复演练。
- 成本统计。
- 工具调用审计。
- 模型升级回归测试。

## 5. 建议实施顺序

### P0：安全和数据基础

1. 定义第一批业务对象，例如客户、订单和业务任务。
2. 建立业务数据库或第一个业务连接器。
3. 先实现只读业务工具。
4. 实现高风险操作审批流程。
5. 生产环境强制工作区限制和 Shell 沙箱。
6. 建立业务操作审计记录。
7. 增加数据库、Session 和 Memory 备份恢复。
8. 为所有业务写操作增加幂等键。

### P1：形成业务价值

1. 增加客户、订单、合同和财务查询。
2. 增加企业文档检索。
3. 增加日报、周报和逾期提醒。
4. 增加业务字段验证器。
5. 增加可恢复 BusinessTask。
6. 增加来源、更新时间和引用。
7. 增加失败队列和补偿机制。

### P2：提高智能程度

1. 用 Planner 处理复杂业务流程。
2. 用子 Agent 处理独立分析任务。
3. 增加父任务总预算。
4. 建立企业 Golden Tasks。
5. 建立离线回放。
6. 对模型升级进行回归评测。
7. 统计成功率、成本、误操作率和安全拒绝率。

## 6. 最终结论

从当前源码看，MiniUnicorn 的通用 Agent 底盘已经基本形成，较强的部分包括：

- 编排循环。
- 工具系统。
- Provider 适配。
- 记忆治理。
- 上下文管理。
- 会话持久化。
- 错误恢复。
- 子 Agent。
- 终止控制。

但要成为小型企业可以长期使用的业务智能体，下一阶段最重要的工作不是继续增加通用工具或复杂 Prompt，而是建立：

```text
企业业务模型
      +
权威数据连接器
      +
业务级工具
      +
审批与审计
      +
真实证据验证
      +
可恢复业务任务
```

最终形态应当是：

```text
MiniUnicorn Agent Harness
          +
企业业务领域层
          +
企业数据连接器
          +
审批、审计和任务恢复系统
```

对于暂不考虑多用户的前提，推荐继续采用单进程、单机、SQLite、文件工作区和 Cron 的轻量架构，但必须优先补齐业务状态、业务数据来源、审批、幂等、审计和备份。
