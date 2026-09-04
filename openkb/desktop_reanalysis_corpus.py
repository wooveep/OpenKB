"""Atomic activation of completed extended Knowledge Reanalysis runs."""

from __future__ import annotations

import json
import sqlite3

from openkb.desktop_corpus_knowledge import synthesize_qualified_corpus_in
from openkb.desktop_import_artifacts import DocumentIRBlock
from openkb.desktop_knowledge_analysis import (
    DesktopKnowledgeAnalysis,
    knowledge_analysis_from_checkpoint,
    knowledge_analysis_provenance_from_checkpoint,
)
from openkb.desktop_knowledge_analysis_reuse import (
    ReusableKnowledgeAnalysis,
    analysis_evidence_for_document_in,
    canonical_analysis_document_id_in,
    canonical_analysis_evidence_map_in,
)
from openkb.desktop_knowledge_candidate_pipeline import materialize_candidate_registry_in
from openkb.desktop_missing_sources import record_missing_source_candidates_in


def activate_completed_corpus_reanalysis_in(
    connection: sqlite3.Connection, *, run_id: str, now: str
) -> int | None:
    """Activate only when every selected job has a complete extended checkpoint."""
    rows = connection.execute(
        """
        SELECT document_id, status, checkpoint_json
        FROM knowledge_reanalysis_jobs
        WHERE run_id = ? ORDER BY created_at, rowid
        """,
        (run_id,),
    ).fetchall()
    if not rows or any(str(row[1]) != "completed" or row[2] is None for row in rows):
        return None
    prepared: list[
        tuple[
            str,
            DesktopKnowledgeAnalysis,
            str,
            tuple[tuple[str, DocumentIRBlock], ...],
        ]
    ] = []
    for row in rows:
        checkpoint = _checkpoint(str(row[2]))
        analysis = knowledge_analysis_from_checkpoint(checkpoint)
        if analysis is None or not analysis.corpus_ready:
            return None
        document_id = str(row[0])
        evidence = analysis_evidence_for_document_in(connection, document_id)
        provenance_json = knowledge_analysis_provenance_from_checkpoint(checkpoint)
        prepared.append((document_id, analysis, provenance_json, evidence))
    for document_id, analysis, provenance_json, evidence in prepared:
        reusable = ReusableKnowledgeAnalysis(analysis, provenance_json, evidence)
        evidence_map = canonical_analysis_evidence_map_in(connection, document_id, reusable)
        materialize_candidate_registry_in(
            connection,
            document_id=document_id,
            analysis=analysis,
            evidence_id_map=evidence_map,
            evidence=evidence,
            analysis_provenance_json=provenance_json,
            now=now,
        )
        record_missing_source_candidates_in(
            connection,
            document_id=canonical_analysis_document_id_in(connection, document_id),
            claims=analysis.missing_source_claims(evidence_map),
            evidence=evidence,
            analysis_provenance_json=provenance_json,
        )
    return synthesize_qualified_corpus_in(
        connection,
        now=now,
    )


def _checkpoint(value: str) -> dict[str, object]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("Knowledge Reanalysis checkpoint is invalid.")
    return parsed
