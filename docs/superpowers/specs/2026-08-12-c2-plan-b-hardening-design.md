# C2 方案 B：原子幂等与稳定证据链加固设计

**日期：** 2026-08-12  
**状态：** 已批准，待实施  
**基线：** `e1e999cad6523cb49d9a6254a83920146ff29092`  
**适用仓库：** Erza

## 1. 目标

本设计修复 C2 复审确认的重试、并发、证据来源、作用域、迁移和命令健壮性缺口。完成后必须满足：

1. 同一 `source_batch + content_hash` 在并发、崩溃重试、手动提升和内容合并后只创建一次。
2. journal 继续是结构化记忆的唯一事实源，不增加外部幂等账本、数据库或向量设施。
3. history 与 reflection evidence ref 在模型输入中明确可见，并能长期唯一定位来源。
4. user/session scope 只在批次身份完全明确且一致时开放，混合或缺失身份时 fail closed。
5. migration manifest 可跨进程串行更新、崩溃安全保存，并兼容旧路径。
6. 状态转换不能删除已有候选冲突关系。
7. 用户畸形命令返回 usage，不泄漏内部异常。

## 2. 全局约束

- 不修改 `SCHEMA_VERSION`，现有 journal 必须可直接重放。
- 不创建第二份结构化事实账本；所有幂等索引必须由 journal 重建。
- 不引入 embedding、向量索引、向量数据库、知识图谱或相关可选依赖。
- governed 模式保持 fail closed；candidate 永不进入 recall。
- 所有文件锁必须跨进程有效，Windows 与 POSIX 均须通过测试。
- 新行为采用 TDD：先证明测试在旧实现上失败，再实现，再运行回归。
- 不顺带重构与本设计无关的 Agent、Session、Provider 或 GitStore 代码。

## 3. Repository：区分创建幂等与修改 provenance

### 3.1 两类可重建索引

Repository 重放 journal 时维护两类不同语义的索引：

```python
_created_by_source_content: dict[tuple[str, str], str]
_source_batches_by_record: dict[str, set[str]]
```

- `_created_by_source_content[(source_batch, content_hash)] = memory_id` 只在该 ID 的 revision 1 首次创建时写入，之后永不移动或覆盖。它是 Lifecycle 创建幂等的唯一查询依据。
- `_source_batches_by_record[memory_id]` 累积该记录所有非空 `source_batch`，用于审计和 provenance 查询。发布新 revision 时不得删除历史 batch。
- 状态、scope、subject、tag、alias、active conflict 等“当前记录索引”仍随 revision 先 unindex 再 index。
- 旧的 `_records_by_source` 不再承担“最后一次写入”语义；公开的 `record_ids_for_source()` 返回累计关联，或在没有调用者后移除。

重放 revision 1 时如果发现相同非空 `(source_batch, content_hash)` 已指向另一个 ID，校验层抛出 `DuplicateMemoryIdempotencyKey(InvalidMemoryTransition)`，Repository 必须降级，错误码使用 `duplicate_idempotency_key`。不得依靠异常消息字符串区分错误类型。这能检测历史或外部写入造成的不一致。

### 3.2 原子条件创建

Repository 增加接口：

```python
def append_create_if_absent(
    self,
    transaction: MemoryTransaction,
) -> tuple[MemoryRecord, bool]:
    """返回 (current_record, created)。"""
```

调用契约：

- transaction 必须有非空 `source_batch`；
- 必须恰好包含一个 operation；
- operation 必须创建 revision 1；
- `expected_revisions` 对该 ID 必须为 0；
- 不符合契约时抛项目 typed `MemoryWriteError`。

整个操作必须在 journal 的共享 `FileLock` 内完成：

1. 检查当前 Repository health；
2. 在锁内重放 journal，同步其他实例的提交；
3. 用 transaction 内 record 的 `content_hash` 查询 `_created_by_source_content`；
4. 已存在时返回该 ID 的当前 revision 和 `False`，不追加 journal；
5. 不存在时执行现有 checksum、revision、transition、tag、active 唯一性校验；
6. 追加、flush、fsync、发布索引；
7. 返回创建的当前记录和 `True`。

`append_transaction()` 继续处理普通 revision。创建型 transaction 仍可通过该方法用于迁移或测试，但 Lifecycle 的候选创建必须只调用 `append_create_if_absent()`。

### 3.3 Lifecycle 接入

`StructuredMemoryLifecycle.ingest()` 不再在锁外调用 `_find_existing_record()`。它先构造候选和创建事务，再调用 `append_create_if_absent()`：

- `created=True`：按原规则执行 promotion/conflict；
- `created=False` 且 current 为 candidate：恢复未完成的 promotion/conflict；
- current 为 active：直接返回 `REASON_EXISTING`，零写入；
- current 为 terminal：零写入返回 terminal 状态；若为 superseded 且 `replacement_id` 指向 active，则同时返回该 active ID。

这保证 candidate 创建成功但 promotion 失败后可以安全恢复，也保证手动 promote、identical merge 和跨实例并发后不会重复建档。

`source_batch` 为空的手动命令不走条件创建，只产生普通 revision，因此不需要伪造批次 ID。

## 4. Dream evidence 与批次身份

### 4.1 history evidence

发给模型的每条 history 必须包含 catalog 中的真实 ref：

```text
[history:42 | 2026-08-12T08:30:00Z] 用户确认使用 PostgreSQL。
```

模板不得再描述 `history:1..N`。统一规则为：只可引用输入中方括号明确显示的 `history:<cursor>` 或 `reflection:<stable_id>`。

### 4.2 stable reflection ID

structured Reflection 写入时由程序生成：

```text
rfl_<32 lowercase hex>
```

模型只输出 lesson，不参与 ID 分配。新记录持久化字段为：

```json
{
  "reflection_id": "rfl_...",
  "lesson": "..."
}
```

Dream evidence ref 使用 `reflection:<reflection_id>`，并在 prompt 中明确显示。文件截断、旋转、重排均不能改变 ID。

兼容旧条目：

- ID 匹配 `rfl_[0-9a-f]{32}` 时直接使用；
- 旧的 `R<number>` 或缺失 ID 不可信，读取时基于规范化 JSON 内容生成 `rfl_legacy_<24 hex sha256>`；
- legacy fallback 只用于 evidence ref，不要求回写旧文件。

Reflection 模板改为只输出：

```json
{"lesson":"一条原子、可执行的经验"}
```

### 4.3 Dream batch ID

Dream 的 `source_batch` 不再使用可能重置的 cursor 拼接。它由本次实际 evidence ref 集合确定：

```text
dream:<sha256(sorted evidence refs joined by newline) first 24 hex>
```

相同输入重试得到同一 batch ID；不同 history/reflection 集合得到不同 ID。合法空 proposal 仍可推进对应 cursor。

### 4.4 动态 scope

在调用 Provider 之前，根据当前实际输入构造 `scope_by_hint` 和允许提示：

- 始终允许 `project`、`shared`；
- 所有输入项都有同一个规范化 `session_key` 时允许 `session`；
- 所有输入项都有同一个 `user_key` 时允许 `user`；
- 任一输入缺失身份、身份不一致或混合多个身份时，不允许对应细粒度 scope；
- Reflection 当前没有 `user_key` 时，包含 Reflection 的批次不得开放 user scope；
- 模型只看到允许的 hint 名称，不需要看到实际 user/session key。

模板动态渲染允许值。例如：

```text
Allowed scope_hint values for this batch: project, shared, session, user.
```

Parser 继续使用同一 `scope_by_hint` 的键集合做强校验，提示与运行时规则不得分叉。

## 5. Migration 并发与恢复

新增 `memory/structured/migration-v1.lock`。`MemoryMigration.apply()` 从扫描、加载 state、逐项 ingest、逐项保存直到最终 `completed_at` 保存的整个 read-modify-write 周期都持有此锁。另一个 apply 必须等待或抛 typed `MemoryLockTimeout`，不得并行修改 manifest。

`MigrationState.save()`：

1. 在目标目录创建唯一 sibling temp，不共用固定 `.tmp`；
2. 写 JSON、flush、fsync 文件；
3. `os.replace()` 到 canonical manifest；
4. POSIX 上打开父目录并 fsync；Windows 或不支持目录 fsync 时只忽略明确的 unsupported/OSError；
5. finally 删除尚存的本次 temp。

读取统一为公开 helper：

```python
def load_migration_state(workspace: Path) -> MigrationState:
```

规则：canonical 存在时只读取 canonical；canonical 不存在时读取 legacy path；两个都不存在则返回空 state。canonical 损坏时必须 fail closed，不得回退可能过期的 legacy 完成标记。

`MemoryMigration`、`MemoryStore.migration_completed()` 和 `/memory-status` 必须全部调用该 helper。旧 state 在下一次 apply 时保存到 canonical 位置，旧文件保留不删除。

## 6. 状态转换与命令健壮性

### 6.1 blocked_by

candidate 保持 candidate 的 revision 中，`evidence`、`derived_from` 和 `blocked_by` 都必须是上一 revision 的集合超集。允许添加阻塞来源，禁止删除或替换已有 ID。candidate 转为 active、superseded、revoked 或 expired 时，按现有状态转换规则决定是否清空。

### 6.2 slash command

`/memory-show`、`/memory-promote`、`/memory-revoke` 的 `shlex.split()` 统一经过安全 helper。未闭合引号等 `ValueError` 返回各自 usage。不得扩大 `_requires_stack` 去吞掉所有 `ValueError`，避免隐藏真正的程序错误。

## 7. No-vector 边界与文档

边界测试使用 AST 扫描结构化记忆运行路径的 import：

- `erza/agent/memory.py`
- `erza/agent/memory_*.py`
- `erza/agent/context.py`
- `erza/agent/loop.py`
- `erza/agent/reflection.py`
- `erza/command/memory.py`
- `erza/config/schema.py`

禁止 import 根至少包含：`chromadb`、`faiss`、`lancedb`、`qdrant_client`、`pinecone`、`weaviate`、`pymilvus`、`annoy`、`hnswlib`、`sentence_transformers`。同时继续扫描 `pyproject.toml` 的正式和可选依赖。

文档必须准确说明 recall audit 包含：timestamp、scope hashes、hit ID/score/reason code、候选与过滤计数、预算排除、token 数、degraded 和 error code；不包含原始 query、statement 或 evidence excerpt。

## 8. 故障语义

- Repository 条件创建在锁等待超时时抛 `MemoryLockTimeout`，不追加、不发布索引。
- journal append 结果不确定时维持现有 `write_uncertain` degraded 语义。
- Dream evidence 或 scope 不合法时不推进任何相关 cursor。
- Migration lock 超时或 manifest 保存失败时不设置 `completed_at`；已经提交的 journal 项由稳定幂等键在下次重试识别。
- Reflection 生成失败不得写半条 JSONL；ID 由程序在成功持久化的 entry 上保存。
- Recall audit 写入失败仍不得阻断 governed recall。

## 9. 测试矩阵

必须至少覆盖：

1. 手动 promote 后重试原 batch，不创建新 ID。
2. identical merge 后重试原 batch，零写入且结果确定。
3. 两个预先构造的 Repository 实例串行模拟竞争，只创建一个 ID。
4. 真正的两个进程同时条件创建，只创建一个 ID。
5. journal 重建后创建幂等映射保持一致。
6. 重放检测重复 `(source_batch, content_hash)` 并 degraded。
7. history cursor 从 42 开始时 prompt 显示 `history:42`，模型引用可解析。
8. reflection prune/rotate 前后 stable ID 不重复。
9. 旧 Reflection 条目得到稳定 legacy hash ref。
10. 单一 user/session 批次开放对应 scope；混合或缺失身份时拒绝。
11. Dream batch ID 对相同 evidence 稳定、对不同 evidence 不同。
12. 两个 migration apply 不会重复导入或覆盖 manifest。
13. canonical/legacy manifest 状态在迁移、启动门禁和 status 命令中一致。
14. candidate same-status revision 删除或替换 `blocked_by` 被拒绝。
15. 三个命令的未闭合引号均返回 usage。
16. runtime import 向量后端时边界测试失败。

## 10. 完成标准

- 所有新增回归测试通过，并在旧实现上确认过对应失败。
- C2 聚焦测试、完整 `tests/agent`、Ruff、`git diff --check` 全部通过。
- 如果完整套件出现环境或基线失败，必须在未修改基线 worktree 复现并记录，不能直接归类为无关。
- journal schema 与历史数据保持兼容。
- 没有新增依赖、向量入口、第二事实源或用户数据泄露。
- 文档、命令输出和实现行为一致。
