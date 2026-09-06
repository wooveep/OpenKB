"""Read-only progress projection for durable Knowledge Analysis batches."""

from __future__ import annotations

import sqlite3

from openkb.importing.types import (
    DesktopKnowledgeAnalysisProgress,
    DesktopModelCall,
)


def knowledge_analysis_progress_in(
    connection: sqlite3.Connection,
    job_id: str,
    model_calls: tuple[DesktopModelCall, ...],
) -> DesktopKnowledgeAnalysisProgress | None:
    """Project batch/merge checkpoints without exposing persistence details."""
    del model_calls
    rows = connection.execute(
        """
        SELECT batch_ordinal, status
        FROM knowledge_analysis_batches
        WHERE job_id = ?
        ORDER BY batch_ordinal
        """,
        (job_id,),
    ).fetchall()
    if not rows:
        return None
    statuses = [str(row[1]) for row in rows]
    merge_row = connection.execute(
        "SELECT status FROM knowledge_analysis_merges WHERE job_id = ?", (job_id,)
    ).fetchone()
    merge_status = str(merge_row[0]) if merge_row is not None else "pending"
    current = next(
        (int(row[0]) + 1 for row in rows if str(row[1]) in {"running", "failed", "pending"}),
        None,
    )
    phase = (
        "completed"
        if merge_status == "completed"
        else ("merge" if all(status == "completed" for status in statuses) else "batches")
    )
    return DesktopKnowledgeAnalysisProgress(
        total=len(rows),
        completed=statuses.count("completed"),
        active=statuses.count("running"),
        failed=statuses.count("failed"),
        current_batch=current,
        phase=phase,
    )
