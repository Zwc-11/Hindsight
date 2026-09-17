"""Thin entry point into the native engine; no second portfolio or model implementation."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path


def run_native(args: Sequence[str], *, repo_root: Path | None = None) -> int:
    root = repo_root or Path(__file__).resolve().parents[1]
    override = os.environ.get("HINDSIGHT_NATIVE_BIN")
    executable = "hindsight-economic.exe" if os.name == "nt" else "hindsight-economic"
    binary = Path(override) if override else root / "target" / "debug" / executable
    if not binary.is_file():
        print(
            f"Native engine not found: {binary}. Build with cargo build --locked "
            "(or sh scripts/cargo-local.sh build --locked).",
            file=sys.stderr,
        )
        return 2
    try:
        return subprocess.run([str(binary), *(args or ["--help"])], check=False).returncode
    except OSError as exc:
        print(f"Cannot execute native engine: {exc}", file=sys.stderr)
        return 2
