"""Tests for the /remember and /记住 built-in commands."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from miniunicorn.agent.explicit_memory import RelationResult
from miniunicorn.agent.loop import AgentLoop
from miniunicorn.agent.memory_sources import MemorySourceCatalog
from miniunicorn.bus.events import InboundMessage
from miniunicorn.bus.queue import MessageBus
from miniunicorn.command.builtin import (
    build_help_text,
    builtin_command_palette,
    register_builtin_commands,
)
from miniunicorn.command.router import CommandContext, CommandRouter


def _provider(default_model: str = "base-model") -> MagicMock:
    provider = MagicMock()
    provider.get_default_model.return_value = default_model
    provider.generation = SimpleNamespace(
        max_tokens=123,
        temperature=0.1,
        reasoning_effort=None,
    )
    return provider


def _make_loop(tmp_path) -> AgentLoop:
    return AgentLoop(
        bus=MessageBus(),
        provider=_provider(),
        workspace=tmp_path,
        model="base-model",
        context_window_tokens=1000,
    )


def _classifier(label: str, normalized_fact: str = ""):
    async def _classify(raw_text: str, candidates):
        candidate = candidates[0] if candidates else None
        return RelationResult(
            label=label,
            candidate_memory_id=candidate.memory_id if candidate else None,
            normalized_fact=normalized_fact or raw_text,
            scope=None,
            reason="test",
        )

    return _classify


def _msg(content: str) -> InboundMessage:
    return InboundMessage(channel="cli", sender_id="user", chat_id="direct", content=content)


def _ctx(loop: AgentLoop, raw: str) -> CommandContext:
    msg = _msg(raw)
    return CommandContext(msg=msg, session=None, key=msg.session_key, raw=raw, loop=loop)


def test_remember_commands_in_help_and_palette() -> None:
    palette = builtin_command_palette()

    assert any(item["command"] == "/remember" and item["arg_hint"] == "<内容>" for item in palette)
    assert any(item["command"] == "/记住" and item["arg_hint"] == "<内容>" for item in palette)
    assert "/remember <内容>" in build_help_text()
    assert "/记住 <内容>" in build_help_text()


@pytest.mark.asyncio
async def test_remember_command_registered_on_router_and_saves(tmp_path) -> None:
    router = CommandRouter()
    register_builtin_commands(router)
    loop = _make_loop(tmp_path)
    loop._classify_memory_relation = _classifier("supplement", "用户被称作Alice")

    out = await router.dispatch(_ctx(loop, "/remember call me Alice"))

    assert out is not None
    assert "已保存" in out.content
    effective = loop.explicit_memory.journal.effective()
    assert len(effective) == 1
    assert effective[0].normalized_fact == "用户被称作Alice"


@pytest.mark.asyncio
async def test_remember_command_conflict_then_update_flow(tmp_path) -> None:
    loop = _make_loop(tmp_path)
    loop._classify_memory_relation = _classifier("conflict", "用户喜欢深色主题")
    journal = loop.explicit_memory.journal
    existing = journal.append_new("用户喜欢浅色主题", "用户喜欢浅色主题", None)
    session = loop.sessions.get_or_create("cli:direct")

    reply = await loop._handle_explicit_memory(_msg("/记住 我喜欢深色主题"), session)

    assert "冲突" in reply.content
    assert "浅色主题" in reply.content and "深色主题" in reply.content
    assert session.metadata["pending_memory_proposal"]["action"] == "confirmation_required"
    assert len(journal.effective()) == 1

    resolved = await loop._handle_explicit_memory(_msg("更新记忆"), session)

    assert "已更新" in resolved.content
    assert "pending_memory_proposal" not in session.metadata
    effective = journal.effective()
    assert len(effective) == 1
    assert effective[0].revision == 2
    assert len(journal.history(existing.memory_id)) == 2
    records = [
        r
        for r in MemorySourceCatalog(tmp_path).scan().records
        if r.source_type == "explicit"
    ]
    assert len(records) == 1
    assert records[0].source_id == f"explicit:{existing.memory_id}"
    assert records[0].source_revision == "2"


@pytest.mark.asyncio
async def test_pending_proposal_survives_gateway_restart(tmp_path) -> None:
    loop1 = _make_loop(tmp_path)
    loop1._classify_memory_relation = _classifier("conflict", "用户喜欢深色主题")
    journal = loop1.explicit_memory.journal
    journal.append_new("用户喜欢浅色主题", "用户喜欢浅色主题", None)
    session1 = loop1.sessions.get_or_create("cli:direct")

    await loop1._handle_explicit_memory(_msg("/记住 我喜欢深色主题"), session1)
    assert "pending_memory_proposal" in session1.metadata

    loop2 = _make_loop(tmp_path)
    session2 = loop2.sessions.get_or_create("cli:direct")

    assert "pending_memory_proposal" in session2.metadata
    resolved = await loop2._handle_explicit_memory(_msg("更新记忆"), session2)

    assert "已更新" in resolved.content
    assert "pending_memory_proposal" not in session2.metadata
    assert journal.effective()[0].revision == 2
