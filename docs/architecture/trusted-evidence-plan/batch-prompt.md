# WorkBuddy 分批实施提示词模板

> 用法：每批复制一次，只改【本批次任务书】一行为对应批次文件路径，其余不动。
> 批次顺序：02 → 03 → 04 → 05（可插 06）→ 07 → 08 → 09。
> 前置条件：阶段 0 基线已提交（tag `baseline-pre-w0` 存在）。

***

## 提示词正文（复制以下全部内容）

```markdown
# Erza 实施任务 —— 批次 W0-A1（证据管道）

## 角色与背景

你是 Erza 项目的资深 Python 实施工程师。项目定位：小型企业长期使用的业务智能体（单用户），核心诉求是高效、精简、易维护。本任务是一个多批次实施计划中的**一个批次**：独立实施、独立验证、独立提交，完成即停。

工作目录：D:\MyProject\Erza

## 第一步：必读文件（按顺序，实施前读完）

1. `docs/architecture/trusted-evidence-plan/00-overview.md` —— 计划总览与全局约束（重点第 4 节）
2. `docs/architecture/trusted-evidence-plan/02-w0a1-evidence-pipeline.md` —— **本批次任务书（唯一实施依据）**

任务书自包含：包含现状锚点、改动方案、测试要求、禁改清单、验收自检。你的全部实施范围以任务书为准。

## 实施纪律（优先级高于任何效率考虑）

### 三种情况立即停止并报告（禁止猜测着继续）

1. 任务书中任何"现状锚点"与代码实况不符（符号找不到、结构对不上）
2. 完成任务必须触碰"禁改清单"内的文件或逻辑
3. 测试无法全绿且原因不明——**禁止为让测试通过而弱化断言或删改既有测试**（任务书明确允许更新的既有测试除外，逐条列出）

### 全局红线（完整版见 00-overview.md 第 4 节，此处为摘要）

1. **受保护行为不得触碰语义**：三态审批门（allow/deny/approval_callback）、tool_blocked 审计 checkpoint、runtime_checkpoint 恢复链路、子代理预算与 high_risk_policy 传播、verifier 异常 fail-closed 回退
2. **定位用符号**（类名/函数名），任务书中的行号只是编写时参考值
3. **坚决不做**：不引入 Port/Adapter；不把 Planning 拆成模型自主工具；不用文本/关键词/status=ok 作为完成证明；不做 weak/provisional accepted；不做跨轮计划恢复；不在工具调用栈内嵌套主循环；不删 FAST/MANAGED 档位；不一次性搬迁全部工具文件
4. **不加计划外功能**：不顺手重构、不补"以后可能有用"的抽象、不修复任务书范围外的既有问题（发现了就在报告中记录，不动手）
5. **注释与代码风格**：遵循项目现有风格；不写解释"改了什么"的注释

### Git 纪律

- 单批单 commit；禁止 `git add .`，只 add 明确路径
- 禁止混入 `.tmp-*`、临时报告、无关文件
- commit 消息：`feat(w0a1): <一句话主题>`（批次号按任务书，refactor/test 前缀按改动性质选）

## 执行流程

1. 通读两份文件 → 逐条核对任务书"现状锚点"与代码实况，记录核对结论
2. 一致 → 严格按任务书"改动"节实施，范围外一行不动
3. 实现任务书"测试要求"节的**全部**新测试（一条不落）
4. 验证门（全部通过才算完成，未过不得 commit）：
   - `pytest tests/ -q` → 0 failed，passed 数不低于改动前水平
   - `ruff check erza/ && ruff format --check erza/` → 零输出
5. 按明确路径分批 `git add` 后 commit
6. 输出批次报告（格式见下）
7. **完成即停**：不继续后续批次，不优化其他文件，不等用户追问就结束

若单次无法完成全部内容：明确报告已完成到哪一步，未过验证门不得 commit，不要留下说不清的半成品。

## 批次报告格式（最后输出）

```

## 批次报告：W0-A1

1. 锚点核对：逐条一致 / 第 N 条偏差：<说明>
2. 改动文件：<逐个列出>
3. 新增测试：<文件名 + 用例数>
4. 验证门：pytest \<N passed / 0 failed>；ruff <零告警>
5. 与任务书偏差：逐条说明 / 无
6. 验收自检：<任务书末节清单逐项 √，未达成的说明原因>
7. 顺带发现（仅记录不动手）：<问题清单 / 无>

```

---

## 每批替换用批次参数表

| 批次 | 任务书路径 | commit 前缀 |
|---|---|---|
| W0-A1 | 02-w0a1-evidence-pipeline.md | feat(w0a1) |
| W0-A2 | 03-w0a2-receipt-protocol.md | feat(w0a2) |
| W0-A3 | 04-w0a3-acceptance-semantics.md | feat(w0a3) |
| W0-A4 | 05-w0a4-planner-protocol.md | feat(w0a4) |
| W0-B | 06-w0b-checkpoint-isolation.md | fix(w0b) |
| W1-1 | 07-w1-lazy-loading.md | refactor(w1-1) |
| W1-2 | 08-w1-lifecycle-ownership.md | refactor(w1-2) |
| W1-3 | 09-w1-activate-plan.md | feat(w1-3) |

替换时改两处：提示词标题里的批次号、必读文件第 2 条的路径。
```

