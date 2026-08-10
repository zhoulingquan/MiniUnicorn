import importlib.util

from miniunicorn.providers.base import LLMProvider
from miniunicorn.providers.openai_compat_provider import OpenAICompatProvider


def test_provider_contract_has_no_embedding_api() -> None:
    assert "embed" not in LLMProvider.__dict__
    assert "embed" not in OpenAICompatProvider.__dict__
    assert importlib.util.find_spec("miniunicorn.providers.embedding") is None
