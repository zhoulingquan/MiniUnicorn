# memory 家族外置评估(W6-1 之后)

> 日期:2026-09-02 · 数据来源:程序化依赖扫描(rg/AST 级)+ W4/W5 系列结论
> 前提:本文所有耦合分析以 **W6-1 落地后** 的状态为准(W6-1 把 call_ledger/turn_budget 抽成顶层 ledger 叶子)。

## 结论先行

**值得做,但不紧急;建议作为 W7 立项,排在 W6-1 验证全绿之后。**

核心论据:W6-1 落地后,memory 家族与 agent 核心的真实耦合只剩**一个工具函数**(`reflection._atomic_rewrite_lines`,原子重写文件,utils 已有同类 `_write_text_atomic` 可合并下沉)。外置的技术成本约为 W5-1 的四分之一(13 文件 vs 55 文件、~10 消费方 vs 86 消费方),而收益是把 agent/ 收成纯编排核心——与"core + tool library"的既定架构方向完全同构。

不做的唯一硬理由是"W4 刚稳定、facade 已是功能边界"——成立但弱:facade 解决的是**内部**耦合,不解决**位置**(cli/command 仍要穿 agent 命名空间取记忆,agent 包 34% 的体积是数据平面)。

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
| git mv | 13 文件 → `miniunicorn/memory/` |
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
- **W7-1**:memory 家族 + reflection 外置 `miniunicorn/memory/`,含守护白名单手术与门面重建(门面 re-export 清单照抄现 memory.py,含私有名 noqa)
- 判据:W6-1 全绿落地后启动;期间若有更高优先级业务需求,本项可无限期后移——facade 保证了后移不产生新的耦合债

## 七、被否决的替代方案

- **包目录原地收编**(agent/memory/ 子包):W4 已否决——同名模块与包不能共存,必须与删 memory.py 同批;且不解决 cli/command 穿透问题
- **只加守护不搬家**:守护只能防新债,防不了既有的位置与体积问题;agent 包 34% 是数据平面的事实不变
