"""Code Atlas scanner: walks the project tree and extracts lightweight
metadata (language, function count, imports) per file using regex heuristics.

Full tree-sitter integration is left as a TODO; the current implementation
works across all languages without native deps.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..client.http_client import BackendClient
from ..config import Config
from ..utils.logger import get_logger
from ..utils.path_utils import SKIP_DIRS

logger = get_logger("obsmcp.atlas")

LANGUAGES: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".jsx": "jsx",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".c": "c",
    ".h": "c",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".kt": "kotlin",
    ".scala": "scala",
    ".vue": "vue",
    ".svelte": "svelte",
    ".sh": "shell",
}

_IMPORT_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "python": [
        re.compile(r"^\s*import\s+([\w.]+)", re.M),
        re.compile(r"^\s*from\s+([\w.]+)\s+import", re.M),
    ],
    "javascript": [re.compile(r"""(?:import|require)\s*\(?['"]([^'"]+)['"]""")],
    "typescript": [re.compile(r"""(?:import|require)\s*\(?['"]([^'"]+)['"]""")],
    "tsx": [re.compile(r"""(?:import|require)\s*\(?['"]([^'"]+)['"]""")],
    "jsx": [re.compile(r"""(?:import|require)\s*\(?['"]([^'"]+)['"]""")],
    "go": [re.compile(r"""^\s*import\s+['"]([^'"]+)['"]""", re.M)],
    "rust": [re.compile(r"^\s*use\s+([\w:]+)", re.M)],
    "java": [re.compile(r"^\s*import\s+([\w.]+);", re.M)],
}

_FUNCTION_PATTERNS: dict[str, re.Pattern[str]] = {
    "python": re.compile(r"^\s*(?:async\s+)?def\s+\w+", re.M),
    "javascript": re.compile(r"\bfunction\s+\w+|=>\s*{|\b\w+\s*=\s*\("),
    "typescript": re.compile(r"\bfunction\s+\w+|=>\s*{|\b\w+\s*\("),
    "go": re.compile(r"^\s*func\s+\w+", re.M),
    "rust": re.compile(r"^\s*fn\s+\w+", re.M),
    "java": re.compile(r"\b(?:public|private|protected|static)[\w\s]*\s+\w+\s*\(", re.M),
}


class CodeAtlasScanner:
    def __init__(self, client: BackendClient, config: Config) -> None:
        self.client = client
        self.config = config
        self.project_path = Path(config.project_path) if config.project_path else None
        self.scan_interval = config.scan_interval_seconds

    async def run(self) -> None:
        if not self.project_path or not self.project_path.exists():
            logger.warning("Code atlas disabled (invalid project path: %s)", self.project_path)
            return
        while True:
            try:
                await self.perform_scan()
            except Exception:  # noqa: BLE001
                logger.exception("Scan failed")
            await asyncio.sleep(self.scan_interval)

    async def perform_scan(self) -> str:
        assert self.project_path is not None
        logger.info("Starting code atlas scan of %s", self.project_path)
        scan = await self.client.trigger_scan(self.config.project_id or "")
        scan_id = scan["id"]

        batch: list[dict[str, Any]] = []
        total = 0
        for path, metadata in self._iter_files():
            total += 1
            batch.append(
                {
                    "scan_id": scan_id,
                    "project_id": self.config.project_id or None,
                    "file_path": str(path.relative_to(self.project_path)),
                    "language": metadata["language"],
                    "functions_count": metadata["functions_count"],
                    "imports": metadata["imports"],
                    "exports": metadata.get("exports", []),
                }
            )
            if len(batch) >= 50:
                await self.client.add_scan_files(batch)
                batch = []
                await asyncio.sleep(0)
        if batch:
            await self.client.add_scan_files(batch)

        await self.client.update_scan(
            scan_id,
            {
                "status": "completed",
                "total_files": total,
                "scanned_files": total,
            },
        )
        logger.info("Scan %s complete: %d files", scan_id, total)
        return scan_id

    def _iter_files(self) -> Iterator[tuple[Path, dict[str, Any]]]:
        assert self.project_path is not None
        for path in self.project_path.rglob("*"):
            if not path.is_file():
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            suffix = path.suffix.lower()
            if suffix not in LANGUAGES:
                continue
            meta = self._extract(path, LANGUAGES[suffix])
            if meta is not None:
                yield path, meta

    def _extract(self, path: Path, language: str) -> dict[str, Any] | None:
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None
        imports: list[str] = []
        for pattern in _IMPORT_PATTERNS.get(language, []):
            imports.extend(pattern.findall(content))
        funcs = _FUNCTION_PATTERNS.get(language)
        functions_count = len(funcs.findall(content)) if funcs else 0
        return {
            "language": language,
            "imports": sorted(set(imports))[:100],
            "functions_count": functions_count,
            "exports": [],
        }
