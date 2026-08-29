"""Transactional persistence primitives for evidence-bound Knowledge Graph payloads."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openkb.desktop_knowledge_graph_interpretation import GraphInterpretation

_MAX_NORMALIZED_LABEL_CHARS = 320
CANONICAL_GRAPH_SCHEMA_VERSION = "openkb.graph-schema.v2"
GRAPH_NORMALIZER_VERSION = "openkb.graph-normalizer.v1"
GRAPH_VERIFICATION_POLICY_VERSION = "openkb.graph-verification.v1"


@dataclass(frozen=True)
class GraphNode:
    local_id: str
    evidence_id: str
    node_type: str
    label: str
    extraction_method: str
    support_start: int | None = None
    support_end: int | None = None
    verification_state: str | None = None


@dataclass(frozen=True)
class GraphEdge:
    evidence_id: str
    source_local_id: str
    target_local_id: str
    edge_type: str
    support_score: float
    extraction_method: str
    relation_label: str | None = None
    support_start: int | None = None
    support_end: int | None = None
    verification_state: str | None = None


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
    return _persist_graph_result_in(
        connection,
        document_id,
        payload,
        capability_identity=capability_identity,
        prompt_contract_digest=prompt_contract_digest,
        extraction_method=extraction_method,
        quality="full",
        retained_count=len(payload.nodes) + len(payload.edges),
        weakened_count=0,
        rejected_count=0,
        issues=(),
        failure_signature=None,
    )


def persist_graph_interpretation_in(
    connection: sqlite3.Connection,
    document_id: str,
    interpretation: GraphInterpretation,
    *,
    capability_identity: str | None,
    prompt_contract_digest: str | None,
) -> bool:
    """Persist one completed model interpretation without exposing candidate content."""
    payload = interpretation.payload
    if payload is None or interpretation.quality is None:
        raise ValueError("Only completed Knowledge Graph interpretations can be published.")
    return _persist_graph_result_in(
        connection,
        document_id,
        payload,
        capability_identity=capability_identity,
        prompt_contract_digest=prompt_contract_digest,
        extraction_method="model",
        quality=interpretation.quality,
        retained_count=interpretation.counts.retained,
        weakened_count=interpretation.counts.weakened,
        rejected_count=interpretation.counts.rejected,
        issues=tuple(
            (issue.code, issue.path, issue.disposition, issue.failure_class)
            for issue in interpretation.issues
        ),
        failure_signature=interpretation.failure_signature,
    )


def persist_failed_graph_interpretation_in(
    connection: sqlite3.Connection,
    document_id: str,
    interpretation: GraphInterpretation,
    *,
    capability_identity: str | None,
    prompt_contract_digest: str | None,
) -> int:
    """Record one failed interpretation and return its independent-document count."""
    if interpretation.lifecycle != "failed" or interpretation.payload is not None:
        raise ValueError("Only failed Knowledge Graph interpretations can be recorded.")
    attempt_id = uuid.uuid4().hex
    compatibility = _compatibility_in(connection, document_id)
    connection.execute(
        """
        INSERT INTO knowledge_graph_attempts (
            attempt_id, document_id, result_id, lifecycle, quality,
            capability_identity, prompt_contract_digest, extraction_method,
            node_count, edge_count, retained_count, weakened_count, rejected_count,
            failure_signature, document_version, evidence_snapshot_digest,
            canonical_schema_version, normalizer_version, verification_policy_version, created_at
        ) VALUES (?, ?, NULL, 'failed', NULL, ?, ?, 'model', 0, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            attempt_id,
            document_id,
            capability_identity,
            prompt_contract_digest,
            interpretation.counts.retained,
            interpretation.counts.weakened,
            interpretation.counts.rejected,
            interpretation.failure_signature,
            *compatibility,
            _timestamp(),
        ),
    )
    _insert_attempt_issues(
        connection,
        attempt_id,
        tuple(
            (issue.code, issue.path, issue.disposition, issue.failure_class)
            for issue in interpretation.issues
        ),
    )
    if interpretation.failure_signature is None:
        return 0
    row = connection.execute(
        """
        SELECT COUNT(DISTINCT document_id)
        FROM knowledge_graph_attempts
        WHERE lifecycle = 'failed' AND failure_signature = ?
            AND capability_identity IS ? AND prompt_contract_digest IS ?
        """,
        (
            interpretation.failure_signature,
            capability_identity,
            prompt_contract_digest,
        ),
    ).fetchone()
    return int(row[0]) if row is not None else 0


def _persist_graph_result_in(
    connection: sqlite3.Connection,
    document_id: str,
    payload: GraphPayload,
    *,
    capability_identity: str | None,
    prompt_contract_digest: str | None,
    extraction_method: str,
    quality: str,
    retained_count: int,
    weakened_count: int,
    rejected_count: int,
    issues: tuple[tuple[str, str, str, str], ...],
    failure_signature: str | None,
) -> bool:
    result_id = uuid.uuid4().hex
    attempt_id = uuid.uuid4().hex
    created_at = _timestamp()
    compatibility = _compatibility_in(connection, document_id)
    connection.execute(
        """
        INSERT INTO knowledge_graph_results (
            result_id, document_id, status, capability_identity,
            prompt_contract_digest, extraction_method, node_count, edge_count, created_at,
            quality, retained_count, weakened_count, rejected_count,
            document_version, evidence_snapshot_digest, canonical_schema_version,
            normalizer_version, verification_policy_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            quality,
            retained_count,
            weakened_count,
            rejected_count,
            *compatibility,
        ),
    )
    if payload.nodes:
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
    lifecycle = "completed" if payload.nodes else "completed_empty"
    connection.execute(
        """
        INSERT INTO knowledge_graph_attempts (
            attempt_id, document_id, result_id, lifecycle, quality,
            capability_identity, prompt_contract_digest, extraction_method,
            node_count, edge_count, retained_count, weakened_count, rejected_count,
            failure_signature, document_version, evidence_snapshot_digest,
            canonical_schema_version, normalizer_version, verification_policy_version, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            attempt_id,
            document_id,
            result_id,
            lifecycle,
            quality,
            capability_identity,
            prompt_contract_digest,
            extraction_method,
            len(payload.nodes),
            len(payload.edges),
            retained_count,
            weakened_count,
            rejected_count,
            failure_signature,
            *compatibility,
            created_at,
        ),
    )
    _insert_attempt_issues(connection, attempt_id, issues)
    _select_current_result(
        connection,
        document_id,
        result_id,
        quality=quality,
        compatibility=compatibility,
    )
    return True


def _select_current_result(
    connection: sqlite3.Connection,
    document_id: str,
    result_id: str,
    *,
    quality: str,
    compatibility: tuple[str | None, str, str, str, str],
) -> None:
    if quality == "degraded":
        compatible_full = connection.execute(
            """
            SELECT 1
            FROM knowledge_graph_current AS current
            JOIN knowledge_graph_results AS results ON results.result_id = current.result_id
            WHERE current.document_id = ? AND results.quality = 'full'
                AND results.document_version IS ?
                AND results.evidence_snapshot_digest = ?
                AND results.canonical_schema_version = ?
                AND results.normalizer_version = ?
                AND results.verification_policy_version = ?
            """,
            (document_id, *compatibility),
        ).fetchone()
        if compatible_full is not None:
            return
    connection.execute(
        """
        INSERT INTO knowledge_graph_current (document_id, result_id)
        VALUES (?, ?) ON CONFLICT(document_id) DO UPDATE SET result_id = excluded.result_id
        """,
        (document_id, result_id),
    )


def _insert_attempt_issues(
    connection: sqlite3.Connection,
    attempt_id: str,
    issues: tuple[tuple[str, str, str, str], ...],
) -> None:
    if not issues:
        return
    connection.executemany(
        """
        INSERT INTO knowledge_graph_attempt_issues (
            attempt_id, ordinal, code, contract_path, disposition, failure_class
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (attempt_id, ordinal, code, path, disposition, failure_class)
            for ordinal, (code, path, disposition, failure_class) in enumerate(issues)
        ],
    )


def _insert_nodes(connection: sqlite3.Connection, nodes: tuple[GraphNode, ...]) -> dict[str, str]:
    created_at = _timestamp()
    persisted = {node.local_id: uuid.uuid4().hex for node in nodes}
    connection.executemany(
        """
        INSERT INTO knowledge_graph_nodes (
            node_id, evidence_id, node_type, label, normalized_label, extraction_method, created_at,
            support_start, support_end, verification_state
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                node.support_start,
                node.support_end,
                node.verification_state or "source_anchored",
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
            edge.relation_label,
            edge.support_start,
            edge.support_end,
            edge.verification_state or "source_anchored",
        )
        for edge in edges
        if edge.source_local_id in local_to_persisted and edge.target_local_id in local_to_persisted
    ]
    if values:
        connection.executemany(
            """
            INSERT INTO knowledge_graph_edges (
                edge_id, evidence_id, source_node_id, target_node_id, edge_type, support_score,
                extraction_method, created_at, relation_label, support_start, support_end,
                verification_state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
    return tuple(str(value[0]) for value in values)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compatibility_in(
    connection: sqlite3.Connection,
    document_id: str,
) -> tuple[str | None, str, str, str, str]:
    document = connection.execute(
        "SELECT asset_sha256 FROM source_documents WHERE document_id = ?",
        (document_id,),
    ).fetchone()
    if document is None:
        raise ValueError("Knowledge Graph document is unavailable.")
    evidence = connection.execute(
        """
        SELECT evidence_id, text
        FROM evidence_refs
        WHERE document_id = ?
        ORDER BY ordinal, evidence_id
        """,
        (document_id,),
    ).fetchall()
    serialized = json.dumps(
        [(str(row[0]), str(row[1])) for row in evidence],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        str(document[0]),
        hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        CANONICAL_GRAPH_SCHEMA_VERSION,
        GRAPH_NORMALIZER_VERSION,
        GRAPH_VERIFICATION_POLICY_VERSION,
    )
