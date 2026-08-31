# W4-2:Consolidator → memory_consolidator.py

> 前置依赖:W4-1 已合并(MemoryStore 落位 `memory_store.py`)。基线测试数以 W4-1 报告为准(≥ 4119)。
> 本批是**纯搬家重构**:零行为变更、零新抽象、消费者零改动。

## 一、现状锚点(以符号定位,行号为 W4-1 合并后的参考)

`miniunicorn/agent/memory.py`(W4-1 后约 1146 行):

| 锚点 | 说明 |
|---|---|
| 文件头 docstring | 保持不变(三类职责描述在 W4-3 收门面时再改写) |
| import 块 | W4-1 后已含 `from miniunicorn.agent.memory_store import ...` 门面导入;`estimate_message_tokens`、`estimate_prompt_tokens_chain`、`find_legal_message_start`、`truncate_text`、`strip_think`(helpers 家族)仍在本文件——本批随 Consolidator 迁出 |
| `class Consolidator:` | 约 985-1517 原行号区;class 属性 `_MAX_CONSOLIDATION_ROUNDS`(988)、`_SAFETY_BUFFER`(990)、`_HYGIENE_THROTTLE`(995)、`_CHECKPOINT_RATIO`(1003)、`_VERBATIM_RECENT_USER_MSGS`(1151);方法:`__init__`(1005,9 个参数:store/provider/model/sessions/context_window_tokens/build_messages/get_tool_definitions/max_completion_tokens/consolidation_ratio/checkpoint_ratio,含 ratio 断言 1031-1032)、`set_provider`、`get_lock`(WeakValueDictionary 锁池)、`pick_consolidation_boundary`、`_full_unconsolidated_history`(**1072 行 `estimate_message_tokens(message)` 调用点**)、`_replay_overflow_boundary`、`_consolidate_replay_overflow`、`_extract_verbatim_recent`、`_persist_last_summary`、`estimate_session_prompt_tokens`(**含 `estimate_message_tokens` 第二调用点**)、`_input_token_budget`、`_truncate_to_token_budget`(`_RAW_ARCHIVE_MAX_CHARS` 使用点 1239)、`archive`(**1291 行 `self.store._archive_identity(messages)` 跨模块私有访问**)、`_checkpoint_threshold`、`maybe_consolidate_by_tokens`(公共入口)、`compact_idle_session` |
| `_ARCHIVE_SUMMARY_MAX_CHARS` | 常量区仅存此一条;唯一使用点 Consolidator.archive 的 `max_chars=_ARCHIVE_SUMMARY_MAX_CHARS`(1294) |
| `class Dream:` | 本批不动 |

**跨模块私有访问**(搬家后保持原样,不"顺手"改名):`self.store._archive_identity(...)`、对 store 上 git/`_file_cache` 相关私有成员的一切既有访问。

## 二、变更方案

### 2.1 新建 `miniunicorn/agent/memory_consolidator.py`

- 模块 docstring:`"""Consolidator: LLM-driven session-history consolidation (extracted from memory.py)."""`(单行)
- 头部:`from __future__ import annotations` + `import asyncio, weakref`(按实际引用)+ loguru + `Session` + TYPE_CHECKING 块(`MemoryStore`、`LLMProvider`、`SessionManager`——文件有 `from __future__ import annotations`,参数注解为惰性字符串,TYPE_CHECKING 即可;若发现非注解性运行时引用(默认值/isinstance)则改为直接导入) + helpers 家族中实际引用者(`estimate_message_tokens`、`estimate_prompt_tokens_chain`、`find_legal_message_start`、`truncate_text`、`strip_think`)
- **共享常量**:`from miniunicorn.agent.memory_store import _RAW_ARCHIVE_MAX_CHARS`(运行时值,必须直接导入;**不得在本文件重定义**)
- `call_purpose`/`CallPurpose`、`dataclasses_replace`、`suppress` 等仅在实际引用时导入(以 ruff 核实)
- 主体:`class Consolidator:` 逐行照搬(含 class 属性、全部 docstring、ratio 断言、中文注释)+ 文件级 `_ARCHIVE_SUMMARY_MAX_CHARS = 8_000  # LLM-produced consolidation summary`(注释随行)
- **禁止**:搬 Dream、四个 Dream 函数、MemoryStore;"修复" `_archive_identity` 的私有可见性

### 2.2 收缩 `miniunicorn/agent/memory.py`

- 删除 `class Consolidator:` 全部与 `_ARCHIVE_SUMMARY_MAX_CHARS`
- import 块新增门面 re-export:

```python
from miniunicorn.agent.memory_consolidator import Consolidator  # noqa: F401
```

- 原 import 块仅被 Consolidator 引用的名字(`estimate_message_tokens`、`estimate_prompt_tokens_chain`、`find_legal_message_start`、`strip_think`、`CallPurpose`/`call_purpose`、weakref、Session 等——以 Dream 段实际引用为准逐项核实)迁出或删除
- Dream 与四个模块函数逐字不动
- 保留行数预期:约 612 行(1146 - 534)

### 2.3 测试 monkeypatch 目标调整(6 处,纯导入路径调整)

`estimate_message_tokens` 的 6 个补丁点全部改 patch `miniunicorn.agent.memory_consolidator` 命名空间:

| 文件 | 位置 | 调整 |
|---|---|---|
| tests/agent/test_loop_consolidation_tokens.py | 文件头 `import miniunicorn.agent.memory as memory_module` + 5 处 `monkeypatch.setattr(memory_module, "estimate_message_tokens", ...)`(行 55/120/159/191/253 附近) | 新增或改 import 为 `miniunicorn.agent.memory_consolidator`(变量名可沿用 `memory_module`,仅换导入路径);5 处 setattr 形式不变 |
| tests/agent/test_consolidation_ratio.py | 文件头同上 + 1 处 setattr(行 89 附近) | 同上 |

**断言、测试逻辑、lambda 体零修改**。若某文件其余测试仍需 patch 原 memory 模块的其他符号,允许同时保留两个模块 import(命名清晰即可)。

### 2.4 新测试(追加到 `tests/agent/test_memory_module_split.py`)

1. `test_facade_identity_consolidator`:`miniunicorn.agent.memory.Consolidator is miniunicorn.agent.memory_consolidator.Consolidator`
2. `test_shared_constant_single_definition`:`memory_consolidator._RAW_ARCHIVE_MAX_CHARS is memory_store._RAW_ARCHIVE_MAX_CHARS`(同源,非重定义)
3. `test_archive_summary_constant_moved`:`memory_consolidator._ARCHIVE_SUMMARY_MAX_CHARS == 8_000`,且 `miniunicorn.agent.memory` 命名空间**不再**含 `_ARCHIVE_SUMMARY_MAX_CHARS`(以 `not hasattr(memory, "_ARCHIVE_SUMMARY_MAX_CHARS")` 断言,防止门面误留死符号)——若实现选择门面也 re-export 该常量,则改为 identity 断言并在此说明
4. `test_memory_py_shrunk_further`:memory.py 行数 < 650
5. `test_consolidator_token_estimate_patchable_via_defining_module`:构造最小 fake(直接调 `Consolidator.estimate_session_prompt_tokens` 不可行则跳过函数级验证,以 2.3 调整后的既有 6 测试通过作为行为覆盖——此用例仅在可轻量构造时落成,否则并入报告说明)

(实际落成 4-5 个用例。)

## 三、不可触碰清单

- W4-1 成果:memory_store.py 本批零改动(共享常量已就位,只导入)
- Dream 段、四个 Dream 模块函数、memory.py 门面已有 re-export 顺序零改动
- `_archive_identity` 等跨模块私有访问原样保留
- 协同模块(memory_models/repository/jsonl_import/audit_export/reflection)与 8 个消费者文件零改动
- 既有测试断言零修改(唯一例外:2.3 列出的 6 处模块路径调整)

## 四、验收清单

- [ ] 全量测试绿(passed ≥ W4-1 基线 + 新增)且既有断言零修改;ruff 零告警
- [ ] `from miniunicorn.agent.memory import Consolidator` 仍可用且 identity 成立
- [ ] `_RAW_ARCHIVE_MAX_CHARS` 全库仍仅 memory_store.py 一处定义;memory_consolidator.py 为导入
- [ ] monkeypatch 语义等价:6 个 token 估算测试经新模块目标通过,lambda 体与断言零修改
- [ ] 纯搬家多重集合核对:删除侧恰为 Consolidator + `_ARCHIVE_SUMMARY_MAX_CHARS` + 迁出的 import,新增侧恰为新模块 docstring、import 块、共享常量导入、门面 re-export
- [ ] 偏差逐条说明
