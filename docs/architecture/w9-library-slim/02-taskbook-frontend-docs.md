# W9-2 任务书：前端与文档清理

> 前置：W9-1 后端删除已完成并全量通过。本批纯删除，禁止兼容层。

## 前端删除清单（webui/src，整文件）

1. `webui/src/components/settings/sections/WebSearchSettings.tsx`
2. `webui/src/components/settings/sections/ImageGenerationSettings.tsx`
3. `webui/src/components/settings/hooks/useWebSearchSection.ts`
4. `webui/src/components/settings/hooks/useImageGenerationSection.ts`

## 前端修改清单

- `webui/src/components/settings/SettingsView.tsx`（10 处引用）：删除两个 section 组件的 import 与挂载/tab 项。
- `webui/src/components/settings/hooks/useSettingsState.ts`（5 处）：删除相关 state / payload 字段。
- `webui/src/lib/types.ts`（2 处）：删除 WebSearch / ImageGeneration 相关类型定义。
- 前端测试（各 2 处引用）：
  - `webui/src/tests/thread-shell.test.tsx`
  - `webui/src/tests/settings-view.test.tsx`
  - `webui/src/tests/app-layout.test.tsx`
  按引用性质删用例或删断言，不留 skip。

## 文档清理

- `docs/image-generation.md`：整篇删除。
- `docs/configuration.md`：删除 web_search / deep_research / image_generation 相关章节与配置表条目（provider 表、示例 JSON 段）。保留 web_fetch（WebToolsConfig）相关内容。
- `docs/architecture/module-boundaries.md`：rg 检查是否提及三子包；若有（如模块登记表或工具库描述），同步删改为现状。**注意**：该文件有架构守护测试扫描（tests/agent/test_structured_memory_boundary.py 扫 docs/**/*.md 中无 legacy/迁移 语境的旧版 journal 存储文件名裸引用），改动时勿引入违规。
- 架构历史任务书（docs/architecture/ 下 w2~w8、trusted-evidence-plan、agent-core-tool-library-split-plan.md、agent-harness-*.md）：**不改**。
- 其他活文档（README、docs/*.md）：rg 检查提及，逐个判断删改；工具列表/功能列表里出现这三个工具的条目删除。

## 执行注意（防中断）

- 轮询/等待类命令必须每次带唯一变体（时间戳或计数器后缀），禁止连续执行 5 次以上完全相同的命令——hub 的 loopDetection 会在 5 次相同签名调用后终止会话。
- 单条命令控制在 25 秒内完成；更长的等待拆成多条带唯一变体的短命令。
- 执行前先看 `webui/package.json` 的 scripts 确认测试/构建命令名，不要臆测。
- 后端已重组出 `erza/webui/web_fetch_api.py`（WebFetch 保留功能的配置域模块）；前端 `web` 设置区块对应保留，勿误删。

## 验证步骤

1. TypeScript 编译：
   ```
   npx tsc -p webui/tsconfig.build.json --noEmit
   ```
2. 前端测试：
   ```
   npm --prefix webui test -- --run
   ```
   （以项目实际脚本为准，先查 webui/package.json 的 scripts。）
3. 残留扫描（全仓，docs/architecture 历史任务书除外）：
   ```
   rg -n "web_search|deep_research|image_generation|WebSearch|DeepResearch|ImageGeneration" webui/src docs README.md
   ```
   排除规则：docs/architecture/ 下 w2~w8 目录、trusted-evidence-plan/、agent-core-tool-library-split-plan.md、agent-harness-*.md 为历史存档可保留。`web_fetch` / `WebToolsConfig` 不属本任务，勿误删。

## 完成标准

- 4 个前端文件整删，3 个修改文件零残留。
- tsc 零错误，前端测试全绿。
- 文档与现状一致，历史任务书未动。
- 独立 commit，message 说明前端与文档清理范围。
