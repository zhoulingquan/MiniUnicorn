# memory 家族外置评估(W6-1 之后)

> 日期:2026-09-02(复评,基于 W6-1 落地 + schema 隐患修复后的代码实态)· 数据来源:程序化依赖扫描(rg/AST 级)
> 首评:2026-09-01 · 复评触发:W6-1 全绿(4139)、schema 急切解析隐患已修(`58806943`)

## 复评结论(最终裁决)

**建议做,分两步:W7-0 立即做(保险栓),W7-1 有明确触发条件时做(或作为 W 系列收官战)。**

裁决依据(与首评的差异点):

1. **依赖方向已经归零在望**:memory 家族 13 文件的外部依赖只剩 `memory_store.py:22 → reflection._atomic_rewrite_lines` **一个工具函数**(W6-1 已把 call_ledger 移出依赖面)。下沉该函数后,memory → agent 方向字面归零
2. **无同名冲突**:W4 否决包目录的原因是 `agent/memory.py`(模块)与 `agent/memory/`(包)同目录不能共存;搬到**顶层** `erza/memory/` 与 `agent/memory.py` 不同父目录,Python 解析上无任何冲突——甚至允许新旧短暂共存,搬家比 W4 内部重组更安全
3. **运行时归属已在库侧**:`RuntimeResourceRegistry`(agent/runtime_resources.py)已按"长生命周期服务归组合/运行时层"的项目约定持有 Consolidator/Dream/DreamIdleTrigger 及 per-workspace 缓存——**运行时模型早就是库形态,只有模块位置不匹配**。仅 `context.py:116` 在 ContextBuilder 内直接构造 MemoryStore(外置时改为注入即可,一行改动)
4. **agent 公共 API 面失真**:`agent/__init__.py` 的 `__all__` 把 MemoryStore/Dream 当作 agent 核心的门面导出——数据平面服务冒充核心 API。外置后 agent 的公共面回归纯编排(ContextBuilder/AgentLoop/hook/SubagentManager/SkillsLoader)
5. **首评低估的成本项**:测试足迹约 **17 个测试文件**(15 个走 facade + 直插模块的若干),守护手术 5 处硬编码 + W4 实例化白名单。总接触面 ~35 文件,介于 W6-1(~31)与 W5-1(~90)之间,单次 Cline 会话可完成

**不构成"必须现在做"的理由**(诚实声明):运行时边界已经清晰(registry 持有、注入使用),纯结构卫生诉求;除非触发条件出现,推迟无功能损失。但推迟的代价是 memory 每新增消费方,搬家重指向成本单调上涨。

## 触发条件(任一出现即启动 W7-1)

1. **记忆可选化/隐私配置**成为需求(小微企业用户关闭持久化记忆)——外置是其自然实现形态
2. agent/ 需要继续瘦身(下一个系列,如 loop 拆分)需要先把数据平面挪走
3. memory 家族计划加新能力(向量检索/多后端)——加之前搬,免得消费方翻倍
4. W 系列决定收官(把它作为收官战,agent/ 达成 ~11.4k 纯编排核心终态)

## 复评数据(2026-09-02 实测)

### memory 家族内部依赖(外向,已剔除内部互引)

| 模块 | loc | 外向依赖 |
|---|---|---|
| memory_models | 740 | **(无)** |
| memory_sqlite_schema | 352 | models |
| memory_extraction | 137 | models |
| memory_repository | 768 | models, sqlite_schema, jsonl_import |
| memory_recall | 283 | models, repository |
| memory_lifecycle | 692 | models, repository |
| memory_jsonl_import | 297 | models, repository, sqlite_schema |
| memory_audit_export | 436 | models, sqlite_schema, utils |
| memory_store | 912 | 上述全部 + **reflection(唯一核心依赖,一个函数)** + config, utils |
| memory_consolidator | 567 | store, ledger, providers, session, utils |
| memory_dream | 571 | extraction, lifecycle, models, store, bus, ledger, providers, utils |
| memory_backup | 354 | audit_export, models, sqlite_schema, utils |
| memory.py(门面) | 23 | consolidator, dream, jsonl_import, store |

全部外向依赖均为叶子/库包(utils/config/session/bus/providers/ledger)——**纯数据平面,零控制平面词汇**。

### 消费方全景

- **生产,agent 外**:command/memory.py(4 处)、cli/_gateway_runner.py(1 处)——仅 2 文件
- **生产,agent 内**:`__init__.py`(门面 re-export)、loop、context、runtime_resources、dream_trigger、autocompact——6 文件;**agent/execution/ 零依赖**(已验证)
- **测试**:~17 文件,15 个走 memory.py 门面
- **守护手术**:test_structured_memory_boundary 5 处硬编码路径(行 164/268/297/300/362)+ `_REPOSITORY_INSTANTIATION_ALLOWED` 白名单同步

### reflection.py 归属裁决(首评遗留设计题)

**留在 agent,不随 memory 走。** 依据:

- 它是"写入侧认知"——由核心(execution/planning 的 PlanningReflectionService)惰性调用,让 LLM 产出教训;依赖仅 ledger + utils,零 memory 家族依赖
- 与 memory 的唯一牵连是共享 `reflections.jsonl` 文件与 memory_store 文件锁注册表中的字符串标签 `"reflection"`(纯字符串,无代码依赖)
- 随 memory 走会让 memory 包变成"存储+认知"混合体,破坏数据平面纯度;留核心则方向为 核心→写文件、memory→读写同一文件,由注册表治理
- 前置:W7-0 下沉 `_atomic_rewrite_lines`(reflection.py 内工具函数,与 utils 的 `_write_text_atomic` 合并评估)后,memory→reflection 的最后一条边消失

## 建议批次

- **W7-0(立即,~30 分钟,人工可完成)**:`_atomic_rewrite_lines` 下沉 utils(与既有 `_write_text_atomic` 合并或并列),memory_store 改指向。效果:memory 家族对 agent 核心依赖**字面归零**,保险栓落地
- **W7-1(触发条件出现时)**:13 文件 git mv → `erza/memory/`,门面重建(`memory/__init__.py` re-export 清单照抄现 facade),~9 个生产文件改指向(含 context.py:116 构造改注入、agent/__init__ 门面收窄),~17 测试文件改指向 + 搬 tests/memory/,守护 5 处手术,module-boundaries 登记,新增 memory 包零核心依赖守护(并入 test_dependency_direction 六包规则)
- 验证门模板照抄 W6-1(零残留 rg + 全量 pytest + 双 ruff + 中断防护)

---

以下为首评(2026-09-01)存档,结论被上文复评取代:

## 首评结论(存档)

**值得做,但不紧急;建议作为 W7 立项,排在 W6-1 验证全绿之后。**

核心论据:W6-1 落地后,memory 家族与 agent 核心的真实耦合只剩**一个工具函数**(`reflection._atomic_rewrite_lines`,原子重写文件,utils 已有同类 `_write_text_atomic` 可合并下沉)。外置的技术成本约为 W5-1 的四分之一(13 文件 vs 55 文件、~10 消费方 vs 86 消费方),而收益是把 agent/ 收成纯编排核心——与"core + tool library"的既定架构方向完全同构。

## 一、现状数据

| 维度 | 数值 |
|---|---|
| 规模 | 13 文件(memory_* 12 个 + memory.py 门面)/ **6132 loc** |
| 占比 | agent 包总体积的 **34%** |
| 形态 | 平铺在 `agent/` 下,W4 拆分(平铺三模块 + 23 行门面),W4 明确否决过包目录方案(与 memory.py 同名冲突,须同批删) |
| 外部消费方 | `command/memory.py`(6 处导入)、`cli/_gateway_runner.py`(1 处)——共 **2 个文件** |
| agent 内消费方 | loop / context / autocompact / dream_trigger / runtime_resources(5 文件,基本经门面;context 直插 memory_store) |
| 守护 | `tests/agent/test_structured_memory_boundary.py` 硬编码 **~8 处路径**(含 `_REPOSITORY_INSTANTIATION_ALLOWED` 白名单写死 memory_store.py 等) |

## 二、耦合矩阵(W6-1 后)

```
memory 家族 ──→ agent 核心:   reflection._atomic_rewrite_lines(唯一,工具函数,可下沉 utils)
             ──→ 库/叶子:     config, utils, session, bus, providers, ledger(③ 之后)
agent 核心   ──→ memory:      loop, context, autocompact, dream_trigger, runtime_resources
上层         ──→ memory:      command/, cli/
```

关键观察:**memory → agent 方向可归零**(下沉一个函数后),外置不会制造 tools↔agent 那种包级环(那是 W5 裁决接受的形态,但这里连环都不需要)。依赖方向变成纯粹的单向:上层与核心 → memory 库。

## 三、成本(若做:W7)

| 项 | 量级 |
|---|---|
| git mv | 13 文件 → `erza/memory/` |
| 消费方改指向 | agent 内 5 + command/cli 2 + 测试若干,约 10 文件 |
| 守护手术 | test_structured_memory_boundary ~8 处硬编码路径(**W4 教训:搬家前预先 grep 白名单,同批同步调整**) |
| 测试搬家 | tests/agent/test_memory*.py → tests/memory/(仿 tests/tools 约定) |
| 文档 | module-boundaries.md 登记新包 |
| 验证 | 全量 pytest 一次(~8 分钟,中断防护已实证有效) |

总体量级:单次 Cline 会话可完成的一个批次,远小于 W5-1。

## 四、收益

1. **agent/ 收成纯编排核心**:~11.4k loc(去掉 memory 6.1k + ledger 0.4k 后),目录一眼可读——loop/runner/execution/planner/验收治理链/子代理簇
2. **memory 成为一等库**:与 tools/channels/providers/skills 同构,数据平面(存储/整理/dream)与控制平面分离
3. **cli/command 不再穿 agent 命名空间**
4. **可选化变自然**:记忆是隐私敏感面(对话内容持久化),外置后"关闭记忆"成为包级配置而非核心代码路径,对小微企业用户是真实卖点

## 五、关键设计题:reflection.py 的归属(必须在 W7 设计中裁决)

- `reflections.jsonl` 的文件锁注册表有三主人:`dream` / `reflection` / `memory_store`
- reflection.py(304 loc)由 **execution/planning(核心)** 惰性调用——它是"写入侧认知":让 LLM 生成教训并追加进记忆文件
- 它的依赖只有 ledger 词汇 + utils——**外置时建议随 memory 走**:核心经库接口触发认知写入(干净);若留在 agent,则跨边界共享同一文件(脏)
- `memory_store` 对它仅依赖 `_atomic_rewrite_lines`——该函数先下沉 utils,W7-0 或并入 W7-1 均可

## 六、建议的批次切分(W7)

- **W7-0**(可与 W6-1 合并或紧随):`_atomic_rewrite_lines` 下沉 utils(与既有 `_write_text_atomic` 合并评估,同批改 memory_store 导入)
- **W7-1**:memory 家族 + reflection 外置 `erza/memory/`,含守护白名单手术与门面重建(门面 re-export 清单照抄现 memory.py,含私有名 noqa)
- 判据:W6-1 全绿落地后启动;期间若有更高优先级业务需求,本项可无限期后移——facade 保证了后移不产生新的耦合债

## 七、被否决的替代方案

- **包目录原地收编**(agent/memory/ 子包):W4 已否决——同名模块与包不能共存,必须与删 memory.py 同批;且不解决 cli/command 穿透问题
- **只加守护不搬家**:守护只能防新债,防不了既有的位置与体积问题;agent 包 34% 是数据平面的事实不变
