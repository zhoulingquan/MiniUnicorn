# W9 阶段：Library 精简——可选子包剥离

> 日期：2026-09-04 · 前置：W8 收官（core + library 终态达成，4147 passed）
> 决策：三个可选子包（web_search / deep_research / image_generation）**彻底删除**，git 历史可找回。

## 背景

Library 精简评估发现三个可选子包合计 27 文件 / 3.5k loc，占 tools 包 31%：

| 子包 | 文件 | loc | 默认状态 |
|---|---|---|---|
| tools/web_search/ | 12 | 1213 | enable=True |
| tools/deep_research/ | 5 | 873 | enable=True，依赖 web_search |
| tools/image_generation/ | 10 | 1447 | enabled=False |

连带耦合面（勘察于 2026-09-04）：

- config/schema.py：ToolsConfig 三个字段（568-582 行）经 `_lazy_default` 惰性引用子包 Config 类；文件底部 model_rebuild 段（828-834 行附近）三处 import。
- webui：web_search_api.py(180 loc)、image_generation_api.py(145 loc) 两个专属 API 模块；settings_api.py 聚合 payload 与 re-export。
- channels/websocket/handlers/settings.py：web_search_update / image_generation_update 两个路由 + imports。
- agent：runner_strategies.py:34 与 agent_generator.py:40-41 的工具名单字符串（非 import 耦合）。
- 前端 webui/src：WebSearchSettings.tsx、ImageGenerationSettings.tsx、useWebSearchSection.ts、useImageGenerationSection.ts 四个专属文件；SettingsView.tsx / useSettingsState.ts / types.ts 挂载与类型。
- 测试：test_deep_research.py、test_image_generation.py 专属；另有约 7 个文件字符串级附带引用。
- 文档：docs/image-generation.md 整篇；docs/configuration.md 相关章节。

## 兼容性结论（勘察确认，执行时不必重查）

1. `config/schema.py` 的 `Base`（26 行）未设 extra 策略，pydantic 默认 ignore 未知字段——删除 ToolsConfig 三字段后，用户 config.json 里残留的 `web_search` / `deep_research` / `image_generation` 段会被**静默忽略，不报错**。不需要任何迁移代码。
2. 当前用户 `~/.miniunicorn/config.json` 无这三字段配置。
3. ToolLoader 的 `_SKIP_MODULES` 不含三个子包名（它们经 pkgutil 目录扫描发现）；删除目录后工具自然消失，loader 零改动。
4. `pyproject.toml` 的 `[project.entry-points."miniunicorn.tools"]` 为注释示例，无需改动。
5. deep_research 依赖 web_search（`DeepResearchTool.enabled` 检查 web_search 可用性），两者同批删除，无残留依赖。

## 批次划分

- **批次 1（01-taskbook-backend.md）**：后端 Python 删除（子包 + config + webui API + channels 路由 + agent 名单 + 后端测试），pytest 全量验证。
- **批次 2（02-taskbook-frontend-docs.md）**：前端 webui/src 清理 + 文档清理，vitest 验证。

## 原则

- 纯删除，不新增任何兼容层/抽象（反抽象税原则）。
- 架构历史任务书（docs/architecture/ 下 w2~w8 系列、trusted-evidence-plan、agent-core-tool-library-split-plan.md、agent-harness-*.md）为历史记录，**不改写**。
- module-boundaries.md 与 configuration.md 为活文档，需同步。
- 每批独立验证，pytest 必须用 `.venv\Scripts\python.exe`。
