# Erza 模块化单体改造交接设计

## 目的

本文档提供一份可直接交给另一个工程 Agent 的提示词。该 Agent 的当前职责仅限于代码勘察、架构设计和分阶段实施计划；未经用户明确批准，不得修改生产代码、测试、配置或持久化格式。

本轮只解决“模块化单体”。“确定性业务工作流”只定义未来端口、语义和技术选型标准，不实现工作流引擎或示例业务流程。

## 已确认的架构方向

- 保持一个代码库、一个部署单元和一个进程内运行时，不拆微服务。
- 使用唯一、显式、静态的 Composition Root 装配模块，不建立 Everything-is-Plugin 或动态插件体系。
- 核心控制流使用直接、可追踪的方法调用；事件仅用于审计、通知、指标、缓存更新等非关键副作用。
- 模块拥有公开接口、内部实现、配置、生命周期、持久化边界和独立测试。
- AgentLoop 保留为兼容外观，但逐步退化为薄协调器。
- AgentRunner 在 AgentLoop 外围边界稳定后再拆分，避免同时重写两个核心执行路径。
- 迁移必须保持 CLI、WebUI、Channel、SDK、会话恢复、工具执行、流式输出、取消和并发语义兼容。
- 目标是降低依赖方向和变化传播范围，不以文件大小、接口数量或目录数量衡量成功。

## 当前代码事实

- `erza/agent/loop.py` 约 1710 行，负责运行时装配、资源解析、消息调度、会话并发、命令、Turn 执行、响应组装、持久化和恢复等多类职责。
- `erza/agent/runner.py` 约 1859 行，负责模型请求、工具执行、上下文治理、规划反思和多类恢复策略。
- `AgentLoopBuilder` 约 276 行，并明确用于处理 `AgentLoop` 的 30 多个构造参数。
- `_state_machine.py`、`_mcp_lifecycle.py` 和 `_provider_switching.py` 虽已拆文件，但通过 `self: AgentLoop` 访问中心对象内部状态，尚未形成独立模块边界。
- 当前会话并发模型是同会话串行、跨会话并发；会话持久化、Checkpoint、子代理、中途消息注入、Provider 切换和 MCP 生命周期都是迁移时必须保留的行为。
- 工作区目前存在用户的未跟踪目录 `.trae-html-share-packages/` 和 `arch-eval-erza-vs-aniyaa/`，不得删除、移动、覆盖或纳入无关提交。

## 可直接交给下一个 Agent 的提示词

```text
你正在接手 Erza 的架构改造设计。工作区位于：

D:\MyProject\Erza

你的任务不是立即修改代码，而是基于实际代码完成一份可执行、可分阶段审批的“模块化单体改造设计 + 实施计划”。用户批准设计和计划之前，不得修改生产代码、测试、配置、依赖、数据库或会话格式；不得开始重构。允许执行只读检查、静态分析和不会改写项目文件的诊断测试。若你的工作流要求生成设计文档，只能新增或修改明确的设计/计划文档，并先说明文件路径。

一、架构哲学

将 Erza 改造成：

“模块化单体 + 确定性业务工作流”

本阶段只落地“模块化单体”的架构设计。“确定性业务工作流”只约定未来端口、状态语义和技术选型标准，不实现工作流引擎，不实现具体业务流程，也不提前引入 Temporal、BPMN 或其他重量级依赖。

模块化单体必须满足：

1. 一个代码库、一个部署单元、一个进程内运行时。
2. 通过唯一、显式、静态的 Composition Root 完成模块装配。
3. 不建立 Everything-is-Plugin 架构，不支持任意动态安装、卸载或热替换内部业务模块。
4. 模块通过明确的公开接口协作；禁止跨模块读取对方私有状态。
5. 关键业务和 Agent 控制流优先使用显式方法调用，不通过事件监听顺序完成隐式编排。
6. 事件仅用于审计、通知、指标、缓存更新和其他非关键副作用；关键状态变更必须有单一权威写入点。
7. 外部开放能力放在系统边界，通过 HTTP/OpenAPI、MCP、消息队列或明确的 Adapter 接口提供。
8. 保持核心体验垂直整合，避免为了“可扩展”而制造当前没有消费者的抽象。

二、必须基于代码验证的现状

不要依据 README 中的产品定位进行判断，必须检查实际代码、调用路径、测试和近期提交。至少检查：

- erza/agent/loop.py
- erza/agent/loop_builder.py
- erza/agent/runner.py
- erza/agent/_state_machine.py
- erza/agent/_mcp_lifecycle.py
- erza/agent/_provider_switching.py
- erza/agent/context.py
- erza/agent/context_governor.py
- erza/agent/tools/registry.py
- erza/agent/tools/loader.py
- erza/session/manager.py
- erza/bus/
- erza/channels/
- erza/providers/
- erza/runtime/
- 相关 tests/ 目录

已知线索只能作为调查起点，必须自行复核：

- AgentLoop 约 1710 行，并直接承担多类运行时和应用职责。
- AgentRunner 约 1859 行，同样存在中心化复杂度。
- AgentLoopBuilder 用于处理 30 多个构造参数。
- 当前 Mixin 虽拆成独立文件，但仍反向依赖 AgentLoop 的内部属性。
- 当前基础并发语义为同会话串行、跨会话并发。

先输出当前架构的证据化分析：

1. 列出 AgentLoop 和 AgentRunner 的职责清单。
2. 标出每项职责的入口、状态所有者、依赖、输出和主要测试。
3. 绘制或用文本表示模块依赖方向和关键调用链。
4. 识别循环依赖、隐式共享状态、全局注册表、ContextVar、Mixin 反向依赖和跨层调用。
5. 区分“文件已经拆开”和“真正具有独立边界”。
6. 识别哪些行为是外部兼容契约，哪些只是内部实现细节。

引用结论时给出具体文件和符号；不要只给原则性评价。

三、目标架构

采用“模块化单体 + 静态组合根”路线。你需要根据代码证据确定最终模块名称和目录，不得机械套用下面的示例名称，但目标职责至少应覆盖：

1. Composition Root
   - 负责配置解析后的对象创建、模块装配、生命周期启动和逆序关闭。
   - 是具体实现彼此可见的唯一位置。
   - 禁止把现有 30 多个参数简单塞进通用 Services 字典、RuntimeContext 或 Service Locator。
   - 可以使用按内聚职责划分的强类型依赖对象，但每个依赖必须有明确消费者。

2. Message Dispatch
   - 消息接收与路由。
   - 同会话串行、跨会话并发。
   - 活跃任务、等待队列、中途消息注入、停止与取消。
   - 不拥有模型调用、工具执行或会话持久化细节。

3. Session Turn Application Service
   - 加载或创建会话。
   - 用户消息的提前持久化。
   - Turn 提交、Checkpoint 设置/恢复/清除。
   - 明确事务或提交边界，避免多个模块都能任意写 Session。

4. Turn Orchestration
   - 将当前 StateMixin 改造成具有显式输入、输出和依赖的可测试协调器。
   - 不允许通过 `self: AgentLoop` 读取任意私有属性。
   - 状态转换和失败语义必须可观察、可测试。

5. Runtime Resources
   - 负责 Workspace Scope 以及 Memory、Consolidator、Dream 等按工作区或会话解析的资源。
   - 明确缓存所有权、生命周期和清理条件。
   - 不允许 AgentLoop 继续直接维护多套资源缓存和映射。

6. Command Application Service
   - 命令识别和执行与普通 Agent Turn 分离。
   - 明确哪些命令影响运行时，哪些影响会话，哪些只是查询。

7. Response Assembly
   - 负责 Channel 无关的出站结果、持久化内容清洗和响应元数据组装。
   - Channel Adapter 只处理协议映射，不承载 Agent 核心规则。

8. Agent Execution Engine
   - AgentRunner 最终保留为薄执行引擎或 Facade。
   - 它的拆分必须晚于 AgentLoop 外围边界稳定，不允许和外围重构同时大爆炸式进行。

四、AgentLoop 与 AgentRunner 的强制改造目标

AgentLoop 必须纳入本次设计，不能只新建模块目录后保留原有中心耦合。设计应使 AgentLoop 最终退化为兼容 Facade，原则上只保留：

- run
- stop
- process_direct
- 少量运行状态查询
- 对应用服务的薄编排

不要把行数作为唯一验收标准，但必须说明它最终仍拥有的状态和职责，以及为什么这些职责不可再下沉。

AgentRunner 也必须纳入总体设计，但作为后续独立里程碑。至少评估是否应形成以下内聚能力，名称可调整：

- ModelRequestExecutor：模型请求、流处理、Provider 错误和请求级重试。
- ToolExecutionCoordinator：工具解析、批次划分、并行/串行语义、结果提交和规范化。
- ContextGovernanceService：上下文预算、压缩、裁剪和注入消息。
- TurnRecoveryPolicy：空响应、长度超限、最大迭代和工具致命错误等恢复策略。
- PlanningReflectionService：Planner、Reflection 和步骤完成逻辑。

先验证这些边界是否与现有代码的变化原因一致。不要为了缩短文件创建只有一个调用方、没有独立变化原因的空壳类。

五、推荐迁移阶段

你的计划至少需要比较并细化以下阶段；可以根据代码证据调整顺序，但必须解释：

Phase 0：行为冻结和架构测绘

- 建立关键调用链、依赖图和状态所有权表。
- 找出现有 Characterization Tests 和缺口。
- 冻结 CLI、WebUI、Channel、SDK、会话格式及运行语义。
- 给出迁移期间必须持续通过的测试集合。

Phase 1：建立静态 Composition Root 和模块规则

- 集中对象创建和生命周期装配。
- 保留 AgentLoop 现有公共 API。
- 不改变运行行为和持久化格式。
- 定义模块公开 API、依赖方向和禁止依赖规则。

Phase 2：抽离 AgentLoop 外围职责

- 优先迁移 Message Dispatch、Session Turn、Runtime Resources、Command 和 Response Assembly。
- 每次只迁移一个职责。
- 每个迁移步骤必须可以独立测试、合并和回滚。

Phase 3：显式 Turn Orchestrator

- 去除 StateMixin 对 AgentLoop 私有状态的反向依赖。
- 把状态机改造成显式输入、输出和依赖。
- 保持 Turn/Step、取消、Checkpoint 和中途注入语义。

Phase 4：AgentLoop 退化为兼容 Facade

- 删除已迁移状态和委托逻辑。
- 收缩构造函数。
- 更新 Composition Root 和调用方，但保持外部 API 兼容。

Phase 5：拆分 AgentRunner

- 在外围边界稳定后再拆请求、工具执行、上下文治理和恢复策略。
- 保持工具顺序、并发、重试、流式输出和错误映射兼容。

Phase 6：清理和边界守卫

- 删除过渡适配层和无用参数。
- 增加依赖方向检查、模块契约测试和架构测试。
- 更新架构文档。

不要假设所有 Phase 必须在一个 PR 或一次实施中完成。请提出合理的 PR/里程碑切分，并标明每个阶段的回滚点。

六、必须保持的行为契约

至少覆盖以下内容，缺失项应通过代码调查补充：

- CLI、WebUI、各 Channel 和 Python SDK 的现有入口。
- 同会话串行、跨会话并发。
- 中途消息注入和 pending queue 顺序。
- `/stop`、任务取消和子代理清理。
- 流式输出、Reasoning 和 Tool trace。
- Turn/Step 与最大迭代语义。
- 工具调用顺序、允许的并行行为和结果顺序。
- MCP 连接与关闭生命周期。
- Provider/Model Preset 切换。
- Session JSONL 兼容、原子保存和 Checkpoint 恢复。
- Workspace 限制、SSRF 防护和权限策略。
- Memory、Consolidator、Dream 与 Workspace Scope 的现有行为。

如果你认为某项旧行为应改变，只能在设计中列为独立决策，不得在未批准时顺带修改。

七、确定性业务工作流的本阶段边界

本阶段不实现工作流引擎，只定义未来端口和决策标准。设计中应预留一个不污染 AgentLoop 的 Application Port，并约定：

- Workflow Definition、Instance、State、Command、Transition、Result 的语义。
- 显式状态转换；关键流程不能依赖 Prompt、插件顺序或 LLM 自主选择。
- 幂等键、操作者/租户上下文、Correlation ID、审计记录。
- 超时、重试、补偿、人工审批和取消的语义位置。
- 工作流状态与 Agent 会话状态相互独立，通过明确 ID 关联。
- 未来实现必须可在轻量状态机、BPMN 2.0 引擎和 Temporal 类持久化工作流之间替换或评估，而不要求它们共享同一个内部模型。

请给出后续选型时的决策矩阵，至少比较：

- 轻量、数据库持久化状态机。
- BPMN 2.0 工作流引擎。
- Temporal 类 Durable Execution 平台。

只定义评估维度和建议触发条件；当前不得选型落地或添加依赖。

八、明确禁止

- 未批准前修改代码、测试、配置、依赖或持久化格式。
- 把 Erza 改造成动态插件平台。
- 使用全局事件总线编排关键控制流。
- 用一个新的 God Object、Service Locator、万能 Context 或字典替换 AgentLoop。
- 为每个函数创建接口或抽象类。
- 以“文件不超过多少行”作为主要设计目标。
- 同时重写 AgentLoop 和 AgentRunner。
- 在缺乏 Characterization Tests 时进行移动或重命名。
- 顺带改造无关 UI、业务功能或协议。
- 删除、覆盖或提交用户已有的未跟踪文件和目录。
- 使用 `git reset --hard`、`git checkout --` 等破坏性命令。

九、必须提交的设计输出

你的第一轮回复必须完整、自洽，并包含：

1. Executive Summary
   - 当前核心问题。
   - 推荐目标架构。
   - 为什么这是模块化单体而不是插件平台或微服务。

2. Current-State Evidence
   - AgentLoop/AgentRunner 职责矩阵。
   - 状态所有权和依赖图。
   - 关键代码路径及测试证据。

3. Target Module Map
   - 每个模块做什么。
   - 公开 API。
   - 拥有的状态和持久化数据。
   - 允许依赖和禁止依赖。
   - 生命周期所有者。

4. Runtime Data Flow
   - 普通用户消息。
   - 命令消息。
   - 工具调用。
   - 中途消息注入。
   - 取消和恢复。

5. AgentLoop/AgentRunner End State
   - 最终保留职责。
   - 被抽离职责。
   - 兼容 Facade 策略。

6. Incremental Migration Plan
   - Phase、具体改动面、依赖关系、测试、验收和回滚点。
   - 推荐的 PR 切分。
   - 不允许出现“重构 AgentLoop”这种无法执行的笼统步骤。

7. Risk Register
   - 并发顺序变化。
   - Session/Checkpoint 数据损坏。
   - 生命周期泄漏。
   - ContextVar 或全局状态泄漏。
   - 工具顺序和错误语义变化。
   - 兼容层长期残留。

8. Architecture Decision Records
   - 列出需要用户批准的关键决策及推荐项。
   - 至少包括模块边界、Composition Root、Session 写入权、事件使用规则和工作流端口。

9. Acceptance Criteria
   - 可客观验证，不使用“更加优雅”“更易维护”等不可测表述。

10. Approval Gate
   - 明确停止在设计和计划阶段。
   - 最后向用户请求审核。
   - 未收到明确批准前，不得开始任何代码修改。

十、设计质量标准

- 每个模块必须能回答：做什么、如何调用、依赖什么、拥有何种状态、如何启动和关闭。
- 模块公开 API 应小而稳定，内部实现可以替换。
- 关键状态只有一个权威所有者和提交点。
- 依赖方向必须可静态检查或通过架构测试验证。
- 迁移期间任一阶段都应保持系统可运行。
- 计划必须建立在当前代码和测试事实之上，不把目标目录结构当成设计本身。
- 发现现有未提交改动时必须保护，不得覆盖；若与设计调查冲突，报告而不是清理。

现在只进行勘察、分析和设计。不要修改代码。完成上述输出后停止，等待用户批准。
```

## 提示词自审结果

- 没有未决的 `TBD`、`TODO` 或占位符。
- 当前范围仅包含设计和计划，不授权代码实施。
- AgentLoop 与 AgentRunner 都被纳入目标，但被拆分为连续里程碑，避免同时重写。
- 工作流只定义未来端口和选型标准，不引入依赖或实现。
- 明确保护现有未跟踪文件，不授权清理工作区。
- 验收要求基于行为契约、依赖方向和状态所有权，不依赖主观代码美学或单纯行数指标。
