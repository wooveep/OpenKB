"""Startup and workspace-switch recovery for explicit Knowledge Reanalysis."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from openkb.locks import kb_ingest_lock
from openkb.shared.clock import timestamp as _timestamp
from openkb.storage.sqlite import connect_database
from openkb.workspace.paths import desktop_state_database_path, desktop_state_dir


def recover_interrupted_knowledge_reanalysis(kb_dir: Path) -> None:
    """Mark stale work failed on open without starting any model call."""
    state_dir = desktop_state_dir(kb_dir)
    database_path = desktop_state_database_path(kb_dir)
    with kb_ingest_lock(state_dir):
        connection = _connect(database_path)
        try:
            with connection:
                now = _timestamp()
                connection.execute(
                    """
                    UPDATE knowledge_reanalysis_batches
                    SET status = 'failed', error_code = 'knowledge_reanalysis_interrupted',
                        updated_at = ? WHERE status = 'running'
                    """,
                    (now,),
                )
                connection.execute(
                    """
                    UPDATE knowledge_reanalysis_merges
                    SET status = 'failed', error_code = 'knowledge_reanalysis_interrupted',
                        updated_at = ? WHERE status = 'running'
                    """,
                    (now,),
                )
                connection.execute(
                    """
                    UPDATE knowledge_reanalysis_jobs
                    SET status = 'failed', phase = 'failed',
                        error_code = 'knowledge_reanalysis_interrupted',
                        reason = 'Knowledge Reanalysis stopped with the Desktop Runtime.',
                        completed_at = ?, execution_token = NULL
                    WHERE status IN ('pending', 'running')
                    """,
                    (now,),
                )
                for row in connection.execute(
                    """
                    SELECT run_id FROM knowledge_reanalysis_runs
                    WHERE status IN ('pending', 'running')
                    """
                ).fetchall():
                    _refresh_run_in(connection, str(row[0]), now)
        finally:
            connection.close()


def _refresh_run_in(connection: sqlite3.Connection, run_id: str, now: str) -> None:
    counts = dict(
        connection.execute(
            """
            SELECT status, COUNT(*) FROM knowledge_reanalysis_jobs
            WHERE run_id = ? GROUP BY status
            """,
            (run_id,),
        ).fetchall()
    )
    active = int(counts.get("pending", 0)) + int(counts.get("running", 0))
    failed = int(counts.get("failed", 0))
    completed = int(counts.get("completed", 0))
    if active:
        status, completed_at = "running", None
    elif failed and completed:
        status, completed_at = "partial_failure", now
    elif failed:
        status, completed_at = "failed", now
    else:
        status, completed_at = "completed", now
    connection.execute(
        """
        UPDATE knowledge_reanalysis_runs SET status = ?, completed_at = ? WHERE run_id = ?
        """,
        (status, completed_at, run_id),
    )


def _connect(database_path: Path) -> sqlite3.Connection:
    connection = connect_database(database_path)
    return connection
