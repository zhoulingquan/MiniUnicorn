"""Structured-mode Dream tests: strict extraction, fail-closed cursors, idempotent retry."""

from __future__ import annotations

import json
import re
from unittest.mock import AsyncMock, MagicMock

import pytest

from miniunicorn.config.schema import StructuredMemoryConfig
from miniunicorn.memory import Dream, MemoryStore
from miniunicorn.memory.models import MemoryWriteError, ScopeKind


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(
        tmp_path,
        structured_config=StructuredMemoryConfig(
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
        for index, lesson in enumerate(lines, 1):
            f.write(
                json.dumps(
                    {
                        "timestamp": "2026-08-11 08:30",
                        "trigger": "tool_error",
                        "iteration": 3,
                        "context": "boom",
                        "reflection": lesson,
                        "lesson": lesson,
                        "reflection_id": f"rfl_{index:032x}",
                        "session_key": "test",
                    }
                )
                + "\n"
            )


def write_history_direct(store, cursor: int, timestamp: str, content: str, **identity):
    record = {"cursor": cursor, "timestamp": timestamp, "content": content}
    record.update({k: v for k, v in identity.items() if v})
    with open(store.history_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_reflection_entry(store, entry: dict):
    rf = store.memory_dir / "reflections.jsonl"
    rf.parent.mkdir(parents=True, exist_ok=True)
    with open(rf, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def reflection_entry(
    lesson: str, ref_id: str, *, session_key="test", user_key=None, timestamp="2026-08-11 08:30"
):
    entry = {
        "timestamp": timestamp,
        "trigger": "tool_error",
        "iteration": 3,
        "context": "boom",
        "reflection": lesson,
        "lesson": lesson,
        "reflection_id": ref_id,
        "session_key": session_key,
    }
    if user_key:
        entry["user_key"] = user_key
    return entry


def seed_record(store, scope_kind: ScopeKind, scope_key: str, statement: str) -> None:
    from datetime import datetime, timezone

    from miniunicorn.memory.extraction import parse_extraction_batch
    from miniunicorn.memory.lifecycle import IngestContext
    from miniunicorn.memory.models import (
        ActorKind,
        EvidenceKind,
        EvidenceRef,
        MemoryScope,
    )

    now = datetime.now(timezone.utc)
    evidence_catalog = {
        "history:seed": EvidenceRef(
            kind=EvidenceKind.HISTORY,
            ref="history:seed",
            excerpt=statement,
            observed_at=now,
        )
    }
    extracted = parse_extraction_batch(
        raw_batch(
            proposal(
                scope_hint=scope_kind.value, statement=statement, evidence_refs=["history:seed"]
            )
        ),
        evidence_catalog,
        store.structured_repository.tag_catalog,
        allowed_scope_hints={ScopeKind.PROJECT, ScopeKind.SHARED, scope_kind},
    )
    ctx = IngestContext(
        actor=ActorKind.DREAM,
        reason="test seed",
        source_batch=f"seed:{statement}",
        scope=MemoryScope(kind=scope_kind, key=scope_key),
        evidence_catalog=evidence_catalog,
        now=now,
    )
    result = store.structured_lifecycle.ingest(extracted.proposals[0], ctx)
    store.structured_lifecycle.promote(
        result.candidate_id,
        actor=ActorKind.SYSTEM,
        reason="test seed promote",
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

    async def test_partitioned_identity_scope_hint_uses_first_partition(
        self, store, dream, mock_provider
    ):
        store.append_history("Alice fact.", session_key="web:chat-7", user_key="user:alice")
        store.append_history("Bob fact.", session_key="web:chat-8", user_key="user:bob")
        set_provider_response(mock_provider, raw_batch(proposal(scope_hint="user")))

        assert await dream.run() is True
        assert store.get_last_dream_cursor() == 1
        record = all_records(store)[0]
        assert record.scope.kind is ScopeKind.USER
        assert record.scope.key == "user:alice"


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
    async def test_reflection_only_batch_consumes_reflections(self, store, dream, mock_provider):
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
    def test_dream_has_no_direct_file_mutation_tools(self, dream):
        assert not hasattr(dream, "_tools")
        assert not hasattr(dream, "_build_tools")

    def test_default_store_also_has_no_direct_file_mutation_tools(self, tmp_path, mock_provider):
        dream = Dream(
            store=MemoryStore(tmp_path),
            provider=mock_provider,
            model="test-model",
            max_batch_size=5,
        )
        assert not hasattr(dream, "_tools")


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

    async def test_reflection_without_governed_id_is_not_sent_to_model(
        self, store, dream, mock_provider
    ):
        rf = store.memory_dir / "reflections.jsonl"
        rf.parent.mkdir(parents=True, exist_ok=True)
        rf.write_text(
            json.dumps(
                {
                    "timestamp": "2026-08-11 08:30",
                    "trigger": "tool_error",
                    "iteration": 3,
                    "reflection": "Untrusted record without an application ID.",
                    "lesson": "Untrusted record without an application ID.",
                    "session_key": "test",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        result = await dream.run()

        assert result is True
        mock_provider.chat_with_retry.assert_not_awaited()
        assert store.get_last_reflections_cursor() == 0
        assert not rf.exists() or not rf.read_text(encoding="utf-8").strip()


class TestDreamSourceBatch:
    def test_evidence_set_batch_is_order_stable(self):
        from miniunicorn.memory import _dream_source_batch

        refs = ["history:7", "reflection:rfl_abc"]
        assert _dream_source_batch(refs) == _dream_source_batch(reversed(refs))
        assert re.fullmatch(r"dream:[0-9a-f]{24}", _dream_source_batch(refs))

    def test_evidence_set_batch_changes_when_refs_change(self):
        from miniunicorn.memory import _dream_source_batch

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

    async def test_consistent_identity_opens_all_fine_scopes(self, store, dream, mock_provider):
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

    async def test_mixed_users_are_partitioned_with_exact_user_scope(
        self, store, dream, mock_provider
    ):
        store.append_history("Alice fact.", session_key="web:chat-7", user_key="user:alice")
        store.append_history("Bob fact.", session_key="web:chat-8", user_key="user:bob")
        set_provider_response(mock_provider, '{"schema_version":1,"proposals":[]}')

        assert await dream.run() is True

        system_prompt = mock_provider.chat_with_retry.await_args_list[0].kwargs["messages"][0][
            "content"
        ]
        assert self.allowed_scope_line(system_prompt) == (
            "Allowed scope_hint values for this batch: project, shared, session, user."
        )
        user_prompt = mock_provider.chat_with_retry.await_args_list[0].kwargs["messages"][1][
            "content"
        ]
        assert "Alice fact." in user_prompt
        assert "Bob fact." not in user_prompt

    async def test_mixed_sessions_are_partitioned_with_exact_session_scope(
        self, store, dream, mock_provider
    ):
        store.append_history("Alice fact.", session_key="web:chat-7", user_key="user:alice")
        store.append_history("Bob fact.", session_key="web:chat-8", user_key="user:bob")
        set_provider_response(mock_provider, '{"schema_version":1,"proposals":[]}')

        assert await dream.run() is True

        system_prompt = mock_provider.chat_with_retry.await_args_list[0].kwargs["messages"][0][
            "content"
        ]
        assert "session" in self.allowed_scope_line(system_prompt)
        assert store.get_last_dream_cursor() == 1

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

    async def test_different_parent_sessions_are_partitioned(self, store, dream, mock_provider):
        store.append_history("Chat A subagent fact.", session_key="web:chat#sub:a")
        store.append_history("Chat B subagent fact.", session_key="web:other#sub:a")
        set_provider_response(mock_provider, '{"schema_version":1,"proposals":[]}')

        assert await dream.run() is True

        system_prompt = mock_provider.chat_with_retry.await_args_list[0].kwargs["messages"][0][
            "content"
        ]
        assert self.allowed_scope_line(system_prompt) == (
            "Allowed scope_hint values for this batch: project, shared, session."
        )
        user_prompt = mock_provider.chat_with_retry.await_args_list[0].kwargs["messages"][1][
            "content"
        ]
        assert "Chat A subagent fact." in user_prompt
        assert "Chat B subagent fact." not in user_prompt

    async def test_known_and_identityless_sessions_are_partitioned(
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
            "Allowed scope_hint values for this batch: project, shared, session."
        )
        user_prompt = mock_provider.chat_with_retry.await_args_list[0].kwargs["messages"][1][
            "content"
        ]
        assert "Namespaced fact." in user_prompt
        assert "Identity-less fact." not in user_prompt

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


# ---------------------------------------------------------------------------
# Governed-memory Dream partitioning: exact identity tuples are drained
# one partition at a time, cursor-safely, across repeated runs.
# ---------------------------------------------------------------------------


class TestPartitionedBatches:
    async def test_mixed_user_batches_drain_by_partition(self, store, dream, mock_provider):
        store.append_history("Alice fact.", session_key="web:chat-7", user_key="user:alice")
        store.append_history("Bob fact.", session_key="web:chat-8", user_key="user:bob")
        set_provider_response(mock_provider, '{"schema_version":1,"proposals":[]}')

        assert await dream.run() is True
        first_prompt = mock_provider.chat_with_retry.await_args_list[0].kwargs["messages"][1][
            "content"
        ]
        assert "Alice fact." in first_prompt
        assert "Bob fact." not in first_prompt
        assert store.get_last_dream_cursor() == 1

        assert await dream.run() is True
        second_prompt = mock_provider.chat_with_retry.await_args_list[1].kwargs["messages"][1][
            "content"
        ]
        assert "Bob fact." in second_prompt
        assert "Alice fact." not in second_prompt
        assert store.get_last_dream_cursor() == 2

    async def test_interleaved_partitions_drain_exactly_once_across_runs(
        self, store, dream, mock_provider
    ):
        store.append_history("Alice fact one.", session_key="web:chat-7", user_key="user:alice")
        store.append_history("Bob fact.", session_key="web:chat-8", user_key="user:bob")
        store.append_history("Alice fact two.", session_key="web:chat-7", user_key="user:alice")
        set_provider_response(mock_provider, '{"schema_version":1,"proposals":[]}')

        prompts = []
        for _ in range(3):
            assert await dream.run() is True
            prompts.append(
                mock_provider.chat_with_retry.await_args_list[-1].kwargs["messages"][1]["content"]
            )

        assert "Alice fact one." in prompts[0]
        assert "Bob fact." not in prompts[0]
        assert "Bob fact." in prompts[1]
        assert "Alice fact two." not in prompts[1]
        assert "Alice fact two." in prompts[2]
        assert "Bob fact." not in prompts[2]
        assert store.get_last_dream_cursor() == 3
        assert await dream.run() is False

    async def test_history_and_reflection_combine_on_full_identity_match(
        self, store, dream, mock_provider
    ):
        write_history_direct(
            store,
            1,
            "2026-08-10 08:00",
            "Alice fact.",
            session_key="web:chat-7",
            user_key="user:alice",
        )
        write_reflection_entry(
            store,
            reflection_entry(
                "Alice lesson.",
                "rfl_00000000000000000000000000000001",
                session_key="web:chat-7",
                user_key="user:alice",
                timestamp="2026-08-11 08:30",
            ),
        )
        set_provider_response(mock_provider, '{"schema_version":1,"proposals":[]}')

        assert await dream.run() is True
        prompt = mock_provider.chat_with_retry.await_args_list[0].kwargs["messages"][1]["content"]
        assert "Alice fact." in prompt
        assert "Alice lesson." in prompt
        assert store.get_last_dream_cursor() == 1
        assert store.get_last_reflections_cursor() == 0  # consumed then pruned

    async def test_history_and_reflection_stay_split_when_user_keys_differ(
        self, store, dream, mock_provider
    ):
        write_history_direct(
            store,
            1,
            "2026-08-10 08:00",
            "Alice fact.",
            session_key="web:chat-7",
            user_key="user:alice",
        )
        write_reflection_entry(
            store,
            reflection_entry(
                "Bob lesson.",
                "rfl_00000000000000000000000000000001",
                session_key="web:chat-7",
                user_key="user:bob",
                timestamp="2026-08-11 08:30",
            ),
        )
        set_provider_response(mock_provider, '{"schema_version":1,"proposals":[]}')

        # History partition is primary (older timestamp). The reflection
        # belongs to a different user partition and must NOT be consumed.
        assert await dream.run() is True
        first = mock_provider.chat_with_retry.await_args_list[0].kwargs["messages"][1]["content"]
        assert "Alice fact." in first
        assert "Bob lesson." not in first
        assert store.get_last_dream_cursor() == 1
        assert store.get_last_reflections_cursor() == 0

        assert await dream.run() is True
        second = mock_provider.chat_with_retry.await_args_list[1].kwargs["messages"][1]["content"]
        assert "Bob lesson." in second
        assert "Alice fact." not in second
        assert store.get_last_reflections_cursor() == 0  # consumed then pruned

    async def test_oldest_pending_partition_wins(self, store, dream, mock_provider):
        write_history_direct(
            store,
            1,
            "2026-08-10 08:00",
            "Alice fact.",
            session_key="web:chat-7",
            user_key="user:alice",
        )
        write_reflection_entry(
            store,
            reflection_entry(
                "Bob lesson.",
                "rfl_00000000000000000000000000000001",
                session_key="web:chat-8",
                user_key="user:bob",
                timestamp="2026-08-09 08:00",
            ),
        )
        set_provider_response(mock_provider, '{"schema_version":1,"proposals":[]}')

        # The older reflection (Bob) is primary and drains alone.
        assert await dream.run() is True
        first = mock_provider.chat_with_retry.await_args_list[0].kwargs["messages"][1]["content"]
        assert "Bob lesson." in first
        assert "Alice fact." not in first
        assert store.get_last_dream_cursor() == 0

        assert await dream.run() is True
        second = mock_provider.chat_with_retry.await_args_list[1].kwargs["messages"][1]["content"]
        assert "Alice fact." in second
        assert "Bob lesson." not in second

    async def test_max_batch_size_caps_total_evidence_across_sources(self, store, mock_provider):
        dream = Dream(store=store, provider=mock_provider, model="test-model", max_batch_size=5)
        for i in range(1, 5):
            write_history_direct(
                store,
                i,
                f"2026-08-10 08:0{i}",
                f"History fact {i}.",
                session_key="web:chat-7",
                user_key="user:alice",
            )
        for i in range(1, 5):
            write_reflection_entry(
                store,
                reflection_entry(
                    f"Reflection fact {i}.",
                    f"rfl_{i:032x}",
                    session_key="web:chat-7",
                    user_key="user:alice",
                    timestamp="2026-08-11 08:30",
                ),
            )
        set_provider_response(mock_provider, '{"schema_version":1,"proposals":[]}')

        assert await dream.run() is True
        first = mock_provider.chat_with_retry.await_args_list[0].kwargs["messages"][1]["content"]
        assert first.count("[history:") == 4
        assert first.count("[reflection:") == 1
        assert store.get_last_dream_cursor() == 4

        assert await dream.run() is True
        second = mock_provider.chat_with_retry.await_args_list[1].kwargs["messages"][1]["content"]
        assert second.count("[reflection:") == 3
        assert "[history:" not in second

    async def test_invalid_reflection_quarantined_before_valid_other_partition(
        self, store, dream, mock_provider
    ):
        write_history_direct(
            store,
            1,
            "2026-08-10 08:00",
            "Alice fact.",
            session_key="web:chat-7",
            user_key="user:alice",
        )
        write_reflection_entry(
            store,
            {
                "timestamp": "2026-08-11 08:30",
                "trigger": "tool_error",
                "iteration": 1,
                "reflection": "Untrusted without a governed id.",
                "lesson": "Untrusted without a governed id.",
                "session_key": "web:chat-7",
                "user_key": "user:alice",
            },
        )
        write_reflection_entry(
            store,
            reflection_entry(
                "Bob lesson.",
                "rfl_00000000000000000000000000000002",
                session_key="web:chat-8",
                user_key="user:bob",
                timestamp="2026-08-11 08:30",
            ),
        )
        set_provider_response(mock_provider, '{"schema_version":1,"proposals":[]}')

        # Primary = Alice history partition. The leading invalid reflection is
        # quarantined (cursor advances over it) but Bob's reflection, in a
        # different partition, is NOT consumed.
        assert await dream.run() is True
        first = mock_provider.chat_with_retry.await_args_list[0].kwargs["messages"][1]["content"]
        assert "Alice fact." in first
        assert "Bob lesson." not in first
        assert store.get_last_dream_cursor() == 1
        remaining = store.read_unprocessed_reflections(since_cursor=0)
        assert [e.get("lesson") for e in remaining] == ["Bob lesson."]

        assert await dream.run() is True
        second = mock_provider.chat_with_retry.await_args_list[1].kwargs["messages"][1]["content"]
        assert "Bob lesson." in second
        assert "Alice fact." not in second
        assert store.read_unprocessed_reflections(since_cursor=0) == []


class TestDreamBounds:
    async def test_tiny_budget_keeps_prompt_catalog_batch_and_cursor_in_sync(
        self, store, mock_provider, monkeypatch
    ):
        from miniunicorn.memory import _dream_source_batch
        from miniunicorn.memory.models import MemoryScope
        from miniunicorn.utils.helpers import estimate_message_tokens

        store.append_history("First governed fact.")
        store.append_history("Second governed fact.")
        dream = Dream(
            store=store,
            provider=mock_provider,
            model="test-model",
            max_batch_size=5,
            max_completion_tokens=64,
        )
        history = store.read_unprocessed_history(since_cursor=0)
        allowed_scopes = {MemoryScope(kind=ScopeKind.PROJECT, key=store.project_scope_key)}
        minimum_one_evidence_prompt = dream._render_user_prompt(
            store.structured_repository,
            history[:1],
            [],
            allowed_scopes,
            history_preview=128,
            reflection_preview=128,
            include_summaries=False,
        )
        prompt_budget = estimate_message_tokens(
            {"role": "user", "content": minimum_one_evidence_prompt}
        )
        dream.context_window_tokens = (
            dream.max_completion_tokens + dream._PROMPT_SAFETY_TOKENS + prompt_budget
        )

        captured_contexts = []
        real_ingest = store.structured_lifecycle.ingest

        def capture_ingest(proposal_obj, context):
            captured_contexts.append(context)
            return real_ingest(proposal_obj, context)

        async def respond_with_visible_evidence(**kwargs):
            user_prompt = kwargs["messages"][1]["content"]
            visible_refs = re.findall(r"\[(history:\d+)\s*\|", user_prompt)
            return MagicMock(content=raw_batch(proposal(evidence_refs=[visible_refs[0]])))

        monkeypatch.setattr(store.structured_lifecycle, "ingest", capture_ingest)
        mock_provider.chat_with_retry.side_effect = respond_with_visible_evidence

        assert await dream.run() is True

        sent_prompt = mock_provider.chat_with_retry.await_args.kwargs["messages"][1]["content"]
        sent_refs = re.findall(r"\[(history:\d+)\s*\|", sent_prompt)
        assert sent_refs == ["history:1"]
        assert store.get_last_dream_cursor() == 1
        assert len(captured_contexts) == 1
        assert set(captured_contexts[0].evidence_catalog) == set(sent_refs)
        assert captured_contexts[0].source_batch == _dream_source_batch(sent_refs)

    async def test_non_positive_prompt_budget_does_not_call_provider_or_advance(
        self, store, mock_provider
    ):
        store.append_history("Must remain pending.")
        dream = Dream(
            store=store,
            provider=mock_provider,
            model="test-model",
            context_window_tokens=1,
            max_completion_tokens=512,
        )

        assert await dream.run() is False

        mock_provider.chat_with_retry.assert_not_awaited()
        assert store.get_last_dream_cursor() == 0
        assert store.get_last_reflections_cursor() == 0

    async def test_budget_too_small_for_evidence_body_defers_batch(self, store, mock_provider):
        from miniunicorn.memory.models import MemoryScope
        from miniunicorn.utils.helpers import estimate_message_tokens

        store.append_history("The model must receive this governed fact.")
        dream = Dream(
            store=store,
            provider=mock_provider,
            model="test-model",
            max_completion_tokens=64,
        )
        history = store.read_unprocessed_history(since_cursor=0)
        allowed_scopes = {MemoryScope(kind=ScopeKind.PROJECT, key=store.project_scope_key)}
        empty_body_prompt = dream._render_user_prompt(
            store.structured_repository,
            history,
            [],
            allowed_scopes,
            history_preview=0,
            reflection_preview=0,
            include_summaries=False,
        )
        empty_body_budget = estimate_message_tokens({"role": "user", "content": empty_body_prompt})
        dream.context_window_tokens = (
            dream.max_completion_tokens + dream._PROMPT_SAFETY_TOKENS + empty_body_budget
        )

        assert await dream.run() is False

        mock_provider.chat_with_retry.assert_not_awaited()
        assert store.get_last_dream_cursor() == 0

    async def test_budget_shrink_does_not_skip_valid_reflection_after_invalid(
        self, store, mock_provider
    ):
        from miniunicorn.memory.models import MemoryScope
        from miniunicorn.utils.helpers import estimate_message_tokens

        write_history_direct(
            store,
            1,
            "2026-08-10 08:00",
            "History evidence that fits alone.",
            session_key="web:chat-7",
            user_key="user:alice",
        )
        write_reflection_entry(
            store,
            {
                "timestamp": "2026-08-11 08:00",
                "lesson": "Invalid reflection.",
                "session_key": "web:chat-7",
                "user_key": "user:alice",
            },
        )
        valid_reflection_id = "rfl_00000000000000000000000000000002"
        write_reflection_entry(
            store,
            reflection_entry(
                "Valid reflection must remain pending.",
                valid_reflection_id,
                session_key="web:chat-7",
                user_key="user:alice",
                timestamp="2026-08-11 08:01",
            ),
        )
        dream = Dream(
            store=store,
            provider=mock_provider,
            model="test-model",
            max_batch_size=5,
            max_completion_tokens=64,
        )
        history = store.read_unprocessed_history(since_cursor=0)
        allowed_scopes = {
            MemoryScope(kind=ScopeKind.PROJECT, key=store.project_scope_key),
            MemoryScope(kind=ScopeKind.SESSION, key="session:web:chat-7"),
            MemoryScope(kind=ScopeKind.USER, key="user:alice"),
        }
        minimum_history_prompt = dream._render_user_prompt(
            store.structured_repository,
            history,
            [],
            allowed_scopes,
            history_preview=128,
            reflection_preview=128,
            include_summaries=False,
        )
        prompt_budget = estimate_message_tokens({"role": "user", "content": minimum_history_prompt})
        dream.context_window_tokens = (
            dream.max_completion_tokens + dream._PROMPT_SAFETY_TOKENS + prompt_budget
        )
        set_provider_response(mock_provider, '{"schema_version":1,"proposals":[]}')

        assert await dream.run() is True

        sent_prompt = mock_provider.chat_with_retry.await_args.kwargs["messages"][1]["content"]
        assert "history:1" in sent_prompt
        assert valid_reflection_id not in sent_prompt
        assert store.get_last_dream_cursor() == 1
        remaining = store.read_unprocessed_reflections(since_cursor=0)
        assert [entry.get("reflection_id") for entry in remaining] == [valid_reflection_id]

    async def test_evidence_excerpt_truncated_to_1000_chars(self, store, dream, mock_provider):
        store.append_history("A" * 5000)
        set_provider_response(mock_provider, raw_batch(proposal(evidence_refs=["history:1"])))

        assert await dream.run() is True

        record = all_records(store)[0]
        assert record.evidence[0].ref == "history:1"
        assert len(record.evidence[0].excerpt) <= 1000

    async def test_oversized_reflection_excerpt_truncated(self, store, dream, mock_provider):
        ref_id = "rfl_0123456789abcdef0123456789abcdef"
        write_reflection_entry(
            store,
            reflection_entry("L" * 5000, ref_id, session_key="test"),
        )
        set_provider_response(
            mock_provider,
            raw_batch(
                proposal(
                    kind="procedure",
                    slot="verification.evidence",
                    statement="Keep evidence excerpts short.",
                    evidence_refs=[f"reflection:{ref_id}"],
                    speech_act="repeated_experience",
                )
            ),
        )

        assert await dream.run() is True
        record = all_records(store)[0]
        assert len(record.evidence[0].excerpt) <= 1000

    async def test_prompt_respects_token_budget(self, store, mock_provider):
        dream = Dream(
            store=store,
            provider=mock_provider,
            model="test-model",
            max_batch_size=5,
            context_window_tokens=2000,
            max_completion_tokens=512,
        )
        from miniunicorn.utils.helpers import estimate_message_tokens

        store.append_history("A" * 4000)
        set_provider_response(mock_provider, raw_batch(proposal()))

        assert await dream.run() is True

        prompt = mock_provider.chat_with_retry.await_args_list[0].kwargs["messages"][1]["content"]
        budget = 2000 - 512 - 1024
        assert estimate_message_tokens({"role": "user", "content": prompt}) <= budget
        assert store.get_last_dream_cursor() == 1

    async def test_token_budget_not_enforced_when_window_unknown(self, store, dream, mock_provider):
        store.append_history("A" * 4000)
        set_provider_response(mock_provider, raw_batch(proposal()))

        assert await dream.run() is True

        prompt = mock_provider.chat_with_retry.await_args_list[0].kwargs["messages"][1]["content"]
        assert "AAAA" in prompt

    async def test_summary_excludes_other_user_records(self, store, dream, mock_provider):
        seed_record(store, ScopeKind.USER, "user:alice", "ALICE USER FACT")
        seed_record(store, ScopeKind.USER, "user:bob", "BOB USER FACT")
        store.append_history("Alice fact.", session_key="web:chat-7", user_key="user:alice")
        set_provider_response(mock_provider, '{"schema_version":1,"proposals":[]}')

        assert await dream.run() is True

        prompt = mock_provider.chat_with_retry.await_args_list[0].kwargs["messages"][1]["content"]
        assert "ALICE USER FACT" in prompt
        assert "BOB USER FACT" not in prompt

    async def test_identity_less_summary_shows_only_workspace_scopes(
        self, store, dream, mock_provider
    ):
        seed_record(store, ScopeKind.PROJECT, store.project_scope_key, "PROJECT FACT")
        seed_record(store, ScopeKind.USER, "user:alice", "ALICE USER FACT")
        store.append_history("Identity-less history.")
        set_provider_response(mock_provider, '{"schema_version":1,"proposals":[]}')

        assert await dream.run() is True

        prompt = mock_provider.chat_with_retry.await_args_list[0].kwargs["messages"][1]["content"]
        assert "PROJECT FACT" in prompt
        assert "ALICE USER FACT" not in prompt


class TestAuditExportTrigger:
    async def test_successful_dream_batch_exports_audit(self, store, dream, mock_provider):
        store.append_history("Main uses deterministic structured recall.")
        set_provider_response(mock_provider, raw_batch(proposal()))

        assert await dream.run() is True

        stats = store.structured_repository.storage_stats()
        assert stats.transaction_count >= 1
        assert stats.audit_lag == 0
        audit = store.workspace / "memory" / "structured" / "audit"
        manifest = json.loads((audit / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["database_last_tx_seq"] == stats.last_transaction_seq

    async def test_failed_dream_batch_does_not_export(self, store, dream, mock_provider):
        store.append_history("A fact to ingest.")
        set_provider_response(mock_provider, '{"schema_version":1,"proposals":[{"bad"]}')

        assert await dream.run() is False
        assert store.structured_repository.storage_stats().transaction_count == 0
        assert not (store.workspace / "memory" / "structured" / "audit").exists()


class TestProviderResponseGuards:
    """Phase-1 fails closed on truncated/error provider responses and
    tolerates models that omit the top-level schema_version key."""

    async def test_truncated_response_fails_before_parse(
        self, store, dream, mock_provider, monkeypatch
    ):
        import miniunicorn.memory.extraction as extraction_module

        store.append_history("A fact that must not be half-parsed.")
        mock_provider.chat_with_retry.return_value = MagicMock(
            content=raw_batch(proposal()), finish_reason="length"
        )
        parse_spy = MagicMock()
        monkeypatch.setattr(extraction_module, "parse_extraction_batch", parse_spy)

        assert await dream.run() is False

        parse_spy.assert_not_called()
        assert store.get_last_dream_cursor() == 0
        assert all_records(store) == ()

    async def test_missing_schema_version_batch_still_succeeds(self, store, dream, mock_provider):
        store.append_history("Main uses deterministic structured recall.")
        set_provider_response(mock_provider, json.dumps({"proposals": [proposal()]}))

        assert await dream.run() is True

        assert store.get_last_dream_cursor() == 1
        assert len(all_records(store)) == 1

    async def test_extraction_failure_logs_raw_sample(self, store, dream, mock_provider):
        from loguru import logger as loguru_logger

        store.append_history("A fact whose extraction will fail.")
        bad_raw = '{"schema_version": 1, "proposa": []}'
        set_provider_response(mock_provider, bad_raw)

        records: list = []
        handler_id = loguru_logger.add(lambda m: records.append(m), level="WARNING")
        try:
            result = await dream.run()
        finally:
            loguru_logger.remove(handler_id)

        assert result is False
        assert store.get_last_dream_cursor() == 0
        joined = "".join(str(record) for record in records)
        assert "code=extraction_error" in joined
        assert "raw=" in joined
        assert bad_raw in joined
