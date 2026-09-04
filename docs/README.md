# MiniUnicorn Docs

本目录是 MiniUnicorn 的文档源，跟随仓库一起演进。

## Core Docs

从这里开始：安装、日常使用与部署。

| 主题 | 文档 | 内容 |
|---|---|---|
| 安装与快速开始 | [`quick-start.md`](./quick-start.md) | 安装、初始化引导与首次运行 |
| 聊天平台接入 | [`chat-apps.md`](./chat-apps.md) | 接入飞书 / 钉钉 / 企微 / 微信 / QQ / WebSocket |
| 配置参考 | [`configuration.md`](./configuration.md) | Providers、工具、频道、MCP 与运行时设置 |
| WebUI | [`../webui/README.md`](../webui/README.md) | 内置浏览器 UI、局域网访问、Vite 开发服务器 |
| 多实例 | [`multiple-instances.md`](./multiple-instances.md) | 用独立 config 和 workspace 运行多个 bot |
| CLI 参考 | [`cli-reference.md`](./cli-reference.md) | 核心 CLI 命令与常用入口 |
| 会话内指令 | [`chat-commands.md`](./chat-commands.md) | 斜杠指令与定时任务行为 |
| OpenAI 兼容 API | [`openai-api.md`](./openai-api.md) | 本地 API 端点、请求格式与文件上传 |
| 部署 | [`deployment.md`](./deployment.md) | Docker、Linux service 与 macOS LaunchAgent |

## Advanced Docs

需要深度定制、集成或扩展时参考。

| 主题 | 文档 | 内容 |
|---|---|---|
| 记忆系统 | [`memory.md`](./memory.md) | MiniUnicorn 如何存储、整合与恢复记忆 |
| Python SDK | [`python-sdk.md`](./python-sdk.md) | 以库形式在 Python 中调用 MiniUnicorn |
| 频道插件指南 | [`channel-plugin-guide.md`](./channel-plugin-guide.md) | 构建并测试自定义聊天频道插件 |
| WebSocket 频道 | [`websocket.md`](./websocket.md) | 实时 WebSocket 接入与协议细节 |
| 自定义工具 | [`my-tool.md`](./my-tool.md) | 通过 `my` 工具查看与调整运行时状态 |
