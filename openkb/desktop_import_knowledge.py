"""Transactional application of validated import-time knowledge analysis."""

from __future__ import annotations

import sqlite3

from openkb.desktop_corpus_knowledge import (
    replace_document_corpus_analysis_in,
    synthesize_qualified_corpus_in,
)
from openkb.desktop_import_artifacts import DocumentIRBlock
from openkb.desktop_import_clock import timestamp
from openkb.desktop_knowledge_analysis import DesktopKnowledgeAnalysis
from openkb.desktop_knowledge_analysis_reuse import (
    ReusableKnowledgeAnalysis,
    canonical_analysis_document_id_in,
    canonical_analysis_evidence_map_in,
)
from openkb.desktop_knowledge_reconciliation import DesktopKnowledgeReconciliationService
from openkb.desktop_missing_sources import record_missing_source_candidates_in


def apply_import_knowledge_analysis_in(
    connection: sqlite3.Connection,
    *,
    document_id: str,
    analysis: DesktopKnowledgeAnalysis,
    analysis_provenance_json: str,
    evidence: tuple[tuple[str, DocumentIRBlock], ...],
    reconciliation: DesktopKnowledgeReconciliationService,
) -> None:
    """Bind canonical Evidence and publish either corpus or compatibility knowledge."""
    reusable = ReusableKnowledgeAnalysis(analysis, analysis_provenance_json, evidence)
    evidence_id_map = canonical_analysis_evidence_map_in(connection, document_id, reusable)
    changes = analysis.incoming_changes(
        evidence_id_map,
        analysis_provenance_json=analysis_provenance_json,
    )
    if analysis.corpus_ready:
        replace_document_corpus_analysis_in(
            connection,
            document_id=document_id,
            analysis=analysis,
            evidence_id_map=evidence_id_map,
            evidence=evidence,
            analysis_provenance_json=analysis_provenance_json,
            now=timestamp(),
        )
        synthesize_qualified_corpus_in(connection, now=timestamp())
    else:
        reconciliation.record_analysis_changes_in(connection, document_id, changes)
    record_missing_source_candidates_in(
        connection,
        document_id=canonical_analysis_document_id_in(connection, document_id),
        claims=analysis.missing_source_claims(evidence_id_map),
        evidence=evidence,
        analysis_provenance_json=analysis_provenance_json,
    )
