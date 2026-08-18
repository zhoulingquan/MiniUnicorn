"""零依赖的回调类型别名。

``ProgressCallback`` 定义在这里而不是 ``progress_events`` (后者 import
agent.hook, 会被 agent 包初始化链反向依赖形成环), 保证任何模块都能安全
先行导入。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

# 进度回调契约(有意保持宽松, 说明如下):
# - 第一参数为进度文本 (str);
# - 可选关键字参数按通道能力协商: tool_hint / tool_events / reasoning /
#   reasoning_end / file_edit_events, 兼容性在运行时通过签名检查判定
#   (见 progress_events._on_progress_accepts);
# - 通道实现可只声明自己关心的参数子集, 因此无法给出精确签名,
#   只能停留在 Callable[..., Awaitable[None]] (命名以固定契约入口)。
ProgressCallback = Callable[..., Awaitable[None]]
