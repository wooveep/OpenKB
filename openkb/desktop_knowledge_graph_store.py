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


def persist_graph_payload_in(
    connection: sqlite3.Connection,
    document_id: str,
    payload: GraphPayload,
    *,
    capability_identity: str | None,
    prompt_contract_digest: str | None,
    extraction_method: str,
) -> bool:
    """Publish one immutable result and atomically advance its document pointer."""
    result_id = uuid.uuid4().hex
    created_at = _timestamp()
    connection.execute(
        """
        INSERT INTO knowledge_graph_results (
            result_id, document_id, status, capability_identity,
            prompt_contract_digest, extraction_method, node_count, edge_count, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            result_id,
            document_id,
            "completed" if payload.nodes else "completed_empty",
            capability_identity,
            prompt_contract_digest,
            extraction_method,
            len(payload.nodes),
            len(payload.edges),
            created_at,
        ),
    )
    if not payload.nodes:
        _select_current_result(connection, document_id, result_id)
        return True
    local_to_persisted = _insert_nodes(connection, payload.nodes)
    connection.executemany(
        "INSERT INTO knowledge_graph_result_nodes (result_id, node_id) VALUES (?, ?)",
        [(result_id, node_id) for node_id in local_to_persisted.values()],
    )
    edge_ids = _insert_edges(connection, payload.edges, local_to_persisted)
    connection.executemany(
        "INSERT INTO knowledge_graph_result_edges (result_id, edge_id) VALUES (?, ?)",
        [(result_id, edge_id) for edge_id in edge_ids],
    )
    _select_current_result(connection, document_id, result_id)
    return True


def _select_current_result(
    connection: sqlite3.Connection, document_id: str, result_id: str
) -> None:
    connection.execute(
        """
        INSERT INTO knowledge_graph_current (document_id, result_id)
        VALUES (?, ?) ON CONFLICT(document_id) DO UPDATE SET result_id = excluded.result_id
        """,
        (document_id, result_id),
    )


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
) -> tuple[str, ...]:
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
    return tuple(str(value[0]) for value in values)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()
