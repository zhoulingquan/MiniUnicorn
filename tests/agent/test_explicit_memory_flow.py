"""Flow tests for explicit-memory capture, LLM relation judgment and confirmation."""

import pytest

from miniunicorn.agent.explicit_memory import (
    CaptureIntent,
    ExplicitMemoryJournal,
    ExplicitMemoryService,
    MemoryResolution,
    RelationResult,
)
from miniunicorn.agent.memory_sources import MemorySourceCatalog


@pytest.fixture
def journal(tmp_path):
    journal = ExplicitMemoryJournal(tmp_path)
    journal.append_new("用户喜欢浅色主题", "用户喜欢浅色主题", None)
    return journal


@pytest.fixture
def service(journal):
    return ExplicitMemoryService(journal)


def classifier(kind: str):
    async def _classify(raw_text: str, candidates):
        candidate = candidates[0] if candidates else None
        return RelationResult(
            label=kind,
            candidate_memory_id=candidate.memory_id if candidate else None,
            normalized_fact="用户喜欢深色主题",
            scope=None,
            reason="conflicts with existing preference",
        )

    return _classify


@pytest.mark.parametrize("text,fact", [
    ("/remember call me Alice", "call me Alice"),
    ("/记住 我不吃香菜", "我不吃香菜"),
    ("请记住我喜欢深色主题", "我喜欢深色主题"),
    ("帮我记一下，提交前运行测试", "提交前运行测试"),
    ("remember that I prefer concise answers", "I prefer concise answers"),
])
def test_unambiguous_triggers(text, fact):
    assert ExplicitMemoryService.detect(text) == CaptureIntent("explicit", fact)


@pytest.mark.parametrize("text", [
    "不要记住我刚才的话",
    "他说'请记住我喜欢红色'",
    "什么情况下会说 remember that？",
])
def test_negated_quoted_or_discussed_phrases_do_not_write(text):
    assert ExplicitMemoryService.detect(text).kind != "explicit"


@pytest.mark.asyncio
async def test_conflict_requires_confirmation_before_append(service, journal):
    result = await service.propose("我喜欢深色主题", classifier=classifier("conflict"))
    assert result.action == "confirmation_required"
    assert "浅色主题" in result.user_message and "深色主题" in result.user_message
    assert len(journal.effective()) == 1


@pytest.mark.asyncio
async def test_supplement_saves_a_new_revision(service, journal):
    result = await service.propose("我喜欢深色主题", classifier=classifier("supplement"))
    assert result.action == "saved"
    assert len(journal.effective()) == 2
    assert journal.effective()[-1].normalized_fact == "用户喜欢深色主题"


@pytest.mark.asyncio
async def test_duplicate_writes_nothing(service, journal):
    result = await service.propose("我喜欢浅色主题", classifier=classifier("duplicate"))
    assert result.action == "duplicate"
    assert len(journal.effective()) == 1


@pytest.mark.asyncio
async def test_resolution_update_appends_revision_and_catalog_indexes_it(service, journal):
    proposal = await service.propose("我喜欢深色主题", classifier=classifier("conflict"))
    resolved = await service.resolve(proposal, MemoryResolution("update"))
    assert resolved.action == "saved"
    assert len(journal.effective()) == 1
    effective = journal.effective()[0]
    assert effective.revision == 2
    assert effective.normalized_fact == "用户喜欢深色主题"
    assert len(journal.history(proposal.candidate_memory_id)) == 2
    records = [r for r in MemorySourceCatalog(journal.workspace).scan().records if r.source_type == "explicit"]
    assert len(records) == 1
    assert records[0].source_id == f"explicit:{proposal.candidate_memory_id}"
    assert records[0].source_revision == "2"


@pytest.mark.asyncio
async def test_resolution_keep_writes_nothing(service, journal):
    proposal = await service.propose("我喜欢深色主题", classifier=classifier("conflict"))
    resolved = await service.resolve(proposal, MemoryResolution("keep"))
    assert resolved.action == "ignored"
    assert len(journal.effective()) == 1


@pytest.mark.asyncio
async def test_resolution_both_splits_old_and_new_scopes(service, journal):
    proposal = await service.propose("我喜欢深色主题", classifier=classifier("conflict"))
    resolved = await service.resolve(
        proposal, MemoryResolution("both", old_scope="personal", new_scope="work")
    )
    assert resolved.action == "saved"
    effective = journal.effective()
    assert len(effective) == 2
    by_scope = {row.scope: row for row in effective}
    assert set(by_scope) == {"personal", "work"}
    assert by_scope["work"].revision == 1
    assert len(journal.history(proposal.candidate_memory_id)) == 2


@pytest.mark.asyncio
async def test_resolution_both_with_missing_scope_keeps_asking(service, journal):
    proposal = await service.propose("我喜欢深色主题", classifier=classifier("conflict"))
    resolved = await service.resolve(
        proposal, MemoryResolution("both", old_scope="", new_scope="work")
    )
    assert resolved.action == "clarification_required"
    assert len(journal.effective()) == 1


def test_parse_resolution_keywords(service):
    assert service.parse_resolution("更新记忆").kind == "update"
    assert service.parse_resolution("update").kind == "update"
    assert service.parse_resolution("保留原记忆").kind == "keep"
    assert service.parse_resolution("不用记").kind == "keep"
    assert service.parse_resolution("确认记住").kind == "confirm"
    both = service.parse_resolution("分别适用：旧=个人; 新=工作")
    assert both.kind == "both"
    assert both.old_scope == "个人" and both.new_scope == "工作"
    assert service.parse_resolution("随便聊聊") is None


@pytest.mark.asyncio
async def test_ambiguous_confirm_runs_classifier_before_saving(service, journal):
    intent = ExplicitMemoryService.detect("可能我喜欢深色主题")
    assert intent.kind == "ambiguous"
    proposal = service.ambiguous(intent)
    assert proposal.action == "clarification_required"
    assert len(journal.effective()) == 1
    resolved = await service.resolve(
        proposal, MemoryResolution("confirm"), classifier=classifier("supplement")
    )
    assert resolved.action == "saved"
    assert len(journal.effective()) == 2


def test_empty_fact_after_trigger_is_ambiguous():
    assert ExplicitMemoryService.detect("/记住").kind == "ambiguous"
    assert ExplicitMemoryService.detect("记住  ").kind == "ambiguous"


def test_hedged_statement_is_ambiguous():
    assert ExplicitMemoryService.detect("也许周末会去公园").kind == "ambiguous"
    assert ExplicitMemoryService.detect("maybe I like it").kind == "ambiguous"
