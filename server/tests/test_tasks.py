def test_task_crud_flow(client):
    res = client.post("/api/tasks", json={"title": "Write docs"})
    assert res.status_code == 200
    task = res.json()
    task_id = task["id"]
    assert task["status"] == "open"

    res = client.get("/api/tasks")
    assert res.status_code == 200
    assert any(t["id"] == task_id for t in res.json())

    res = client.put(f"/api/tasks/{task_id}", json={"status": "done"})
    assert res.status_code == 200
    assert res.json()["status"] == "done"

    res = client.delete(f"/api/tasks/{task_id}")
    assert res.status_code == 200
    assert res.json()["ok"] is True


def test_task_bulk(client):
    ids = []
    for i in range(3):
        res = client.post("/api/tasks", json={"title": f"t{i}"})
        ids.append(res.json()["id"])
    ops = [{"id": ids[0], "action": "update", "data": {"status": "in_progress"}}, {"id": ids[1], "action": "delete"}]
    res = client.post("/api/tasks/bulk", json={"operations": ops})
    assert res.status_code == 200
    remaining = client.get("/api/tasks").json()
    remaining_ids = {t["id"] for t in remaining}
    assert ids[0] in remaining_ids
    assert ids[1] not in remaining_ids
    assert ids[2] in remaining_ids


def test_stats(client):
    client.post("/api/tasks", json={"title": "one", "status": "open"})
    client.post("/api/tasks", json={"title": "two", "status": "done"})
    res = client.get("/api/stats")
    assert res.status_code == 200
    stats = res.json()
    assert stats["tasks"]["total"] >= 2
    assert stats["tasks"]["done"] >= 1
