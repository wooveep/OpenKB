"""Durable lifecycle controls for optional PageTree enrichment tasks."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from openkb.locks import kb_ingest_lock

INTERRUPTED_CODE = "page_tree_enrichment_interrupted"
INTERRUPTED_REASON = "PageTree enrichment was interrupted and can resume."


def recover_interrupted_in(state_dir: Path, database_path: Path) -> int:
    """Return process-owned running work to its durable pending state."""
    with kb_ingest_lock(state_dir):
        connection = _connect(database_path)
        try:
            with connection:
                cursor = connection.execute(
                    """
                    UPDATE document_page_tree_enrichment_tasks
                    SET status = 'pending', execution_token = NULL,
                        error_code = ?, error_reason = ?, updated_at = ?, completed_at = NULL
                    WHERE status = 'running'
                    """,
                    (INTERRUPTED_CODE, INTERRUPTED_REASON, _timestamp()),
                )
                return cursor.rowcount
        finally:
            connection.close()


def request_cancel_in(state_dir: Path, database_path: Path, document_id: str) -> bool:
    """Mark pending work interrupted; a running worker observes Engine cancellation."""
    with kb_ingest_lock(state_dir):
        connection = _connect(database_path)
        try:
            with connection:
                row = connection.execute(
                    "SELECT status FROM document_page_tree_enrichment_tasks "
                    "WHERE document_id = ?",
                    (document_id,),
                ).fetchone()
                if row is None or str(row[0]) not in {"pending", "running"}:
                    return False
                if str(row[0]) == "pending":
                    connection.execute(
                        """
                        UPDATE document_page_tree_enrichment_tasks
                        SET error_code = ?, error_reason = ?, updated_at = ?
                        WHERE document_id = ? AND status = 'pending'
                        """,
                        (INTERRUPTED_CODE, INTERRUPTED_REASON, _timestamp(), document_id),
                    )
                return True
        finally:
            connection.close()


def retry_document_in(
    state_dir: Path,
    database_path: Path,
    document_id: str,
    *,
    provider: str,
    model: str,
    prompt_digest: str,
) -> bool:
    """Make one interrupted or failed task runnable after explicit user action."""
    with kb_ingest_lock(state_dir):
        connection = _connect(database_path)
        try:
            with connection:
                cursor = connection.execute(
                    """
                    UPDATE document_page_tree_enrichment_tasks
                    SET status = 'pending', execution_token = NULL,
                        error_code = NULL, error_reason = NULL,
                        updated_at = ?, completed_at = NULL
                    WHERE document_id = ? AND status IN ('pending', 'failed')
                        AND provider = ? AND model = ? AND prompt_digest = ?
                        AND EXISTS (
                            SELECT 1 FROM source_documents AS documents
                            WHERE documents.document_id = ?
                                AND documents.availability = 'available'
                        )
                    """,
                    (
                        _timestamp(),
                        document_id,
                        provider,
                        model,
                        prompt_digest,
                        document_id,
                    ),
                )
                return cursor.rowcount == 1
        finally:
            connection.close()


def _connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()
