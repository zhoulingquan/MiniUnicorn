<div align="center">

<img src="docs/logo.svg" alt="MiniUnicorn Logo" width="200" height="200">

**一个开源、超轻量级的个人 AI 代理框架**

围绕一个可读的核心循环构建——消息进来，LLM 决策，工具执行，记忆按需注入。

[![Python](https://img.shields.io/badge/python-≥3.11-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](./LICENSE)
[![Release](https://img.shields.io/badge/release-v0.2.0-success)](https://github.com/zhoulingquan/MiniUnicorn/releases)
[![Status](https://img.shields.io/badge/status-alpha-orange)]()

</div>

---

## 这是什么

MiniUnicorn 是一个可以长期运行的个人 AI 代理。它不是聊天机器人框架，也不是编排引擎——它只是一个**小的代理循环**：接收消息、调用 LLM、执行工具、返回结果。所有重的东西（频道适配、工具实现、记忆策略）都挂在循环外围，核心保持可读、可审计、可替换。

基于 [Nanobot](https://github.com/marm-io/nanobot) 项目二次开发，在其轻量级代理核心基础上扩展了频道适配、记忆系统、WebUI 和多平台部署能力。

## 组成结构

整个系统围绕一个异步消息总线展开，分四层：

<div align="center">
<table>
<tr><td>

**频道层** `channels/`<br>
飞书 · 钉钉 · 企微 · 微信 · QQ · WebSocket(WebUI)

</td></tr>
</table>

<sub>↓ InboundMessage</sub>

<table>
<tr><td>

**消息总线** `bus/queue.py` (49 行)<br>
异步队列，解耦频道与代理

</td></tr>
</table>

<sub>↓</sub>

<table>
<tr><td>

**代理核心** `agent/`<br>
<code>AgentLoop</code> → <code>AgentRunner</code> → <code>LLM Provider</code><br>
&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;├─ ToolRegistry (工具调度)<br>
&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;├─ SessionManager (会话与压缩)<br>
&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;├─ Dream (两阶段记忆整合)<br>
&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;└─ SubagentManager (子代理委派)

</td></tr>
</table>

<sub>↓ OutboundMessage</sub>

<table>
<tr><td>

**能力层** `tools/ · skills/ · providers/`<br>
文件系统 · Shell · 网页搜索 · MCP · 定时任务 · ...

</td></tr>
</table>
</div>

### 核心运行时

| 模块 | 职责 |
|------|------|
| `agent/` | AgentLoop 协调对话轮次，AgentRunner 执行 LLM 循环，含上下文治理策略与自动压缩 |
| `session/` | 会话历史持久化、自动压缩、目标状态跟踪 |
| `config/` | Pydantic 配置模型，支持 `${VAR}` 环境变量 |
| `cron/` | 自然语言定时任务，持久化，重启补执行 |
| `bus/` | 异步消息总线 |
| `command/` | 斜杠命令路由（priority/exact/prefix 三层匹配） |

### 扩展模块

| 模块 | 职责 |
|------|------|
| `channels/` | 6 个频道适配器（飞书/钉钉/企微/微信/QQ/WebSocket） |
| `agent/tools/` | 25 类内置工具（文件/Shell/搜索/MCP/子代理...） |
| `webui/`（仓库根） | React 18 + Vite + TypeScript 前端（约 4 万行 TS/TSX） |
| `miniunicorn/webui/` | Python 网关：HTTP/WebSocket 路由、设置/频道/工具管理 API |
| `apps/` | Agent App 生态：CLI 应用目录、安装与扩展市场协议 |
| `cli/` | Typer CLI 命令、终端渲染、网关运行器 |
| `utils/` | 文档解析、媒体解码、Git 存储等工具 |
| `providers/` | LLM 提供商抽象与 OpenAI 兼容实现 |
| `security/` | 工作区限制、SSRF 防护、Shell 沙箱 |
| `pairing/` | DM 发送者配对码审批存储（0600 权限持久化） |
| `api/` | OpenAI 兼容 HTTP API |

## 技术特点

### 1. 核心循环可读

`AgentLoop` → `AgentRunner` 是整个系统唯一的处理路径。没有插件钩子链，没有中间件栈，没有动态编排。读这两个文件就能理解代理如何工作。

### 2. 总线解耦

频道与代理通过 `MessageBus`（49 行）完全解耦。频道只管发布 `InboundMessage`、消费 `OutboundMessage`，不感知代理内部状态。添加新频道不需要修改核心。

### 3. 扩展在边缘

新能力通过五种方式接入，**不进入核心循环**：

- **频道**（`channels/`）— 接入新的聊天平台
- **工具**（`agent/tools/`）— 暴露新能力给 LLM
- **技能**（`skills/`）— Markdown 知识包，按需注入上下文
- **CLI 应用**（`run_cli_app`）— 调用本机已安装的命令行程序（ffmpeg、pandoc、git 等），通过 SKILL.md 指导代理使用
- **MCP 服务器** — 外部进程，通过 MCP 协议调用

### 4. 记忆即上下文

记忆不是一个巨大的文件，而是分层的：短期会话、压缩归档、长期知识文件各司其职，按需注入而非持久编排（详见下方 [记忆系统](#记忆系统)）。会话写入是原子的（临时文件 + fsync + rename），崩溃安全。

上下文治理是策略化的：`runner_strategies` + `context_governor` + 轮次预算共同决定注入什么、压缩什么、丢弃什么，第三方策略可通过入口点插件注册。

### 5. 安全边界明确

| 边界 | 机制 |
|------|------|
| 文件访问 | `_resolve_path` 强制路径在工作区内 |
| Shell 执行 | 可选 `bwrap` 沙箱，工作区限制 |
| 出站 HTTP | `validate_url_target` 阻止 RFC1918 和云元数据端点 |
| DM 准入 | 频道发送者配对码审批 |

### 6. 工具生态

25 类内置工具（`pkgutil` 自动发现，第三方工具可经入口点插件注册）：

| 类别 | 工具 |
|------|------|
| 文件系统 | `read_file` · `write_file` · `edit_file` · `apply_patch` · `list_dir` · `find_files` · `grep` |
| 执行 | `exec`（沙箱可选，持久会话）· `write_stdin` · `list_exec_sessions` · `run_cli_app`（本机 CLI） |
| 检索 | `web_search`（多后端聚合 + 缓存/熔断）· `web_fetch` · `deep_research` |
| 编排 | `cron` · `long_task` · `execute_plan` · `complete_goal` |
| 子代理 | `spawn` · `delegate` · `create_agent` |
| 外部 | `mcp_*`（多服务器）· `message`（跨频道）· `image_generation`（多 provider） |
| 自省 | `self` · `recall`（记忆检索） |

## 记忆系统

设计信条：**记忆应当有生命力，但不应当混乱**。好的记忆不是一堆笔记，而是一套安静的注意力系统——记住值得记的，放下不再需要的，把经历沉淀为持久可用的知识。

### 分层存储

| 层 | 载体 | 角色 |
|----|------|------|
| 短期会话 | `session.messages` | 活跃对话的完整上下文 |
| 压缩归档 | `memory/history.jsonl` | 追加式、带游标的历史摘要（机器优先） |
| 长期知识 | `SOUL.md` · `USER.md` · `memory/MEMORY.md` | 语气人格 / 用户画像 / 项目事实 |
| 版本历史 | `GitStore`（内嵌 Git） | 长期文件的每次变更可追溯、可回滚 |

### 两阶段流转

**阶段一：Consolidator（压缩）**——当会话增长到逼近上下文窗口时，把最旧的安全片段摘要后追加进 `history.jsonl`。基于 Token 预算触发，跳过活跃任务。它不是最终记忆，而是塑造最终记忆的原料。

**阶段二：Dream（整合）**——按 `intervalH` 周期（默认 2 小时）或 `/dream` 手动触发。读取新增历史摘要与三个长期文件，先理解「哪些是新的、哪些已知」，再对长期文件做**最小外科手术式编辑**，而非整体重写。这让记忆不只是归档，而是被解释过的。

### 可检索、可审计、可回滚

- **向量检索**（可选 `[vector]` 依赖）：`recall` 工具经 sqlite-vec 对历史摘要、情景事件、语义事实做 top-k 相似检索；未安装时自动降级为全量注入策略，系统照常工作
- **审计**：`/dream-log [sha]` 查看每次 Dream 改动的 diff
- **回滚**：`/dream-restore [sha]` 恢复到任意一次变更之前——自动记忆很强大，但用户始终保有检查、理解与撤销的权利

详细机制与配置见 [docs/memory.md](./docs/memory.md)。

## 适用场景

### 适合

- **个人 AI 助手**：接入飞书/钉钉/微信，7×24 小时在线，记忆跨会话保留
- **开发辅助**：文件读写、Shell 执行、代码搜索、补丁应用——代理可自主完成多步任务
- **定时自动化**：自然语言调度，`/goal` 持续目标，重启后补执行
- **研究实验**：代码可读，核心循环可审计，适合研究工具使用、记忆策略、代理行为
- **编程式集成**：Python SDK 或 OpenAI 兼容 API 嵌入现有系统
- **多平台部署**：Docker、Linux 服务、macOS LaunchAgent

### 不适合

- 需要复杂 DAG 编排或工作流引擎的场景
- 需要多租户隔离的 SaaS 部署
- 不接受文件系统/Shell 访问的高沙箱要求环境

## 安装

```bash
# 从源码（最新特性）
git clone https://github.com/zhoulingquan/miniunicorn.git
cd miniunicorn
pip install -e .

# 可选附加依赖
pip install -e ".[api,vector,pdf,dev]"   # HTTP API / 向量记忆 / PDF 解析 / 测试
```

运行时依赖约 30 个 Python 包，无原生编译依赖（除 lxml 外）。

## 快速开始

**一条命令启动**——配置文件和工作区会自动初始化，LLM API Key 可以启动后在 WebUI 里配置。

```bash
miniunicorn gateway
# → 浏览器访问 http://127.0.0.1:8765
```

首次启动时没有 LLM 配置，对话功能暂不可用。在 WebUI 的 **设置 → 模型配置** 里填入任意 OpenAI 兼容提供商的 API Key（DeepSeek、OpenRouter、Moonshot 等），保存后即可对话——无需重启。

**其他启动方式**

```bash
# CLI 终端对话（需要先配置 LLM）
miniunicorn agent

# 仅 OpenAI 兼容 API 服务
miniunicorn serve

# 交互式配置向导（可选，用于预配置频道等）
miniunicorn onboard --wizard
```

**手动编辑配置**（可选）：配置文件位于 `~/.miniunicorn/config.json`，支持 `${VAR}` 环境变量替换。

## 编程式接入

### Python SDK

```python
from miniunicorn import Miniunicorn

bot = Miniunicorn.from_config()
result = await bot.run("总结这个仓库的架构", hooks=[MyHook()])
print(result.content)
print(result.tools_used)
```

### OpenAI 兼容 API

```bash
curl http://127.0.0.1:8765/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "default",
    "messages": [{"role": "user", "content": "你好"}],
    "stream": true
  }'
```

端点：`/v1/chat/completions`（支持 SSE 流式）、`/v1/models`、文件上传。

## 频道接入

| 频道 | 接入方式 | 扫码登录 |
|------|---------|---------|
| WebSocket | 内置 WebUI | — |
| 飞书 | App ID + App Secret | ✓ |
| 钉钉 | App Key + App Secret | — |
| 企业微信 | Bot ID + Bot Secret | — |
| 微信 | 扫码 | ✓ |
| QQ | App ID + App Secret | — |

频道通过 `pkgutil` 自动发现，支持入口点插件扩展。

## LLM 提供商

基于统一基类，支持：

- **OpenAI 兼容**：DeepSeek、OpenRouter、Moonshot/Kimi、MiniMax、VolcEngine、StepFun、LongCat、Azure、Bedrock、NVIDIA NIM、GitHub Copilot、LM Studio、Ollama、vLLM 等
- **OpenAI Responses API**：GPT-5 / o-series 推理模型
- **Anthropic**：Claude 系列，自适应思考与缓存优化
- **Fallback**：主模型失败自动切换备用
- **自动检测**：根据 API Key 识别提供商
- **行为声明式配置**：通过 `ProviderSpec` 的行为 flag 字段（如 `force_string_content`、`normalize_tool_call_ids`）声明 provider 特殊行为，消除代码中的硬编码分支

## 内置技能

Markdown + YAML frontmatter 定义，按需加载：

`cron` · `document-processing` · `github` · `image-generation` · `long-goal` · `memory` · `my` · `skill-creator` · `summarize` · `tmux` · `update-setup` · `weather`

## 测试与质量

约 185 个测试文件覆盖全部核心模块（agent 59 · channels 27 · tools 23 · utils 16 · providers 14 · cli/config/session/cron/security/pairing 等），`pytest-asyncio` 自动模式 + 覆盖率统计，`ruff` 静态检查。

```bash
pip install -e ".[dev]"
pytest
```

## 文档

### 核心文档

| 主题 | 链接 | 覆盖内容 |
|------|------|---------|
| 快速开始 | [quick-start.md](./docs/quick-start.md) | 安装、onboarding、首次运行 |
| 配置参考 | [configuration.md](./docs/configuration.md) | 提供商、工具、频道、MCP、运行时设置 |
| 聊天应用 | [chat-apps.md](./docs/chat-apps.md) | 频道接入详细说明 |
| WebUI | [../webui/README.md](./webui/README.md) | 内置浏览器 UI、局域网访问、Vite 开发 |
| 多实例 | [multiple-instances.md](./docs/multiple-instances.md) | 独立配置与工作区隔离 |
| CLI 参考 | [cli-reference.md](./docs/cli-reference.md) | 核心 CLI 命令与入口 |
| 聊天命令 | [chat-commands.md](./docs/chat-commands.md) | 斜杠命令与定时任务行为 |
| OpenAI API | [openai-api.md](./docs/openai-api.md) | 本地 API 端点与文件上传 |
| 部署 | [deployment.md](./docs/deployment.md) | Docker、Linux 服务、macOS LaunchAgent |

### 进阶文档

| 主题 | 链接 | 覆盖内容 |
|------|------|---------|
| 记忆系统 | [memory.md](./docs/memory.md) | 存储、整合、恢复机制 |
| Python SDK | [python-sdk.md](./docs/python-sdk.md) | 编程式调用 |
| 频道插件 | [channel-plugin-guide.md](./docs/channel-plugin-guide.md) | 自定义频道插件开发 |
| WebSocket | [websocket.md](./docs/websocket.md) | 实时 WebSocket 协议细节 |
| 图像生成 | [image-generation.md](./docs/image-generation.md) | 图像 provider、WebUI 图像模式 |
| 自省工具 | [my-tool.md](./docs/my-tool.md) | `my` 工具运行时状态 |

完整文档目录见 [docs/README.md](./docs/README.md)。

## 贡献

PR 欢迎。代码库刻意保持可读。

| 分支 | 用途 |
|------|------|
| `main` | 稳定发布 |
| `nightly` | 实验特性 |

详见 [CONTRIBUTING.md](./CONTRIBUTING.md)。

## 许可证

MIT — 见 [LICENSE](./LICENSE) 与 [THIRD_PARTY_NOTICES.md](./THIRD_PARTY_NOTICES.md)。

---

<div align="center">

<em>核心小，扩展在边缘，记忆即上下文。</em>

</div>
