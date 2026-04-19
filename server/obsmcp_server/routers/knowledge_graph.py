"""Knowledge graph endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from ..db import get_db, rows_to_list
from ..sse import broadcast_event
from ._helpers import delete_row, insert_row, list_rows, new_id, now_iso, update_row

router = APIRouter()


class NodeCreate(BaseModel):
    id: str | None = None
    project_id: str | None = None
    agent_id: str | None = None
    node_type: str
    name: str
    description: str | None = None
    metadata: dict[str, Any] | None = None


class NodeUpdate(BaseModel):
    node_type: str | None = None
    name: str | None = None
    description: str | None = None
    metadata: dict[str, Any] | None = None


class EdgeCreate(BaseModel):
    id: str | None = None
    project_id: str | None = None
    from_node_id: str
    to_node_id: str
    edge_type: str
    weight: float = 1.0
    metadata: dict[str, Any] | None = None


class NodesBulk(BaseModel):
    nodes: list[NodeCreate]


class EdgesBulk(BaseModel):
    edges: list[EdgeCreate]


@router.get("")
def get_graph() -> dict[str, Any]:
    return {
        "nodes": list_rows("knowledge_nodes"),
        "edges": list_rows("knowledge_edges"),
    }


@router.post("/nodes")
def add_node(body: NodeCreate) -> dict[str, Any]:
    data = {
        "id": body.id or new_id(),
        **body.model_dump(exclude_unset=False, exclude={"id"}),
        "created_at": now_iso(),
    }
    row = insert_row("knowledge_nodes", data, json_columns=("metadata",))
    broadcast_event("node_created", row)
    return row


@router.post("/nodes/bulk")
def add_nodes_bulk(body: NodesBulk) -> dict[str, Any]:
    rows = []
    for n in body.nodes:
        data = {
            "id": n.id or new_id(),
            **n.model_dump(exclude_unset=False, exclude={"id"}),
            "created_at": now_iso(),
        }
        rows.append(insert_row("knowledge_nodes", data, json_columns=("metadata",)))
    broadcast_event("nodes_bulk_created", {"count": len(rows)})
    return {"count": len(rows), "nodes": rows}


@router.get("/nodes")
def list_nodes(type: str | None = None, project_id: str | None = None) -> list[dict[str, Any]]:
    wheres: list[str] = []
    params: list[Any] = []
    if type:
        wheres.append("node_type=?")
        params.append(type)
    if project_id:
        wheres.append("project_id=?")
        params.append(project_id)
    return list_rows("knowledge_nodes", " AND ".join(wheres), tuple(params))


@router.put("/nodes/{node_id}")
def update_node(node_id: str, body: NodeUpdate) -> dict[str, Any]:
    row = update_row(
        "knowledge_nodes", node_id, body.model_dump(exclude_unset=True), json_columns=("metadata",)
    )
    broadcast_event("node_updated", row)
    return row


@router.delete("/nodes/{node_id}")
def delete_node(node_id: str) -> dict[str, Any]:
    delete_row("knowledge_nodes", node_id)
    broadcast_event("node_deleted", {"id": node_id})
    return {"ok": True}


@router.post("/edges")
def add_edge(body: EdgeCreate) -> dict[str, Any]:
    data = {
        "id": body.id or new_id(),
        **body.model_dump(exclude_unset=False, exclude={"id"}),
        "created_at": now_iso(),
    }
    row = insert_row("knowledge_edges", data, json_columns=("metadata",))
    broadcast_event("edge_created", row)
    return row


@router.post("/edges/bulk")
def add_edges_bulk(body: EdgesBulk) -> dict[str, Any]:
    rows = []
    for e in body.edges:
        data = {
            "id": e.id or new_id(),
            **e.model_dump(exclude_unset=False, exclude={"id"}),
            "created_at": now_iso(),
        }
        rows.append(insert_row("knowledge_edges", data, json_columns=("metadata",)))
    broadcast_event("edges_bulk_created", {"count": len(rows)})
    return {"count": len(rows), "edges": rows}


@router.get("/edges")
def list_edges(
    type: str | None = None,
    from_id: str | None = None,
    to_id: str | None = None,
) -> list[dict[str, Any]]:
    wheres: list[str] = []
    params: list[Any] = []
    if type:
        wheres.append("edge_type=?")
        params.append(type)
    if from_id:
        wheres.append("from_node_id=?")
        params.append(from_id)
    if to_id:
        wheres.append("to_node_id=?")
        params.append(to_id)
    return list_rows("knowledge_edges", " AND ".join(wheres), tuple(params))


@router.delete("/edges/{edge_id}")
def delete_edge(edge_id: str) -> dict[str, Any]:
    delete_row("knowledge_edges", edge_id)
    broadcast_event("edge_deleted", {"id": edge_id})
    return {"ok": True}


@router.get("/query")
def query_graph(
    node_id: str | None = None,
    edge_type: str | None = None,
    depth: int = 1,
    q: str | None = None,
) -> dict[str, Any]:
    db = get_db()
    if q:
        rows = db.execute(
            "SELECT * FROM knowledge_nodes WHERE name LIKE ? OR description LIKE ? LIMIT 500",
            (f"%{q}%", f"%{q}%"),
        ).fetchall()
        return {"nodes": rows_to_list(rows), "edges": []}
    if node_id:
        visited: set[str] = {node_id}
        frontier = {node_id}
        edges_out: list[dict[str, Any]] = []
        for _ in range(max(1, depth)):
            placeholders = ",".join(["?"] * len(frontier))
            if edge_type:
                rows = db.execute(
                    f"SELECT * FROM knowledge_edges WHERE edge_type=? AND (from_node_id IN ({placeholders}) OR to_node_id IN ({placeholders}))",
                    (edge_type, *frontier, *frontier),
                ).fetchall()
            else:
                rows = db.execute(
                    f"SELECT * FROM knowledge_edges WHERE from_node_id IN ({placeholders}) OR to_node_id IN ({placeholders})",
                    (*frontier, *frontier),
                ).fetchall()
            edges_list = rows_to_list(rows)
            edges_out.extend(edges_list)
            next_frontier: set[str] = set()
            for e in edges_list:
                for k in ("from_node_id", "to_node_id"):
                    if e[k] not in visited:
                        next_frontier.add(e[k])
            visited.update(next_frontier)
            frontier = next_frontier
            if not frontier:
                break
        if visited:
            placeholders = ",".join(["?"] * len(visited))
            node_rows = db.execute(
                f"SELECT * FROM knowledge_nodes WHERE id IN ({placeholders})",
                tuple(visited),
            ).fetchall()
        else:
            node_rows = []
        return {"nodes": rows_to_list(node_rows), "edges": edges_out}
    return {"nodes": list_rows("knowledge_nodes"), "edges": list_rows("knowledge_edges")}


@router.get("/stats")
def graph_stats() -> dict[str, Any]:
    db = get_db()
    node_counts = {
        row["node_type"]: row["c"]
        for row in db.execute(
            "SELECT node_type, COUNT(*) as c FROM knowledge_nodes GROUP BY node_type"
        ).fetchall()
    }
    edge_counts = {
        row["edge_type"]: row["c"]
        for row in db.execute(
            "SELECT edge_type, COUNT(*) as c FROM knowledge_edges GROUP BY edge_type"
        ).fetchall()
    }
    return {
        "nodes": {"total": sum(node_counts.values()), "by_type": node_counts},
        "edges": {"total": sum(edge_counts.values()), "by_type": edge_counts},
    }
