"""
MiniUnicorn - A lightweight AI agent framework
"""

try:
    import tomllib
except ImportError:
    import tomli as tomllib
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


@lru_cache(maxsize=1)
def _read_installed_version() -> str | None:
    """Read the version from installed metadata (importlib.metadata).

    Returns ``None`` when the distribution is not installed (e.g. running
    directly from a source checkout without ``pip install``). Used as a
    fallback when the source-tree ``pyproject.toml`` is unavailable.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover — Python 3.8+ always has importlib.metadata
        return None
    try:
        return version("miniunicorn-ai")
    except PackageNotFoundError:
        return None
    except Exception:  # pragma: no cover — defensive against broken metadata backends
        return None


def _resolve_version() -> str:
    """Resolve the runtime version per design §4.6.

    Order of precedence:
      1. Source-tree ``pyproject.toml`` (works for editable installs and
         source checkouts; always reflects the latest committed version).
      2. ``importlib.metadata.version("miniunicorn-ai")`` (works for
         wheel/sdist installs where the source tree is absent).
      3. Explicit ``"0.0.0+unknown"`` development sentinel — never return a
         misleading historical hardcoded version like the old ``0.3.0``.
    """
    return _read_pyproject_version() or _read_installed_version() or "0.0.0+unknown"


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
