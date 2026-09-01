import asyncio

import pytest

from miniunicorn.agent.call_ledger import CallLedger, CallPurpose, bind_call_ledger, call_purpose
from miniunicorn.providers.base import LLMProvider, LLMResponse


class ScriptedProvider(LLMProvider):
    """A scripted provider that returns predefined responses without recording."""

    def __init__(self, responses):
        super().__init__()
        self._responses = list(responses)
        self.calls = 0
        self.last_kwargs: dict = {}

    async def chat(self, *args, **kwargs) -> LLMResponse:
        self.calls += 1
        self.last_kwargs = kwargs
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    async def chat_stream(self, *args, **kwargs) -> LLMResponse:
        self.calls += 1
        self.last_kwargs = kwargs
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def get_default_model(self) -> str:
        return "test-model"


@pytest.mark.asyncio
async def test_chat_with_retry_records_once_per_turn(monkeypatch) -> None:
    """chat_with_retry should produce exactly one ledger record regardless of retry count."""
    provider = ScriptedProvider(
        [
            LLMResponse(content="429 rate limit", finish_reason="error"),
            LLMResponse(content="ok", usage={"prompt_tokens": 10, "completion_tokens": 20}),
        ]
    )

    delays: list[int] = []

    async def _fake_sleep(delay: int) -> None:
        delays.append(delay)

    monkeypatch.setattr("miniunicorn.providers.base.asyncio.sleep", _fake_sleep)

    # Bind a ledger for the turn
    ledger = CallLedger()
    async with bind_call_ledger(ledger):
        async with call_purpose(CallPurpose.EXECUTOR):
            response = await provider.chat_with_retry(
                messages=[{"role": "user", "content": "hello"}],
            )

    assert response.finish_reason == "stop"
    assert response.content == "ok"
    # Should be exactly ONE record, not one per retry attempt
    assert "executor" in ledger.purpose_usage
    assert ledger.purpose_usage["executor"] == {"prompt_tokens": 10, "completion_tokens": 20}
    assert ledger.total_usage == {"prompt_tokens": 10, "completion_tokens": 20}


@pytest.mark.asyncio
async def test_chat_stream_with_retry_records_once(monkeypatch) -> None:
    """chat_stream_with_retry should produce exactly one ledger record regardless of retry count."""
    provider = ScriptedProvider(
        [
            LLMResponse(content="429 rate limit", finish_reason="error"),
            LLMResponse(content="ok", usage={"prompt_tokens": 5, "completion_tokens": 15}),
        ]
    )

    delays: list[int] = []

    async def _fake_sleep(delay: int) -> None:
        delays.append(delay)

    monkeypatch.setattr("miniunicorn.providers.base.asyncio.sleep", _fake_sleep)

    ledger = CallLedger()
    async with bind_call_ledger(ledger):
        async with call_purpose(CallPurpose.EXECUTOR):
            response = await provider.chat_stream_with_retry(
                messages=[{"role": "user", "content": "hello"}],
            )

    assert response.content == "ok"
    assert "executor" in ledger.purpose_usage
    assert ledger.purpose_usage["executor"] == {"prompt_tokens": 5, "completion_tokens": 15}


@pytest.mark.asyncio
async def test_chat_with_retry_budget_accumulation(monkeypatch) -> None:
    """TurnBudget should accumulate cost across retry attempts exactly once."""

    provider = ScriptedProvider(
        [
            LLMResponse(content="429 rate limit", finish_reason="error", usage={"cost_usd": 0.01}),
            LLMResponse(content="ok", usage={"cost_usd": 0.02}),
        ]
    )

    delays: list[int] = []

    async def _fake_sleep(delay: int) -> None:
        delays.append(delay)

    monkeypatch.setattr("miniunicorn.providers.base.asyncio.sleep", _fake_sleep)

    ledger = CallLedger()
    async with bind_call_ledger(ledger):
        # Disable auto-check for test
        ledger.check_budget = lambda b: None

        async with call_purpose(CallPurpose.EXECUTOR):
            response = await provider.chat_with_retry(
                messages=[{"role": "user", "content": "hello"}],
            )

    assert response.content == "ok"
    # Cost should be accumulated once for the single recorded call (final response only)
    assert ledger._cost_usd == 0.02, f"Expected 0.02, got {ledger._cost_usd}"


@pytest.mark.asyncio
async def test_chat_stream_with_retry_fallback_no_double_record(monkeypatch) -> None:
    """When chat_stream_with_retry uses default stream fallback to chat(), only one record should be made."""

    # Provider that uses default chat_stream (falls back to chat) but overrides chat to succeed
    class FallbackProvider(LLMProvider):
        def __init__(self):
            super().__init__()
            self.chat_calls = 0

        async def chat(self, *args, **kwargs) -> LLMResponse:
            self.chat_calls += 1
            self.last_kwargs = kwargs
            return LLMResponse(content="ok", usage={"prompt_tokens": 8, "completion_tokens": 12})

        def get_default_model(self) -> str:
            return "test-model"

    provider = FallbackProvider()

    delays: list[int] = []

    async def _fake_sleep(delay: int) -> None:
        delays.append(delay)

    monkeypatch.setattr("miniunicorn.providers.base.asyncio.sleep", _fake_sleep)

    ledger = CallLedger()
    async with bind_call_ledger(ledger):
        async with call_purpose(CallPurpose.EXECUTOR):
            response = await provider.chat_stream_with_retry(
                messages=[{"role": "user", "content": "hello"}],
            )

    assert response.content == "ok"
    assert provider.chat_calls == 1
    # Should be exactly ONE record since default chat_stream calls chat() once and base records once
    assert "executor" in ledger.purpose_usage
    assert ledger.purpose_usage["executor"] == {"prompt_tokens": 8, "completion_tokens": 12}
    assert ledger.total_usage == {"prompt_tokens": 8, "completion_tokens": 12}


@pytest.mark.asyncio
async def test_purpose_scope_separation(monkeypatch) -> None:
    """Different call_purpose scopes should separate purpose_usage."""
    provider = ScriptedProvider(
        [
            LLMResponse(content="plan", usage={"prompt_tokens": 100, "completion_tokens": 50}),
            LLMResponse(content="execute", usage={"prompt_tokens": 10, "completion_tokens": 20}),
        ]
    )

    ledger = CallLedger()
    async with bind_call_ledger(ledger):
        async with call_purpose(CallPurpose.PLANNER):
            await provider.chat_with_retry(messages=[{"role": "user", "content": "plan"}])

        async with call_purpose(CallPurpose.EXECUTOR):
            await provider.chat_with_retry(messages=[{"role": "user", "content": "execute"}])

    assert "planner" in ledger.purpose_usage
    assert "executor" in ledger.purpose_usage
    assert ledger.purpose_usage["planner"] == {"prompt_tokens": 100, "completion_tokens": 50}
    assert ledger.purpose_usage["executor"] == {"prompt_tokens": 10, "completion_tokens": 20}
    assert ledger.total_usage == {"prompt_tokens": 110, "completion_tokens": 70}


@pytest.mark.asyncio
async def test_concurrent_ledger_isolation(monkeypatch) -> None:
    """Concurrent tasks should have isolated ledger state."""

    async def run_task(task_id: int, usage: dict):
        provider = ScriptedProvider([LLMResponse(content=f"response-{task_id}", usage=usage)])
        ledger = CallLedger()
        async with bind_call_ledger(ledger):
            async with call_purpose(CallPurpose.EXECUTOR):
                await provider.chat_with_retry(
                    messages=[{"role": "user", "content": f"task-{task_id}"}]
                )
        return ledger

    ledger1, ledger2 = await asyncio.gather(
        run_task(1, {"prompt_tokens": 10, "completion_tokens": 20}),
        run_task(2, {"prompt_tokens": 5, "completion_tokens": 15}),
    )

    assert ledger1.total_usage == {"prompt_tokens": 10, "completion_tokens": 20}
    assert ledger2.total_usage == {"prompt_tokens": 5, "completion_tokens": 15}
    assert "executor" in ledger1.purpose_usage
    assert "executor" in ledger2.purpose_usage
