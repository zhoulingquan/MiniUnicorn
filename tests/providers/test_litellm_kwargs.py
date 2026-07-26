"""Tests for OpenAICompatProvider spec-driven behavior.

只覆盖 registry 已注册的 provider（custom/deepseek/opencode）以及不依赖
spec 的通用行为。其他 provider（openai/openrouter/gemini/dashscope/minimax/
volcengine/byteplus/moonshot/zhipu/mistral/aihubmix 等）的测试已移除，因为
它们对应的 ProviderSpec 未在 registry 注册。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from miniunicorn.providers.openai_compat_provider import OpenAICompatProvider
from miniunicorn.providers.registry import find_by_name


def _fake_chat_response(content: str = "ok") -> SimpleNamespace:
    """Build a minimal OpenAI chat completion response."""
    message = SimpleNamespace(
        content=content,
        tool_calls=None,
        reasoning_content=None,
    )
    choice = SimpleNamespace(message=message, finish_reason="stop")
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    return SimpleNamespace(choices=[choice], usage=usage)


def _fake_tool_call_response() -> SimpleNamespace:
    """Build a minimal chat response that includes Gemini-style extra_content."""
    function = SimpleNamespace(
        name="exec",
        arguments='{"cmd":"ls"}',
        provider_specific_fields={"inner": "value"},
    )
    tool_call = SimpleNamespace(
        id="call_123",
        index=0,
        type="function",
        function=function,
        extra_content={"google": {"thought_signature": "signed-token"}},
    )
    message = SimpleNamespace(
        content=None,
        tool_calls=[tool_call],
        reasoning_content=None,
    )
    choice = SimpleNamespace(message=message, finish_reason="tool_calls")
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    return SimpleNamespace(choices=[choice], usage=usage)


def _fake_responses_response(content: str = "ok") -> MagicMock:
    """Build a minimal Responses API response object."""
    resp = MagicMock()
    resp.model_dump.return_value = {
        "output": [{
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": content}],
        }],
        "status": "completed",
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    }
    return resp


def _fake_responses_stream(text: str = "ok"):
    async def _stream():
        yield SimpleNamespace(type="response.output_text.delta", delta=text)
        yield SimpleNamespace(
            type="response.completed",
            response=SimpleNamespace(
                status="completed",
                usage=SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15),
                output=[],
            ),
        )

    return _stream()


def _fake_chat_stream(text: str = "ok"):
    async def _stream():
        yield SimpleNamespace(
            choices=[SimpleNamespace(finish_reason=None, delta=SimpleNamespace(content=text, reasoning_content=None, tool_calls=None))],
            usage=None,
        )
        yield SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop", delta=SimpleNamespace(content=None, reasoning_content=None, tool_calls=None))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

    return _stream()


def _fake_chat_stream_reasoning_chunks():
    """Mimic DeepSeek-style ``chat.completions`` stream: ``reasoning_content`` then ``content``."""

    async def _stream():
        yield SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason=None,
                    delta=SimpleNamespace(
                        content=None,
                        reasoning_content="step1",
                        reasoning=None,
                        tool_calls=None,
                    ),
                ),
            ],
            usage=None,
        )
        yield SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason=None,
                    delta=SimpleNamespace(
                        content=None,
                        reasoning_content="step2",
                        reasoning=None,
                        tool_calls=None,
                    ),
                ),
            ],
            usage=None,
        )
        yield SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason=None,
                    delta=SimpleNamespace(
                        content="answer",
                        reasoning_content=None,
                        tool_calls=None,
                    ),
                ),
            ],
            usage=None,
        )
        yield SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    delta=SimpleNamespace(
                        content=None,
                        reasoning_content=None,
                        tool_calls=None,
                    ),
                ),
            ],
            usage=SimpleNamespace(
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
            ),
        )

    return _stream()


def _fake_chat_stream_tool_call_chunks():
    """Mimic OpenAI-compatible streaming tool-call argument deltas."""

    async def _stream():
        yield SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason=None,
                    delta=SimpleNamespace(
                        content=None,
                        reasoning_content=None,
                        reasoning=None,
                        tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id="call_write",
                                function=SimpleNamespace(
                                    name="write_file",
                                    arguments='{"path":"notes.md","content":"',
                                ),
                            )
                        ],
                    ),
                ),
            ],
            usage=None,
        )
        yield SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason=None,
                    delta=SimpleNamespace(
                        content=None,
                        reasoning_content=None,
                        reasoning=None,
                        tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id=None,
                                function=SimpleNamespace(name=None, arguments='line\\n"}'),
                            )
                        ],
                    ),
                ),
            ],
            usage=None,
        )
        yield SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="tool_calls",
                    delta=SimpleNamespace(
                        content=None,
                        reasoning_content=None,
                        reasoning=None,
                        tool_calls=None,
                    ),
                ),
            ],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

    return _stream()


def _fake_chat_stream_legacy_function_call_chunks():
    """Mimic older OpenAI-compatible ``delta.function_call`` chunks."""

    async def _stream():
        yield SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason=None,
                    delta=SimpleNamespace(
                        content=None,
                        reasoning_content=None,
                        reasoning=None,
                        tool_calls=None,
                        function_call=SimpleNamespace(
                            name="write_file",
                            arguments='{"path":"notes.md","content":"',
                        ),
                    ),
                ),
            ],
            usage=None,
        )
        yield SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason=None,
                    delta=SimpleNamespace(
                        content=None,
                        reasoning_content=None,
                        reasoning=None,
                        tool_calls=None,
                        function_call=SimpleNamespace(
                            name=None,
                            arguments='line\\n"}',
                        ),
                    ),
                ),
            ],
            usage=None,
        )
        yield SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="function_call",
                    delta=SimpleNamespace(
                        content=None,
                        reasoning_content=None,
                        reasoning=None,
                        tool_calls=None,
                        function_call=None,
                    ),
                ),
            ],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

    return _stream()


class _FakeResponsesError(Exception):
    def __init__(self, status_code: int, text: str):
        super().__init__(text)
        self.status_code = status_code
        self.response = SimpleNamespace(status_code=status_code, text=text, headers={})


class _StalledStream:
    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(3600)
        raise StopAsyncIteration


@pytest.mark.asyncio
async def test_openai_compat_stream_forwards_reasoning_deltas_deepseek_style() -> None:
    """Regression: DeepSeek-V4 / reasoner expose ``delta.reasoning_content`` during streaming."""
    mock_chat = AsyncMock(return_value=_fake_chat_stream_reasoning_chunks())
    spec = find_by_name("deepseek")
    thinking: list[str] = []
    content: list[str] = []

    async def on_thinking(d: str) -> None:
        thinking.append(d)

    async def on_content(d: str) -> None:
        content.append(d)

    with patch("miniunicorn.providers.openai_compat_provider.AsyncOpenAI") as mock_openai:
        client_instance = mock_openai.return_value
        client_instance.chat.completions.create = mock_chat

        provider = OpenAICompatProvider(
            api_key="sk-test",
            default_model="deepseek-v4-pro",
            spec=spec,
        )
        result = await provider.chat_stream(
            messages=[{"role": "user", "content": "hi"}],
            model="deepseek-v4-pro",
            reasoning_effort="high",
            on_content_delta=on_content,
            on_thinking_delta=on_thinking,
        )

    assert thinking == ["step1", "step2"]
    assert content == ["answer"]
    assert result.reasoning_content == "step1step2"
    assert result.content == "answer"
    mock_chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_openai_compat_stream_forwards_tool_call_argument_deltas_deepseek() -> None:
    """DeepSeek streaming tool-call argument deltas are forwarded to on_tool_call_delta."""
    mock_chat = AsyncMock(return_value=_fake_chat_stream_tool_call_chunks())
    spec = find_by_name("deepseek")
    deltas: list[dict] = []

    async def on_tool_delta(delta: dict) -> None:
        deltas.append(delta)

    with patch("miniunicorn.providers.openai_compat_provider.AsyncOpenAI") as mock_openai:
        client_instance = mock_openai.return_value
        client_instance.chat.completions.create = mock_chat

        provider = OpenAICompatProvider(
            api_key="sk-test",
            default_model="deepseek-chat",
            spec=spec,
        )
        result = await provider.chat_stream(
            messages=[{"role": "user", "content": "write"}],
            tools=[{"type": "function", "function": {"name": "write_file"}}],
            model="deepseek-chat",
            on_tool_call_delta=on_tool_delta,
        )

    assert deltas == [
        {
            "index": 0,
            "call_id": "call_write",
            "name": "write_file",
            "arguments_delta": '{"path":"notes.md","content":"',
        },
        {"index": 0, "call_id": "", "name": "", "arguments_delta": 'line\\n"}'},
    ]
    assert result.tool_calls[0].name == "write_file"
    assert result.tool_calls[0].arguments == {"path": "notes.md", "content": "line\n"}
    kwargs = mock_chat.await_args.kwargs
    assert kwargs.get("extra_body", {}).get("tool_stream") is None


@pytest.mark.asyncio
async def test_openai_compat_stream_forwards_legacy_function_call_argument_deltas() -> None:
    mock_chat = AsyncMock(return_value=_fake_chat_stream_legacy_function_call_chunks())
    deltas: list[dict] = []

    async def on_tool_delta(delta: dict) -> None:
        deltas.append(delta)

    with patch("miniunicorn.providers.openai_compat_provider.AsyncOpenAI") as mock_openai:
        client_instance = mock_openai.return_value
        client_instance.chat.completions.create = mock_chat

        provider = OpenAICompatProvider(
            api_key="sk-test",
            default_model="deepseek-chat",
            spec=find_by_name("deepseek"),
        )
        result = await provider.chat_stream(
            messages=[{"role": "user", "content": "write"}],
            tools=[{"type": "function", "function": {"name": "write_file"}}],
            model="deepseek-chat",
            on_tool_call_delta=on_tool_delta,
        )

    assert deltas == [
        {
            "index": 0,
            "call_id": "",
            "name": "write_file",
            "arguments_delta": '{"path":"notes.md","content":"',
        },
        {"index": 0, "call_id": "", "name": "", "arguments_delta": 'line\\n"}'},
    ]
    assert result.tool_calls[0].name == "write_file"
    assert result.tool_calls[0].arguments == {"path": "notes.md", "content": "line\n"}


@pytest.mark.asyncio
async def test_standard_provider_passes_model_through() -> None:
    """Standard provider (e.g. deepseek) passes model name through as-is."""
    mock_create = AsyncMock(return_value=_fake_chat_response())
    spec = find_by_name("deepseek")

    with patch("miniunicorn.providers.openai_compat_provider.AsyncOpenAI") as MockClient:
        client_instance = MockClient.return_value
        client_instance.chat.completions.create = mock_create

        provider = OpenAICompatProvider(
            api_key="sk-deepseek-test-key",
            default_model="deepseek-chat",
            spec=spec,
        )
        await provider.chat(
            messages=[{"role": "user", "content": "hello"}],
            model="deepseek-chat",
        )

    call_kwargs = mock_create.call_args.kwargs
    assert call_kwargs["model"] == "deepseek-chat"


def test_openai_compat_supports_temperature_matches_reasoning_model_rules() -> None:
    assert OpenAICompatProvider._supports_temperature("gpt-4o") is True
    assert OpenAICompatProvider._supports_temperature("gpt-5-chat") is False
    assert OpenAICompatProvider._supports_temperature("o3-mini") is False
    assert OpenAICompatProvider._supports_temperature("gpt-4o", reasoning_effort="medium") is False


def test_openai_compat_preserves_message_level_reasoning_fields() -> None:
    with patch("miniunicorn.providers.openai_compat_provider.AsyncOpenAI"):
        provider = OpenAICompatProvider()

    sanitized = provider._sanitize_messages([
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "done",
            "reasoning_content": "hidden",
            "extra_content": {"debug": True},
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "fn", "arguments": "{}"},
                    "extra_content": {"google": {"thought_signature": "sig"}},
                }
            ],
        },
        {"role": "user", "content": "thanks"},
    ])

    assert sanitized[1]["content"] is None
    assert sanitized[1]["reasoning_content"] == "hidden"
    assert sanitized[1]["extra_content"] == {"debug": True}
    assert sanitized[1]["tool_calls"][0]["extra_content"] == {"google": {"thought_signature": "sig"}}


def _deepseek_kwargs(messages: list[dict]) -> dict:
    with patch("miniunicorn.providers.openai_compat_provider.AsyncOpenAI"):
        provider = OpenAICompatProvider(
            api_key="sk-test",
            default_model="deepseek-v4-flash",
            spec=find_by_name("deepseek"),
        )

    return provider._build_kwargs(
        messages=messages,
        tools=None,
        model="deepseek-v4-flash",
        max_tokens=1024,
        temperature=0.7,
        reasoning_effort="high",
        tool_choice=None,
    )


def _tool_call(call_id: str) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "my", "arguments": "{}"},
    }


def test_deepseek_thinking_backfills_missing_reasoning_content_on_tool_history() -> None:
    """Backfill reasoning_content="" instead of dropping the turn (#3554, #3584)."""
    kwargs = _deepseek_kwargs([
        {"role": "system", "content": "system"},
        {"role": "user", "content": "can we use wechat?"},
        {"role": "assistant", "content": "", "tool_calls": [_tool_call("call_bad")]},
        {"role": "tool", "tool_call_id": "call_bad", "name": "my", "content": "channels"},
        {"role": "user", "content": "continue"},
    ])

    assert [m["role"] for m in kwargs["messages"]] == [
        "system", "user", "assistant", "tool", "user",
    ]
    assistant = kwargs["messages"][2]
    assert assistant["reasoning_content"] == ""
    assert assistant["tool_calls"][0]["function"]["name"] == "my"


def test_deepseek_thinking_keeps_tool_history_with_reasoning_content() -> None:
    kwargs = _deepseek_kwargs([
        {"role": "user", "content": "can we use wechat?"},
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "I should inspect supported channels.",
            "tool_calls": [_tool_call("call_good")],
        },
        {"role": "tool", "tool_call_id": "call_good", "name": "my", "content": "channels"},
        {"role": "user", "content": "continue"},
    ])

    assistant = kwargs["messages"][1]
    assert assistant["role"] == "assistant"
    assert assistant["reasoning_content"] == "I should inspect supported channels."
    assert kwargs["messages"][2]["role"] == "tool"


def test_openai_compat_preserves_tool_call_ids_after_consecutive_assistant_messages() -> None:
    with patch("miniunicorn.providers.openai_compat_provider.AsyncOpenAI"):
        provider = OpenAICompatProvider()

    sanitized = provider._sanitize_messages([
        {"role": "user", "content": "不错"},
        {"role": "assistant", "content": "对，破 4 万指日可待"},
        {
            "role": "assistant",
            "content": " modelling我再查一下",
            "tool_calls": [
                {
                    "id": "call_function_akxp3wqzn7ph_1",
                    "type": "function",
                    "function": {"name": "exec", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_function_akxp3wqzn7ph_1", "name": "exec", "content": "ok"},
        {"role": "user", "content": "多少star了呢"},
    ])

    assert sanitized[1]["role"] == "assistant"
    assert sanitized[1]["content"] is None
    assert sanitized[1]["tool_calls"][0]["id"] == "call_function_akxp3wqzn7ph_1"
    assert sanitized[2]["tool_call_id"] == "call_function_akxp3wqzn7ph_1"


def test_openai_compat_deduplicates_duplicate_tool_call_ids_in_history() -> None:
    with patch("miniunicorn.providers.openai_compat_provider.AsyncOpenAI"):
        provider = OpenAICompatProvider()

    sanitized = provider._sanitize_messages([
        {"role": "user", "content": "check both files"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "ab1b45c2a",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'},
                },
                {
                    "id": "ab1b45c2a",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"b.txt"}'},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "ab1b45c2a", "name": "read_file", "content": "a"},
        {"role": "tool", "tool_call_id": "ab1b45c2a", "name": "read_file", "content": "b"},
        {"role": "user", "content": "continue"},
    ])

    tool_call_ids = [tc["id"] for tc in sanitized[1]["tool_calls"]]
    tool_result_ids = [sanitized[2]["tool_call_id"], sanitized[3]["tool_call_id"]]

    assert tool_call_ids[0] == "ab1b45c2a"
    assert len(tool_call_ids) == len(set(tool_call_ids)) == 2
    assert tool_result_ids == tool_call_ids


def test_openai_compat_stringifies_dict_tool_arguments() -> None:
    with patch("miniunicorn.providers.openai_compat_provider.AsyncOpenAI"):
        provider = OpenAICompatProvider()

    sanitized = provider._sanitize_messages([
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "exec", "arguments": {"cmd": "ls -la"}},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "name": "exec", "content": "ok"},
        {"role": "user", "content": "done"},
    ])

    assert sanitized[1]["tool_calls"][0]["function"]["arguments"] == '{"cmd": "ls -la"}'


def test_openai_compat_repairs_non_json_tool_arguments_string() -> None:
    with patch("miniunicorn.providers.openai_compat_provider.AsyncOpenAI"):
        provider = OpenAICompatProvider()

    sanitized = provider._sanitize_messages([
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "exec", "arguments": "{'cmd': 'pwd'}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "name": "exec", "content": "ok"},
        {"role": "user", "content": "done"},
    ])

    assert sanitized[1]["tool_calls"][0]["function"]["arguments"] == '{"cmd": "pwd"}'


def test_openai_compat_defaults_missing_tool_arguments_to_empty_object() -> None:
    with patch("miniunicorn.providers.openai_compat_provider.AsyncOpenAI"):
        provider = OpenAICompatProvider()

    sanitized = provider._sanitize_messages([
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "exec"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "name": "exec", "content": "ok"},
        {"role": "user", "content": "done"},
    ])

    assert sanitized[1]["tool_calls"][0]["function"]["arguments"] == "{}"


# ---------------------------------------------------------------------------
# Provider-specific thinking parameters (extra_body)
# ---------------------------------------------------------------------------

def _build_kwargs_for(provider_name: str, model: str, reasoning_effort=None):
    spec = find_by_name(provider_name)
    with patch("miniunicorn.providers.openai_compat_provider.AsyncOpenAI"):
        p = OpenAICompatProvider(api_key="k", default_model=model, spec=spec)
    return p._build_kwargs(
        messages=[{"role": "user", "content": "hi"}],
        tools=None, model=model, max_tokens=1024, temperature=0.7,
        reasoning_effort=reasoning_effort, tool_choice=None,
    )


def test_deepseek_thinking_enabled() -> None:
    """DeepSeek V4 requires extra_body.thinking when reasoning_effort is set."""
    kw = _build_kwargs_for("deepseek", "deepseek-v4-pro", reasoning_effort="high")
    assert kw["extra_body"] == {"thinking": {"type": "enabled"}}


def test_deepseek_thinking_disabled_for_minimal() -> None:
    """reasoning_effort='minimal' must send thinking.type=disabled to DeepSeek."""
    kw = _build_kwargs_for("deepseek", "deepseek-v4-pro", reasoning_effort="minimal")
    assert kw["extra_body"] == {"thinking": {"type": "disabled"}}


def test_deepseek_no_extra_body_when_reasoning_effort_none() -> None:
    """Without reasoning_effort the thinking param must not be injected."""
    kw = _build_kwargs_for("deepseek", "deepseek-chat", reasoning_effort=None)
    assert "extra_body" not in kw


def test_deepseek_backfills_reasoning_content_on_legacy_tool_call_messages() -> None:
    """Session messages from before thinking mode was enabled may have assistant
    messages with tool_calls but no reasoning_content. DeepSeek V4 rejects these
    with 400. _build_kwargs must backfill reasoning_content='' on them."""
    spec = find_by_name("deepseek")
    with patch("miniunicorn.providers.openai_compat_provider.AsyncOpenAI"):
        p = OpenAICompatProvider(api_key="k", default_model="deepseek-v4-pro", spec=spec)
    messages = [
        {"role": "user", "content": "search for news"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "tc1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "tc1", "content": "result"},
        {"role": "assistant", "content": "Here are the results."},
        {"role": "user", "content": "hi"},
    ]
    kw = p._build_kwargs(
        messages=messages, tools=None, model="deepseek-v4-pro",
        max_tokens=1024, temperature=0.7,
        reasoning_effort="high", tool_choice=None,
    )
    for msg in kw["messages"]:
        if msg.get("role") == "assistant":
            assert "reasoning_content" in msg, "legacy assistant message missing reasoning_content"
            assert msg["reasoning_content"] == ""


def test_backfill_does_not_touch_messages_when_thinking_explicitly_off() -> None:
    """When thinking is explicitly disabled, legacy messages must NOT be altered."""
    spec = find_by_name("deepseek")
    with patch("miniunicorn.providers.openai_compat_provider.AsyncOpenAI"):
        p = OpenAICompatProvider(api_key="k", default_model="deepseek-v4-pro", spec=spec)
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "tc1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "tc1", "content": "result"},
        {"role": "user", "content": "thanks"},
    ]
    for effort in ("minimal", "none"):
        kw = p._build_kwargs(
            messages=list(messages), tools=None, model="deepseek-v4-pro",
            max_tokens=1024, temperature=0.7,
            reasoning_effort=effort, tool_choice=None,
        )
        for msg in kw["messages"]:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                assert "reasoning_content" not in msg


def test_deepseek_v4_backfills_incomplete_reasoning_history_when_effort_implicit() -> None:
    """DeepSeek-V4 reasons natively: backfill even without explicit reasoning_effort."""
    spec = find_by_name("deepseek")
    with patch("miniunicorn.providers.openai_compat_provider.AsyncOpenAI"):
        p = OpenAICompatProvider(api_key="k", default_model="deepseek-v4-pro", spec=spec)
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "tc1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "tc1", "content": "result"},
        {"role": "user", "content": "thanks"},
    ]

    kw = p._build_kwargs(
        messages=list(messages), tools=None, model="deepseek-v4-pro",
        max_tokens=1024, temperature=0.7,
        reasoning_effort=None, tool_choice=None,
    )

    assert [msg["role"] for msg in kw["messages"]] == [
        "system", "user", "assistant", "tool", "user",
    ]
    assert kw["messages"][2]["reasoning_content"] == ""
    assert kw["messages"][-1]["content"] == "thanks"


def test_deepseek_chat_keeps_tool_history_when_effort_implicit() -> None:
    """Non-thinking deepseek-chat must keep history untouched and must NOT
    receive backfilled reasoning_content (#3554, #3584)."""
    spec = find_by_name("deepseek")
    with patch("miniunicorn.providers.openai_compat_provider.AsyncOpenAI"):
        p = OpenAICompatProvider(api_key="k", default_model="deepseek-chat", spec=spec)
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "tc1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "tc1", "content": "result"},
        {"role": "user", "content": "thanks"},
    ]

    kw = p._build_kwargs(
        messages=list(messages), tools=None, model="deepseek-chat",
        max_tokens=1024, temperature=0.7,
        reasoning_effort=None, tool_choice=None,
    )

    roles = [msg["role"] for msg in kw["messages"]]
    assert roles == ["user", "assistant", "tool", "user"]
    assert kw["messages"][1]["tool_calls"]
    assert "reasoning_content" not in kw["messages"][1]


def test_deepseek_coerces_list_content_to_string() -> None:
    """DeepSeek chat endpoint expects message.content to be a string."""
    spec = find_by_name("deepseek")
    with patch("miniunicorn.providers.openai_compat_provider.AsyncOpenAI"):
        p = OpenAICompatProvider(api_key="k", default_model="deepseek-chat", spec=spec)

    kw = p._build_kwargs(
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": "hello "},
                {"type": "text", "text": "world"},
            ],
        }],
        tools=None,
        model="deepseek-chat",
        max_tokens=1024,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    )

    assert isinstance(kw["messages"][0]["content"], str)
    assert "hello" in kw["messages"][0]["content"]
    assert "world" in kw["messages"][0]["content"]


# ---------------------------------------------------------------------------
# reasoning_effort="none" — treated as thinking disabled
# ---------------------------------------------------------------------------

def test_deepseek_thinking_disabled_for_none_string() -> None:
    """reasoning_effort='none' must send thinking.type=disabled and skip reasoning_effort field."""
    kw = _build_kwargs_for("deepseek", "deepseek-v4-pro", reasoning_effort="none")
    assert kw.get("extra_body") == {"thinking": {"type": "disabled"}}
    assert "reasoning_effort" not in kw


def test_deepseek_no_backfill_when_reasoning_effort_none_string() -> None:
    """reasoning_effort='none' must NOT trigger reasoning_content backfill (thinking inactive)."""
    spec = find_by_name("deepseek")
    with patch("miniunicorn.providers.openai_compat_provider.AsyncOpenAI"):
        p = OpenAICompatProvider(api_key="k", default_model="deepseek-v4-pro", spec=spec)
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "continue"},
    ]
    kw = p._build_kwargs(
        messages=list(messages), tools=None, model="deepseek-v4-pro",
        max_tokens=1024, temperature=0.7,
        reasoning_effort="none", tool_choice=None,
    )
    assistant = kw["messages"][1]
    assert "reasoning_content" not in assistant
