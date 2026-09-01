"""W0-A2: ToolReceiptClaim — structured side-effect receipts.

A receipt may only exist when the tool's write actually landed on disk, and it
must never leak onto the model-visible or persisted event surfaces. These tests
lock both directions: the three contract tools emit, everything else does not.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from miniunicorn.agent.runner import AgentRunner, AgentRunSpec
from miniunicorn.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from miniunicorn.tools.apply_patch import ApplyPatchTool
from miniunicorn.tools.base import Tool
from miniunicorn.tools.filesystem import EditFileTool, WriteFileTool
from miniunicorn.tools.receipts import (
    ToolReceiptClaim,
    content_digest,
    emit_receipt,
    take_receipt,
)
from miniunicorn.tools.registry import ToolRegistry

_MAX_RESULT_CHARS = 10000


class _FakeTool(Tool):
    """Successful tool that performs no side effect — must not emit a receipt."""

    @property
    def name(self) -> str:
        return "fake_ok"

    @property
    def description(self) -> str:
        return "Always succeeds without touching the filesystem"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str:
        return "ok"


def _registry(*tools: Tool) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


def _call(name: str, arguments: dict[str, Any] | None = None) -> ToolCallRequest:
    return ToolCallRequest(id=f"call_{name}", name=name, arguments=arguments or {})


async def _execute(
    tools: ToolRegistry,
    calls: list[ToolCallRequest],
    *,
    concurrent: bool = False,
) -> tuple[list[Any], list[dict[str, Any]], list[Any]]:
    """Run the calls through the real execution path and build observations."""
    runner = AgentRunner(MagicMock(spec=LLMProvider))
    spec = AgentRunSpec(
        initial_messages=[],
        tools=tools,
        model="test-model",
        max_iterations=1,
        max_tool_result_chars=_MAX_RESULT_CHARS,
        concurrent_tools=concurrent,
    )
    results, events, _fatal = await runner.execute_tools(spec, calls, {}, {})
    observations = runner.tool_execution.build_observations(calls, results, events)
    return observations, events, results


def _disk_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _has_receipt_key(node: Any) -> bool:
    """True when any nested mapping carries a ``receipt`` key."""
    if isinstance(node, dict):
        return "receipt" in node or any(_has_receipt_key(v) for v in node.values())
    if isinstance(node, list):
        return any(_has_receipt_key(v) for v in node)
    return False


# --- 1: write_file success carries a receipt matching the on-disk bytes -----


@pytest.mark.asyncio
async def test_write_file_emits_receipt_with_disk_digest(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    observations, _events, results = await _execute(
        _registry(WriteFileTool(workspace=tmp_path)),
        [_call("write_file", {"path": "a.txt", "content": "hello world"})],
    )

    assert not results[0].startswith("Error:")
    receipt = observations[0].receipt
    assert receipt is not None
    assert receipt["tool"] == "write_file"
    assert receipt["operation"] == "write"
    assert receipt["target"] == str(target)
    assert receipt["committed"] is True
    # Digest is computed from what actually landed on disk.
    assert receipt["digest"] == _disk_digest(target)
    assert receipt["digest"] == content_digest("hello world")


# --- 2: write_file failure leaves no receipt --------------------------------


@pytest.mark.asyncio
async def test_write_file_failure_emits_no_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_write_text = Path.write_text

    def _denied(self: Path, *args: Any, **kwargs: Any) -> int:
        raise PermissionError("read-only filesystem")

    monkeypatch.setattr(Path, "write_text", _denied)
    try:
        observations, _events, results = await _execute(
            _registry(WriteFileTool(workspace=tmp_path)),
            [_call("write_file", {"path": "a.txt", "content": "hello"})],
        )
    finally:
        monkeypatch.setattr(Path, "write_text", real_write_text)

    assert results[0].startswith("Error:")
    assert observations[0].receipt is None


# --- 3: every edit_file success path emits ----------------------------------


@pytest.mark.parametrize(
    "case",
    ["create", "overwrite_empty", "replace"],
)
@pytest.mark.asyncio
async def test_edit_file_success_paths_emit_receipt(tmp_path: Path, case: str) -> None:
    target = tmp_path / "e.txt"
    if case == "overwrite_empty":
        target.write_text("", encoding="utf-8")
        arguments = {"path": "e.txt", "old_text": "", "new_text": "brand new"}
    elif case == "replace":
        target.write_text("alpha beta", encoding="utf-8")
        arguments = {"path": "e.txt", "old_text": "beta", "new_text": "GAMMA"}
    else:
        arguments = {"path": "e.txt", "old_text": "", "new_text": "brand new"}

    observations, _events, results = await _execute(
        _registry(EditFileTool(workspace=tmp_path)),
        [_call("edit_file", arguments)],
    )

    assert not results[0].startswith("Error:"), results[0]
    receipt = observations[0].receipt
    assert receipt is not None
    assert receipt["tool"] == "edit_file"
    assert receipt["operation"] == "edit"
    assert receipt["target"] == str(target)
    assert receipt["digest"] == _disk_digest(target)


# --- 4: apply_patch commit emits one multi-file receipt ---------------------


@pytest.mark.asyncio
async def test_apply_patch_emits_multi_file_receipt(tmp_path: Path) -> None:
    edits = [
        {"action": "add", "path": "a.txt", "new_text": "aaa\n"},
        {"action": "add", "path": "b.txt", "new_text": "bbb\n"},
    ]

    observations, _events, results = await _execute(
        _registry(ApplyPatchTool(workspace=tmp_path)),
        [_call("apply_patch", {"edits": edits})],
    )

    assert results[0].startswith("Patch applied:")
    receipt = observations[0].receipt
    assert receipt is not None
    assert receipt["tool"] == "apply_patch"
    assert receipt["operation"] == "patch"
    assert receipt["digest"] is None
    assert len(receipt["files"]) == 2
    by_path = {entry["path"]: entry for entry in receipt["files"]}
    assert set(by_path) == {str(tmp_path / "a.txt"), str(tmp_path / "b.txt")}
    for name, text in (("a.txt", "aaa\n"), ("b.txt", "bbb\n")):
        entry = by_path[str(tmp_path / name)]
        assert entry["digest"] == _disk_digest(tmp_path / name)
        assert entry["digest"] == content_digest(text)
        assert entry["added"] == 1
        assert entry["deleted"] == 0


# --- 5: apply_patch dry_run must not emit (anti-forgery) --------------------


@pytest.mark.asyncio
async def test_apply_patch_dry_run_emits_no_receipt(tmp_path: Path) -> None:
    edits = [{"action": "add", "path": "a.txt", "new_text": "aaa\n"}]

    observations, _events, results = await _execute(
        _registry(ApplyPatchTool(workspace=tmp_path)),
        [_call("apply_patch", {"edits": edits, "dry_run": True})],
    )

    assert results[0].startswith("Patch dry-run succeeded:")
    assert not (tmp_path / "a.txt").exists()
    assert observations[0].receipt is None


# --- 6: apply_patch rollback emits no receipt and restores backups ----------


@pytest.mark.asyncio
async def test_apply_patch_rollback_emits_no_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = tmp_path / "a.txt"
    existing.write_text("original\n", encoding="utf-8")

    real_write_text = Path.write_text
    attempts = {"n": 0}

    def _fail_second(self: Path, *args: Any, **kwargs: Any) -> int:
        attempts["n"] += 1
        if attempts["n"] == 2:
            raise OSError("disk full")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", _fail_second)
    try:
        observations, _events, results = await _execute(
            _registry(ApplyPatchTool(workspace=tmp_path)),
            [
                _call(
                    "apply_patch",
                    {
                        "edits": [
                            {"action": "add", "path": "new.txt", "new_text": "new\n"},
                            {
                                "action": "replace",
                                "path": "a.txt",
                                "old_text": "original",
                                "new_text": "changed",
                            },
                        ]
                    },
                )
            ],
        )
    finally:
        monkeypatch.setattr(Path, "write_text", real_write_text)

    assert results[0].startswith("Error applying patch:")
    assert observations[0].receipt is None
    # Rollback: the pre-existing file is restored, the new one is removed.
    assert existing.read_text(encoding="utf-8") == "original\n"
    assert not (tmp_path / "new.txt").exists()


# --- 7: concurrent batch keeps receipts isolated per task -------------------


@pytest.mark.asyncio
async def test_concurrent_batch_receipts_do_not_cross_contaminate(tmp_path: Path) -> None:
    observations, _events, _results = await _execute(
        _registry(WriteFileTool(workspace=tmp_path)),
        [
            _call("write_file", {"path": "one.txt", "content": "first"}),
            _call("write_file", {"path": "two.txt", "content": "second"}),
        ],
        concurrent=True,
    )

    receipts = [o.receipt for o in observations]
    assert all(receipt is not None for receipt in receipts)
    targets = {receipt["target"] for receipt in receipts}
    assert targets == {str(tmp_path / "one.txt"), str(tmp_path / "two.txt")}
    digests = {receipt["digest"] for receipt in receipts}
    assert digests == {content_digest("first"), content_digest("second")}


# --- 8: receipt never reaches the persisted / model-visible event surface ---


@pytest.mark.asyncio
async def test_receipt_absent_from_event_surfaces(tmp_path: Path) -> None:
    registry = _registry(WriteFileTool(workspace=tmp_path))
    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock(
        side_effect=[
            LLMResponse(
                content=None,
                tool_calls=[_call("write_file", {"path": "a.txt", "content": "hello"})],
                usage={},
            ),
            LLMResponse(content="all done", usage={}),
        ]
    )
    checkpoints: list[dict[str, Any]] = []

    async def _capture(payload: dict[str, Any]) -> None:
        checkpoints.append(payload)

    result = await AgentRunner(provider).run(
        AgentRunSpec(
            initial_messages=[{"role": "user", "content": "write something"}],
            tools=registry,
            model="test-model",
            max_iterations=3,
            max_tool_result_chars=_MAX_RESULT_CHARS,
            checkpoint_callback=_capture,
        )
    )

    assert result.tool_events, "expected the run to record a tool event"
    assert all("receipt" not in event for event in result.tool_events)
    # 按"键"检查而非子串：临时目录名里可能恰好含 "receipt" 字样。
    assert not [_has_receipt_key(c) for c in checkpoints if _has_receipt_key(c)]

    # The observation keeps the receipt, the event itself is clean again.
    observations, events, _results = await _execute(
        registry, [_call("write_file", {"path": "b.txt", "content": "hello"})]
    )
    assert observations[0].receipt is not None
    assert "receipt" not in events[0]


# --- 9: take_receipt resets the contextvar ----------------------------------


def test_take_receipt_resets() -> None:
    emit_receipt(ToolReceiptClaim(tool="write_file", operation="write", target="/tmp/a"))

    assert take_receipt() is not None
    assert take_receipt() is None


# --- 10: non-contract tools never emit --------------------------------------


@pytest.mark.asyncio
async def test_non_contract_tool_emits_no_receipt() -> None:
    observations, _events, results = await _execute(_registry(_FakeTool()), [_call("fake_ok")])

    assert results[0] == "ok"
    assert observations[0].receipt is None
