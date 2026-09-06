<div align="center">

**开源、自托管的 Agent 运行时引擎**

把任意 LLM 变成长期运行、可治理、可审计的 Agent 系统——  
一条透明的执行内核，一套确定性的治理机制，一层可插拔的接入面。

![Python](https://img.shields.io/badge/python-≥3.11-blue) ![License](https://img.shields.io/badge/license-MIT-green) ![Release](https://img.shields.io/badge/release-v0.4.0-success) ![Status](https://img.shields.io/badge/status-alpha-orange)

**[简体中文]** | [English](./README.en.md)

</div>

---

## 这是什么

Erza 不是个人 AI 助手，也不是聊天机器人框架。**Erza 是一个 Agent 运行时（Agent Runtime）**：位于 LLM 之下、应用之上的一层基础设施，负责把"会说话的模型"变成"能干活、可托管的软件系统"。

它由三部分构成：

- **执行内核**（约 3.9k 行）：一条固定的 ReAct 循环和一台轮次状态机。没有插件钩子链，没有中间件栈，没有动态编排——读 `agent/loop.py` 和 `agent/runner.py` 就能理解 Agent 的全部行为路径。
- **治理机制**：调用账本、结构化工具回执、基于证据的步骤验收、受治理的记忆生命周期。这些机制不依赖模型自觉，全部是确定性代码。
- **接入面**：5 个 IM 频道 + WebSocket、OpenAI 兼容 HTTP API、Python SDK、WebUI 控制台。所有流量从边缘进入，汇入同一条消息总线。

基于 [Nanobot](https://github.com/marm-io/nanobot) 二次开发。项目命名取自《妖精的尾巴》的 Erza：本体是稳定的核心，装备按需从武器库换装——对应本项目的 **core + library** 架构。

> *"If you're not the model, you're the harness."* —— 当模型能力跨过阈值后，决定 Agent 生产力的是包裹模型的工程化基础设施。Erza 就是这样一个完整实现。

### 适用与不适用

**适合**：构建需要长期运行、有状态、可审计的 Agent 应用（垂直 Agent、运维代理、研发助理）；需要接入 IM 或暴露 API 的自托管部署；研究 Agent 执行内核、记忆治理与工具治理的实现。

**不适合**：需要复杂 DAG 工作流引擎的场景；多租户 SaaS（当前为单工作区隔离模型）；不接受文件系统 / Shell 访问的高沙箱环境。


## 整体架构

系统围绕一条异步消息总线展开，分四层：

<div align="center">

![Erza 四层架构：频道层 → 消息总线 → 代理核心 → 能力层](docs/architecture.svg)

</div>

```
接入层    channels/(飞书·微信·企微·钉钉·QQ·WebSocket)   api_compat/(OpenAI 兼容)   cli/   webui/(控制台)
             │                    │                        │
             └────────────┬───────┴────────────┬───────────┘
                          ▼                    ▼
                   bus/MessageBus ──────► command/ 斜杠命令路由
                          │
                          ▼
执行内核   agent/  AgentLoop（轮次状态机）──► AgentRunner（ReAct 循环）
             │              │                    │
             │              │             ┌──────┴──────┐
             │              ▼             ▼             ▼
治理机制     │        ledger/CallLedger  planner/  step_acceptance(回执+证据)
             │
             ├──► tools/registry ──► tools/*（25+ 内置工具）＋ mcp_runtime（MCP 服务器）
             ├──► providers/（多提供商 + Fallback 链）
             ├──► memory/（SQLite 结构化记忆 + Dream 蒸馏）＋ session/（会话持久化）
             └──► security/（工作区边界 · SSRF 防护 · 沙箱 · 风险分级）

控制面     webui/ Python 网关 ──► React 18 前端（设置/频道/工具/记忆管理）
组合根     composition/  gateway / agent / serve 三种装配方式
```

频道的入站消息经 49 行的 `MessageBus`（有界队列 + 背压）进入内核；内核之下，工具、技能、提供商组成能力层。**所有跨层通信都走显式接口，没有隐式全局状态。**

## 执行内核

### 轮次状态机（`agent/turn_orchestrator.py`）

每一轮对话是确定性的状态流转：

```
RESTORE → COMPACT → COMMAND → BUILD → RUN → SAVE → RESPOND → DONE
```

状态机从 `AgentLoop` 中独立出来，依赖通过 `TurnDeps` 显式注入。恢复（RESTORE）和压缩（COMPACT）在进入对话之前发生，保证崩溃后可续跑、上下文不腐烂。

### ReAct 循环（`agent/runner.py`）

Thought → Action → Observation 的固定循环。这是系统**唯一的处理路径**："Dumb Loop" 哲学——循环保持笨拙而透明，智能留给模型，复杂性推到边缘。

### 计划与执行（`agent/planner.py`）

启用 Planner 后，LLM 先把任务分解为有序步骤，再逐步入 ReAct 循环执行；失败步骤携带原因触发重新规划（replan），剩余步骤延续。计划快照（`plan_snapshot.py`）与恢复检查点隔离持久化。

### 回执与证据验收（`tools/receipts.py` · `agent/step_acceptance.py`）

每个契约类工具调用产出**结构化回执**（status / result_excerpt / receipt）。步骤是否完成由**确定性规则**基于回执证据判定，规则拒绝时才回退到 LLM 验证器——步骤完成与否不再依赖模型自述。

### 调用账本（`ledger/call_ledger.py`）

单轮内所有 LLM 调用按用途记账（executor / planner / replan / reflection），`turn_budget` 据此约束单轮资源消耗，防止失控循环。

### 上下文治理（`agent/context_governor.py`）

可插拔策略链，在每次 LLM 调用前把上下文压回窗口内：Snip（裁剪最旧片段交给 Consolidator）→ Microcompact（压缩旧工具结果）→ 孤儿治理（保证工具结果消息序列合法）→ AutoCompact（空闲会话主动压缩）。第三方策略经入口点 `erza.context_strategies` 注册。

### 反思与 Dream（`agent/reflection.py` · `memory/dream.py`）

失败或每 N 轮触发一句话反思，写入 `reflections.jsonl`；**Dream** 在空闲时（`dream_trigger.py`，用户停用 5 分钟后触发，与 cron 保底互补）把新增摘要与反思蒸馏成候选事实，进入受治理的记忆生命周期。目标是跨轮次学习——不重复同一个错误。

## 状态与记忆

| 层    | 载体                            | 职责                                                                                                          |
| ---- | ----------------------------- | ----------------------------------------------------------------------------------------------------------- |
| 短期会话 | `session/`                    | 活跃对话上下文，原子写入（临时文件 + fsync + rename），崩溃安全                                                                    |
| 压缩归档 | `memory/history.jsonl`        | 追加式历史摘要，带游标，由 Consolidator 维护                                                                               |
| 长期知识 | `memory/structured/memory.db` | **SQLite 单一事实存储**（`memory/repository.py`，fail-closed 健康检查），仅 `memory/lifecycle.py` 有权晋升/替换/吊销/过期记录，全部变更走单事务 |
| 教训沉淀 | `memory/reflections.jsonl`    | 失败与周期性反思                                                                                                    |
| 版本历史 | `utils/` GitStore（内嵌 Git）     | 长期文件每次变更可 diff、可回滚                                                                                          |

记忆进入提示词的唯一路径是**确定性召回**：只召回符合精确作用域的 active 事实；候选事实、历史归档不会整体注入。旧版 JSONL 日志仅作为迁移输入（`memory/jsonl_import.py`）。

## 能力层

### 工具系统（`tools/`，25+ 内置）

`ToolRegistry` 动态注册 + 别名机制，`pkgutil` 自动发现；每个工具有显式 JSON Schema，执行受安全层约束。

| 类别   | 工具                                                                                            |
| ---- | --------------------------------------------------------------------------------------------- |
| 文件系统 | `read_file` · `write_file` · `edit_file` · `apply_patch` · `list_dir` · `find_files` · `grep` |
| 执行   | `exec`（可选拌箱，持久会话）· `write_stdin` · `list_exec_sessions`                                       |
| 检索   | `web_fetch`（URL → Markdown，SSRF 防护 + DNS rebinding 钉扎）                                        |
| 编排   | `cron` · `long_task` · `execute_plan` · `activate_plan`                                       |
| 子代理  | `spawn` · `delegate` · `create_agent`                                                         |
| 外部   | `mcp_*`（多服务器连接栈）· `message`（跨频道）                                                              |
| 自省   | `self`（运行时状态查询，白名单门控）                                                                         |

外部能力三条接入路径，全部不碰内核：**MCP 服务器**（`tools/mcp_runtime.py`，由组合根持有连接栈）、**技能**（Markdown + YAML frontmatter，按需注入）、**Python 入口点插件**。

### LLM 提供商（`providers/`）

统一基类 + `ProviderSpec` 声明式行为 flag（`force_string_content`、`normalize_tool_call_ids` 等）消除各家差异的硬编码分支：

- OpenAI 兼容（DeepSeek、OpenRouter、Moonshot、Azure、vLLM、Ollama 等）
- OpenAI Responses API（GPT-5 / o-series 独立解析路径）
- Anthropic（自适应思考与缓存优化）
- `FallbackProvider` 主模型失败自动切换；签名指纹（`ProviderSignature`）驱动的运行时热切换

## 接入层

| 模块                   | 职责                                                                                                                   |
| -------------------- | -------------------------------------------------------------------------------------------------------------------- |
| `channels/`（12.6k 行） | 5 个 IM 适配器（飞书/微信/企微/钉钉/QQ）+ WebSocket，统一 `BaseChannel` 接口，二维码扫码登录（`QRCodeAuthHandler`），`allowFrom` 准入白名单，均为可选 extras |
| `bus/`               | 49 行异步消息总线，有界队列 + 自然背压，入站/出站解耦                                                                                       |
| `command/`           | 斜杠命令路由，priority / exact / prefix 三层匹配，含治理型记忆管理命令                                                                     |
| `cron/`              | 自然语言定时任务，持久化存储，重启后补执行                                                                                                |
| `api_compat/`        | OpenAI 兼容 HTTP API（`/v1/chat/completions` SSE 流式、`/v1/models`），可选 extras `[api]`                                     |
| `webui/`             | Python 网关：HTTP/WebSocket 路由、设置/频道/工具/记忆管理 API；前端为 React 18 + Vite + TypeScript（约 4 万行）                               |

## 安全模型

| 边界       | 机制                                                                              |
| -------- | ------------------------------------------------------------------------------- |
| 文件访问     | 工作区路径边界（`security/workspace_policy.py`），越界是硬策略错误，模型不可用 shell 技巧绕过               |
| Shell 执行 | 可选 `bwrap` 沙箱、受限环境变量注入、exec_session 配置门控                                        |
| 出站 HTTP  | SSRF 防护：传输层钩子拦截 IP 字面量目标、DNS rebinding 钉扎（30s TTL）、重定向复检（`security/network.py`） |
| 风险分级     | `RiskLevel` 标注工具风险，高危工具经审批门（approval gate）与检查点隔离（`agent/tool_checkpoint.py`）    |
| 频道准入     | 各频道 `allowFrom` 白名单                                                             |

权限架构与推理架构分离：安全检查在工具执行层强制执行，不依赖模型自觉。

## 组合根与入口

所有长生命周期对象（总线、cron、会话管理、MCP 连接栈）由组合根创建并按逆序关闭，内核不自行创建资源：

| 入口                          | 组合根                              | 用途                      |
| --------------------------- | -------------------------------- | ----------------------- |
| `erza gateway`              | `composition/gateway.py`         | 全功能网关（频道 + WebUI + API） |
| `erza agent` / `erza serve` | `composition/agent_app.py`       | 无头终端对话 / 纯 API 服务       |
| Python SDK                  | `erza.py` 的 `Erza.from_config()` | 编程式嵌入                   |

```python
from erza import Erza

bot = Erza.from_config()
result = await bot.run("总结这个仓库的架构")
print(result.content, result.tools_used)
```


## 代码地图

Python 源码约 **7.0 万行**（69.6k），WebUI TypeScript 约 **4.0 万行**，测试 **253 个文件 / 8.5 万行**：

| 包                   | 行数     | 职责                                      |
| ------------------- | ------ | --------------------------------------- |
| `erza/channels/`    | 12,644 | IM 频道适配器与媒体处理                           |
| `erza/agent/`       | 12,511 | 执行内核：状态机、ReAct、规划、验收、上下文治理              |
| `erza/tools/`       | 8,963  | 内置工具、注册表、MCP 运行时、沙箱                     |
| `erza/webui/`       | 6,485  | 控制台网关 API（前端在仓库根 `webui/`）              |
| `erza/memory/`      | 6,163  | SQLite 记忆仓库、生命周期治理、Dream 蒸馏             |
| `erza/providers/`   | 5,121  | 多提供商抽象与 Fallback 链                      |
| `erza/cli/`         | 3,717  | Typer 命令、终端渲染、网关运行器                     |
| `erza/utils/`       | 3,516  | 文档解析、媒体解码、GitStore、原子写                  |
| `erza/skills/`      | 2,105  | 内置技能包                                   |
| `erza/session/`     | 1,608  | 会话持久化与目标状态                              |
| `erza/config/`      | 1,285  | Pydantic 配置模型（camelCase/snake_case 双兼容） |
| `erza/command/`     | 1,258  | 斜杠命令路由                                  |
| `erza/security/`    | 1,240  | 工作区边界、SSRF、风险分级                         |
| `erza/cron/`        | 1,014  | 定时任务服务                                  |
| `erza/composition/` | 573    | 组合根（gateway / agent_app）                |
| `erza/api_compat/`  | 557    | OpenAI 兼容 API                           |
| `erza/ledger/`      | 431    | 调用账本与轮次预算                               |
| `erza/bus/`         | 141    | 消息总线                                    |
| `erza/erza.py`      | SDK 门面 | `Erza.from_config().run()`              |

## 快速开始

```bash
# 从源码安装
git clone https://github.com/zhoulingquan/Erza.git
cd Erza
pip install -e .

# 可选附加依赖
pip install -e ".[api,pdf,dev]"   # HTTP API / PDF 解析 / 测试
```

运行时依赖约 30 个纯 Python 包（除 lxml 外无原生编译）。也可以用 Docker / Linux 服务 / macOS LaunchAgent 部署，见 [deployment.md](./docs/deployment.md)。

**一条命令启动**——配置和工作区自动初始化：

```bash
erza gateway
# → 浏览器访问 http://127.0.0.1:8765
```

首次启动没有 LLM 配置，在 WebUI **设置 → 模型配置** 填入任意 OpenAI 兼容提供商的 API Key，保存即生效，无需重启。

```bash
erza agent            # CLI 终端对话（需先配置 LLM）
erza serve            # 仅 OpenAI 兼容 API
erza onboard --wizard # 交互式配置向导
```

配置文件位于 `~/.erza/config.json`，支持 `${VAR}` 环境变量替换。

## 频道接入

| 频道        | 凭据接入                 | WebUI 扫码登录 |
| --------- | -------------------- | ---------- |
| WebSocket | 内置 WebUI，无需配置        | —          |
| 飞书        | App ID + App Secret  | ✓          |
| 钉钉        | App Key + App Secret | ✓          |
| 企业微信      | Bot ID + Bot Secret  | ✓          |
| 微信        | —                    | ✓          |
| QQ        | App ID + App Secret  | ✓          |

频道经 `pkgutil` 自动发现，支持入口点插件扩展，详见 [channel-plugin-guide.md](./docs/channel-plugin-guide.md)。

## 内置技能

Markdown + YAML frontmatter 定义，按需加载：

`cron` · `document-processing` · `github` · `long-goal` · `memory` · `my` · `skill-creator` · `summarize` · `tmux` · `update-setup` · `weather`

## 测试与质量

253 个测试文件、8.5 万行测试代码，覆盖全部核心模块；`pytest-asyncio` 自动模式 + 覆盖率统计；`ruff` 静态检查；CI 覆盖三大操作系统矩阵。

```bash
pip install -e ".[dev]"
pytest
```

## 文档

| 主题         | 链接                                                        |
| ---------- | --------------------------------------------------------- |
| 快速开始       | [quick-start.md](./docs/quick-start.md)                   |
| 配置参考       | [configuration.md](./docs/configuration.md)               |
| 频道接入       | [chat-apps.md](./docs/chat-apps.md)                       |
| WebUI      | [../webui/README.md](./webui/README.md)                   |
| CLI 参考     | [cli-reference.md](./docs/cli-reference.md)               |
| 聊天命令       | [chat-commands.md](./docs/chat-commands.md)               |
| OpenAI API | [openai-api.md](./docs/openai-api.md)                     |
| 部署         | [deployment.md](./docs/deployment.md)                     |
| 记忆系统       | [memory.md](./docs/memory.md)                             |
| Python SDK | [python-sdk.md](./docs/python-sdk.md)                     |
| 频道插件       | [channel-plugin-guide.md](./docs/channel-plugin-guide.md) |

完整文档目录见 [docs/README.md](./docs/README.md)。

## 贡献

PR 欢迎。代码库刻意保持可读——执行内核、治理机制、接入面三层边界清晰，改哪层就读哪层。

| 分支        | 用途   |
| --------- | ---- |
| `main`    | 稳定发布 |
| `nightly` | 实验特性 |

详见 [CONTRIBUTING.md](./CONTRIBUTING.md)。

## 许可证

MIT — 见 [LICENSE](./LICENSE) 与 [THIRD\_PARTY\_NOTICES.md](./THIRD_PARTY_NOTICES.md)。

---

<div align="center">

<em>透明的内核，确定的治理，边缘的扩展。</em>

</div>
