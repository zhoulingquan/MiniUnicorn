"""Structured-mode Dream tests: strict extraction, fail-closed cursors, idempotent retry."""

from __future__ import annotations

import json
import re
from unittest.mock import AsyncMock, MagicMock

import pytest

from miniunicorn.agent.memory import Dream, MemoryStore
from miniunicorn.agent.memory_models import MemoryWriteError, ScopeKind
from miniunicorn.config.schema import StructuredMemoryConfig


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(
        tmp_path,
        structured_config=StructuredMemoryConfig(
            mode="governed",
            auto_promote_verified=True,
        ),
    )
    return s


@pytest.fixture
def mock_provider():
    p = MagicMock()
    p.chat_with_retry = AsyncMock()
    return p


@pytest.fixture
def dream(store, mock_provider):
    return Dream(store=store, provider=mock_provider, model="test-model", max_batch_size=5)


def proposal(**overrides):
    p = {
        "proposal_index": 0,
        "kind": "decision",
        "scope_hint": "project",
        "subject": "MiniUnicorn",
        "slot": "memory.retrieval.strategy",
        "statement": "Main uses deterministic structured recall.",
        "detail": "No embeddings are used.",
        "tags": ["architecture.memory", "project.decision"],
        "aliases": [],
        "confidence": 1.0,
        "importance": 5,
        "evidence_refs": ["history:1"],
        "speech_act": "confirmed_decision",
        "expires_at": None,
    }
    p.update(overrides)
    return p


def raw_batch(*proposals):
    return json.dumps({"schema_version": 1, "proposals": list(proposals)})


def set_provider_response(mock_provider, raw: str):
    mock_provider.chat_with_retry.return_value = MagicMock(content=raw)


def write_reflections(store, lines: list[str]):
    rf = store.memory_dir / "reflections.jsonl"
    rf.parent.mkdir(parents=True, exist_ok=True)
    with open(rf, "w", encoding="utf-8") as f:
        for lesson in lines:
            f.write(
                json.dumps(
                    {
                        "timestamp": "2026-08-11 08:30",
                        "trigger": "tool_error",
                        "iteration": 3,
                        "context": "boom",
                        "reflection": lesson,
                        "session_key": "test",
                    }
                )
                + "\n"
            )


def all_records(store):
    return store.structured_repository.current_records()


class TestStructuredBatchIngest:
    async def test_extracts_and_ingests_proposals(self, store, dream, mock_provider):
        store.append_history("Main uses deterministic structured recall.")
        store.append_history("This is not a fact.")
        set_provider_response(mock_provider, raw_batch(proposal()))

        result = await dream.run()

        assert result is True
        assert store.get_last_dream_cursor() == 2
        records = all_records(store)
        assert len(records) == 1
        assert records[0].statement == "Main uses deterministic structured recall."

    async def test_history_evidence_refs_use_persisted_cursor_across_batches(
        self, store, dream, mock_provider
    ):
        store.append_history("First batch.")
        store.set_last_dream_cursor(1)
        store.append_history("Second batch fact.")
        set_provider_response(
            mock_provider,
            raw_batch(proposal(evidence_refs=["history:2"])),
        )

        result = await dream.run()

        assert result is True
        record = all_records(store)[0]
        assert record.evidence[0].ref == "history:2"

    async def test_valid_empty_batch_advances_cursor_without_records(
        self, store, dream, mock_provider
    ):
        store.append_history("Nothing memorable happened.")
        set_provider_response(mock_provider, '{"schema_version":1,"proposals":[]}')

        result = await dream.run()

        assert result is True
        assert store.get_last_dream_cursor() == 1
        assert all_records(store) == ()

    async def test_noop_when_nothing_unprocessed(self, store, dream, mock_provider):
        result = await dream.run()
        assert result is False
        mock_provider.chat_with_retry.assert_not_called()

    @pytest.mark.parametrize("scope_hint", ["session", "user"])
    async def test_maps_identity_scope_hints_to_exact_batch_scope(
        self, store, dream, mock_provider, scope_hint
    ):
        store.append_history(
            "Alice prefers compact answers in this session.",
            session_key="web:chat-7",
            user_key="user:alice",
        )
        set_provider_response(
            mock_provider,
            raw_batch(proposal(scope_hint=scope_hint)),
        )

        assert await dream.run() is True

        record = all_records(store)[0]
        assert record.scope.kind is ScopeKind(scope_hint)
        expected = "session:web:chat-7" if scope_hint == "session" else "user:alice"
        assert record.scope.key == expected

    async def test_rejects_identity_scope_hint_for_mixed_identity_batch(
        self, store, dream, mock_provider
    ):
        store.append_history(
            "Alice fact.", session_key="web:chat-7", user_key="user:alice"
        )
        store.append_history(
            "Bob fact.", session_key="web:chat-8", user_key="user:bob"
        )
        set_provider_response(mock_provider, raw_batch(proposal(scope_hint="user")))

        assert await dream.run() is False
        assert store.get_last_dream_cursor() == 0
        assert all_records(store) == ()


class TestFailClosedCursors:
    async def test_ingest_failure_does_not_advance_cursor(
        self, store, dream, mock_provider, monkeypatch
    ):
        store.append_history("A fact to ingest.")
        set_provider_response(mock_provider, raw_batch(proposal()))

        def boom(*args, **kwargs):
            raise MemoryWriteError("journal locked")

        monkeypatch.setattr(store.structured_lifecycle, "ingest", boom)

        result = await dream.run()

        assert result is False
        assert store.get_last_dream_cursor() == 0
        assert all_records(store) == ()

    async def test_provider_error_does_not_advance_cursor(self, store, dream, mock_provider):
        store.append_history("A fact.")
        mock_provider.chat_with_retry.side_effect = RuntimeError("provider down")

        result = await dream.run()

        assert result is False
        assert store.get_last_dream_cursor() == 0
        assert all_records(store) == ()

    async def test_malformed_extraction_does_not_advance_cursor(self, store, dream, mock_provider):
        store.append_history("A fact.")
        set_provider_response(mock_provider, "nothing new here")

        result = await dream.run()

        assert result is False
        assert store.get_last_dream_cursor() == 0
        assert all_records(store) == ()

    async def test_retry_after_partial_failure_is_idempotent(
        self, store, dream, mock_provider, monkeypatch
    ):
        store.append_history("Fact one.")
        store.append_history("Fact two.")
        proposals = (
            proposal(statement="Fact one is true."),
            proposal(proposal_index=1, statement="Fact two is true."),
        )
        set_provider_response(mock_provider, raw_batch(*proposals))

        real_ingest = store.structured_lifecycle.ingest
        calls = {"n": 0}

        def flaky_ingest(proposal_obj, context):
            calls["n"] += 1
            if calls["n"] == 2:
                raise MemoryWriteError("second proposal fails")
            return real_ingest(proposal_obj, context)

        monkeypatch.setattr(store.structured_lifecycle, "ingest", flaky_ingest)

        first = await dream.run()
        assert first is False
        assert store.get_last_dream_cursor() == 0
        assert len(all_records(store)) == 1

        second = await dream.run()
        assert second is True
        assert store.get_last_dream_cursor() == 2
        assert len(all_records(store)) == 2

    async def test_git_commit_failure_does_not_roll_back(self, store, dream, mock_provider):
        assert store.git.init()
        store.append_history("Fact to commit.")
        set_provider_response(mock_provider, raw_batch(proposal()))
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            store.git, "auto_commit", MagicMock(side_effect=RuntimeError("git down"))
        )
        try:
            result = await dream.run()
        finally:
            monkeypatch.undo()

        assert result is True
        assert store.get_last_dream_cursor() == 1
        assert len(all_records(store)) == 1


class TestReflectionOnlyBatches:
    async def test_reflection_only_batch_consumes_reflections(
        self, store, dream, mock_provider
    ):
        write_reflections(store, ["Lesson one.", "Lesson two."])
        set_provider_response(mock_provider, '{"schema_version":1,"proposals":[]}')

        result = await dream.run()

        assert result is True
        assert store.get_last_dream_cursor() == 0
        assert all_records(store) == ()
        rf = store.memory_dir / "reflections.jsonl"
        assert not rf.exists() or rf.read_text(encoding="utf-8").strip() == ""
        assert await dream.run() is False

    async def test_reflection_only_batch_ingests_with_reflection_evidence(
        self, store, dream, mock_provider
    ):
        ref_id = "rfl_0123456789abcdef0123456789abcdef"
        rf = store.memory_dir / "reflections.jsonl"
        rf.parent.mkdir(parents=True, exist_ok=True)
        with open(rf, "w", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "timestamp": "2026-08-11 08:30",
                        "trigger": "tool_error",
                        "iteration": 3,
                        "context": "boom",
                        "reflection": "Always verify evidence refs before citing them.",
                        "lesson": "Always verify evidence refs before citing them.",
                        "reflection_id": ref_id,
                        "session_key": "test",
                    }
                )
                + "\n"
            )
        set_provider_response(
            mock_provider,
            raw_batch(
                proposal(
                    kind="procedure",
                    slot="verification.evidence",
                    statement="Always verify evidence refs before citing them.",
                    evidence_refs=[f"reflection:{ref_id}"],
                    speech_act="repeated_experience",
                )
            ),
        )

        result = await dream.run()

        assert result is True
        records = all_records(store)
        assert len(records) == 1
        assert records[0].evidence[0].ref == f"reflection:{ref_id}"


class TestStructuredToolRegistry:
    def test_structured_mode_omits_edit_file(self, dream):
        tools = dream._build_tools()
        assert tools.get("edit_file") is None
        assert tools.has("read_file")
        assert tools.has("write_file")

    def test_legacy_mode_keeps_edit_file(self, tmp_path, mock_provider):
        legacy = Dream(
            store=MemoryStore(tmp_path),
            provider=mock_provider,
            model="test-model",
            max_batch_size=5,
        )
        tools = legacy._build_tools()
        assert tools.has("edit_file")


# ---------------------------------------------------------------------------
# C2 plan B: exact evidence refs, stable source batches, dynamic identity scopes
# ---------------------------------------------------------------------------


class TestEvidencePromptContract:
    async def test_user_prompt_shows_real_history_cursor_and_citing_it_succeeds(
        self, store, dream, mock_provider
    ):
        for i in range(41):
            store.append_history(f"Prior fact {i}.")
        store.set_last_dream_cursor(41)
        store.append_history("Second batch fact.")
        set_provider_response(
            mock_provider,
            raw_batch(proposal(evidence_refs=["history:42"])),
        )

        result = await dream.run()

        assert result is True
        messages = mock_provider.chat_with_retry.await_args_list[0].kwargs["messages"]
        user_prompt = messages[1]["content"]
        assert "[history:42 |" in user_prompt
        for message in messages:
            assert "history:1" not in message["content"]
        record = all_records(store)[0]
        assert record.evidence[0].ref == "history:42"

    async def test_reflection_prompt_shows_stable_reflection_id_verbatim(
        self, store, dream, mock_provider
    ):
        ref_id = "rfl_0123456789abcdef0123456789abcdef"
        rf = store.memory_dir / "reflections.jsonl"
        rf.parent.mkdir(parents=True, exist_ok=True)
        with open(rf, "w", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "timestamp": "2026-08-11 08:30",
                        "trigger": "tool_error",
                        "iteration": 3,
                        "context": "boom",
                        "reflection": "Verify exact evidence IDs.",
                        "lesson": "Verify exact evidence IDs.",
                        "reflection_id": ref_id,
                        "session_key": "test",
                    }
                )
                + "\n"
            )
        set_provider_response(
            mock_provider,
            raw_batch(
                proposal(
                    kind="procedure",
                    slot="verification.evidence",
                    statement="Verify exact evidence IDs.",
                    evidence_refs=[f"reflection:{ref_id}"],
                    speech_act="repeated_experience",
                )
            ),
        )

        result = await dream.run()

        assert result is True
        user_prompt = mock_provider.chat_with_retry.await_args_list[0].kwargs["messages"][1][
            "content"
        ]
        assert f"[reflection:{ref_id} |" in user_prompt
        record = all_records(store)[0]
        assert record.evidence[0].ref == f"reflection:{ref_id}"


class TestDreamSourceBatch:
    def test_evidence_set_batch_is_order_stable(self):
        from miniunicorn.agent.memory import _dream_source_batch

        refs = ["history:7", "reflection:rfl_abc"]
        assert _dream_source_batch(refs) == _dream_source_batch(reversed(refs))
        assert re.fullmatch(r"dream:[0-9a-f]{24}", _dream_source_batch(refs))

    def test_evidence_set_batch_changes_when_refs_change(self):
        from miniunicorn.agent.memory import _dream_source_batch

        base = _dream_source_batch(["history:1", "history:2"])
        assert _dream_source_batch(["history:1"]) != base
        assert _dream_source_batch(["history:1", "history:2", "history:3"]) != base

    async def test_ingest_retry_reuses_stable_evidence_batch(
        self, store, dream, mock_provider, monkeypatch
    ):
        store.append_history("A stable fact.")
        set_provider_response(mock_provider, raw_batch(proposal()))
        captured: list[str] = []
        real_ingest = store.structured_lifecycle.ingest

        def flaky_ingest(proposal_obj, context):
            captured.append(context.source_batch)
            if len(captured) == 1:
                raise MemoryWriteError("first attempt fails")
            return real_ingest(proposal_obj, context)

        monkeypatch.setattr(store.structured_lifecycle, "ingest", flaky_ingest)

        assert await dream.run() is False
        assert await dream.run() is True

        assert len(captured) == 2
        assert captured[0] == captured[1]
        assert re.fullmatch(r"dream:[0-9a-f]{24}", captured[0]), captured[0]
        assert len(all_records(store)) == 1


class TestDynamicIdentityScopes:
    @staticmethod
    def allowed_scope_line(system_prompt: str) -> str:
        for line in system_prompt.splitlines():
            if "Allowed scope_hint values for this batch" in line:
                return line.strip().lstrip("-").strip()
        raise AssertionError("allowed scope_hint line missing from system prompt")

    async def test_consistent_identity_opens_all_fine_scopes(
        self, store, dream, mock_provider
    ):
        store.append_history(
            "Alice prefers compact answers.",
            session_key="web:chat-7",
            user_key="user:alice",
        )
        set_provider_response(mock_provider, '{"schema_version":1,"proposals":[]}')

        assert await dream.run() is True

        system_prompt = mock_provider.chat_with_retry.await_args_list[0].kwargs["messages"][0][
            "content"
        ]
        assert (
            self.allowed_scope_line(system_prompt)
            == "Allowed scope_hint values for this batch: project, shared, session, user."
        )

    async def test_mixed_users_omit_user_scope(self, store, dream, mock_provider):
        store.append_history("Alice fact.", session_key="web:chat-7", user_key="user:alice")
        store.append_history("Bob fact.", session_key="web:chat-8", user_key="user:bob")
        set_provider_response(mock_provider, '{"schema_version":1,"proposals":[]}')

        assert await dream.run() is True

        system_prompt = mock_provider.chat_with_retry.await_args_list[0].kwargs["messages"][0][
            "content"
        ]
        assert self.allowed_scope_line(system_prompt) == (
            "Allowed scope_hint values for this batch: project, shared."
        )

    async def test_mixed_sessions_omit_session_scope(self, store, dream, mock_provider):
        store.append_history("Alice fact.", session_key="web:chat-7", user_key="user:alice")
        store.append_history("Bob fact.", session_key="web:chat-8", user_key="user:bob")
        set_provider_response(mock_provider, '{"schema_version":1,"proposals":[]}')

        assert await dream.run() is True

        system_prompt = mock_provider.chat_with_retry.await_args_list[0].kwargs["messages"][0][
            "content"
        ]
        assert self.allowed_scope_line(system_prompt) == (
            "Allowed scope_hint values for this batch: project, shared."
        )

    async def test_missing_identity_omits_fine_scopes(self, store, dream, mock_provider):
        store.append_history("A fact without identity.")
        set_provider_response(mock_provider, '{"schema_version":1,"proposals":[]}')

        assert await dream.run() is True

        system_prompt = mock_provider.chat_with_retry.await_args_list[0].kwargs["messages"][0][
            "content"
        ]
        assert self.allowed_scope_line(system_prompt) == (
            "Allowed scope_hint values for this batch: project, shared."
        )

    async def test_subagent_namespaced_sessions_open_parent_session_scope(
        self, store, dream, mock_provider
    ):
        store.append_history("Subagent A fact.", session_key="web:chat#sub:a")
        store.append_history("Subagent B fact.", session_key="web:chat#sub:b")
        set_provider_response(
            mock_provider,
            raw_batch(
                proposal(
                    scope_hint="session",
                    statement="Use deterministic structured recall.",
                    evidence_refs=["history:1", "history:2"],
                )
            ),
        )

        assert await dream.run() is True

        system_prompt = mock_provider.chat_with_retry.await_args_list[0].kwargs["messages"][0][
            "content"
        ]
        allowed = self.allowed_scope_line(system_prompt)
        assert "session" in allowed
        record = all_records(store)[0]
        assert record.scope.kind is ScopeKind.SESSION
        assert record.scope.key == "session:web:chat"

    async def test_different_parent_sessions_omit_session_scope(
        self, store, dream, mock_provider
    ):
        store.append_history("Chat A subagent fact.", session_key="web:chat#sub:a")
        store.append_history("Chat B subagent fact.", session_key="web:other#sub:a")
        set_provider_response(mock_provider, '{"schema_version":1,"proposals":[]}')

        assert await dream.run() is True

        system_prompt = mock_provider.chat_with_retry.await_args_list[0].kwargs["messages"][0][
            "content"
        ]
        assert self.allowed_scope_line(system_prompt) == (
            "Allowed scope_hint values for this batch: project, shared."
        )

    async def test_mixed_session_identity_omits_session_scope(
        self, store, dream, mock_provider
    ):
        store.append_history("Namespaced fact.", session_key="web:chat#sub:a")
        store.append_history("Identity-less fact.")
        set_provider_response(mock_provider, '{"schema_version":1,"proposals":[]}')

        assert await dream.run() is True

        system_prompt = mock_provider.chat_with_retry.await_args_list[0].kwargs["messages"][0][
            "content"
        ]
        assert self.allowed_scope_line(system_prompt) == (
            "Allowed scope_hint values for this batch: project, shared."
        )

    async def test_reflection_only_batch_without_user_key_omits_user_scope(
        self, store, dream, mock_provider
    ):
        write_reflections(store, ["A lesson without user identity."])
        set_provider_response(mock_provider, '{"schema_version":1,"proposals":[]}')

        assert await dream.run() is True

        system_prompt = mock_provider.chat_with_retry.await_args_list[0].kwargs["messages"][0][
            "content"
        ]
        allowed = self.allowed_scope_line(system_prompt)
        assert "user" not in allowed
