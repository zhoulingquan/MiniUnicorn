# W4-1:MemoryStore + WorkspaceMemoryRegistry → memory_store.py

> 前置依赖:tag `baseline-pre-w4`(HEAD `e5d62312`,4114 passed)。本批是 W4 系列基座批:最大的一个,但机械性最强(两段连续区整体搬迁,无分支手术)。
> 本批是**纯搬家重构**:零行为变更、零新抽象、消费者零改动。

## 一、现状锚点(以符号定位,行号为参考)

`miniunicorn/agent/memory.py` 共 2018 行:

| 锚点 | 行号(参考) | 说明 |
|---|---|---|
| 文件 docstring | 1 | `"""Memory system: pure file I/O store, lightweight Consolidator, and Dream processor."""` |
| import 块 | 3-48 | 含 `migrate_legacy_journal`(24-27)、`_atomic_rewrite_lines`(28)、helpers 家族(32-39)、TYPE_CHECKING 块(42-47:MemoryStatus/RecallQuery/RecallResult、StructuredMemoryRepository、StructuredMemoryConfig、LLMProvider、SessionManager) |
| 模块函数 | 55-97 | `_parse_datetime_loose`(55)、`reflection_evidence_id`(72)、`_dream_source_batch`(80)、`count_pending_dream_entries`(91)——**全部 Dream 专用,本批留在原位不动** |
| `class MemoryStore:` | 100-929 | ~830 行;class attr `_DEFAULT_MAX_HISTORY = 1000`(103);`__init__`(141-183,含 GitStore 构造、`_build_structured_stack`、`_export_audit_pending()` 收尾调用);懒导入点:memory_audit_export(242)、StructuredMemoryConfig(174) |
| `class WorkspaceMemoryRegistry:` | 930-971 | ~42 行,per-workspace 注册表 |
| 常量区 | 975-982 | `_RAW_ARCHIVE_MAX_CHARS = 16_000`(980,**被 MemoryStore:902 与 Consolidator:1239 共用,本批随迁**)、`_ARCHIVE_SUMMARY_MAX_CHARS = 8_000`(981,仅 Consolidator 用,**本批留在原位**)、`_HISTORY_ENTRY_HARD_CAP = 64_000`(982,仅 MemoryStore 用,**本批随迁**);978 行注释 `_HISTORY_ENTRY_HARD_CAP at append_history() ...` 随 `_HISTORY_ENTRY_HARD_CAP` 走 |
| `class Consolidator:` | 985-1517 | 本批不动;其对 `_RAW_ARCHIVE_MAX_CHARS` 的引用(1239)经门面命名空间继续可见(见下) |
| `class Dream:` | 1523-2018 | 本批不动 |

**依赖方向验证结论**:MemoryStore 区段对 Consolidator/Dream 零代码引用(仅注释提及)——基座先行成立。

## 二、变更方案

### 2.1 新建 `miniunicorn/agent/memory_store.py`

- 模块 docstring:`"""MemoryStore: pure file-I/O memory store layer (extracted from memory.py)."""`(单行即可)
- 头部:`from __future__ import annotations` + `import os, re, threading` + `from contextlib import suppress`(按实际引用裁剪)+ `tiktoken`、`filelock`、`loguru` + 下列 miniunicorn 导入中实际引用者:`migrate_legacy_journal`、`LegacyJournalImportError`(若 MemoryStore 区段引用)、`_atomic_rewrite_lines`、`session_key_base`、`Session`、`GitStore`、`GOVERNED_MEMORY_TRACKED_FILES`、helpers 家族中实际引用者、TYPE_CHECKING 块中实际引用者
- **裁剪基准**:新模块只保留其代码实际引用的 import;以 ruff check(未使用导入报错)逐项核实,不凭记忆
- 主体:`class MemoryStore:`(100-929 逐行照搬,含全部 docstring/注释/中文注释)→ `class WorkspaceMemoryRegistry:`(930-971 照搬)→ 模块级 `_RAW_ARCHIVE_MAX_CHARS` 与 `_HISTORY_ENTRY_HARD_CAP` 定义(含 978 注释行,放在两 class 之后、与原常量区相同的相对顺序)
- **禁止**:搬入任何 Dream 专用函数、`_ARCHIVE_SUMMARY_MAX_CHARS`、Consolidator/Dream 类

### 2.2 收缩 `miniunicorn/agent/memory.py`

- 删除 MemoryStore、WorkspaceMemoryRegistry 两个类定义及 `_RAW_ARCHIVE_MAX_CHARS`、`_HISTORY_ENTRY_HARD_CAP` 常量(975-982 中仅删这两条,保留 `_ARCHIVE_SUMMARY_MAX_CHARS`)
- import 块顶部新增:

```python
from miniunicorn.agent.memory_store import (  # noqa: F401
    MemoryStore,
    WorkspaceMemoryRegistry,
    _HISTORY_ENTRY_HARD_CAP,
    _RAW_ARCHIVE_MAX_CHARS,
)
```

- 文件其余部分(四个 Dream 模块函数、Consolidator、Dream、`_ARCHIVE_SUMMARY_MAX_CHARS`)逐字不动;Consolidator 内 `_RAW_ARCHIVE_MAX_CHARS` 引用经上述 import 继续可见
- 原 import 块中**仅被已搬出代码引用**的名字(如 `migrate_legacy_journal`、`_atomic_rewrite_lines`、`GitStore`、tiktoken/filelock 若 Consolidator/Dream 不用)从 memory.py 删除——以 ruff 未使用导入核实为准
- 保留行数预期:约 1146 行(2018 - 872)

### 2.3 测试 monkeypatch 目标调整(2 处 + 1 处守护白名单,纯导入路径调整)

| 位置 | 现状 | 调整为 |
|---|---|---|
| tests/agent/test_memory_store.py:799-811 | `import miniunicorn.agent.memory as memory_module` + `monkeypatch.setattr(memory_module, "migrate_legacy_journal", spy_migrate, raising=False)` | import 目标改为 `miniunicorn.agent.memory_store`(变量名可留 `memory_module`,仅换导入路径),setattr 调用形式不变 |
| tests/agent/test_reflection_structured.py:341-355 | `import miniunicorn.agent.memory as memory_module` + `monkeypatch.setattr(memory_module, "_atomic_rewrite_lines", fail_file_rewrite)` | 同上,导入路径改为 `miniunicorn.agent.memory_store` |
| tests/agent/test_structured_memory_boundary.py:300-306(实施时发现,任务书设计时遗漏) | `_REPOSITORY_INSTANTIATION_ALLOWED` 白名单硬编码 `"miniunicorn/agent/memory.py"`——守护 `StructuredMemoryRepository(` 实例化点;MemoryStore 的 `_build_structured_stack` 随迁后,实例化点落入 `memory_store.py`,守护失败 | 白名单条目改为 `"miniunicorn/agent/memory_store.py"`(附注释 `# Moved with MemoryStore in W4-1`);守护语义(唯一 SQLite 仓库实例化点)零变更;`_EXPLICIT_RUNTIME_FILES`(166 行)不改——`memory_glob = agent.glob("memory_*.py")` 已自动覆盖新文件且 memory.py 仍存在 |

**断言、测试逻辑、测试数据零修改**——只换被 patch 的模块对象。

### 2.4 新测试 `tests/agent/test_memory_module_split.py`

1. `test_facade_identity_store`:`miniunicorn.agent.memory.MemoryStore is miniunicorn.agent.memory_store.MemoryStore`;`WorkspaceMemoryRegistry` 同款断言
2. `test_memory_store_standalone_import`:`importlib.import_module("miniunicorn.agent.memory_store")` 成功且不在 sys.modules 出现循环特征(直接成功导入即为通过)
3. `test_constants_live_in_store_module`:`memory_store._HISTORY_ENTRY_HARD_CAP == 64_000`、`memory_store._RAW_ARCHIVE_MAX_CHARS == 16_000`;且 `miniunicorn.agent.memory._HISTORY_ENTRY_HARD_CAP is memory_store._HISTORY_ENTRY_HARD_CAP`
4. `test_agent_package_reexport_identity`:`miniunicorn.agent.MemoryStore is memory_store.MemoryStore`(agent/__init__ 经门面 re-export 的链路完整)
5. `test_memory_py_shrunk`:`inspect.getsource` 或行数统计,`memory.py` 行数 < 1150 且 `memory_store.py` 行数 < 1000
6. `test_consolidator_still_resolves_shared_constant`:导入 `miniunicorn.agent.memory` 命名空间的 `_RAW_ARCHIVE_MAX_CHARS` 后,Consolidator 模块内引用同源(以门面 identity 断言间接覆盖,即第 3 条;此条可并入第 3 条,不必单独存在)

(实际落成 5 个用例即可,第 6 条为指导性说明。)

## 三、不可触碰清单

- W0-W3 全部保护成果(见总览第五节红线 7);runner.py、loop.py 零改动
- Consolidator(985-1517)、Dream(1523-1819)、四个 Dream 模块函数(55-97)、`_ARCHIVE_SUMMARY_MAX_CHARS` 零改动
- memory_models.py、memory_repository.py、memory_jsonl_import.py、memory_audit_export.py、reflection.py 零改动
- 8 个消费者文件的 import 语句零改动
- 既有测试断言零修改(唯一例外:2.3 列出的 2 处模块路径调整)

## 四、验收清单

- [ ] 全量测试绿(passed ≥ 4114 + 新增 5)且既有断言零修改;ruff check / format --check 零告警
- [ ] `from miniunicorn.agent.memory import MemoryStore, WorkspaceMemoryRegistry, _HISTORY_ENTRY_HARD_CAP` 与全部原符号路径仍可用(2.3 的 2 处路径调整除外)
- [ ] memory_store.py 独立可导入,无循环 import
- [ ] 纯搬家核对:对基线与工作区做代码行多重集合对比,删除侧恰为 MemoryStore/Registry/两常量,新增侧恰为新模块 docstring、精简 import、门面 re-export 块
- [ ] 共享常量唯一性:`_RAW_ARCHIVE_MAX_CHARS` 全库仅 memory_store.py 一处定义
- [ ] 偏差逐条说明(含 2.3 调整逐处确认)
