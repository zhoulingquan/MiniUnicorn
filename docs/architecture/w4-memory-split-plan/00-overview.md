# W4 系列:memory.py 拆分(平铺三模块 + 门面收缩)

> 状态:方案已裁决,三批任务书齐备。基线:`e5d62312`(W3-2 已合并,全量测试 4114 passed / 0 failed)。
> 性质:**纯搬家重构**——零行为变更、零新抽象、消费者零改动,靠兼容门面保住全部既有 import 路径。
> 前置阅读:W3 总览的"全局红线"在本系列继续有效(trusted-evidence 管线、三态审批门、W1-2 注入-回退双路径、W2/W3 已定型的分段形态)。

## 一、问题

`miniunicorn/agent/memory.py` 共 **2018 行**,一个文件承载记忆系统三类互不相同的职责:

| 区段 | 行号 | 规模 | 职责 |
|---|---|---|---|
| 模块辅助函数 | 55-97 | ~43 行 | `_parse_datetime_loose`、`reflection_evidence_id`、`_dream_source_batch`、`count_pending_dream_entries`(全部只服务 Dream) |
| `MemoryStore` + `WorkspaceMemoryRegistry` | 100-971 | ~872 行 | 纯文件 I/O 存储层(soul/notes/history 读写缓存、structured 召回、GitStore 版本管理、cursor 推进、审计导出触发) |
| 常量区 | 975-982 | 8 行 | `_RAW_ARCHIVE_MAX_CHARS`(MemoryStore 与 Consolidator 共用)、`_ARCHIVE_SUMMARY_MAX_CHARS`(仅 Consolidator)、`_HISTORY_ENTRY_HARD_CAP`(仅 MemoryStore) |
| `Consolidator` | 985-1517 | ~533 行 | 会话历史 LLM 整合(token 预算、归档、回放溢出、空闲压缩) |
| `Dream` | 1523-2018 | ~496 行 | 离线知识沉淀(批量提取、结构化写入、批次有界化) |

单文件三类职责 = 单 PR 审查面过大、编辑冲突集中、检索定位成本高。而其周边早已演化出平铺家族:`memory_models.py`(数据类型)、`memory_repository.py`(结构化仓库)、`memory_jsonl_import.py`(legacy 迁移)、`memory_audit_export.py`(审计导出,MemoryStore 内懒导入)——**memory.py 本体是唯一还没拆的那个**。

## 二、裁决表

| # | 候选 | 裁决 | 理由 |
|---|---|---|---|
| 1 | 平铺三模块 + 门面渐进收缩 | **采纳** | 与既有 `memory_*.py` 家族命名一致;每批独立可验证;`from miniunicorn.agent.memory import X` 全程不破 |
| 2 | `agent/memory/` 包目录 | 否决 | 同名模块与包不能共存,建包必须与删 `memory.py` 同批,破坏"小批独立验证"纪律;平铺已满足边界表达 |
| 3 | stale 规划的 `miniunicorn/memory/` 顶层包(store/models/repository/lifecycle/recall/maintenance/adapters 七文件) | 否决 | 那是 Agent Core/Tool Library 架构愿景的组成,MemoryPort 适配层已随 W2 裁决否决(单实现 + 无类型检查器强制 = 抽象税);models/repository 已独立存在,无需搬家 |
| 4 | MemoryStore 类内再拆(recall/git/cursor mixin) | 否决 | W2 同款裁决:类级 mixin 抽取是抽象税;830 行单类职责内聚(纯文件 I/O),拆类只会引入跨 mixin 私有访问 |
| 5 | Consolidator/Dream 先搬(小批热身) | 否决 | 依赖单向(Consolidator/Dream → MemoryStore);基座先行(M1)使新模块直接 `from memory_store import ...`,彻底消除经门面的循环 import 问题 |
| 6 | 消费者(loop/runtime_resources/dream_trigger 等)改直连新模块 | 否决 | 兼容 re-export 惯例(禁止删除 compat re-export,用 noqa 保留);消费者零改动是纯搬家的验收标准 |

**最终形态**:

```
miniunicorn/agent/
  memory.py                 # 纯门面(≤60 行):docstring + 全符号 re-export(# noqa: F401)
  memory_store.py           # MemoryStore + WorkspaceMemoryRegistry + _RAW_ARCHIVE_MAX_CHARS + _HISTORY_ENTRY_HARD_CAP(~900 行)
  memory_consolidator.py    # Consolidator + _ARCHIVE_SUMMARY_MAX_CHARS(~560 行)
  memory_dream.py           # Dream + 四个模块函数(~550 行)
  memory_models.py          # (既有,不动)
  memory_repository.py      # (既有,不动)
  memory_jsonl_import.py    # (既有,不动)
  memory_audit_export.py    # (既有,不动)
```

## 三、批次索引

| 批次 | 内容 | 规模 | 依赖 |
|---|---|---|---|
| W4-1 | `MemoryStore` + `WorkspaceMemoryRegistry` + 共享常量 → `memory_store.py`;门面开始积累 re-export | ~872 行搬出 | 无(基座) |
| W4-2 | `Consolidator` + `_ARCHIVE_SUMMARY_MAX_CHARS` → `memory_consolidator.py`(MemoryStore 从 `memory_store` 导入) | ~534 行搬出 | W4-1 |
| W4-3 | `Dream` + 四个模块函数 → `memory_dream.py`;`memory.py` 收缩为纯门面 | ~539 行搬出 | W4-1 |

每批一个 commit(`refactor(w4-N): ...`),独立通过验证门后才进入下一批。

## 四、测试影响面(本系列特有,已全量排查)

测试对 `memory` 模块**命名空间**的 monkeypatch(搬家后名字解析迁移到新模块,补丁必须跟着改模块目标——这是"纯导入路径调整"例外的全部内容,断言零修改):

| 补丁符号 | 使用方(新归属) | 测试文件 | 处数 | 随批调整 |
|---|---|---|---|---|
| `migrate_legacy_journal` | MemoryStore(282) | tests/agent/test_memory_store.py:811 | 1 | W4-1 |
| `_atomic_rewrite_lines` | MemoryStore(538/548/550) | tests/agent/test_reflection_structured.py:355 | 1 | W4-1 |
| `estimate_message_tokens` | Consolidator(1072) | tests/agent/test_loop_consolidation_tokens.py(5 处)、test_consolidation_ratio.py(1 处) | 6 | W4-2 |

其余测试(`store.structured_lifecycle`、`os.replace` 等对象级 patch)不受影响。全库未发现 `monkeypatch.setattr("miniunicorn.agent.memory...")` 字符串目标。

## 五、全局红线

1. **纯搬家**:类/函数/常量整体照搬,语句逐句不变;日志文本、锁顺序、缓存失效时机、五类 cursor 语义零改动。既有测试断言零修改——**唯一例外是第四节列出的 8 处 monkeypatch 模块目标路径调整**(逐处列在各任务书,断言本体不动)
2. **零新抽象**:不建 MemoryPort/Protocol/基类/配置项;新模块就是旧类的容器
3. **消费者零改动**:`agent/__init__.py`、loop.py、runtime_resources.py、dream_trigger.py、autocompact.py、context.py、command/memory.py、cli/_gateway_runner.py 的 import 语句一律不动(经门面)
4. **共享常量归属**:`_RAW_ARCHIVE_MAX_CHARS` 定义于 `memory_store.py`,`memory_consolidator.py` 导入使用(M2→M1 单向依赖,与类依赖同向)
5. **协同模块零改动**:memory_models.py、memory_repository.py、memory_jsonl_import.py、memory_audit_export.py、reflection.py 一个字符都不动
6. **W0-W3 既有形态零触碰**:memory 拆分不得触碰 runner.py、loop.py 的分段成果;memory.py 内被搬代码的 docstring 原样随行
7. 定位一律以符号(class/def/常量名)为准,任务书行号是编写时参考值

## 六、验证门(每批必须全绿后才可 commit)

```
.venv\Scripts\python.exe -m pytest tests/ -q        # 0 failed,passed >= 4114(新测试只增不减;W4-1 前基线)
.venv\Scripts\python.exe -m ruff check miniunicorn/  # 零输出
.venv\Scripts\python.exe -m ruff format --check miniunicorn/
```

已知环境事实:系统默认 Python 3.10 缺 `typing.Self` 会在收集阶段报 14 个 ImportError——必须用 `.venv\Scripts\python.exe`。全量 pytest 约 8-13 分钟。

## 七、完成标准

三批全部合并后:memory.py 为纯门面(≤60 行),四个职责模块独立,消费者 import 路径全部未动,测试只增不减,monkeypatch 目标全部指向定义模块。此后 memory 子系统的演进(memory_store 内部优化、Dream 策略调整)可以在各自文件独立进行。
