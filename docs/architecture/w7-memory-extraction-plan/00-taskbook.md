# W7-1 任务书:memory 家族搬家至顶层 `erza/memory/`

> 日期:2026-09-02 · 前置:W7-0 已完成(`5bf8fa1f`,memory 家族对 agent 核心依赖为零)
> 性质:纯搬家重构(同 W5-1/W6-1 模式):git mv + import 路径改写 + 守护手术,**零逻辑改动**
> 勘察:全仓程序化扫描完成,本文所有行号、文件清单均有实测依据

## 0. 红线(违反任何一条即失败)

1. **只用 git mv**,禁止"新建文件+删旧文件"(保历史:搬家后 `git log --follow` 必须能看到搬家前历史)
2. **零逻辑改动**:只改 import 行、路径字符串、守护白名单;不重排函数、不改签名、不动实现
3. **不留 shim/兼容层**:`erza.agent.memory` 命名空间彻底消失,无 re-export
4. **测试文件不搬家**:唯一例外是 `tests/agent/test_memory_module_split.py` → `tests/memory/test_memory_package_split.py`(它是本批次的包结构守护,随包走);其余 26 个测试文件**原地只改 import**
5. 历史文档(W4/W5/W6 的 plan 目录)一律不动;只更新 `docs/architecture/module-boundaries.md`(新增 §2.19)
6. 新增文档中**不得出现裸的 legacy journal 文件名字样**(架构守护会扫 docs/**/*.md;如确需提及,行内必须含"迁移"或"legacy"字样)
7. 验证门一过**立即 commit**(防中断丢工作)

## 1. 模块映射表(13 个文件)

| 旧路径 `erza/agent/` | 新路径 `erza/memory/` |
|---|---|
| memory.py(门面 23 行) | `__init__.py` |
| memory_models.py(740) | models.py |
| memory_sqlite_schema.py(352) | sqlite_schema.py |
| memory_repository.py(768) | repository.py |
| memory_recall.py(283) | recall.py |
| memory_lifecycle.py(692) | lifecycle.py |
| memory_extraction.py(137) | extraction.py |
| memory_jsonl_import.py(297) | jsonl_import.py |
| memory_audit_export.py(436) | audit_export.py |
| memory_store.py(912) | store.py |
| memory_consolidator.py(567) | consolidator.py |
| memory_dream.py(571) | dream.py |
| memory_backup.py(354) | backup.py |

注意:模块名同步瘦身(`memory_store` → `store`),消除 `memory.memory_store` 口吃。每个文件的 import 改写都走同一张表。

**导入改写总规则**(适用于所有文件,含函数级惰性 import 与字符串):

- `from erza.agent.memory_X import A, B` → `from erza.memory.<新名> import A, B`
- `from erza.agent.memory import A` → `from erza.memory import A`(门面名不变)
- `import erza.agent.memory_X as M` → `import erza.memory.<新名> as M`
- 字符串形式同步:`"erza.agent.memory_X"` → `"erza.memory.<新名>"`;`"erza/agent/memory_X.py"` → `"erza/memory/<新名>.py"`
- `importlib.import_module("erza.agent.memory_store")` / `find_spec(...)` 同表改写

## 2. 手术清单(生产代码,22 文件)

### 2.1 搬家 + 家族内部互引(13 文件)

先建目录再逐个 git mv(注意 memory.py 最后变成 `__init__.py`):

```powershell
New-Item -ItemType Directory -Force erza\memory | Out-Null
git mv erza/agent/memory_models.py         erza/memory/models.py
git mv erza/agent/memory_sqlite_schema.py  erza/memory/sqlite_schema.py
git mv erza/agent/memory_repository.py     erza/memory/repository.py
git mv erza/agent/memory_recall.py         erza/memory/recall.py
git mv erza/agent/memory_lifecycle.py      erza/memory/lifecycle.py
git mv erza/agent/memory_extraction.py     erza/memory/extraction.py
git mv erza/agent/memory_jsonl_import.py   erza/memory/jsonl_import.py
git mv erza/agent/memory_audit_export.py   erza/memory/audit_export.py
git mv erza/agent/memory_store.py          erza/memory/store.py
git mv erza/agent/memory_consolidator.py   erza/memory/consolidator.py
git mv erza/agent/memory_dream.py          erza/memory/dream.py
git mv erza/agent/memory_backup.py         erza/memory/backup.py
git mv erza/agent/memory.py                erza/memory/__init__.py
```

家族内部互引改写(实测清单,含函数级惰性 import,如 dream.py 的行 201/334/338/339):

- `__init__.py`(原 memory.py):4 处 import 指向 consolidator/dream/jsonl_import/store
- audit_export.py:行 43-44(models, sqlite_schema)
- backup.py:行 47-49(audit_export, models, sqlite_schema)
- consolidator.py:行 13(store)
- dream.py:行 21-22、201、334、338-339(models, store, extraction, lifecycle)
- extraction.py:行 17(models)
- jsonl_import.py:行 30、40、223(models, sqlite_schema, repository[惰性])
- lifecycle.py:行 20、39-42(models ×2, repository)
- recall.py:行 14、24(models, repository)
- repository.py:行 32、36、56(jsonl_import, models, sqlite_schema)
- sqlite_schema.py:行 20(models)
- store.py:行 18、31-32、178-184(jsonl_import, models[TC], repository[TC], audit_export[惰性], lifecycle[惰性], recall[惰性], repository[惰性])

### 2.2 agent 核心消费方(6 文件)

| 文件 | 行 | 改写 |
|---|---|---|
| agent/__init__.py | 6 | **删除整行** `from erza.agent.memory import Dream, MemoryStore`,并从 `__all__` 移除 `"MemoryStore"`、`"Dream"`(门面收窄,agent 公共面回归纯编排) |
| agent/autocompact.py | 15 | → `from erza.memory import Consolidator` |
| agent/context.py | 13-14 | → `from erza.memory import MemoryStore, WorkspaceMemoryRegistry` + `from erza.memory.models import MemoryScope, RecallQuery, ScopeKind` |
| agent/dream_trigger.py | 22, 25 | → `from erza.memory import count_pending_dream_entries` / `from erza.memory import Dream` |
| agent/loop.py | 24 | → `from erza.memory import Consolidator, Dream, MemoryStore` |
| agent/runtime_resources.py | 24 | → `from erza.memory import Consolidator, Dream, MemoryStore` |

`context.py:116` 的 MemoryStore 直接构造**本批不改**(保持零逻辑改动;注入化留给后续)。

### 2.3 agent 外消费方(2 文件)

| 文件 | 行 | 改写 |
|---|---|---|
| command/memory.py | 13-16 | 门面 + backup + lifecycle + models 四行,按映射表 |
| cli/_gateway_runner.py | 32 | → `from erza.memory import count_pending_dream_entries` |

### 2.4 脚本(1 文件)

`scripts/benchmark_memory_sqlite.py` 行 40-41、53-54:audit_export/models/repository/sqlite_schema 四行,按映射表。

## 3. 手术清单(测试,28 文件)

全部**原地改写**(§0 红线 4 例外除外),逐文件实测引用行:

- tests/agent/:test_consolidation_ratio.py(8)、test_consolidator.py(7/504/547)、test_context_structured_memory.py(11-13/64)、test_cursor_recovery.py(11)、test_dream_structured_memory.py(11-12/125-127/520/527/960-961/1035/1069)、test_dream_trigger.py(15)、test_git_store.py(252/258)、test_loop_consolidation_tokens.py(5)、test_memory_audit_export.py(12 处)、test_memory_backup.py(19-21/36/208-209)、test_memory_commands.py(19 处)、test_memory_extraction.py(10/14)、test_memory_jsonl_import.py(19/23/222/247/412)、test_memory_lifecycle.py(14/31/51/54)、test_memory_models.py(11/594)、test_memory_recall.py(13/30/37)、test_memory_repository.py(16/39/43)、test_memory_repository_contract.py(24)、test_memory_sqlite_schema.py(11-12)、test_memory_store.py(11-12/799/807)、test_onboard_logic.py(13)、test_reflection_structured.py(14/325)、test_workspace_memory_routing.py(22-23/63/432)
- tests/session/test_unified_session.py(323/355/389,函数级)
- tests/agent/test_structured_memory_boundary.py:见 §4 守护手术

### 3.1 唯一搬家的测试:test_memory_module_split.py → tests/memory/test_memory_package_split.py

git mv 后按映射表改写,并做**语义手术**(W7 改变了结构,断言要跟着变):

1. 所有 `erza.agent.memory*` 路径按映射表更新(`memory_facade` → `import erza.memory as memory_facade` 等)
2. `test_agent_package_reexport_identity`(行 43-45)**反转**:`erza.agent` 不再 re-export——改为断言 `not hasattr(erza.agent, "MemoryStore")` 且 `"MemoryStore" not in erza.agent.__all__`
3. `test_consumer_entry_points`(行 161-163):`erza.agent.Dream` 断言删除,改为 `not hasattr(erza.agent, "Dream")`;保留 `dream_trigger.count_pending_dream_entries is memory_dream.count_pending_dream_entries`(换新路径后仍成立)
4. `test_memory_py_*` 三个门面体积断言:`find_spec("erza.memory")` 取 `__init__.py`,阈值断言保留(≤60 行、无 class 定义)
5. `test_memory_store_standalone_import`:`import_module("erza.memory.store")`
6. **新增两条**:
   - `test_cold_import_loads_no_agent_modules`:subprocess 起新解释器执行 `import erza.memory`,然后检查 `sys.modules` 中不存在 `"erza.agent"` 也不存在任何 `"erza.agent.*"` 前缀模块(已实证:依赖闭包 gitstore/helpers/session.manager/bus/providers.base/ledger/prompt_templates 冷加载零 agent 模块;若 stderr 出现 config.schema 的 circular-import deferral warning 属预期,忽略)
   - `test_memory_package_is_agent_free`:AST 扫描 `erza/memory/**/*.py`,断言无任何 `erza.agent` import
7. tests/memory/ 目录结构**照抄 tests/ledger/**(若后者有 `__init__.py` 则同建)

## 4. 守护手术

### 4.1 tests/agent/test_structured_memory_boundary.py(6 处)

| 行 | 现状 | 改为 |
|---|---|---|
| 19-21 | 三行 import(agent.memory/lifecycle/models) | `erza.memory` / `erza.memory.lifecycle` / `erza.memory.models` |
| 164 | `_EXPLICIT_RUNTIME_FILES` 含 `"erza/agent/memory.py"` | 删除该项(该文件不存在了;家族文件由下面 glob 收集) |
| 171-175 | `_runtime_memory_files`:`agent.glob("memory_*.py")` | 改为 `(root/"erza"/"memory").glob("*.py")`(显式列表里 context/loop/reflection/command/schema 五项保留) |
| 268 | `_JOURNAL_MIGRATOR_SOURCES = ("erza/agent/memory_jsonl_import.py",)` | `("erza/memory/jsonl_import.py",)` |
| 297/300 | `_REPOSITORY_INSTANTIATION_ALLOWED` 两项 | `"erza/memory/store.py"` / `"erza/memory/jsonl_import.py"`(注释里的 W4-1 字样可保留,补一句 W7-1 搬迁说明) |
| 362 | `rel == "erza/agent/memory_repository.py"` | `rel == "erza/memory/repository.py"` |

### 4.2 tests/architecture/test_dependency_direction.py(2 处)

- 行 165 sink 集合 `{"providers", "utils", "security", "config", "bus", "ledger"}` → 加入 `"memory"`(锁死"memory 包禁 import agent"——W7-0 已把它做成事实,本条固化)
- 行 42 `BUSINESS_PACKAGES` → 加入 `"memory"`(memory 不 import composition,已实证)

### 4.3 docs/architecture/module-boundaries.md

新增 §2.19(格式仿 §2.18 ledger):位置 `erza/memory/`、门面 re-export 清单(MemoryStore/WorkspaceMemoryRegistry/Consolidator/Dream/count_pending_dream_entries/reflection_evidence_id + 私有名)、依赖仅 utils/config/session/bus/providers/ledger、生命周期所有者 agent(RuntimeResourceRegistry 持有)、**零 agent 依赖**由 test_dependency_direction 固化。同时在 §2.2 agent 节补一行:memory 家族已于 W7-1 外置(见 §2.19)。

## 5. 验证门(全过才 commit)

```powershell
# 门 1:零残留(源码/测试/脚本,退出码必须为 1 = 零命中)
rg -n "erza\.agent\.memory|erza/agent/memory" erza/ tests/ scripts/

# 门 2:三个入口顺序冷导入全部成功(各起子进程)
.venv\Scripts\python.exe -c "import erza.memory; print('ok')"
.venv\Scripts\python.exe -c "import erza.agent; print('ok')"
.venv\Scripts\python.exe -c "import erza.config.schema; print('ok')"

# 门 3:全量 pytest(后台 Start-Process 跑,轮询日志,勿同步等待)
.venv\Scripts\python.exe -m pytest tests/ -q
# 期望:0 failed;总数 ≥ 4144 passed(W7-0 基线 4144 + 新增 2 条守护)

# 门 4:双 ruff 零
.venv\Scripts\python.exe -m ruff check erza/ tests/ scripts/
.venv\Scripts\python.exe -m ruff format --check erza/ tests/ scripts/

# 门 5:历史追踪
git log --follow --oneline erza/memory/store.py | Measure-Object -Line   # 必须 > 1(能看到搬家前提交)
```

## 6. 反中断规程(经验固化,必守)

1. **轮询命令每次必须变体**:等待后台 pytest 时,轮询命令交替使用不同变体(如 `Get-Content log -Tail 2` 与 `Get-Content log -Last 3` 交替,或带递增序号/时间戳参数),**严禁同一命令连发 5 次**(会触发 mistake tracker 硬停,报 misleading 的 "aborted by another client")
2. 单条命令 25 秒内必须返回;长任务(全量 pytest 约 10 分钟)一律 `Start-Process` 后台化 + 轮询
3. 测试用 `.venv\Scripts\python.exe`(3.12);系统 Python 是 3.10 会在收集期 ImportError
4. 验证门一过立即 `git add` + `git commit`,不要攒

## 7. 提交

单个 commit,message 建议:

```
refactor(memory): extract memory family to top-level erza/memory package

Pure move (git mv) of 13 modules (6132 loc) out of agent/; module names
slimmed (memory_store -> store etc.). agent/__init__ no longer re-exports
MemoryStore/Dream. Guard surgery: boundary whitelist paths, sink-package
set gains memory (agent-import ban now enforced), new cold-import and
agent-free package guards.
```
