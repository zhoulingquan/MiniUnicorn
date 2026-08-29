# MiniUnicorn Agent Harness 评估报告 — 独立复核结论

**复核日期：** 2026-08-25
**复核基线：** `HEAD 035770e3b1d1ae02586e8cf540a6662278e36899`（与原报告一致，已验证）
**复核对象：** [agent-harness-evaluation-report-2026-08-25.md](./agent-harness-evaluation-report-2026-08-25.md) 第 9 节任务清单全部 8 项
**复核方式：** 静态调用链追踪 + 最小失败测试 + 隔离环境（独立 HOME/USERPROFILE/MINIUNICORN_HOME/TEMP）分批测试重跑 + ruff 重跑

---

## 0. 复核总览

| # | 原报告待复核问题 | 复核结论 |
|---:|---|---|
| 1 | F-001 执行前审批缺失 | **成立（确认为真实 P0 缺陷）**，调用链完整闭合，无任何绕过障碍——因为本来就没有拦截 |
| 2 | F-002 step acceptance 误接受 | **成立且比原报告更严重**：LLM verifier 对规则误接受完全短路，无法纠正 |
| 3 | 默认值 fail-closed | 混合：workspace 限制 fail-closed ✓；shell 沙箱 **fail-open**；API 认证 **fail-open（本地）** |
| 4 | 子 Agent 继承 | workspace/sandbox/模型/迭代/递归深度继承 ✓；**turn budget 不继承（预算逃逸）** |
| 5 | 测试 failed/error 归因 | **环境与测试设计问题为主**；已定位全量挂死精确根因；真实代码缺陷远少于 878 |
| 6 | retry/fallback 重复副作用 | 无重复记账、无工具重复执行；存在失败尝试 usage **漏记**（预算低估） |
| 7 | 文档旧路径 | **纯文档漂移**，无兼容模块；"别名兼容"的登记描述失真 |
| 8 | 重新评分 | 核心层 4.0 / 平台层 3.4 / 生产层 3.8，综合 **3.5/5（Alpha+）维持**，理由见 §9 |

---

## 1. F-001：执行前审批缺失 — 成立

### 1.1 完整调用链（已逐环验证）

```
AgentLoop._dispatch (loop.py:1127)
  └─ runner.run(AgentRunSpec(checkpoint_callback=_checkpoint))
       └─ ToolExecutionCoordinator.run_tool()          [tool_execution.py:130]
            ├─ verdict = SafetyPolicy.evaluate()        [tool_execution.py:141]
            ├─ if HIGH: logger.warning(...)             [tool_execution.py:142-143]  ← 全部响应仅此一行日志
            ├─ await _run_tool_impl(...)                [tool_execution.py:145]      ← 工具实际执行
            │    ├─ prepare_call: 仅参数类型转换+参数校验 [registry.py:84-117]
            │    └─ tool.execute(**params)              [tool_execution.py:252/254]
            └─ await emit_checkpoint("tool_completed")   [tool_execution.py:168]     ← 执行完成后才发出
                 └─ await callback(payload)              [runner.py:1498-1505]       ← 返回值丢弃
```

### 1.2 三个关键否定证据

1. **`emit_checkpoint` 无同步阻塞/拒绝语义**：实现仅为 `await callback(payload)`（runner.py:1498-1505），返回值被完全忽略，不存在 allow/deny/异常中断路径。
2. **checkpoint 消费方均为单向用途**：
   - 主循环 `_checkpoint`（loop.py:1018-1021）→ 将 payload 写入 session 的 runtime checkpoint 存储（纯持久化）；
   - 子代理 `_on_checkpoint`（subagent.py:393-395）→ 仅更新 `status.phase/iteration`（状态展示）。
3. **全代码库不存在工具执行前审批机制**：关键词 `approv|confirm|human-in-the-loop|ask_user|pending_approval` 的全部命中都在 **DM pairing（用户配对请求的 approve/deny/revoke）** 语境，与工具审批无关。

### 1.3 HIGH 风险分类是真实可达的（修正原报告的一个模糊点）

原报告未确认 HIGH 分类是否实际出现。复核确认 **16 个工具通过 `@property risk_level` 显式声明 HIGH**：`shell`、`filesystem`（写/删两处）、`apply_patch`、`delegate`、`execute_plan`、`spawn`、`create_agent`、`cron`、`cli_apps`、`exec_session`、`long_task`（两处）、`self`、`message`、`image_generation`（部分）。即：**shell 执行、文件写入删除等高危操作每天都在被分类为 HIGH，而系统的全部响应是一行 warning 日志**。

### 1.4 附加发现：`requires_checkpoint` 是死字段

`SafetyPolicy` 精心计算的 `requires_checkpoint`（safety_policy.py:44）在 `run_tool()` 中**从未被读取**——checkpoint 只要有 callback 就无条件发出（tool_execution.py:152）。风险分类结果实际只影响 checkpoint 里一个展示字段和日志级别。

### 1.5 可绕过路径

无需绕过。唯一的执行前屏障是：
- `restrict_to_workspace=True`（路径边界，见 §3）；
- SSRF 防护（网络边界）；
- `repeated_external_lookup_error`（重复外查拦截，tool_execution.py:182）。

以上均不构成对 **workspace 内 shell 执行、文件删除、外发消息** 的审批。`classify_violation`（违规分类）只在 prep_error/异常/错误文本出现后**事后**调用，属于事后归因而非事前拦截。

**结论：F-001 从"待复核"升级为"已确认的 P0 缺陷"。**

---

## 2. F-002：step acceptance 误接受 — 成立，严重性上调

### 2.1 缺陷代码（step_acceptance.py:242-256）

```python
if final_content and final_content.strip():
    if step.done_criteria:
        if step.done_criteria.lower() in final_content.lower():
            return True
        if tool_calls:
            return True      # ← done_criteria 未满足，仅因执行过工具即接受
        return False
    return True
return False
```

### 2.2 最小失败测试（已编写并运行）

测试文件：`.tmp-audit/test_f002_step_acceptance_audit.py`

- **Case 1（红，证明缺陷）**：`done_criteria="report.md created"`，最终文本为 "I listed the directory contents."（不含 criteria），`tool_calls=[{"name":"shell",...}]` → `accepted=True`。断言 `not accepted` 失败，缺陷实锤。
- **Case 2（绿，证明 verifier 无法纠正）**：`enable_verifier=True` 时用 FakeProvider 记录调用 → **零调用**。规则误接受时 LLM verifier 完全不参与。

### 2.3 比原报告更严重的证据

原报告问"verifier 是否总会覆盖它"——答案是**否，且方向相反**：`evaluate_with_verifier()` 第 105 行 `if rule_accepted or not enable_verifier: return evidence` 短路——**verifier 只能纠正"误拒绝"（false negative），永远无法纠正"误接受"（false positive）**。而 F-002 恰恰是误接受缺陷。

叠加因素：`enable_step_verifier` 默认 `False`（runner.py:133），规则模式是默认路径。

### 2.4 实际影响链（planning.py:241-332）

`complete_plan_step()` 中 `evidence.accepted=True` → 步骤标记 `COMPLETED`（planning.py:309）→ 计划提前推进/提前宣告 `plan_completed` → replan 不会触发。在 Managed plan（delegate_plan）路径下，"跑过工具"被系统性等价为"步骤完成"。

**结论：F-002 确认成立，且 verifier 兜底逻辑对该缺陷无效，严重性高于原报告评估。**

---

## 3. 默认值 fail-closed 核查 — 三项三项结论

| 项 | 默认值 | 判定 | 证据 |
|---|---|---|---|
| `restrict_to_workspace` | `True` | **fail-closed ✓** | config/schema.py:571；经 loop_builder.py:396 注入 AgentLoop；子代理经 loop.py:499 继承 |
| Shell sandbox | `sandbox=""`、`sandbox_required=False` | **fail-open ✗** | shell.py:87-90；Windows 上沙箱不可用时仅 `logger.warning` 后**无沙箱继续执行**（shell.py:471-482）；沙箱后端仅 bwrap（Linux） |
| API 认证 | `api_key=""` | **fail-open（本地）✗** | server.py:484-486：空 key 时全部路由开放；schema.py:437-442：非本地绑定+无 key 仅 warning 不阻止启动；设置后为 Bearer + `hmac.compare_digest`，仅 `/health` 公开（server.py:63） |

补充：`ExecToolConfig.sandbox_required` 的注释自述"默认 False 保持向后兼容(仅记录告警);生产环境建议开启"——代码作者已知悉该 fail-open 取舍。

---

## 4. 子 Agent 继承核查

| 维度 | 继承情况 | 证据 |
|---|---|---|
| workspace scope | ✓ 继承 | loop.py:499 → SubagentManager；subagent.py:232 `restrict_to_workspace=self.restrict_to_workspace`；workspace_scope 存在时进一步收紧（subagent.py:404-406） |
| exec/sandbox 配置 | ✓ 继承 | subagent.py:230 `exec=self.tools_config.exec` |
| 模型 | ✓ 继承 | subagent.py:431 `model=self.model` |
| max_iterations | ✓ 继承 | subagent.py:433 |
| 递归深度 | ✓ 有控制 | subagent.py:425 ContextVar depth+1 + `max_subagent_recursion_depth` |
| 并发限制 | ✓ 信号量 | subagent.py:398 `_spawn_semaphore` |
| 工具白名单 | ✓ 支持 | subagent.py:256-263 `_filter_tools` |
| **turn budget** | **✗ 不继承** | subagent.py:427-443 的 AgentRunSpec 无 `turn_budget` 字段（runner.py:110 默认 None）→ 子代理 CallLedger 无预算，**主 turn 的 token/cost 预算对 spawn/delegate 派生调用存在逃逸路径** |
| provider_retry_mode | ✗ 不传递 | 同上 spec 未设置 |

**新发现（原报告未列）：预算逃逸。** `TurnBudget`（turn_budget.py:36-60）是 per-turn 累计 token/cost 硬上限，但子代理运行独立的 `runner.run()` 且不携带预算——一次 turn 通过 spawn 派生 N 个子代理时，实际 LLM 消耗可达主预算的 N 倍以上而不触发任何预算终止。

---

## 5. 测试结果复核 — 878 errors 主因已定位

### 5.1 隔离环境分批重跑结果（独立 HOME/TEMP，`.venv` 现状）

| 批次 | 结果 |
|---|---|
| tests/agent + providers + security + session + architecture | **2283 passed**, 1 failed, 3 skipped, 1 xfailed（250s） |
| tests/config + command + pairing | 123 passed |
| tests/utils + cron | 249 passed |
| tests/composition | 5 passed |
| tests/tools（除 mcp_probe）+ mcp_tool | 488 passed, 21 skipped |
| tests/webui + cli_apps | 56 passed, 1 failed |
| tests/channels + cli | 621 passed, 5 skipped |
| tests/tools/test_mcp_probe.py | **挂死（无限等待）** |
| **合计（可达部分）** | **约 3825 passed / 2 failed / 29 skipped** |

两个失败：
1. `test_deep_research.py::test_full_workflow_with_reflect_extra_queries` — mock 环境下触发真实 30s 墙钟超时，单独重跑仍失败，属**超时敏感的测试设计问题**；
2. `test_mcp_presets_api.py::test_test_mcp_preset_connects_and_reports_tools` — MCP preset 真实连接，**网络依赖型失败**。

### 5.2 全量挂死的精确根因（新发现，原报告未定位）

- `tests/tools/test_mcp_probe.py::test_probe_uses_default_port_for_http` 探测 `http://unreachable-host.test/mcp`（test 文件第 32-34 行），本机网络环境下 DNS/连接无超时保护 → **无限挂起**；
- `.venv` **缺少 `pytest-timeout`**，而 pyproject.toml:176-177 配置了 `timeout = 600`/`timeout_method = "thread"` 且依赖组未声明该包 → **配置完全无效**（unknown config warning 可复现）→ 挂起测试永远不被打断；
- 全量测试因此必然挂死；被强杀/中断时 pytest 将未完成测试计为 error。

**归因结论：原报告的"878 errors"主要由 ① 挂起中断的级联 error ② 本机 `~/.miniunicorn` 权限 ③ 无效 timeout 配置构成；真实代码缺陷 ≤ 2 个（且均为测试设计/网络依赖问题，非产品缺陷）。** 原报告"不能等价为 878 个真实缺陷"的判断正确，且现在有了具体机制证据。

另注：原报告"定向子集 117 passed"的子集选择过窄——仅 tests/agent 等五个目录实际可通过 2283 项。

### 5.3 ruff（与原报告一致）

- `ruff check`：1 error（config/schema.py 元类 `__call__` 首参数命名 N805）；
- `ruff format --check`：32 文件待格式化。

---

## 6. Provider retry/fallback 副作用与计账

| 疑虑 | 复核结论 | 证据 |
|---|---|---|
| 重复记账 | **不会**。`chat_with_retry` 仅在最终响应后记录一次 ledger | base.py:689-698 "Record the ledger once with the final response" |
| 重复计费 | 真实成本可能增加（失败尝试若已耗 token），但 **ledger 不体现** → 预算低估（少记而非多记） | 同上 |
| 工具重复执行 | **不会**。retry 仅包裹 LLM chat 调用；工具调用不在 retry 范围 | base.py:681-687 |
| fallback 重复输出 | 有防护：`chat_stream` 已流出内容则跳过 failover | fallback_provider.py:130-142 |
| fallback 风暴 | 有熔断：主 provider 连续失败后 cooldown（半开探测） | fallback_provider.py:112-119 |

净结论：无"重复副作用/重复记账"缺陷；存在"失败尝试 usage 漏记"导致的预算低估，属于成本观测精度问题而非正确性问题。

---

## 7. 文档路径漂移 — 纯漂移，登记失真

`docs/architecture/module-boundaries.md:233/242/254` 引用的三个文件**均不存在于文件系统**：

| 文档引用 | 实际文件 |
|---|---|
| `miniunicorn/agent/safety.py` | `safety_policy.py` |
| `miniunicorn/agent/planning.py` | `planning_policy.py` |
| `miniunicorn/agent/delegation.py`（文档声称"原 execute_plan.py，别名兼容"） | `tools/execute_plan.py`（**delegation.py 不存在，"别名兼容"描述失真**） |

结论：纯文档漂移，无兼容模块。架构登记表需同步（原报告 P2-3 建议成立且需加强：不止路径，连"别名兼容"的说法都是错的）。

---

## 8. 原报告其他论断抽查

- 3.1 节引用的 7 处代码位置（turn_orchestrator.py:55、call_ledger.py:67、context_governor.py:89、memory.py:100、turn_telemetry.py:21、step_acceptance.py:242、safety_policy.py:21）**全部准确**（均为对应类/函数定义行）。
- "API 认证是单一 Bearer Key 语义"（§4）基本准确，但应补充：**默认配置下无任何鉴权**（空 key 全开放，仅本地绑定时可接受）。
- README 12 项清单与代码对应关系抽查属实（`FallbackProvider`、`SubagentManager`、`TurnBudget`、`ContextGovernor` 等均有真实实现）。

---

## 9. 重新评分

| 维度 | 原报告 | 复核评分 | 差异说明 |
|---|---:|---:|---|
| Agent 核心闭环 | 4.0/5 | **4.0/5** | 维持。2283 项定向测试通过支撑主流程质量；F-002 已确认且 verifier 无法兜底，但影响面限于 Managed plan 路径，不足以降档 |
| Harness 平台 | 3.4/5 | **3.4/5** | 维持。新增负面证据（子代理预算逃逸、失败尝试漏记）与新增正面证据（fallback 熔断、stream 防重）大致相抵 |
| 安全与治理 | 2.8/5 | **2.6/5** | **下调 0.2**：F-001 从"待复核"变为"确认成立"（16 个 HIGH 工具零审批）+ shell 沙箱/API 认证双 fail-open 确认 + `requires_checkpoint` 死字段 |
| 可观测与评估 | 2.8/5 | 2.8/5 | 维持。测试面广（3800+ 可通过）但基建缺陷确认（无超时保护、网络测试挂死） |
| 产品与单机部署 | 3.8/5 | 3.8/5 | 维持 |
| **综合** | **3.5/5** | **3.5/5（Alpha+）** | 维持总量级；安全维度下调被测试归因利好抵消 |

**与原报告不同的结论清单：**

1. F-001 由"待复核"**确认为成立**（P0）；
2. F-002 严重性**上调**：verifier 只能纠正误拒、无法纠正误接受，兜底逻辑对本缺陷无效；
3. **新发现**：主 turn 预算对子代理不生效（预算逃逸路径）；
4. **新发现**：全量测试挂死根因 = `test_mcp_probe.py` 网络探测无超时 + `pytest-timeout` 依赖缺失且配置无效；878 errors 以环境/级联因素为主，真实代码缺陷 ≤ 2；
5. **新发现**：`requires_checkpoint` 字段在 `run_tool()` 中未被消费（死字段）；
6. 原报告定向测试子集（117 passed）覆盖远小于实际可通过量（2283）；
7. 文档漂移比原报告描述更糟：连"别名兼容"的说明本身都是错的。

---

## 10. 修订后的 P0 建议（在原报告 §8 基础上）

1. **F-001**：在 `run_tool()` 的 `_run_tool_impl()` 之前插入审批门（allow/deny/approve 三态），HIGH 工具默认 deny-until-approved；同时消费 `requires_checkpoint` 字段或删除之。
2. **F-002**：删除 `_is_accepted()` 中 `if tool_calls: return True` 分支；将 verifier 从"仅拒绝时兜底"改为"规则接受但 done_criteria 显式存在且未匹配时也复核"。
3. **预算逃逸**：子代理 AgentRunSpec 传入（共享或按比例派生的）turn_budget，或将子代理 ledger 汇入主 turn。
4. **测试基建**：`.venv` 补装 `pytest-timeout` 并在依赖组声明（或删除 pyproject 无效配置）；给 `test_mcp_probe.py` 的网络探测加超时/标记 `@pytest.mark.network`。

---

## 11. 复核产物

- 最小失败测试：`.tmp-audit/test_f002_step_acceptance_audit.py`（Case 1 红=缺陷证据；Case 2 绿=verifier 短路证据）
- 本报告：`docs/agent-harness-evaluation-review-2026-08-25.md`

## 12. 限制

- 未验证子代理的深度取消语义（主 turn 用户中断时子代理后台 task 的传播路径），时间所限留待后续；
- Windows 环境下 bwrap 沙箱路径未实测（仅静态确认其不可用时降级行为）；
- 全量测试未完整跑通（挂起源已定位并单独验证其余目录），合计数字为分批加总。
