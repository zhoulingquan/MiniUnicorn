# 阶段 0：受保护批次基线提交

> 执行者：主会话（GLM）在用户授权下执行，**不交给 Opencode**。
> 原因：本批是 git 提交操作，且需要人工审查 diff 范围，不适合委托实施。

## 目标

把工作区约 52 个未提交文件（+782/−308，2026-08-26 code review 修复批次）提交为独立基线，使后续 W0/W1 批次不与受保护改动混流。

## 这批改动是什么

2026-08-26 对"审批门修复批次"的 code review 发现的 7 项修复：

1. 高风险策略向子代理传播（SubagentManager 构造注入 high_risk_policy）
2. _AUDIT_ONLY_CHECKPOINT_PHASES 审计过滤（tool_started/completed/blocked 不覆盖恢复槽）
3. tool_blocked 审计 checkpoint 补发
4. 审批回调支持同步形态（inspect.isawaitable）
5. AgentRunSpec 策略值校验（非法值抛 ValueError）
6. subagent phase 注释更新
7. 测试补充（LowRiskFakeTool、fail_on_tool_error、同步回调用例等）

## 执行清单

1. `git status` 确认文件清单；确认没有 `.tmp-*`、临时报告、无关文件混入
2. 逐类审查 diff：
   - `git diff --stat` 总览
   - 重点文件人工过目：session_turn.py、tool_execution.py、subagent.py、runner.py（审批门相关段落）
3. 跑全量测试：`pytest tests/ -q`，确认 ≥ 3970 passed / 0 failed
4. `ruff check erza/ && ruff format --check erza/`
5. 分批 `git add <明确路径>`（按主题分 1-3 个 commit，禁止 `git add .`）：
   - 建议 commit 1：审批门策略传播 + 回调形态 + 校验（agent/subagent.py、runner.py 相关）
   - 建议 commit 2：checkpoint 审计过滤 + tool_blocked（session_turn.py、tool_execution.py 相关）
   - 建议 commit 3：测试补充
6. 打 tag：`git tag -a baseline-pre-w0 -m "受保护修复批次基线，W0 可信证据管道起点"`
7. 记录受保护测试清单（00-overview.md 第 4.1 节所列），作为后续批次的禁区清单

## 完成标准

- 工作区干净（git status 无未跟踪的源码改动）
- tag baseline-pre-w0 存在
- 全量测试与 ruff 在提交后状态绿

## 回滚

无需回滚设计——本阶段只提交已验证存在的改动，不产生新代码。
