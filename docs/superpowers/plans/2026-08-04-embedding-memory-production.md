# Embedding Memory Production Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把现有本地 embedding 原型完善为默认开启、失败不影响聊天、来源可追溯、索引可完整重建，并能由用户确认更新冲突记忆的生产级长期记忆功能。

**Architecture:** `SOUL.md`、`USER.md`、`memory/MEMORY.md` 和各 JSONL 文件始终是唯一可信来源，`memory/memory.db` 只是可丢弃、可原子重建的派生索引。单进程运行时通过模型管理器、来源目录、向量索引、召回服务和显式记忆服务协作；聊天 LLM 只收到有界核心与最多 5 条相关记忆，任何 embedding 故障都退回有界文件记忆。

**Tech Stack:** Python 3.11+、Pydantic 2、Typer、FastEmbed 0.8.x（ONNX Runtime CPU）、Hugging Face Hub、sqlite-vec、SQLite、pytest/pytest-asyncio、React 18、TypeScript、Vitest、Testing Library。

## Global Constraints

- 实施起点必须包含 embedding-only 基线提交 `f0adcf63`；可以包含设计提交 `29201117`，不得 cherry-pick `codex/three-worker-latest` 或任何三 Worker runtime/lease/journal 模块。
- 用户单人维护且在另一台电脑实施：直接在普通 clone 的 `codex/embedding-memory-production` 分支工作，不创建 Git worktree。
- 模型固定为 `BAAI/bge-small-zh-v1.5`，上游 revision 固定为 `7999e1d3359715c523056ef9478215996d62a620`，CPU 推理，输出必须是有限值、L2 归一化的 512 维向量。
- 模型不进入 wheel 或源码仓库；推荐安装必须安装 `vector` extra，首次 setup 下载、逐文件 SHA-256 校验并做真实中文向量自检。
- 下载、校验、加载、推理、SQLite、解析或磁盘写入失败都不得阻止普通聊天；向量功能 fail-closed，聊天 fail-open，并返回明确 fallback reason。
- `agents.defaults.vectorRecall` 缺省为 `true`；用户显式配置 `false` 时不下载、不初始化、不召回，且不修改任何记忆文件。
- 权威来源固定为 `SOUL.md`、`USER.md`、`memory/MEMORY.md`、`memory/history.jsonl`、`memory/episodic.jsonl`、`memory/procedural.jsonl`、`memory/explicit.jsonl`；`SOUL.md` 不进入向量索引。
- `memory/memory.db` 是派生索引：删除后必须能从上述文件完整重建，任何失败或取消的 rebuild 都必须保留旧的有效数据库。
- 每次聊天 LLM 调用只固定注入有界 `SOUL.md`、`USER.md`/`MEMORY.md` 的有界 `Always` 核心，以及最多 5 条且不超过 1200 token 的召回结果。
- 显式记忆只追加 revision，不物理删除；潜在冲突必须先展示新旧内容并由用户选择更新、保留原记忆或分别限定适用范围。
- LLM 只能把关系判断为 `duplicate`、`supplement`、`conflict`、`unrelated` 并建议措辞，没有文件或数据库写权限。
- 所有测试遵循 TDD：先增加单一失败测试并确认失败原因，再写最小实现，最后运行定向测试；每个 Task 单独提交。
- 不碰现有无关未跟踪内容：`.workbuddy/`、`webui/package-lock.json`、`webui/public/favicon_decoded.png`、`webui/public/logo_decoded.png`。

---

## 0. Execution Preflight

- [ ] **Step 1: 确认基线和工作区，不清理用户文件**

```powershell
git status --short --branch
git merge-base --is-ancestor f0adcf63 HEAD
git log -1 --oneline
```

Expected: 第二条命令退出码为 `0`；状态输出允许出现上面列出的四类未跟踪文件，但不允许已有业务代码改动。

- [ ] **Step 2: 创建普通分支，不创建 worktree**

```powershell
git switch -c codex/embedding-memory-production
```

Expected: 输出 `Switched to a new branch 'codex/embedding-memory-production'`。若分支已存在，使用 `git switch codex/embedding-memory-production`，不要重建或删除分支。

- [ ] **Step 3: 建立基线证据**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/providers/test_local_embedding.py tests/agent/test_vector_memory_fingerprint.py tests/agent/test_memory_store.py tests/agent/test_context_builder.py tests/agent/test_upgrade_integration.py -q
```

Expected: 现有定向测试通过；把数量和 skip 原因记录在实施日志中。若环境没有 `.venv`，先执行 `py -3.11 -m venv .venv` 和 `.\.venv\Scripts\python.exe -m pip install -e ".[dev,vector]"`。

## File and Component Map

| File | Responsibility |
|---|---|
| `miniunicorn/embedding/types.py` | 模型、索引、来源、召回状态及 typed failure 的唯一数据契约 |
| `miniunicorn/embedding/model_manager.py` | 固定模型 manifest、下载、哈希校验、CPU 自检和模型状态 |
| `miniunicorn/embedding/control.py` | CLI、WebUI、AgentLoop 共享的单进程服务装配与操作互斥 |
| `miniunicorn/embedding/status_service.py` | 从同一组 manager/catalog/index/recall state 生成 CLI/WebUI 共享四段状态 |
| `miniunicorn/providers/local_embedding.py` | 惰性加载已验证本地模型并返回 typed embedding result |
| `miniunicorn/agent/memory_sources.py` | 扫描权威文件、解析 JSONL、按标题分块、生成稳定 source identity |
| `miniunicorn/agent/explicit_memory.py` | append-only 显式记忆 revision journal 与冲突业务流程 |
| `miniunicorn/agent/vector_index.py` | `memory.db` schema、幂等 upsert、增量 reconcile、原子 rebuild、KNN 查询 |
| `miniunicorn/agent/memory_recall.py` | 查询 embedding、过滤/去重/重排、5 条/1200 token 预算及 fallback |
| `miniunicorn/agent/memory_prompt.py` | `SOUL`、`Always` core、召回和文件 fallback 的有界 prompt 组装 |
| `miniunicorn/agent/loop.py` | 每次 LLM 前召回、显式记忆触发及确认流程接入 |
| `miniunicorn/agent/context.py` | 消费已经预算好的 memory prompt，不再自行全量注入长期文件 |
| `miniunicorn/cli/embedding_commands.py` | `setup/status/verify/rebuild` Typer 子命令 |
| `miniunicorn/webui/embedding_api.py` | WebUI 状态、操作、进度和搜索 API |
| `webui/src/components/settings/sections/MemoryEmbeddingSettings.tsx` | 四张状态卡、详情、操作按钮和来源搜索 |
| `scripts/verify_embedding_memory.py` | 使用真实 FastEmbed/sqlite-vec 的离线发布证明 |

### Task 1: 默认配置、受控路径与固定常量

**Files:**
- Modify: `miniunicorn/config/schema.py`
- Modify: `miniunicorn/config/paths.py`
- Create: `miniunicorn/embedding/__init__.py`
- Test: `tests/config/test_embedding_config.py`
- Modify: `tests/config/test_config_paths.py`

**Interfaces:**
- Consumes: `get_data_dir() -> Path`、`AgentDefaults` 的 Pydantic alias 规则。
- Produces: `MODEL_ID: str`、`MODEL_REVISION: str`、`MODEL_DIMENSION: int`、`get_models_dir() -> Path`、`get_embedding_model_dir() -> Path`，以及缺省为真的 `AgentDefaults.vector_recall`。

- [ ] **Step 1: 写配置和路径失败测试**

```python
from miniunicorn.config.schema import AgentDefaults
from miniunicorn.config.paths import get_embedding_model_dir
from miniunicorn.embedding import MODEL_DIMENSION, MODEL_ID, MODEL_REVISION


def test_vector_recall_defaults_on_but_explicit_false_is_preserved():
    assert AgentDefaults().vector_recall is True
    assert AgentDefaults.model_validate({"vectorRecall": False}).vector_recall is False


def test_embedding_constants_and_path(monkeypatch, tmp_path):
    monkeypatch.setattr("miniunicorn.config.paths.get_data_dir", lambda: tmp_path)
    assert MODEL_ID == "BAAI/bge-small-zh-v1.5"
    assert MODEL_REVISION == "7999e1d3359715c523056ef9478215996d62a620"
    assert MODEL_DIMENSION == 512
    assert get_embedding_model_dir() == tmp_path / "models" / "bge-small-zh-v1.5" / MODEL_REVISION
```

- [ ] **Step 2: 运行测试并确认只因新契约尚未存在而失败**

Run: `.\.venv\Scripts\python.exe -m pytest tests/config/test_embedding_config.py tests/config/test_config_paths.py -q`

Expected: FAIL，原因是 `miniunicorn.embedding` 或 `get_embedding_model_dir` 不存在，以及旧默认值是 `False`；不得出现已有路径测试回归。

- [ ] **Step 3: 添加固定常量、受控路径和新默认值**

```python
# miniunicorn/embedding/__init__.py
MODEL_ID = "BAAI/bge-small-zh-v1.5"
MODEL_REVISION = "7999e1d3359715c523056ef9478215996d62a620"
MODEL_DIMENSION = 512


# miniunicorn/config/paths.py
def get_models_dir() -> Path:
    return get_runtime_subdir("models")


def get_embedding_model_dir() -> Path:
    from miniunicorn.embedding import MODEL_REVISION

    return ensure_dir(get_models_dir() / "bge-small-zh-v1.5" / MODEL_REVISION)


# miniunicorn/config/schema.py, AgentDefaults.vector_recall
vector_recall: bool = Field(
    default=True,
    validation_alias=AliasChoices("vectorRecall"),
    serialization_alias="vectorRecall",
)
```

保留 `embedding_model` alias 以兼容旧配置，但在 Task 2 的 manifest 校验中拒绝任何非固定 model ID。所有路径在 `.resolve(strict=False)` 后必须位于 `get_data_dir().resolve()` 下。

- [ ] **Step 4: 运行定向测试**

Run: `.\.venv\Scripts\python.exe -m pytest tests/config/test_embedding_config.py tests/config/test_config_paths.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add miniunicorn/config/schema.py miniunicorn/config/paths.py miniunicorn/embedding/__init__.py tests/config/test_embedding_config.py tests/config/test_config_paths.py
git commit -m "feat(embedding): enable managed local model by default"
```

### Task 2: 共享状态、结果和 manifest 类型

**Files:**
- Create: `miniunicorn/embedding/types.py`
- Create: `tests/embedding/test_types.py`

**Interfaces:**
- Consumes: Task 1 的 model constants。
- Produces: `EmbeddingFailure`、`EmbeddingResult`、`ModelManifest`、`ModelStatus`、`IndexStatus`、`SourceStatus`、`RecallStatus`、`EmbeddingStatus` 及其 `to_dict()`。

- [ ] **Step 1: 写 typed result 和 JSON contract 失败测试**

```python
from miniunicorn.embedding.types import (
    EmbeddingFailure,
    EmbeddingResult,
    EmbeddingStatus,
    IndexStatus,
    ModelStatus,
    RecallStatus,
    SourceStatus,
)


def test_embedding_result_never_contains_vectors_and_failure_together():
    failure = EmbeddingFailure("dependency_missing", "install vector extra", True)
    with pytest.raises(ValueError):
        EmbeddingResult(vectors=((1.0,),), failure=failure)


def test_shared_status_contract_has_four_sections():
    status = EmbeddingStatus(
        model=ModelStatus(state="not_downloaded"),
        index=IndexStatus(state="missing"),
        sources=SourceStatus(discovered=0, indexed=0, pending=0, stale=0, invalid=0, inactive=0),
        recall=RecallStatus(configured=True, active=False, fallback_reason="model_not_ready"),
    ).to_dict()
    assert set(status) == {"model", "index", "sources", "recall"}
    assert status["recall"]["fallback_reason"] == "model_not_ready"
```

在测试文件顶部显式 `import pytest`。

- [ ] **Step 2: 运行测试并确认导入失败**

Run: `.\.venv\Scripts\python.exe -m pytest tests/embedding/test_types.py -q`

Expected: FAIL with `ModuleNotFoundError: miniunicorn.embedding.types`。

- [ ] **Step 3: 实现冻结数据类型和确定的字面值状态**

```python
# miniunicorn/embedding/types.py
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

FailureCode = Literal[
    "disabled", "dependency_missing", "not_downloaded", "download_failed",
    "hash_mismatch", "model_load_failed", "inference_failed",
    "model_mismatch", "dimension_mismatch", "non_finite", "index_missing", "index_stale",
    "index_corrupt", "source_invalid", "io_error", "cancelled",
]
ModelState = Literal["not_downloaded", "downloading", "verifying", "ready", "corrupt", "failed"]
IndexState = Literal["missing", "building", "ready", "stale", "corrupt", "failed"]


@dataclass(frozen=True)
class EmbeddingFailure:
    code: FailureCode
    message: str
    retryable: bool


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: tuple[tuple[float, ...], ...] = ()
    failure: EmbeddingFailure | None = None

    def __post_init__(self) -> None:
        if self.vectors and self.failure is not None:
            raise ValueError("embedding result cannot contain vectors and failure")

    @property
    def ok(self) -> bool:
        return self.failure is None


@dataclass(frozen=True)
class ModelManifest:
    model_id: str
    revision: str
    dimension: int
    files: dict[str, str]


@dataclass(frozen=True)
class ModelStatus:
    state: ModelState
    model_id: str | None = None
    revision: str | None = None
    dimension: int | None = None
    cache_path: str | None = None
    bytes: int = 0
    last_self_test: str | None = None
    last_error_code: str | None = None
    message: str = ""


@dataclass(frozen=True)
class IndexStatus:
    state: IndexState
    path: str | None = None
    bytes: int = 0
    last_rebuild: str | None = None
    last_error_code: str | None = None
    message: str = ""


@dataclass(frozen=True)
class SourceStatus:
    discovered: int
    indexed: int
    pending: int
    stale: int
    invalid: int
    inactive: int
    errors: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True)
class RecallStatus:
    configured: bool
    active: bool
    fallback_reason: str | None
    last_self_test: str | None = None
    last_latency_ms: float | None = None


@dataclass(frozen=True)
class EmbeddingStatus:
    model: ModelStatus
    index: IndexStatus
    sources: SourceStatus
    recall: RecallStatus
    operation: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)
```

状态 API 不得加入 raw vector 或 secret 字段。

- [ ] **Step 4: 运行测试和 lint**

Run: `.\.venv\Scripts\python.exe -m pytest tests/embedding/test_types.py -q; .\.venv\Scripts\python.exe -m ruff check miniunicorn/embedding/types.py tests/embedding/test_types.py`

Expected: 两条命令均退出 `0`。

- [ ] **Step 5: 提交**

```powershell
git add miniunicorn/embedding/types.py tests/embedding/test_types.py
git commit -m "feat(embedding): define shared status contracts"
```

### Task 3: 固定 revision 的模型 setup、校验和 CPU 自检

**Files:**
- Create: `miniunicorn/embedding/model_manager.py`
- Create: `tests/embedding/test_model_manager.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: `MODEL_ID`、`MODEL_REVISION`、`MODEL_DIMENSION`、`get_embedding_model_dir()`、Task 2 types。
- Produces: `EmbeddingModelManager.status() -> ModelStatus`、`async setup(force: bool = False) -> ModelStatus`、`async verify(run_self_test: bool = True) -> ModelStatus`、`validated_model_path() -> Path | None`。

- [ ] **Step 1: 写下载参数、manifest 哈希和损坏检测失败测试**

```python
@pytest.mark.asyncio
async def test_setup_pins_revision_hashes_files_and_runs_self_test(tmp_path, monkeypatch):
    calls = {}
    def fake_snapshot_download(**kwargs):
        calls.update(kwargs)
        model = Path(kwargs["local_dir"])
        model.mkdir(parents=True)
        (model / "model.onnx").write_bytes(b"onnx")
        (model / "tokenizer.json").write_text("{}", encoding="utf-8")
        return str(model)

    manager = EmbeddingModelManager(tmp_path, snapshot_download=fake_snapshot_download)
    monkeypatch.setattr(manager, "_self_test_sync", lambda path: (512, 1.0))
    status = await manager.setup()
    assert status.state == "ready"
    assert calls["repo_id"] == MODEL_ID
    assert calls["revision"] == MODEL_REVISION
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["files"]["model.onnx"] == hashlib.sha256(b"onnx").hexdigest()


@pytest.mark.asyncio
async def test_verify_rejects_changed_runtime_file(ready_model_dir):
    (ready_model_dir / "model.onnx").write_bytes(b"tampered")
    status = await EmbeddingModelManager(ready_model_dir).verify(run_self_test=False)
    assert status.state == "corrupt"
    assert status.last_error_code == "hash_mismatch"
```

测试 fixture 必须建立与生产相同的 `manifest.json`，并把 downloader 和 self-test 注入，单元测试不能访问网络。

- [ ] **Step 2: 运行测试并确认类不存在**

Run: `.\.venv\Scripts\python.exe -m pytest tests/embedding/test_model_manager.py -q`

Expected: FAIL with import error for `EmbeddingModelManager`。

- [ ] **Step 3: 实现安全下载、manifest 和真实 self-test**

```python
class EmbeddingModelManager:
    def __init__(self, model_dir: Path | None = None, *, snapshot_download=None) -> None:
        self.model_dir = (model_dir or get_embedding_model_dir()).resolve(strict=False)
        self.manifest_path = self.model_dir / "manifest.json"
        self._snapshot_download = snapshot_download
        self._operation_lock = asyncio.Lock()

    async def setup(self, force: bool = False) -> ModelStatus:
        async with self._operation_lock:
            if not force and (await self.verify(run_self_test=True)).state == "ready":
                return self.status()
            self.model_dir.mkdir(parents=True, exist_ok=True)
            try:
                await asyncio.to_thread(self._download_sync)
                self._write_manifest_atomic(self._build_manifest())
                return await self.verify(run_self_test=True)
            except asyncio.CancelledError:
                self._write_state("failed", "cancelled", "模型下载已取消，可重新执行 setup")
                raise
            except Exception as exc:
                self._quarantine_unverified_files()
                return self._write_state("failed", "download_failed", str(exc))

    def _download_sync(self) -> None:
        downloader = self._snapshot_download
        if downloader is None:
            from huggingface_hub import snapshot_download
            downloader = snapshot_download
        downloader(
            repo_id=MODEL_ID,
            revision=MODEL_REVISION,
            local_dir=str(self.model_dir),
            local_dir_use_symlinks=False,
            resume_download=True,
        )

    def _self_test_sync(self, path: Path) -> tuple[int, float]:
        from fastembed import TextEmbedding
        model = TextEmbedding(
            model_name=MODEL_ID,
            specific_model_path=str(path),
            providers=["CPUExecutionProvider"],
        )
        vector = [float(value) for value in next(model.embed(["我喜欢安静的工作环境"]))]
        if len(vector) != MODEL_DIMENSION or not all(math.isfinite(value) for value in vector):
            raise ValueError("self-test returned invalid vector")
        norm = math.sqrt(sum(value * value for value in vector))
        if not 0.999 <= norm <= 1.001:
            raise ValueError(f"self-test vector norm is {norm}")
        return len(vector), norm
```

实现细则必须全部落地：

- `_build_manifest()` 递归遍历模型目录中除 `manifest.json`、`.state.json`、`.partial/`、`.quarantine/` 外的普通文件，键使用 POSIX 相对路径，值为分块读取计算的 SHA-256；至少一个 `.onnx` 和 tokenizer/config 文件，否则失败。
- Hugging Face 的 `.cache/` 下载元数据不进入 manifest；除此之外下载快照中的每个普通文件都必须 hash，确保 FastEmbed 实际可能加载的 ONNX、tokenizer、config 和词表全部被覆盖。
- manifest 用临时文件加 `os.replace()` 写入，固定包含 `model_id`、`revision`、`dimension`、`files`、`verified_at`。
- `verify()` 先校验固定 ID/revision/dimension，再校验文件集合和每个 hash，最后通过 `asyncio.to_thread` 跑 self-test；缺依赖返回 `failed/dependency_missing`，hash 不符返回 `corrupt/hash_mismatch`。
- `status()` 只读 `.state.json`、manifest 和文件大小，不加载模型；所有对外路径是字符串。
- `validated_model_path()` 必须同步重算 manifest 中所有 runtime file hash，且 `.state.json` 为 ready 时才返回 model dir；任何文件在 verify 后被篡改都返回 `None` 并把状态改为 `corrupt/hash_mismatch`。
- 在 `[project.optional-dependencies].vector` 增加直接依赖 `huggingface-hub>=0.27.0,<2.0.0`，不要加入基础 dependencies。

- [ ] **Step 4: 运行模型管理器测试**

Run: `.\.venv\Scripts\python.exe -m pytest tests/embedding/test_model_manager.py -q; .\.venv\Scripts\python.exe -m ruff check miniunicorn/embedding/model_manager.py tests/embedding/test_model_manager.py`

Expected: PASS，且测试日志无网络访问。

- [ ] **Step 5: 提交**

```powershell
git add pyproject.toml miniunicorn/embedding/model_manager.py tests/embedding/test_model_manager.py
git commit -m "feat(embedding): manage pinned model assets"
```

### Task 4: LocalEmbeddingProvider 的 typed failure 与本地只读加载

**Files:**
- Modify: `miniunicorn/providers/local_embedding.py`
- Modify: `tests/providers/test_local_embedding.py`

**Interfaces:**
- Consumes: `EmbeddingModelManager.validated_model_path()`、`EmbeddingResult`、固定 model constants。
- Produces: `LocalEmbeddingProvider(manager).embed(texts: list[str], model: str | None = None) -> EmbeddingResult`；不再用空列表混淆“空输入”和“失败”。

- [ ] **Step 1: 写 typed failure、有限值和本地路径测试**

```python
@pytest.mark.asyncio
async def test_missing_validated_model_is_typed_failure(tmp_path):
    provider = LocalEmbeddingProvider(manager=EmbeddingModelManager(tmp_path))
    result = await provider.embed(["测试"])
    assert result.vectors == ()
    assert result.failure.code == "not_downloaded"


@pytest.mark.asyncio
async def test_non_finite_vector_is_rejected(fake_ready_manager, monkeypatch):
    provider = LocalEmbeddingProvider(manager=fake_ready_manager)
    provider._model = FakeModel([[float("nan")] * 512])
    result = await provider.embed(["测试"])
    assert result.failure.code == "non_finite"


@pytest.mark.asyncio
async def test_loader_uses_verified_path_and_cpu(fake_ready_manager, fake_fastembed_module):
    provider = LocalEmbeddingProvider(manager=fake_ready_manager)
    result = await provider.embed(["中文记忆"])
    assert result.ok and len(result.vectors[0]) == 512
    assert fake_fastembed_module.kwargs["specific_model_path"] == str(fake_ready_manager.model_dir)
    assert fake_fastembed_module.kwargs["providers"] == ["CPUExecutionProvider"]
```

- [ ] **Step 2: 运行测试并确认旧接口断言失败**

Run: `.\.venv\Scripts\python.exe -m pytest tests/providers/test_local_embedding.py -q`

Expected: FAIL，因为旧实现返回 `list[list[float]]` 且允许 FastEmbed自行找/下载模型。

- [ ] **Step 3: 改为验证后加载并返回 typed result**

```python
async def embed(self, texts: list[str], model: str | None = None) -> EmbeddingResult:
    if not texts:
        return EmbeddingResult()
    if model not in (None, MODEL_ID):
        return EmbeddingResult(failure=EmbeddingFailure(
            "model_mismatch", f"unsupported embedding model: {model}", False
        ))
    path = self.manager.validated_model_path()
    if path is None:
        status = self.manager.status()
        code = "dependency_missing" if status.last_error_code == "dependency_missing" else "not_downloaded"
        return EmbeddingResult(failure=EmbeddingFailure(code, status.message or "模型尚未就绪", True))
    try:
        await self._ensure_model_loaded(path)
        raw = await asyncio.to_thread(self._raw_embed, texts)
    except Exception as exc:
        return EmbeddingResult(failure=EmbeddingFailure("inference_failed", str(exc), True))
    vectors: list[tuple[float, ...]] = []
    for raw_vector in raw:
        if len(raw_vector) != MODEL_DIMENSION:
            return EmbeddingResult(failure=EmbeddingFailure("dimension_mismatch", "expected 512 dimensions", False))
        if not all(math.isfinite(value) for value in raw_vector):
            return EmbeddingResult(failure=EmbeddingFailure("non_finite", "embedding contains non-finite values", True))
        normalized = _l2_normalize(raw_vector)
        vectors.append(tuple(normalized))
    return EmbeddingResult(vectors=tuple(vectors))
```

`_ensure_model_loaded(path)` 必须继续使用 `asyncio.Lock` 和 `asyncio.to_thread`，构造 FastEmbed 时传 `specific_model_path=str(path)`、`providers=["CPUExecutionProvider"]`，不得允许运行时联网下载。把旧测试断言同步为 `result.vectors`/`result.failure`，保留并覆盖并发只加载一次、错误 model、维数错误和归一化测试。

- [ ] **Step 4: 运行 provider 测试**

Run: `.\.venv\Scripts\python.exe -m pytest tests/providers/test_local_embedding.py tests/embedding/test_model_manager.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add miniunicorn/providers/local_embedding.py tests/providers/test_local_embedding.py
git commit -m "refactor(embedding): return typed local inference results"
```

### Task 5: 权威来源扫描、稳定身份与有界分块

**Files:**
- Create: `miniunicorn/agent/memory_sources.py`
- Create: `tests/agent/test_memory_sources.py`

**Interfaces:**
- Consumes: workspace `Path` 和固定来源文件格式。
- Produces: `MemorySourceRecord`、`SourceParseError`、`SourceScan`、`MemorySourceCatalog.scan() -> SourceScan`。

- [ ] **Step 1: 写 Markdown、JSONL、legacy identity 和坏行隔离测试**

```python
def test_catalog_emits_stable_markdown_and_jsonl_ids(tmp_path):
    (tmp_path / "USER.md").write_text("# Always\n叫我小王\n# Preferences\n喜欢深色主题", encoding="utf-8")
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "history.jsonl").write_text(
        '{"cursor":7,"timestamp":"2026-08-04 10:00","content":"完成了项目初始化"}\n',
        encoding="utf-8",
    )
    scan = MemorySourceCatalog(tmp_path).scan()
    ids = {record.source_id for record in scan.records}
    assert "user:preferences:1" in ids
    assert "history:7" in ids
    assert not any(record.source_type == "soul" for record in scan.records)


def test_invalid_jsonl_line_is_reported_without_blocking_valid_lines(tmp_path):
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "procedural.jsonl").write_text(
        '{bad json}\n{"cursor":2,"content":"提交前运行测试"}\n', encoding="utf-8"
    )
    scan = MemorySourceCatalog(tmp_path).scan()
    assert [record.source_id for record in scan.records] == ["procedural:2"]
    assert scan.errors[0].line == 1
```

- [ ] **Step 2: 运行测试并确认模块不存在**

Run: `.\.venv\Scripts\python.exe -m pytest tests/agent/test_memory_sources.py -q`

Expected: FAIL with import error for `memory_sources`。

- [ ] **Step 3: 实现规范化 record 和扫描规则**

```python
@dataclass(frozen=True)
class MemorySourceRecord:
    source_id: str
    source_type: str
    source_file: str
    source_revision: str
    content_hash: str
    text: str
    importance: float
    active: bool = True
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceParseError:
    source_file: str
    line: int | None
    code: str
    message: str


@dataclass(frozen=True)
class SourceScan:
    records: tuple[MemorySourceRecord, ...]
    errors: tuple[SourceParseError, ...]


class MemorySourceCatalog:
    def __init__(self, workspace: Path, *, max_chunk_chars: int = 2400) -> None:
        self.workspace = workspace.resolve(strict=False)
        self.max_chunk_chars = max_chunk_chars

    def scan(self) -> SourceScan:
        records: list[MemorySourceRecord] = []
        errors: list[SourceParseError] = []
        records.extend(self._scan_markdown("USER.md", "user", importance=0.9))
        records.extend(self._scan_markdown("memory/MEMORY.md", "memory", importance=0.8))
        for path, source_type, importance in (
            ("memory/history.jsonl", "history", 0.5),
            ("memory/episodic.jsonl", "episodic", 0.6),
            ("memory/procedural.jsonl", "procedural", 0.8),
        ):
            valid, invalid = self._scan_jsonl(path, source_type, importance)
            records.extend(valid)
            errors.extend(invalid)
        return SourceScan(tuple(records), tuple(errors))
```

规则必须逐条实现并测试：

- 所有 `source_file` 使用 workspace-relative POSIX 路径；解析前 `.resolve()` 并拒绝越出 workspace 的 symlink。
- Markdown 按 ATX heading 切分；heading path 小写、连续非字母数字归一成 `-`，同一路径按块序号从 1 计；每块最多 2400 字符，优先空行、其次句末、最后硬切。
- `# Always` 也建立索引，但 Task 10 会从召回结果去重；空白或模板内容不建记录。
- history ID=`history:<cursor>`，procedural ID=`procedural:<cursor>`；cursor 缺失/非正整数则该行 invalid。
- episodic 有非空 `event_id` 时 ID=`episodic:<event_id>`；否则 ID=`episodic:legacy:<line>:<sha256前12位>`。
- text 字段依次取 `content`、`summary`、`text` 中第一个非空字符串；revision 取 `revision`/`updated_at`/`timestamp`，均无时使用 `line:<line>:<content_hash前12位>`。
- `content_hash=sha256(normalized_text.encode("utf-8")).hexdigest()`；换行统一为 `\n`，行尾空白去除。

- [ ] **Step 4: 运行来源测试**

Run: `.\.venv\Scripts\python.exe -m pytest tests/agent/test_memory_sources.py -q; .\.venv\Scripts\python.exe -m ruff check miniunicorn/agent/memory_sources.py tests/agent/test_memory_sources.py`

Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add miniunicorn/agent/memory_sources.py tests/agent/test_memory_sources.py
git commit -m "feat(memory): catalog authoritative memory sources"
```

### Task 6: append-only 显式记忆 revision journal

**Files:**
- Create: `miniunicorn/agent/explicit_memory.py`
- Modify: `miniunicorn/agent/memory.py`
- Create: `tests/agent/test_explicit_memory.py`
- Modify: `tests/agent/test_memory_store.py`

**Interfaces:**
- Consumes: workspace boundary checks和 `memory/explicit.jsonl`。
- Produces: `ExplicitMemoryRevision`、`ExplicitMemoryJournal.append_new()`、`append_update()`、`restore()`、`effective()`、`history(memory_id)`；Task 12 在此之上实现 semantic flow。

- [ ] **Step 1: 写 revision、重开恢复和坏尾行测试**

```python
def test_update_keeps_history_but_only_latest_is_effective(tmp_path):
    journal = ExplicitMemoryJournal(tmp_path)
    first = journal.append_new("我喜欢浅色主题", "用户喜欢浅色主题", None)
    second = journal.append_update(first.memory_id, "我喜欢深色主题", "用户喜欢深色主题", None)
    assert second.revision == 2
    assert second.supersedes_revision == 1
    assert [row.revision for row in journal.history(first.memory_id)] == [1, 2]
    assert ExplicitMemoryJournal(tmp_path).effective()[0].normalized_fact == "用户喜欢深色主题"


def test_invalid_trailing_line_does_not_hide_valid_revisions(tmp_path):
    journal = ExplicitMemoryJournal(tmp_path)
    saved = journal.append_new("叫我小王", "称呼用户为小王", None)
    with journal.path.open("a", encoding="utf-8") as handle:
        handle.write("{broken\n")
    assert journal.effective()[0].memory_id == saved.memory_id
    assert journal.errors()[0].line == 2


def test_restore_appends_a_new_revision_instead_of_rewriting_history(tmp_path):
    journal = ExplicitMemoryJournal(tmp_path)
    first = journal.append_new("浅色", "用户喜欢浅色主题", None)
    journal.append_update(first.memory_id, "深色", "用户喜欢深色主题", None)
    restored = journal.restore(first.memory_id, revision=1)
    assert restored.revision == 3
    assert restored.normalized_fact == "用户喜欢浅色主题"
    assert [row.revision for row in journal.history(first.memory_id)] == [1, 2, 3]
```

- [ ] **Step 2: 运行测试并确认 API 不存在**

Run: `.\.venv\Scripts\python.exe -m pytest tests/agent/test_explicit_memory.py tests/agent/test_memory_store.py -q`

Expected: FAIL with import/API errors；现有 MemoryStore 测试仍应通过到新断言之前。

- [ ] **Step 3: 实现 journal 和 MemoryStore 路径白名单**

```python
@dataclass(frozen=True)
class ExplicitMemoryRevision:
    memory_id: str
    revision: int
    raw_text: str
    normalized_fact: str
    scope: str | None
    created_at: str
    supersedes_revision: int | None = None


class ExplicitMemoryJournal:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve(strict=False)
        self.path = self.workspace / "memory" / "explicit.jsonl"

    def append_new(self, raw_text: str, normalized_fact: str, scope: str | None) -> ExplicitMemoryRevision:
        row = ExplicitMemoryRevision(
            memory_id=str(uuid.uuid4()), revision=1,
            raw_text=_required(raw_text), normalized_fact=_required(normalized_fact),
            scope=_optional(scope), created_at=_utc_now(), supersedes_revision=None,
        )
        self._append(row)
        return row

    def append_update(self, memory_id: str, raw_text: str, normalized_fact: str, scope: str | None) -> ExplicitMemoryRevision:
        history = self.history(memory_id)
        if not history:
            raise KeyError(memory_id)
        current = history[-1]
        row = ExplicitMemoryRevision(
            memory_id=memory_id, revision=current.revision + 1,
            raw_text=_required(raw_text), normalized_fact=_required(normalized_fact),
            scope=_optional(scope), created_at=_utc_now(), supersedes_revision=current.revision,
        )
        self._append(row)
        return row

    def restore(self, memory_id: str, revision: int) -> ExplicitMemoryRevision:
        archived = next((row for row in self.history(memory_id) if row.revision == revision), None)
        if archived is None:
            raise KeyError(f"{memory_id}@{revision}")
        return self.append_update(
            memory_id, archived.raw_text, archived.normalized_fact, archived.scope
        )
```

`_append()` 必须先验证 path 在 workspace 内，再 `mkdir(parents=True, exist_ok=True)`，以单行 `json.dumps(asdict(row), ensure_ascii=False, separators=(",", ":")) + "\n"` 追加、flush、`os.fsync()`。读取时逐行校验 UUID、revision 连续递增、supersedes 指向前一版；坏行进入 `SourceParseError`，不影响其他 memory ID。`effective()` 每个 ID 只返回最高有效 revision。

在 `MemoryStore.__init__` 增加 `self.explicit_file`，并在 `_WRITER_WHITELIST` 精确加入：

```python
"memory/explicit.jsonl": {"explicit_memory", "memory_store"},
```

- [ ] **Step 4: 运行 journal 和 MemoryStore 测试**

Run: `.\.venv\Scripts\python.exe -m pytest tests/agent/test_explicit_memory.py tests/agent/test_memory_store.py -q`

Expected: PASS，包括白名单覆盖 `memory/explicit.jsonl`。

- [ ] **Step 5: 提交**

```powershell
git add miniunicorn/agent/explicit_memory.py miniunicorn/agent/memory.py tests/agent/test_explicit_memory.py tests/agent/test_memory_store.py
git commit -m "feat(memory): add append-only explicit memory revisions"
```

### Task 7: 新 `memory.db` schema 与幂等 source upsert

**Files:**
- Create: `miniunicorn/agent/vector_index.py`
- Create: `tests/agent/test_vector_index.py`
- Modify: `miniunicorn/agent/vector_memory.py`
- Modify: `tests/agent/test_vector_memory_fingerprint.py`
- Modify: `tests/agent/test_upgrade_integration.py`

**Interfaces:**
- Consumes: `MemorySourceRecord`、固定 model ID/revision/dimension、sqlite-vec。
- Produces: `VectorIndexManager`、`IndexFingerprint`、`IndexCandidate`、`upsert(record, embedding) -> Literal["inserted", "updated", "unchanged"]`、`mark_inactive_except(source_ids) -> int`。

- [ ] **Step 1: 写 schema、唯一性、更新和 fingerprint 失败测试**

```python
def test_upsert_is_idempotent_and_changed_content_reuses_source_row(vec_db, source_record):
    manager = VectorIndexManager(vec_db)
    assert manager.upsert(source_record, [1.0] + [0.0] * 511) == "inserted"
    assert manager.upsert(source_record, [1.0] + [0.0] * 511) == "unchanged"
    changed = replace(source_record, source_revision="2", content_hash="b" * 64, text="新内容")
    assert manager.upsert(changed, [0.0, 1.0] + [0.0] * 510) == "updated"
    assert manager.count_sources() == 1
    assert manager.get_source(source_record.source_id).text == "新内容"


def test_mismatched_model_revision_cannot_search(vec_db, source_record):
    manager = VectorIndexManager(vec_db)
    manager.upsert(source_record, [1.0] + [0.0] * 511)
    manager.close()
    wrong = VectorIndexManager(vec_db, model_revision="wrong")
    assert wrong.status().state == "stale"
    assert wrong.search([1.0] + [0.0] * 511, limit=5) == []
```

测试用 `pytest.importorskip("sqlite_vec")` 只跳过真实 sqlite-vec 用例；纯 schema/fingerprint 检查不得全部 skip。

- [ ] **Step 2: 运行测试并确认新模块不存在**

Run: `.\.venv\Scripts\python.exe -m pytest tests/agent/test_vector_index.py tests/agent/test_vector_memory_fingerprint.py -q`

Expected: FAIL with import error for `vector_index`。

- [ ] **Step 3: 实现 version 2 schema 和单事务 upsert**

```sql
CREATE TABLE index_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE sources (
    id INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL UNIQUE,
    source_type TEXT NOT NULL,
    source_file TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    text TEXT NOT NULL,
    importance REAL NOT NULL CHECK (importance >= 0 AND importance <= 1),
    active INTEGER NOT NULL CHECK (active IN (0, 1)),
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE vectors USING vec0(
    embedding float[512] distance=cosine
);
CREATE INDEX sources_active_type ON sources(active, source_type);
```

```python
@dataclass(frozen=True)
class IndexFingerprint:
    schema_version: str = "2"
    model_id: str = MODEL_ID
    model_revision: str = MODEL_REVISION
    vector_dimension: int = MODEL_DIMENSION


@dataclass(frozen=True)
class IndexCandidate:
    source_id: str
    source_type: str
    source_file: str
    source_revision: str
    content_hash: str
    text: str
    importance: float
    metadata: dict[str, object]
    similarity: float
    updated_at: str


def upsert(self, record: MemorySourceRecord, embedding: Sequence[float]) -> UpsertAction:
    self._require_writable_ready()
    vector = self._validate_vector(embedding)
    with self._lock, self._conn:
        existing = self._conn.execute(
            "SELECT id, source_revision, content_hash, active FROM sources WHERE source_id=?",
            (record.source_id,),
        ).fetchone()
        if existing and existing["source_revision"] == record.source_revision \
                and existing["content_hash"] == record.content_hash and existing["active"]:
            return "unchanged"
        row_id = self._upsert_source_row(record, existing)
        self._conn.execute("DELETE FROM vectors WHERE rowid=?", (row_id,))
        self._conn.execute(
            "INSERT INTO vectors(rowid, embedding) VALUES (?, ?)",
            (row_id, _serialize_f32(vector)),
        )
        return "updated" if existing else "inserted"
```

构造函数只在新数据库写 fingerprint；已有库缺少或不匹配 `schema_version/model_id/model_revision/vector_dimension` 时状态设为 `stale`，只读诊断，不建表、不迁移、不搜索。所有 search 只连接 `active=1` 的 source row。`mark_inactive_except()` 使用临时表或参数分批，空集合时将全部行设 inactive，但不删除 source 或 vector 历史行。

把 `vector_memory.py` 改成兼容导出层：旧的 `create_vector_store()` 暂时转调 `VectorIndexManager`，`VectorMemoryStore = VectorIndexManager`，`NoOpVectorStore` 保留到 Task 11 完成调用迁移；不要维持两套数据库 schema 实现。同步把 `test_upgrade_integration.py` 中依赖旧 `_try_load_sqlite_vec` monkeypatch、任意维数和 `index(text, vector)` 的开发期测试改成 Task 7 的 source-record API 与固定 512 维契约。

- [ ] **Step 4: 运行向量索引测试**

Run: `.\.venv\Scripts\python.exe -m pytest tests/agent/test_vector_index.py tests/agent/test_vector_memory_fingerprint.py tests/agent/test_upgrade_integration.py -q`

Expected: PASS；如果 sqlite-vec 已安装，schema/upsert/search 用例实际运行而非 skip。

- [ ] **Step 5: 提交**

```powershell
git add miniunicorn/agent/vector_index.py miniunicorn/agent/vector_memory.py tests/agent/test_vector_index.py tests/agent/test_vector_memory_fingerprint.py tests/agent/test_upgrade_integration.py
git commit -m "feat(memory): add provenance-aware vector index schema"
```

### Task 8: 增量 reconcile 与 explicit effective view

**Files:**
- Modify: `miniunicorn/agent/memory_sources.py`
- Modify: `miniunicorn/agent/vector_index.py`
- Create: `tests/agent/test_vector_reconcile.py`
- Modify: `tests/agent/test_explicit_memory.py`

**Interfaces:**
- Consumes: `MemorySourceCatalog.scan()`、`ExplicitMemoryJournal.effective()`、`LocalEmbeddingProvider.embed()`、`VectorIndexManager.upsert()`。
- Produces: `async VectorIndexManager.reconcile(scan, embedder) -> ReconcileReport`，其中 report 精确计数 `discovered/inserted/updated/unchanged/inactive/invalid/failed`。

- [ ] **Step 1: 写 unchanged 不重嵌、changed 更新和旧 explicit 不生效测试**

```python
@pytest.mark.asyncio
async def test_reconcile_embeds_only_new_or_changed_records(manager, source_record):
    embedder = RecordingEmbedder()
    first = await manager.reconcile(SourceScan((source_record,), ()), embedder)
    second = await manager.reconcile(SourceScan((source_record,), ()), embedder)
    changed = replace(source_record, source_revision="2", content_hash="c" * 64, text="changed")
    third = await manager.reconcile(SourceScan((changed,), ()), embedder)
    assert (first.inserted, second.unchanged, third.updated) == (1, 1, 1)
    assert embedder.texts == [source_record.text, "changed"]
    assert manager.count_sources() == 1


def test_catalog_indexes_only_effective_explicit_revision(tmp_path):
    journal = ExplicitMemoryJournal(tmp_path)
    first = journal.append_new("浅色", "用户喜欢浅色主题", None)
    journal.append_update(first.memory_id, "深色", "用户喜欢深色主题", None)
    records = [r for r in MemorySourceCatalog(tmp_path).scan().records if r.source_type == "explicit"]
    assert len(records) == 1
    assert records[0].source_id == f"explicit:{first.memory_id}"
    assert records[0].source_revision == "2"
    assert records[0].text == "用户喜欢深色主题"
```

- [ ] **Step 2: 运行测试并确认 reconcile 缺失**

Run: `.\.venv\Scripts\python.exe -m pytest tests/agent/test_vector_reconcile.py tests/agent/test_explicit_memory.py -q`

Expected: FAIL because `reconcile` and explicit catalog integration are absent。

- [ ] **Step 3: 实现两阶段 diff/embed/upsert**

```python
@dataclass(frozen=True)
class ReconcileReport:
    discovered: int
    inserted: int
    updated: int
    unchanged: int
    inactive: int
    invalid: int
    failed: int
    failures: tuple[dict[str, str], ...] = ()


async def reconcile(self, scan: SourceScan, embedder: LocalEmbeddingProvider) -> ReconcileReport:
    current = self.source_fingerprints()
    changed = [
        row for row in scan.records
        if row.active and current.get(row.source_id) != (row.source_revision, row.content_hash, True)
    ]
    result = await embedder.embed([row.text for row in changed]) if changed else EmbeddingResult()
    if result.failure is not None:
        return ReconcileReport(
            discovered=len(scan.records), inserted=0, updated=0,
            unchanged=len(scan.records) - len(changed), inactive=0,
            invalid=len(scan.errors), failed=len(changed),
            failures=({"code": result.failure.code, "message": result.failure.message},),
        )
    inserted = updated = 0
    for record, vector in zip(changed, result.vectors, strict=True):
        action = self.upsert(record, vector)
        inserted += action == "inserted"
        updated += action == "updated"
    active_ids = {record.source_id for record in scan.records if record.active}
    inactive = self.mark_inactive_except(active_ids)
    return ReconcileReport(
        discovered=len(scan.records), inserted=inserted, updated=updated,
        unchanged=len(scan.records) - len(changed), inactive=inactive,
        invalid=len(scan.errors), failed=0,
    )
```

批量 embedding 最多 32 条一批；某一批失败时不写该批、继续其他批并记录 failure。只有 embedding 数量与输入严格相等才写。Catalog 扫描 explicit journal 时将每个 effective revision 映射为 `source_id=explicit:<memory_id>`、`source_revision=str(revision)`、`source_file=memory/explicit.jsonl`、importance `1.0`，metadata 含 `memory_id/revision/scope/created_at`。

- [ ] **Step 4: 运行 reconcile 测试**

Run: `.\.venv\Scripts\python.exe -m pytest tests/agent/test_vector_reconcile.py tests/agent/test_memory_sources.py tests/agent/test_explicit_memory.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add miniunicorn/agent/memory_sources.py miniunicorn/agent/vector_index.py tests/agent/test_vector_reconcile.py tests/agent/test_explicit_memory.py
git commit -m "feat(memory): reconcile authoritative sources incrementally"
```

### Task 9: 完整、可取消、原子 rebuild

**Files:**
- Modify: `miniunicorn/agent/vector_index.py`
- Create: `tests/agent/test_vector_rebuild.py`

**Interfaces:**
- Consumes: Task 8 `reconcile()` 和 catalog scan。
- Produces: `async VectorIndexManager.rebuild(catalog, embedder, cancel_event=None, progress=None) -> RebuildReport`、`validate() -> ValidationReport`。

- [ ] **Step 1: 写删除重建、失败保旧库、cancel 保旧库和 backup 测试**

```python
@pytest.mark.asyncio
async def test_rebuild_restores_all_sources_after_db_deleted(tmp_path, catalog, embedder):
    db = tmp_path / "memory" / "memory.db"
    manager = VectorIndexManager(db)
    await manager.rebuild(catalog, embedder)
    manager.close()
    db.unlink()
    report = await VectorIndexManager(db).rebuild(catalog, embedder)
    assert report.validated is True
    assert VectorIndexManager(db).count_active_sources() == len(catalog.scan().records)


@pytest.mark.asyncio
async def test_cancelled_rebuild_leaves_previous_database_bytes(tmp_path, catalog, blocking_embedder):
    db = tmp_path / "memory" / "memory.db"
    manager = seeded_index(db)
    manager.close()
    before = db.read_bytes()
    cancel = asyncio.Event()
    cancel.set()
    report = await VectorIndexManager(db).rebuild(catalog, blocking_embedder, cancel_event=cancel)
    assert report.state == "cancelled"
    assert db.read_bytes() == before
    assert not db.with_name("memory.db.rebuilding").exists()
```

- [ ] **Step 2: 运行测试并确认 rebuild 缺失**

Run: `.\.venv\Scripts\python.exe -m pytest tests/agent/test_vector_rebuild.py -q`

Expected: FAIL because rebuild/validation contracts do not exist。

- [ ] **Step 3: 实现临时库验证后替换**

```python
async def rebuild(self, catalog, embedder, *, cancel_event=None, progress=None) -> RebuildReport:
    target = self.db_path
    rebuilding = target.with_name(target.name + ".rebuilding")
    rebuilding.unlink(missing_ok=True)
    temporary = VectorIndexManager(rebuilding, create=True)
    try:
        scan = catalog.scan()
        if cancel_event is not None and cancel_event.is_set():
            raise asyncio.CancelledError
        report = await temporary.reconcile(scan, embedder, progress=progress, cancel_event=cancel_event)
        validation = await temporary.validate(embedder)
        if report.failed or not validation.ok:
            return RebuildReport.failed(report, validation)
        temporary.close()
        self.close()
        backup = None
        if target.exists():
            backup = target.with_name(f"{target.name}.backup.{_utc_file_stamp()}")
            shutil.copy2(target, backup)
        os.replace(rebuilding, target)
        return RebuildReport.ready(report, validation, backup)
    except asyncio.CancelledError:
        return RebuildReport.cancelled()
    finally:
        temporary.close()
        rebuilding.unlink(missing_ok=True)
```

`validate()` 必须同时检查 fingerprint 完整匹配、active source 数等于成功 reconcile 的 active 数、每个 active source 恰有一个 vector row、所有 BLOB 长度为 `512*4`、反序列化后全为 finite，以及用第一条 source 的 text 做真实 embedding 后能查询回其 source ID。验证期间不能关闭/覆盖旧库。使用 `FileLock(str(db_path)+".lock")` 防止同一进程外的 CLI 与 gateway 同时 rebuild；lock 超时返回 `failed/io_error`，不等待无限时间。

- [ ] **Step 4: 运行 rebuild 测试**

Run: `.\.venv\Scripts\python.exe -m pytest tests/agent/test_vector_rebuild.py tests/agent/test_vector_reconcile.py tests/agent/test_vector_index.py -q`

Expected: PASS；测试结束后不残留 `.rebuilding` 文件。

- [ ] **Step 5: 提交**

```powershell
git add miniunicorn/agent/vector_index.py tests/agent/test_vector_rebuild.py
git commit -m "feat(memory): rebuild vector index atomically"
```

### Task 10: 召回过滤、排序、去重与 token 预算

**Files:**
- Create: `miniunicorn/agent/memory_recall.py`
- Create: `tests/agent/test_memory_recall.py`

**Interfaces:**
- Consumes: `LocalEmbeddingProvider.embed()`、`VectorIndexManager.search()`、Task 2 failure/status types。
- Produces: `RecallRecord`、`RecallOutcome`、`MemoryRecallService.recall(query, core_texts=()) -> RecallOutcome`。

- [ ] **Step 1: 写 top-5、阈值、内容去重、core 去重和 fallback 测试**

```python
@pytest.mark.asyncio
async def test_recall_returns_five_unique_budgeted_records():
    index = FakeIndex(candidates=[
        candidate("a", "重复内容", 0.91, 0.5),
        candidate("b", "重复内容", 0.90, 1.0),
        *[candidate(str(i), "记忆" + str(i) * 400, 0.89 - i / 100, 0.5) for i in range(8)],
    ])
    outcome = await MemoryRecallService(index, FakeEmbedder(), token_budget=1200).recall(
        "查询", core_texts=("固定核心",)
    )
    assert outcome.fallback_reason is None
    assert len(outcome.records) <= 5
    assert sum(row.token_count for row in outcome.records) <= 1200
    assert len({row.content_hash for row in outcome.records}) == len(outcome.records)


@pytest.mark.asyncio
async def test_embedding_failure_returns_typed_fallback():
    embedder = FailingEmbedder("not_downloaded")
    outcome = await MemoryRecallService(FakeIndex(), embedder).recall("你好")
    assert outcome.records == ()
    assert outcome.fallback_reason == "model_not_ready"
```

- [ ] **Step 2: 运行测试并确认模块不存在**

Run: `.\.venv\Scripts\python.exe -m pytest tests/agent/test_memory_recall.py -q`

Expected: FAIL with import error for `memory_recall`。

- [ ] **Step 3: 实现确定性的召回策略**

```python
@dataclass(frozen=True)
class RecallRecord:
    source_id: str
    source_type: str
    source_file: str
    source_revision: str
    text: str
    content_hash: str
    similarity: float
    score: float
    token_count: int
    synchronized: bool


@dataclass(frozen=True)
class RecallOutcome:
    records: tuple[RecallRecord, ...]
    fallback_reason: str | None
    latency_ms: float


async def recall(self, query: str, *, core_texts: Sequence[str] = ()) -> RecallOutcome:
    started = time.perf_counter()
    embedded = await self.embedder.embed([query[:2000]])
    if embedded.failure is not None:
        return self._fallback(_map_failure(embedded.failure.code), started)
    if not self.index.is_search_ready():
        return self._fallback(self.index.fallback_reason(), started)
    candidates = self.index.search(list(embedded.vectors[0]), limit=30)
    core_hashes = {_content_hash(text) for text in core_texts if text.strip()}
    selected: list[RecallRecord] = []
    seen = set(core_hashes)
    used = 0
    ranked = sorted(candidates, key=self._rank_key)
    for candidate in ranked:
        if candidate.similarity < self.similarity_floor or candidate.content_hash in seen:
            continue
        tokens = self.token_counter(candidate.text)
        if tokens > self.token_budget - used:
            continue
        selected.append(self._to_record(candidate, tokens))
        seen.add(candidate.content_hash)
        used += tokens
        if len(selected) == self.max_results:
            break
    return RecallOutcome(tuple(selected), None, _elapsed_ms(started))
```

固定默认值：`similarity_floor=0.45`、over-fetch `30`、`max_results=5`、`token_budget=1200`。score 精确为 `0.85*similarity + 0.10*clamp(importance,0,1) + 0.05*recency`；recency 为更新时间距现在 0 天得 1、365 天及以上得 0，线性衰减。排序 key 为 `(-score, -similarity, source_id)`，保证可复现。token counter 优先 `tiktoken.get_encoding("cl100k_base")`，异常时用 `max(1, ceil(len(text)/4))`。failure 映射至少覆盖 `model_not_ready/dependency_missing/inference_failed/index_missing/index_stale/index_corrupt/disabled`。

- [ ] **Step 4: 运行召回测试**

Run: `.\.venv\Scripts\python.exe -m pytest tests/agent/test_memory_recall.py tests/agent/test_vector_index.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add miniunicorn/agent/memory_recall.py tests/agent/test_memory_recall.py
git commit -m "feat(memory): add bounded provenance-aware recall"
```

### Task 11: Prompt memory policy 与单进程 runtime 接入

**Files:**
- Create: `miniunicorn/agent/memory_prompt.py`
- Create: `miniunicorn/embedding/control.py`
- Create: `miniunicorn/embedding/status_service.py`
- Modify: `miniunicorn/agent/context.py`
- Modify: `miniunicorn/agent/loop.py`
- Modify: `miniunicorn/agent/loop_builder.py`
- Modify: `miniunicorn/agent/runner.py`
- Modify: `miniunicorn/agent/memory.py`
- Create: `tests/agent/test_memory_prompt.py`
- Create: `tests/embedding/test_status_service.py`
- Modify: `tests/agent/test_context_builder.py`
- Modify: `tests/agent/test_upgrade_integration.py`
- Modify: `tests/agent/test_runner_core.py`

**Interfaces:**
- Consumes: model manager、catalog、index manager、recall service、`AgentDefaults.vector_recall`。
- Produces: `MemoryPromptPayload`、`MemoryPromptPolicy.build(recall) -> MemoryPromptPayload`、`EmbeddingStatusService.snapshot()`、`EmbeddingControl.for_workspace()`、`async recall_for_turn(query) -> RecallOutcome`。

- [ ] **Step 1: 写 always core、SOUL 有界、fallback 有界和每次 turn 调用测试**

```python
def test_prompt_uses_always_core_not_full_user_and_memory(tmp_path):
    write_workspace(tmp_path, user="# Always\n叫我小王\n# Archive\n" + "旧资料" * 5000,
                    memory="# Always\n项目用 Python\n# Details\n" + "细节" * 5000)
    payload = MemoryPromptPolicy(tmp_path).build(RecallOutcome((), None, 1.0))
    assert "叫我小王" in payload.text and "项目用 Python" in payload.text
    assert "旧资料" * 100 not in payload.text
    assert payload.token_count <= 5200


def test_fallback_is_bounded_and_explains_reason(tmp_path):
    write_workspace(tmp_path, user="U" * 50000, memory="M" * 50000)
    payload = MemoryPromptPolicy(tmp_path).build(RecallOutcome((), "model_not_ready", 0.0))
    assert payload.mode == "file_fallback"
    assert payload.token_count <= 5200
    assert "model_not_ready" in payload.diagnostic


@pytest.mark.asyncio
async def test_runner_refreshes_memory_before_every_main_provider_call(runner, run_spec):
    refresh = AsyncMock(side_effect=lambda messages: messages)
    run_spec.before_provider_call = refresh
    await runner._request_model(run_spec, [{"role": "user", "content": "问题"}], fake_hook(), fake_context())
    await runner._request_model(run_spec, [{"role": "user", "content": "问题"}], fake_hook(), fake_context())
    assert refresh.await_count == 2


def test_status_service_derives_source_counts_without_creating_index(tmp_path):
    write_workspace(tmp_path, user="# Always\n叫我小王", memory="# Facts\n项目用 Python")
    status = make_status_service(tmp_path, index_state="missing").snapshot(configured=True)
    assert status.sources.discovered == 2
    assert status.sources.indexed == 0
    assert status.sources.pending == 2
    assert status.index.state == "missing"
    assert status.recall.fallback_reason == "index_missing"
    assert not (tmp_path / "memory" / "memory.db").exists()
```

- [ ] **Step 2: 运行测试并确认旧全量注入行为失败**

Run: `.\.venv\Scripts\python.exe -m pytest tests/agent/test_memory_prompt.py tests/agent/test_context_builder.py tests/agent/test_upgrade_integration.py -q`

Expected: FAIL，因为 `USER.md` 仍在 bootstrap 全量注入、`MEMORY.md` fallback 无独立预算，且 AgentLoop 传 raw query embedding。

- [ ] **Step 3: 实现 prompt policy 和共享 control**

```python
SOUL_TOKEN_BUDGET = 4000
CORE_TOKEN_BUDGET = 1200
RECALL_TOKEN_BUDGET = 1200
FILE_FALLBACK_TOKEN_BUDGET = 2400


@dataclass(frozen=True)
class MemoryPromptPayload:
    text: str
    mode: Literal["vector", "file_fallback", "disabled"]
    token_count: int
    diagnostic: str


def extract_always_core(markdown: str, token_budget: int) -> str:
    sections = _markdown_sections(markdown)
    always = [body for heading, body in sections if heading.casefold() == "always"]
    selected = "\n\n".join(always).strip()
    if not selected:
        selected = markdown.strip()
    return truncate_to_tokens(selected, token_budget)


class EmbeddingControl:
    _instances: ClassVar[dict[tuple[Path, bool], "EmbeddingControl"]] = {}

    @classmethod
    def for_workspace(cls, workspace: Path, *, configured: bool = True) -> "EmbeddingControl":
        key = (workspace.resolve(strict=False), configured)
        if key not in cls._instances:
            cls._instances[key] = cls(key[0], configured=configured)
        return cls._instances[key]

    async def recall_for_turn(self, query: str) -> RecallOutcome:
        if not self.configured:
            return RecallOutcome((), "disabled", 0.0)
        if self.index.status().state in ("missing", "stale", "corrupt"):
            self.start_guarded_rebuild()
            return RecallOutcome((), self.index.fallback_reason(), 0.0)
        await self.reconcile_guarded()
        core = self.prompt_policy.core_texts()
        outcome = await self.recall_service.recall(query, core_texts=core)
        self._record_recall(outcome)
        return outcome
```

`EmbeddingStatusService.snapshot(configured: bool) -> EmbeddingStatus` 必须是共享状态的唯一组装点：model 直接取 manager status；index 只读 fingerprint/文件状态；sources 每次 scan 后以 `(source_revision, content_hash, active)` 对比 DB，计算 discovered/indexed/pending/stale/invalid/inactive；recall 的 active 仅在 configured、model ready、index ready 三者都满足时为真，并携带最近一次 latency/self-test/fallback。status 读取不得创建空 DB、下载模型或启动 rebuild。

`EmbeddingControl` 对同一 `(workspace, configured)` 只构造一个 manager/provider/catalog/index/recall/prompt/status 组合；同 workspace 用 `asyncio.Lock` 保护 reconcile，用一个保存引用的 `asyncio.Task` 启动 rebuild，已有任务未完成时不得重复启动。`configured=False` 的实例必须保持惰性，不创建 DB 或加载模型。Task 3 的 setup 只能由 CLI/WebUI/推荐安装显式触发，gateway 启动不得自动 pip install；模型已下载而 DB 缺失时可以后台 rebuild。

- [ ] **Step 4: 修改 ContextBuilder 和 AgentLoop 数据流**

```python
# ContextBuilder
BOOTSTRAP_FILES = ["AGENTS.md"]

def build_system_prompt(..., memory_prompt: MemoryPromptPayload | None = None, ...) -> str:
    # identity 与 AGENTS.md 保持现有逻辑
    soul = MemoryPromptPolicy(root).bounded_soul()
    if soul:
        parts.append((self._PRIORITY_CRITICAL, f"# Soul\n\n{soul}"))
    if memory_prompt and memory_prompt.text:
        parts.append((self._PRIORITY_MEMORY, memory_prompt.text))
    # 删除此方法内直接 vs.search、全量 get_memory_context、全量 shared memory 和 vector history 二次查询


# AgentLoop.__init__
self.embedding_control = EmbeddingControl.for_workspace(
    self.workspace, configured=vector_recall
)


# AgentLoop._build_initial_messages
recall = await self.embedding_control.recall_for_turn(msg.content)
memory_prompt = self.embedding_control.prompt_policy.build(recall)
return self.context.build_messages(..., memory_prompt=memory_prompt, ...)


# AgentRunSpec and AgentRunner._request_model
before_provider_call: Callable[[list[dict[str, Any]]], Awaitable[list[dict[str, Any]]]] | None = None

if spec.before_provider_call is not None:
    messages = await spec.before_provider_call(messages)
kwargs = self._build_request_kwargs(spec, messages, tools=spec.tools.get_definitions())
```

在 `_request_finalization_retry()` 构造 `retry_messages` 之后也执行同一个 callback，再调用 `_build_request_kwargs()`；测试再断言一次 finalization retry 会把 await count 从 2 增到 3。

同步修改 `build_messages()` 参数，完全移除 `query_embedding` 和 ContextBuilder 中的 `vector_recall` 搜索分支。当前全量注入 `memory/shared/MEMORY_SHARED.md` 的分支也移除，但文件本身保持不动；本轮只使用 Global Constraints 指定的权威来源。`AgentLoop` 给主对话的 `AgentRunSpec.before_provider_call` 注入闭包：用本轮原始用户 query 再调用 `recall_for_turn()`，再通过 `MemoryPromptPolicy.replace_section(messages, payload)` 只替换带 `<!-- miniunicorn-memory:start/end -->` 标记的 memory section。这样工具迭代和 finalization retry 每次真正调用聊天 provider 前都会重新读索引，但不会重复追加 prompt；Planner、Dream、Reflection 等内部专用 LLM 不读取用户长期记忆。`MemoryStore.index_text()` 及 Consolidator/Dream 的即时重复索引改为调用 `embedding_control.request_reconcile()` 或直接删除；文件成功写入后由 catalog reconcile 统一索引。保留 compatibility property 时只能返回同一 `VectorIndexManager`，不能再次 insert。`light_context` 仍必须包含有界 SOUL 和 core memory，只可跳过 AGENTS/skills/history。

- [ ] **Step 5: 运行运行时集成测试并提交**

Run: `.\.venv\Scripts\python.exe -m pytest tests/agent/test_memory_prompt.py tests/embedding/test_status_service.py tests/agent/test_context_builder.py tests/agent/test_runner_core.py tests/agent/test_upgrade_integration.py tests/agent/test_memory_store.py -q`

Expected: PASS；断言同一 procedural source 连续两次 Dream/reconcile 后 source count 不增加。

```powershell
git add miniunicorn/agent/memory_prompt.py miniunicorn/embedding/control.py miniunicorn/embedding/status_service.py miniunicorn/agent/context.py miniunicorn/agent/loop.py miniunicorn/agent/loop_builder.py miniunicorn/agent/runner.py miniunicorn/agent/memory.py tests/agent/test_memory_prompt.py tests/embedding/test_status_service.py tests/agent/test_context_builder.py tests/agent/test_runner_core.py tests/agent/test_upgrade_integration.py
git commit -m "feat(memory): integrate bounded recall into every chat turn"
```

### Task 12: “记住”触发、LLM 关系判断与冲突确认

**Files:**
- Modify: `miniunicorn/agent/explicit_memory.py`
- Modify: `miniunicorn/agent/loop.py`
- Modify: `miniunicorn/command/builtin.py`
- Create: `miniunicorn/templates/agent/memory_relation.md`
- Create: `tests/agent/test_explicit_memory_flow.py`
- Create: `tests/command/test_remember_command.py`

**Interfaces:**
- Consumes: active recall candidates、chat provider `chat_with_retry()`、journal append API、session metadata。
- Produces: `CaptureIntent`、`MemoryProposal`、`ExplicitMemoryService.detect()`、`propose()`、`resolve()`；AgentLoop 在普通 LLM 前调用 `_handle_explicit_memory()`。

- [ ] **Step 1: 写触发、否定/引用排除、duplicate 和 conflict 无写入测试**

```python
@pytest.mark.parametrize("text,fact", [
    ("/remember call me Alice", "call me Alice"),
    ("/记住 我不吃香菜", "我不吃香菜"),
    ("请记住我喜欢深色主题", "我喜欢深色主题"),
    ("帮我记一下，提交前运行测试", "提交前运行测试"),
    ("remember that I prefer concise answers", "I prefer concise answers"),
])
def test_unambiguous_triggers(text, fact):
    assert ExplicitMemoryService.detect(text) == CaptureIntent("explicit", fact)


@pytest.mark.parametrize("text", [
    "不要记住我刚才的话", "他说‘请记住我喜欢红色’", "什么情况下会说 remember that？"
])
def test_negated_quoted_or_discussed_phrases_do_not_write(text):
    assert ExplicitMemoryService.detect(text).kind != "explicit"


@pytest.mark.asyncio
async def test_conflict_requires_confirmation_before_append(service, journal):
    result = await service.propose("我喜欢深色主题", classifier=classifier("conflict"))
    assert result.action == "confirmation_required"
    assert "浅色主题" in result.user_message and "深色主题" in result.user_message
    assert len(journal.effective()) == 1
```

- [ ] **Step 2: 运行测试并确认服务不存在**

Run: `.\.venv\Scripts\python.exe -m pytest tests/agent/test_explicit_memory_flow.py -q`

Expected: FAIL with missing `ExplicitMemoryService`/capture contracts。

- [ ] **Step 3: 实现 detector、严格 JSON classifier 和业务状态机**

```python
Relation = Literal["duplicate", "supplement", "conflict", "unrelated"]

@dataclass(frozen=True)
class CaptureIntent:
    kind: Literal["explicit", "ambiguous", "none"]
    fact: str = ""


@dataclass(frozen=True)
class MemoryProposal:
    proposal_id: str
    action: Literal["saved", "duplicate", "confirmation_required", "clarification_required", "ignored", "failed"]
    raw_text: str
    normalized_fact: str
    candidate_memory_id: str | None
    candidate_revision: int | None
    user_message: str
    created_at: str


async def propose(self, raw_text: str, classifier: RelationClassifier) -> MemoryProposal:
    candidates = await self._candidate_facts(raw_text, limit=3)
    relation = await classifier(raw_text, candidates)
    if relation.label == "duplicate":
        return self._duplicate_proposal(raw_text, relation)
    if relation.label == "conflict":
        return self._confirmation_proposal(raw_text, relation)
    if relation.label in ("supplement", "unrelated"):
        saved = self.journal.append_new(raw_text, relation.normalized_fact, relation.scope)
        self.control.request_reconcile()
        return self._saved_proposal(saved)
    raise ValueError(f"unsupported relation: {relation.label}")
```

`detect()` 的明确前缀固定为 `/remember`、`/记住`、`记住`、`请记住`、`帮我记一下`、`以后请一直`、`remember that`、`please remember`、`save this to memory`，忽略大小写和开头空白；空 fact 是 ambiguous。句首 8 字符/20 英文字母内出现 `不要/别/do not/don't/never` 则 none；触发短语处于中文/英文引号或后接问号讨论时 none。含“可能、也许、有时候、暂时、maybe、sometimes”且不是 slash command 时 ambiguous，先回复“你希望我把这条保存为长期记忆吗？请回复‘确认记住’或‘不用记’。”

classifier prompt 必须只发送 proposal 与最多 3 个候选的 `memory_id/revision/text/scope/created_at/source_file`，要求只输出：

```json
{"label":"conflict","candidate_memory_id":"uuid-or-null","normalized_fact":"用户喜欢深色主题","scope":null,"reason":"与现有主题偏好相反"}
```

使用 `json_repair.loads` 后再次校验 label 白名单和 candidate ID 必须来自输入候选；解析/LLM 失败返回 `failed`，不写 journal。

- [ ] **Step 4: 接入 session-persisted 确认流程**

```python
async def _handle_explicit_memory(self, msg: InboundMessage, session: Session) -> OutboundMessage | None:
    pending = session.metadata.get("pending_memory_proposal")
    if pending:
        resolution = self.explicit_memory.parse_resolution(msg.content)
        if resolution is not None:
            result = await self.explicit_memory.resolve(MemoryProposal.from_dict(pending), resolution)
            session.metadata.pop("pending_memory_proposal", None)
            self.sessions.save(session)
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=result.user_message)
    intent = self.explicit_memory.detect(msg.content)
    if intent.kind == "none":
        return None
    if intent.kind == "ambiguous":
        session.metadata["pending_memory_proposal"] = self.explicit_memory.ambiguous(intent).to_dict()
        self.sessions.save(session)
        return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content="你希望我把这条保存为长期记忆吗？请回复“确认记住”或“不用记”。")
    proposal = await self.explicit_memory.propose(intent.fact, self._classify_memory_relation)
    if proposal.action in ("confirmation_required", "clarification_required"):
        session.metadata["pending_memory_proposal"] = proposal.to_dict()
        self.sessions.save(session)
    return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=proposal.user_message)
```

在普通 command dispatch 和主 LLM 调用前执行此方法，并在 `miniunicorn/command/builtin.py` 的 command spec/help 中登记 `/remember <内容>` 与 `/记住 <内容>`，handler 只转交 `ctx.loop._handle_explicit_memory()`，不得自行写文件。resolution 固定支持：

- `更新记忆`/`update`：对候选 `memory_id` append revision+1，新版 active。
- `保留原记忆`/`keep`/`不用记`：不写入。
- `分别适用：旧=<scope>; 新=<scope>` 或 `both: old=<scope>; new=<scope>`：先给旧 memory append 仅 scope 变化的新 revision，再用新 UUID append 新事实；任一 scope 为空则继续询问，不写入。
- `确认记住` 只用于 ambiguous proposal；确认后才进入同一 relation classifier，而不是直接写。

所有 response 必须包含人能看懂的新旧内容、来源和时间；不得显示向量。测试覆盖 gateway 重启后从 session metadata 继续 pending proposal，old revision 仍可 `history()` 查看但 catalog 只索引新 revision。

- [ ] **Step 5: 运行显式记忆全套测试并提交**

Run: `.\.venv\Scripts\python.exe -m pytest tests/agent/test_explicit_memory.py tests/agent/test_explicit_memory_flow.py tests/command/test_remember_command.py tests/agent/test_memory_sources.py tests/agent/test_upgrade_integration.py -q`

Expected: PASS，且 conflict 用例在确认前 journal 行数不变。

```powershell
git add miniunicorn/agent/explicit_memory.py miniunicorn/agent/loop.py miniunicorn/command/builtin.py miniunicorn/templates/agent/memory_relation.md tests/agent/test_explicit_memory_flow.py tests/command/test_remember_command.py
git commit -m "feat(memory): confirm conflicting explicit memory updates"
```

### Task 13: CLI `embedding` 命令组

**Files:**
- Create: `miniunicorn/cli/embedding_commands.py`
- Modify: `miniunicorn/cli/commands.py`
- Modify: `tests/cli/test_commands.py`
- Create: `tests/cli/test_embedding_commands.py`

**Interfaces:**
- Consumes: `EmbeddingControl.for_workspace()`、model `setup/verify/status`、index `rebuild`。
- Produces: `miniunicorn embedding setup|status|verify|rebuild`，均支持 `--workspace`；`status` 另支持 `--json`。

- [ ] **Step 1: 写帮助、状态 JSON 和失败退出码测试**

```python
def test_embedding_help_lists_all_operations(cli_runner):
    result = cli_runner.invoke(app, ["embedding", "--help"])
    assert result.exit_code == 0
    for command in ("setup", "status", "verify", "rebuild"):
        assert command in result.stdout


def test_embedding_status_json_uses_shared_contract(cli_runner, monkeypatch, tmp_path):
    monkeypatch.setattr(
        "miniunicorn.cli.embedding_commands.EmbeddingControl.for_workspace",
        lambda workspace, configured=True: FakeControl(status_payload()),
    )
    result = cli_runner.invoke(app, ["embedding", "status", "--workspace", str(tmp_path), "--json"])
    assert result.exit_code == 0
    assert set(json.loads(result.stdout)) >= {"model", "index", "sources", "recall"}


def test_embedding_verify_failure_is_nonzero_but_does_not_delete_files(cli_runner, failing_control, tmp_path):
    source = tmp_path / "memory" / "MEMORY.md"
    source.parent.mkdir()
    source.write_text("保留我", encoding="utf-8")
    result = cli_runner.invoke(app, ["embedding", "verify", "--workspace", str(tmp_path)])
    assert result.exit_code == 1
    assert source.read_text(encoding="utf-8") == "保留我"
```

- [ ] **Step 2: 运行测试并确认命令组不存在**

Run: `.\.venv\Scripts\python.exe -m pytest tests/cli/test_embedding_commands.py tests/cli/test_commands.py -q`

Expected: FAIL because `embedding` is not registered。

- [ ] **Step 3: 实现 Typer 命令和统一渲染**

```python
embedding_app = typer.Typer(help="Manage local embedding and memory index")


def _control(workspace: str | None) -> EmbeddingControl:
    return EmbeddingControl.for_workspace(get_workspace_path(workspace), configured=True)


@embedding_app.command("status")
def status_command(
    workspace: str | None = typer.Option(None, "--workspace", "-w"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    payload = _control(workspace).status().to_dict()
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        _render_status(payload)


@embedding_app.command("setup")
def setup_command(workspace: str | None = typer.Option(None, "--workspace", "-w"),
                  force: bool = typer.Option(False, "--force")) -> None:
    result = asyncio.run(_control(workspace).setup(force=force))
    _render_status(result.to_dict())
    if result.model.state != "ready":
        raise typer.Exit(1)
```

`verify` 执行真实 model self-test 加 index validation；`rebuild` 先 verify model，再完整 rebuild。请求操作失败退出 `1`，参数错误由 Typer 退出 `2`，成功退出 `0`。Rich 表格四行固定中文标签“模型/索引/来源同步/实际检索”，技术字段随后显示；`--json` stdout 只能含一个 JSON object，日志进 stderr。

在 `commands.py` 注册且不引入 import cycle：

```python
from miniunicorn.cli.embedding_commands import embedding_app

app.add_typer(embedding_app, name="embedding")
```

- [ ] **Step 4: 运行 CLI 测试和真实 disabled status smoke test**

Run: `.\.venv\Scripts\python.exe -m pytest tests/cli/test_embedding_commands.py tests/cli/test_commands.py -q; .\.venv\Scripts\miniunicorn.exe embedding status --json`

Expected: tests PASS；smoke test 输出可解析 JSON，即使模型未下载也正常退出并显示 `not_downloaded`，status 本身不因 not-ready 退出非零。

- [ ] **Step 5: 提交**

```powershell
git add miniunicorn/cli/embedding_commands.py miniunicorn/cli/commands.py tests/cli/test_embedding_commands.py tests/cli/test_commands.py
git commit -m "feat(cli): add embedding setup and repair commands"
```

### Task 14: WebUI 后端状态、互斥操作和安全搜索 API

**Files:**
- Create: `miniunicorn/webui/embedding_api.py`
- Create: `miniunicorn/channels/websocket/handlers/embedding.py`
- Modify: `miniunicorn/channels/websocket/handlers/__init__.py`
- Create: `tests/webui/test_embedding_api.py`
- Modify: `tests/channels/test_websocket_http_routes.py`

**Interfaces:**
- Consumes: shared `EmbeddingControl` 和 `RouteContext.deps.workspace_path`。
- Produces: authenticated endpoints `GET /api/embedding/status`、`POST /api/embedding/setup`、`POST /api/embedding/verify`、`POST /api/embedding/rebuild`、`GET /api/embedding/search?q=...`。

- [ ] **Step 1: 写 auth、状态一致性、操作冲突和路径脱敏测试**

```python
def test_embedding_status_matches_shared_contract(client, fake_control):
    response = client.get("/api/embedding/status", headers=auth())
    assert response.status_code == 200
    assert response.json() == fake_control.status().to_dict()


def test_second_long_operation_returns_409(client, running_control):
    response = client.post("/api/embedding/rebuild", headers=auth())
    assert response.status_code == 409
    assert response.json()["error"] == "operation_already_running"


def test_search_returns_workspace_relative_source_and_no_vector(client, ready_control):
    response = client.get("/api/embedding/search?q=主题", headers=auth())
    row = response.json()["results"][0]
    assert row["source_file"] == "USER.md"
    assert "embedding" not in row and "vector" not in row
    assert not Path(row["source_file"]).is_absolute()
```

沿用现有 websocket HTTP 测试 fixture；每个 endpoint 还要有无 token 返回 `401` 的参数化测试。

- [ ] **Step 2: 运行测试并确认路由是 404**

Run: `.\.venv\Scripts\python.exe -m pytest tests/webui/test_embedding_api.py tests/channels/test_websocket_http_routes.py -q`

Expected: FAIL，新增路由返回 404 或 handler 未注册。

- [ ] **Step 3: 实现 API service 和 operation 状态**

```python
@dataclass
class OperationState:
    id: str
    kind: Literal["setup", "verify", "rebuild"]
    state: Literal["running", "succeeded", "failed", "cancelled"]
    completed: int = 0
    total: int = 0
    message: str = ""


class EmbeddingApiService:
    def __init__(self, workspace: Path, *, configured: bool) -> None:
        self.control = EmbeddingControl.for_workspace(workspace, configured=configured)

    def start(self, kind: str) -> dict[str, object]:
        if self.control.operation_running:
            raise EmbeddingApiError(409, "operation_already_running", "已有模型或索引任务正在运行")
        operation = self.control.start_operation(kind)
        return {"accepted": True, "operation": operation.to_dict()}

    async def search(self, query: str) -> dict[str, object]:
        clean = query.strip()
        if not clean or len(clean) > 500:
            raise EmbeddingApiError(400, "invalid_query", "搜索内容长度必须为 1 到 500 字符")
        outcome = await self.control.recall_service.recall(clean)
        return {"results": [asdict(row) for row in outcome.records],
                "fallback_reason": outcome.fallback_reason,
                "latency_ms": outcome.latency_ms}
```

`EmbeddingControl.start_operation()` 用 task 引用加 lock 保证 setup/verify/rebuild 全局互斥；progress callback 更新 `completed/total/message`。完成或失败状态至少保留到下一次操作启动。status 中始终包含 operation 快照，所以前端可轮询。HTTP 返回不等待长任务完成：成功启动为 `202`；冲突 `409`；同步参数错误 `400`；内部失败由 operation 变 `failed`，status 仍为 `200`。

- [ ] **Step 4: 注册独立 handler**

```python
@router.route("/api/embedding/status", methods={"GET"})
@require_auth
def embedding_status(ctx: RouteContext) -> Response:
    configured = load_config().agents.defaults.vector_recall
    service = EmbeddingApiService(ctx.deps.workspace_path, configured=configured)
    return _http_json_response(service.control.status().to_dict())


@router.route("/api/embedding/rebuild", methods={"POST"})
@require_auth
def embedding_rebuild(ctx: RouteContext) -> Response:
    try:
        configured = load_config().agents.defaults.vector_recall
        return _http_json_response(
            EmbeddingApiService(ctx.deps.workspace_path, configured=configured).start("rebuild"), status=202
        )
    except EmbeddingApiError as exc:
        return _http_json_response({"error": exc.code, "message": exc.message}, status=exc.status)
```

用同一模式实现 setup/verify；search handler 为 async。显式 `vectorRecall:false` 时 status 仍返回 `configured=false/fallback_reason=disabled`，mutating action 返回 `409 embedding_disabled`，且不得创建 DB。把 `embedding` 加进 `handlers/__init__.py` 导入清单。不得把新路由塞回巨大的 `_http_routes.py`。

- [ ] **Step 5: 运行后端 API 测试并提交**

Run: `.\.venv\Scripts\python.exe -m pytest tests/webui/test_embedding_api.py tests/channels/test_websocket_http_routes.py tests/webui/test_settings_api.py -q`

Expected: PASS，现有 settings 路由无回归。

```powershell
git add miniunicorn/webui/embedding_api.py miniunicorn/channels/websocket/handlers/embedding.py miniunicorn/channels/websocket/handlers/__init__.py tests/webui/test_embedding_api.py tests/channels/test_websocket_http_routes.py
git commit -m "feat(webui): expose embedding status and repair API"
```

### Task 15: WebUI 四卡状态页、进度与来源搜索

**Files:**
- Modify: `webui/src/lib/types.ts`
- Modify: `webui/src/lib/api.ts`
- Modify: `webui/src/components/settings/types.ts`
- Modify: `webui/src/components/settings/SettingsView.tsx`
- Create: `webui/src/components/settings/sections/MemoryEmbeddingSettings.tsx`
- Create: `webui/src/components/settings/hooks/useEmbeddingStatus.ts`
- Modify: `webui/src/tests/settings-view.test.tsx`
- Create: `webui/src/tests/memory-embedding-settings.test.tsx`

**Interfaces:**
- Consumes: Task 14 JSON endpoints。
- Produces: `EmbeddingStatusPayload`、`fetchEmbeddingStatus()`、`startEmbeddingOperation()`、`searchEmbeddingMemory()`，以及 settings 导航中的 `memory` section。

- [ ] **Step 1: 写四卡、展开详情、操作互斥和搜索来源测试**

```tsx
it("renders four plain-language status cards", async () => {
  render(<MemoryEmbeddingSettings token="token" />);
  expect(await screen.findByText("模型")).toBeInTheDocument();
  expect(screen.getByText("索引")).toBeInTheDocument();
  expect(screen.getByText("来源同步")).toBeInTheDocument();
  expect(screen.getByText("实际检索")).toBeInTheDocument();
});

it("disables every operation while rebuild is running", async () => {
  server.use(statusWithOperation({ kind: "rebuild", state: "running", completed: 2, total: 8 }));
  render(<MemoryEmbeddingSettings token="token" />);
  expect(await screen.findByText("2 / 8")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "重新下载模型" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "校验模型" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "重建索引" })).toBeDisabled();
});

it("shows source identity but never raw vectors", async () => {
  render(<MemoryEmbeddingSettings token="token" />);
  await userEvent.type(await screen.findByRole("searchbox"), "主题");
  await userEvent.click(screen.getByRole("button", { name: "搜索记忆" }));
  expect(await screen.findByText("USER.md · user:preferences:1")).toBeInTheDocument();
  expect(screen.queryByText(/embedding.*\[/i)).not.toBeInTheDocument();
});
```

- [ ] **Step 2: 运行前端测试并确认组件/导航不存在**

Run: `cd webui; npm test -- --run src/tests/memory-embedding-settings.test.tsx src/tests/settings-view.test.tsx`

Expected: FAIL with missing component/type or missing “记忆与 Embedding” navigation。

- [ ] **Step 3: 定义前端 contract 和 API 函数**

```typescript
export interface EmbeddingStatusPayload {
  model: { state: "not_downloaded" | "downloading" | "verifying" | "ready" | "corrupt" | "failed"; model_id?: string | null; revision?: string | null; dimension?: number | null; cache_path?: string | null; bytes: number; last_self_test?: string | null; last_error_code?: string | null; message: string };
  index: { state: "missing" | "building" | "ready" | "stale" | "corrupt" | "failed"; path?: string | null; bytes: number; last_rebuild?: string | null; last_error_code?: string | null; message: string };
  sources: { discovered: number; indexed: number; pending: number; stale: number; invalid: number; inactive: number; errors: Array<Record<string, unknown>> };
  recall: { configured: boolean; active: boolean; fallback_reason?: string | null; last_self_test?: string | null; last_latency_ms?: number | null };
  operation?: { id: string; kind: "setup" | "verify" | "rebuild"; state: "running" | "succeeded" | "failed" | "cancelled"; completed: number; total: number; message: string } | null;
}

export function fetchEmbeddingStatus(token: string, base = "") {
  return request<EmbeddingStatusPayload>(`${base}/api/embedding/status`, token);
}

export function startEmbeddingOperation(token: string, kind: "setup" | "verify" | "rebuild", base = "") {
  return request<{ accepted: boolean; operation: NonNullable<EmbeddingStatusPayload["operation"]> }>(
    `${base}/api/embedding/${kind}`, token, { method: "POST" },
  );
}
```

search 类型必须逐字段定义 `source_id/source_type/source_file/source_revision/text/content_hash/similarity/score/token_count/synchronized`，不能用 `any`。

- [ ] **Step 4: 实现 hook、四卡页和 settings 导航**

```tsx
export function useEmbeddingStatus(token: string) {
  const [status, setStatus] = useState<EmbeddingStatusPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const refresh = useCallback(async () => {
    try { setStatus(await fetchEmbeddingStatus(token)); setError(null); }
    catch (value) { setError((value as Error).message); }
  }, [token]);
  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => {
    if (status?.operation?.state !== "running") return;
    const id = window.setInterval(() => void refresh(), 2000);
    return () => window.clearInterval(id);
  }, [refresh, status?.operation?.state]);
  return { status, error, refresh };
}
```

在 `SettingsSectionKey` 增加 `"memory"`，nav item 使用 `BrainCircuit` 图标、fallback `"Memory & Embedding"`；`SettingsView.renderSection()` 增加 `<MemoryEmbeddingSettings token={token} />`。页面第一屏只显示四张大字状态卡及一句行动建议；“技术详情”折叠后再显示 model ID/revision/dimension/cache path/bytes、index path/bytes/rebuild time、source counts/error lines、recall latency/fallback。状态颜色固定：ready/active 绿色，building/downloading/verifying 蓝色，missing/stale/not_downloaded 黄色，corrupt/failed 红色，disabled 灰色。按钮中文为“重新下载模型”“校验模型”“重建索引”，running 时三者全 disabled 并显示进度。

- [ ] **Step 5: 运行前端测试、lint、build 并提交**

Run: `cd webui; npm test -- --run src/tests/memory-embedding-settings.test.tsx src/tests/settings-view.test.tsx; npm run lint; npm run build`

Expected: 三条命令均退出 `0`，窄屏四卡为单列、桌面为 2x2，页面不存在横向溢出。

```powershell
git add webui/src/lib/types.ts webui/src/lib/api.ts webui/src/components/settings/types.ts webui/src/components/settings/SettingsView.tsx webui/src/components/settings/sections/MemoryEmbeddingSettings.tsx webui/src/components/settings/hooks/useEmbeddingStatus.ts webui/src/tests/settings-view.test.tsx webui/src/tests/memory-embedding-settings.test.tsx
git commit -m "feat(webui): add observable memory embedding center"
```

### Task 16: 推荐安装首次自动 setup、包边界和用户文档

**Files:**
- Modify: `miniunicorn/cli/commands.py`
- Modify: `tests/cli/test_commands.py`
- Modify: `README.md`
- Modify: `README.en.md`
- Modify: `THIRD_PARTY_NOTICES.md`
- Modify: `pyproject.toml`
- Modify: `docs/README.md`
- Create: `docs/embedding-memory.md`
- Create: `tests/packaging/test_vector_distribution.py`

**Interfaces:**
- Consumes: Task 13 `EmbeddingControl.setup()`。
- Produces: 推荐安装 `pip install "miniunicorn-ai[vector]"` + `miniunicorn onboard` 自动尝试 setup；失败只告警且 onboard 成功。

- [ ] **Step 1: 写 onboard fail-open 和 wheel 不含模型测试**

```python
def test_onboard_attempts_embedding_setup_but_download_failure_is_nonfatal(cli_runner, monkeypatch, tmp_path):
    setup = AsyncMock(return_value=failing_status("download_failed"))
    monkeypatch.setattr("miniunicorn.cli.commands._setup_embedding_after_onboard", setup)
    result = cli_runner.invoke(app, ["onboard", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    setup.assert_awaited_once()
    assert "聊天仍可正常使用" in result.stdout


def test_built_wheel_contains_no_model_binary(built_wheel):
    with zipfile.ZipFile(built_wheel) as archive:
        names = archive.namelist()
    assert not any(name.endswith((".onnx", ".bin", ".safetensors")) for name in names)
    assert not any("bge-small-zh-v1.5" in name for name in names)
```

- [ ] **Step 2: 运行测试并确认自动 setup/文档尚未实现**

Run: `.\.venv\Scripts\python.exe -m pytest tests/cli/test_commands.py tests/packaging/test_vector_distribution.py -q`

Expected: 新 onboard 断言失败；已有打包测试不得回归。

- [ ] **Step 3: 在 onboard 完成后 fail-open 地执行 setup**

```python
async def _setup_embedding_after_onboard(workspace: Path, *, vector_recall: bool) -> ModelStatus | None:
    if not vector_recall:
        return None
    control = EmbeddingControl.for_workspace(workspace, configured=True)
    return (await control.setup(force=False)).model


def _report_embedding_onboard(status: ModelStatus | None) -> None:
    if status is None:
        console.print("[dim]向量记忆已按配置关闭。[/dim]")
    elif status.state == "ready":
        console.print("[green]本地 Embedding 模型已就绪。[/green]")
    else:
        console.print(
            "[yellow]Embedding 模型暂未就绪；聊天仍可正常使用。"
            "稍后运行 `miniunicorn embedding setup` 重试。[/yellow]"
        )
```

只在 onboard 已经成功写入配置和 workspace 后调用 `asyncio.run()`；捕获所有 setup 异常并转成失败 status，绝不能让 onboard 因模型失败回滚。若 base install 缺 vector extra，显示精确命令 `pip install "miniunicorn-ai[vector]"`。显式 `vectorRecall:false` 完全跳过。

- [ ] **Step 4: 写面向小白的文档和第三方声明**

`docs/embedding-memory.md` 必须按以下固定顺序给出完整内容：

1. “它解决什么”：文件保存原始记忆，`memory.db` 加速查找，删除 DB 不丢记忆。
2. “推荐安装”：`pip install "miniunicorn-ai[vector]"`，再 `miniunicorn onboard`；首次会下载模型，失败仍能聊天。
3. “平时什么时候读取”：每次 LLM 前本地查 DB，只把最多 5 条相关内容发给 LLM；本地查 DB 本身不花 LLM token。
4. “哪些文件始终/按需读取”：SOUL 有界始终发送，USER/MEMORY 仅 Always 核心固定发送，其余按需召回。
5. “查看和修复”：四条 CLI 命令和 WebUI 四卡。
6. “更新记忆”：明确触发词、duplicate、冲突三种选择、旧 revision 保留。
7. “关闭和回滚”：JSON `{"agents":{"defaults":{"vectorRecall":false}}}`，不改任何源文件。
8. “隐私”：CPU 本地 embedding，模型下载来自固定 revision，只有被召回的小片段进入聊天 LLM。

README 中把基础安装与推荐完整安装区分清楚，推荐项放前面；`THIRD_PARTY_NOTICES.md` 增加 FastEmbed、ONNX Runtime、Hugging Face Hub、sqlite-vec 的许可证/项目链接，不复制模型权重。在 `pyproject.toml` 的 `dev` extra 增加 `build>=1.2.0,<2.0.0`，使发布门禁命令在全新 dev 环境可执行。

- [ ] **Step 5: 运行测试、构建 wheel、检查内容并提交**

Run: `.\.venv\Scripts\python.exe -m pytest tests/cli/test_commands.py tests/packaging/test_vector_distribution.py -q; .\.venv\Scripts\python.exe -m build; .\.venv\Scripts\python.exe -m zipfile -l (Get-ChildItem dist\*.whl | Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName`

Expected: 测试 PASS，wheel 列表包含 Python/WebUI 文件但不含 `.onnx/.bin/.safetensors` 或模型缓存。

```powershell
git add miniunicorn/cli/commands.py tests/cli/test_commands.py README.md README.en.md THIRD_PARTY_NOTICES.md pyproject.toml docs/README.md docs/embedding-memory.md tests/packaging/test_vector_distribution.py
git commit -m "docs(embedding): add fail-open recommended setup"
```

### Task 17: 真实中文 embedding 发布证明脚本

**Files:**
- Create: `scripts/verify_embedding_memory.py`
- Create: `tests/scripts/test_verify_embedding_memory_script.py`
- Modify: `docs/embedding-memory.md`

**Interfaces:**
- Consumes: production model manager/provider/catalog/index/recall，不可 monkeypatch production proof。
- Produces: `python scripts/verify_embedding_memory.py [--model-dir PATH] [--keep-workspace]`，成功 stdout 最后一行为 JSON，失败退出 `1`。

- [ ] **Step 1: 写脚本参数和证据字段 contract 测试**

```python
def test_proof_payload_requires_every_release_evidence():
    assert REQUIRED_EVIDENCE == {
        "model_ready", "cpu_512_finite_normalized", "two_chinese_memories",
        "persisted_after_reopen", "relevant_query_ranked_first",
        "unchanged_reconcile_idempotent", "changed_source_updated",
        "deleted_db_rebuilt", "cancelled_rebuild_preserved_old_index",
        "fingerprint_mismatch_rejected", "max_five_under_budget",
        "provenance_complete", "safe_noop_chat_fallback",
    }
```

这个单元测试只校验 parser、临时目录清理和 payload schema；它不能把真实模型执行伪造成通过。

- [ ] **Step 2: 运行测试并确认脚本不存在**

Run: `.\.venv\Scripts\python.exe -m pytest tests/scripts/test_verify_embedding_memory_script.py -q`

Expected: FAIL with import/file error for proof script。

- [ ] **Step 3: 实现真实 proof 流程**

```python
async def run_proof(model_dir: Path | None, keep_workspace: bool) -> dict[str, object]:
    root = Path(tempfile.mkdtemp(prefix="miniunicorn-embedding-proof-"))
    evidence: dict[str, bool] = {name: False for name in REQUIRED_EVIDENCE}
    try:
        write_authoritative_sources(root)
        manager = EmbeddingModelManager(model_dir)
        if manager.status().state != "ready":
            await manager.setup()
        provider = LocalEmbeddingProvider(manager=manager)
        sample = await provider.embed(["我喜欢在早晨喝热茶"])
        evidence["model_ready"] = manager.status().state == "ready"
        vector = sample.vectors[0]
        evidence["cpu_512_finite_normalized"] = (
            len(vector) == 512 and all(math.isfinite(v) for v in vector)
            and 0.999 <= math.sqrt(sum(v * v for v in vector)) <= 1.001
        )
        catalog = MemorySourceCatalog(root)
        index = VectorIndexManager(root / "memory" / "memory.db")
        await index.rebuild(catalog, provider)
        # 后续每项用 production API 实际断言并更新 evidence
        return {"ok": all(evidence.values()), "workspace": str(root), "evidence": evidence}
    finally:
        if not keep_workspace:
            shutil.rmtree(root, ignore_errors=True)
```

`write_authoritative_sources()` 必须写两条语义明显不同的中文事实：“用户早餐喜欢喝无糖豆浆”和“项目部署服务器使用 Debian 12”。proof 按以下实际顺序运行并逐项赋值：初次 rebuild → close/reopen → 查询“早餐喝什么”且豆浆第一、similarity>=0.45 → unchanged reconcile 前后 row/count/hash 不变 → 编辑豆浆为“燕麦奶”后同 source ID 更新 → close 并删除 DB → 从源文件完整 rebuild → 对已有有效 DB 启动预置 cancel_event 的 rebuild 并比较前后查询结果 → 用错误 revision 打开并确认不能 search → 构造 8 个候选验证最多 5 条/1200 token → 检查每条 source file/id/revision/synchronized → 临时重命名 manifest 后构造 prompt fallback 并确认能生成普通 messages。

脚本不得导入或复用三 Worker 分支的 runtime store。每个 evidence 为 `False` 时 stderr 输出具体 expected/actual；最终 JSON 前可打印进度，但最后一行必须是机器可读 JSON。任一 false 退出 `1`。

- [ ] **Step 4: 运行单元测试和真实 proof**

Run: `.\.venv\Scripts\python.exe -m pytest tests/scripts/test_verify_embedding_memory_script.py -q; .\.venv\Scripts\python.exe scripts/verify_embedding_memory.py`

Expected: 单元测试 PASS；首次真实 proof 可以下载模型，最后 JSON 为 `{"ok":true,...}`，13 个 evidence 全为 true。若网络下载失败，聊天 fallback 证据仍应通过，但整个 proof 必须退出 `1` 并明确指出 `model_ready=false`，不能假通过。

- [ ] **Step 5: 文档记录 proof 命令并提交**

```powershell
git add scripts/verify_embedding_memory.py tests/scripts/test_verify_embedding_memory_script.py docs/embedding-memory.md
git commit -m "test(embedding): add real local memory proof"
```

### Task 18: 全量回归、Windows 打包门禁和弃用代码收口

**Files:**
- Modify: `miniunicorn/agent/vector_memory.py`
- Modify: `miniunicorn/agent/memory.py`
- Modify: `tests/agent/test_upgrade_integration.py`
- Modify: `docs/embedding-memory.md`

**Interfaces:**
- Consumes: Tasks 1–17 的最终 production APIs。
- Produces: 单一索引实现、无死调用、全量可发布证据和清晰 rollback 文档。

- [ ] **Step 1: 写最终架构不变量测试**

```python
def test_runtime_has_one_vector_index_implementation():
    import miniunicorn.agent.vector_memory as compatibility
    from miniunicorn.agent.vector_index import VectorIndexManager
    assert compatibility.VectorMemoryStore is VectorIndexManager


def test_memory_store_writes_sources_but_never_directly_inserts_vectors(tmp_path):
    source = inspect.getsource(MemoryStore)
    assert ".index(" not in source
    assert "memory.db" not in source


def test_explicit_false_constructs_no_embedding_runtime(tmp_path):
    loop = build_loop(tmp_path, vector_recall=False)
    assert loop.embedding_control.configured is False
    assert not (tmp_path / "memory" / "memory.db").exists()
```

- [ ] **Step 2: 运行最终定向 suite 并修复实际回归**

Run: `.\.venv\Scripts\python.exe -m pytest tests/providers/test_local_embedding.py tests/embedding tests/agent/test_memory_sources.py tests/agent/test_explicit_memory.py tests/agent/test_explicit_memory_flow.py tests/agent/test_vector_index.py tests/agent/test_vector_reconcile.py tests/agent/test_vector_rebuild.py tests/agent/test_memory_recall.py tests/agent/test_memory_prompt.py tests/agent/test_context_builder.py tests/agent/test_upgrade_integration.py tests/cli/test_embedding_commands.py tests/webui/test_embedding_api.py tests/channels/test_websocket_http_routes.py -q`

Expected: PASS；只有明确需要缺失可选二进制的用例允许 skip，skip reason 必须写明缺少 `sqlite_vec` 或真实模型。

- [ ] **Step 3: 删除双写路径并保留窄兼容面**

```python
# miniunicorn/agent/vector_memory.py final content
"""Backward-compatible imports for the production vector index."""

from miniunicorn.agent.vector_index import VectorIndexManager

VectorMemoryStore = VectorIndexManager


class NoOpVectorStore:
    def __init__(self, reason: str = "disabled") -> None:
        self.enabled = False
        self.reason = reason

    def index(self, *args, **kwargs):
        return None

    def search(self, *args, **kwargs):
        return []

    def count(self) -> int:
        return 0

    def close(self) -> None:
        return None


def create_vector_store(db_path, embedding_dim=512, model_id="BAAI/bge-small-zh-v1.5"):
    if embedding_dim != 512 or model_id != "BAAI/bge-small-zh-v1.5":
        return NoOpVectorStore(reason="fingerprint_mismatch")
    return VectorIndexManager(db_path)
```

如果现有第三方/测试仍依赖 `NoOpVectorStore.index/search/enabled/close`，保留这个小类且返回 typed reason；生产 `AgentLoop`、`MemoryStore`、Dream、Consolidator 不得再 import 它。用 `rg -n "index_text\(|attach_vector_store\(|_compute_query_embedding|query_embedding=" miniunicorn tests` 找出旧入口：生产调用应为零，测试只允许兼容契约测试。不得顺手重构无关 memory/dream 代码。

- [ ] **Step 4: 执行全量质量和发布门禁**

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check miniunicorn tests scripts
Push-Location webui
npm test -- --run
npm run lint
npm run build
Pop-Location
.\.venv\Scripts\python.exe -m build
.\.venv\Scripts\python.exe scripts/verify_embedding_memory.py
git diff --check
```

Expected: 所有命令退出 `0`；真实 proof 最后一行 `ok=true`；wheel 和 sdist 构建成功；WebUI production build 成功；`git diff --check` 无输出。若真实 proof 因网络不可用失败，不得声称完成：保留失败证据，在有网络机器重跑到通过。

- [ ] **Step 5: 检查 scope、提交最终收口**

```powershell
git status --short
git diff --stat f0adcf63..HEAD
git log --oneline f0adcf63..HEAD
git add miniunicorn/agent/vector_memory.py miniunicorn/agent/memory.py tests/agent/test_upgrade_integration.py docs/embedding-memory.md
git commit -m "refactor(memory): retire prototype vector write paths"
git status --short --branch
```

Expected: 最终提交只含本 Task 的兼容收口；工作区只允许保留用户原有无关未跟踪文件。不得执行 `git clean`、`git reset --hard` 或删除用户文件。

## Design-to-Task Traceability

| Approved design requirement | Implemented by |
|---|---|
| 固定 BGE 模型、revision、CPU、512 维、manifest hash | Tasks 1–4 |
| 推荐安装首次下载但失败不阻断聊天 | Tasks 3、13、16 |
| 文件是唯一可信来源、SOUL 不索引 | Tasks 5、6、8 |
| source ID/revision/hash/provenance | Tasks 5、7、8 |
| 幂等增量同步，不重复 procedural/history | Tasks 7、8、11 |
| 删除 DB 完整重建、取消保留旧库、mismatch 禁用 | Tasks 7–9 |
| 每次 LLM 前本地召回，最多 5 条/1200 token | Tasks 10、11 |
| SOUL 始终有界，USER/MEMORY 只固定发 Always core | Task 11 |
| embedding 不可用时有界文件 fallback | Tasks 10、11 |
| `/remember`、自然触发、含糊确认 | Task 12 |
| duplicate 不新增 identity、conflict 先询问、旧 revision 可查 | Tasks 6、8、12 |
| CLI 四命令共享状态 | Task 13 |
| WebUI 四卡、详情、进度、操作互斥、来源搜索 | Tasks 14、15 |
| wheel 不含模型、隐私和回滚文档 | Task 16 |
| 真实中文模型、持久化、重建和 safe-noop 证明 | Task 17 |
| Python/前端/lint/type/build/Windows 发布门禁 | Task 18 |
| 不引入三 Worker | Global Constraints、Tasks 17–18 |

## Final Handoff Checklist

- [ ] 18 个 Task 均有独立提交，提交顺序与本计划一致。
- [ ] `git log --oneline f0adcf63..HEAD` 不包含三 Worker runtime 提交。
- [ ] `miniunicorn embedding status --json` 与 `/api/embedding/status` 四个 section 字段和值一致。
- [ ] 删除测试 workspace 的 `memory/memory.db` 后，源文件仍在且 rebuild 恢复全部 active records。
- [ ] conflict 自动化测试证明用户确认前 `explicit.jsonl` 字节不变。
- [ ] `scripts/verify_embedding_memory.py` 在有网络/有模型环境下返回 `ok=true`。
- [ ] 全量 pytest、Ruff、Vitest、ESLint、TypeScript/Vite build、Python build 全部通过。
- [ ] 最终 `git status --short` 没有意外业务文件；四类用户原有未跟踪文件保持原样。
