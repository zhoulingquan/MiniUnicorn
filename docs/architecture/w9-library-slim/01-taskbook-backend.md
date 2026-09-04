# W9-1 任务书：后端删除 web_search / deep_research / image_generation

> 纯删除任务。禁止新增兼容层、stub、re-export。禁止改写架构历史任务书。

## 删除清单（整目录/整文件）

1. `miniunicorn/tools/web_search/`（12 文件）
2. `miniunicorn/tools/deep_research/`（5 文件）
3. `miniunicorn/tools/image_generation/`（10 文件，含 providers/ 子目录）
4. `miniunicorn/webui/web_search_api.py`
5. `miniunicorn/webui/image_generation_api.py`
6. `tests/agent/tools/test_deep_research.py`
7. `tests/agent/tools/test_image_generation.py`

## 修改清单（逐文件）

### miniunicorn/config/schema.py

- 删除 ToolsConfig 的三个字段定义：`web_search`（约 568-572 行）、`deep_research`（约 573-577 行）、`image_generation`（约 578-582 行）。
- 删除文件顶部 TYPE_CHECKING / 前向引用块中三处 import（约 17-23 行：DeepResearchConfig、ImageGenerationConfig、WebSearchConfig）。
- 删除文件底部 model_rebuild 段中三处 import 与引用（约 828-834 行）。
- 全文 rg 确认无 `web_search|deep_research|image_generation|WebSearchConfig|DeepResearchConfig|ImageGenerationConfig` 残留。

### miniunicorn/webui/settings_api.py

- 删除 `from .image_generation_api import (...)` 与 `from .web_search_api import (...)` 两个 import 块。
- 删除 `payload.update(web_search_payload(config))` 与 `payload.update(image_generation_payload(config))`（约 71/74 行）。
- 删除 `__all__` 中的 `image_generation_payload`、`update_image_generation_settings`、`update_web_search_settings`、`web_search_payload`（约 95-110 行）。
- 模块 docstring 中提到 web_search_api 的说明（约 7 行）同步删改。

### miniunicorn/channels/websocket/handlers/settings.py

- 删除 imports 中 `update_image_generation_settings`、`update_web_search_settings`（约 19/25 行）。
- 删除路由函数 `web_search_update`（约 162 行起）与 `image_generation_update`（约 173 行起）。
- 检查路由注册表：若有这两个路由的 URL 注册项（如 `/api/settings/web_search`、`/api/settings/image_generation`），一并删除。

### miniunicorn/agent/runner_strategies.py

- 第 34 行工具名单删除 `"web_search",` 一项。

### miniunicorn/agent/agent_generator.py

- 第 40-41 行工具名单删除 `"web_search",` 与 `"deep_research",` 两项。

### miniunicorn/webui/mcp_presets_api.py

- 第 228 行附近注释提到 "brave-search / tavily 已移至 web_search backends"：该注释指向已删除的功能，改写或删除该注释。若周边有 brave-search / tavily 相关 MCP preset 逻辑仅为 web_search 服务，一并评估删除；若是独立 MCP preset 功能则仅清理注释。

### 附带引用测试（逐个判断，删引用或删用例，不删整文件）

对以下文件 rg 检索 `web_search|deep_research|image_generation|WebSearch|DeepResearch|ImageGeneration`：

- `tests/webui/test_mcp_presets_api.py`
- `tests/agent/tools/test_self_tool.py`
- `tests/channels/test_websocket_channel.py`
- `tests/providers/test_extra_body_config.py`
- `tests/agent/test_acceptance_semantics.py`
- `tests/agent/test_progress_policy.py`
- `tests/agent/test_step_acceptance.py`

处理原则：
- 若测试用例专测这三个工具/配置段 → 删该用例。
- 若仅是 fixture / config dict 里的多余键 → 删该键。
- 若是断言工具列表包含 "web_search" → 改为断言不含（或直接删该断言行）。
- 禁止留下 skip 标记或 xfail 占位。

### 特别检查：self.py（my 工具）

`miniunicorn/tools/self.py` 若在配置展示/设置白名单中含 `web_search` / `image_generation` 键（之前 W2-1 建立的 allow-list），删除对应键与展示分支。rg 确认。

## 验证步骤（顺序执行）

1. 冷导入验证：
   ```
   .venv\Scripts\python.exe -c "import miniunicorn.agent, miniunicorn.config.schema, miniunicorn.webui.settings_api, miniunicorn.channels.websocket.handlers.settings; print('ok')"
   ```
2. 守护测试：
   ```
   .venv\Scripts\python.exe -m pytest tests/architecture/ -q
   ```
   预期 5 passed。
3. 全量回归（后台执行，勿前台阻塞）：
   ```
   .venv\Scripts\python.exe -m pytest tests/ -q --retries 20
   ```
   预期全绿。失败数应仅来自漏删引用，修复后复跑。
4. 残留扫描（必须零命中，docs/ 与 webui/src 除外——属批次 2）：
   ```
   rg -n "web_search|deep_research|image_generation|WebSearch|DeepResearch|ImageGeneration" miniunicorn tests
   ```
   注意：`tools/web.py` 的 WebFetchTool 与 `WebToolsConfig` 属 web_fetch（保留功能），名字相近勿误删；扫描命中 `web_search` 字样的才算本任务残留。

## 完成标准

- 上述 7 项整删完成，7 个修改文件零残留。
- 全量 pytest 通过（数量较 4147 减少属预期：删除了专属测试）。
- git 状态干净，改动一次性提交，commit message 说明删除范围与理由。
