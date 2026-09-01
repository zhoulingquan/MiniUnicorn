"""F-001: Pre-execution approval gate for HIGH-risk tools.

Tests that ``ToolExecutionCoordinator.run_tool()`` enforces the three-state
approval model before any HIGH-risk tool runs:

- policy="allow" (default): executes, emitting a ``tool_started`` audit
  checkpoint *before* ``tool_completed``;
- policy="deny": static fail-closed refusal, even when an approving
  callback is present;
- ``approval_callback`` set: HIGH tools must be approved (True) or they
  are blocked before execution; callback errors are treated as denial
  (fail-closed). LOW-risk tools never consult the callback and never
  emit ``tool_started`` (``requires_checkpoint`` is False).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from miniunicorn.agent.execution.tool_execution import ToolExecutionCoordinator
from miniunicorn.agent.runner import AgentRunner, AgentRunSpec
from miniunicorn.agent.safety_policy import RiskLevel
from miniunicorn.providers.base import LLMProvider, ToolCallRequest


class HighRiskFakeTool:
    """HIGH 风险假工具: 记录执行次数。"""

    name = "fake_high"
    concurrency_safe = False

    def __init__(self) -> None:
        self.exec_count = 0

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.HIGH

    async def execute(self, **kwargs: Any) -> str:
        self.exec_count += 1
        return "high-ok"


class LowRiskFakeTool(HighRiskFakeTool):
    name = "fake_low"

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.LOW

    async def execute(self, **kwargs: Any) -> str:
        self.exec_count += 1
        return "low-ok"


class FakeRegistry:
    """Minimal registry stub: ``get`` for safety, async ``execute`` to run."""

    def __init__(self, *tools: Any) -> None:
        self._tools = {t.name: t for t in tools}

    def get(self, name: str) -> Any:
        return self._tools.get(name)

    async def execute(self, name: str, params: dict[str, Any]) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            return f"Error: unknown tool '{name}'"
        return await tool.execute(**(params or {}))


def _spec(
    registry: FakeRegistry,
    checkpoints: list[dict[str, Any]] | None = None,
    **overrides: Any,
) -> AgentRunSpec:
    return AgentRunSpec(
        initial_messages=[],
        tools=registry,
        model="test-model",
        max_iterations=2,
        max_tool_result_chars=1000,
        checkpoint_callback=_make_collector(checkpoints) if checkpoints is not None else None,
        **overrides,
    )


def _make_collector(checkpoints: list[dict[str, Any]]):
    async def _collect(payload: dict[str, Any]) -> None:
        checkpoints.append(payload)

    return _collect


def _tool_call(name: str = "fake_high") -> ToolCallRequest:
    return ToolCallRequest(id="call_abc", name=name, arguments={"target": "x"})


def _coordinator() -> ToolExecutionCoordinator:
    provider = MagicMock(spec=LLMProvider)
    runner = AgentRunner(provider)
    return ToolExecutionCoordinator(runner)


# 1. HIGH + 默认 policy("allow") + 无 callback → 执行成功;
#    tool_started 在 tool_completed 之前。


@pytest.mark.asyncio
async def test_high_risk_default_allow_runs_and_starts_before_completed() -> None:
    checkpoints: list[dict[str, Any]] = []
    tool = HighRiskFakeTool()
    spec = _spec(FakeRegistry(tool), checkpoints)

    coord = _coordinator()
    result, event, error = await coord.run_tool(spec, _tool_call(), {}, {})

    assert error is None
    assert event["status"] == "ok"
    assert result == "high-ok"
    assert tool.exec_count == 1
    phases = [c.get("phase") for c in checkpoints]
    assert "tool_started" in phases and "tool_completed" in phases
    assert phases.index("tool_started") < phases.index("tool_completed")
    started = checkpoints[phases.index("tool_started")]["tool_checkpoint"]
    assert started["status"] == "started"
    assert started["risk_level"] == "high"
    assert started["tool_name"] == "fake_high"


# 2. HIGH + approval_callback 返回 False → 不执行、error 结果、无 tool_completed。


@pytest.mark.asyncio
async def test_high_risk_callback_denied_blocks_execution() -> None:
    checkpoints: list[dict[str, Any]] = []
    tool = HighRiskFakeTool()
    seen: list[dict[str, Any]] = []

    async def deny(info: dict[str, Any]) -> bool:
        seen.append(info)
        return False

    spec = _spec(FakeRegistry(tool), checkpoints, approval_callback=deny)

    coord = _coordinator()
    result, event, error = await coord.run_tool(spec, _tool_call(), {}, {})

    assert tool.exec_count == 0
    assert error is None
    assert event["status"] == "error"
    assert "blocked before execution" in str(result)
    assert "approval_callback denied" in event["detail"]
    assert len(seen) == 1
    info = seen[0]
    assert info["tool_name"] == "fake_high"
    assert info["risk_level"] == "high"
    assert info["arguments"] == {"target": "x"}
    assert all(c.get("phase") != "tool_completed" for c in checkpoints)

    # 拒绝路径必须留审计痕迹（修复 3）。
    blocked = [c for c in checkpoints if c.get("phase") == "tool_blocked"]
    assert len(blocked) == 1
    blocked_cp = blocked[0]["tool_checkpoint"]
    assert blocked_cp["status"] == "blocked"
    assert blocked_cp["risk_level"] == "high"


# 3. HIGH + approval_callback 返回 True → 执行成功。


@pytest.mark.asyncio
async def test_high_risk_callback_approved_executes() -> None:
    checkpoints: list[dict[str, Any]] = []
    tool = HighRiskFakeTool()

    async def approve(info: dict[str, Any]) -> bool:
        return True

    spec = _spec(FakeRegistry(tool), checkpoints, approval_callback=approve)

    coord = _coordinator()
    result, event, error = await coord.run_tool(spec, _tool_call(), {}, {})

    assert error is None
    assert event["status"] == "ok"
    assert result == "high-ok"
    assert tool.exec_count == 1

    # 批准路径不得产生 tool_blocked 审计记录（修复 3）。
    assert all(c.get("phase") != "tool_blocked" for c in checkpoints)


# 4. HIGH + policy="deny" + callback 返回 True → 不执行(deny 优先于 callback)。


@pytest.mark.asyncio
async def test_high_risk_policy_deny_overrides_approving_callback() -> None:
    checkpoints: list[dict[str, Any]] = []
    tool = HighRiskFakeTool()
    seen: list[dict[str, Any]] = []

    async def approve(info: dict[str, Any]) -> bool:
        seen.append(info)
        return True

    spec = _spec(
        FakeRegistry(tool),
        checkpoints,
        high_risk_policy="deny",
        approval_callback=approve,
    )

    coord = _coordinator()
    result, event, error = await coord.run_tool(spec, _tool_call(), {}, {})

    assert tool.exec_count == 0
    assert seen == []  # deny 短路,回调根本不被咨询
    assert error is None
    assert event["status"] == "error"
    assert "high_risk_policy=deny" in str(result)

    # policy=deny 拒绝路径同样必须留 tool_blocked 审计 checkpoint（修复 3）。
    blocked = [c for c in checkpoints if c.get("phase") == "tool_blocked"]
    assert len(blocked) == 1
    blocked_cp = blocked[0]["tool_checkpoint"]
    assert blocked_cp["status"] == "blocked"
    assert blocked_cp["risk_level"] == "high"


# 5. LOW 工具 + callback 存在 → callback 不被调用、执行成功、无 tool_started。


@pytest.mark.asyncio
async def test_low_risk_skips_callback_and_start_checkpoint() -> None:
    checkpoints: list[dict[str, Any]] = []
    tool = LowRiskFakeTool()
    seen: list[dict[str, Any]] = []

    async def approve(info: dict[str, Any]) -> bool:
        seen.append(info)
        return True

    spec = _spec(FakeRegistry(tool), checkpoints, approval_callback=approve)

    coord = _coordinator()
    result, event, error = await coord.run_tool(spec, _tool_call("fake_low"), {}, {})

    assert error is None
    assert event["status"] == "ok"
    assert result == "low-ok"  # LOW 工具覆写 execute 返回自己的结果
    assert tool.exec_count == 1
    assert seen == []  # LOW 工具不触发审批回调
    phases = [c.get("phase") for c in checkpoints]
    assert "tool_started" not in phases  # requires_checkpoint=False
    assert "tool_completed" in phases


# 6. approval_callback 抛异常 → 按"拒绝"处理(fail-closed),不执行。


@pytest.mark.asyncio
async def test_approval_callback_exception_fail_closed() -> None:
    checkpoints: list[dict[str, Any]] = []
    tool = HighRiskFakeTool()

    async def broken(info: dict[str, Any]) -> bool:
        raise RuntimeError("approval service unavailable")

    spec = _spec(FakeRegistry(tool), checkpoints, approval_callback=broken)

    coord = _coordinator()
    result, event, error = await coord.run_tool(spec, _tool_call(), {}, {})

    assert tool.exec_count == 0
    assert error is None
    assert event["status"] == "error"
    assert "blocked before execution" in str(result)


# 7. fail_on_tool_error=True + callback 拒绝 → error 是 RuntimeError 且消息含
#    "blocked before execution"。


@pytest.mark.asyncio
async def test_fail_on_tool_error_denied_raises_runtime_error() -> None:
    checkpoints: list[dict[str, Any]] = []
    tool = HighRiskFakeTool()

    async def deny(info: dict[str, Any]) -> bool:
        return False

    spec = _spec(
        FakeRegistry(tool),
        checkpoints,
        approval_callback=deny,
        fail_on_tool_error=True,
    )

    coord = _coordinator()
    result, event, error = await coord.run_tool(spec, _tool_call(), {}, {})

    assert tool.exec_count == 0
    assert isinstance(error, RuntimeError)
    assert "blocked before execution" in str(error)
    assert "blocked before execution" in str(result)
    assert event["status"] == "error"


# 8. 同步（非 async）approval_callback 返回 True → 正常执行（修复 4：
#    此前直接 await 同步回调会 TypeError 被 suppress 吞掉而静默拒绝）。


@pytest.mark.asyncio
async def test_sync_approval_callback_returning_true_executes() -> None:
    checkpoints: list[dict[str, Any]] = []
    tool = HighRiskFakeTool()
    seen: list[dict[str, Any]] = []

    def approve_sync(info: dict[str, Any]) -> bool:
        seen.append(info)
        return True

    spec = _spec(FakeRegistry(tool), checkpoints, approval_callback=approve_sync)

    coord = _coordinator()
    result, event, error = await coord.run_tool(spec, _tool_call(), {}, {})

    assert len(seen) == 1  # 同步回调被真正咨询，而非静默拒绝
    assert error is None
    assert event["status"] == "ok"
    assert result == "high-ok"
    assert tool.exec_count == 1


# 9. 非法 high_risk_policy 在 AgentRunSpec 构造期即抛 ValueError（修复 5）。


def test_invalid_high_risk_policy_rejected_at_construction() -> None:
    with pytest.raises(ValueError):
        AgentRunSpec(
            initial_messages=[],
            tools=FakeRegistry(HighRiskFakeTool()),
            model="test-model",
            max_iterations=2,
            max_tool_result_chars=1000,
            high_risk_policy="Allow",
        )
