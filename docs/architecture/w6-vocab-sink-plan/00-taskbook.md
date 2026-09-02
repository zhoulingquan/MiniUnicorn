# W6-1 词汇下沉收口(①RiskLevel 直连 + ③ledger 叶子)

> 状态:任务书就绪,待执行。
> 基线:HEAD 于 2026-09-02 核实,全量 **4135 passed / 0 failed / 29 skipped**,ruff check + format 双零。
> ruff 版本 0.15.21(与 uv.lock 一致),验证一律用 `.venv\Scripts\python.exe -m ruff`。

## 背景与目标

W5 后的架构评估(2026-09-01,程序化扫描)确认 "core + tool library" 形态已成立,但残留两笔词汇级反向债:

- **① tools 里 14 处 RiskLevel 仍经 `agent.safety_policy` re-export**(W5-0 已建好 `security/risk.py` 叶子,只差改 import 指向)
- **③ 底层库反向咬核心**:`providers/base.py`(2 处惰性)、`utils/evaluator.py`、`tools/deep_research/tool.py` 导入 `agent.call_ledger`;`utils/progress_events.py` 导入 `agent.hook`。守护测试 `test_dependency_direction.py` 只约束 session/channels→agent,providers/utils→agent 目前无规则,故存活至今

本批把这两笔债清零,并新增守护规则锁死成果。本批完成后,`providers/utils/security/config/bus` 及新 `ledger` 包对 `miniunicorn.agent.*` 的 import 为零。

## 一、W6-1a:① RiskLevel 直连(纯机械,14 处)

全部在 tools/ 下,把 `from miniunicorn.agent.safety_policy import RiskLevel` 改为 `from miniunicorn.security.risk import RiskLevel`:

| 文件 | 行 |
|---|---|
| tools/spawn.py | 8 |
| tools/cron.py | 9 |
| tools/cli_apps.py | 10 |
| tools/apply_patch.py | 12 |
| tools/exec_session.py | 12 |
| tools/filesystem.py | 10 |
| tools/delegate.py | 14 |
| tools/base.py | 14(**TYPE_CHECKING 块内,保持缩进**) |
| tools/create_agent.py | 18 |
| tools/execute_plan.py | 19 |
| tools/shell.py | 20 |
| tools/long_task.py | 23 |
| tools/activate_plan.py | 23 |
| tools/image_generation/tool.py | 37 |

不改 `agent/safety_policy.py`(它对 security.risk 的导入保持现状);不改其他 agent 导入(activate_plan 的 planner 导入、delegate/spawn 的 subagent 导入等是 W5 裁决过的合法词汇方向)。

**门(过即 commit)**:`rg "from miniunicorn.agent.safety_policy import" miniunicorn/tools/` 零输出;ruff check 零;`pytest tests/tools/ tests/security/ -q` 全过。
**Commit 1**: `refactor(tools): import RiskLevel directly from security.risk (14 sites)`

## 二、W6-1b:③ call_ledger/turn_budget 抽为顶层 ledger 包

### 2.1 新包结构(git mv 保历史)

```
miniunicorn/ledger/
  __init__.py        # re-export 公共 API(# noqa: F401)
  call_ledger.py     # git mv 自 miniunicorn/agent/call_ledger.py(286 loc)
  turn_budget.py     # git mv 自 miniunicorn/agent/turn_budget.py(127 loc,内容零改动)
```

- `call_ledger.py` 唯一内容改动:`from miniunicorn.agent.turn_budget import TurnBudget` → `from miniunicorn.ledger.turn_budget import TurnBudget`
- `__init__.py` re-export 9 个公共名:`CallPurpose, CallRecord, CallLedger, TurnBudget, current_call_ledger, bind_call_ledger, reset_call_ledger, call_purpose, allow_call_ledger_child_tasks`(风格参照 agent/memory.py 门面的 noqa 写法)
- **不留 agent 侧兼容 shim**(W5-1 先例:全量改指向后验证零残留,不留旧命名空间)

### 2.2 生产消费方改指向(14 文件 / 17 处)

统一改为 `from miniunicorn.ledger import ...`(多行括号导入保持原有符号清单):

| 文件 | 行 | 形态 |
|---|---|---|
| agent/agent_generator.py | 20 | 顶层 |
| agent/execution/planning.py | 21 | 顶层 |
| agent/execution/model_request.py | 20 | 顶层括号多行 |
| agent/loop.py | 299 | 方法内惰性(turn_budget) |
| agent/memory_consolidator.py | 13 | 顶层 |
| agent/memory_dream.py | 13 | 顶层 |
| agent/planner.py | 23 | 顶层 |
| agent/reflection.py | 25 | 顶层 |
| agent/runner.py | 14 | 顶层括号多行 |
| agent/step_acceptance.py | 251 | 函数内惰性 |
| agent/turn_orchestrator.py | 30 / 45 | 顶层 / TYPE_CHECKING(turn_budget) |
| providers/base.py | 632 / 690 | 函数内惰性 ×2 |
| tools/deep_research/tool.py | 21 | 顶层 |
| utils/evaluator.py | 13 | 顶层 |

另:agent/runner.py:114 的注释提及 `miniunicorn.agent.turn_budget` 路径,同步更新为 ledger 路径(仅注释,不动 `turn_budget: Any | None` 类型注解本身)。

### 2.3 测试改指向与搬家(4 文件)

| 文件 | 处理 |
|---|---|
| tests/agent/test_call_ledger.py | 搬至 **tests/ledger/**(新建目录,含 `__init__.py`,仿 tests/tools 约定);行 7、167、183、208、230、255、281、304、327、344、368、390、399、411、499、590、610 共 17 处改指向 |
| tests/agent/test_turn_call_ledger.py | 搬至 **tests/ledger/**;行 11、23 改指向 |
| tests/providers/test_call_ledger_integration.py | 原地;行 5 改指向 |
| tests/agent/test_upgrade_integration.py | 原地;行 30 改指向 |

### 2.4 progress_events 与 agent.hook 解耦(签名收窄)

- `utils/progress_events.py`:删除行 9 的 AgentHookContext 导入;`build_tool_event_finish_payloads(context: AgentHookContext)` 签名改为 `(tool_calls, tool_results, tool_events)` 三参数,函数体 `context.tool_calls/tool_results/tool_events` 相应去前缀(行为零变化)
- `agent/progress_hook.py:162`:调用处改传三参数(`context.tool_calls, context.tool_results, context.tool_events`)
- 全仓仅此一处调用方(已核实)

### 2.5 新增守护规则(锁死成果)

`tests/architecture/test_dependency_direction.py` 仿既有两条规则的 AST 扫描风格,新增:

**规则:providers / utils / security / config / bus / ledger 六包不得 import `miniunicorn.agent.*`(零豁免)**。本批修复后该断言天然成立;若出现违例即测试失败。

### 2.6 新增包测试

`tests/ledger/test_ledger_package_split.py`(仿 W5 的 tests/tools/test_tools_package_split.py):
1. 冷导入 `miniunicorn.ledger` 后 `sys.modules` 不含任何 `miniunicorn.agent` 模块
2. `__init__` re-export 9 个公共名完整(逐一 hasattr)
3. `miniunicorn.ledger.turn_budget` 可独立导入且不拉入 agent

### 2.7 文档登记

- `docs/architecture/module-boundaries.md`:仿 §2.6(bus)格式新增 ledger 小节登记模块职责与依赖方向;tools 的 RiskLevel 直连在对应小节补一句说明
- docs/superpowers/ 下的历史日期规格(2026-08-21 两处提及 agent.call_ledger)**不改**——历史记录保持原样,零残留核验范围仅 miniunicorn/ 与 tests/

## 三、红线

1. **纯机械 + 一处签名收窄**:除 2.4 明确的签名改动外,零逻辑改动、零测试断言改动、零删测试;call_ledger/turn_budget 搬家内容逐字节一致(除 2.1 指明的 import 行)
2. **git mv 保历史**:两个模块用 `git mv`,不许删了重建
3. 不动 pyproject.toml(ledger 是普通包,无 entry_points 变更)、不动 ruff 配置与版本
4. 测试一律 `.venv\Scripts\python.exe`(3.12);系统 Python 3.10 收集阶段即报 ImportError
5. 顺序:W6-1a 全部门过 → commit 1 → W6-1b → 全部门过 → commit 2(两批各自提交,互不搭车)
6. 若全量 pytest 出现非本批文件的失败:先单独重跑确认是否 flaky;真失败则停下报告,禁止为过测试改断言

## 四、验证门(W6-1b,全过才允许 commit 2)

1. `.venv\Scripts\python.exe -m pytest tests/ -q` → **0 failed / 29 skipped 不变;passed = 4135 + 本批新增守护规则与包测试数(约 +4,报告里写明确数)**;搬家测试计入其中
2. `.venv\Scripts\python.exe -m ruff check miniunicorn/ tests/` → 零输出
3. `.venv\Scripts\python.exe -m ruff format --check miniunicorn/ tests/` → 零输出
4. **零残留**:`rg "miniunicorn\.agent\.(call_ledger|turn_budget)" miniunicorn\ tests\` → 零输出(注释里的历史提及一并清掉)
5. `rg "from miniunicorn.agent.safety_policy import" miniunicorn/tools/` → 零输出
6. `git status` → 仅清单内路径(miniunicorn/ledger/ 新增、agent/ 与 tests/ 的既有文件改动、docs/architecture/module-boundaries.md、两个 commit 之间无越界文件)
7. 守护:`pytest tests/architecture/test_dependency_direction.py -q` 全过(含新规则)

## 五、Git 纪律

- 两个 commit:
  1. `refactor(tools): import RiskLevel directly from security.risk (14 sites)`
  2. `refactor(ledger): extract call_ledger and turn_budget into top-level ledger package`
- 只 add 清单内路径;绝不 `git add .`;验证门一过立即提交,先提交后写报告

## 六、中断防护(必守,lint 批已实证有效)

- 全量 pytest ~7-8 分钟:`Start-Process` 分离式后台启动并重定向日志,随后轮询
- **每次轮询命令必须互不相同**(带递增计数器/时间戳/变体):相同命令连续 5 次会被 mistake tracker 强制中止会话
- 单条 shell 命令 <25s
- 两个 commit 分别在各自门通过后立即提交,不攒批
