# MiniUnicorn Main 无向量记忆设计

日期：2026-08-10
状态：已批准，待制定实施计划

## 1. 背景与结论

MiniUnicorn 采用三条产品线：

- `main`：不包含 embedding 或向量召回能力，只提供结构化记忆。
- `origin/codex/embedding-no-worker-latest`：完整的 embedding 增强版本。
- `origin/codex/embedding-memory-production`：embedding 加 3-worker 的后续增强版本，暂时冻结，留待业务量增长后继续完善。

本轮只清理 `main`。不得 rebase、删除、重写或强推两个增强分支，也不把两个增强分支的提交重新移植到 `main`。

`main` 的目标不是“默认关闭向量能力”，而是完全不存在该能力及其预留接口。Git 历史中曾出现相关实现不属于违规；验收对象是清理完成后的当前树和发布产物。

## 2. 设计原则

1. **产品边界明确**：需要向量召回时直接使用 embedding 分支，不在 `main` 保留兼容层或扩展钩子。
2. **结构化记忆优先**：继续维护人类可读、可编辑、可审计的长期记忆和行为记忆。
3. **旧数据无损但完全隔离**：不删除用户已有的 `memory.db`，同时绝不读取、创建、迁移或更新它。
4. **拒绝模糊兼容**：旧向量配置必须得到明确的“不支持”错误，不能被静默接受或转换成空操作。
5. **发布物与源码一致**：源码、配置、依赖、测试、用户界面、产品文档和发布包表达同一个无向量产品边界。

## 3. 保留的记忆能力

`main` 保留以下结构化记忆能力：

- `SOUL.md`、`USER.md`、`MEMORY.md`
- `history.jsonl`
- Consolidator 与 Dream
- Reflection
- episodic、procedural、shared memory
- notes 与 scratchpad
- GitStore 的审计与恢复能力

模型配置中诸如 `max_position_embeddings` 的上下文窗口元数据不是记忆 embedding，继续保留。

## 4. 必须删除的内容

### 4.1 运行代码

从 `main` 删除：

- `miniunicorn/agent/vector_memory.py`
- `miniunicorn/agent/tools/recall.py`
- `miniunicorn/providers/embedding.py`
- `LLMProvider.embed()` 及 OpenAI-compatible provider 中的 embedding 实现
- `MemoryStore` 的向量字段、向量方法和 `index_text()`
- `AgentLoop`、`ContextBuilder`、`AgentLoopBuilder` 中的向量参数、初始化和分支
- MCP lifecycle 与 tool context 中的向量参数和生命周期逻辑
- CLI、API、WebUI 中的向量召回或 embedding 入口

不得用 NoOp vector store、兼容导入、空方法、保留参数、feature flag 或抽象接口代替删除。

### 4.2 配置

删除以下配置表面及其内部传递路径：

- `vectorRecall`
- agent/provider 的 `embeddingModel`
- `embeddingProvider`
- `embeddingApiBase`
- `embeddingApiKey`

配置示例、schema、序列化、环境变量映射、命令行参数和前端表单必须同步清理。

### 4.3 依赖、测试与文档

- 删除 `[vector]` optional dependency 和 `sqlite-vec`。
- 删除只验证 embedding、向量写入、向量召回或兼容预留的测试与 fixtures。
- 更新仍然有效的集成测试，使其只验证结构化记忆链路。
- 删除 README、用户文档和界面中对向量召回支持的声明。
- 删除只服务于 embedding 实现的设计、计划和验证文档。

本规格用于记录清理决策，因此在设计与实施期间可以出现相关术语。实施完成时，本规格也应从 `main` 当前树中删除；其提交记录保留在 Git 历史中。

## 5. 清理后的数据流

结构化记忆的数据流为：

```text
对话与工具结果
    -> history.jsonl
    -> Consolidator / Reflection / Dream
    -> SOUL.md / USER.md / MEMORY.md
       episodic / procedural / shared memory
       notes / scratchpad
    -> ContextBuilder 按结构和规则装配上下文
    -> AgentLoop
```

约束如下：

- 数据进入 `history.jsonl` 后，由现有结构化记忆组件提炼和维护。
- `ContextBuilder` 只读取结构化记忆与近期历史，不生成查询向量，也不执行相似度检索。
- `AgentLoop` 不初始化向量存储或 embedding provider。
- GitStore 继续跟踪记忆文件变化，提供审计、版本追踪与恢复。
- Dream、Reflection、Consolidator 的非向量行为保持不变。

## 6. 旧配置与旧数据行为

### 6.1 旧配置

旧配置中出现已删除字段时，应由项目现有的严格未知字段校验立即拒绝。错误至少要指出具体字段不被当前 schema 接受，但运行时代码不得为了这些旧字段保留专用识别表、迁移器或分支提示。这样既不会静默接受旧配置，也不会形成兼容预留。

配置处理不得：

- 静默忽略旧字段；
- 自动转换为其他设置；
- 提供在 `main` 中重新启用的开关；
- 暗示 `main` 内部仍保留可用实现；
- 为旧字段枚举专用错误文案或增强分支路由。

如果当前 schema 不是严格模式，实施时应统一收紧未知字段校验，而不是为被删除字段增加例外。增强分支的选择属于版本文档和分支管理范围，不进入 `main` 的运行时配置接口。

### 6.2 旧数据库

- 用户磁盘上已有的 `memory.db` 保持原样，不删除、不改名、不迁移。
- `main` 启动、对话和记忆整理过程都不访问该文件。
- 遗留 `memory.db` 的存在不应导致启动失败；它被视为与 `main` 无关的普通文件。
- 全新工作区以及不存在该文件的旧工作区均不得创建 `memory.db`。

## 7. 测试与验证

### 7.1 静态边界检查

- 当前运行代码不导入已删除的 embedding/vector 模块。
- schema、示例配置、CLI、API 和 WebUI 不暴露已删除配置。
- 项目依赖和 lock/构建元数据不包含 `sqlite-vec`。
- README 和用户文档不宣称支持向量召回。
- 不存在 NoOp 实现、兼容导入或未来接口预留。

静态文本检查必须区分功能术语与模型元数据，不能误删 `max_position_embeddings` 等正常字段。

### 7.2 行为测试

至少覆盖：

- AgentLoop 与 ContextBuilder 在无向量参数情况下正常工作；
- 结构化记忆读取、写入和上下文装配；
- Consolidator、Dream 与 Reflection；
- episodic、procedural、shared memory；
- notes、scratchpad 与 GitStore 审计/恢复；
- 任意未知配置字段都会被严格校验拒绝，旧配置字段不享有例外；
- 存在遗留 `memory.db` 时系统不读取、不修改且可正常启动；
- 全新工作区启动、对话、整理和退出后均未生成 `memory.db`。

### 7.3 发布物验证

构建 wheel 和 sdist，检查：

- 包内不存在已删除模块；
- 依赖元数据中不存在 `[vector]` 或 `sqlite-vec`；
- 默认配置和随包文档不包含相关产品入口或支持声明；
- 从发布包安装后执行最小启动与结构化记忆 smoke test，不生成 `memory.db`。

## 8. 验收标准

只有同时满足以下条件，本轮清理才算完成：

1. `main` 运行时没有 embedding/vector 模块、调用链或预留接口。
2. `main` 配置表面没有 embedding/vector 设置。
3. `main` 依赖与发布包不包含 `sqlite-vec`。
4. `main` 在任何正常生命周期中都不创建、读取或迁移 `memory.db`。
5. `main` 的产品文档与界面不宣称或暗示向量召回能力。
6. 结构化记忆、Dream、Reflection 及 GitStore 的相关测试继续通过。
7. 两个增强分支的引用与提交历史保持不变。

## 9. 非目标

本轮不做以下工作：

- 不修改或完善 embedding 增强分支。
- 不修改 3-worker 版本。
- 不设计新的向量接口或重新预留扩展点。
- 不迁移或删除用户的历史 `memory.db`。
- 不清理 Git 历史中已有的 embedding 提交与文字。
- 不进行与记忆边界无关的重构。
