# SQLite 记忆存储增强设计

**状态：** 待实施
**日期：** 2026-08-14
**适用仓库：** Erza
**前置基线：** `main@f4700458`，单路径受治理结构化记忆已经完成

## 1. 决策结论

Erza 保留现有记忆模型、生命周期、Dream 提取、权限隔离和确定性召回规则，只替换结构化记忆的持久化底座：

- `memory/structured/memory.db` 是唯一运行时事实库；
- 现有 `memory/structured/journal.jsonl` 只作为一次性迁移输入，迁移后不再追加；
- 新 JSONL 位于 `memory/structured/audit/`，由 SQLite 事务确定性生成，是可重建审计导出，不是第二事实源；
- 运行时没有 JSONL/SQLite 模式开关、双写开关或回退路径；
- SQLite 损坏或迁移失败时 fail closed，不静默切回 JSONL；
- 使用 Python 标准库 `sqlite3`，不增加 SQLAlchemy、数据库服务或第三方依赖；
- 要求运行时 SQLite >= 3.37.0，以使用 STRICT tables；版本不足时以 `unsupported_sqlite_version` fail closed；
- 继续禁止 embedding、向量检索和向量数据库。

这次升级改变“记忆存在哪里和怎样查”，不改变“什么内容可以成为正式记忆”。

## 2. 目标

1. 正常启动不再重放全部 JSONL。
2. 每次写入不再重读全部历史事务。
3. 当前记录、历史版本、冲突键、来源批次和召回范围均使用数据库索引查询。
4. 多进程并发写入由 SQLite 事务和唯一约束保证，不丢更新、不产生重复候选。
5. 现有 JSONL 数据可一次性、幂等、可验证地迁移，不删除原文件。
6. SQLite 中仍保存完整 `MemoryTransaction` 和 `MemoryRecord` JSON，审计语义不降级。
7. JSONL 审计可按事务序号分段导出并从数据库完全重建。
8. 结构化存储故障继续 fail closed，不向提示词泄露不可信事实。
9. 保持 `StructuredMemoryRepository` 的业务接口稳定，减少 Lifecycle、Dream 和命令层改动。
10. 为未来迁移 PostgreSQL 留出 Repository 接口边界，但本次不实现 PostgreSQL。

## 3. 非目标

- 不把聊天历史、Reflection 和附件全部塞进 SQLite；`history.jsonl`、`reflections.jsonl` 继续按现有上限和消费机制运行。
- 不增加多个运行模式，不长期保留 JSONL Repository。
- 不做云同步、多主复制、跨设备共识或中心化多租户数据库。
- 不实现向量召回、语义重排、知识图谱或 FTS5 自动降级路径。
- 不在本次重写 Lifecycle、Dream 提示词、记忆评分公式和状态机。
- 不把 `memory.db`、WAL、SHM、备份或审计段提交到 Git。

## 4. 方案对比

### 方案 A：继续优化单个 JSONL

增加 snapshot、尾部 offset 和内存索引，可以延长寿命，但仍要维护文件锁、重建、归档和随机查询逻辑。适合个人 Agent，不适合作为长期业务底座。

### 方案 B：JSONL 为事实源，SQLite 为投影

最能保留旧架构，但写入要协调两个持久化介质，必须处理“JSONL 已 fsync、SQLite 未提交”的恢复协议，长期复杂度较高。

### 方案 C：SQLite 为事实库，JSONL 为审计导出（采用）

一笔记忆事务只在 SQLite 中原子提交；JSONL 从事务表派生，可延迟或重建。它最符合小微企业单机/轻服务部署：无需数据库服务器，同时消除全量重放和双写一致性问题。

## 5. 总体架构

```text
Consolidator/history + Reflection
                 |
                 v
              Dream
                 |
                 v
       StructuredMemoryLifecycle
                 |
                 v
       StructuredMemoryRepository
                 |
        +--------+---------+
        |                  |
        v                  v
 memory.db (事实库)   audit/*.jsonl (派生审计)
        |
        v
 scoped SQL filter -> existing lexical scoring -> prompt
```

组件边界：

| 组件 | 职责 |
|---|---|
| `memory_repository.py` | 保持公开 Repository 类名；负责 SQLite 事务、查询、健康状态 |
| `memory_sqlite_schema.py` | DDL、连接 PRAGMA、schema version 和升级 |
| `memory_jsonl_import.py` | 只读旧 journal、校验并导入临时数据库 |
| `memory_audit_export.py` | 从 SQLite 按序导出分段 JSONL、生成 manifest、重建导出 |
| `memory_backup.py` | SQLite 在线备份、完整性验证和显式恢复 |
| `memory_recall.py` | 保留现有词法路由和评分；候选集合改由 SQL 先做 scope/status/kind/expiry 过滤 |
| `memory.py` | 组装 repository/lifecycle/recall；不承载 SQL 细节 |

## 6. 文件布局

```text
workspace/memory/structured/
├── memory.db                         # 唯一运行时事实库
├── memory.db-wal                     # SQLite 临时运行文件，不跟踪
├── memory.db-shm                     # SQLite 临时运行文件，不跟踪
├── memory-maintenance.lock           # 迁移/恢复/审计目录替换锁
├── tags.json                         # 继续作为受控标签目录
├── journal.jsonl                     # 旧数据，只读迁移输入；迁移后不再写
├── storage-migration-v2.json         # 迁移结果和源文件摘要
├── audit/
│   ├── manifest.json                 # 派生段清单
│   ├── journal-000000000001-000000010000.jsonl
│   └── journal-open.jsonl            # 不足一个完整段的尾部
├── backups/
│   └── memory-<UTC>-<tx_seq>.db      # 用户显式创建的备份
└── recovery/
    └── <UTC>/                        # 恢复前数据库和旧审计目录
```

`memory.db*`、`audit/`、`backups/`、`recovery/` 和 lock 文件不得进入 GitStore。`tags.json` 与 `POLICY.md` 继续跟踪。

## 7. SQLite 数据模型

数据库 `PRAGMA user_version=1`。所有 JSON 使用现有 canonical JSON 规则，时间继续保存 UTC ISO-8601。

### 7.1 `memory_transactions`

每次 Repository 提交对应一行，保存完整原始事务，是数据库内的审计主表。

```sql
CREATE TABLE memory_transactions (
    tx_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    tx_id TEXT NOT NULL UNIQUE,
    recorded_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    source_batch TEXT NOT NULL,
    checksum_sha256 TEXT NOT NULL,
    transaction_json TEXT NOT NULL
) STRICT;
```

### 7.2 `memory_revisions`

每个 `MemoryRecord` revision 一行，历史 revision 永不更新；只有前一 revision 的 `is_current` 会在同一事务内从 1 改成 0。

```sql
CREATE TABLE memory_revisions (
    memory_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    tx_seq INTEGER NOT NULL REFERENCES memory_transactions(tx_seq),
    op_index INTEGER NOT NULL,
    is_current INTEGER NOT NULL CHECK (is_current IN (0, 1)),
    status TEXT NOT NULL,
    kind TEXT NOT NULL,
    scope_kind TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    subject_norm TEXT NOT NULL,
    conflict_key TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    source_level TEXT NOT NULL,
    importance INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT,
    record_json TEXT NOT NULL,
    PRIMARY KEY (memory_id, revision),
    UNIQUE (tx_seq, op_index)
) STRICT;

CREATE UNIQUE INDEX ux_memory_current_id
ON memory_revisions(memory_id) WHERE is_current = 1;

CREATE UNIQUE INDEX ux_memory_active_conflict
ON memory_revisions(conflict_key)
WHERE is_current = 1 AND status = 'active';

CREATE INDEX ix_memory_recall_scope
ON memory_revisions(status, scope_kind, scope_key, kind, expires_at, updated_at)
WHERE is_current = 1;
```

### 7.3 辅助表

- `memory_creation_keys(source_batch, content_hash, memory_id, created_tx_seq)`：唯一键保证 Dream 重试幂等；
- `memory_source_batches(memory_id, source_batch, first_tx_seq)`：累计来源批次；
- `memory_tags(memory_id, revision, tag)`：支持受控 Tag 查询；
- `memory_aliases(memory_id, revision, alias_norm)`：支持别名查询；
- `storage_meta(key, value)`：schema、迁移来源、最近成功写入和审计导出信息。

所有辅助表写入与 transaction/revision 必须处于同一个 `BEGIN IMMEDIATE` 事务中。

## 8. 连接与事务策略

每次 Repository 操作创建短连接，避免长期连接跨线程问题。每个连接设置：

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;
PRAGMA foreign_keys=ON;
PRAGMA trusted_schema=OFF;
PRAGMA busy_timeout=<lock_timeout_s * 1000>;
```

写入协议：

1. 打开连接并执行 `BEGIN IMMEDIATE`；
2. 校验 Repository health、事务 checksum、Tag 和 operation 结构；
3. 从 `memory_revisions WHERE is_current=1` 读取受影响 ID；
4. 校验 expected revision、合法状态转换和 same-status revision；
5. 校验 `memory_creation_keys`、tx_id 和 active conflict 唯一性；
6. 插入 `memory_transactions`；
7. 把旧 current revision 标记为非 current；
8. 插入完整新 revision、Tag、alias 和来源关系；
9. 提交 SQLite 事务；
10. 提交成功后触发非阻塞/可失败的审计导出检查。

步骤 1–9 任一失败必须 rollback。审计导出失败不回滚已提交事实，只增加 `audit_lag` 和告警；因为 JSONL 可从数据库重建。

SQLite `locked/busy` 映射为 `MemoryLockTimeout`；唯一键、revision 和状态错误继续映射现有类型化异常；磁盘、schema 和完整性错误进入 `RepositoryHealth(state="degraded")`。

## 9. Repository 接口兼容

保留以下既有方法及语义：

- `append_transaction(transaction) -> None`
- `append_create_if_absent(transaction) -> tuple[MemoryRecord, bool]`
- `get(memory_id) -> MemoryRecord | None`
- `get_current(memory_id, synchronize=True) -> MemoryRecord | None`
- `revisions(memory_id) -> tuple[MemoryRecord, ...]`
- `current_records(status=None) -> tuple[MemoryRecord, ...]`
- `active_for_conflict_key(key) -> MemoryRecord | None`
- `candidate_records()`
- `candidate_ids_for_source(source_batch)`
- `record_created_for(source_batch, content_hash)`
- `record_ids_for_source(source_batch)`
- `health`、`tag_catalog`、`workspace`

`get_current(..., synchronize=True)` 为兼容保留参数，但 SQLite 读取天然看到已提交状态，不再触发 rebuild。

新增：

```python
def recall_candidates(
    self,
    *,
    allowed_scopes: tuple[MemoryScope, ...],
    requested_kinds: frozenset[MemoryKind],
    now: datetime,
) -> tuple[MemoryRecord, ...]: ...

def transaction_log(self, *, limit: int = 20, tx_id: str | None = None) -> tuple[MemoryTransaction, ...]: ...

def storage_stats(self) -> MemoryStorageStats: ...
```

Lifecycle 不得执行 SQL，也不得知道表结构。

## 10. 召回策略

评分和路由规则不变。变化仅限候选集来源：

1. SQL 只读取 `is_current=1 AND status='active'`；
2. 使用精确 `(scope_kind, scope_key)` 白名单；
3. 在 SQL 中过滤 requested kinds 和已过期记录；
4. Python 继续执行稳定 ID、subject、Tag、catalog alias 和 record alias 路由；
5. Python 继续执行来源强度、scope、importance、freshness 排序和 token budget。

不在本阶段加入 FTS5，避免改变 CJK/ASCII 路由语义。达到单 scope 大量 active 记录的实际瓶颈后，再以独立设计评估 FTS5。

## 11. 旧 JSONL 迁移

触发条件：`memory.db` 不存在且 `journal.jsonl` 存在且非空。

迁移流程：

1. 获取 `memory-maintenance.lock`；
2. 计算旧 journal 的 SHA-256、字节数和非空行数；
3. 创建 `memory.db.importing`；
4. 按原顺序逐行解析 `MemoryTransaction`；
5. 复用现有 checksum、revision、状态转换、Tag 和幂等验证；
6. 每笔事务写入临时 SQLite；
7. 运行 `PRAGMA integrity_check` 和 `PRAGMA foreign_key_check`；
8. 对比事务数、当前记录数、各状态数量、每个 ID 最新 revision 和 canonical current-state digest；
9. fsync 临时数据库及父目录；
10. 使用 `os.replace()` 原子替换为 `memory.db`；
11. 原子写入 `storage-migration-v2.json`；
12. 保留旧 journal 原文件，不重命名、不截断、不删除；
13. 从 SQLite 生成第一版审计导出。

迁移失败只删除 `.importing` 临时文件，原 journal 不变，Repository 进入 degraded。不得创建“暂时继续使用 JSONL”的回退模式。

如果 migration manifest 已标记完成但 `memory.db` 丢失，必须 fail closed 并提示从备份恢复，不能重新导入旧 journal，因为它不包含迁移后的新事务。

## 12. JSONL 审计导出

数据库事务表已经是实时审计来源。JSONL 是便于人工查看、外部备份和离线分析的派生物。

- 每个完整段最多 10,000 笔事务；
- 完整段从 SQLite 查询固定 tx_seq 范围，写入临时文件、fsync 后原子替换；
- 不足一个段的尾部写 `journal-open.jsonl`，每次导出都从 SQLite 原子重建，最多重写 10,000 行；
- `manifest.json` 保存段范围、行数、文件 SHA-256、第一/最后 tx_id；
- 自动导出发生在 Dream 批次完成、显式记忆命令完成和进程启动发现 lag 时；
- 自动导出失败不影响事实提交；`/memory-status` 必须显示 lag；
- `/memory-export-audit --rebuild` 在临时目录完成全量导出并原子替换 audit 目录；
- 审计导出不得包含数据库之外的新字段或模型生成内容。

## 13. 备份与恢复

GitStore 不再承担结构化事实的版本恢复。新增数据库原生管理命令：

- `/memory-log [tx-id]`：读取事务表；
- `/memory-backup`：使用 SQLite backup API 创建一致性备份并执行完整性检查；
- `/memory-restore <backup-id>`：验证备份，先创建恢复前安全备份，再恢复数据库并重建审计导出；
- `/memory-export-audit [--rebuild]`：导出审计 JSONL。

旧 `/dream-log` 与 `/dream-restore` 删除，同时更新帮助和文档。项目仍在开发期，不增加兼容别名和双命令语义。

恢复期间获取 maintenance lock，数据库恢复必须使用 SQLite backup API，不直接复制正在运行的 WAL 数据库。恢复失败时保留当前数据库和安全备份。

## 14. 健康状态与可观测性

`RepositoryHealth` 增加可选字段：

- `backend="sqlite"`
- `schema_version`
- `last_transaction_seq`
- `migration_state`
- `audit_exported_seq`
- `audit_lag`
- `database_bytes`

启动检查：

- schema version 支持；
- `PRAGMA quick_check(1)`；
- foreign keys 开启；
- migration manifest 与数据库来源一致；
- audit lag 只告警，不将 Repository 设为 degraded。

新增日志事件：

- `memory_sqlite_opened`
- `memory_sqlite_transaction_committed`
- `memory_sqlite_busy`
- `memory_storage_migration_started/completed/failed`
- `memory_audit_export_completed/failed`
- `memory_backup_created`
- `memory_restore_completed/failed`

## 15. 安全和隐私

- SQL 参数必须全部使用占位符，禁止拼接用户内容；
- 数据库、临时文件、备份和审计路径必须经过 workspace containment 校验；
- 新建数据库和备份在支持的平台上尽力设置为仅当前用户可读写；
- `/memory-list/show/log` 继续执行调用者 scope 授权；不存在和未授权保持相同响应；
- audit segment 包含记忆正文和 evidence，因此视为敏感文件，不进入 Git；
- recall audit 继续只保存脱敏元数据并保留最多 1000 条。

## 16. 性能边界和验收目标

功能性硬要求：

1. 正常启动和正常写入不得读取旧 `journal.jsonl`。
2. 写入复杂度与历史事务总数无关，只与本次 operations 和相关索引查询有关。
3. 召回 SQL 必须先按 status、scope、kind、expiry 缩小集合。
4. 100,000 笔事务数据集上，重启不得构建全部 revision 的 Python 内存索引。
5. 四进程并发创建/提升测试不得丢事务、重复创建或产生两个同 conflict key 的 active。

基准目标只用于 benchmark 报告，不作为共享 CI 的硬时间断言：

- 100,000 笔事务数据库打开与健康检查目标小于 2 秒；
- 单 operation 写入 p95 目标小于 50ms；
- 单 scope 10,000 active 中候选 SQL 读取目标小于 100ms；
- 数据库恢复和旧 journal 迁移必须输出吞吐与耗时报告。

## 17. 发布与回滚

这是一次单路径切换：

1. 合并前用真实或合成 journal 完成 dry-run 迁移验证；
2. 首次启动自动迁移并保留旧 journal；
3. 迁移完成后所有新写入只进入 SQLite；
4. 回滚新代码前必须先执行 `/memory-export-audit --rebuild` 和 `/memory-backup`；
5. 不允许旧版本直接接管迁移后的 workspace，因为旧 journal 不含新事务；
6. 如必须代码回滚，使用本版本的导出工具生成兼容 JSONL，再由人工确认切换。

## 18. 完成定义

只有同时满足以下条件才算完成：

1. SQLite 是唯一运行时事实库，源码不存在 JSONL Repository 模式选择；
2. 旧 journal 可完整迁移，失败不破坏源文件；
3. Lifecycle、Dream、命令和召回通过既有行为回归测试；
4. Repository 并发、幂等、原子替换、状态转换和故障测试通过；
5. scope 隔离、candidate 不注入和 fail-closed 不变量通过；
6. 审计 JSONL 可分段导出、验证并全量重建；
7. 备份可验证，恢复前有安全备份，恢复后审计可重建；
8. GitStore 不再跟踪 database/audit/backup；
9. 文档、模板、技能说明和命令帮助不再称 journal 为运行时事实源；
10. 全量 Python、WebUI、Ruff 和 compileall 验证通过；
11. 代码和配置中仍不存在 embedding、vector store 或知识图谱入口。
