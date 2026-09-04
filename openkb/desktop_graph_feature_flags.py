"""Conservative, evaluation-backed activation for Desktop local graph retrieval."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from pathlib import Path

from openkb.desktop_workspace import desktop_state_database_path, desktop_state_dir
from openkb.locks import kb_ingest_lock

_LOCAL_GRAPH_KEY = "local_graph"


def local_graph_default_enabled(kb_dir: Path) -> bool:
    """Return false unless a matching approved revision enables local graph use."""
    resolved = kb_dir.expanduser().resolve()
    database_path = desktop_state_database_path(resolved)
    if not database_path.is_file():
        return False
    try:
        connection = sqlite3.connect(database_path)
        try:
            row = connection.execute(
                """
                SELECT enabled, approved_snapshot_revision
                FROM desktop_graph_feature_flags
                WHERE feature_key = ?
                """,
                (_LOCAL_GRAPH_KEY,),
            ).fetchone()
            if (
                row is None
                or row[0] != 1
                or isinstance(row[1], bool)
                or not isinstance(row[1], int)
            ):
                return False
            return knowledge_snapshot_revision_in(connection) == row[1]
        finally:
            connection.close()
    except sqlite3.Error:
        return False


def desktop_knowledge_snapshot_digest(kb_dir: Path) -> str:
    """Hash the available retrieval corpus without retaining it in an evaluation report."""
    resolved = kb_dir.expanduser().resolve()
    database_path = desktop_state_database_path(resolved)
    if not database_path.is_file():
        raise ValueError("Desktop retrieval evaluation requires an open knowledge base.")
    connection = sqlite3.connect(database_path)
    try:
        return knowledge_snapshot_digest_in(connection, resolved)
    except sqlite3.Error as error:
        raise ValueError("Desktop retrieval evaluation evidence is unavailable.") from error
    finally:
        connection.close()


def desktop_knowledge_snapshot_revision(kb_dir: Path) -> int:
    """Read the constant-time revision used by normal local-graph gating."""
    resolved = kb_dir.expanduser().resolve()
    database_path = desktop_state_database_path(resolved)
    if not database_path.is_file():
        raise ValueError("Desktop retrieval evaluation requires an open knowledge base.")
    connection = sqlite3.connect(database_path)
    try:
        return knowledge_snapshot_revision_in(connection)
    except sqlite3.Error as error:
        raise ValueError("Desktop retrieval evaluation evidence is unavailable.") from error
    finally:
        connection.close()


def enable_local_graph_after_evaluation(
    kb_dir: Path,
    suite_digest: str,
    knowledge_snapshot_digest: str,
    knowledge_snapshot_revision: int,
) -> None:
    """Persist one reviewed, passing suite as the authority for local graph defaults."""
    if not isinstance(suite_digest, str) or not suite_digest:
        raise ValueError("A passing Desktop retrieval evaluation suite digest is required.")
    if not isinstance(knowledge_snapshot_digest, str) or not knowledge_snapshot_digest:
        raise ValueError("A passing Desktop retrieval evaluation corpus digest is required.")
    if (
        isinstance(knowledge_snapshot_revision, bool)
        or not isinstance(knowledge_snapshot_revision, int)
        or knowledge_snapshot_revision < 0
    ):
        raise ValueError("A passing Desktop retrieval evaluation corpus revision is required.")
    resolved = kb_dir.expanduser().resolve()
    with kb_ingest_lock(desktop_state_dir(resolved)):
        connection = sqlite3.connect(desktop_state_database_path(resolved))
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN IMMEDIATE")
            if knowledge_snapshot_revision_in(connection) != knowledge_snapshot_revision:
                raise ValueError(
                    "The Desktop Knowledge Base changed after this retrieval evaluation; "
                    "run it again."
                )
            if knowledge_snapshot_digest_in(connection, resolved) != knowledge_snapshot_digest:
                raise ValueError(
                    "The Desktop Knowledge Base changed after this retrieval evaluation; "
                    "run it again."
                )
            updated = connection.execute(
                """
                UPDATE desktop_graph_feature_flags
                SET enabled = 1,
                    approved_suite_digest = ?,
                    approved_snapshot_digest = ?,
                    approved_snapshot_revision = ?,
                    updated_at = ?
                WHERE feature_key = ?
                """,
                (
                    suite_digest,
                    knowledge_snapshot_digest,
                    knowledge_snapshot_revision,
                    _timestamp(),
                    _LOCAL_GRAPH_KEY,
                ),
            )
            if updated.rowcount != 1:
                raise ValueError("Desktop local graph feature state is unavailable.")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


def _timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def knowledge_snapshot_revision_in(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT revision FROM desktop_retrieval_corpus_state WHERE singleton = 1"
    ).fetchone()
    if row is None or isinstance(row[0], bool) or not isinstance(row[0], int):
        raise ValueError("Desktop retrieval evaluation corpus state is unavailable.")
    return row[0]


def knowledge_snapshot_digest_in(connection: sqlite3.Connection, kb_dir: Path) -> str:
    """Return a stable, KB-local summary of every retrieval-affecting record."""
    payload = {
        "knowledge_base_path_digest": hashlib.sha256(str(kb_dir).encode("utf-8")).hexdigest(),
        "documents": _rows(
            connection,
            """
            SELECT document_id, asset_sha256, display_name, source_format
            FROM source_documents
            WHERE availability = 'available'
            ORDER BY document_id
            """,
        ),
        "blocks": _rows(
            connection,
            """
            SELECT document_ir_blocks.document_id, document_ir_blocks.ordinal,
                document_ir_blocks.kind, document_ir_blocks.text,
                document_ir_blocks.heading_path, document_ir_blocks.locator_json
            FROM document_ir_blocks
            JOIN source_documents
                ON source_documents.document_id = document_ir_blocks.document_id
            WHERE source_documents.availability = 'available'
            ORDER BY document_ir_blocks.document_id, document_ir_blocks.ordinal
            """,
        ),
        "evidence": _rows(
            connection,
            """
            SELECT evidence_occurrences.document_id, evidence_occurrences.ordinal,
                evidence_occurrences.evidence_id, evidence_refs.text,
                evidence_refs.locator_json
            FROM evidence_occurrences
            JOIN source_documents
                ON source_documents.document_id = evidence_occurrences.document_id
            JOIN evidence_refs ON evidence_refs.evidence_id = evidence_occurrences.evidence_id
            WHERE source_documents.availability = 'available'
            ORDER BY evidence_occurrences.document_id, evidence_occurrences.ordinal
            """,
        ),
        "graph_selection": _rows(
            connection,
            """
            SELECT current.document_id, results.status, results.capability_identity,
                results.prompt_contract_digest, results.extraction_method,
                results.node_count, results.edge_count
            FROM knowledge_graph_current AS current
            JOIN knowledge_graph_results AS results ON results.result_id = current.result_id
            JOIN source_documents AS documents ON documents.document_id = current.document_id
            WHERE documents.availability = 'available'
            ORDER BY current.document_id
            """,
        ),
        "graph_nodes": _rows(
            connection,
            """
            SELECT nodes.node_id, nodes.evidence_id, nodes.node_type, nodes.label,
                nodes.normalized_label, nodes.extraction_method
            FROM current_knowledge_graph_nodes AS nodes
            JOIN evidence_occurrences
                ON evidence_occurrences.evidence_id = nodes.evidence_id
            JOIN source_documents
                ON source_documents.document_id = evidence_occurrences.document_id
            WHERE source_documents.availability = 'available'
            ORDER BY nodes.node_id
            """,
        ),
        "graph_edges": _rows(
            connection,
            """
            SELECT edges.edge_id, edges.evidence_id, edges.source_node_id,
                edges.target_node_id, edges.edge_type, edges.support_score,
                edges.extraction_method
            FROM current_knowledge_graph_edges AS edges
            JOIN evidence_occurrences
                ON evidence_occurrences.evidence_id = edges.evidence_id
            JOIN source_documents
                ON source_documents.document_id = evidence_occurrences.document_id
            WHERE source_documents.availability = 'available'
            ORDER BY edges.edge_id
            """,
        ),
        "knowledge_generation": _rows(
            connection,
            """
            SELECT state.current_generation_id, items.item_key, items.kind, items.title,
                items.content_sha256, items.source_document_id
            FROM knowledge_generation_state AS state
            LEFT JOIN knowledge_generation_items AS items
                ON items.generation_id = state.current_generation_id
            WHERE state.singleton = 1
            ORDER BY state.current_generation_id, items.item_key
            """,
        ),
        "semantic_relationships": _rows(
            connection,
            """
            SELECT relationships.generation_id, relationships.source_item_key,
                relationships.target_item_key, relationships.relation_kind,
                relationships.applicability_json, relationships.provenance
            FROM knowledge_generation_state AS state
            JOIN knowledge_generation_relationships AS relationships
              ON relationships.generation_id = state.current_generation_id
            WHERE state.singleton = 1
            ORDER BY relationships.source_item_key, relationships.target_item_key,
                relationships.relation_kind
            """,
        ),
        "semantic_relationship_sources": _rows(
            connection,
            """
            SELECT sources.generation_id, sources.source_item_key,
                sources.target_item_key, sources.relation_kind,
                sources.binding_role, sources.evidence_id
            FROM knowledge_generation_state AS state
            JOIN knowledge_generation_relationship_sources AS sources
              ON sources.generation_id = state.current_generation_id
            WHERE state.singleton = 1
            ORDER BY sources.source_item_key, sources.target_item_key,
                sources.relation_kind, sources.binding_role, sources.evidence_id
            """,
        ),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _rows(connection: sqlite3.Connection, query: str) -> list[list[object]]:
    return [list(row) for row in connection.execute(query).fetchall()]
