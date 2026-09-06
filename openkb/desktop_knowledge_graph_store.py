"""Persist current-epoch, evidence-bound semantic relation results."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from openkb.desktop_canonical_json import canonical_json_digest

if TYPE_CHECKING:
    from openkb.desktop_semantic_graph import SemanticGraphInterpretation

SEMANTIC_GRAPH_SCHEMA_VERSION = "openkb.semantic-identity-graph.v2"
SEMANTIC_GRAPH_NORMALIZER_VERSION = "openkb.semantic-identity-normalizer.v2"
SEMANTIC_GRAPH_VERIFICATION_POLICY_VERSION = "openkb.semantic-relation-verification.v1"


def persist_semantic_graph_interpretation_in(
    connection: sqlite3.Connection,
    document_id: str,
    interpretation: SemanticGraphInterpretation,
    *,
    node_count: int,
    capability_identity: str | None,
    prompt_contract_digest: str | None,
    candidate_generation_id: str | None,
    candidate_generation_digest: str | None,
) -> str:
    """Publish metadata for one verified, model-labelled relation assertion set."""
    if interpretation.lifecycle not in {"completed", "completed_empty"}:
        raise ValueError("Only completed semantic graph interpretations can be published.")
    if interpretation.quality not in {"full", "degraded"}:
        raise ValueError("Completed semantic graph interpretations require a quality.")
    if not candidate_generation_id or not candidate_generation_digest:
        raise ValueError("Semantic graph results require a Candidate Registry Generation.")
    if node_count < 0:
        raise ValueError("Semantic graph identity count cannot be negative.")

    relation_count = len(interpretation.relations)
    lifecycle = "completed" if node_count else "completed_empty"
    if lifecycle == "completed_empty" and relation_count:
        raise ValueError("An empty identity registry cannot publish semantic relations.")

    result_id = uuid.uuid4().hex
    compatibility = _compatibility_in(connection, document_id)
    connection.execute(
        """
        INSERT INTO knowledge_graph_results (
            result_id, document_id, status, capability_identity,
            prompt_contract_digest, node_count, edge_count, created_at,
            quality, retained_count, weakened_count, rejected_count,
            document_version, evidence_snapshot_digest, canonical_schema_version,
            normalizer_version, verification_policy_version,
            candidate_generation_id, candidate_generation_digest
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            result_id,
            document_id,
            lifecycle,
            capability_identity,
            prompt_contract_digest,
            node_count,
            relation_count,
            _timestamp(),
            interpretation.quality,
            interpretation.counts.retained,
            interpretation.counts.weakened,
            interpretation.counts.rejected,
            *compatibility,
            candidate_generation_id,
            candidate_generation_digest,
        ),
    )
    if not _compatible_full_is_current(
        connection,
        document_id,
        quality=interpretation.quality,
        compatibility=compatibility,
        candidate_generation_id=candidate_generation_id,
        candidate_generation_digest=candidate_generation_digest,
    ):
        connection.execute(
            """
            INSERT INTO knowledge_graph_current (document_id, result_id)
            VALUES (?, ?)
            ON CONFLICT(document_id) DO UPDATE SET result_id = excluded.result_id
            """,
            (document_id, result_id),
        )
    return result_id


def _compatible_full_is_current(
    connection: sqlite3.Connection,
    document_id: str,
    *,
    quality: str,
    compatibility: tuple[str, str, str, str, str],
    candidate_generation_id: str,
    candidate_generation_digest: str,
) -> bool:
    """Protect an equivalent full result from replacement by a degraded retry."""
    if quality != "degraded":
        return False
    row = connection.execute(
        """
        SELECT 1
        FROM knowledge_graph_current AS current
        JOIN knowledge_graph_results AS results ON results.result_id = current.result_id
        WHERE current.document_id = ? AND results.quality = 'full'
            AND results.document_version = ?
            AND results.evidence_snapshot_digest = ?
            AND results.canonical_schema_version = ?
            AND results.normalizer_version = ?
            AND results.verification_policy_version = ?
            AND results.candidate_generation_id = ?
            AND results.candidate_generation_digest = ?
        """,
        (
            document_id,
            *compatibility,
            candidate_generation_id,
            candidate_generation_digest,
        ),
    ).fetchone()
    return row is not None


def _compatibility_in(
    connection: sqlite3.Connection,
    document_id: str,
) -> tuple[str, str, str, str, str]:
    document = connection.execute(
        "SELECT asset_sha256 FROM source_documents WHERE document_id = ?",
        (document_id,),
    ).fetchone()
    if document is None or not isinstance(document[0], str) or not document[0]:
        raise ValueError("Semantic graph document is unavailable.")
    evidence = connection.execute(
        """
        SELECT evidence_id, text
        FROM evidence_refs
        WHERE document_id = ?
        ORDER BY ordinal, evidence_id
        """,
        (document_id,),
    ).fetchall()
    return (
        document[0],
        canonical_json_digest([(str(row[0]), str(row[1])) for row in evidence]),
        SEMANTIC_GRAPH_SCHEMA_VERSION,
        SEMANTIC_GRAPH_NORMALIZER_VERSION,
        SEMANTIC_GRAPH_VERIFICATION_POLICY_VERSION,
    )


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()
