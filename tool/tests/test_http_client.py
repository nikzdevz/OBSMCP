import asyncio

import pytest
from obsmcp.client.http_client import BackendClient
from obsmcp.config import Config


@pytest.fixture()
def client(tmp_path):
    cfg = Config()
    cfg.local_db_path = str(tmp_path / "obsmcp.db")
    cfg.project_path = str(tmp_path)
    cfg.project_id = "p-1"
    cfg.backend_url = ""  # standalone
    cfg.mode = "standalone"
    c = BackendClient(cfg)
    yield c
    asyncio.run(c.close())


def test_local_task_crud(client):
    async def run() -> None:
        t = await client.create_task({"title": "write tests"})
        assert t["id"]
        assert client.list_tasks()[0]["id"] == t["id"]
        await client.update_task(t["id"], {"status": "done"})
        assert client.list_tasks()[0]["status"] == "done"
        await client.delete_task(t["id"])
        assert client.list_tasks() == []

    asyncio.run(run())


def test_local_blocker_lifecycle(client):
    async def run() -> None:
        b = await client.log_blocker({"description": "stuck"})
        assert b["status"] == "active"
        await client.resolve_blocker(b["id"], "fixed")
        rows = client._query("blockers", "id=?", (b["id"],))  # noqa: SLF001
        assert rows[0]["status"] == "resolved"

    asyncio.run(run())
