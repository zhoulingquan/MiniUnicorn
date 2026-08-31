# W4-3:Dream → memory_dream.py,memory.py 收缩为纯门面

> 前置依赖:W4-2 已合并(`2b322b9d`,**4124 passed / 0 failed**)。本批是 W4 系列收官批。
> 本批是**纯搬家重构**:零行为变更、零新抽象、消费者零改动。
> 行号锚点已按 W4-2 合并后的 memory.py(共 584 行)刷新;**行号仅为参考值,定位以符号为准**。

## 一、现状锚点(W4-2 合并后)

| 锚点 | 行号(参考) | 说明 |
|---|---|---|
| 文件头 docstring | 1 | `"""Memory system: pure file I/O store, lightweight Consolidator, and Dream processor."""`——本批改为门面描述(见 2.2) |
| import 块 | 3-32 | `hashlib`、`re`、`dataclasses_replace`、`datetime`/`timezone`、`logger`、`CallPurpose`/`call_purpose`、`session_key_base`、`estimate_message_tokens`、`render_template`、TYPE_CHECKING 块(21-22:`MemoryStatus`、`LLMProvider`);23-32 为 W4-1/W4-2 门面块(consolidator + store,`# noqa: F401`) |
| **残留横幅** | 34-36 | `# MemoryStore — pure file I/O layer`——W4-1 遗留的过期注释(类已迁走),现悬于 Dream 函数上方;**本批随删除区清除,不带入新文件** |
| 四个 Dream 模块函数 | 39-82 | `_parse_datetime_loose`(39)、`reflection_evidence_id`(56)、`_dream_source_batch`(64)、`count_pending_dream_entries`(75,签名 `store: "MemoryStore"` 字符串注解) |
| Dream 横幅 | 84-86 | `# Dream — ...` 区段横幅,随类迁入新文件 |
| `class Dream:` | 89-584 | ~496 行;class 属性 8 条(`_HISTORY_ENTRY_PREVIEW_MAX_CHARS` 等);`__init__`、`set_provider`、`async run()`、`_partition_identity`、`_entry_timestamp`、`_structured_summary`、`_history_prompt_line`、`_reflection_prompt_line`、`_render_user_prompt`、`_bounded_user_prompt`(嵌套 `render`)、`_fit_bounded_batch`(**`estimate_message_tokens` 6 处调用点 ~273-300**)、`async _run_structured_batch`(嵌套 `take_history`/`take_reflections`,**三处 `store._export_audit_pending()` 调用**) |

**import 分工**(已程序化核查;最终以 ruff 为准):

| 归属(全部迁入 memory_dream.py) | 名字 |
|---|---|
| 标准库 | `hashlib`、`re`、`dataclasses_replace`、`datetime`、`timezone` |
| 三方 | `logger`(loguru,11 处使用) |
| miniunicorn 运行时 | `CallPurpose`/`call_purpose`、`session_key_base`(157 行)、`estimate_message_tokens`(273-300)、`render_template`(502) |
| TYPE_CHECKING | `MemoryStatus`(21,注解 171 + 方法内局部导入 214)、`LLMProvider`、`MemoryStore`(75 行字符串注解,TYPE_CHECKING 即可——Dream 区段**无** MemoryStore 运行时引用,与 W4-2 的 Consolidator 不同) |

**守护面核查结论**(已验证):Dream 区段(39-584)零 legacy journal 文件名字符串、零 `StructuredMemoryRepository(` 实例化;`memory_*.py` glob 守护自动覆盖 memory_dream.py,其 import 根(miniunicorn/loguru/标准库)不在向量后端禁止清单。

**monkeypatch 核查结论**(已验证):Dream 相关测试(test_dream_structured_memory.py、test_dream_trigger.py、test_reflection_structured.py)对 memory 命名空间**零** monkeypatch——本批无测试调整(区别于 W4-1 的 2 处与 W4-2 的 2 行 import)。

## 二、变更方案

### 2.1 新建 `miniunicorn/agent/memory_dream.py`

- 模块 docstring:`"""Dream: offline knowledge distillation into structured memory (extracted from memory.py)."""`(单行)
- 头部:`from __future__ import annotations` + 上表全部 import + TYPE_CHECKING 块(`MemoryStatus`、`LLMProvider`、`MemoryStore`)
- 主体顺序:四个模块函数(39-82 逐行照搬,含 docstring)→ Dream 横幅(84-86)→ `class Dream:`(89-584 逐行照搬,含 8 条 class 属性、全部 docstring、嵌套函数、三处 `store._export_audit_pending()` 原样)
- **禁止**:搬 MemoryStore/Consolidator/常量;"修复" `_export_audit_pending` 私有可见性;改动批次有界化逻辑;写入 34-36 的过期横幅

### 2.2 `miniunicorn/agent/memory.py` 收缩为纯门面

- 删除 34-584 全部(残留横幅、四函数、Dream 横幅、Dream 类)与随之不再使用的 import
- 最终形态(约 35-45 行):

```python
"""Memory system facade: re-exports the split memory modules (store / consolidator / dream)."""

from miniunicorn.agent.memory_consolidator import (  # noqa: F401
    _ARCHIVE_SUMMARY_MAX_CHARS,
    Consolidator,
)
from miniunicorn.agent.memory_dream import (  # noqa: F401
    Dream,
    _dream_source_batch,
    _parse_datetime_loose,
    count_pending_dream_entries,
    reflection_evidence_id,
)
from miniunicorn.agent.memory_jsonl_import import (  # noqa: F401
    LegacyJournalImportError,
    migrate_legacy_journal,
)
from miniunicorn.agent.memory_store import (  # noqa: F401
    _HISTORY_ENTRY_HARD_CAP,
    _RAW_ARCHIVE_MAX_CHARS,
    MemoryStore,
    WorkspaceMemoryRegistry,
)
```

- jsonl 转发说明:基线 memory.py 命名空间含这两个名字(W4-1 时被 ruff 以未使用为由裁掉);终态门面按"禁止删除 compat re-export"惯例恢复转发——**属恢复基线命名空间,非新增面**
- **禁止**:门面里写任何逻辑(除 import 与 docstring);删除任何 compat re-export

### 2.3 消费者零改动核验(本批无测试调整)

`agent/__init__.py`、`loop.py`、`runtime_resources.py`、`dream_trigger.py`、`autocompact.py`、`context.py`、`command/memory.py`、`cli/_gateway_runner.py` 的 import 语句一个字符都不改。既有测试的 `from miniunicorn.agent.memory import Dream, MemoryStore, count_pending_dream_entries, _dream_source_batch, reflection_evidence_id`(test_dream_structured_memory.py 等 3 个文件,含 519/526/959 三处函数局部导入)经门面全部继续成立。若发现某消费者或既有测试**必须**改动才能工作,说明搬家有错,回滚重查。

### 2.4 新测试(追加到 `tests/agent/test_memory_module_split.py`,现 10 个用例)

1. `test_final_facade_identity`:11 个符号(MemoryStore、WorkspaceMemoryRegistry、Consolidator、Dream、count_pending_dream_entries、reflection_evidence_id、_dream_source_batch、_parse_datetime_loose、_HISTORY_ENTRY_HARD_CAP、_RAW_ARCHIVE_MAX_CHARS、_ARCHIVE_SUMMARY_MAX_CHARS)逐一经 `is` 断言门面与定义模块同源
2. `test_memory_py_is_pure_facade`:memory.py 行数 ≤ 60;且源码文本不含 `class `(以 `Path.read_text` 断言,`"class " not in src`)
3. `test_jsonl_import_reexports`:`memory.LegacyJournalImportError is memory_jsonl_import.LegacyJournalImportError`、`migrate_legacy_journal` 同款
4. `test_dream_helpers_moved_with_class`:`memory_dream.reflection_evidence_id({"reflection_id": "rfl_" + "a" * 32})` 返回该 id(纯函数冒烟)
5. `test_consumer_entry_points`:`miniunicorn.agent.Dream is memory_dream.Dream`(agent 包 re-export 链路)、`miniunicorn.agent.dream_trigger` 可导入且其模块级 `count_pending_dream_entries` 为 memory_dream 版本 identity

## 三、不可触碰清单

- W4-1/W4-2 成果零改动:memory_store.py、memory_consolidator.py 本批不动
- 协同模块(memory_models/repository/jsonl_import/audit_export/reflection)零改动
- 8 个消费者文件零改动(见 2.3)
- 既有测试断言零修改(本批无例外:monkeypatch 调整已在 W4-1/W4-2 完成,已核查 Dream 符号无命名空间级 patch)
- 守护测试零改动(已核查无暴露;若守护测试失败,视为搬家出错,停下报告——不得自行改守护白名单)

## 四、验收清单

- [ ] 全量测试绿(passed ≥ 4124 + 新增 5)且既有断言零修改;ruff 零告警
- [ ] memory.py 为纯门面(≤60 行、零 class、零函数体)
- [ ] 消费者 import 路径全部未动(`git diff --stat` 中不含 8 个消费者文件)
- [ ] 三处 `store._export_audit_pending()` 调用原样存在于 memory_dream.py
- [ ] 纯搬家多重集合核对:删除侧恰为四函数 + Dream + 迁出 import,新增侧恰为 memory_dream.py 主体与门面块
- [ ] 偏差逐条说明(含 34-36 过期横幅清除、jsonl 转发恢复两项预期内偏差)
