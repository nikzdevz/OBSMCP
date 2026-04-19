"""Extracts knowledge graph nodes (classes/functions) from Python files.

Kept intentionally minimal — language-specific AST support is expected to
be added via tree-sitter in a follow-up.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

from ..client.http_client import BackendClient
from ..config import Config
from ..utils.logger import get_logger
from ..utils.path_utils import SKIP_DIRS

logger = get_logger("obsmcp.graph.nodes")


class NodeExtractor:
    def __init__(self, client: BackendClient, config: Config) -> None:
        self.client = client
        self.config = config
        self.project_path = Path(config.project_path) if config.project_path else None
        self.interval = config.graph_build_interval_seconds

    async def run(self) -> None:
        if not self.config.enabled_modules.knowledge_graph:
            return
        while True:
            try:
                await self._build()
            except Exception:  # noqa: BLE001
                logger.exception("node extraction failed")
            await asyncio.sleep(self.interval)

    async def _build(self) -> None:
        if not self.project_path or not self.project_path.exists():
            return
        nodes = []
        for path in self.project_path.rglob("*.py"):
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
            except SyntaxError:
                continue
            rel = str(path.relative_to(self.project_path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    nodes.append(
                        {
                            "project_id": self.config.project_id or None,
                            "node_type": "class",
                            "name": node.name,
                            "description": ast.get_docstring(node) or "",
                            "metadata": {"file": rel, "line": node.lineno},
                        }
                    )
                elif isinstance(node, ast.FunctionDef):
                    nodes.append(
                        {
                            "project_id": self.config.project_id or None,
                            "node_type": "function",
                            "name": node.name,
                            "description": ast.get_docstring(node) or "",
                            "metadata": {"file": rel, "line": node.lineno},
                        }
                    )
        if nodes:
            await self.client.add_nodes(nodes)
            logger.info("added %d graph nodes", len(nodes))
