"""Invalidate derived import checkpoints when a user explicitly requests reparsing."""

from __future__ import annotations

import sqlite3

from openkb.importing.artifacts import DesktopImportError
from openkb.knowledge.analysis.recovery_store import discard_analysis_in


def reset_parser_checkpoints_in(
    connection: sqlite3.Connection, job_id: str, stage_ids: dict[str, str], now: str
) -> None:
    """Keep verified source bytes and invalidate every result derived from DocumentIR."""
    row = connection.execute(
        "SELECT document_id FROM import_jobs WHERE job_id = ?", (job_id,)
    ).fetchone()
    if row is None or row[0] is not None:
        raise DesktopImportError(
            "import_reparse_requires_new_import",
            "An already published document requires a new import to change its parsing.",
        )
    discard_analysis_in(connection, job_id)
    for stage in ("document_ir", "evidence", "deterministic_page_tree", "model_analysis", "search"):
        stage_id = stage_ids[stage]
        connection.execute(
            "UPDATE stage_runs SET status = 'pending', progress = 0, error_code = NULL, "
            "started_at = NULL, completed_at = NULL WHERE stage_run_id = ? AND job_id = ?",
            (stage_id, job_id),
        )
        connection.execute(
            "UPDATE stage_run_runtime SET status = 'pending', checkpoint_json = NULL, "
            "error_code = NULL, updated_at = ? WHERE stage_run_id = ? AND job_id = ?",
            (now, stage_id, job_id),
        )
