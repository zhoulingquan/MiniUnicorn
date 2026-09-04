# W9-6 任务书：仓库卫生 + apps 包归位

> 状态：待实施（Cline）。
> 本批是 W9 系列收尾后的卫生批次，两件事：清理违反 gitignore 的历史追踪文件；把 CLI Apps 删除后仅剩协议残骸的 `miniunicorn/apps/` 包归位。
> **裁决记录**：本批 supersedes `03-taskbook-cliapps-backend.md` 中「apps/protocol.py 保留」的决定——保留的前提（apps 生态存在）已消失，唯一消费方是 mcp_presets_api.py，归位为内联私有辅助函数。

## 批次 W9-6a：仓库卫生（一个提交）

### A1. untrack `docs/superpowers/`（16 个已追踪文件）

现状：`.gitignore` 已声明 `docs/superpowers/` 为 AI 过程产物应忽略，但这批 2026-07/08 的 plans/specs 在规则添加前已入库，gitignore 对已追踪文件无效。内容已被 W 系列实现取代。

操作：
```
git rm -r --cached docs/superpowers/
```

- 仅移出索引，**本地文件保留**（不删盘上文件）
- 16 个文件清单（`git ls-files docs/superpowers` 校验）：plans/ 下 7 个 + specs/ 下 9 个
- 验证：`git ls-files docs/superpowers` 零输出；`git status` 显示 16 个 staged deletion；`docs/superpowers/` 目录在盘上仍存在

### A2. node_modules 规则升级

现状：`.gitignore` 只写了 `webui/node_modules/`；仓库根目录的 `node_modules/`（vitest 误建缓存，仅含 `.vite/vitest/`）未被覆盖，永远出现在 untracked。

操作：
1. `.gitignore` 中 `webui/node_modules/` 一行改为 `node_modules/`（裸模式匹配任意层级，webui 下的同样覆盖）
2. 删除本地根目录 `node_modules/`：`Remove-Item -Recurse -Force node_modules`

验证：`git check-ignore node_modules` 命中；`git status` 干净（无 untracked node_modules）；`git check-ignore webui/node_modules` 仍命中。

### A3. 提交

```
chore(repo): untrack superpowers AI artifacts; ignore node_modules at any level
```

git add 仅限：.gitignore + 16 个 superpowers 删除（git rm --cached 已暂存）。本地 node_modules 删除不入库（本来就是 untracked）。

## 批次 W9-6b：apps 包归位（一个提交）

### B1. 现状锚点

- `miniunicorn/apps/` 仅 2 文件 53 行：`__init__.py`（9 行 re-export：APP_PROTOCOL_SCHEMA、app_manifest）+ `protocol.py`（44 行：APP_PROTOCOL_SCHEMA 常量、compact_dict()、app_manifest()）
- 唯一生产消费方：`miniunicorn/webui/mcp_presets_api.py:19` `from miniunicorn.apps.protocol import app_manifest, compact_dict`
- 使用点（同文件）：compact_dict 于 557/569/590/598/627/636/644 行（7 处），app_manifest 于 580/619 行（2 处）
- 零测试直接引用 app_manifest/compact_dict/miniunicorn.apps
- **外部契约**：manifest 字典的 `"schema": "agent-app.v1"` 被 `webui/src/lib/types.ts:381` 类型化、被 `tests/webui/test_mcp_presets_api.py:306` 断言——内联后必须原样保留该值
- 守护测试 `tests/architecture/test_dependency_direction.py` 的 sink 清单不含 apps；`docs/architecture/module-boundaries.md` 未注册 apps（无需改）

### B2. 操作

1. `miniunicorn/webui/mcp_presets_api.py`：
   - 删 19 行的 `from miniunicorn.apps.protocol import app_manifest, compact_dict`
   - 在 `_preset_manifest` 函数定义紧上方内联三个私有定义（从 protocol.py 原样搬移，仅去公开导出身份）：
     ```python
     _APP_MANIFEST_SCHEMA = "agent-app.v1"

     def _compact_dict(values: dict[str, Any]) -> dict[str, Any]:
         """Drop empty optional values while preserving explicit booleans and zeros."""
         ...

     def _app_manifest(*, ...) -> dict[str, Any]:
         """Build a stable app manifest dictionary."""
         ...
     ```
     - docstring 保留原文；`_app_manifest` 内部 `"schema": APP_PROTOCOL_SCHEMA` 改用 `_APP_MANIFEST_SCHEMA`
   - 9 处调用点同步改名：`compact_dict(` → `_compact_dict(`、`app_manifest(` → `_app_manifest(`
2. 删除整个包：`git rm -r miniunicorn/apps/`
3. **零其他改动**：不改 mcp_presets_api 其余逻辑、不改任何测试、不改 module-boundaries.md

### B3. 验证门槛（全部通过才提交）

- `rg "miniunicorn\.apps" miniunicorn/ tests/` → 零输出
- `rg "agent-app\.v1" miniunicorn/` → 仅 mcp_presets_api.py 一处（`_APP_MANIFEST_SCHEMA` 定义）
- `.venv\Scripts\python.exe -m pytest tests/webui/ -q` → 全过（test_mcp_presets_api.py:306 的 schema 断言是本批的契约守护）
- `.venv\Scripts\python.exe -m ruff check miniunicorn/webui/mcp_presets_api.py` → 零输出
- `.venv\Scripts\python.exe -m ruff format --check miniunicorn/webui/mcp_presets_api.py` → 零输出
- 全量 `.venv\Scripts\python.exe -m pytest tests/ -q` → **已知环境失败 1 个**：`test_exec_guard_allows_public_urls[curl -s -o /dev/null -w "%{http_code}" https://www.google.com]`（本机 hosts 把 google.com 劫持为 127.0.0.1，SSRF 守护按设计拦截；与本批无关）。除此之外 0 failed；passed 基线 4007（本批不增删测试，应仍为 4007）、skipped 29 不变。任何**其他**失败 → 停止报告
- `git status` → 仅声明的路径：mcp_presets_api.py 修改 + miniunicorn/apps/ 删除

### B4. 提交

```
refactor(webui): inline manifest helpers from apps.protocol; drop apps package
```

## 禁改清单

- `docs/superpowers/` 盘上文件内容（只 untrack 不删改）
- `docs/architecture/` 既有任务书（历史记录不可变）
- mcp_presets_api.py 除声明改动外的任何逻辑
- 任何测试文件的断言（test_mcp_presets_api.py:306 尤其不许动）

## 验收自检

- [ ] `git ls-files docs/superpowers` 零输出，盘上文件仍在
- [ ] `git check-ignore node_modules` 与 `git check-ignore webui/node_modules` 均命中
- [ ] 根目录 node_modules/ 已从盘上删除
- [ ] `rg "miniunicorn\.apps" miniunicorn/ tests/` 零输出
- [ ] tests/webui/ 全过，schema 契约断言原样通过
- [ ] 全量 pytest 除已知环境失败外 0 failed，passed=4007、skipped=29
- [ ] 两个提交各自独立，git log 干净
