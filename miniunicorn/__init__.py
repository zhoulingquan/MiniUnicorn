"""
MiniUnicorn - A lightweight AI agent framework
"""

import tomllib
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def _read_pyproject_version() -> str | None:
    """Read the version from the source-tree pyproject.toml.

    Always reads pyproject.toml (not importlib.metadata) so that editing
    ``version`` takes effect on the next import without reinstalling the
    editable install. Cached for the process lifetime via lru_cache.
    """
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if not pyproject.exists():
        return None
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return data.get("project", {}).get("version")


def _resolve_version() -> str:
    return _read_pyproject_version() or "0.3.0"


__version__ = _resolve_version()
__logo__ = "🐱"

_LAZY_EXPORTS = {
    "Miniunicorn": ".miniunicorn",
    "RunResult": ".miniunicorn",
}


def __getattr__(name: str):
    module_path = _LAZY_EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module
    mod = import_module(module_path, __name__)
    val = getattr(mod, name)
    globals()[name] = val
    return val


__all__ = ["Miniunicorn", "RunResult"]
