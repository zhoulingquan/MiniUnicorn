import importlib.util
import inspect

from miniunicorn.agent.memory import Consolidator, Dream, MemoryStore
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
