# W3 瘦身系列总览:loop.py 两大方法分段

> 状态:方案定稿,待分批实施(Cline)。
> 基线:W2 三批全部合并(HEAD `0262a60b`),`pytest tests/ -q` 4095 passed / 0 failed,ruff 零告警。
> 本方案文档以独立 docs commit 入库后,打 tag:`git tag -a baseline-pre-w3 -m "W2 收官基线,W3 瘦身系列起点"`。

## 一、背景

W2 收官后,遗留的最大架构文档是 `docs/architecture/agent-core-tool-library-split-plan.md`(Agent Core + Tool Library 拆分总规划)。该规划编写于 W1/W2 之前,其中多个阶段已被后续裁决覆盖:

- 阶段 2(ToolLoader 惰性化)——已由 **W1-1 LazyToolRegistry** 以更轻形式实现(register 不触发装载);
- 阶段 3(抽取 TurnController 类)——已在 **W2 裁决中否决**(10+ 字段构造的抽象税),以 W2-2 主循环分段方法化替代。

本方案对总规划**剩余活项**逐一对照当前代码实况(2026-08-31 核实)重新裁决,只保留与 `loop.py` 直接相关、可纯搬家完成的两个批次。

## 二、候选项裁决表(本方案的核心决策)

| 候选项 | 裁决 | 理由(基于 2026-08-31 代码核实) |
|---|---|---|
| `_run_agent_loop` 分段方法化 | **做(W3-2,大)** | loop.py 1346 行内最大方法(`_run_agent_loop`,1025-1256 约 232 行),混杂七类关注点:hook 装配、`_checkpoint` 闭包、**58 行 `_drain_pending` 嵌套闭包**(内含 subagent 阻塞等待语义)、scope/telemetry 绑定、agent_override 解析、goal 提示构建、spec 构建、遥测收尾。W2-2 模式可直接复刻 |
| `__init__` 装配分段方法化 | **做(W3-1,中,热身批)** | `__init__`(336-601 约 265 行)是线性装配巨方法,所有权不可见。分段后构造顺序与各层归属一目了然。顺序敏感(属性 setter 经 `_provider_registry` 路由)恰是纯搬家纪律的价值所在 |
| 组装下沉 composition(stale 阶段 4 主体:新增 agent_runtime.py、AgentLoop 降为纯门面) | **否决** | 与 W1-2 受保护的**注入-回退双路径**冲突:回退路径服务测试直连构造(`tests/agent/conftest.py` 的 `make_loop` 全量直连 `AgentLoop(**kwargs)`),下沉将迫使全部测试改造,高扰动低价值。W1-2 已提供组合根注入点(bus/dispatcher/session_turn/resources/turn_orchestrator/response/mcp_runtime/subagent_manager 均可注入),组合根所有权已实现,无需门面化 |
| 移除 CronService 持有 / SubagentManager 创建 / MCP 生命周期(stale 阶段 4 任务 3-6) | **否决** | 同上,回退分支是直连构造的语义依赖。McpLifecycleMixin(79 行)是 gateway/cli/webui/dispatch 的调用面;其委托属性(loop.py 626-656)是 `tools/mcp.py` RuntimeState 兼容面,W1-2 明确保留 |
| loop.py 死代码清理 | **已审计,无标的** | ruff F401/F402 零告警;`resolved_context_window_tokens`(webui/model_settings_api.py、command/builtin.py)、`_PENDING_USER_TURN_KEY`/`_RUNTIME_CHECKPOINT_KEY`(3 个测试文件)、`run_all_dreams`/`memory_for`(composition/gateway.py、command/memory.py 等)均有消费方 |
| memory.py 拆分(2018 行,stale 阶段 6) | **移出 W3,另立系列** | 体量大、记忆召回行为风险高,与 loop 瘦身互不重叠。待 W3 收官后按同法裁决 |
| Planning 模式删除(stale 阶段 7) | **否决** | 与受保护清单直接冲突:activate_plan 激活器语义、证据管道步骤推进、FAST→MANAGED escalation 均为 P1/W0 设计成果,非负债 |

## 三、批次索引

| 批次 | 文件 | 内容 | 规模 | 依赖 |
|---|---|---|---|---|
| W3-1 | 01-w3-1-init-phase-split.md | `AgentLoop.__init__` 装配分段方法化(8 个 `_init_*` 阶段方法) | 中 | baseline-pre-w3 |
| W3-2 | 02-w3-2-run-agent-loop-split.md | `_run_agent_loop` 分段方法化 + `_drain_pending` 闭包提取 + 回调绑定契约保持 | **大** | baseline-pre-w3,建议在 W3-1 之后 |

两批**互不重叠**(W3-1 只动 `__init__` 区段 336-601;W3-2 只动 `_run_agent_loop` 区段 1025-1256;两批各新增独立测试文件),但同文件修改,必须串行。建议顺序 **W3-1 → W3-2**(纯语句搬移热身、精细闭包提取殿后)。

## 四、全局约束(继承并扩充)

### 4.1 受保护清单(W0/W1/W2 成果,语义不得触碰)

1. 可信证据管道:ToolObservation / receipts / effective_evidence_level 判定 / observations_digest 缓存键 / verifier 熔断
2. 三态审批门(allow/deny/approval_callback)与 policy 传播
3. tool_blocked 审计 checkpoint 与 runtime_checkpoint 恢复链路(含 plan_snapshot 审计隔离)
4. LazyToolRegistry 的装载语义(register 不触发装载)
5. McpRuntime / SubagentManager 的组合根所有权(W1-2 的注入-回退双路径)
6. activate_plan 的激活器语义(挂载即返回,主循环唯一推进者)
7. verifier 异常 fail-closed 回退
8. W2-2 的主循环分段成果(`_run_with_ledger` 纯编排形态与其 phase 方法)

### 4.2 W3 特有红线

1. **本系列是纯搬家重构**:语义逐句对应(代码库既有惯例,见 runner.py"自 run 拆出"注释),不趁机修 bug、不改行为;全量测试必须零变化通过(不允许更新任何既有测试的断言,除非纯导入路径调整)
2. **W3-1 的顺序敏感性**:属性赋值顺序不得改变——`self.provider` / `self.model` / `self.context_window_tokens` 是 property setter,写入路由进 `_provider_registry`,必须先建 registry 再赋值;`self.__dict__["_session_turn"] = ...` 绕过 property 的写法逐字保留
3. **W3-2 的回调契约**:`injection_callback` / `checkpoint_callback` 以 `functools.partial` 绑定后,`inspect.signature` 解析出的参数集必须与原闭包一致(runner.py 438-449 依赖 `"limit" in signature.parameters` 判定)
4. 不引入新抽象:无新类、无新配置项、无新事件(`functools.partial` 绑定不算新抽象)
5. 定位用符号,行号是编写时参考值
6. 单批单 commit,禁止 `git add .`

## 五、验证门(每批统一)

```
.venv\Scripts\python.exe -m pytest tests/ -q        # 0 failed,passed >= 4104(新测试只增不减;W3-1 合并后基线)
.venv\Scripts\python.exe -m ruff check erza/  # 零输出
.venv\Scripts\python.exe -m ruff format --check erza/
```

注意:必须用项目 venv 的 Python 3.12(`.venv\Scripts\python.exe`),系统默认 Python 3.10 缺 `typing.Self` 会导致收集错误。全量测试约 8-13 分钟,后台运行重定向到临时文件后轮询,不要放弃全量。

## 六、实施方式

交由 Cline 分批实施,提示词模板与 W2 系列同构(见各批 `w3-N-cline-prompt.txt`)。每批完成以该批任务书"验收自检"清单为准。
