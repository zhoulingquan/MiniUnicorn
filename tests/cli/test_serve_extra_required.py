"""Tests for the ``miniunicorn serve`` entry point without the api extra.

The OpenAI-compatible HTTP server is an optional extra. When aiohttp is
not installed, ``serve`` must fail fast with an actionable install hint
instead of a bare traceback.
"""

from __future__ import annotations

from typer.testing import CliRunner

from miniunicorn.cli.commands import app

runner = CliRunner()


def test_serve_without_aiohttp_prints_install_hint(monkeypatch) -> None:
    """Simulate the core install (no aiohttp) and check the error message."""
    # Setting a sys.modules entry to None makes `import aiohttp` raise
    # ImportError, which is exactly what happens without the [api] extra.
    monkeypatch.setitem(__import__("sys").modules, "aiohttp", None)

    result = runner.invoke(app, ["serve"])

    assert result.exit_code == 1
    assert "miniunicorn-ai[api]" in result.output
