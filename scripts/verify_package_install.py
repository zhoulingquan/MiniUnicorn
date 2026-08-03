#!/usr/bin/env python3
"""Isolated package install verifier.

Builds a throwaway venv, installs the given wheel/sdist artifact with a
specific extra, and verifies that the expected set of packages is
importable (and that forbidden packages are absent).

Usage::

    python scripts/verify_package_install.py \
        --artifact dist/miniunicorn_ai-*.whl \
        --extra documents \
        --work-dir .package-smoke-documents

Exit code 0 = all assertions passed; non-zero = failure.
"""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# (module_name, display_name) for each backend we track.
_BACKENDS = {
    "pypdf": ("pypdf", "pypdf"),
    "python-docx": ("docx", "python-docx"),
    "openpyxl": ("openpyxl", "openpyxl"),
    "python-pptx": ("pptx", "python-pptx"),
    "pymupdf": ("fitz", "PyMuPDF"),
}

# What each extra must provide / must NOT provide.
_EXTRA_EXPECTATIONS: dict[str, dict[str, list[str]]] = {
    "base": {
        "present": ["miniunicorn"],
        "absent": ["pypdf", "docx", "openpyxl", "pptx", "fitz"],
    },
    "documents": {
        "present": ["miniunicorn", "pypdf", "docx", "openpyxl", "pptx"],
        "absent": ["fitz"],
    },
    "pdf": {
        "present": ["miniunicorn", "pypdf", "fitz"],
        "absent": ["docx", "openpyxl", "pptx"],
    },
}


def _find_spec(name: str) -> bool:
    """Return True if *name* is importable in the current interpreter."""
    return importlib.util.find_spec(name) is not None


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, check=True, **kwargs)


def verify(
    artifact: Path,
    extra: str,
    work_dir: Path,
) -> int:
    """Create a venv, install the artifact, and check import expectations."""
    if extra not in _EXTRA_EXPECTATIONS:
        print(f"ERROR: unknown extra '{extra}'", file=sys.stderr)
        return 2

    expectations = _EXTRA_EXPECTATIONS[extra]
    work_dir.mkdir(parents=True, exist_ok=True)
    work_dir = work_dir.resolve()

    venv_dir = work_dir / "venv"
    if venv_dir.exists():
        shutil.rmtree(venv_dir)

    print(f"[1/4] Creating venv at {venv_dir}")
    _run([sys.executable, "-m", "venv", str(venv_dir)])

    venv_python = venv_dir / "bin" / "python"
    if not venv_python.exists():
        venv_python = venv_dir / "Scripts" / "python.exe"
    if not venv_python.exists():
        print(f"ERROR: venv python not found at {venv_python}", file=sys.stderr)
        return 1

    # Install the artifact with the requested extra.
    if extra == "base":
        install_spec = str(artifact)
    else:
        install_spec = f"{artifact}[{extra}]"

    print(f"[2/4] Installing {install_spec}")
    _run([str(venv_python), "-m", "pip", "install", "--no-cache-dir", install_spec])

    # Write a verification script that runs inside the venv.
    verify_script = work_dir / "_verify_imports.py"
    present = expectations["present"]
    absent = expectations["absent"]
    verify_script.write_text(
        "import importlib.util, sys\n"
        f"present = {present!r}\n"
        f"absent = {absent!r}\n"
        "errors = []\n"
        "for mod in present:\n"
        "    if importlib.util.find_spec(mod) is None:\n"
        "        errors.append(f'EXPECTED present but MISSING: {mod}')\n"
        "for mod in absent:\n"
        "    if importlib.util.find_spec(mod) is not None:\n"
        "        errors.append(f'EXPECTED absent but FOUND: {mod}')\n"
        "if errors:\n"
        "    for e in errors:\n"
        "        print(e, file=sys.stderr)\n"
        "    sys.exit(1)\n"
        "print('All import expectations met.')\n"
    )

    # Run the verification from a temp dir outside the repo so the source
    # checkout is not on sys.path.
    with tempfile.TemporaryDirectory() as tmpcwd:
        print(f"[3/4] Verifying imports from {tmpcwd}")
        result = subprocess.run(
            [str(venv_python), str(verify_script)],
            cwd=tmpcwd,
            capture_output=True,
            text=True,
        )
        print(result.stdout, end="")
        if result.returncode != 0:
            print(result.stderr, end="", file=sys.stderr)
            print(f"[4/4] FAILED for extra '{extra}'", file=sys.stderr)
            return 1

    print(f"[4/4] PASSED for extra '{extra}'")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify isolated package install for a given extra."
    )
    parser.add_argument("--artifact", required=True, type=Path, help="Path to wheel or sdist.")
    parser.add_argument(
        "--extra",
        required=True,
        choices=list(_EXTRA_EXPECTATIONS),
        help="Which install profile to verify.",
    )
    parser.add_argument(
        "--work-dir",
        required=True,
        type=Path,
        help="Working directory for venv and temp files.",
    )
    args = parser.parse_args()

    if not args.artifact.exists():
        print(f"ERROR: artifact not found: {args.artifact}", file=sys.stderr)
        return 1

    return verify(args.artifact, args.extra, args.work_dir)


if __name__ == "__main__":
    sys.exit(main())
