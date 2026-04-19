"""Main entry point for the OBSMCP local tool.

Usage::

    python -m obsmcp                 # full agent + local UI (standalone)
    python -m obsmcp --mcp-stdio      # MCP stdio server only
    python -m obsmcp --agent-only     # background monitors, no UI
    python -m obsmcp --ui-only        # local UI only (standalone mode)
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from .client.http_client import BackendClient
from .config import Config, load_config
from .graph.edge_builder import EdgeBuilder
from .graph.node_extractor import NodeExtractor
from .local_ui import serve_local_ui
from .monitors.file_watcher import FileWatcher
from .monitors.git_monitor import GitMonitor
from .monitors.perf_monitor import PerformanceMonitor
from .monitors.session_monitor import SessionMonitor
from .monitors.task_monitor import TaskMonitor
from .scanners.code_atlas import CodeAtlasScanner
from .scanners.semantic_index import SemanticIndexer
from .utils.logger import get_logger

logger = get_logger("obsmcp")


async def run_agent(config: Config) -> None:
    client = BackendClient(config)
    await client.register()
    tasks = []
    m = config.enabled_modules
    if m.session_monitor:
        tasks.append(asyncio.create_task(SessionMonitor(client, config).run()))
    if m.task_monitor:
        tasks.append(asyncio.create_task(TaskMonitor(client, config).run()))
    if m.file_watcher:
        tasks.append(asyncio.create_task(FileWatcher(client, config).run()))
    if m.git_monitor:
        tasks.append(asyncio.create_task(GitMonitor(client, config).run()))
    if m.perf_monitor:
        tasks.append(asyncio.create_task(PerformanceMonitor(client, config).run()))
    if m.code_atlas:
        tasks.append(asyncio.create_task(CodeAtlasScanner(client, config).run()))
    if m.semantic_index:
        tasks.append(asyncio.create_task(SemanticIndexer(client, config).run()))
    if m.knowledge_graph:
        tasks.append(asyncio.create_task(NodeExtractor(client, config).run()))
        tasks.append(asyncio.create_task(EdgeBuilder(client, config).run()))

    async def heartbeat_loop() -> None:
        while True:
            await asyncio.sleep(30)
            await client.heartbeat()

    tasks.append(asyncio.create_task(heartbeat_loop()))
    logger.info("OBSMCP agent running (mode=%s, %d modules)", config.mode, len(tasks))
    try:
        await asyncio.gather(*tasks)
    finally:
        await client.close()


async def main_async(args: argparse.Namespace) -> None:
    config = load_config()
    if not config.project_path:
        print(
            "OBSMCP is not configured. Run `obsmcp-setup` or launch `start.bat` first.",
            file=sys.stderr,
        )
        sys.exit(1)

    coros = []
    if args.mcp_stdio:
        from .mcp_server import run_stdio

        await run_stdio()
        return

    if not args.ui_only:
        coros.append(run_agent(config))
    if not args.agent_only and config.mode == "standalone":
        coros.append(serve_local_ui(config))

    if not coros:
        print("Nothing to do — pick one of --agent-only / --ui-only / --mcp-stdio.")
        return

    await asyncio.gather(*coros)


def main() -> None:
    parser = argparse.ArgumentParser(prog="obsmcp")
    parser.add_argument(
        "--mcp-stdio",
        action="store_true",
        help="Run the MCP stdio tool server (for Claude Desktop / Cursor).",
    )
    parser.add_argument("--agent-only", action="store_true", help="Run monitors only, no UI.")
    parser.add_argument("--ui-only", action="store_true", help="Run local dashboard only.")
    args = parser.parse_args()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        logger.info("shutting down")


if __name__ == "__main__":  # pragma: no cover
    main()
