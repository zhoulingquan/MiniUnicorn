import importlib.util
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from miniunicorn.agent.loop import AgentLoop
from miniunicorn.agent.loop_builder import AgentLoopBuilder
from miniunicorn.agent.memory import Consolidator, Dream, MemoryStore
from miniunicorn.agent.tools.context import ToolContext
from miniunicorn.bus.queue import MessageBus
from miniunicorn.providers.base import LLMProvider
from miniunicorn.providers.openai_compat_provider import OpenAICompatProvider


def test_provider_contract_has_no_embedding_api() -> None:
    assert "embed" not in LLMProvider.__dict__
    assert "embed" not in OpenAICompatProvider.__dict__
    assert importlib.util.find_spec("miniunicorn.providers.embedding") is None


def test_structured_memory_has_no_vector_side_channel() -> None:
    source = "\n".join(
        inspect.getsource(obj) for obj in (MemoryStore, Consolidator, Dream)
    )
    forbidden = (
        "_vector_store",
        "_embed_provider",
        "_embed_model",
        "attach_vector_store",
        "set_embed_provider",
        "index_text",
        "vec_decayed",
        "vec_archived",
    )
    assert not [token for token in forbidden if token in source]


def test_context_builder_accepts_only_structured_memory_inputs() -> None:
    from miniunicorn.agent.context import ContextBuilder

    for method in (ContextBuilder.build_system_prompt, ContextBuilder.build_messages):
        params = inspect.signature(method).parameters
        assert "query_embedding" not in params
        assert "vector_recall" not in params


def _provider() -> MagicMock:
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test-model"
    provider.generation = SimpleNamespace(
        max_tokens=4096,
        temperature=0.1,
        reasoning_effort=None,
    )
    return provider


def test_runtime_constructors_have_no_vector_reservations() -> None:
    loop_params = inspect.signature(AgentLoop).parameters
    assert "vector_recall" not in loop_params
    assert "embedding_model" not in loop_params
    assert not hasattr(AgentLoopBuilder, "with_vector_recall")
    assert not hasattr(AgentLoopBuilder, "with_embedding_model")
    assert "memory_store" not in ToolContext.__dataclass_fields__
    assert importlib.util.find_spec("miniunicorn.agent.vector_memory") is None
    assert importlib.util.find_spec("miniunicorn.agent.tools.recall") is None


def test_agent_startup_does_not_create_a_database(tmp_path: Path) -> None:
    AgentLoop(
        bus=MessageBus(),
        provider=_provider(),
        workspace=tmp_path,
        model="test-model",
        context_window_tokens=128_000,
    )
    assert list(tmp_path.rglob("*.db")) == []


def test_agent_startup_leaves_an_existing_database_untouched(tmp_path: Path) -> None:
    legacy = tmp_path / "memory" / "memory.db"
    legacy.parent.mkdir(parents=True)
    original = b"user-owned-legacy-data"
    legacy.write_bytes(original)

    AgentLoop(
        bus=MessageBus(),
        provider=_provider(),
        workspace=tmp_path,
        model="test-model",
        context_window_tokens=128_000,
    )

    assert legacy.read_bytes() == original
