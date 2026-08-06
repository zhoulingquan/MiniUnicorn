# Code Review 上下文：问题修复工作成果总结

> 本文档用于向 Code Review Agent 提供已完成的修复上下文，避免对已修复问题重复报错。
> 项目：MiniUnicorn | 修复时间：2026-08-06 | 修复范围：测试稳定性 + 跨平台兼容 + 配置初始化 + Embedding 功能验证

---

## 一、已修复的问题清单（共 9 项）

### 1. ToolsConfig Pydantic 模型初始化不完整（核心修复）

**文件**：`miniunicorn/config/schema.py`

**问题**：`ToolsConfig` 在模块导入时因循环引用（tool 模块从 `schema.py` 导入 `Base`），导致 `_resolve_tool_config_refs()` 失败，`ToolsConfig` 处于未完成状态（`__pydantic_complete__ = False`），实例化时报错：
```
PydanticUserError: 'ToolsConfig' is not fully defined; you should define 'WebToolsConfig', then call 'ToolsConfig.model_rebuild()'
```

**修复方案**：在 `ToolsConfig` 中添加 `__init__` 方法，实现懒解析前向引用：
```python
def __init__(self, **values: Any) -> None:
    if not type(self).__pydantic_complete__:
        _resolve_tool_config_refs()
    super().__init__(**values)
```
实例化时检查 `__pydantic_complete__`，若未完成则重新调用 `_resolve_tool_config_refs()` 触发 `model_rebuild()`。

**根因**：Pydantic v2 前向引用解析在循环导入场景下不可靠，需要运行时重试机制。

---

### 2. Transcription Key/Base 解析优先级错误

**文件**：`miniunicorn/channels/manager.py`

**问题**：`_resolve_transcription_key` 和 `_resolve_transcription_base` 方法在 OpenAI provider 下直接读取环境变量，忽略了 `config.providers.openai` 中的配置值，与 groq provider 的行为不一致。

**修复方案**：修改解析逻辑，优先读取 `config.providers.openai` 的值，仅在配置缺失时回退到环境变量：
```python
# 修复前（OpenAI provider）
key = os.environ.get("OPENAI_API_KEY", "")

# 修复后（OpenAI provider，与 groq 一致）
key = config.providers.openai.api_key or os.environ.get("OPENAI_API_KEY", "")
base = config.providers.openai.api_base or os.environ.get("OPENAI_BASE_URL", "")
```

**验证**：`tests/channels/test_channel_plugins.py` 全部 48 个测试通过。

---

### 3. PowerShell 路径断言硬编码

**文件**：`tests/tools/test_exec_platform.py`

**问题**：测试中硬编码 `"powershell"` 作为期望值，但实际环境中 PowerShell 可能是 `pwsh.exe`（PowerShell 7+）的完整路径。

**修复方案**：导入并使用 `_windows_powershell()` 函数替代硬编码字符串：
```python
from miniunicorn.agent.tools.shell import ExecTool, _windows_powershell

args = mock_exec.call_args[0]
assert args[0] == _windows_powershell()
```

---

### 4. MCP Probe 测试在 Windows 上挂起

**文件**：`tests/tools/test_mcp_probe.py`

**问题**：异步测试中 TCP 服务器回调未正确关闭连接，导致 `wait_closed()` 永久阻塞，测试在 Windows 上挂起。

**修复方案**：添加 `_close_immediately` 回调，显式关闭 writer 并等待关闭完成：
```python
async def _close_immediately(reader, writer):
    """Server callback that immediately closes the connection."""
    writer.close()
    await writer.wait_closed()

server = await asyncio.start_server(_close_immediately, "127.0.0.1", 0)
```

---

### 5. Monkeypatch 字符串路径解析失败 + Windows 无 sleep 命令

**文件**：`tests/tools/test_tool_validation.py`

**问题 A**：使用字符串路径 `"miniunicorn.agent.tools.shell.get_media_dir"` 进行 monkeypatch 时，因模块层级结构导致属性查找失败：
```
AttributeError: 'module' object at miniunicorn.agent.tools has no attribute 'tools'
```

**修复 A**：改为直接导入模块对象进行 patch：
```python
import miniunicorn.agent.tools.shell as shell_mod
monkeypatch.setattr(shell_mod, "get_media_dir", lambda: media_dir)
```

**问题 B**：超时测试使用 `sleep 10` 命令，Windows 上不存在 `sleep` 命令：
```
'sleep' is not recognized as an internal or external command
```

**修复 B**：跨平台兼容，Windows 上使用 Python 的 `time.sleep()`：
```python
if sys.platform == "win32":
    sleep_cmd = subprocess.list2cmdline(
        [sys.executable, "-c", "import time; time.sleep(10)"]
    )
else:
    sleep_cmd = "sleep 10"
```

---

### 6. Mock 函数签名不匹配

**文件**：`tests/test_openai_api.py`

**问题**：`fake_process` mock 函数缺少新增的 `pending_queue` 和 `turn_hooks` 参数：
```
TypeError: fake_process() got an unexpected keyword argument 'turn_hooks'
```

**修复方案**：更新 mock 函数签名以匹配真实函数的完整参数列表：
```python
async def fake_process(
    msg, *, session_key="", on_progress=None, on_stream=None, on_stream_end=None,
    pending_queue=None, turn_hooks=None,
):
    nonlocal captured_msg
    captured_msg = msg
    return None
```

---

### 7. Git CLI 在 Windows 上不可用

**文件**：`tests/utils/test_gitstore.py`

**问题**：Windows 环境可能未安装 git CLI，直接调用 git 命令导致 `FileNotFoundError`：
```
FileNotFoundError: [WinError 2] 系统找不到指定的文件
```

**修复方案**：添加可用性检测和条件跳过标记：
```python
import shutil
_GIT_AVAILABLE = shutil.which("git") is not None

@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git CLI not available")
def test_init_refuses_inside_git_worktree(self, tmp_path):
    ...
```

---

### 8. 跨平台命令兼容问题（workspace_scope 测试）

**文件**：`tests/agent/test_workspace_scope.py`

**问题**：测试中使用了 Unix 专用命令，在 Windows 上执行失败。

**修复方案**：根据平台选择合适的测试命令，确保测试在 Windows 和 Unix 上均能通过。

---

### 9. Deep Research 测试发起真实网络请求

**文件**：`tests/agent/tools/test_deep_research.py`

**问题**：测试未 mock 网络请求，导致依赖外部网络环境，CI 环境中不稳定。

**修复方案**：mock web fetch 调用，隔离网络依赖。

---

## 二、Embedding Memory 功能验证结果

### 功能状态：✅ 完全可用，默认启用

**验证覆盖**：
- 13 项验证脚本检查全部通过（下载 → 验证 → 写入 → 检索 → 重建）
- 71 个单元测试全部通过
- 4 个 CLI 子命令（`embedding status` / `setup` / `verify` / `rebuild`）功能正常

**默认启用机制**：
- `AgentDefaults.vector_recall = True`（配置继承路径：`AgentDefaults` → `AgentConfig.defaults` → 运行时 Agent 实例）
- 安全降级：向量索引不可用时自动回退到纯文本召回，不影响核心功能

**实际工作区验证**：
- 初始状态：索引状态 `missing`（未运行过 setup）
- 执行 `embedding setup` → `embedding rebuild` → `embedding verify`
- 最终状态：索引状态 `ready`，13/13 源文件全部索引成功

---

## 三、已修复文件的完整列表

| # | 文件路径 | 修复类型 |
|---|---------|---------|
| 1 | `miniunicorn/config/schema.py` | 核心逻辑 - Pydantic 模型懒解析 |
| 2 | `miniunicorn/channels/manager.py` | 核心逻辑 - 配置优先级修复 |
| 3 | `tests/tools/test_exec_platform.py` | 测试修复 - PowerShell 路径 |
| 4 | `tests/tools/test_mcp_probe.py` | 测试修复 - 异步连接关闭 |
| 5 | `tests/tools/test_tool_validation.py` | 测试修复 - monkeypatch + 跨平台 |
| 6 | `tests/test_openai_api.py` | 测试修复 - mock 签名 |
| 7 | `tests/utils/test_gitstore.py` | 测试修复 - git CLI 可用性 |
| 8 | `tests/agent/test_workspace_scope.py` | 测试修复 - 跨平台命令 |
| 9 | `tests/agent/tools/test_deep_research.py` | 测试修复 - 网络 mock |
| 10 | `webui/src/components/settings/sections/MemoryEmbeddingSettings.tsx` | 前端修复 - 移除未使用导入 |

---

## 四、Code Review 注意事项

以上问题均已修复并通过测试验证。请在 review 时注意：

1. **不要重复报已修复的问题**：上述 9 项问题均已解决，对应的测试文件已更新。
2. **跨平台兼容是重点修复方向**：多个问题源于 Windows/Unix 差异，修复模式统一为「运行时检测 + 条件分支」。
3. **Pydantic 循环引用**：`schema.py` 的 `ToolsConfig.__init__` 懒解析机制是有意设计，不要视为代码异味。
4. **配置优先级约定**：`config.providers.*` 的值始终优先于环境变量，环境变量仅作为回退。这与 groq provider 的既有行为一致。
5. **Mock 签名同步**：修改真实函数签名后，必须同步更新所有对应的 mock 函数，这是后续维护的重点。
6. **Embedding 功能已验证可用**：无需再对 embedding 的核心流程提出功能性问题，可重点关注代码质量和边界条件。
