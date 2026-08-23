"""Transactional persistence primitives for evidence-bound Knowledge Graph payloads."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

_MAX_NORMALIZED_LABEL_CHARS = 320


@dataclass(frozen=True)
class GraphNode:
    local_id: str
    evidence_id: str
    node_type: str
    label: str
    extraction_method: str


@dataclass(frozen=True)
class GraphEdge:
    evidence_id: str
    source_local_id: str
    target_local_id: str
    edge_type: str
    support_score: float
    extraction_method: str


@dataclass(frozen=True)
class GraphPayload:
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]


def persist_graph_payload_in(connection: sqlite3.Connection, payload: GraphPayload) -> bool:
    """Insert a payload into the caller-owned transaction without committing it."""
    if not payload.nodes:
        return False
    evidence_ids = tuple(dict.fromkeys(node.evidence_id for node in payload.nodes))
    existing = _existing_evidence_ids(connection, evidence_ids)
    nodes = tuple(node for node in payload.nodes if node.evidence_id not in existing)
    if not nodes:
        return False
    local_to_persisted = _insert_nodes(connection, nodes)
    _insert_edges(connection, payload.edges, local_to_persisted)
    return True


def _existing_evidence_ids(
    connection: sqlite3.Connection, evidence_ids: tuple[str, ...]
) -> set[str]:
    rows = connection.execute(
        f"""
        SELECT DISTINCT evidence_id
        FROM knowledge_graph_nodes
        WHERE evidence_id IN ({_placeholders(evidence_ids)})
        """,
        evidence_ids,
    ).fetchall()
    return {str(row[0]) for row in rows}


def _insert_nodes(connection: sqlite3.Connection, nodes: tuple[GraphNode, ...]) -> dict[str, str]:
    created_at = _timestamp()
    persisted = {node.local_id: uuid.uuid4().hex for node in nodes}
    connection.executemany(
        """
        INSERT INTO knowledge_graph_nodes (
            node_id, evidence_id, node_type, label, normalized_label, extraction_method, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                persisted[node.local_id],
                node.evidence_id,
                node.node_type,
                node.label,
                " ".join(node.label.casefold().split())[:_MAX_NORMALIZED_LABEL_CHARS],
                node.extraction_method,
                created_at,
            )
            for node in nodes
        ],
    )
    return persisted


def _insert_edges(
    connection: sqlite3.Connection,
    edges: tuple[GraphEdge, ...],
    local_to_persisted: dict[str, str],
) -> None:
    values = [
        (
            uuid.uuid4().hex,
            edge.evidence_id,
            local_to_persisted[edge.source_local_id],
            local_to_persisted[edge.target_local_id],
            edge.edge_type,
            edge.support_score,
            edge.extraction_method,
            _timestamp(),
        )
        for edge in edges
        if edge.source_local_id in local_to_persisted and edge.target_local_id in local_to_persisted
    ]
    if values:
        connection.executemany(
            """
            INSERT INTO knowledge_graph_edges (
                edge_id, evidence_id, source_node_id, target_node_id, edge_type, support_score,
                extraction_method, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )


def _placeholders(values: tuple[object, ...]) -> str:
    return ", ".join("?" for _value in values)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()
