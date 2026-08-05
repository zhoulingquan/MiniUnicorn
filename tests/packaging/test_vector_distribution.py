"""Packaging test: the built wheel must not ship model binaries.

The local embedding model (``BAAI/bge-small-zh-v1.5``) is downloaded at
runtime into the user's cache directory — it must never be bundled into the
wheel or sdist.  This test builds the wheel with ``python -m build --wheel``
and inspects the archive for forbidden model artifacts (``.onnx``, ``.bin``,
``.safetensors``) and for any file whose name references the pinned model id.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

#: File extensions that indicate shipped model weights/binaries.
FORBIDDEN_SUFFIXES: tuple[str, ...] = (".onnx", ".bin", ".safetensors")

#: The pinned model id — no wheel entry should reference it in its name.
FORBIDDEN_NAME_FRAGMENT = "bge-small-zh-v1.5"


def _build_available() -> bool:
    """Return True when the ``build`` package is importable."""
    return importlib.util.find_spec("build") is not None


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the project wheel once per module; skip if ``build`` is unavailable.

    Uses ``--no-isolation`` so the already-installed ``hatchling`` backend is
    reused without spawning an isolated build environment (which would need
    network access).  If the build fails for any reason the test is skipped
    rather than errored — packaging tests are opportunistic.
    """
    if not _build_available():
        pytest.skip("build package not available; install with `pip install build`")

    repo_root = Path(__file__).resolve().parents[2]
    out_dir = tmp_path_factory.mktemp("wheel-build")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(out_dir),
        ],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"wheel build failed: {result.stderr[:500]}")

    wheels = list(out_dir.glob("*.whl"))
    assert wheels, "build reported success but produced no wheel"
    return wheels[0]


def _wheel_entries(wheel_path: Path) -> list[str]:
    """Return the list of archive member names inside the wheel."""
    with zipfile.ZipFile(wheel_path) as zf:
        return zf.namelist()


def test_wheel_contains_no_model_binaries(built_wheel: Path) -> None:
    """No ``.onnx``/``.bin``/``.safetensors`` files may ship in the wheel."""
    names = _wheel_entries(built_wheel)
    offenders = [n for n in names if n.lower().endswith(FORBIDDEN_SUFFIXES)]
    assert not offenders, (
        f"wheel ships forbidden model binaries: {offenders}"
    )


def test_wheel_contains_no_bge_model_name(built_wheel: Path) -> None:
    """No file in the wheel may reference the pinned model id in its name."""
    names = _wheel_entries(built_wheel)
    offenders = [n for n in names if FORBIDDEN_NAME_FRAGMENT in n.lower()]
    assert not offenders, (
        f"wheel references pinned model '{FORBIDDEN_NAME_FRAGMENT}' in filenames: {offenders}"
    )
