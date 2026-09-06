# SQLite Memory Storage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将结构化记忆从无限增长且每次写入全量重放的 JSONL Repository，升级为单路径 SQLite 事实库，同时保留现有治理语义、提供旧 journal 一次性迁移、分段 JSONL 审计导出和数据库备份恢复。

**Architecture:** `StructuredMemoryRepository` 保持业务接口和类名，内部改用 Python 标准库 `sqlite3`。SQLite 的事务表和 revision 表是唯一运行时事实源；旧 `journal.jsonl` 只读导入；新审计 JSONL 从数据库按 tx_seq 派生，失败不会回滚正式记忆。Lifecycle、Dream 和词法评分继续依赖 Repository 接口，不接触 SQL。

**Tech Stack:** Python 3.11+、标准库 `sqlite3`、Pydantic 2、pytest、FileLock（仅迁移/恢复/审计目录维护）、Ruff、现有 GitStore。

## Global Constraints

- 设计规格：`docs/superpowers/specs/2026-08-14-sqlite-memory-storage-design.md`。
- 基线：`main@f4700458`；实施必须在独立 worktree/feature branch 中进行，不直接在 main 开发。
- SQLite 是唯一运行时路径；不得增加 `mode`、`backend`、`fallbackToJsonl` 或双写开关。
- 只使用标准库 `sqlite3`；不得引入 SQLAlchemy、aiosqlite、PostgreSQL、Redis 或数据库服务。
- 运行时 SQLite 最低版本为 3.37.0；版本不足必须以 `unsupported_sqlite_version` fail closed，不能静默去掉 STRICT tables。
- 继续禁止 embedding、向量召回、向量数据库和知识图谱。
- `history.jsonl`、`reflections.jsonl` 和 `recall-audit.jsonl` 的现有行为不在本次重写范围。
- 现有 `MemoryRecord`、`MemoryTransaction`、生命周期状态机、评分常量、token budget 和 scope 授权语义不得改变。
- 迁移失败必须 fail closed；不得静默继续使用旧 JSONL Repository。
- 旧 `journal.jsonl` 不删除、不改写、不截断；迁移后的新事务不再写入它。
- 数据库、WAL、SHM、审计、备份、恢复目录和 lock 不进入 GitStore。
- 所有 SQL 使用参数绑定；所有存储路径经过 workspace containment 校验。
- 每项功能先写失败测试，再写最小实现；每个 Task 独立提交。

---

## File Map

### 新建文件

- `erza/agent/memory_sqlite_schema.py`：连接工厂、DDL、PRAGMA、schema version 和升级。
- `erza/agent/memory_jsonl_import.py`：旧 journal 读取、校验、临时数据库导入和 manifest。
- `erza/agent/memory_audit_export.py`：按 tx_seq 分段导出和重建 JSONL audit。
- `erza/agent/memory_backup.py`：SQLite 在线备份、验证和恢复。
- `tests/agent/test_memory_sqlite_schema.py`：schema、索引、PRAGMA 和升级测试。
- `tests/agent/test_memory_jsonl_import.py`：迁移成功、失败、幂等和数据等价测试。
- `tests/agent/test_memory_audit_export.py`：分段、校验、lag 和重建测试。
- `tests/agent/test_memory_backup.py`：备份、损坏拒绝、恢复安全性测试。
- `scripts/benchmark_memory_sqlite.py`：100k 事务基准，不作为共享 CI 时间硬门槛。

### 重点修改文件

- `erza/agent/memory_repository.py`：由 JSONL+进程内索引改为 SQLite Repository。
- `erza/agent/memory_models.py`：增加存储健康/统计模型，不改现有记录字段。
- `erza/agent/memory_recall.py`：使用 Repository 的 scoped candidate 查询。
- `erza/agent/memory.py`：启动迁移、SQLite wiring、存储状态和文件所有权。
- `erza/command/memory.py`：新增 log/backup/restore/export 命令，更新 status。
- `erza/command/builtin.py` 或 Dream 命令注册文件：移除 `/dream-log`、`/dream-restore`。
- `erza/utils/gitstore.py`：停止跟踪旧 journal；忽略 SQLite 运行文件。
- `erza/templates/AGENTS.md`
- `erza/templates/agent/identity.md`
- `erza/templates/memory/POLICY.md`
- `erza/skills/memory/SKILL.md`
- `docs/memory.md`
- `tests/agent/test_memory_repository.py`
- `tests/agent/test_memory_lifecycle.py`
- `tests/agent/test_memory_recall.py`
- `tests/agent/test_memory_store.py`
- `tests/agent/test_memory_commands.py`
- `tests/agent/test_git_store.py`
- `tests/agent/test_structured_memory_boundary.py`
- `tests/agent/test_workspace_memory_routing.py`
- `tests/command/test_builtin_dream.py`

---

### Task 1: 固化 Repository 契约和 SQLite 健康模型

**Files:**
- Modify: `erza/agent/memory_models.py`
- Modify: `tests/agent/test_memory_models.py`
- Create: `tests/agent/test_memory_repository_contract.py`

**Interfaces:**
- Consumes: 现有 `MemoryRecord`、`MemoryTransaction`、`RepositoryHealth`。
- Produces: `MemoryStorageStats`；Repository 必须继续提供设计 §9 列出的既有方法。

- [ ] **Step 1: 为存储统计写失败测试**

在 `tests/agent/test_memory_models.py` 增加：

```python
from erza.agent.memory_models import MemoryStorageStats


def test_memory_storage_stats_is_sqlite_and_non_negative() -> None:
    stats = MemoryStorageStats(
        backend="sqlite",
        schema_version=1,
        transaction_count=3,
        revision_count=5,
        current_count=2,
        last_transaction_seq=3,
        audit_exported_seq=2,
        database_bytes=4096,
    )
    assert stats.audit_lag == 1
    assert stats.backend == "sqlite"
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `pytest tests/agent/test_memory_models.py -q`
Expected: FAIL，提示无法导入 `MemoryStorageStats`。

- [ ] **Step 3: 增加严格统计模型并扩展健康模型**

在 `memory_models.py` 增加：

```python
class MemoryStorageStats(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    backend: Literal["sqlite"] = "sqlite"
    schema_version: int = Field(ge=1)
    transaction_count: int = Field(ge=0)
    revision_count: int = Field(ge=0)
    current_count: int = Field(ge=0)
    last_transaction_seq: int = Field(ge=0)
    audit_exported_seq: int = Field(ge=0)
    database_bytes: int = Field(ge=0)

    @property
    def audit_lag(self) -> int:
        return max(0, self.last_transaction_seq - self.audit_exported_seq)
```

给 `RepositoryHealth` 增加带默认值的字段，保持旧构造调用兼容：

```python
backend: Literal["sqlite"] = "sqlite"
schema_version: int | None = None
last_transaction_seq: int = 0
migration_state: Literal["not_needed", "pending", "completed", "failed"] = "not_needed"
audit_exported_seq: int = 0
database_bytes: int = 0
```

- [ ] **Step 4: 写 Repository 契约测试**

`test_memory_repository_contract.py` 使用 `inspect.signature()` 固化这些名称：

```python
REQUIRED_METHODS = {
    "append_transaction",
    "append_create_if_absent",
    "get",
    "get_current",
    "revisions",
    "current_records",
    "active_for_conflict_key",
    "candidate_records",
    "candidate_ids_for_source",
    "record_created_for",
    "record_ids_for_source",
    "recall_candidates",
    "transaction_log",
    "storage_stats",
}


def test_repository_public_contract() -> None:
    from erza.agent.memory_repository import StructuredMemoryRepository

    assert REQUIRED_METHODS <= set(dir(StructuredMemoryRepository))
```

- [ ] **Step 5: 先给当前类增加三个抛出 `NotImplementedError` 的新方法，使契约测试通过**

只增加签名，不改变旧持久化行为；后续 Task 替换实现。

- [ ] **Step 6: 运行模型和契约测试**

Run: `pytest tests/agent/test_memory_models.py tests/agent/test_memory_repository_contract.py -q`
Expected: PASS。

- [ ] **Step 7: 提交**

```bash
git add erza/agent/memory_models.py tests/agent/test_memory_models.py tests/agent/test_memory_repository_contract.py
git commit -m "test(memory): define sqlite repository contract"
```

---

### Task 2: 建立 SQLite schema 和连接工厂

**Files:**
- Create: `erza/agent/memory_sqlite_schema.py`
- Create: `tests/agent/test_memory_sqlite_schema.py`

**Interfaces:**
- Produces: `SCHEMA_VERSION: int`、`connect_memory_db(path, lock_timeout_s)`、`initialize_schema(connection)`、`check_schema(connection)`。
- Consumes: Task 3 的 Repository 只通过这些函数建立连接，不复制 PRAGMA/DDL。

- [ ] **Step 1: 写新数据库初始化失败测试**

```python
def test_initialize_schema_creates_required_tables_and_indexes(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    with connect_memory_db(db, lock_timeout_s=0.2) as connection:
        initialize_schema(connection)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "storage_meta",
            "memory_transactions",
            "memory_revisions",
            "memory_creation_keys",
            "memory_source_batches",
            "memory_tags",
            "memory_aliases",
        } <= tables
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
```

- [ ] **Step 2: 写 SQLite 版本、PRAGMA 和不支持 schema 测试**

断言 `sqlite3.sqlite_version_info >= (3, 37, 0)`、`foreign_keys=1`、`journal_mode=wal`、`synchronous` 为 FULL；版本检查函数接收可测试的 version tuple，低于 `(3, 37, 0)` 时抛出 error code=`unsupported_sqlite_version`。手工把 `user_version` 改为 999 后，`check_schema()` 抛出 `RepositoryDegradedError`，不得自动降级或重建。

- [ ] **Step 3: 运行并确认失败**

Run: `pytest tests/agent/test_memory_sqlite_schema.py -q`
Expected: FAIL，模块不存在。

- [ ] **Step 4: 实现连接工厂和完整 DDL**

连接必须使用 URI 之外的普通 workspace 路径、`isolation_level=None`、`timeout=lock_timeout_s` 和 `row_factory=sqlite3.Row`。DDL 必须包含设计 §7 的表、唯一约束、外键和 partial indexes。辅助表使用：

```sql
CREATE TABLE memory_creation_keys (
    source_batch TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    created_tx_seq INTEGER NOT NULL REFERENCES memory_transactions(tx_seq),
    PRIMARY KEY (source_batch, content_hash)
) STRICT;

CREATE TABLE memory_source_batches (
    memory_id TEXT NOT NULL,
    source_batch TEXT NOT NULL,
    first_tx_seq INTEGER NOT NULL REFERENCES memory_transactions(tx_seq),
    PRIMARY KEY (memory_id, source_batch)
) STRICT;

CREATE TABLE memory_tags (
    memory_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    tag TEXT NOT NULL,
    PRIMARY KEY (memory_id, revision, tag),
    FOREIGN KEY (memory_id, revision)
        REFERENCES memory_revisions(memory_id, revision)
) STRICT;

CREATE INDEX ix_memory_tags_tag ON memory_tags(tag, memory_id, revision);
```

`memory_aliases` 使用同样复合外键，列名为 `alias_norm`。

- [ ] **Step 5: 增加 schema 重入和并发初始化测试**

初始化同一数据库两次必须成功；两个线程同时初始化只能得到一个完整 schema，不能留下部分表。

- [ ] **Step 6: 运行测试和 Ruff**

Run: `pytest tests/agent/test_memory_sqlite_schema.py -q`
Expected: PASS。
Run: `ruff check erza/agent/memory_sqlite_schema.py tests/agent/test_memory_sqlite_schema.py`
Expected: PASS。

- [ ] **Step 7: 提交**

```bash
git add erza/agent/memory_sqlite_schema.py tests/agent/test_memory_sqlite_schema.py
git commit -m "feat(memory): add sqlite schema and connection policy"
```

---

### Task 3: 用 SQLite 实现 Repository 读取路径

**Files:**
- Modify: `erza/agent/memory_repository.py`
- Modify: `tests/agent/test_memory_repository.py`

**Interfaces:**
- Consumes: `connect_memory_db()`、`initialize_schema()`、现有 Pydantic models。
- Produces: SQLite 版本 `get`、`get_current`、`revisions`、`current_records`、冲突/来源查询、`storage_stats`、`transaction_log`。

- [ ] **Step 1: 把 Repository fixture 的路径断言改成 SQLite**

保留测试中 `StructuredMemoryRepository(workspace, lock_timeout_s=0.1)` 的构造方式，新增：

```python
def test_repository_uses_canonical_sqlite_path(repository) -> None:
    assert repository.database_path == (
        repository.workspace / "memory" / "structured" / "memory.db"
    )
    assert repository.health.backend == "sqlite"
    assert repository.health.state == "healthy"
```

- [ ] **Step 2: 增加数据库种子 helper 和读取失败测试**

测试 helper 必须通过 SQL 插入一笔 transaction 和一条 current revision，再断言所有读取方法返回经 `MemoryRecord.model_validate_json()` 重建的对象，不能返回裸 dict。

- [ ] **Step 3: 运行并确认失败**

Run: `pytest tests/agent/test_memory_repository.py -q -x`
Expected: FAIL 于 `database_path` 或 SQLite 读取断言。

- [ ] **Step 4: 替换 Repository 初始化和读取实现**

删除 `_current`、`_revision_history` 和全部进程内倒排字典。增加内部转换函数：

```python
@staticmethod
def _record_from_row(row: sqlite3.Row | None) -> MemoryRecord | None:
    if row is None:
        return None
    return MemoryRecord.model_validate_json(row["record_json"])
```

所有 current 查询必须包含 `is_current = 1`；`current_records(status)` 最终按 `memory_id` 排序；`revisions()` 按 revision 升序；`transaction_log()` 按 tx_seq 倒序返回，指定 tx_id 时最多一条。

- [ ] **Step 5: 实现 scoped candidate SQL**

`recall_candidates()` 动态生成 scope 占位符，但不得插入 scope 内容：

```python
scope_clause = " OR ".join("(scope_kind = ? AND scope_key = ?)" for _ in scopes)
params = [value for scope in scopes for value in (scope.kind.value, scope.key)]
```

空 scope 立即返回 `()`；SQL 固定过滤 current、active、expiry 和 requested kinds，返回按 memory_id 排序的完整记录。

- [ ] **Step 6: 实现 health 和 storage stats**

初始化运行 `check_schema()` 和 `PRAGMA quick_check(1)`；失败时 health degraded，读取/写入事实正文均 fail closed。`storage_stats()` 使用 `COUNT(*)`、`MAX(tx_seq)` 和 `Path.stat().st_size`，审计 exported seq 暂读 `storage_meta` 的 `audit_exported_seq`，缺失为 0。

- [ ] **Step 7: 运行 Repository 读取测试**

Run: `pytest tests/agent/test_memory_repository.py -q`
Expected: 尚未改写的 JSONL 写入测试可以失败；所有读取、排序、health 和查询测试通过。使用 `-k "read or current or revision or source or stats or health"` 单独确认读取组全绿。

- [ ] **Step 8: 提交**

```bash
git add erza/agent/memory_repository.py tests/agent/test_memory_repository.py
git commit -m "refactor(memory): serve repository reads from sqlite"
```

---

### Task 4: 实现 SQLite 原子写入、幂等和并发

**Files:**
- Modify: `erza/agent/memory_repository.py`
- Modify: `tests/agent/test_memory_repository.py`
- Modify: `tests/agent/test_memory_lifecycle.py`

**Interfaces:**
- Produces: 完整 `append_transaction()` 和 `append_create_if_absent()`；异常类型保持现有调用者兼容。
- Consumes: 现有 `assert_transition()`、`validate_same_status_revision()`、`transaction_checksum()` 和 TagCatalog。

- [ ] **Step 1: 为单事务原子性写失败测试**

覆盖：新建、revision 更新、多 operation 替换、checksum 错、expected revision 错、非法状态、未知 tag、重复 tx_id。多 operation 中第二项失败时，事务表和第一项 revision 都必须为 0 行。

- [ ] **Step 2: 为数据库约束写失败测试**

直接构造两个相同 conflict key 的 active，断言第二个事务失败且数据库只有一个 active；两个相同 `(source_batch, content_hash)` 创建必须返回同一 ID。

- [ ] **Step 3: 运行并确认失败**

Run: `pytest tests/agent/test_memory_repository.py -q -x`
Expected: FAIL 于仍使用 JSONL 的 append 路径或缺失 SQL 写入。

- [ ] **Step 4: 实现 `_append_in_connection()`**

内部函数必须接收已执行 `BEGIN IMMEDIATE` 的连接：

```python
def _append_in_connection(
    self,
    connection: sqlite3.Connection,
    transaction: MemoryTransaction,
) -> None:
    validated = self._validate_against_database(connection, transaction)
    tx_seq = self._insert_transaction(connection, validated)
    for op_index, operation in enumerate(validated.operations):
        self._insert_revision(connection, tx_seq, op_index, operation.record)
```

`_insert_revision()` 必须依次：把旧 current 改为 0、插入 revision、插入 tags/aliases、累计 source batch、仅 revision 1 插入 creation key。所有步骤处于同一 SQLite transaction。

- [ ] **Step 5: 实现异常映射**

```python
except sqlite3.OperationalError as exc:
    connection.rollback()
    if "locked" in str(exc).casefold() or "busy" in str(exc).casefold():
        raise MemoryLockTimeout(
            f"sqlite lock timeout after {self.lock_timeout_s}s"
        ) from exc
    self._degrade("sqlite_operational_error", str(exc))
    raise MemoryWriteError(str(exc)) from exc
```

`sqlite3.IntegrityError` 必须根据约束和当前数据转换为 `DuplicateMemoryIdempotencyKey`、`MemoryRevisionConflict` 或 `InvalidMemoryTransition`，不能把数据库原始报错直接返回用户。

- [ ] **Step 6: 实现 `append_create_if_absent()` 的单事务幂等**

在 `BEGIN IMMEDIATE` 后先查 creation key；存在则返回当前记录和 `False`；不存在才验证并写入。并发冲突时重新读取 creation key 并返回胜出记录，不得创建重复 ID。

- [ ] **Step 7: 写四进程并发测试**

使用 `multiprocessing.get_context("spawn")` 启动四个 worker，每个打开独立 Repository。覆盖：

- 100 个不同创建全部存在；
- 20 个相同 creation key 最终只有一个 memory ID；
- 两个进程竞争同一 expected revision 只有一个成功；
- active conflict unique index 永远不允许两个当前 active。

- [ ] **Step 8: 运行 Repository 与 Lifecycle 测试**

Run: `pytest tests/agent/test_memory_repository.py tests/agent/test_memory_lifecycle.py -q`
Expected: PASS；旧测试中直接检查 journal 文本的断言改为检查 `transaction_log()`，不能删除其业务覆盖。

- [ ] **Step 9: 提交**

```bash
git add erza/agent/memory_repository.py tests/agent/test_memory_repository.py tests/agent/test_memory_lifecycle.py
git commit -m "feat(memory): commit governed revisions atomically in sqlite"
```

---

### Task 5: 实现旧 journal 一次性、可验证迁移

**Files:**
- Create: `erza/agent/memory_jsonl_import.py`
- Create: `tests/agent/test_memory_jsonl_import.py`
- Modify: `erza/agent/memory_repository.py`

**Interfaces:**
- Produces: `JsonlImportResult`、`migrate_legacy_journal(workspace, lock_timeout_s) -> JsonlImportResult`。
- Consumes: `StructuredMemoryRepository` 的内部导入入口必须执行与正常写入相同的事务验证，但不得触发 audit export。

- [ ] **Step 1: 写成功迁移失败测试**

测试创建三笔旧格式 canonical `MemoryTransaction`，包含 create、promote 和第二条候选。迁移后断言：

```python
assert result.migrated is True
assert result.transaction_count == 3
assert result.source_sha256 == hashlib.sha256(journal.read_bytes()).hexdigest()
assert journal.read_bytes() == original_bytes
assert repository.current_records() == expected_current_records
assert repository.revisions(memory_id) == expected_revisions
```

- [ ] **Step 2: 写损坏和中断测试**

覆盖 invalid JSON、checksum mismatch、revision 跳号、未知 tag、第二行失败、`os.replace` 失败。每种情况必须满足：

- `journal.jsonl` 字节不变；
- `memory.db` 不存在；
- `memory.db.importing*` 被清理；
- manifest 不标 completed；
- 错误包含行号和稳定 error code，不回显完整 evidence。

- [ ] **Step 3: 运行并确认失败**

Run: `pytest tests/agent/test_memory_jsonl_import.py -q`
Expected: FAIL，模块不存在。

- [ ] **Step 4: 实现严格旧日志迭代器**

```python
def iter_legacy_transactions(path: Path) -> Iterator[tuple[int, MemoryTransaction]]:
    with path.open("r", encoding="utf-8") as stream:
        for line_number, raw in enumerate(stream, start=1):
            if not raw.strip():
                continue
            try:
                value = json.loads(raw)
                transaction = MemoryTransaction.model_validate(value)
            except (json.JSONDecodeError, ValidationError) as exc:
                raise LegacyJournalImportError(line_number, "invalid_transaction") from exc
            if transaction_checksum(transaction) != transaction.checksum_sha256:
                raise LegacyJournalImportError(line_number, "checksum_mismatch")
            yield line_number, transaction
```

不得跳过任何非空坏行，也不得用 `json-repair` 修复事实日志。

- [ ] **Step 5: 实现临时数据库导入与原子切换**

使用唯一临时文件名 `memory.db.importing-<pid>-<uuid>`；逐笔调用 Repository 的受验证导入函数。完成后执行：

```python
assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
```

计算 canonical current-state digest：按 memory_id 排序，把每个 current `record_json` 加换行后做 SHA-256。把 source digest、计数、各状态计数、current digest、完成时间写入 manifest 临时文件，数据库和 manifest 都 fsync 后再 `os.replace()`。

- [ ] **Step 6: 实现启动决策矩阵测试**

| memory.db | journal | completed manifest | 结果 |
|---|---|---|---|
| 无 | 无/空 | 无 | 创建全新 SQLite |
| 无 | 非空 | 无 | 执行迁移 |
| 有 | 任意 | 有/无 | 打开 SQLite，不再读取 journal |
| 无 | 任意 | completed | degraded，提示数据库丢失 |
| `.importing` 残留 | 非空 | 无 | 删除残留后重新迁移 |

用 monkeypatch 让旧 journal 的 `open()` 在已有 DB 场景抛异常，证明正常启动完全不读旧日志。

- [ ] **Step 7: 运行迁移与 Repository 测试**

Run: `pytest tests/agent/test_memory_jsonl_import.py tests/agent/test_memory_repository.py -q`
Expected: PASS。

- [ ] **Step 8: 提交**

```bash
git add erza/agent/memory_jsonl_import.py erza/agent/memory_repository.py tests/agent/test_memory_jsonl_import.py
git commit -m "feat(memory): migrate legacy journal into sqlite"
```

---

### Task 6: 实现可重建的分段 JSONL 审计导出

**Files:**
- Create: `erza/agent/memory_audit_export.py`
- Create: `tests/agent/test_memory_audit_export.py`
- Modify: `erza/agent/memory_repository.py`

**Interfaces:**
- Produces: `MemoryAuditExporter.export_pending()`、`MemoryAuditExporter.rebuild()`、`AuditExportResult`。
- Consumes: Repository 提供只读的 `(tx_seq, transaction_json)` 范围查询；固定 `SEGMENT_SIZE = 10_000`。

- [ ] **Step 1: 写小 segment 的失败测试**

测试通过构造 `MemoryAuditExporter(..., segment_size=3)` 写入 8 笔事务，期望：

```text
audit/journal-000000000001-000000000003.jsonl
audit/journal-000000000004-000000000006.jsonl
audit/journal-open.jsonl             # tx 7..8
audit/manifest.json
```

每个 sealed segment 恰好三行，open 两行，所有行逐条等于数据库 `transaction_json`，manifest 的 SHA-256 与文件一致。

- [ ] **Step 2: 写崩溃安全和重建测试**

monkeypatch 临时文件 `write/fsync/os.replace` 分别失败，断言旧 audit 目录仍可读、数据库事务不回滚、`storage_stats().audit_lag > 0`。修复后 `rebuild()` 必须生成与干净导出逐字节相同的目录。

- [ ] **Step 3: 运行并确认失败**

Run: `pytest tests/agent/test_memory_audit_export.py -q`
Expected: FAIL，模块不存在。

- [ ] **Step 4: 实现范围导出**

完整段直接从数据库读取固定区间；尾段最多 10,000 行。写文件统一使用：

```python
with temp_path.open("w", encoding="utf-8", newline="\n") as stream:
    for transaction_json in rows:
        stream.write(transaction_json)
        stream.write("\n")
    stream.flush()
    os.fsync(stream.fileno())
os.replace(temp_path, destination)
```

manifest 使用 canonical JSON，字段固定为 `schema_version`、`generated_at`、`database_last_tx_seq`、`segments`；segment 字段固定为 `path`、`first_tx_seq`、`last_tx_seq`、`rows`、`sha256`、`first_tx_id`、`last_tx_id`。

- [ ] **Step 5: 实现整目录重建**

`rebuild()` 在同级唯一临时目录生成完整审计，验证全部段后：

1. 获取 `memory-maintenance.lock`；
2. 已有 audit 原子移动到 `recovery/<UTC>/audit`；
3. 临时目录原子移动为 `audit`；
4. 更新 `storage_meta.audit_exported_seq`。

任何一步失败都保留数据库和可恢复旧目录。

- [ ] **Step 6: 接入自动导出触发点但不影响事实提交**

Repository 提交只更新事实库；`MemoryStore` 在 Dream 成功批次、显式 memory 修改命令返回前和启动发现 lag 时调用 `export_pending()`。捕获导出异常，记录 `memory_audit_export_failed`，不得把已提交的 memory command 改成失败。

- [ ] **Step 7: 运行审计测试**

Run: `pytest tests/agent/test_memory_audit_export.py tests/agent/test_dream_structured_memory.py tests/agent/test_memory_commands.py -q`
Expected: PASS。

- [ ] **Step 8: 提交**

```bash
git add erza/agent/memory_audit_export.py erza/agent/memory_repository.py tests/agent/test_memory_audit_export.py tests/agent/test_dream_structured_memory.py tests/agent/test_memory_commands.py
git commit -m "feat(memory): export rebuildable segmented audit journals"
```

---

### Task 7: 将召回候选过滤下推到 SQLite

**Files:**
- Modify: `erza/agent/memory_recall.py`
- Modify: `tests/agent/test_memory_recall.py`
- Modify: `tests/agent/test_context_structured_memory.py`
- Modify: `tests/agent/test_workspace_memory_routing.py`

**Interfaces:**
- Consumes: Task 3 的 `repository.recall_candidates(...)`。
- Produces: 与当前完全相同的 `RecallResult`、分数、Why、排序和 prompt 文本。

- [ ] **Step 1: 写候选 SQL 调用测试**

用 spy Repository 断言 `StructuredMemoryRecall.recall()` 不再调用 `current_records(ACTIVE)`，而是准确传递：

```python
repository.recall_candidates.assert_called_once_with(
    allowed_scopes=query.allowed_scopes,
    requested_kinds=query.requested_kinds,
    now=query.now,
)
```

- [ ] **Step 2: 写结果等价参数化测试**

把现有 fixture 的同一批记录分别交给旧纯 Python baseline helper 与新 SQLite recall，逐字段比较：

- hit IDs 和顺序；
- score/reasons；
- candidates/filtered/excluded_by_budget；
- tokens_used 和 render_prompt；
- CJK 子串、ASCII 边界、显式 ID、tag、catalog alias、record alias；
- expired、candidate、terminal、未授权 scope。

- [ ] **Step 3: 运行并确认失败**

Run: `pytest tests/agent/test_memory_recall.py -q -x`
Expected: FAIL，spy 显示仍调用 `current_records()`。

- [ ] **Step 4: 修改 recall 候选来源**

替换循环开头：

```python
records = self._repository.recall_candidates(
    allowed_scopes=query.allowed_scopes,
    requested_kinds=query.requested_kinds,
    now=query.now,
)
for record in records:
    candidates += 1
    # 后续 route、score、sort、budget 保持现有实现
```

SQL 已过滤的 scope/status/kind/expiry 不在 Python 重复计为 filtered。更新计数语义文档和测试：`candidates` 表示数据库授权预过滤后的候选数量，`filtered` 表示未命中词法 route 的数量。

- [ ] **Step 5: 验证 SQL 查询计划**

新增测试运行 `EXPLAIN QUERY PLAN`，断言使用 `ix_memory_recall_scope` 或等价 partial index，不能出现对全部 revision 的无条件 scan。不要断言 SQLite 内部易变的完整字符串，只断言 detail 包含索引名。

- [ ] **Step 6: 运行召回、Context 和 workspace 隔离测试**

Run: `pytest tests/agent/test_memory_recall.py tests/agent/test_context_structured_memory.py tests/agent/test_workspace_memory_routing.py -q`
Expected: PASS，prompt 快照除 candidates/filtered 诊断计数外无变化；任何正文变化必须解释并获得审查，不可直接更新快照。

- [ ] **Step 7: 提交**

```bash
git add erza/agent/memory_recall.py tests/agent/test_memory_recall.py tests/agent/test_context_structured_memory.py tests/agent/test_workspace_memory_routing.py
git commit -m "perf(memory): prefilter recall candidates in sqlite"
```

---

### Task 8: 接入 MemoryStore 单路径和文件所有权

**Files:**
- Modify: `erza/agent/memory.py`
- Modify: `erza/utils/gitstore.py`
- Modify: `tests/agent/test_memory_store.py`
- Modify: `tests/agent/test_git_store.py`
- Modify: `tests/agent/test_structured_memory_boundary.py`

**Interfaces:**
- Consumes: Task 5 迁移器、Task 6 exporter、SQLite Repository。
- Produces: 每个 effective workspace 独立 `memory.db`；MemoryStore 初始化后始终只有 SQLite stack。

- [ ] **Step 1: 写 MemoryStore 路径和启动矩阵失败测试**

断言：

- 新 workspace 创建 `memory.db`；
- 旧 journal workspace 自动迁移；
- 已迁移 workspace 第二次打开不访问 journal；
- 不同 workspace 的 DB 完全隔离；
- manifest completed 但 DB 丢失时 health degraded；
- `structured_repository`、lifecycle、recall 始终初始化，不存在 backend/mode 属性。

- [ ] **Step 2: 更新 writer whitelist**

删除对运行时追加 `journal.jsonl` 的含义，增加：

```python
"memory/structured/memory.db": {"memory_store"},
"memory/structured/storage-migration-v2.json": {"memory_store"},
"memory/structured/audit": {"memory_store"},
"memory/structured/backups": {"memory_store"},
```

路径目录项的校验必须使用 containment/prefix 规则；主 Agent 仍禁止直接写 structured 下任何文件。

- [ ] **Step 3: 修改 `_build_structured_stack()`**

在构造 Repository 前执行启动决策：新库初始化或旧 journal 迁移；构造后创建 lifecycle、recall 和 exporter。不得保留旧 `rebuild()` 全量日志语义。

- [ ] **Step 4: 更新 GitStore tracked/ignored 文件**

`GOVERNED_MEMORY_TRACKED_FILES` 移除 `memory/structured/journal.jsonl`，保留 tags/POLICY/SOUL。workspace Git ignore 必须包含：

```gitignore
memory/structured/memory.db
memory/structured/memory.db-wal
memory/structured/memory.db-shm
memory/structured/audit/
memory/structured/backups/
memory/structured/recovery/
memory/structured/*.lock
```

测试断言数据库路径从未进入 `GitStore._tracked_files`。

- [ ] **Step 5: 移除 `restore_memory_version()` 的 Git journal 恢复**

MemoryStore 不再通过 Git revert 恢复结构化事实；方法先保留为内部删除点，等 Task 9 完成新 backup API 后彻底移除调用。此 Task 中所有调用测试改为预期明确 `NotImplementedError("use memory backup restore")`，防止静默只恢复 audit 而不恢复 DB。

- [ ] **Step 6: 运行 Store、边界和 Git 测试**

Run: `pytest tests/agent/test_memory_store.py tests/agent/test_structured_memory_boundary.py tests/agent/test_git_store.py tests/agent/test_workspace_memory_routing.py -q`
Expected: PASS。

- [ ] **Step 7: 提交**

```bash
git add erza/agent/memory.py erza/utils/gitstore.py tests/agent/test_memory_store.py tests/agent/test_git_store.py tests/agent/test_structured_memory_boundary.py tests/agent/test_workspace_memory_routing.py
git commit -m "refactor(memory): wire sqlite as the only runtime store"
```

---

### Task 9: 增加数据库日志、备份、恢复和审计命令

**Files:**
- Create: `erza/agent/memory_backup.py`
- Create: `tests/agent/test_memory_backup.py`
- Modify: `erza/command/memory.py`
- Modify: `erza/command/builtin.py`
- Modify: `tests/agent/test_memory_commands.py`
- Modify: `tests/command/test_builtin_dream.py`

**Interfaces:**
- Produces: `MemoryBackupManager.create_backup()`、`restore_backup()`；命令 `/memory-log`、`/memory-backup`、`/memory-restore`、`/memory-export-audit`。
- Consumes: Repository `transaction_log/storage_stats`、Task 6 exporter。

- [ ] **Step 1: 写一致性备份失败测试**

```python
def test_backup_is_integrity_checked_snapshot(tmp_path: Path) -> None:
    store = seeded_store(tmp_path)
    result = MemoryBackupManager(store.structured_repository).create_backup()
    assert result.path.parent == tmp_path / "memory" / "structured" / "backups"
    with sqlite3.connect(result.path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_transactions"
        ).fetchone()[0] == 1
```

备份必须使用 `source_connection.backup(destination_connection)`，禁止 `shutil.copyfile(memory.db)`。

- [ ] **Step 2: 写恢复安全测试**

覆盖：合法恢复、损坏 DB、错误 schema、workspace 外路径、恢复过程失败。合法恢复必须先生成 `recovery/<UTC>/memory-before-restore.db`；失败时当前数据库记录不变。恢复成功后 Repository health healthy、revision/count 与备份一致、audit 通过 `rebuild()` 与恢复后数据库一致。

- [ ] **Step 3: 运行并确认失败**

Run: `pytest tests/agent/test_memory_backup.py -q`
Expected: FAIL，模块不存在。

- [ ] **Step 4: 实现 backup manager**

公开结果使用 frozen Pydantic/dataclass，至少包含 `backup_id`、`path`、`created_at`、`last_transaction_seq`、`sha256`。恢复流程：

1. 解析 backup id，只允许 `backups/` 下 canonical 文件；
2. 只读打开备份，校验 `user_version`、`integrity_check` 和 `foreign_key_check`；
3. 获取 maintenance lock；
4. 用 SQLite backup API 创建恢复前安全备份；
5. 用 backup API 把目标复制到 live connection；
6. 重新执行 quick check 和 health 刷新；
7. 重建 audit；
8. 返回安全备份 ID 和恢复后的 tx_seq。

- [ ] **Step 5: 为新命令写失败测试**

命令行为：

- `/memory-status` 增加 backend/schema/transactions/revisions/current/database size/audit lag/migration；
- `/memory-log` 默认最近 20 笔，只显示 tx_id、时间、actor、reason、record IDs，不显示完整 evidence；
- `/memory-log <tx-id>` 显示该事务 operations，但 evidence excerpt 仍截断到 200 字符；
- `/memory-backup` 返回 backup id 和校验摘要；
- `/memory-restore <backup-id>` 恢复并显示自动安全备份 id；
- `/memory-export-audit` 导出 pending；`--rebuild` 全量重建；
- 未授权调用不能利用 log/backup 路径绕过 scope 权限。

- [ ] **Step 6: 实现命令并移除旧 Dream Git 命令**

删除 `/dream-log`、`/dream-restore` 注册、帮助、测试 fixture 和对 Git SHA diff 的依赖。`/dream` 本身保留不变。不要增加兼容 alias；这是开发阶段单路径清理。

- [ ] **Step 7: 运行备份和命令测试**

Run: `pytest tests/agent/test_memory_backup.py tests/agent/test_memory_commands.py tests/command/test_builtin_dream.py -q`
Expected: PASS。

- [ ] **Step 8: 提交**

```bash
git add erza/agent/memory_backup.py erza/command/memory.py erza/command/builtin.py tests/agent/test_memory_backup.py tests/agent/test_memory_commands.py tests/command/test_builtin_dream.py
git commit -m "feat(memory): add sqlite audit backup and restore commands"
```

---

### Task 10: 清理 JSONL 运行时实现和更新全部说明

**Files:**
- Modify: `erza/agent/memory_repository.py`
- Modify: `erza/templates/AGENTS.md`
- Modify: `erza/templates/agent/identity.md`
- Modify: `erza/templates/memory/POLICY.md`
- Modify: `erza/skills/memory/SKILL.md`
- Modify: `docs/memory.md`
- Modify: `erza/channels/websocket/handlers/bootstrap_file.py`
- Modify: `tests/agent/test_context_prompt_cache.py`
- Modify: `tests/agent/test_onboard_logic.py`
- Modify: `tests/agent/test_memory_store.py`

**Interfaces:**
- Produces: 单路径 SQLite 用户文档和不含旧写入代码的 Repository。
- Consumes: Tasks 1–9 的最终命令和文件布局。

- [ ] **Step 1: 添加“禁止旧运行时残留”的边界测试**

在 `tests/agent/test_structured_memory_boundary.py` 扫描 Python 源码和配置，断言不存在：

- `open(journal_path, "a"...)` 或 `.journal_path.open("a"...)`；
- `_synchronize_locked()`、`_replay_line()`、`_clear_index()` 旧 Repository 方法；
- `structuredMemory.mode/backend/fallback` 配置；
- SQLite 事实库之外的第二 Repository 实例；
- `embedding`、`vector store`、`faiss`、`chromadb` 等入口。

允许 `journal.jsonl` 字符串只出现在迁移器、迁移测试和“旧迁移输入”文档上下文。

- [ ] **Step 2: 删除旧 JSONL Repository 私有实现**

从 `memory_repository.py` 删除文件 append/replay、全量进程索引、revision history dict 和 file-lock 写协议。旧日志解析只存在于 `memory_jsonl_import.py`。保持文件职责单一；若 Repository 超过约 600 行，将 SQL row conversion/statement constants 移入 `memory_sqlite_schema.py`，不要重新形成巨型文件。

- [ ] **Step 3: 更新模板和 memory skill**

统一使用以下用户概念：

```text
长期事实由 memory/structured/memory.db 中的受治理记录保存。
不要直接编辑数据库或 memory/structured/ 下的运行文件；使用 /memory-* 命令。
journal.jsonl 仅是旧版本迁移输入，audit/*.jsonl 是可重建审计导出。
```

不得告诉主 Agent 用 filesystem/sqlite CLI 直接修改数据库。

- [ ] **Step 4: 重写 `docs/memory.md` 存储与故障章节**

文档必须包括：新数据流、SQLite 表职责、旧 journal 自动迁移、audit lag、backup/restore、完整命令表、degraded 处理、无向量边界。不再出现“三模式”“旧用户兼容”或“journal 是唯一事实源”。

- [ ] **Step 5: 更新 WebSocket bootstrap 文件可见性**

数据库、WAL、SHM、备份和 audit 不得作为可在线编辑 bootstrap 文件暴露；`tags.json` 和 `POLICY.md` 保持现有只读/管理语义。对应测试断言敏感文件不出现在列表。

- [ ] **Step 6: 运行边界、模板和 onboarding 测试**

Run: `pytest tests/agent/test_structured_memory_boundary.py tests/agent/test_context_prompt_cache.py tests/agent/test_onboard_logic.py tests/agent/test_memory_store.py -q`
Expected: PASS。

- [ ] **Step 7: 执行残留搜索**

Run: `rg -n "journal\.jsonl|dream-log|dream-restore|_synchronize_locked|_replay_line" erza docs tests`
Expected: `journal.jsonl` 只出现在迁移相关位置；旧命令和旧私有方法零结果。

- [ ] **Step 8: 提交**

```bash
git add erza/agent/memory_repository.py erza/templates erza/skills/memory/SKILL.md docs/memory.md erza/channels/websocket/handlers/bootstrap_file.py tests/agent/test_structured_memory_boundary.py tests/agent/test_context_prompt_cache.py tests/agent/test_onboard_logic.py tests/agent/test_memory_store.py
git commit -m "docs(memory): document sqlite single-path storage"
```

---

### Task 11: 加入规模、并发和故障基准

**Files:**
- Create: `scripts/benchmark_memory_sqlite.py`
- Modify: `tests/agent/test_memory_repository.py`
- Modify: `tests/agent/test_memory_jsonl_import.py`
- Modify: `tests/agent/test_memory_audit_export.py`

**Interfaces:**
- Produces: 可重复的 100k transaction benchmark JSON 报告；CI 使用小规模结构性测试，不使用脆弱 wall-clock 断言。

- [ ] **Step 1: 写基准脚本参数解析测试或可导入 smoke test**

脚本参数固定：

```text
--workspace PATH
--transactions 100000
--active-per-scope 10000
--writers 4
--json-output PATH
--keep
```

默认创建安全临时 workspace；没有 `--keep` 时只删除该脚本创建且经过 resolved containment 验证的目录。

- [ ] **Step 2: 实现确定性数据生成**

使用固定随机种子和现有 MemoryRecord helper，报告至少包含：Python/SQLite/OS 版本、DB bytes、insert throughput、startup/health time、append p50/p95、scoped candidate query p50/p95、audit export throughput、migration throughput、peak RSS（平台支持时）。

- [ ] **Step 3: 增加结构性性能测试**

测试不得断言“必须小于 N 毫秒”，而应断言：

- 打开已有 DB 时 monkeypatch 旧 journal 读取会抛错但启动成功；
- 写一笔事务执行的 SQL 数量有固定上界，不随历史从 100 增到 10,000 而增加；
- Repository 不保存 `_revision_history` 或全部 current Python dict；
- recall query plan 使用 scope partial index；
- audit open segment 重建最多读取 `SEGMENT_SIZE` 行对应的事务范围。

- [ ] **Step 4: 运行小规模 CI 组**

Run: `pytest tests/agent/test_memory_repository.py tests/agent/test_memory_jsonl_import.py tests/agent/test_memory_audit_export.py -q`
Expected: PASS。

- [ ] **Step 5: 运行 100k 本地基准**

Run: `python scripts/benchmark_memory_sqlite.py --transactions 100000 --active-per-scope 10000 --writers 4 --json-output .benchmark-memory-sqlite.json`
Expected: exit 0，生成有效 JSON；人工对照设计 §16 记录结果。`.benchmark-memory-sqlite.json` 不提交。

如果目标未达到，先用 `EXPLAIN QUERY PLAN` 和 profiler 定位；不得通过减少 fsync、关闭 FULL synchronous、跳过校验或降低测试数据量伪造达标。

- [ ] **Step 6: 提交**

```bash
git add scripts/benchmark_memory_sqlite.py tests/agent/test_memory_repository.py tests/agent/test_memory_jsonl_import.py tests/agent/test_memory_audit_export.py
git commit -m "test(memory): add sqlite scale and recovery benchmarks"
```

---

### Task 12: 全量回归、迁移演练和完成审查

**Files:**
- Modify only if verification finds a scoped defect; do not perform unrelated refactors.

**Interfaces:**
- Produces: 可合并的单路径 SQLite 记忆系统。

- [ ] **Step 1: 运行记忆专项测试**

Run:

```bash
pytest \
  tests/agent/test_memory_models.py \
  tests/agent/test_memory_sqlite_schema.py \
  tests/agent/test_memory_repository_contract.py \
  tests/agent/test_memory_repository.py \
  tests/agent/test_memory_jsonl_import.py \
  tests/agent/test_memory_audit_export.py \
  tests/agent/test_memory_backup.py \
  tests/agent/test_memory_lifecycle.py \
  tests/agent/test_memory_recall.py \
  tests/agent/test_memory_store.py \
  tests/agent/test_memory_commands.py \
  tests/agent/test_dream_structured_memory.py \
  tests/agent/test_context_structured_memory.py \
  tests/agent/test_structured_memory_boundary.py \
  tests/agent/test_workspace_memory_routing.py \
  -q
```

Expected: PASS；skips 只能是基线已有且原因明确的 skip。

- [ ] **Step 2: 运行 Python 全量测试**

Run: `pytest -q`
Expected: PASS。

- [ ] **Step 3: 运行 WebUI 回归和 build**

先读取 `webui/package.json` 的 package manager scripts，使用仓库锁文件对应命令。基线命令预期为：

```bash
cd webui
npm test -- --run
npm run build
```

Expected: tests PASS，build exit 0。

- [ ] **Step 4: 运行静态和编译检查**

Run: `ruff check erza tests scripts/benchmark_memory_sqlite.py`
Expected: PASS。
Run: `python -m compileall -q erza`
Expected: exit 0。

- [ ] **Step 5: 做真实旧 journal 迁移演练**

复制一个测试 workspace 到临时目录，记录旧 journal SHA-256，启动新 MemoryStore，执行 `/memory-status`、一次 recall、一次 create/promote、audit rebuild、backup、restore。验收：

- 原 journal SHA-256 不变；
- current records/revisions 与迁移前重放结果一致；
- 新事务只存在 SQLite 与 audit，不追加旧 journal；
- 恢复后 scope 隔离和 active 唯一性仍成立；
- 删除临时演练目录前验证 resolved path 位于本次创建的 temp 根目录。

- [ ] **Step 6: 检查 diff 和残留**

Run: `git diff --check`
Expected: 无 whitespace error。
Run: `git status --short`
Expected: 只有本方案相关文件。
Run: `rg -n "legacy|shadow|governed.*mode|vectorPath|embeddingPath|journal is.*source|journal.*唯一.*事实" erza docs tests`
Expected: 无旧三模式、向量入口或 journal 事实源残留；迁移语境中的 `legacy journal` 允许存在。

- [ ] **Step 7: 对照设计完成定义逐项审查**

在 PR/交付说明列出设计 §18 的 11 项，每项附测试文件或命令证据。特别确认：

- SQLite 单路径；
- 迁移失败 fail closed；
- candidate 永不召回；
- multi-operation 原子；
- 四进程并发无丢失；
- audit 可重建；
- backup 可恢复；
- database/audit 不进 Git；
- 无向量入口。

- [ ] **Step 8: 处理验证阶段发现的问题**

如果 Step 1–7 发现缺陷，回到拥有该文件和行为的 Task，补充一个先失败后通过的回归测试，并用准确描述该缺陷的 `fix(memory): ...` 信息单独提交。修复后重新执行 Step 1–7；如果没有缺陷，不创建空提交。

- [ ] **Step 9: 请求代码审查，不直接合并**

审查者必须重点检查：事务边界、SQLite busy/error 映射、迁移原子性、workspace 路径、scope SQL、恢复安全备份以及旧 JSONL 是否被意外修改。全部通过后再使用 `finishing-a-development-branch` 决定 merge/PR；不得在验证失败时声称完成。

---

## Implementation Notes for the Executing Agent

1. 先运行基线记忆测试并保存结果；不要把基线失败误判为本次引入。
2. `sqlite3` 是同步 API，当前 Repository/Lifecycle 也是同步的，不要为“看起来现代”引入异步数据库层。
3. 保持 `StructuredMemoryRepository` 类名可以避免大量非必要调用方重写；“Structured” 描述治理模型，不描述存储格式。
4. 数据库内同时保存索引列和完整 canonical JSON。索引列用于查询，Pydantic JSON 用于精确重建和未来 schema 检验。
5. 任何 database row 转为业务对象时都必须经过 Pydantic 验证；坏 JSON 使 Repository degraded，不能跳过。
6. 不要用 SQLite trigger 隐藏业务规则；状态转换、checksum 和 evidence 规则继续由 Python 明确执行，数据库约束作为最后一道防线。
7. 不要在每次启动运行全量 `integrity_check`；启动使用 `quick_check(1)`，backup/migration/restore 使用完整 `integrity_check`。
8. 不要自动删除旧 journal、backup 或 recovery；将来若需要 retention，另做明确设计。
9. Audit 是派生物。audit 失败时正式事实仍成功，但 status 必须显示 lag，用户可以 rebuild。
10. 如果实施中发现必须改变 `MemoryRecord` 或状态机，立即暂停：这超出存储增强范围，需要更新设计规格后再继续。
