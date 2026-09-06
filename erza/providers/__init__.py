"""LLM provider abstraction module."""

from __future__ import annotations

from importlib import import_module

from erza.providers.base import LLMProvider, LLMResponse

__all__ = [
    "LLMProvider",
    "LLMResponse",
    "OpenAICompatProvider",
]

_LAZY_IMPORTS = {
    "OpenAICompatProvider": ".openai_compat_provider",
}


def __getattr__(name: str):
    """Lazily expose provider implementations without importing all backends up front."""
    module_name = _LAZY_IMPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(module_name, __name__)
    return getattr(module, name)
