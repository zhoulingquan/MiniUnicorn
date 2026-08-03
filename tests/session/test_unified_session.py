"""Tests for unified_session feature.

Covers:
- AgentLoop._effective_session_key() rewrites session_key to "unified:default" when enabled
- Existing session_key_override is respected (not overwritten)
- Feature is off by default (no behavior change for existing users)
- Config schema serialises unified_session as camelCase "unifiedSession"
- onboard-generated config.json contains "unifiedSession" key
- /new command correctly clears the shared session in unified mode
- /new is NOT a priority command (goes through TurnDispatcher.process_message, key rewrite applies)
- Context window consolidation is unaffected by unified_session
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from miniunicorn.agent.loop import UNIFIED_SESSION_KEY, AgentLoop
from miniunicorn.bus.events import InboundMessage
from miniunicorn.bus.queue import MessageBus
from miniunicorn.command.builtin import cmd_new, register_builtin_commands
from miniunicorn.command.router import CommandContext, CommandRouter
from miniunicorn.config.schema import AgentDefaults, Config
from miniunicorn.session.manager import Session, SessionManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_loop(tmp_path: Path, unified_session: bool = False) -> AgentLoop:
    """Create a minimal AgentLoop for dispatch-level tests."""
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    with (
        patch("miniunicorn.agent.loop.SessionManager"),
        patch("miniunicorn.agent.loop.SubagentManager") as MockSubMgr,
        patch("miniunicorn.agent.loop.Dream"),
    ):
        MockSubMgr.return_value.cancel_by_session = AsyncMock(return_value=0)
        loop = AgentLoop(
            bus=bus,
            provider=provider,
            workspace=tmp_path,
            unified_session=unified_session,
        )
    return loop


def _make_msg(
    channel: str = "telegram", chat_id: str = "111", session_key_override: str | None = None
) -> InboundMessage:
    return InboundMessage(
        channel=channel,
        chat_id=chat_id,
        sender_id="user1",
        content="hello",
        session_key_override=session_key_override,
    )


# ---------------------------------------------------------------------------
# TestUnifiedSessionDispatch — core behaviour
# ---------------------------------------------------------------------------


class TestUnifiedSessionDispatch:
    """AgentLoop._effective_session_key() session key rewriting logic."""

    @pytest.mark.asyncio
    async def test_unified_session_rewrites_key_to_unified_default(self, tmp_path: Path):
        """When unified_session=True, all messages use 'unified:default' as session key."""
        loop = _make_loop(tmp_path, unified_session=True)
        msg = _make_msg(channel="telegram", chat_id="111")
        assert loop._effective_session_key(msg) == UNIFIED_SESSION_KEY

    @pytest.mark.asyncio
    async def test_unified_session_different_channels_share_same_key(self, tmp_path: Path):
        """Messages from different channels all resolve to the same session key."""
        loop = _make_loop(tmp_path, unified_session=True)
        assert (
            loop._effective_session_key(_make_msg(channel="telegram", chat_id="111"))
            == UNIFIED_SESSION_KEY
        )
        assert (
            loop._effective_session_key(_make_msg(channel="discord", chat_id="222"))
            == UNIFIED_SESSION_KEY
        )
        assert (
            loop._effective_session_key(_make_msg(channel="cli", chat_id="direct"))
            == UNIFIED_SESSION_KEY
        )

    @pytest.mark.asyncio
    async def test_unified_session_disabled_preserves_original_key(self, tmp_path: Path):
        """When unified_session=False (default), session key is channel:chat_id as usual."""
        loop = _make_loop(tmp_path, unified_session=False)
        msg = _make_msg(channel="telegram", chat_id="999")
        assert loop._effective_session_key(msg) == "telegram:999"

    @pytest.mark.asyncio
    async def test_unified_session_respects_existing_override(self, tmp_path: Path):
        """If session_key_override is already set (e.g. Telegram thread), it is NOT overwritten."""
        loop = _make_loop(tmp_path, unified_session=True)
        msg = _make_msg(
            channel="telegram", chat_id="111", session_key_override="telegram:thread:42"
        )
        assert loop._effective_session_key(msg) == "telegram:thread:42"

    def test_unified_session_default_is_false(self, tmp_path: Path):
        """unified_session defaults to False — no behavior change for existing users."""
        loop = _make_loop(tmp_path)
        assert loop._unified_session is False


# ---------------------------------------------------------------------------
# TestUnifiedSessionConfig — schema & serialisation
# ---------------------------------------------------------------------------


class TestUnifiedSessionConfig:
    """Config schema and onboard serialisation for unified_session."""

    def test_agent_defaults_unified_session_default_is_false(self):
        """AgentDefaults.unified_session defaults to False."""
        defaults = AgentDefaults()
        assert defaults.unified_session is False

    def test_agent_defaults_unified_session_can_be_enabled(self):
        """AgentDefaults.unified_session can be set to True."""
        defaults = AgentDefaults(unified_session=True)
        assert defaults.unified_session is True

    def test_config_serialises_unified_session_as_camel_case(self):
        """model_dump(by_alias=True) outputs 'unifiedSession' (camelCase) for JSON."""
        config = Config()
        data = config.model_dump(mode="json", by_alias=True)
        agents_defaults = data["agents"]["defaults"]
        assert "unifiedSession" in agents_defaults
        assert agents_defaults["unifiedSession"] is False

    def test_config_parses_unified_session_from_camel_case(self):
        """Config can be loaded from JSON with camelCase 'unifiedSession'."""
        raw = {"agents": {"defaults": {"unifiedSession": True}}}
        config = Config.model_validate(raw)
        assert config.agents.defaults.unified_session is True

    def test_config_parses_unified_session_from_snake_case(self):
        """Config also accepts snake_case 'unified_session' (populate_by_name=True)."""
        raw = {"agents": {"defaults": {"unified_session": True}}}
        config = Config.model_validate(raw)
        assert config.agents.defaults.unified_session is True

    def test_onboard_generated_config_contains_unified_session(self, tmp_path: Path):
        """save_config() writes 'unifiedSession' into config.json (simulates MiniUnicorn onboard)."""
        from miniunicorn.config.loader import save_config

        config = Config()
        config_path = tmp_path / "config.json"
        save_config(config, config_path)

        with open(config_path, encoding="utf-8") as f:
            data = json.load(f)

        agents_defaults = data["agents"]["defaults"]
        assert "unifiedSession" in agents_defaults, (
            "onboard-generated config.json must contain 'unifiedSession' key"
        )
        assert agents_defaults["unifiedSession"] is False


# ---------------------------------------------------------------------------
# TestCmdNewUnifiedSession — /new command behaviour in unified mode
# ---------------------------------------------------------------------------


class TestCmdNewUnifiedSession:
    """/new command routing and session-clear behaviour in unified mode."""

    def test_new_is_not_a_priority_command(self):
        """/new must NOT be in the priority table — it must go through TurnDispatcher.process_message
        so the unified session key rewrite applies before cmd_new runs."""
        router = CommandRouter()
        register_builtin_commands(router)
        assert router.is_priority("/new") is False

    def test_new_is_an_exact_command(self):
        """/new must be registered as an exact command."""
        router = CommandRouter()
        register_builtin_commands(router)
        assert "/new" in router._exact

    @pytest.mark.asyncio
    async def test_cmd_new_clears_unified_session(self, tmp_path: Path):
        """cmd_new called with key='unified:default' clears the shared session."""
        sessions = SessionManager(tmp_path)

        # Pre-populate the shared session with some messages
        shared = sessions.get_or_create("unified:default")
        shared.add_message("user", "hello from telegram")
        shared.add_message("assistant", "hi there")
        sessions.save(shared)
        assert len(sessions.get_or_create("unified:default").messages) == 2

        # _schedule_background is a *sync* method that schedules a coroutine via
        # asyncio.create_task().  Mirror that exactly so the coroutine is consumed
        # and no RuntimeWarning is emitted.
        loop = SimpleNamespace(
            sessions=sessions,
            consolidator=SimpleNamespace(archive=AsyncMock(return_value=True)),
            subagents=SimpleNamespace(cancel_by_session=AsyncMock(return_value=0)),
        )
        loop._schedule_background = lambda coro: asyncio.ensure_future(coro)

        msg = InboundMessage(
            channel="telegram",
            sender_id="user1",
            chat_id="111",
            content="/new",
            session_key_override="unified:default",  # as _effective_session_key() would resolve it
        )
        ctx = CommandContext(msg=msg, session=None, key="unified:default", raw="/new", loop=loop)

        result = await cmd_new(ctx)

        assert "New session started" in result.content
        # Invalidate cache and reload from disk to confirm persistence
        sessions.invalidate("unified:default")
        reloaded = sessions.get_or_create("unified:default")
        assert reloaded.messages == []

    @pytest.mark.asyncio
    async def test_cmd_new_in_unified_mode_does_not_affect_other_sessions(self, tmp_path: Path):
        """Clearing unified:default must not touch other sessions on disk."""
        sessions = SessionManager(tmp_path)

        other = sessions.get_or_create("discord:999")
        other.add_message("user", "discord message")
        sessions.save(other)

        shared = sessions.get_or_create("unified:default")
        shared.add_message("user", "shared message")
        sessions.save(shared)

        loop = SimpleNamespace(
            sessions=sessions,
            consolidator=SimpleNamespace(archive=AsyncMock(return_value=True)),
            subagents=SimpleNamespace(cancel_by_session=AsyncMock(return_value=0)),
        )
        loop._schedule_background = lambda coro: asyncio.ensure_future(coro)

        msg = InboundMessage(
            channel="telegram",
            sender_id="user1",
            chat_id="111",
            content="/new",
            session_key_override="unified:default",
        )
        ctx = CommandContext(msg=msg, session=None, key="unified:default", raw="/new", loop=loop)
        await cmd_new(ctx)

        sessions.invalidate("unified:default")
        sessions.invalidate("discord:999")
        assert sessions.get_or_create("unified:default").messages == []
        assert len(sessions.get_or_create("discord:999").messages) == 1


# ---------------------------------------------------------------------------
# TestConsolidationUnaffectedByUnifiedSession — consolidation is key-agnostic
# ---------------------------------------------------------------------------


class TestConsolidationUnaffectedByUnifiedSession:
    """maybe_consolidate_by_tokens() behaviour is identical regardless of session key."""

    @pytest.mark.asyncio
    async def test_consolidation_skips_empty_session_for_unified_key(self):
        """Empty unified:default session → consolidation exits immediately, archive not called."""
        from miniunicorn.agent.memory import Consolidator, MemoryStore

        store = MagicMock(spec=MemoryStore)
        mock_provider = MagicMock()
        mock_provider.chat_with_retry = AsyncMock(return_value=MagicMock(content="summary"))
        # Use spec= so MagicMock doesn't auto-generate AsyncMock for non-async methods,
        # which would leave unawaited coroutines and trigger RuntimeWarning.
        sessions = MagicMock(spec=SessionManager)

        consolidator = Consolidator(
            store=store,
            provider=mock_provider,
            model="test-model",
            sessions=sessions,
            context_window_tokens=1000,
            build_messages=MagicMock(return_value=[]),
            get_tool_definitions=MagicMock(return_value=[]),
            max_completion_tokens=100,
        )
        consolidator.archive = AsyncMock()

        session = Session(key="unified:default")
        session.messages = []

        await consolidator.maybe_consolidate_by_tokens(session)

        consolidator.archive.assert_not_called()

    @pytest.mark.asyncio
    async def test_consolidation_behaviour_identical_for_any_key(self):
        """archive call count is the same for 'telegram:123' and 'unified:default'
        under identical token conditions."""
        from miniunicorn.agent.memory import Consolidator, MemoryStore

        archive_calls: dict[str, int] = {}

        for key in ("telegram:123", "unified:default"):
            store = MagicMock(spec=MemoryStore)
            mock_provider = MagicMock()
            mock_provider.chat_with_retry = AsyncMock(return_value=MagicMock(content="summary"))
            sessions = MagicMock(spec=SessionManager)

            consolidator = Consolidator(
                store=store,
                provider=mock_provider,
                model="test-model",
                sessions=sessions,
                context_window_tokens=1000,
                build_messages=MagicMock(return_value=[]),
                get_tool_definitions=MagicMock(return_value=[]),
                max_completion_tokens=100,
            )

            session = Session(key=key)
            session.messages = []  # empty → exits immediately for both keys

            consolidator.archive = AsyncMock()
            await consolidator.maybe_consolidate_by_tokens(session)
            archive_calls[key] = consolidator.archive.call_count

        assert archive_calls["telegram:123"] == archive_calls["unified:default"] == 0

    @pytest.mark.asyncio
    async def test_consolidation_triggers_when_over_budget_unified_key(self):
        """When tokens exceed budget, consolidation attempts to find a boundary —
        behaviour is identical to any other session key."""
        from miniunicorn.agent.memory import Consolidator, MemoryStore

        store = MagicMock(spec=MemoryStore)
        mock_provider = MagicMock()
        sessions = MagicMock(spec=SessionManager)

        consolidator = Consolidator(
            store=store,
            provider=mock_provider,
            model="test-model",
            sessions=sessions,
            context_window_tokens=1000,
            build_messages=MagicMock(return_value=[]),
            get_tool_definitions=MagicMock(return_value=[]),
            max_completion_tokens=100,
        )

        session = Session(key="unified:default")
        session.messages = [{"role": "user", "content": "msg"}]
        sessions.get_or_create.return_value = session

        # Simulate over-budget: estimated > budget
        consolidator.estimate_session_prompt_tokens = MagicMock(return_value=(950, "tiktoken"))
        # No valid boundary found → returns gracefully without archiving
        consolidator.pick_consolidation_boundary = MagicMock(return_value=None)
        consolidator.archive = AsyncMock()

        await consolidator.maybe_consolidate_by_tokens(session)

        # estimate was called (consolidation was attempted)
        consolidator.estimate_session_prompt_tokens.assert_called_once_with(
            session,
        )
        # but archive was not called (no valid boundary)
        consolidator.archive.assert_not_called()


# ---------------------------------------------------------------------------
# TestStopCommandWithUnifiedSession — /stop command integration
# ---------------------------------------------------------------------------


class TestStopCommandWithUnifiedSession:
    """Verify /stop command works correctly with unified session enabled."""

    @pytest.mark.asyncio
    async def test_effective_key_is_unified_in_unified_mode(self, tmp_path: Path):
        """When unified_session=True, the effective session key is UNIFIED_SESSION_KEY."""
        loop = _make_loop(tmp_path, unified_session=True)
        msg = _make_msg(channel="telegram", chat_id="123456")
        key = loop._effective_session_key(msg)
        assert key == UNIFIED_SESSION_KEY

    @pytest.mark.asyncio
    async def test_stop_command_finds_task_in_unified_mode(self, tmp_path: Path):
        """cmd_stop cancels subagents using the effective unified key."""
        from miniunicorn.command.builtin import cmd_stop

        loop = _make_loop(tmp_path, unified_session=True)
        loop.subagents.cancel_by_session = AsyncMock(return_value=1)  # type: ignore[method-assign]

        msg = InboundMessage(
            channel="telegram",
            chat_id="123456",
            sender_id="user1",
            content="/stop",
            session_key_override=UNIFIED_SESSION_KEY,
        )
        key = loop._effective_session_key(msg)
        ctx = CommandContext(msg=msg, session=None, key=key, raw="/stop", loop=loop)
        result = await cmd_stop(ctx)

        loop.subagents.cancel_by_session.assert_awaited_once_with(UNIFIED_SESSION_KEY)
        assert result.content == "Stopped 1 subagent(s)."

    @pytest.mark.asyncio
    async def test_stop_command_uses_effective_key_without_session_override(self, tmp_path: Path):
        """Priority /stop cancels the unified session even without an explicit override."""
        from miniunicorn.command.builtin import cmd_stop

        loop = _make_loop(tmp_path, unified_session=True)
        loop.subagents.cancel_by_session = AsyncMock(return_value=1)  # type: ignore[method-assign]

        msg = InboundMessage(
            channel="telegram",
            chat_id="123456",
            sender_id="user1",
            content="/stop",
        )
        key = loop._effective_session_key(msg)
        ctx = CommandContext(msg=msg, session=None, key=key, raw="/stop", loop=loop)
        result = await cmd_stop(ctx)

        loop.subagents.cancel_by_session.assert_awaited_once_with(UNIFIED_SESSION_KEY)
        assert result.content == "Stopped 1 subagent(s)."

    @pytest.mark.asyncio
    async def test_stop_command_cross_channel_in_unified_mode(self, tmp_path: Path):
        """In unified mode, /stop from one channel cancels subagents from another channel."""
        from miniunicorn.command.builtin import cmd_stop

        loop = _make_loop(tmp_path, unified_session=True)
        loop.subagents.cancel_by_session = AsyncMock(return_value=2)  # type: ignore[method-assign]

        msg = InboundMessage(
            channel="discord",
            chat_id="789012",
            sender_id="user2",
            content="/stop",
            session_key_override=UNIFIED_SESSION_KEY,
        )
        key = loop._effective_session_key(msg)
        ctx = CommandContext(msg=msg, session=None, key=key, raw="/stop", loop=loop)
        result = await cmd_stop(ctx)

        loop.subagents.cancel_by_session.assert_awaited_once_with(UNIFIED_SESSION_KEY)
        assert result.content == "Stopped 2 subagent(s)."
