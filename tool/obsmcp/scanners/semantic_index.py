"""Best-effort LLM semantic descriptions.

Disabled unless the Anthropic API key is available AND the user has enabled
the ``semantic_index`` module in config.
"""

from __future__ import annotations

import asyncio

from ..client.http_client import BackendClient
from ..config import Config
from ..llm.semantic_descriptions import describe_file
from ..utils.logger import get_logger

logger = get_logger("obsmcp.semantic")


class SemanticIndexer:
    def __init__(self, client: BackendClient, config: Config) -> None:
        self.client = client
        self.config = config
        self.interval = max(config.scan_interval_seconds * 2, 600)

    async def run(self) -> None:
        if not self.config.enabled_modules.semantic_index:
            logger.info("semantic indexer disabled in config")
            return
        while True:
            await asyncio.sleep(self.interval)
            try:
                await self._describe_batch()
            except Exception:  # noqa: BLE001
                logger.exception("semantic indexing batch failed")

    async def _describe_batch(self) -> None:
        conn = self.client._conn  # noqa: SLF001
        rows = conn.execute(
            "SELECT id, file_path, language FROM code_atlas_files WHERE semantic_description IS NULL LIMIT 10"
        ).fetchall()
        for row in rows:
            description = await describe_file(
                file_path=row["file_path"],
                language=row["language"] or "unknown",
                content=None,
                config=self.config,
            )
            if description:
                conn.execute(
                    "UPDATE code_atlas_files SET semantic_description=? WHERE id=?",
                    (description, row["id"]),
                )
