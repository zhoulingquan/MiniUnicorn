# W4-3:Dream → memory_dream.py,memory.py 收缩为纯门面

> 前置依赖:W4-1、W4-2 已合并。基线测试数以 W4-2 报告为准。本批是 W4 系列收官批。
> 本批是**纯搬家重构**:零行为变更、零新抽象、消费者零改动。

## 一、现状锚点(以符号定位,行号为 W4-2 合并后的参考)

`miniunicorn/agent/memory.py`(W4-2 后约 612 行)残留三块:

| 锚点 | 原始行号(基线) | 说明 |
|---|---|---|
| 四个 Dream 模块函数 | 55-97 | `_parse_datetime_loose`(55)、`reflection_evidence_id`(72)、`_dream_source_batch`(80)、`count_pending_dream_entries`(91,签名 `store: "MemoryStore"`,读两个 cursor,被 dream_trigger.py / cli/_gateway_runner.py 经门面导入)——**仅 Dream 使用** |
| 门面 re-export 块 | 头部 | W4-1/W4-2 已积累的 `from memory_store import ...`、`from memory_consolidator import ...` |
| `class Dream:` | 1523-2018(基线) | ~496 行;class 属性 8 条(1526-1533:`_HISTORY_ENTRY_PREVIEW_MAX_CHARS`、`_REFLECTION_ENTRY_PREVIEW_MAX_CHARS`、`_MIN_EVIDENCE_PREVIEW_CHARS`、`_EVIDENCE_EXCERPT_MAX_CHARS`、`_SUMMARY_MAX_RECORDS`、`_SUMMARY_RECORD_MAX_CHARS`、`_SUMMARY_MAX_CHARS`、`_PROMPT_SAFETY_TOKENS`);`__init__`(1535,参数 store/provider/model/max_batch_size=20/context_window_tokens/max_completion_tokens,含 `isinstance(provider_max_tokens, int)` 回退逻辑);`set_provider`(1558);`async run()`(1582,公共入口);静态/工具方法 `_partition_identity`、`_entry_timestamp`、`_structured_summary`、`_history_prompt_line`、`_reflection_prompt_line`、`_render_user_prompt`、`_bounded_user_prompt`(嵌套 `render` 1684)、`_fit_bounded_batch`、`async _run_structured_batch`(1773,嵌套 `take_history`/`take_reflections`,**三处 `store._export_audit_pending()` 调用**:1818/1872/2017) |

**跨模块私有访问**(原样保留):三处 `store._export_audit_pending()`;`count_pending_dream_entries` 对 store 的 `read_unprocessed_history`/`get_last_dream_cursor`/`read_unprocessed_reflections`/`get_last_reflections_cursor` 调用。

## 二、变更方案

### 2.1 新建 `miniunicorn/agent/memory_dream.py`

- 模块 docstring:`"""Dream: offline knowledge distillation into structured memory (extracted from memory.py)."""`(单行)
- 头部:`from __future__ import annotations` + `hashlib`、`re`、`datetime`/`timezone`、typing 家族中实际引用者 + `render_template`、`call_purpose`/`CallPurpose`、`truncate_text` 等 helpers 家族中实际引用者 + TYPE_CHECKING 块(`MemoryStore`、`LLMProvider`——`from __future__ import annotations` 下注解惰性;`count_pending_dream_entries` 的 `store: "MemoryStore"` 为字符串注解,TYPE_CHECKING 即可)
- 主体顺序:四个模块函数(55-97 逐行照搬,含 docstring)→ `class Dream:`(逐行照搬,含 8 条 class 属性、全部 docstring、嵌套函数)
- **禁止**:搬 MemoryStore/Consolidator/常量;"修复" `_export_audit_pending` 的私有可见性;改动批次有界化逻辑

### 2.2 `miniunicorn/agent/memory.py` 收缩为纯门面

- 删除四个模块函数、`class Dream:` 全部、以及文件残留的 Consolidator 时代 import
- 最终形态(约 40-60 行):

```python
"""Memory system facade: re-exports the split memory modules (store / consolidator / dream)."""

from miniunicorn.agent.memory_consolidator import (  # noqa: F401
    Consolidator,
    _ARCHIVE_SUMMARY_MAX_CHARS,
)
from miniunicorn.agent.memory_dream import (  # noqa: F401
    Dream,
    _dream_source_batch,
    _parse_datetime_loose,
    count_pending_dream_entries,
    reflection_evidence_id,
)
from miniunicorn.agent.memory_store import (  # noqa: F401
    MemoryStore,
    WorkspaceMemoryRegistry,
    _HISTORY_ENTRY_HARD_CAP,
    _RAW_ARCHIVE_MAX_CHARS,
)
```

- 门面符号表完整清单(10 + 转发 2):`MemoryStore`、`WorkspaceMemoryRegistry`、`Consolidator`、`Dream`、`count_pending_dream_entries`、`reflection_evidence_id`、`_dream_source_batch`、`_parse_datetime_loose`、`_HISTORY_ENTRY_HARD_CAP`、`_RAW_ARCHIVE_MAX_CHARS`、`_ARCHIVE_SUMMARY_MAX_CHARS`,另从 `memory_jsonl_import` 转发 `LegacyJournalImportError` 与 `migrate_legacy_journal`(原 memory.py 顶部即导入这两个名字,保持命名空间兼容)
- **禁止**:门面里写任何逻辑(除 import 与 docstring);删除任何 compat re-export(项目红线:禁止删除 compat re-export,用 noqa 保留)

### 2.3 消费者零改动核验(本批无 monkeypatch 调整)

`agent/__init__.py`、`loop.py`、`runtime_resources.py`、`dream_trigger.py`、`autocompact.py`、`context.py`、`command/memory.py`、`cli/_gateway_runner.py` 的 import 语句一个字符都不改——`from miniunicorn.agent.memory import X` 经门面全部继续成立。若发现某消费者**必须**改动才能工作,说明搬家有错,回滚重查。

### 2.4 新测试(追加到 `tests/agent/test_memory_module_split.py`)

1. `test_final_facade_identity`:10 个符号逐一经 `is` 断言门面与定义模块同源(含 4 个私有名字)
2. `test_memory_py_is_pure_facade`:memory.py 行数 ≤ 60;且源码文本不含 `class `(以 `Path.read_text` 断言 `"class " not in src`)
3. `test_jsonl_import_reexports`:`memory.LegacyJournalImportError is memory_jsonl_import.LegacyJournalImportError`、`migrate_legacy_journal` 同款
4. `test_dream_helpers_moved_with_class`:`memory_dream.reflection_evidence_id({"reflection_id": "rfl_" + "a"*32})` 返回该 id(纯函数冒烟,不动 store)
5. `test_consumer_entry_points`:导入 `miniunicorn.agent`(其 `Dream`/`MemoryStore` 与定义模块 identity)与 `miniunicorn.agent.dream_trigger`(其 `count_pending_dream_entries` 与 `memory_dream` 版本 identity)

## 三、不可触碰清单

- W4-1/W4-2 成果零改动:memory_store.py、memory_consolidator.py 本批不动(除非 import 顺序问题的机械修复,须在报告说明)
- 协同模块(memory_models/repository/jsonl_import/audit_export/reflection)零改动
- 8 个消费者文件零改动(见 2.3)
- 既有测试断言零修改(本批无例外:monkeypatch 调整已在 W4-1/W4-2 完成)

## 四、验收清单

- [ ] 全量测试绿(passed ≥ W4-2 基线 + 新增 5)且既有断言零修改;ruff 零告警
- [ ] memory.py 为纯门面(≤60 行、零 class、零函数体)
- [ ] 消费者 import 路径全部未动(`git diff --stat` 中不含 8 个消费者文件)
- [ ] 三处 `store._export_audit_pending()` 调用原样存在于 memory_dream.py
- [ ] 纯搬家多重集合核对:删除侧恰为四函数 + Dream + 残留 import,新增侧恰为 memory_dream.py 主体与门面块
- [ ] 偏差逐条说明
