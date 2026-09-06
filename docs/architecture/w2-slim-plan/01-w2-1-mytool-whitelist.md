# W2-1：MyTool 白名单化（deny-list → allow-list）

> 前置依赖：tag `baseline-pre-w2`。
> 本批是 **W2 唯一的安全修复批**，优先实施。

## 一、问题（现状锚点，已逐一核实）

`erza/agent/tools/self.py`（560 行）的 MyTool 让模型通过 `my` 工具对 AgentLoop 宿主对象做点路径 check/set，采用 **deny-list** 防护：

1. `BLOCKED`（60-90 行）：bus、provider、tools、runner、sessions、`_mcp_servers`、`_mcp_stacks`、`_background_tasks` 等约 22 个名字。
2. `READ_ONLY`（93-100 行）：subagents、`_current_iteration`、exec_config、workspace_sandbox。
3. `_DENIED_ATTRS`（102-126 行）+ `_SENSITIVE_NAMES`（129-141 行）：dunder 与敏感名。
4. `_resolve_path`（246-266 行）逐段查上述清单；`_modify`（441-475 行）对**不在任何清单里的顶层属性**落到 `_modify_free`（504 行起）→ `_has_real_attr` 为真则直接 `setattr` 到宿主对象上。

**deny-list 腐烂的实际证据**（W0/W1 演进新增的属性均未入清单）：

| 可被 `my set` 整体替换的属性 | 后果 |
|---|---|
| `_mcp_runtime` | 换掉 MCP 连接栈 owner（W1-2 新增；BLOCKED 只挡了旧的 `_mcp_servers`/`_mcp_stacks`，挡不住新 owner） |
| `cron_service` | 换掉 CronService 引用 |
| `workspace_scopes` | 换掉工作区边界对象 |
| `model_preset` / `_provider_snapshot_loader` | 干扰模型预设快照链 |

另有次要问题：`_modify_free` 对不存在属性的行为是写进 `_runtime_vars`（scratchpad，合理），但对**存在**的非清单属性是 setattr 污染宿主（不合理）。

## 二、设计：翻转防护模型

**原则：未明确允许的路径一律拒绝。** deny-list 的三套清单保留为纵深防御，但第一道门换成 allow-list。

### 2.1 新增两个类级清单（self.py，与 BLOCKED 并列）

```python
# check 允许访问的顶层属性（点路径的根）。子路径仍受 _DENIED_ATTRS /
# _SENSITIVE_NAMES 逐段过滤（纵深防御保留）。
INSPECTABLE = frozenset(
    {
        "model", "model_preset", "max_iterations", "context_window_tokens",
        "tool_names", "workspace", "provider_retry_mode",
        "max_tool_result_chars", "_last_usage", "exec_config",
        "workspace_sandbox", "subagents", "_current_iteration",
        "current_iteration", "scratchpad",
    }
)

# set 允许写入的目标：RESTRICTED 三键 + scratchpad（_runtime_vars）。
# 点路径 set 只允许落在 _runtime_vars 内。
SETTABLE = frozenset({"model", "max_iterations", "context_window_tokens"})
```

清单构成依据：
- INSPECTABLE = `_inspect_all`（411-437 行）已经展示的键 + `RuntimeState` 协议（tools/runtime_state.py 15-58 行）声明的可读属性 + `scratchpad` 别名。**不新增能力，只固化现状**。
- `current_iteration` 与 `_current_iteration` 并列（协议用前者、loop 实现为后者，两个都放行，与现状一致）。
- `_runtime_vars` 不直接进 INSPECTABLE（BLOCKED 里就有它），继续走 `scratchpad` 别名与 `_inspect` 的既有 fallback（401-407 行）。

### 2.2 `_resolve_path` 前置门（改 _inspect 与 _modify 的入口检查）

```python
def _inspect(self, key: str | None) -> str:
    if not key:
        return self._inspect_all()
    top = key.split(".")[0]
    if top not in self.INSPECTABLE:            # 新第一道门
        # 保留既有 fallback：scratchpad 别名 + _runtime_vars 简单键（见下）
        ...
```

- `_inspect` 的既有 fallback 逻辑（397-407 行：`scratchpad` 别名、`_runtime_vars` 简单键）**原样保留**——这是 set 写入的 scratchpad 数据的读回通道，砍掉会破坏合法功能。
- `_modify` 的入口改为：`top in SETTABLE` 走 RESTRICTED 校验（473-474 行既有路径）；`top` 是 `_runtime_vars` 的 scratchpad 语义（即：无点路径、且顶层不是宿主真实属性，或就是显式 `scratchpad.x` 形式）→ 写 `_runtime_vars`；**其余一律拒绝**，返回错误信息并在 `_audit` 记录 `WHITELIST-DENY {key}`。

### 2.3 点路径 set 收口（_modify 456-472 行段重写）

现状：任意 `a.b.c = v` 只要每段不踩清单就对 parent 做 setattr/dict 写入。改为：

- 点路径 set 仅当**整条路径落在 scratchpad 命名空间**（`scratchpad.x` 或 `_runtime_vars.x`）时允许；
- 其余点路径 set 一律拒绝（exec_config.sandbox 这类现状就依赖 READ_ONLY 挡顶层，白名单后天然更严）。

### 2.4 工具 description 同步

`description`（182 行起）中 set 的说明改为与白名单一致：可 set 的键 = model / max_iterations / context_window_tokens + scratchpad 笔记。措辞小幅调整，不重写整个 description。

### 2.5 不改的部分

- `_modify_restricted`（477-502 行）：RESTRICTED 校验、model 联动 `_active_preset`、max_iterations 联动 `_sync_subagent_runtime_limits` —— 原样。
- `_audit` 审计日志、`modify_allowed`（allow_set）总开关 —— 原样。
- `_format_value` / `_inspect_all` 输出格式 —— 原样。
- `tools/runtime_state.py` 协议文件 —— 本批不动（它只是类型声明；真正的门在 self.py 运行时）。

## 三、测试要求

扩展 `tests/agent/tools/test_self_tool.py`（另有两个相关文件 `test_self_tool_runtime_sync.py`、`test_onboard_logic.py`、`test_self_model_preset.py` 确认不回归）：

**白名单正面（合法路径不缩水）**：
1. `check` 无 key → 总览输出包含既有键集。
2. `check model` / `check max_iterations` / `check _last_usage.prompt_tokens` / `check exec_config.sandbox` / `check scratchpad` → 正常。
3. `set max_iterations 25` / `set model "x"` → 正常（含校验边界：min/max 拒绝）。
4. `set notes "hello"` → 进 scratchpad，`check notes` 读回。
5. `set scratchpad.foo "bar"` / `check scratchpad.foo` → 正常。

**白名单反面（活漏洞关闭）**：
6. `set _mcp_runtime <x>` → 拒绝（错误信息 + audit 记录）。
7. `set cron_service` / `set workspace_scopes` / `set model_preset` / `set _provider_snapshot_loader` → 拒绝。
8. `check _mcp_runtime` → 拒绝（不在 INSPECTABLE）。
9. `set exec_config.sandbox true` → 拒绝（点路径 set 只允许 scratchpad 命名空间）。
10. `set subagents <x>` → 拒绝（READ_ONLY 语义并入白名单后仍拒绝）。
11. 纵深防御回归：`check __class__` / `check _runtime_vars.api_key`（敏感名）→ 拒绝。
12. `allow_set=false` 时 `set` → 总开关错误（现状保留）。

**宿主完整性**：
13. 一轮 set 操作后 `loop.__dict__` 键集合不变（无 setattr 污染新增键）——用 FakeLoop 断言 `__dict__` keys 集合前后一致（scratchpad 走 `_runtime_vars` 不落宿主 `__dict__`）。

**既有测试处置预案**：deny-list 时代若存在"set 任意新属性成功"类断言（预期在 `_modify_free` 路径），更新为白名单语义（拒绝或走 scratchpad）。逐条列出修改。

## 四、禁改清单

- `MyToolConfig`（enable / allow_set 语义与配置键名）
- `_modify_restricted` 的校验规则与联动副作用
- `_format_value` / `_inspect_all` 的输出格式（webui/onboard 依赖文本形态）
- `tools/runtime_state.py` 协议定义
- AgentLoop 上任何属性的命名（本批只在 self.py 收口，不动宿主）
- 审批门 / 证据管道 / checkpoint（W0/W1 受保护区）

## 五、验收自检

- [ ] 全量测试绿（passed ≥ 4077），ruff 零告警
- [ ] 漏洞用例 6-8（`my set _mcp_runtime` / `cron_service` / `check _mcp_runtime` 拒绝）在批次报告中展示断言输出
- [ ] 合法功能用例 1-5 全部通过（无功能缩水）
- [ ] 既有测试修改逐条列出理由
- [ ] 与本规格的偏差逐条说明
