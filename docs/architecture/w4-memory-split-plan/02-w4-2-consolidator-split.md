# W4-2:Consolidator → memory_consolidator.py

> 前置依赖:W4-1 已合并(`20789f21`,4119 passed / 0 failed)。本批基线:**4119**。
> 本批是**纯搬家重构**:零行为变更、零新抽象、消费者零改动。
> 行号锚点已按 W4-1 合并后的 memory.py(共 1129 行)刷新;**行号仅为参考值,定位以符号为准**。

## 一、现状锚点(W4-1 合并后)

| 锚点 | 行号(参考) | 说明 |
|---|---|---|
| 文件头 docstring | 1 | 保持不变(三类职责描述在 W4-3 收门面时再改写) |
| import 块 | 3-36 | 含 helpers 家族(`estimate_message_tokens`、`estimate_prompt_tokens_chain`、`find_legal_message_start`、`truncate_text`)、`CallPurpose`/`call_purpose`、`Session`、tiktoken、asyncio、weakref、`dataclasses_replace`、`session_key_base`、`render_template`;31-36 为 W4-1 门面块(`from memory_store import ...`,`# noqa: F401`) |
| 四个 Dream 模块函数 | 43-90 | **本批不动**(W4-3 搬) |
| `_ARCHIVE_SUMMARY_MAX_CHARS` | 93 | 唯一使用点 Consolidator.archive(`max_chars=_ARCHIVE_SUMMARY_MAX_CHARS`,约 405 行);**本批随迁** |
| `class Consolidator:` | 96-632 | ~537 行;class 属性:`_MAX_CONSOLIDATION_ROUNDS`(99)、`_SAFETY_BUFFER`(101)、`_HYGIENE_THROTTLE`(106)、`_CHECKPOINT_RATIO`(114)、`_VERBATIM_RECENT_USER_MSGS`(262);方法:`__init__`(116,10 个参数,含 ratio 断言 131-132)、`set_provider`(150)、`get_lock`(161)、`pick_consolidation_boundary`(165)、`_full_unconsolidated_history`(188,**`estimate_message_tokens` 调用点 ~195**)、`_replay_overflow_boundary`(203)、`_consolidate_replay_overflow`(235)、`_extract_verbatim_recent`(264)、`_persist_last_summary`(298)、`estimate_session_prompt_tokens`(310,**`estimate_message_tokens` 第二调用点 ~316**)、`_input_token_budget`(342)、`_truncate_to_token_budget`(346,`_RAW_ARCHIVE_MAX_CHARS` 使用点 ~350)、`archive`(360,**373 行 `MemoryStore._format_messages(messages)` 运行时类引用**;`self.store._archive_identity` 跨模块私有访问)、`_checkpoint_threshold`(431)、`maybe_consolidate_by_tokens`(441,公共入口)、`compact_idle_session`(557) |
| `class Dream:` | 634-1129 | 本批不动 |

**import 分工**(已程序化核查,供裁剪参考;最终以 ruff 为准):

| 归属 | 名字 |
|---|---|
| 仅 Consolidator 用 | `tiktoken`、`asyncio`、`weakref`、`estimate_message_tokens`、`estimate_prompt_tokens_chain`、`find_legal_message_start`、`truncate_text` |
| Consolidator 与 Dream 共用 | `call_purpose`/`CallPurpose`、`render_template`、`re`、`logger` |
| 仅 Dream/残留段用(留在 memory.py) | `dataclasses_replace`、`session_key_base`、`hashlib`(helpers 段用) |

**守护面核查结论**(W4-1 教训,已验证):Consolidator 区段(96-632)**零** `StructuredMemoryRepository(` 实例化、零 legacy journal 文件名字符串——守护测试(白名单/扫描类)不受本批影响;`test_runtime_memory_imports_never_touch_vector_backends` 的 `memory_*.py` glob 自动覆盖新文件,其 import 根与原文件相同(tiktoken 不在禁止清单,现状已通过)。

## 二、变更方案

### 2.1 新建 `miniunicorn/agent/memory_consolidator.py`

- 模块 docstring:`"""Consolidator: LLM-driven session-history consolidation (extracted from memory.py)."""`(单行)
- 头部:`from __future__ import annotations` + 仅 Consolidator 用的标准库/三方导入(`asyncio`、`weakref`、tiktoken、loguru)+ helpers 家族四个名字 + `call_purpose`/`CallPurpose`、`render_template`(按实际引用,ruff 核实)+ TYPE_CHECKING 块(`LLMProvider`、`SessionManager`、`Session`——注解惰性,TYPE_CHECKING 即可)
- **运行时导入(关键)**:`from miniunicorn.agent.memory_store import MemoryStore, _RAW_ARCHIVE_MAX_CHARS`——`MemoryStore` 必须运行时导入(373 行 `MemoryStore._format_messages(messages)` 是运行时类引用,非注解),`_RAW_ARCHIVE_MAX_CHARS` 是运行时值;**不得在本文件重定义任何常量**。循环安全:memory_store.py 不导入 memory_consolidator,依赖单向
- 主体:`_ARCHIVE_SUMMARY_MAX_CHARS = 8_000  # LLM-produced consolidation summary`(放 class 前,与原文件"常量在 class 前"的相对顺序一致)→ `class Consolidator:` 逐行照搬(含 class 属性、全部 docstring、ratio 断言、中文注释、`__init__` 中的 WeakValueDictionary 注释)
- **禁止**:搬 Dream、四个 Dream 函数;"修复" `_archive_identity` 私有可见性;改动锁池/归档计数器语义

### 2.2 收缩 `miniunicorn/agent/memory.py`

- 删除 `_ARCHIVE_SUMMARY_MAX_CHARS`(93)与 `class Consolidator:`(96-632)全部
- import 块新增门面 re-export(ruff isort 会置于 memory_store 块之前):

```python
from miniunicorn.agent.memory_consolidator import (  # noqa: F401
    _ARCHIVE_SUMMARY_MAX_CHARS,
    Consolidator,
)
```

- 原 import 块中仅被 Consolidator 引用的名字(`estimate_message_tokens`、`estimate_prompt_tokens_chain`、`find_legal_message_start`、`truncate_text`、tiktoken、asyncio、weakref、`Session` 等)以 ruff 未使用导入核实后删除;共用名字(`call_purpose`、`render_template` 等)保留
- W4-1 门面块(memory_store 导入)保持不动——`MemoryStore` 等 re-export 仍需服务消费者
- Dream 与四个模块函数逐字不动
- 保留行数预期:约 585 行(1129 - 538 - ruff 裁剪)

### 2.3 测试 monkeypatch 目标调整(7 处,纯导入路径调整)

`estimate_message_tokens` 的补丁点改 patch `miniunicorn.agent.memory_consolidator` 命名空间。**已核实两文件的 `memory_module` 引用仅 import 行 + setattr 行,改 import 行即全覆盖**:

| 文件 | 现状 | 调整 |
|---|---|---|
| tests/agent/test_loop_consolidation_tokens.py | 行 5 `import miniunicorn.agent.memory as memory_module` + **6 处** setattr(行 55/81/120/159/191/253;行 81 为 `token_map` 变体) | import 改为 `import miniunicorn.agent.memory_consolidator as memory_module`;6 处 setattr 逐字不动 |
| tests/agent/test_consolidation_ratio.py | 行 8 `import miniunicorn.agent.memory as memory_module` + 1 处 setattr(行 89) | 同上,仅改 import 行 |

**断言、测试逻辑、lambda 体、token_map 数据零修改**。

### 2.4 新测试(追加到 `tests/agent/test_memory_module_split.py`)

1. `test_facade_identity_consolidator`:`miniunicorn.agent.memory.Consolidator is miniunicorn.agent.memory_consolidator.Consolidator`
2. `test_shared_constant_single_definition`:`memory_consolidator._RAW_ARCHIVE_MAX_CHARS is memory_store._RAW_ARCHIVE_MAX_CHARS`(同源导入,非重定义)
3. `test_archive_summary_constant_identity`:`miniunicorn.agent.memory._ARCHIVE_SUMMARY_MAX_CHARS is memory_consolidator._ARCHIVE_SUMMARY_MAX_CHARS == 8_000`(门面 identity 断言;与 W4-3 终态门面符号表一致,避免下批再改本测试)
4. `test_memory_py_shrunk_further`:memory.py 行数 < 620
5. `test_consolidator_token_estimate_patchable_via_defining_module`(可选,轻量可行才落成):构造最小 fake 验证 `estimate_message_tokens` 经 memory_consolidator 命名空间可替换;不可轻量构造则省略并在报告说明——2.3 调整后的既有 7 测试通过即为行为覆盖

(实际落成 4-5 个用例。)

## 三、不可触碰清单

- W4-1 成果:memory_store.py 本批零改动(共享常量已就位,只导入)
- Dream 段(634-1129)、四个 Dream 模块函数(43-90)零改动
- `_archive_identity` 等跨模块私有访问原样保留
- 协同模块(memory_models/repository/jsonl_import/audit_export/reflection)与 8 个消费者文件零改动
- 既有测试断言零修改(唯一例外:2.3 列出的 7 处中仅 import 行路径调整)
- 守护测试零改动(已核查无暴露;若实施中守护测试失败,视为搬家出错,停下报告)

## 四、验收清单

- [ ] 全量测试绿(passed ≥ 4119 + 新增)且既有断言零修改;ruff 零告警
- [ ] `from miniunicorn.agent.memory import Consolidator` 仍可用且 identity 成立
- [ ] `_RAW_ARCHIVE_MAX_CHARS` 全库仍仅 memory_store.py 一处定义;memory_consolidator.py 为导入
- [ ] monkeypatch 语义等价:7 个 token 估算测试经新模块目标通过,lambda 体与断言零修改
- [ ] `MemoryStore._format_messages` 运行时引用在新文件中经运行时导入成立(全量测试中的 Consolidator 归档路径覆盖)
- [ ] 纯搬家多重集合核对:删除侧恰为 Consolidator + `_ARCHIVE_SUMMARY_MAX_CHARS` + 迁出 import,新增侧恰为新模块 docstring、import 块、运行时导入、门面 re-export
- [ ] 偏差逐条说明
