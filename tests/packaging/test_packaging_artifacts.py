"""Artifact metadata tests for wheel and sdist produced by ``uv build``.

These tests build the package into a temporary directory and inspect the
resulting wheel/sdist archives to verify that:

- Document backends (pypdf, python-docx, openpyxl, python-pptx) are NOT
  in the unconditional ``Requires-Dist`` of the wheel metadata.
- The ``documents`` and ``pdf`` extras are declared.
- The wheel bundles ``miniunicorn/web/dist/index.html``.
- The wheel contains no repository tests or local caches.
"""

from __future__ import annotations

import os
import subprocess
import zipfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

_DOCUMENT_BACKENDS = {"pypdf", "python-docx", "openpyxl", "python-pptx"}


@pytest.fixture(scope="module")
def dist_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build wheel + sdist into a temporary directory and return the path."""
    dist = tmp_path_factory.mktemp("dist-remediation")
    env = {**os.environ, "MINIUNICORN_SKIP_WEBUI_BUILD": "1"}
    subprocess.run(
        ["uv", "build", "--out-dir", str(dist)],
        cwd=_REPO_ROOT,
        check=True,
        env=env,
    )
    return dist


@pytest.fixture(scope="module")
def wheel_path(dist_dir: Path) -> Path:
    wheels = sorted(dist_dir.glob("miniunicorn_ai-*.whl"))
    assert wheels, f"No wheel found in {dist_dir}"
    return wheels[0]


@pytest.fixture(scope="module")
def sdist_path(dist_dir: Path) -> Path:
    sdists = sorted(dist_dir.glob("miniunicorn_ai-*.tar.gz"))
    assert sdists, f"No sdist found in {dist_dir}"
    return sdists[0]


def inspect_wheel(path: Path) -> dict[str, object]:
    """Return the zip namelist and METADATA text for a wheel."""
    with zipfile.ZipFile(path) as zf:
        names = sorted(zf.namelist())
        metadata_name = next((n for n in names if n.endswith(".dist-info/METADATA")), "")
        metadata_text = zf.read(metadata_name).decode("utf-8") if metadata_name else ""
    return {"names": names, "metadata": metadata_text}


def test_wheel_excludes_document_backends_from_requires_dist(
    wheel_path: Path,
) -> None:
    """Unconditional Requires-Dist must not list any document backend."""
    info = inspect_wheel(wheel_path)
    metadata = info["metadata"]
    # Extract Requires-Dist lines that are NOT extra-conditional.
    # Extra-conditional lines look like:
    #   Requires-Dist: pypdf>=5.0.0,<6.0.0 ; extra == "documents"
    # Unconditional lines have no 'extra ==' suffix.
    for line in metadata.splitlines():
        if not line.startswith("Requires-Dist:"):
            continue
        if "extra ==" in line:
            continue
        for backend in _DOCUMENT_BACKENDS:
            assert backend not in line, (
                f"{backend} must not be in unconditional Requires-Dist: {line}"
            )


def test_wheel_declares_documents_extra(wheel_path: Path) -> None:
    """The wheel metadata must declare the ``documents`` extra."""
    info = inspect_wheel(wheel_path)
    metadata = info["metadata"]
    assert "extra == 'documents'" in metadata, "documents extra not found in wheel metadata"
    for backend in _DOCUMENT_BACKENDS:
        assert backend in metadata, f"{backend} must appear in documents extra"


def test_wheel_declares_pdf_extra_with_pypdf(wheel_path: Path) -> None:
    """The ``pdf`` extra must include pypdf."""
    info = inspect_wheel(wheel_path)
    metadata = info["metadata"]
    assert "extra == 'pdf'" in metadata
    pdf_lines = [line for line in metadata.splitlines() if "extra == 'pdf'" in line]
    assert any("pypdf" in line for line in pdf_lines), "pypdf must be in the pdf extra"


def test_wheel_contains_webui_index(wheel_path: Path) -> None:
    """The wheel must bundle miniunicorn/web/dist/index.html."""
    info = inspect_wheel(wheel_path)
    names = info["names"]
    assert any("miniunicorn/web/dist/index.html" in n for n in names), (
        "miniunicorn/web/dist/index.html not found in wheel"
    )


def test_wheel_excludes_tests_and_caches(wheel_path: Path) -> None:
    """The wheel must not contain repository tests or local caches."""
    info = inspect_wheel(wheel_path)
    names = info["names"]
    forbidden_patterns = ["tests/", ".pytest_cache", "__pycache__", ".ruff_cache"]
    for pattern in forbidden_patterns:
        matching = [n for n in names if pattern in n]
        assert not matching, f"Wheel contains forbidden path matching '{pattern}': {matching[:5]}"


def test_sdist_exists(sdist_path: Path) -> None:
    """An sdist must be produced alongside the wheel."""
    assert sdist_path.exists()
    assert sdist_path.suffix == ".gz"
