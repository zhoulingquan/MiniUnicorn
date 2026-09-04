"""工具副作用回执：由工具代码在副作用真实完成后创建。

回执是**白名单制**，只发给能证明"写入确实落盘"的契约工具：
``write_file``、``edit_file``、``apply_patch``。

明确不产生回执的工具（含但不限于）：``shell``、``read_file``、
``list_files``、一切 MCP 工具。原因是它们的输出可被模型文本诱导（echo 关键词、
搜索结果里恰好含目标词），不构成副作用证明。

传递通道用 contextvar：并发批次下每个 ``run_tool`` 协程被包成独立 Task，Task 拷贝
上下文，工具 execute 内的 set 只在本 Task 可见，因此读取必须发生在 ``run_tool``
内部（与工具执行同 Task）。这与 ``FileStates`` / ``current_file_states`` 的既有模式
同构。
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any


def content_digest(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class ToolReceiptClaim:
    """工具在副作用真实落盘后出具的回执。"""

    tool: str
    operation: str
    target: str
    committed: bool = True
    digest: str | None = None
    files: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=_utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "tool": self.tool,
            "operation": self.operation,
            "target": self.target,
            "committed": self.committed,
            # digest 恒输出：apply_patch 的多文件回执为 None，消费方需要能
            # 区分"没有单文件摘要"与"键缺失"。
            "digest": self.digest,
            "created_at": self.created_at,
        }
        if self.files:
            data["files"] = list(self.files)
        return data


_current_receipt: ContextVar[ToolReceiptClaim | None] = ContextVar(
    "current_tool_receipt", default=None
)


def emit_receipt(claim: ToolReceiptClaim) -> None:
    """工具代码在副作用完成后调用。同一次工具调用内重复 emit 保留最后一条。"""
    _current_receipt.set(claim)


def take_receipt() -> ToolReceiptClaim | None:
    """run_tool 在工具执行返回后调用：读取并复位。"""
    claim = _current_receipt.get()
    _current_receipt.set(None)
    return claim
