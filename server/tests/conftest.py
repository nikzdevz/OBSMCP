"""Shared fixtures for backend tests."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    db_path = tmp_path / "obsmcp.db"
    monkeypatch.setenv("OBSMCP_DB_PATH", str(db_path))
    monkeypatch.setenv("OBSMCP_API_TOKEN", "")
    # Force config + db re-init per-test
    from obsmcp_server import config as cfg_module
    from obsmcp_server import db as db_module

    cfg_module.reset_config_cache()
    db_module._state["db_path"] = None  # type: ignore[attr-defined]
    db_module._tls = __import__("threading").local()  # type: ignore[attr-defined]

    from obsmcp_server.main import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c

    # Clean up env to avoid leaking
    for k in ("OBSMCP_DB_PATH", "OBSMCP_API_TOKEN"):
        os.environ.pop(k, None)
