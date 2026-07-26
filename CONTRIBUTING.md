# Contributing to MiniUnicorn

感谢你来到这里。

MiniUnicorn 基于一个简单的理念：好的工具应当平静、清晰、人性化。我们重视有用的功能，但也相信"以少胜多"——
方案应当强大而不沉重，有野心而不无谓复杂。

这份指南不仅是关于如何提交 PR，也是关于我们希望如何一起写代码：
带着关心、清晰，以及对下一位读者的尊重。

## 仓库信息

- 仓库：<https://github.com/zhoulingquan/miniunicorn>
- 许可证：MIT
- 主分支：`main`（稳定发布）

本仓库目前由单人维护，未引入 `nightly` 双分支模型。所有改动直接基于 `main` 进行。

## 开发环境

保持本地构建无聊且可靠，目标是让你尽快进入代码：

```bash
# 克隆仓库
git clone https://github.com/zhoulingquan/miniunicorn.git
cd miniunicorn

# 用 uv 安装依赖（推荐）
uv sync --all-extras

# 运行测试
uv run pytest tests/

# 代码检查（全规则）
uv run ruff check miniunicorn

# 格式检查
uv run ruff format --check miniunicorn
```

## 开发流程

1. 从最新 `main` 拉取并创建主题分支：

   ```bash
   git fetch origin
   git switch main
   git pull --ff-only origin main
   git switch -c your-topic-branch
   ```

2. 保持主题分支聚焦，避免在同一分支中混入无关改动。

3. 提交前确保以下检查通过：

   ```bash
   uv run ruff check miniunicorn
   uv run ruff format --check miniunicorn
   uv run pytest tests/
   ```

4. 提交 PR 到 `main`，并在描述中说明改动的动机与影响范围。

## 代码风格

我们关心的不仅是通过 lint。我们希望 MiniUnicorn 保持小巧、平静、可读。

贡献代码时，请追求以下特质：

- **简单**：用最小的改动解决真实问题
- **清晰**：为下一位读者优化，而非炫技
- **解耦**：保持边界清晰，避免不必要的新抽象
- **诚实**：不隐藏复杂度，也不制造额外复杂度
- **耐用**：选择易于维护、测试和扩展的方案

实践约定：

- 行宽：100 字符（`ruff`）
- 目标版本：Python 3.11+
- Lint：`ruff`，启用 E / F / I / N / W 规则（E501 忽略）
- 异步：全程使用 `asyncio`；pytest 使用 `asyncio_mode = "auto"`
- 优先写可读代码，而非"巧妙"的代码
- 优先提交聚焦的补丁，而非大范围重写
- 如引入新抽象，应明确减少复杂度，而非只是把复杂度搬家

## 修改 CI 工作流

如果 PR 涉及 [.github/workflows/](./.github/workflows/)，请保持在 GitHub Actions 免费额度内：

- 仅使用标准 GitHub 托管 runner（`ubuntu-latest`、`macos-latest`、`windows-latest`）
- 避免大规格 runner（`*-cores`、`*-xlarge`、`*-gpu`）和自建 runner
- 避免上传大体积 artifact 或设置过长保留期
- 避免付费 Marketplace actions

如确实需要超出上述范围，请在 PR 描述中明确说明，以便合并前讨论。

## 贡献许可

提交贡献即表示你确认自己有权提交该内容，并同意按本项目的 MIT 许可证授权。

## 有问题？

欢迎打开 [Issue](https://github.com/zhoulingquan/miniunicorn/issues) 进行讨论。感谢你为 MiniUnicorn 投入的时间与用心。
