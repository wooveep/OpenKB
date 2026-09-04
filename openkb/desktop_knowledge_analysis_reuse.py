"""Replay persisted Knowledge Analysis against canonical Evidence occurrences."""

from __future__ import annotations

import hashlib
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
from openkb.desktop_knowledge_analysis_requests import current_analysis_pipeline_digest
from openkb.desktop_knowledge_reconciliation_changes import IncomingKnowledgeChange
from openkb.desktop_prompt_contracts import prompt_contract_for


@dataclass(frozen=True)
class ReusableKnowledgeAnalysis:
    analysis: DesktopKnowledgeAnalysis
    provenance_json: str
    evidence: tuple[tuple[str, DocumentIRBlock], ...]
    analysis_document_id: str | None = None
    analyzed_at: str | None = None


@dataclass(frozen=True)
class StoredKnowledgeAnalysisCheckpoint:
    """Latest persisted analysis payload before schema-specific parsing."""

    source: str
    job_id: str
    document_id: str
    checkpoint: dict[str, object]
    analyzed_at: str


def persisted_analysis_prompt_digest_in(
    connection: sqlite3.Connection,
    stored: StoredKnowledgeAnalysisCheckpoint,
) -> str | None:
    """Recover the prompt identity that actually produced a stored analysis."""
    checkpoint = stored.checkpoint
    pipeline_digest = _optional_string(checkpoint.get("analysis_prompt_digest"))
    if pipeline_digest is not None:
        return pipeline_digest
    merge_digest = _optional_string(checkpoint.get("prompt_digest"))
    batch_count = checkpoint.get("batch_count")
    if type(batch_count) is not int or batch_count < 2 or merge_digest is None:
        if _uses_current_pipeline_contract(checkpoint, merge_digest):
            return current_analysis_pipeline_digest()
        return merge_digest
    table = (
        "knowledge_analysis_batches"
        if stored.source == "import"
        else "knowledge_reanalysis_batches"
    )
    rows = connection.execute(
        f"SELECT checkpoint_json FROM {table} WHERE job_id = ? ORDER BY batch_ordinal",
        (stored.job_id,),
    ).fetchall()
    if len(rows) != batch_count:
        return None
    batch_digests: set[str] = set()
    for row in rows:
        try:
            batch_checkpoint = _json_value(row[0])
        except ValueError:
            return None
        if not isinstance(batch_checkpoint, dict):
            return None
        digest = _optional_string(batch_checkpoint.get("prompt_digest"))
        if digest is None:
            return None
        batch_digests.add(digest)
    if len(batch_digests) != 1:
        return None
    batch_digest = next(iter(batch_digests))
    if batch_digest == prompt_contract_for("knowledge_fact_harvest").digest and (
        merge_digest == prompt_contract_for("knowledge_analysis_merge").digest
    ):
        return current_analysis_pipeline_digest()
    return hashlib.sha256(f"{batch_digest}:{merge_digest}".encode("utf-8")).hexdigest()


def _uses_current_pipeline_contract(
    checkpoint: dict[str, object], prompt_digest: str | None
) -> bool:
    snapshot = checkpoint.get("prompt_contract_snapshot")
    operation = snapshot.get("operation") if isinstance(snapshot, dict) else None
    return (
        isinstance(operation, str)
        and operation
        in {
            "knowledge_fact_harvest",
            "document_entity_inventory",
        }
        and prompt_digest == prompt_contract_for(operation).digest
    )


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


def canonical_analysis_document_id_in(connection: sqlite3.Connection, document_id: str) -> str:
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
    """Load the latest import or explicit reanalysis for canonical content."""
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        stored = latest_knowledge_analysis_checkpoint_in(connection, document_id)
        if stored is None:
            return None
        if stored.source == "import":
            rows = connection.execute(
                """
                SELECT stages.stage, runtime.checkpoint_json
                FROM stage_runs AS stages
                JOIN stage_run_runtime AS runtime
                    ON runtime.stage_run_id = stages.stage_run_id
                WHERE stages.job_id = ?
                    AND stages.stage IN ('document_ir', 'evidence')
                """,
                (stored.job_id,),
            ).fetchall()
            checkpoints = {str(stage): _json_value(payload) for stage, payload in rows}
            if set(checkpoints) != {"document_ir", "evidence"}:
                return None
            blocks = document_ir_from_checkpoint(checkpoints["document_ir"])
            evidence = evidence_from_checkpoint(checkpoints["evidence"], blocks)
        else:
            evidence = analysis_evidence_for_document_in(connection, document_id)
    finally:
        connection.close()
    analysis = knowledge_analysis_from_checkpoint(stored.checkpoint)
    if analysis is None:
        return None
    return ReusableKnowledgeAnalysis(
        analysis=analysis,
        provenance_json=knowledge_analysis_provenance_from_checkpoint(stored.checkpoint),
        evidence=evidence,
        analysis_document_id=stored.document_id,
        analyzed_at=stored.analyzed_at,
    )


def latest_knowledge_analysis_checkpoint_in(
    connection: sqlite3.Connection, document_id: str
) -> StoredKnowledgeAnalysisCheckpoint | None:
    """Load raw latest checkpoint metadata without requiring the current schema."""
    canonical_document_id = canonical_analysis_document_id_in(connection, document_id)
    import_row = connection.execute(
        """
        SELECT jobs.job_id, jobs.document_id, runtime.checkpoint_json,
            COALESCE(jobs.completed_at, jobs.created_at)
        FROM import_jobs AS jobs
        JOIN stage_runs AS stages ON stages.job_id = jobs.job_id
            AND stages.stage = 'model_analysis' AND stages.status = 'completed'
        JOIN stage_run_runtime AS runtime ON runtime.stage_run_id = stages.stage_run_id
        WHERE jobs.document_id = ? AND runtime.checkpoint_json IS NOT NULL
        ORDER BY jobs.completed_at DESC, jobs.created_at DESC LIMIT 1
        """,
        (canonical_document_id,),
    ).fetchone()
    reanalysis_row = connection.execute(
        """
        SELECT jobs.job_id, jobs.document_id, jobs.checkpoint_json,
            COALESCE(jobs.completed_at, jobs.created_at)
        FROM knowledge_reanalysis_jobs AS jobs
        LEFT JOIN document_content_fingerprints AS fingerprints
            ON fingerprints.document_id = jobs.document_id
        WHERE COALESCE(fingerprints.canonical_document_id, jobs.document_id) = ?
            AND jobs.status = 'completed' AND jobs.checkpoint_json IS NOT NULL
        ORDER BY jobs.completed_at DESC, jobs.created_at DESC LIMIT 1
        """,
        (canonical_document_id,),
    ).fetchone()
    candidates = [
        ("import", import_row) if import_row is not None else None,
        ("reanalysis", reanalysis_row) if reanalysis_row is not None else None,
    ]
    available = [candidate for candidate in candidates if candidate is not None]
    if not available:
        return None
    source, selected = max(available, key=lambda candidate: str(candidate[1][3]))
    return StoredKnowledgeAnalysisCheckpoint(
        source=source,
        job_id=str(selected[0]),
        document_id=str(selected[1]),
        checkpoint=_checkpoint_json(selected[2]),
        analyzed_at=str(selected[3]),
    )


def analysis_evidence_for_document_in(
    connection: sqlite3.Connection, document_id: str
) -> tuple[tuple[str, DocumentIRBlock], ...]:
    """Read persisted Evidence through one Available document occurrence."""
    rows = connection.execute(
        """
        SELECT occurrences.evidence_id, blocks.block_id, blocks.ordinal, blocks.kind,
            blocks.text, blocks.heading_path, blocks.locator_json
        FROM evidence_occurrences AS occurrences
        JOIN source_documents AS documents ON documents.document_id = occurrences.document_id
            AND documents.availability = 'available'
        JOIN document_ir_blocks AS blocks ON blocks.block_id = occurrences.block_id
        WHERE occurrences.document_id = ?
        ORDER BY blocks.ordinal
        """,
        (document_id,),
    ).fetchall()
    evidence: list[tuple[str, DocumentIRBlock]] = []
    for row in rows:
        try:
            heading_path = json.loads(str(row[5]))
            locator = json.loads(str(row[6]))
        except json.JSONDecodeError as error:
            raise ValueError("Knowledge Analysis DocumentIR is invalid.") from error
        if (
            not isinstance(heading_path, list)
            or not all(isinstance(value, str) for value in heading_path)
            or not isinstance(locator, dict)
        ):
            raise ValueError("Knowledge Analysis DocumentIR is invalid.")
        evidence.append(
            (
                str(row[0]),
                DocumentIRBlock(
                    block_id=str(row[1]),
                    ordinal=int(row[2]),
                    kind=str(row[3]),
                    text=str(row[4]),
                    heading_path=tuple(heading_path),
                    line_start=1,
                    line_end=1,
                    locator=locator,
                ),
            )
        )
    if not evidence:
        raise ValueError("Knowledge Analysis requires Available Evidence.")
    return tuple(evidence)


def _checkpoint_json(payload: object) -> dict[str, object]:
    checkpoint = _json_value(payload)
    if not isinstance(checkpoint, dict):
        raise ValueError("Knowledge Analysis checkpoint is invalid.")
    return checkpoint


def _json_value(payload: object) -> object:
    try:
        return json.loads(str(payload))
    except json.JSONDecodeError as error:
        raise ValueError("Knowledge Analysis checkpoint is invalid.") from error


def _optional_string(value: object) -> str | None:
    return str(value) if isinstance(value, str) and value else None
