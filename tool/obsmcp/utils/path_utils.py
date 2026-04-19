"""Cross-platform path helpers."""

from __future__ import annotations

from pathlib import Path

SKIP_DIRS: set[str] = {
    ".git",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    "env",
    "build",
    "dist",
    ".next",
    ".cache",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".obsmcp",
    "target",
    ".turbo",
}


def relative_safe(path: Path, base: Path) -> str:
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)
