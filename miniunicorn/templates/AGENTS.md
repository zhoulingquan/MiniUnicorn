# Agent 指引

## 工作区说明

本文件用于记录项目专属偏好、常用工作流约定，以及希望 agent 始终遵守的项目指令。人格/风格指引放在 `SOUL.md`；长期事实由结构化记忆命令管理，不要直接编辑 `memory/structured/journal.jsonl`。

## 定时提醒

在创建定时提醒前，先检查可用技能（skills）并优先遵循技能指引。
使用内置 `cron` 工具创建/列出/删除任务（不要通过 `exec` 调用 `miniunicorn cron`）。
从当前会话获取 USER_ID 和 CHANNEL（例如 `telegram:8281248569` 中的 `8281248569` 和 `telegram`）。

**不要只让 Agent 记住提醒**——记忆不会触发真正的定时通知。

## 心跳任务

`HEARTBEAT.md` 在注册为 cron 任务后会定期被检查。使用内置 `cron` 工具调度（例如 `cron add --name heartbeat --schedule "every 30m" --message "Check HEARTBEAT.md"`）。

- 对常规任务列表更新，尤其是新增、删除或修改多行时，使用 `apply_patch`。
- `edit_file` 仅用于从当前 `HEARTBEAT.md` 中复制的小范围精确替换。
- `write_file` 用于首次创建或有意整文件重写。

当用户请求周期性/定时任务时，更新 `HEARTBEAT.md` 并通过 `cron` 注册，而不是创建一次性提醒。
