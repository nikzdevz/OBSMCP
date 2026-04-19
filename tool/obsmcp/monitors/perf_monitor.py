"""Periodic CPU / memory / disk sampling via psutil."""

from __future__ import annotations

import asyncio
import contextlib

try:
    import psutil

    HAS_PSUTIL = True
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore[assignment]
    HAS_PSUTIL = False

from ..client.http_client import BackendClient
from ..config import Config
from ..utils.logger import get_logger

logger = get_logger("obsmcp.perf")


class PerformanceMonitor:
    def __init__(self, client: BackendClient, config: Config) -> None:
        self.client = client
        self.project_id = config.project_id or None
        self.interval = config.perf_log_interval_seconds

    async def run(self) -> None:
        if not HAS_PSUTIL or psutil is None:
            logger.warning("psutil not installed — perf monitor disabled")
            return
        while True:
            await asyncio.sleep(self.interval)
            vm = psutil.virtual_memory()
            logs = [
                {
                    "project_id": self.project_id,
                    "metric_name": "cpu_percent",
                    "metric_value": float(psutil.cpu_percent(interval=0.1)),
                    "unit": "percent",
                    "tags": {"core_count": psutil.cpu_count()},
                },
                {
                    "project_id": self.project_id,
                    "metric_name": "memory_percent",
                    "metric_value": float(vm.percent),
                    "unit": "percent",
                    "tags": {
                        "total_gb": round(vm.total / 1e9, 2),
                        "available_gb": round(vm.available / 1e9, 2),
                    },
                },
            ]
            with contextlib.suppress(Exception):
                logs.append(
                    {
                        "project_id": self.project_id,
                        "metric_name": "disk_percent",
                        "metric_value": float(psutil.disk_usage("/").percent),
                        "unit": "percent",
                    }
                )
            await self.client.ingest_performance_logs(logs)
