"""Public document Candidate Pipeline over validation, admission, and generation publish."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path

from openkb.desktop_candidate_registry import (
    CandidateRegistryOutcome,
    publish_candidate_registry_generation_in,
)
from openkb.desktop_corpus_knowledge import replace_document_corpus_analysis_in
from openkb.desktop_import_artifacts import DocumentIRBlock
from openkb.desktop_import_clock import timestamp
from openkb.desktop_knowledge_analysis import DesktopKnowledgeAnalysis
from openkb.desktop_knowledge_analysis_reuse import (
    ReusableKnowledgeAnalysis,
    canonical_analysis_evidence_map_in,
)
from openkb.desktop_workspace import desktop_state_database_path, desktop_state_dir
from openkb.locks import kb_ingest_lock


class DesktopKnowledgeCandidatePipeline:
    """Materialize exactly one validated analysis as an immutable registry generation."""

    def __init__(self, kb_dir: Path) -> None:
        self._kb_dir = kb_dir.expanduser().resolve()
        self._database_path = desktop_state_database_path(self._kb_dir)
        self._state_dir = desktop_state_dir(self._kb_dir)

    def run_document(
        self,
        *,
        document_id: str,
        analysis: DesktopKnowledgeAnalysis,
        analysis_provenance_json: str,
        evidence: tuple[tuple[str, DocumentIRBlock], ...],
    ) -> CandidateRegistryOutcome:
        with kb_ingest_lock(self._state_dir):
            connection = sqlite3.connect(self._database_path)
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                connection.execute("BEGIN IMMEDIATE")
                reusable = ReusableKnowledgeAnalysis(analysis, analysis_provenance_json, evidence)
                evidence_id_map = canonical_analysis_evidence_map_in(
                    connection, document_id, reusable
                )
                outcome = materialize_candidate_registry_in(
                    connection,
                    document_id=document_id,
                    analysis=analysis,
                    analysis_provenance_json=analysis_provenance_json,
                    evidence=evidence,
                    evidence_id_map=evidence_id_map,
                    now=timestamp(),
                )
                connection.commit()
                return outcome
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()


def materialize_candidate_registry_in(
    connection: sqlite3.Connection,
    *,
    document_id: str,
    analysis: DesktopKnowledgeAnalysis,
    analysis_provenance_json: str,
    evidence: tuple[tuple[str, DocumentIRBlock], ...],
    evidence_id_map: Mapping[str, str],
    now: str,
) -> CandidateRegistryOutcome:
    """Internal transaction-aware entry used by the import/reanalysis owners."""
    replace_document_corpus_analysis_in(
        connection,
        document_id=document_id,
        analysis=analysis,
        evidence_id_map=evidence_id_map,
        evidence=evidence,
        analysis_provenance_json=analysis_provenance_json,
        now=now,
    )
    return publish_candidate_registry_generation_in(
        connection,
        document_id=document_id,
        analysis_provenance_json=analysis_provenance_json,
        now=now,
    )
