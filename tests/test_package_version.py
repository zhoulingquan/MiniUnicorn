from __future__ import annotations

import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

import pytest


def test_source_checkout_import_uses_pyproject_version_without_metadata() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    expected = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    script = textwrap.dedent(
        f"""
        import sys
        import types

        sys.path.insert(0, {str(repo_root)!r})
        fake = types.ModuleType("miniunicorn.miniunicorn")
        fake.Miniunicorn = object
        fake.RunResult = object
        sys.modules["miniunicorn.miniunicorn"] = fake

        import miniunicorn

        print(miniunicorn.__version__)
        """
    )

    proc = subprocess.run(
        [sys.executable, "-S", "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == expected


def test_resolve_version_falls_back_to_installed_metadata(monkeypatch) -> None:
    """When pyproject.toml is missing, fall back to importlib.metadata.version."""
    import miniunicorn

    monkeypatch.setattr(miniunicorn, "_read_pyproject_version", lambda: None)
    monkeypatch.setattr(miniunicorn, "_read_installed_version", lambda: "9.9.9-installed")
    assert miniunicorn._resolve_version() == "9.9.9-installed"


def test_resolve_version_returns_unknown_when_both_unavailable(monkeypatch) -> None:
    """Per design §4.6: never return a misleading historical version.

    When neither pyproject.toml nor installed metadata is available, return
    an explicit ``0.0.0+unknown`` development sentinel instead of the old
    hardcoded ``0.3.0``.
    """
    import miniunicorn

    monkeypatch.setattr(miniunicorn, "_read_pyproject_version", lambda: None)
    monkeypatch.setattr(miniunicorn, "_read_installed_version", lambda: None)
    assert miniunicorn._resolve_version() == "0.0.0+unknown"


@pytest.fixture(autouse=True)
def _clear_version_cache():
    """Ensure the lru_cache on _read_pyproject_version never leaks between tests."""
    import miniunicorn

    miniunicorn._read_pyproject_version.cache_clear()
    yield
    miniunicorn._read_pyproject_version.cache_clear()


def _patch_pyproject_read(
    monkeypatch,
    *,
    content: str | None = None,
    raises: BaseException | None = None,
) -> None:
    """Make the real _read_pyproject_version() see ``content`` (or raise).

    Only the source-tree pyproject.toml path is redirected; every other
    Path.exists / Path.read_text call delegates to the real implementation so
    the rest of the test process is unaffected.
    """
    import miniunicorn

    miniunicorn._read_pyproject_version.cache_clear()
    target = Path(miniunicorn.__file__).resolve().parent.parent / "pyproject.toml"

    real_exists = Path.exists
    real_read_text = Path.read_text

    def fake_exists(self, *args, **kwargs):
        if self == target:
            return True
        return real_exists(self, *args, **kwargs)

    def fake_read_text(self, *args, **kwargs):
        if self == target:
            if raises is not None:
                raise raises
            return content
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", fake_exists)
    monkeypatch.setattr(Path, "read_text", fake_read_text)


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("[project\nversion='broken'", id="malformed-toml"),
        pytest.param("[tool.example]\nvalue=1", id="missing-project"),
        pytest.param("[project]\nname='x'", id="missing-version"),
        pytest.param("[project]\nversion=3", id="non-string-version"),
    ],
)
def test_read_pyproject_version_tolerates_bad_metadata(monkeypatch, content) -> None:
    """Bad source pyproject.toml must return None so _resolve_version falls through.

    Per Task 21: malformed TOML, missing [project], missing version, and
    non-string version all return None from _read_pyproject_version(); the
    resolver then falls back to installed metadata instead of raising.
    """
    import miniunicorn

    _patch_pyproject_read(monkeypatch, content=content)
    monkeypatch.setattr(miniunicorn, "_read_installed_version", lambda: "9.9.9")
    assert miniunicorn._resolve_version() == "9.9.9"


def test_read_pyproject_version_tolerates_unreadable_file(monkeypatch) -> None:
    """An unreadable pyproject.toml (OSError) must return None, not raise."""
    import miniunicorn

    _patch_pyproject_read(monkeypatch, raises=OSError("denied"))
    monkeypatch.setattr(miniunicorn, "_read_installed_version", lambda: "9.9.9")
    assert miniunicorn._resolve_version() == "9.9.9"
