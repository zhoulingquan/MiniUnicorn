"""Guards for config.schema's eager tool-config resolution.

The eager resolution at the bottom of schema.py used to swallow ALL
ImportErrors silently; genuinely broken tool config modules only surfaced
later as unattached config classes (the W6-1a incident). These tests pin
the contract: circular imports defer to the lazy rebuild with a visible
warning, while real import breakage raises immediately at the source.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from miniunicorn.config.schema import (
    _is_circular_import_error,
    _try_eager_resolve_tool_config_refs,
)


def test_circular_import_error_is_recognized() -> None:
    assert _is_circular_import_error(
        ImportError(
            "cannot import name 'ExecToolConfig' from partially initialized "
            "module 'miniunicorn.tools.shell' (most likely due to a circular import)"
        )
    )
    assert _is_circular_import_error(ImportError("(most likely due to a circular import)"))


def test_real_import_errors_are_not_circular() -> None:
    assert not _is_circular_import_error(ModuleNotFoundError("No module named 'broken_pkg'"))
    assert not _is_circular_import_error(
        ImportError("cannot import name 'Nope' from 'miniunicorn.tools.shell'")
    )


def test_real_import_error_propagates_from_eager_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _broken() -> None:
        raise ModuleNotFoundError("No module named 'broken_pkg'")

    monkeypatch.setattr("miniunicorn.config.schema._resolve_tool_config_refs", _broken)
    with pytest.raises(ModuleNotFoundError, match="broken_pkg"):
        _try_eager_resolve_tool_config_refs()


def test_circular_import_defers_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    def _cyclic() -> None:
        raise ImportError(
            "cannot import name 'X' from partially initialized module 'm' "
            "(most likely due to a circular import)"
        )

    monkeypatch.setattr("miniunicorn.config.schema._resolve_tool_config_refs", _cyclic)
    _try_eager_resolve_tool_config_refs()


def test_tool_first_import_order_still_builds_config() -> None:
    """Regress the W6-1a incident shape: entering via a tool module defers the
    eager resolution (warning on stderr) and Config still builds via the lazy
    rebuild."""
    code = (
        "from miniunicorn.tools.shell import ExecToolConfig; "
        "from miniunicorn.config.schema import Config; "
        "c = Config(); "
        "assert type(c.tools.exec).__name__ == 'ExecToolConfig'; "
        "print('OK')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[2],
    )
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout
    assert "deferring to lazy rebuild" in proc.stderr
