"""Builds ``imports`` edges between file nodes from the code atlas."""

from __future__ import annotations

import asyncio

from ..client.http_client import BackendClient
from ..config import Config
from ..utils.logger import get_logger

logger = get_logger("obsmcp.graph.edges")


class EdgeBuilder:
    def __init__(self, client: BackendClient, config: Config) -> None:
        self.client = client
        self.config = config
        self.interval = config.graph_build_interval_seconds

    async def run(self) -> None:
        if not self.config.enabled_modules.knowledge_graph:
            return
        while True:
            try:
                await self._build()
            except Exception:  # noqa: BLE001
                logger.exception("edge builder failed")
            await asyncio.sleep(self.interval)

    async def _build(self) -> None:
        conn = self.client._conn  # noqa: SLF001
        rows = conn.execute(
            "SELECT file_path, imports, project_id FROM code_atlas_files"
        ).fetchall()
        edges = []
        for row in rows:
            imports = row["imports"]
            if not imports:
                continue
            try:
                import_list = __import__("json").loads(imports)
            except Exception:  # noqa: BLE001
                continue
            for imp in import_list:
                edges.append(
                    {
                        "project_id": row["project_id"],
                        "from_node_id": f"file:{row['file_path']}",
                        "to_node_id": f"file:{imp}",
                        "edge_type": "imports",
                    }
                )
        if edges:
            await self.client.add_edges(edges)
            logger.info("added %d graph edges", len(edges))
