import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def secure_client(tmp_path, monkeypatch):
    db_path = tmp_path / "obsmcp.db"
    monkeypatch.setenv("OBSMCP_DB_PATH", str(db_path))
    monkeypatch.setenv("OBSMCP_API_TOKEN", "secret123")
    from obsmcp_server import config as cfg_module
    from obsmcp_server import db as db_module

    cfg_module.reset_config_cache()
    db_module._state["db_path"] = None  # type: ignore[attr-defined]
    db_module._tls = __import__("threading").local()  # type: ignore[attr-defined]

    from obsmcp_server.main import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c


def test_public_health_when_token_set(secure_client):
    assert secure_client.get("/healthz").status_code == 200


def test_api_requires_token(secure_client):
    res = secure_client.get("/api/tasks")
    assert res.status_code == 401


def test_api_accepts_bearer(secure_client):
    res = secure_client.get("/api/tasks", headers={"Authorization": "Bearer secret123"})
    assert res.status_code == 200


def test_api_accepts_query_token(secure_client):
    res = secure_client.get("/api/tasks?token=secret123")
    assert res.status_code == 200
