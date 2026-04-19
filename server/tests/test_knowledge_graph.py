def test_nodes_and_edges(client):
    n1 = client.post(
        "/api/knowledge-graph/nodes",
        json={"node_type": "module", "name": "foo"},
    ).json()
    n2 = client.post(
        "/api/knowledge-graph/nodes",
        json={"node_type": "module", "name": "bar"},
    ).json()
    assert n1["id"] and n2["id"]

    e = client.post(
        "/api/knowledge-graph/edges",
        json={"from_node_id": n1["id"], "to_node_id": n2["id"], "edge_type": "imports"},
    ).json()
    assert e["edge_type"] == "imports"

    graph = client.get("/api/knowledge-graph").json()
    assert len(graph["nodes"]) == 2
    assert len(graph["edges"]) == 1

    query = client.get(f"/api/knowledge-graph/query?node_id={n1['id']}&depth=1").json()
    assert {n["id"] for n in query["nodes"]} == {n1["id"], n2["id"]}
