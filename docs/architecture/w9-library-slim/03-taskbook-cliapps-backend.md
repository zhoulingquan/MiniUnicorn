# W9-3 任务书：后端删除 CLI Apps 生态（apps/cli + run_cli_app 工具）

> 背景：本 agent 定位为企业固定工作流执行，CLI Apps（CLI-Anything 第三方工具受控运行层）用不到，按 W9 惯例彻底删除。
> 纯删除任务。禁止新增兼容层、stub、re-export。禁止改写架构历史任务书（w2~w8、trusted-evidence-plan、agent-core-tool-library-split-plan、agent-harness-*）。

## 保留边界（勿误删）

- `miniunicorn/apps/protocol.py`：**保留**。manifest 词汇被 `webui/mcp_presets_api.py` 复用（MCP presets 以 app_manifest 形态暴露）。`apps/__init__.py` 的 protocol re-export 保留，若其中有 cli 相关 re-export 则删除。
- `webui/mcp_presets_api.py`：保留（仅因 import apps.protocol 被扫描命中）。
- MCP presets 全链路（mcp.py 工具、mcp_runtime、mcp_presets_api、前端 MCP 半边）全部不动。

## 删除清单（整目录/整文件）

1. `miniunicorn/apps/cli/` 整个子包（__init__.py、service.py、utils.py）
2. `miniunicorn/tools/cli_apps.py`（run_cli_app 工具）
3. `miniunicorn/webui/cli_apps_api.py`
4. `tests/cli_apps/` 整个目录（test_gate.py、test_service.py、test_tool.py、test_utils.py）

## 修改清单（逐文件，行号为 2026-09-04 勘测值，先 rg 核实再改）

### miniunicorn/config/schema.py

- 删 ToolsConfig 的 `cli_apps` 字段（约 559-561 行）。
- 删 TYPE_CHECKING import `CliAppsToolConfig`（约 16 行）与底部 model_rebuild 段引用（约 809 行）。
- Base 忽略未知字段，用户 config.json 残留 `cli_apps` 段会被静默忽略，无需迁移。

### miniunicorn/agent/context.py（agent→apps.cli 横向依赖全拔除）

- 删 `from miniunicorn.apps.cli import utils as cli_app_utils`（17 行）。
- 删 `_session_extra` 中 `cli_app_utils.session_extra(metadata)` 合并项（36 行，保留 mcp_tools 部分）。
- 删 `cli_apps_enabled` 参数及其线程化：45、49-53、111、115、583 行（含 docstring 中 CLI Apps 说明）。

### miniunicorn/agent/loop.py

- 删 `cli_apps_enabled=_tc.cli_apps.enabled` 传参（约 530 行）。

### miniunicorn/agent/agent_generator.py

- 工具名单删 `"run_cli_app"`（约 38 行）。
- rg `run_cli_app` 全 agent/ 复查（runner_strategies.py 等若有名单项一并删）。

### miniunicorn/channels/websocket/channel.py

- 删 import 块：`from miniunicorn.webui.cli_apps_api import (cli_apps_action, cli_apps_payload, ...)` 与 `normalize_cli_app_mentions`（约 48-51 行）。
- 模块 docstring（6-7 行）中 test monkeypatch 目标列表移除 cli_apps_payload/cli_apps_action。
- 删 user_obj 的 `cli_apps` 透传（约 867-869 行）。
- 删 `normalize_cli_app_mentions(envelope.get("cli_apps"))` 归一化与 metadata 写入（约 1419-1421 行）。

### miniunicorn/channels/websocket/handlers/settings.py

- 删 cli-apps 相关路由（/api/settings/cli-apps 拉取与 action 端点）及其 import。rg `cli.?apps` 定位；若路由注册在 `_http_routes.py` 或别处，rg 全 channels/ 找注册点一并删。

### miniunicorn/session/manager.py

- 删 `[CLI App Attachment: ...]` 历史标记渲染分支（约 175-191 行）。

### miniunicorn/webui/transcript.py

- 删 `cliApps` 行渲染（约 722-724 行）。

## 附带引用测试（删用例或删 fixture，不留 skip）

- `tests/agent/test_context_builder.py`、`tests/agent/test_workspace_scope.py`（cli_apps_enabled 线程化相关）
- `tests/channels/test_websocket_envelope_media.py`、`tests/channels/test_websocket_http_routes.py`（cli-apps 端点/mention）
- `tests/session/test_session_manager_history.py`（CLI App Attachment 标记）
- `tests/tools/test_tool_loader.py`（cli_apps 发现门控）
- `tests/utils/test_webui_transcript.py`（cliApps 行）

## 验证步骤（顺序执行）

1. 冷导入：`.venv\Scripts\python.exe -c "import miniunicorn.agent, miniunicorn.config.schema, miniunicorn.channels.websocket.channel, miniunicorn.webui.mcp_presets_api; print('ok')"`
2. 守护测试：`.venv\Scripts\python.exe -m pytest tests/architecture/ -q`（预期 5 passed）
3. 全量回归（后台 detached 执行，勿前台阻塞）：`.venv\Scripts\python.exe -m pytest tests/ -q`（venv 无 pytest-retries 插件，不用 --retries）
4. 残留扫描（零命中；apps/protocol.py、mcp_presets_api.py 中 `miniunicorn.apps.protocol` 引用除外）：
   `rg -n "cli_apps|run_cli_app|CliApp|cli_apps_api|apps\.cli\b|CLI_ANYTHING" miniunicorn tests`

## 执行注意（防中断）

- 轮询/等待命令必须每次带唯一变体（时间戳/计数器），禁止连续 5 次相同命令（hub loopDetection 会终止会话）。
- 单条命令 25 秒内完成；长等待拆成多条带唯一变体的短命令。
- 不要提交 git，完成后报告验证结果。
