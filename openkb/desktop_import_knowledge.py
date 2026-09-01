"""Transactional application of validated import-time knowledge analysis."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from openkb.config import preferred_knowledge_language
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
from openkb.desktop_okf_projection import (
    discard_okf_projection_staging,
    stage_okf_projection_in,
)
from openkb.desktop_workspace import desktop_state_database_path, desktop_state_dir
from openkb.locks import kb_ingest_lock


def apply_import_knowledge_analysis(
    kb_dir: Path,
    *,
    document_id: str,
    analysis: DesktopKnowledgeAnalysis,
    analysis_provenance_json: str,
    evidence: tuple[tuple[str, DocumentIRBlock], ...],
    reconciliation: DesktopKnowledgeReconciliationService,
) -> Path:
    """Publish derived Knowledge after document availability in its own transaction."""
    staged_projection: Path | None = None
    with kb_ingest_lock(desktop_state_dir(kb_dir)):
        connection = sqlite3.connect(desktop_state_database_path(kb_dir))
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            connection.execute("BEGIN IMMEDIATE")
            available = connection.execute(
                "SELECT 1 FROM source_documents "
                "WHERE document_id = ? AND availability = 'available'",
                (document_id,),
            ).fetchone()
            if available is None:
                raise ValueError("Knowledge analysis requires an Available document.")
            apply_import_knowledge_analysis_in(
                connection,
                document_id=document_id,
                analysis=analysis,
                analysis_provenance_json=analysis_provenance_json,
                evidence=evidence,
                reconciliation=reconciliation,
                preferred_language=preferred_knowledge_language(kb_dir),
            )
            staged_projection = stage_okf_projection_in(connection, kb_dir)
            connection.commit()
        except BaseException:
            connection.rollback()
            if staged_projection is not None:
                discard_okf_projection_staging(staged_projection)
            raise
        finally:
            connection.close()
    assert staged_projection is not None
    return staged_projection


def apply_import_knowledge_analysis_in(
    connection: sqlite3.Connection,
    *,
    document_id: str,
    analysis: DesktopKnowledgeAnalysis,
    analysis_provenance_json: str,
    evidence: tuple[tuple[str, DocumentIRBlock], ...],
    reconciliation: DesktopKnowledgeReconciliationService,
    preferred_language: str | None = None,
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
        synthesize_qualified_corpus_in(
            connection,
            now=timestamp(),
            preferred_language=preferred_language,
        )
    else:
        reconciliation.record_analysis_changes_in(connection, document_id, changes)
    record_missing_source_candidates_in(
        connection,
        document_id=canonical_analysis_document_id_in(connection, document_id),
        claims=analysis.missing_source_claims(evidence_id_map),
        evidence=evidence,
        analysis_provenance_json=analysis_provenance_json,
    )
