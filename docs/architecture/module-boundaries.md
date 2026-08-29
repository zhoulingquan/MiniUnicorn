# 模块边界与依赖规则(Phase 1 初稿)

- 依据:`docs/superpowers/specs/2026-08-19-modular-monolith-phase1-6-execution-plan.md` 步骤 1.5
- 阶段说明:本文件在 Phase 1 落地**规则声明**;机器检查(架构测试)在 Phase 6 新增
  (`tests/architecture/test_dependency_direction.py`),Phase 2 起按阶段细化各模块条目。
- 目标架构:`模块化单体 + 唯一、显式、静态的 Composition Root`。

---

## 1. 分层与依赖方向

```
┌─────────────────────────────────────────────────────────┐
│  composition(唯一知道一切的装配层)                         │
│  miniunicorn/composition/{__init__,gateway,agent_app}.py │
└──────────────────────────────┬──────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────┐
│  业务模块:agent / channels / session / providers / bus    │
│            / command / cron                              │
└──────────────────────────────┬──────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────┐
│  基础库:security / config / utils                        │
└─────────────────────────────────────────────────────────┘
```

- `composition` 只允许被入口(CLI `miniunicorn/cli/*`、SDK `miniunicorn/miniunicorn.py`、WebUI 边界)引用。
- `composition` 可引用任意业务模块与基础库(它是装配层,唯一知道所有模块如何组装的地方)。

### 1.1 禁止依赖清单(Phase 1 起即禁止)

| 禁止依赖 | 原因 | 现状 |
|---|---|---|
| 业务模块 `import miniunicorn.composition` | 避免业务代码自举装配、破坏单一组合根 | ✅ 现状无 |
| `session` import `agent` | 会话层不依赖代理执行细节 | ✅ 现状经 bus 解耦 |
| `channels` import `agent` | 通道层经 bus 解耦,不直接持有 agent | ✅ 现状无 |
| 模块间读取对方下划线私有属性 | 模块边界 = 公开 API 边界 | ⚠️ 已知例外见 §4 |
| 业务模块反向 import `cli/*`(除 `cli` 作为入口的调用方向) | 保持入口 → 业务单向 | ⚠️ 组合根内为测试兼容保留的 late-binding 例外,见 §4.1 |

### 1.2 例外声明(必须逐项登记)

- `miniunicorn/composition/agent_app.py` 与 `miniunicorn/composition/gateway.py` 在**调用时**
  经 `miniunicorn.cli.commands.<name>` 解析 `AgentLoop` / `sync_workspace_templates` /
  `_migrate_cron_store` 等名称。原因:现有测试在 `miniunicorn.cli.commands` 命名空间上 patch
  这些名字(late binding),且组合根被允许知道入口层。Phase 6 评估是否可收敛。
- `miniunicorn/composition/gateway.py` 从 `miniunicorn.cli._gateway_runner` 调用时导入
  `on_cron_job` / `_pick_heartbeat_target` / `_dream_backlog_total`。原因:cron 处理器与
  gateway 装配的历史同居一模块,Phase 1 只移动装配不动处理器;Phase 2 起评估迁移归属。
- `channels/websocket/_http_router.py` 与 `tools/mcp.py` 读写 `agent` 私有状态的历史耦合,
  由 Phase 3(收口私有访问)消化,不属 Phase 1 范围。

---

## 2. 模块登记表(勘察表初稿)

> 以下为 Phase 1 以代码勘察为据的初稿;Phase 2 起按 PR 逐项细化(公开 API / 状态 / 生命周期)。

### 2.1 composition

- 位置:`miniunicorn/composition/`
- 公开 API:
  - `build_agent_application(config, bus=None, cron_service=None, **overrides) -> AgentLoop`
  - `GatewayApplication` / `build_gateway_application(config, *, open_browser_url=…, webui_static_dist=…, webui_runtime_surface=…, webui_runtime_capabilities=…)`
  - `GatewayApplication.start()` / `await GatewayApplication.stop()`
- 拥有的状态:`GatewayApplication` 持有 `config / bus / session_manager / cron / agent / channels`
  以及运行期标志 `_need_dream_catchup`、`_ws_port`。
- 生命周期所有者:装配本身是唯一 owner;`start()` 运行,`stop()` 按逆序关闭。
- 说明:所有装配物在 `GatewayApplication.__init__` 内按既有顺序创建(bus → provider snapshot →
  session manager → cron → agent → MessageTool send 回调 → cron.on_job → channels → 系统任务)。

### 2.2 agent

- 位置:`miniunicorn/agent/`
- 公开 API(稳定契约):`AgentLoop`(`run` / `stop` / `process_direct` / `from_config` /
  `memory_for` / `set_model_preset` / `close_mcp` / `all_dreams` / `run_all_dreams` /
  `current_iteration` / `tool_names` / `llm_runtime` / `model_preset`),
  `AgentLoopBuilder.from_config` 与 `with_*` 全集,`AgentRunner.run(spec)`。
- 拥有的状态:消息调度(`_active_tasks` / `_background_tasks` / `_session_locks` /
  `_pending_queues` / `_concurrency_gate` / `_running`)——**Phase 2 起归
  `MessageDispatcher` 所有,AgentLoop 仅保留读写委托属性**;会话事务
  (`_webui_turns` / checkpoint 键)——归 `SessionTurnService`;运行时资源缓存
  (`_workspace_helpers_lock` / `_workspace_consolidators` / `_workspace_dreams`)
  ——归 `RuntimeResourceRegistry`;遥测(`_last_usage` / `_last_call_usage` /
  `_pending_turn_latency_ms`)——归 `ResponseAssembler`(AgentLoop 保留读写委托,
  `_current_iteration` 仍为 loop 自有)。
- 生命周期所有者:`composition`(Phase 2 起逐步迁往各应用服务)。
- 已知反向依赖(Phase 3 处理):`agent/context.py:45-46` 读 `_mcp_servers/_mcp_stacks`;
  `agent/tools/mcp.py:935` 写 `state._mcp_servers`;`agent/tools/runtime_state.py` Protocol。

### 2.3 channels

- 位置:`miniunicorn/channels/`
- 公开 API:`ChannelManager(config, bus, *, session_manager=…, webui_* …)`,
  `start_all()` / `stop_all()`,`enabled_channels`,各 channel 的 `BaseChannel` 协议。
- 拥有的状态:`channels` 映射、`_dispatch_task`、`_background_tasks`、
  `_origin_reply_fingerprints`。
- 生命周期所有者:`composition`(gateway 装配时创建,stop 阶段调用 `stop_all`)。

### 2.4 session

- 位置:`miniunicorn/session/`
- 公开 API:`SessionManager(workspace)`(get_or_create / save / flush_all / list_sessions /
  invalidate 等),`Session` 模型。
- 拥有的状态:内存会话缓存、JSONL 文件(持久化格式为兼容契约,不得改动)。
- 生命周期所有者:`composition`(创建);`flush_all()` 由 gateway 逆序关闭最后执行。
- 写入点现状(Phase 6 收口候选):`loop` / `_state_machine` / `memory`(Consolidator)/
  `command/builtin` / `tools/long_task` / `webui` / `gateway(_deliver_to_channel)`。

### 2.5 providers

- 位置:`miniunicorn/providers/`
- 公开 API:`build_provider_snapshot(config)`, `load_provider_snapshot(config_path=None)`,
  `make_provider(config)`, `ProviderSnapshot`, `LLMProvider`。
- 拥有的状态:provider/model/context_window 的选择与构建结果(快照)。
- 生命周期所有者:`composition`(gateway 预构建快照并注入 agent;运行时热切换由
  agent 的 `_provider_snapshot_loader` 驱动)。

### 2.6 bus

- 位置:`miniunicorn/bus/`
- 公开 API:`MessageBus`(publish_inbound / consume_inbound / publish_outbound /
  consume_outbound),事件 `InboundMessage` / `OutboundMessage`。
- 拥有的状态:`inbound` / `outbound` 两个 `asyncio.Queue`(容量 1000,背压)。
- 生命周期所有者:`composition`;bus 是 agent 与 channels 的关键控制流通道
  (inbound 唯一消费者 `loop.py:952`)。

### 2.7 command

- 位置:`miniunicorn/command/`
- 公开 API:`CommandRouter`,内置命令注册(`register_builtin_commands`),`CommandContext`,
  `CommandApplicationService`(命令应用服务,见 §2.12)。
- 拥有的状态:命令路由表(注册在内置命令模块);`CommandApplicationService` 持有
  `bus` + `router` 两个协作对象,无独立可变状态。
- 生命周期所有者:`agent`(AgentLoop 构造时创建 router 并注册内置命令,再以
  router + bus 构造应用服务注入 dispatcher)。

### 2.8 cron

- 位置:`miniunicorn/cron/`
- 公开 API:`CronService(store_path)`,`on_job`(可赋值回调),`start()` / `stop()` /
  `await_stop()`, `register_system_job(job)`, `status()`,作业持久化(store JSON)。
- 拥有的状态:作业存储(workspace `cron/jobs.json`)、`_timer_task` / `_exec_tasks`、
  `on_job` 回调。
- 生命周期所有者:`composition`(gateway 装配创建、`start()` 启动、逆序 `stop()/await_stop()`)。

### 2.9 security / config / utils(基础库)

- `security`:权限、SSRF、脚本安全等策略;只读被业务模块引用。
- `config`:`Config` 模式、加载器(`load_config` / `resolve_config_env_vars` /
  `set_config_path`)、路径(`config/paths.py`);状态 = 进程内活动配置上下文。
- `utils`:`helpers` / `restart` 等纯工具,不拥有业务状态。
- 生命周期所有者:无独立生命周期;由 `composition` 及入口层按需解析。

### 2.10 agent/dispatch(MessageDispatcher,PR-2a)

- 位置:`miniunicorn/agent/dispatch.py`
- 公开 API:`MessageDispatcher(agent, bus, commands=None)`;`run()`(消费循环 +
  优先级命令分支)、`stop()`、`_dispatch(msg)`、`_dispatch_command_inline(...)`(兼容委托)、
  `_effective_session_key(msg)`、`_schedule_background(coro)`、
  `_process_system_message(...)`;只读状态属性 `_running` / `_active_tasks` /
  `_background_tasks` / `_session_locks` / `_pending_queues` / `_concurrency_gate`。
- 自有状态:运行标志、per-session 活跃任务与弱引用锁、mid-turn pending 队列、
  后台任务集合、全局并发信号量(`MINIUNICORN_MAX_CONCURRENT_REQUESTS`)。
- 生命周期:与宿主 AgentLoop 同生同灭;`stop()` 停循环;`/stop` 取消任务走
  `_cancel_active_tasks`。
- 依赖方向:持有 `DispatchHost` 协议(显式小接口,当前实现为 AgentLoop)、
  `MessageBus`、`CommandApplicationService`(经显式构造参数);不 import 业务上层。

### 2.11 agent/session_turn(SessionTurnService,PR-2b)

- 位置:`miniunicorn/agent/session_turn.py`
- 公开 API:`SessionTurnService(sessions, *, workspace=None, webui_turns=None,
  max_tool_result_chars=None)`;`_persist_user_message_early` / `_save_turn` /
  `_sanitize_persisted_blocks` / `_persist_subagent_followup` / checkpoint 族
  (`_set/_clear/_restore_runtime_checkpoint`、`_mark/_clear/_restore_pending_user_turn`)。
- 自有状态:无缓存状态;仅协作引用(`sessions` / `workspace` / `webui_turns` /
  `max_tool_result_chars`)与常量 `_RUNTIME_CHECKPOINT_KEY` / `_PENDING_USER_TURN_KEY`。
- 生命周期:与 AgentLoop 同生;会话/检查点数据持久化在 SessionManager(JSONL 格式为
  兼容契约,不可改动)。
- 依赖方向:依赖 `session`(SessionManager)、`utils`;不 import `loop`。
- 范围声明:本阶段只收口 loop 与 state machine 两处 `sessions.save` 写点;memory /
  command / tools / webui / gateway 的写点保留,列入 Phase 6 收口清单。

### 2.12 command/service(CommandApplicationService,PR-2d)

- 位置:`miniunicorn/command/service.py`
- 公开 API:`CommandApplicationService(bus, router)`;`is_priority_command(raw)` /
  `is_dispatchable_command(raw)` / `dispatch_priority_inline(host, msg, key, raw)` /
  `dispatch_inline(host, msg, key, raw)`。
- 自有状态:无可变状态;仅持有 `bus`(结果发布)与 `router`(路由表)引用。
- 生命周期:由 AgentLoop 构造时创建,注入 `MessageDispatcher`;与 loop 同生。
- 依赖方向:依赖 `bus`、`command/router`;宿主 loop 经每次调用的 `host` 参数传入
  (`CommandContext.loop`),服务本身不持 AgentLoop。
- 命令分类(注释声明):运行时命令(`/stop`、`/restart`,priority 层,进入派发锁前
  就地执行)、会话命令(`/new`、`/model`、`/history`、`/goal`、`/dream` 等,exact/prefix
  层,锁内执行)、查询命令(`/status`、`/help`)。CommandRouter/builtin 行为逐字节不变。

### 2.13 agent/runtime_resources(RuntimeResourceRegistry,PR-2c)

- 位置:`miniunicorn/agent/runtime_resources.py`
- 公开 API:`RuntimeResourceRegistry(workspace, *, context, workspace_scopes, sessions,
  provider, model, context_window_tokens, tools, max_completion_tokens, ...)`;
  `memory_for(workspace=None)` / `_consolidator_for(workspace)` / `_dream_for(workspace)` /
  `all_dreams()` / `run_all_dreams()` / `_consolidator_for_session(key)` /
  `_sync_runtime_helpers(...)` / `shutdown()`;只读属性 `consolidator` / `auto_compact` /
  `dream` / `dream_idle_trigger`。
- 自有状态:`_default_root`、`_workspace_helpers_lock`、`_workspace_consolidators`、
  `_workspace_dreams`;默认 `consolidator` / `dream` / `auto_compact` /
  `dream_idle_trigger` 实例。
- 生命周期:`shutdown()` 停止 idle dream 触发;由 Phase 1 组合根在逆序关闭中调用。
- 依赖方向:依赖 `agent/context`、`agent/memory`、`agent/autocompact`、
  `agent/dream_trigger`、`security/workspace_access`、`session`;不 import `loop`。

### 2.14 agent/response(ResponseAssembler,PR-2e)

- 位置:`miniunicorn/agent/response.py`
- 公开 API:`ResponseAssembler(bus, tools)`;`_assemble_outbound(...)`、
  `build_stream_closures(msg)`(on_stream/on_stream_end 闭包)、
  `record_last_usage(result)`、`record_pending_turn_latency(key, latency_ms)`、
  `pop_pending_turn_latency(key)`;状态属性 `_last_usage` / `_last_call_usage` /
  `_pending_turn_latency_ms`。
- 自有状态:遥测三件套(跨会话共享,见 Phase 6 决策项注释);无其它缓存。
- 生命周期:与 AgentLoop 同生;AgentLoop 保留读写委托属性以便命令/工具/测试继续读取。
- 依赖方向:依赖 `bus`(发布流式/最终消息)、`agent/tools`(MessageTool 抑制判定);
  不 import `loop`。
- 独立决策项(不顺带改):`_last_usage` / `_last_call_usage` 为跨会话共享可变遥测,
  与并发模型冲突;本阶段仅迁移写入位置不改语义,Phase 6 改为 per-turn 上下文数据
  (webui turn_end 事件字段不变)。

### 2.15 agent/safety(SafetyPolicy)

- 位置:`miniunicorn/agent/safety_policy.py`
- 公开 API:`SafetyPolicy`(class),`RiskLevel`(enum: `LOW` / `MEDIUM` / `HIGH` / `CRITICAL`),`assess_risk(tool_name, args, context) -> RiskLevel`,`should_block(risk: RiskLevel) -> bool`,`should_require_approval(risk: RiskLevel) -> bool`。
- 拥有的状态:风险规则表(内置,可通过配置扩展)、审批回调注册表。
- 生命周期所有者:`composition`(构建 `AgentLoop` 时注入 `AgentRunner`)。
- 依赖方向:依赖 `config`(读取安全策略配置)、`security/workspace_access`(路径越权判定);不 import `agent/loop`、`agent/execution`。
- 边界说明:风险分级**独立于规划复杂度**。`SafetyPolicy` 仅根据工具名、参数、工作区上下文评估单次调用的风险等级,不关心当前是 FAST 还是 MANAGED 模式,也不参与 `usePlanner`/升级决策。规划层在生成计划时可调用 `assess_risk` 做前置过滤,但最终拦截/审批在工具执行入口(`ToolExecutionCoordinator.execute_tools`)统一执行,保证策略单一入口。

### 2.16 agent/planning(PlanningPolicy)

- 位置:`miniunicorn/agent/planning_policy.py`
- 公开 API:`PlanningPolicy`(class),`ExecutionMode`(enum: `FAST` / `MANAGED`),`select_mode(config, turn_context) -> ExecutionMode`,`should_upgrade(turn_context) -> bool`,`build_upgrade_context(turn_context) -> UpgradeContext`。
- 拥有的状态:模式选择规则(含运行时升级判定)、`max_replans` 计数器(仅 MANAGED 模式生效)。
- 生命周期所有者:`composition`(经 `AgentRunner` 持有,`PlanningReflectionService` 回调使用)。
- 依赖方向:依赖 `config`(读取 `usePlanner`、`plannerMaxReplans`、分层预算等)、`agent/execution/recovery.py`(复用空响应重试计数);不 import `agent/loop`、具体 provider。
- 边界说明:
  1. **FAST/MANAGED 选择**: `usePlanner=true` 直接进入 MANAGED; `false` 走 FAST,但受运行时升级规则影响。
  2. **运行时升级**: 连续 **2 个 turn** 无工具响应(模型直接产出文本) → 下一 turn 自动升级为 MANAGED;升级**每 turn 至多一次**,MANAGED turn 结束后重置,下一 turn 重新评估。此规则在 `PlanningPolicy.should_upgrade` 实现,由 `TurnOrchestrator` 在状态机进入前调用。
  3. **与 SafetyPolicy 解耦**: 升级判定不读取风险等级;风险拦截在工具执行层,规划层只管模式切换与重规划预算。

### 2.17 agent/tools/execute_plan(ExecutePlanTool)

- 位置:`miniunicorn/agent/tools/execute_plan.py`
- 公开 API:`ExecutePlanTool`(class,继承 `Tool` 与 `ContextAware`),注册工具名 `delegate_plan`(别名 `execute_plan`);核心方法 `execute(plan, execution) -> str`,`create(ctx)` 经 `ctx.subagent_manager` 注入管理器。
- 拥有的状态:请求上下文 ContextVar(origin_channel / origin_chat_id / session_key / origin_message_id)与 subagent 管理器引用;无跨请求业务状态。
- 生命周期:随工具注册表构建;作用域 `_scopes = {"core"}`,不对 subagent 开放(避免递归)。
- 依赖方向:依赖 `agent/subagent.py`(`SubagentManager.spawn_and_wait`)、`security/workspace_access`(工作区范围透传)、`safety_policy`(`RiskLevel.HIGH`);不 import `agent/loop`。
- 语义说明:将计划步骤委派给并行 subagent——无数据依赖的步骤走 **parallel** 模式并发执行(每步一个 subagent),有依赖的步骤走 **serial** 模式,上一步结果以 sandbox 标记包裹后作为下一步上下文传入(防 prompt injection)。默认 `auto`:步骤动作引用先前输出则串行,否则并行。

---

## 3. 生命周期与启动/关闭顺序(gateway)

启动(严格保持 `_run_gateway` 原顺序,`GatewayApplication.__init__` → `start()`):

1. `MessageBus()`
2. `build_provider_snapshot(config)`(ValueError 降级为 None + 警告输出)
3. `SessionManager(config.workspace_path)`
4. `CronService(config.workspace_path / "cron" / "jobs.json")`
5. `AgentLoop.from_config(…, provider_snapshot_loader=load_provider_snapshot, …)`
6. MessageTool `set_send_callback(_deliver_to_channel)`
7. `cron.on_job = <wrapper>`
8. `ChannelManager(…, webui_cron_service=cron, webui_tool_registry=agent.tools, …)`
9. Dream / heartbeat 系统任务注册(`cron.register_system_job`)
10. `start()`:`cron.start()` → `agent.run()` + `channels.start_all()`(+ 可选 browser 打开)

关闭(`stop()` 逆序,顺序与旧清理代码一致,不优化):

1. `await agent.close_mcp()`
2. `cron.stop()` + `await cron.await_stop()`
3. `agent.stop()`
4. `await channels.stop_all()`
5. `agent.sessions.flush_all()`
6. 任一清理步骤异常不跳过后续步骤;首个异常在最后重抛(保留取消语义)。

---

## 4. 已知下划线私有访问清单(Phase 3 收口前为事实豁免)

| 位置 | 读取/写入 | 目标 | 计划 |
|---|---|---|---|
| `agent/context.py:45-46` | 读 | `_mcp_servers` / `_mcp_stacks` | Phase 3 `RuntimeStateView` |
| `agent/tools/mcp.py:935` | 写 | `state._mcp_servers` | Phase 3 setter |
| `agent/tools/runtime_state.py:6-59` | 读 | Protocol 私有属性 | Phase 3 |
| `agent/_provider_switching.py:60` | 写 | `self.runner.provider` | Phase 4 ProviderRegistry |
| `agent/_mcp_lifecycle.py:27-31` | 读 | 宿主 AgentLoop 属性 | Phase 3/4 |
| `agent/_state_machine.py` | 读 | 宿主 AgentLoop 属性(约 30 项) | Phase 3 依赖束 |
| `agent/dispatch.py` | 读 | 宿主 `_agent._session_turn/_response/_resources/_webui_turns/_unified_session/_max_messages/_last_call_usage` | 已收敛为显式 `DispatchHost` 协议(小接口),宿主私有读逐步并入 Phase 3 依赖束 |

> 注:Phase 2 抽离的服务(dispatch / session_turn / runtime_resources / command service /
> response)优先经**显式构造参数**取得协作对象(bus / sessions / workspace / tools /
> router),仅 dispatcher 因历史路径保留对宿主的私有读(已声明为 `DispatchHost` 协议)。

---

## 5. 验收对照

- [x] `miniunicorn/composition/` 存在且为唯一装配点
- [x] 四入口(gateway / agent / serve / SDK)均经组合根
- [x] 组装冒烟测试(5 例)通过
- [x] 本文件落地
- [x] 未触碰 `.trae-html-share-packages/`、`arch-eval-miniunicorn-vs-aniyaa/`

## 6. Phase 2 验收对照(PR-2a…2e)

- [x] 五个新模块条目已登记:dispatch(§2.10)、session_turn(§2.11)、
  command service(§2.12)、runtime_resources(§2.13)、response(§2.14),均含
  公开 API / 自有状态 / 生命周期 / 依赖方向
- [x] `CommandApplicationService` / `ResponseAssembler` 落地,dispatcher 经显式
  构造参数持有命令服务;`rg "AgentLoop" command/service.py agent/response.py` 为 0
- [x] CommandRouter/builtin 行为与输出逐字节不变;流式回调签名不变
- [x] `_last_usage`/`_last_call_usage`/`_pending_turn_latency_ms` 收尾写入迁入
  `ResponseAssembler`,语义不变,已标注 Phase 6 决策项
- [x] 全量回归通过数 ≥ 基线(3728 passed,唯一可接受失败
  `test_deep_research.py::test_full_workflow_success` 网络 flake)

---

## 7. Phase 3 依赖束(turn_orchestrator / PR-3a + PR-3b)

### 7.1 状态机归属

- 位置:`miniunicorn/agent/turn_orchestrator.py`
- 公开 API:`TurnOrchestrator(deps: TurnDeps)`、`process_turn(...)`(原
  `_process_message` 驱动循环)、`TurnDeps`(显式依赖束 dataclass)、
  `StateMixin`(薄委托)、`TurnState` / `TurnContext` / `StateTraceEntry` /
  `_TRANSITIONS`(原样迁移)。
- `loop.py` 经 `StateMixin` 多继承保留 `_state_restore`…`_state_respond` /
  `_prepare_message_media` 表面;`_process_message` 仅委托给
  `self._turn_orchestrator.process_turn(...)`(签名不变,`DispatchHost` 协议不变)。
- 兼容层:`miniunicorn/agent/_state_machine.py` 保留为 re-export 模块
  (导出 `StateMixin` / `StateTraceEntry` / `TurnContext` / `TurnState` /
  `extract_documents`),供既有测试导入;`rg "_state_machine" miniunicorn/` 为 0
  (re-export 模块内容不含该字面量,生产代码一律从 `turn_orchestrator` 导入)。
- `test_document_extraction_toggle.py` 的 patch 目标迁移为
  `miniunicorn.agent.turn_orchestrator.extract_documents`(机械搬家,名称查找按
  模块 globals)。

### 7.2 TurnDeps 字段与来源

| TurnDeps 字段 | 类型 | 来源(loop.py 装配) |
|---|---|---|
| `session_turn` | `SessionTurnService` | `self._session_turn` |
| `resources` | `RuntimeResourceRegistry` | `self._resources` |
| `response` | `ResponseAssembler` | `self._response` |
| `runner` | `AgentRunner` | `self.runner` |
| `tools` | `ToolRegistry` | `self.tools` |
| `context_builder` | `ContextBuilder` | `self.context` |
| `commands` | `Callable[msg, session, key, raw] → outbound` | `self._dispatch_command_for_turn`(在 loop 内构造 `CommandContext(loop=self)`) |
| `webui_turns` | `WebuiTurnCoordinator` | `self._webui_turns` |
| `sessions` | `SessionManager` | `self.sessions` |
| `channels_config` | `ChannelsConfig \| None` | `self.channels_config` |
| `max_messages` | `int` | `self._max_messages` |
| `run_agent_loop` | 可调用 | 晚绑定 `lambda: self._run_agent_loop` |
| `build_bus_progress_callback` | 可调用 | 晚绑定 `lambda: self._build_bus_progress_callback` |
| `build_retry_wait_callback` | 可调用 | 晚绑定 `lambda: self._build_retry_wait_callback` |
| `assemble_outbound` | 可调用 | `self._assemble_outbound` |
| `schedule_background` | 可调用 | 晚绑定 `lambda: self._schedule_background` |
| `set_tool_context` | 可调用 | `self._set_tool_context` |
| `build_initial_messages` | 可调用 | `self._build_initial_messages` |
| `replay_token_budget` | 可调用 | `self._replay_token_budget` |
| `llm_runtime` | 可调用 | `self.llm_runtime` |
| `refresh_provider_snapshot` | 可调用 | `self._refresh_provider_snapshot` |
| `resolve_agent_override` | 可调用 | `self._resolve_agent_override` |
| `process_system_message` | 可调用 | `self._process_system_message` |
| `build_turn_budget` | `Callable[[], TurnBudget \| None]` | `self._build_turn_budget` |

> 晚绑定说明:测试在构造后替换 `loop._run_agent_loop` / `loop._schedule_background` /
> `loop.context.build_messages` 等,故所有可调用字段均在调用时经 lambda / 方法体
> 解析宿主属性,构造期绑定会导致 monkeypatch 失效。

### 7.3 宿主私有访问 → 依赖束映射(约 30 项)

| 状态处理函数 | 原宿主访问 | 新归属 |
|---|---|---|
| `_state_restore` | `self._prepare_message_media` | orchestrator 方法(模块级 `extract_documents` / `reference_non_image_attachments`) |
| | `self.sessions.get_or_create` / `save` | `deps.sessions` |
| | `self.workspace_scopes.persist_message_scope` | `deps.resources.workspace_scopes` |
| | `self._restore_runtime_checkpoint` / `_restore_pending_user_turn` | `deps.session_turn.*` |
| `_prepare_message_media` | `self._should_extract_document_text` | orchestrator 方法 |
| `_should_extract_document_text` | `self.channels_config` | `deps.channels_config` |
| `_state_compact` | `self.auto_compact` | `deps.resources.auto_compact` |
| `_state_command` | `self.commands.dispatch`(CommandContext.loop) | `deps.commands`(晚绑定,loop 内构造 `CommandContext(loop=self)`) |
| | `self._persist_user_message_early` / `_clear_pending_user_turn` | `deps.session_turn.*` |
| | `self.sessions.save` | `deps.sessions` |
| `_state_build` | `self._turn_scope` | orchestrator 方法(`deps.resources.workspace_scopes.for_turn`) |
| | `self._consolidator_for` | `deps.resources._consolidator_for` |
| | `self._max_messages` | `deps.max_messages` |
| | `self._set_tool_context` | `deps.set_tool_context` |
| | `self.tools.get` | `deps.tools` |
| | `self._replay_token_budget` | `deps.replay_token_budget` |
| | `self._webui_turns.capture_title_context` | `deps.webui_turns` |
| | `self.llm_runtime()` | `deps.llm_runtime` |
| | `self._build_initial_messages` | `deps.build_initial_messages` |
| | `self._persist_user_message_early` | `deps.session_turn` |
| | `self._build_bus_progress_callback` / `_build_retry_wait_callback` | `deps.build_bus_progress_callback` / `deps.build_retry_wait_callback` |
| `_state_run` | `self._webui_turns.publish_run_status` | `deps.webui_turns` |
| | `self._run_agent_loop` | `deps.run_agent_loop`(晚绑定) |
| `_state_save` | `self._save_turn` / `_clear_pending_user_turn` / `_clear_runtime_checkpoint` | `deps.session_turn.*` |
| | `self._response.record_pending_turn_latency` | `deps.response` |
| | `self._turn_scope` / `self.memory_for` / `self._consolidator_for` | orchestrator 方法 / `deps.resources.memory_for` / `deps.resources._consolidator_for` |
| | `self.sessions.save` | `deps.sessions` |
| | `self._schedule_background` | `deps.schedule_background`(晚绑定) |
| | `self._max_messages` | `deps.max_messages` |
| `_state_respond` | `self._assemble_outbound` | `deps.assemble_outbound` |
| `process_turn`(驱动) | `self._refresh_provider_snapshot` | `deps.refresh_provider_snapshot` |
| | `self._process_system_message` | `deps.process_system_message` |
| | `self._resolve_agent_override` | `deps.resolve_agent_override` |
| | `self._TRANSITIONS` | orchestrator 自有(loop 不再定义) |

### 7.4 收口私有访问(PR-3b)

| 位置 | 原访问 | 收口 |
|---|---|---|
| `agent/context.py:45-46` | 读 `state._mcp_servers` / `state._mcp_stacks` | `runtime_lines(state: RuntimeStateView, …)`;新模块 `agent/runtime_view.py` 定义只读协议 `RuntimeStateView`(`mcp_servers` / `mcp_stacks` 只读属性);`AgentLoop` 新增同名只读 property |
| `agent/tools/mcp.py:935` | 写 `state._mcp_servers = next_servers` | `state._set_mcp_servers(next_servers)`;`RuntimeState` Protocol 增补该方法;实现位于 `agent/_mcp_lifecycle.py`(`McpLifecycleMixin._set_mcp_servers`) |
| `agent/_state_machine.py` | 约 30 项宿主读 | 整模块迁入 `turn_orchestrator.py`(§7.3),re-export 兼容 |
| `agent/loop.py` `_prepare_message_media` 在 `_drain_pending` 的使用 | 宿主方法 | 保留 `StateMixin._prepare_message_media` 委托,`_run_agent_loop` 调用点不变 |

### 7.5 Phase 3 验收对照

- [x] 四验收测试全绿(`test_stop_preserves_context` / `test_loop_save_turn` /
  `test_runner_injections` / `test_document_extraction_toggle`)+
  `test_workspace_memory_routing`
- [x] `rg "_state_machine" miniunicorn/` 为 0(仅保留无字面量的 re-export 模块)
- [x] `rg "AgentLoop" miniunicorn/agent/turn_orchestrator.py` 为 0
- [x] 定向回归(tests/agent + tests/session + tests/command)通过
- [x] 未改动 Phase 1/2 行为与持久化格式;未加第三方依赖


## 8. Phase 4–6 模块登记(终稿)

### 8.1 agent/execution/(PR-5a/5b/5c)

从 `AgentRunner`(1882→1201 行)拆出的五个执行服务。构造遵循**宿主模式**(服务持 runner 引用仅作协作入口,不写宿主私有态;`planning.py` 等经 `self._runner._xxx` 的剩余调用均为测试 monkeypatch 兼容的薄委托反向调用,已逐项登记于豁免清单 §4)。

| 文件 | 类 | 公开 API | 状态所有权 |
|---|---|---|---|
| `execution/model_request.py` | `ModelRequestExecutor` | `request_model` / `build_request_kwargs` / `usage` 合并 | 无(纯函数式,usage 由调用方持有) |
| `execution/tool_execution.py` | `ToolExecutionCoordinator` | `execute_tools` / `run_tool` / `normalize_tool_result` / `partition_tool_batches` | 无 |
| `execution/context_governance.py` | `ContextGovernanceService` | `govern_messages` / `apply_tool_result_budget` / `snip_history` | 无;`runner_strategies` 经 `runner._context_governance` 回调(插件契约 `ContextStrategy` 不变) |
| `execution/recovery.py` | `TurnRecoveryPolicy` | 空响应重试 / 长度恢复 / max-iterations 终结 / fatal 工具错误 / drain | 常量 `_MAX_EMPTY_RETRIES` / `_MAX_LENGTH_RECOVERIES` / `_SNIP_SAFETY_BUFFER`(runner.py 保留 noqa re-export 供测试导入) |
| `execution/planning.py` | `PlanningReflectionService` | 计划步引导 / 周期反思触发 | `_reflection_tasks` |

### 8.2 agent/providers/(Phase 4)

`ProviderRegistry`:LLM provider 构建与快照的唯一权威;`AgentLoop` 构造 33 参 → 10 参 + `AgentServices` 服务束,loop.py 1706→1207 行,退化为兼容 Facade(`_gateway_runner.py` 与 SDK 零改动)。

### 8.3 session/writes.py(Phase 6)

`SessionWriteService`(`SessionManager.writes` property):全部外部会话写点(command/tools/webui/gateway)经 `persist` / `record_message` 收敛;声明的例外:`agent.memory` Consolidator 直写(遗留,登记于豁免清单)。

### 8.4 架构守卫(Phase 6)

`tests/architecture/test_dependency_direction.py`(纯 `ast`,零第三方依赖):
- A. 业务模块禁 import `miniunicorn.composition`(入口豁免);
- B. `session` / `channels` 禁 import `agent`(单向依赖);
- C. 跨顶层包下划线私有访问禁令 + 显式豁免清单(每项须随耦合解除移除)。

遥测收口:`tests/agent/test_turn_end_fields.py` 锁定 `turn_end` 出站消息字段集。

### 8.5 Phase 6 验收对照

- [x] 守卫测试 6 项全绿(A/B/C 规则 + 豁免登记)
- [x] `turn_end` 字段锁定测试 3 项全绿
- [x] Session 写点收敛至 `SessionWriteService`(例外已声明)
- [x] mixin 三文件(`_state_machine` / `_mcp_lifecycle` / `_provider_switching`)保留 re-export——测试仍 import,处置依据 Phase 3 验收记录
- [x] import-linter 与 codex 移植:按方案纪律独立决策,未实施

### 8.6 Lean ReAct Kernel P0

- `AgentLoopConfig` 是执行策略配置的单一传播入口；`usePlanner=false` 保持 FAST
  ReAct 默认路径，`usePlanner=true` 是全局 managed opt-in，不包含复杂度分类器或
  FAST→MANAGED 动态升级。
- `turn_orchestrator.py` 在状态机进入前创建并绑定一个 context-local
  `CallLedger`；`AgentRunner.run()` 复用活动 ledger，直接调用时才建立独立 ledger。
  因此并发 turn/runner 不共享计数，异常与取消均由 async context manager 恢复上下文。
- provider 的 `chat_with_retry` / `chat_stream_with_retry` 是唯一计账边界：一次逻辑
  调用在重试完成后只记录最终响应一次。planner、replan、executor、reflection、
  compact/memory、tool 与 finalization 通过 `CallPurpose` 标注，全部进入同一 turn
  budget。
- 配置 USD 上限时采用 fail-closed 语义：provider 未报告 `cost_usd` 且没有该模型
  pricing 时，budget 返回 `cost_tracking_unavailable`，不会把未知成本静默当作 0。
- ledger 绑定到创建 turn 的 asyncio task；普通子任务继承到的 ContextVar 不获得父
  ledger 写权限。仅 Python 3.11 `wait_for` 包装和 periodic reflection 可显式继承写
  权，且只在父 binding 仍活动时有效。subagent runner 会建立自己的 ledger，迟到的
  fire-and-forget reflection 与 post-turn consolidation 不会污染已完成 usage 快照。
- `Planner.create_plan()` / `replan()` 返回 `PlannerResult`。只有 `VALID` 结果进入
  managed 执行；缺失/非法 JSON、无有效 steps 或 provider 异常返回带稳定 error code
  的 `FALLBACK`，执行退回 FAST。replan 精确允许 `max_replans` 次 provider 尝试，
  有效替换计划继承计数并前置已完成历史；耗尽仍以 `plan_failed` 终止。
- `ContextGovernor` 从 `BUILTIN_PIPELINE` 逐项实例化内置策略，包括 snip 后重复的
  orphan/backfill 清理；仅插件名称与内置名称去重。
- P1/P2 明确延期：不新增任务复杂度 router、classifier LLM call、verifier LLM
  call 或其它自适应路由策略。

