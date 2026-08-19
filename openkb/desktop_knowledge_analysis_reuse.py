"""Replay persisted Knowledge Analysis against canonical Evidence occurrences."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from openkb.desktop_import_artifacts import (
    DocumentIRBlock,
    document_ir_from_checkpoint,
    evidence_from_checkpoint,
)
from openkb.desktop_knowledge_analysis import (
    DesktopKnowledgeAnalysis,
    knowledge_analysis_from_checkpoint,
    knowledge_analysis_provenance_from_checkpoint,
)
from openkb.desktop_knowledge_reconciliation_changes import IncomingKnowledgeChange


@dataclass(frozen=True)
class ReusableKnowledgeAnalysis:
    analysis: DesktopKnowledgeAnalysis
    provenance_json: str
    evidence: tuple[tuple[str, DocumentIRBlock], ...]


def canonical_analysis_changes_in(
    connection: sqlite3.Connection,
    document_id: str,
    reusable: ReusableKnowledgeAnalysis,
) -> tuple[IncomingKnowledgeChange, ...]:
    """Map one checkpoint's transient Evidence IDs by stable Document IR ordinal."""
    evidence_id_map = canonical_analysis_evidence_map_in(connection, document_id, reusable)
    return reusable.analysis.incoming_changes(
        evidence_id_map, analysis_provenance_json=reusable.provenance_json
    )


def canonical_analysis_evidence_map_in(
    connection: sqlite3.Connection,
    document_id: str,
    reusable: ReusableKnowledgeAnalysis,
) -> dict[str, str]:
    """Resolve prompt Evidence IDs to the document version's canonical Evidence IDs."""
    rows = connection.execute(
        """
        SELECT blocks.ordinal, occurrences.evidence_id
        FROM evidence_occurrences AS occurrences
        JOIN document_ir_blocks AS blocks ON blocks.block_id = occurrences.block_id
        WHERE occurrences.document_id = ?
        """,
        (document_id,),
    ).fetchall()
    canonical_by_ordinal = {int(row[0]): str(row[1]) for row in rows}
    return {
        evidence_id: canonical_by_ordinal[block.ordinal]
        for evidence_id, block in reusable.evidence
        if block.ordinal in canonical_by_ordinal
    }


def canonical_analysis_document_id_in(
    connection: sqlite3.Connection, document_id: str
) -> str:
    """Return the content authority shared by D1 document versions."""
    row = connection.execute(
        """
        SELECT COALESCE(canonical_document_id, document_id)
        FROM document_content_fingerprints WHERE document_id = ?
        """,
        (document_id,),
    ).fetchone()
    return str(row[0]) if row is not None else document_id


def load_reusable_knowledge_analysis(
    database_path: Path, document_id: str
) -> ReusableKnowledgeAnalysis | None:
    """Load the latest structured checkpoint for a document's canonical content."""
    connection = sqlite3.connect(database_path)
    try:
        canonical = connection.execute(
            """
            SELECT COALESCE(canonical_document_id, document_id)
            FROM document_content_fingerprints WHERE document_id = ?
            """,
            (document_id,),
        ).fetchone()
        canonical_document_id = str(canonical[0]) if canonical is not None else document_id
        job = connection.execute(
            """
            SELECT jobs.job_id
            FROM import_jobs AS jobs
            JOIN stage_runs AS stages ON stages.job_id = jobs.job_id
                AND stages.stage = 'model_analysis' AND stages.status = 'completed'
            JOIN stage_run_runtime AS runtime ON runtime.stage_run_id = stages.stage_run_id
            WHERE jobs.document_id = ? AND runtime.checkpoint_json IS NOT NULL
            ORDER BY jobs.completed_at DESC, jobs.created_at DESC LIMIT 1
            """,
            (canonical_document_id,),
        ).fetchone()
        if job is None:
            return None
        rows = connection.execute(
            """
            SELECT stages.stage, runtime.checkpoint_json
            FROM stage_runs AS stages
            JOIN stage_run_runtime AS runtime ON runtime.stage_run_id = stages.stage_run_id
            WHERE stages.job_id = ?
                AND stages.stage IN ('document_ir', 'evidence', 'model_analysis')
            """,
            (str(job[0]),),
        ).fetchall()
    finally:
        connection.close()
    checkpoints = {str(stage): json.loads(str(payload)) for stage, payload in rows}
    if set(checkpoints) != {"document_ir", "evidence", "model_analysis"}:
        return None
    analysis = knowledge_analysis_from_checkpoint(checkpoints["model_analysis"])
    if analysis is None:
        return None
    blocks = document_ir_from_checkpoint(checkpoints["document_ir"])
    return ReusableKnowledgeAnalysis(
        analysis=analysis,
        provenance_json=knowledge_analysis_provenance_from_checkpoint(
            checkpoints["model_analysis"]
        ),
        evidence=evidence_from_checkpoint(checkpoints["evidence"], blocks),
    )
