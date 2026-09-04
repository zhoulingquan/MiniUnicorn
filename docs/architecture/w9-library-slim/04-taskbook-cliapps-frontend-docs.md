# W9-4 任务书：CLI Apps 前端与文档清理

> 前置：W9-3 后端删除完成。纯删除任务，禁止兼容层。
> 保留边界：Apps 设置页保留 MCP Presets 半边（页面从"CLI Apps + MCP Presets 一体化"瘦身为 MCP Presets 管理）；MCP presets 全链路不动。

## 删除清单（整文件）

1. `webui/src/lib/cli-app-events.ts`（notifyCliAppsChanged）
2. `webui/src/lib/app-mentions-fixtures.ts`（@mention 测试 fixture）

## 修改清单

- `webui/src/components/settings/sections/AppsSettings.tsx`：剥离 CLI Apps 半边——删 fetchCliApps/runCliAppAction/notifyCliAppsChanged、AppKind "cli" 分支、CliAppInfo/CliAppsPayload 类型和 initialCliApps prop；页面保留 MCP Presets 列表。顶部注释同步改写。
- `webui/src/components/settings/SettingsView.tsx`：删 initialCliApps 注入（rg 定位）。
- `webui/src/components/thread/ThreadComposer.tsx`：删 CLI App @mention 集成（约 1 处引用）。
- `webui/src/components/thread/AgentActivityCluster.tsx`：删 cliApps 展示（约 3 处引用）。
- `webui/src/hooks/useMiniunicornStream.ts`：删 OutboundCliAppMention 类型引用与 cliApps 出站参数（15、409、1009 行附近）。
- `webui/src/lib/miniunicorn-client.ts`：删 cliApps 引用（约 2 处）。
- `webui/src/lib/api.ts`：删 fetchCliApps/runCliAppAction 与 `/api/settings/cli-apps` 端点（225、236 行附近）。
- `webui/src/lib/types.ts`：删 CliAppInfo/CliAppsPayload/OutboundCliAppMention 等类型。
- i18n（zh-CN/en common.json）：删 cliApps 相关键（zh 约 18 处、en 约 8 处；含 apps 导航名若为"CLI 应用"则改为 MCP 预设相关表述，导航键本身保留）。
- 前端测试（删用例/断言，不留 skip）：`useMiniunicornStream.test.tsx`（4）、`miniunicorn-client.test.ts`（2）、`api.test.ts`（3）、`agent-activity-cluster.test.tsx`（7）、apps/settings 相关测试中 cli 半边。

## 文档清理

- `docs/configuration.md`：删 cli_apps / CLI Apps 章节（tools.cli_apps.* 字段表、示例 JSON）。
- `README.md` / `README.en.md`：删 CLI Apps 功能提及；工具计数若有（21→20）同步。
- `docs/architecture/module-boundaries.md`：**删除 agent/context → apps.cli 横向依赖例外条目**（该依赖已随 W9-3 拔除）；apps 包描述改为 protocol-only（MCP presets manifest 词汇）。注意勿引入无 legacy 语境的旧版 journal 存储文件名裸引用（架构守护测试扫描 docs/**/*.md）。
- `docs/miniunicorn-business-agent-code-analysis-report.md`：rg 检查提及，若为活文档则同步删改 CLI Apps 段落。
- 架构历史任务书（w2~w8、trusted-evidence-plan、agent-core-tool-library-split-plan、agent-harness-*、w6-vocab-sink-plan）：**不改**。

## 验证步骤

1. TypeScript 编译：`npx tsc -p webui/tsconfig.build.json --noEmit`（exit 0）
2. 前端测试（先看 webui/package.json scripts 确认命令；分片跑避免单条超时）
3. ESLint 改动文件零 error
4. 残留扫描（零命中，排除 docs/architecture 历史任务书）：
   `rg -n "cliApps|cli_apps|runCliApp|run_cli_app|CliApp|CLI 应用|CLI Apps" webui/src docs README.md README.en.md`
   （MCP presets 相关命中不属于本任务，勿动）

## 执行注意（防中断）

- 轮询/等待命令必须每次带唯一变体（时间戳/计数器），禁止连续 5 次相同命令。
- 单条命令 25 秒内完成；长等待拆多条短命令。
- 不要提交 git，完成后报告验证结果。
