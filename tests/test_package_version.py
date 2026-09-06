from __future__ import annotations

import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path


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
        fake = types.ModuleType("erza.erza")
        fake.Erza = object
        fake.RunResult = object
        sys.modules["erza.erza"] = fake

        import erza

        print(erza.__version__)
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
    import erza

    monkeypatch.setattr(erza, "_read_pyproject_version", lambda: None)
    monkeypatch.setattr(erza, "_read_installed_version", lambda: "9.9.9-installed")
    assert erza._resolve_version() == "9.9.9-installed"


def test_resolve_version_returns_unknown_when_both_unavailable(monkeypatch) -> None:
    """Per design §4.6: never return a misleading historical version.

    When neither pyproject.toml nor installed metadata is available, return
    an explicit ``0.0.0+unknown`` development sentinel instead of the old
    hardcoded ``0.3.0``.
    """
    import erza

    monkeypatch.setattr(erza, "_read_pyproject_version", lambda: None)
    monkeypatch.setattr(erza, "_read_installed_version", lambda: None)
    assert erza._resolve_version() == "0.0.0+unknown"
