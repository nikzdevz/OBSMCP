def test_healthz(client):
    res = client.get("/healthz")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_runtime_discovery(client):
    res = client.get("/runtime-discovery")
    assert res.status_code == 200
    body = res.json()
    assert body["version"] == "0.1.0"
    assert "tasks" in body["features"]


def test_mode_local(client):
    res = client.get("/mode")
    assert res.status_code == 200
    assert res.json() == {"mode": "local"}
