# 本地向量记忆（Embedding Memory）

MiniUnicorn 的长期记忆建立在两层之上：**源文件**（Markdown，人可读、可编辑）与**向量索引**（`memory/memory.db`，机器优先、加速检索）。本文档解释向量记忆解决什么、如何安装、何时读取、如何查看修复、如何更新、如何关闭，以及它的隐私边界。

---

## 1. 它解决什么

长期记忆的原始事实始终保存在工作区的 Markdown 文件里：

```
workspace/
├── SOUL.md              # 人格语气（始终注入，有 token 上限）
├── USER.md              # 用户画像（# Always 段落始终注入，其余按需召回）
└── memory/
    ├── MEMORY.md        # 项目事实与决策（# Always 段落始终注入，其余按需召回）
    ├── history.jsonl    # 追加式历史摘要
    └── memory.db        # 可选的本地向量索引（加速检索，可删除重建）
```

**源文件是真相之源**。`memory/memory.db` 只是一个加速检索的缓存索引——它把上述文件（以及历史摘要）的内容切块、向量化后存入 SQLite + sqlite-vec，供语义相似搜索使用。

关键性质：

- **删除 `memory.db` 不会丢失任何记忆**。下一次对话时，系统会发现索引缺失，自动降级为全量注入（把 `MEMORY.md` 完整内容发给 LLM），并在后台重建索引。
- **编辑 Markdown 文件后**，索引会在下一次检索时自动增量同步（reconcile），把新增/变更的段落重新向量化。
- **索引损坏**也能安全恢复：`miniunicorn embedding rebuild` 会删除旧索引、重新扫描全部源文件并重建。

换句话说，`memory.db` 是"派生物"，任何时候都可以安全删除或重建——只要源文件还在，记忆就在。

---

## 2. 推荐安装

向量记忆依赖三个可选包（`sqlite-vec`、`fastembed`、`huggingface-hub`），不包含在基础安装中。推荐安装方式：

```bash
# 推荐：完整安装（含本地向量记忆）
pip install "miniunicorn-ai[vector]"

# 然后运行初始化
miniunicorn onboard
```

`miniunicorn onboard` 在创建配置和工作区之后，会**自动尝试**下载并校验本地 Embedding 模型（`BAAI/bge-small-zh-v1.5`，约 100 MB）：

- **首次运行**会从 Hugging Face 下载模型文件，下载完成后做 SHA-256 校验和自测。
- **下载失败不阻塞**：onboard 仍然成功完成（退出码 0），你会看到提示"Embedding 模型暂未就绪；聊天仍可正常使用"。之后随时运行 `miniunicorn embedding setup` 重试下载。
- **聊天功能不受影响**：即使向量记忆完全不可用，MiniUnicorn 仍然正常工作——只是退化为全量注入 `MEMORY.md`，而不是语义召回。

未安装 `[vector]` 依赖时，系统检测到缺失会自动降级，不会报错。

---

## 3. 平时什么时候读取

向量记忆的读取发生在**每次 LLM 调用之前**，流程如下：

1. **本地数据库查询**：AgentLoop 在向 LLM 发送请求前，先用用户当前消息作为查询，在 `memory.db` 中做 top-k 语义相似搜索。
2. **最多 5 条**：默认最多召回 5 条最相关的记忆片段（`DEFAULT_MAX_RESULTS = 5`），每条附带来源标注（来自哪个文件、哪一段）。
3. **注入到 prompt**：召回的片段作为"记忆"段落注入 LLM 的上下文，与系统提示、SOUL、Always 核心一起构成完整 prompt。
4. **本地查询不花 LLM token**：向量化（embedding）和相似搜索完全在本地 CPU 上执行，不调用任何远程 API，不产生 LLM 费用。只有最终召回的少量片段会进入 LLM 上下文。

如果索引不存在或不可用，系统自动降级为全量注入 `MEMORY.md`（受 token 预算限制），并在后台触发索引重建。

---

## 4. 哪些文件始终 / 按需读取

MiniUnicorn 的记忆注入是分层的，不同文件有不同的注入策略：

| 文件 | 注入策略 | 说明 |
|------|---------|------|
| `SOUL.md` | **始终注入（有界）** | 人格语气始终进入系统提示，但受 `SOUL_TOKEN_BUDGET`（4000 token）上限截断，防止过长。 |
| `USER.md` 的 `# Always` 段 | **始终注入（有界）** | 用户画像中标记为 `# Always` 的段落始终进入上下文，受 core token 预算限制。 |
| `memory/MEMORY.md` 的 `# Always` 段 | **始终注入（有界）** | 项目事实中标记为 `# Always` 的段落始终进入上下文。 |
| `USER.md` 的其余段落 | **按需召回** | 非 Always 段落被向量化存入索引，仅在语义相关时召回。 |
| `memory/MEMORY.md` 的其余段落 | **按需召回** | 同上。 |
| `memory/history.jsonl` | **按需召回** | 历史摘要全部向量化存入索引，按语义相关性召回。 |

设计原则：**Always 核心固定注入**（保证人格一致性和关键事实始终可见），**其余内容按需召回**（只在与当前对话相关时进入上下文，节省 token）。SOUL 始终发送但有界，避免无限膨胀。

---

## 5. 查看和修复

MiniUnicorn 提供四个 CLI 命令和 WebUI 四个状态卡片来查看和修复向量记忆。

### CLI 命令

```bash
# 查看状态（只读，永不报错退出）
miniunicorn embedding status
miniunicorn embedding status --json          # 机器可读 JSON 输出
miniunicorn embedding status -w /path/to/ws  # 指定工作区

# 下载并校验模型（首次安装或下载失败后重试）
miniunicorn embedding setup
miniunicorn embedding setup --force          # 强制重新下载

# 验证：模型自测 + 索引一致性检查
miniunicorn embedding verify

# 重建索引：验证模型后，删除旧索引并重新扫描全部源文件
miniunicorn embedding rebuild
```

| 命令 | 作用 | 退出码 |
|------|------|--------|
| `status` | 只读快照：模型状态、索引状态、来源同步、实际检索 | 始终 0 |
| `setup` | 下载、SHA-256 校验、自测模型 | 成功 0，失败 1 |
| `verify` | 模型自测 + 现有索引一致性验证 | 成功 0，失败 1 |
| `rebuild` | 验证模型 → 原子重建索引 | 成功 0，失败 1 |

### WebUI 四个卡片

在 WebUI 的设置页面中，向量记忆状态以四个卡片展示：

1. **模型**：显示模型状态（`ready` / `not_downloaded` / `failed` 等）、模型 ID、维度、占用字节数、最近错误码。
2. **索引**：显示索引状态（`ready` / `missing` / `stale` / `corrupt` 等）、占用字节数、最近错误码。
3. **来源同步**：显示已索引 / 已发现的来源数量（如 `12/12 indexed`）。
4. **实际检索**：显示检索是否活跃（`active`）或不活跃原因（`disabled` / `index_missing` / `model_not_ready` 等）。

WebUI 中的操作按钮对应 CLI 命令：可以触发 setup、verify、rebuild，并轮询操作进度。

---

## 6. 更新记忆

记忆更新有两种途径：**用户显式触发**和**Dream 自动整合**。

### 显式触发

用户在对话中使用触发词让 MiniUnicorn 记住某件事。触发词包括：

- 中文：`记住`、`请记住`、`/记住`、`/remember`
- 英文：`remember that`、`please remember`、`/remember`

当触发词出现时，`ExplicitMemoryService` 会判断新内容与现有记忆的关系：

| 关系 | 含义 | 处理方式 |
|------|------|---------|
| **duplicate**（重复） | 新内容与已有记忆语义相同 | 提示用户已存在，可选择保留或覆盖 |
| **supplement**（补充） | 新内容是对已有记忆的补充 | 追加为新 revision，关联到原记忆 |
| **conflict**（冲突） | 新内容与已有记忆矛盾 | 提示冲突，让用户选择保留原记忆或更新 |
| **unrelated**（无关） | 新内容与已有记忆无关 | 作为新记忆独立保存 |

### 版本保留

每次"记住"都是**追加操作**（append-only），从不覆盖或删除旧内容：

- 显式记忆存储在 `memory/explicit.jsonl`，每行一条 JSON，包含 `memory_id`、`revision`、`supersedes_revision`。
- 更新一条记忆时，旧 revision **不会被删除**——系统追加一个新 revision，并标记它取代了哪个旧 revision。
- 你可以查看任意历史版本，也可以回滚到任意旧 revision。
- 这保证了记忆的可审计性：每一次变更都有迹可循，误操作也能恢复。

Dream 整合（`/dream` 或定时触发）则是对 `SOUL.md`、`USER.md`、`MEMORY.md` 的外科手术式编辑，每次变更通过 GitStore 版本化，可通过 `/dream-log` 查看 diff、`/dream-restore` 回滚。

---

## 7. 关闭和回滚

如果你不想使用向量记忆（例如在资源受限的设备上），可以通过配置关闭它。

### 关闭向量记忆

在配置文件（`~/.miniunicorn/config.json`）中设置：

```json
{
  "agents": {
    "defaults": {
      "vectorRecall": false
    }
  }
}
```

关闭后的效果：

- **不修改任何源文件**：`SOUL.md`、`USER.md`、`MEMORY.md` 等文件原封不动。
- **不再查询索引**：AgentLoop 不再在 LLM 调用前做向量检索，改为全量注入 `MEMORY.md`（受 token 预算限制）。
- **`memory.db` 不会被删除**：索引文件保留在磁盘上，重新启用后可立即使用。
- **`miniunicorn onboard` 会跳过**：关闭后 onboard 不再尝试下载或设置模型，直接显示"向量记忆已按配置关闭"。

### 重新启用

把 `vectorRecall` 改回 `true`（或删除该字段，默认为 `true`）即可。如果索引已存在且模型就绪，立即恢复语义召回；否则系统会自动触发设置和重建。

### 删除索引

如果你想彻底清除索引数据：

```bash
rm workspace/memory/memory.db
```

下次对话时系统会发现索引缺失，自动降级为全量注入，并在后台重建。**这不会丢失任何记忆**——所有原始内容都在 Markdown 源文件中。

---

## 8. 隐私

向量记忆的设计以**本地优先**和**最小暴露**为核心原则：

- **CPU 本地 Embedding**：向量化（embedding）完全在本地 CPU 上执行，使用 ONNX Runtime 加速。你的记忆文本不会发送到任何 Embedding API。
- **固定模型版本**：模型为 `BAAI/bge-small-zh-v1.5`（512 维），revision 固定为 `7999e1d3359715c523056ef9478215996d62a620`。下载后做 SHA-256 逐文件校验，防止模型被篡改。模型文件缓存在本地，不会自动更新。
- **只有召回的小片段进入聊天 LLM**：向量检索是本地操作，只有最终召回的少量片段（默认最多 5 条）会进入 LLM 上下文。你的完整记忆文件不会被整体上传给 LLM 提供商。
- **索引不上传**：`memory.db` 是本地 SQLite 文件，永远不会离开你的机器。
- **可审计**：所有记忆源文件都是人可读的 Markdown，索引可随时删除重建，显式记忆有完整的 revision 历史。
