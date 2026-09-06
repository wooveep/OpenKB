"""Durable lifecycle controls for optional PageTree enrichment tasks."""

from __future__ import annotations

import sqlite3

from openkb.shared.clock import timestamp as _timestamp

INTERRUPTED_CODE = "page_tree_enrichment_interrupted"
INTERRUPTED_REASON = "PageTree enrichment was interrupted and can resume."


def recover_interrupted_in(
    connection: sqlite3.Connection,
) -> tuple[int, tuple[str, ...]]:
    """Return process-owned running work to its durable pending state."""
    retry_scopes = tuple(
        str(row[0])
        for row in connection.execute(
            """
            SELECT DISTINCT retry_scope FROM document_page_tree_enrichment_tasks
            WHERE status IN ('pending', 'running') AND retry_scope IS NOT NULL
            """
        ).fetchall()
    )
    cursor = connection.execute(
        """
        UPDATE document_page_tree_enrichment_tasks
        SET status = 'pending', reason = 'explicit_retry', execution_token = NULL,
            retry_scope = NULL, error_code = ?, error_reason = ?,
            updated_at = ?, completed_at = NULL
        WHERE status = 'running' OR retry_scope IS NOT NULL
            OR (
                status = 'pending' AND reason = 'explicit_retry'
                AND error_code IS NULL
            )
        """,
        (INTERRUPTED_CODE, INTERRUPTED_REASON, _timestamp()),
    )
    return cursor.rowcount, retry_scopes


def request_cancel_in(connection: sqlite3.Connection, document_id: str) -> tuple[bool, str | None]:
    """Mark pending work interrupted; a running worker observes Engine cancellation."""
    row = connection.execute(
        "SELECT status, retry_scope FROM document_page_tree_enrichment_tasks WHERE document_id = ?",
        (document_id,),
    ).fetchone()
    if row is None or str(row[0]) not in {"pending", "running"}:
        return False, None
    retry_scope = str(row[1]) if row[1] is not None else None
    connection.execute(
        """
        UPDATE document_page_tree_enrichment_tasks
        SET status = 'pending', reason = 'explicit_retry', execution_token = NULL,
            retry_scope = NULL, error_code = ?, error_reason = ?,
            updated_at = ?, completed_at = NULL
        WHERE document_id = ? AND status IN ('pending', 'running')
        """,
        (INTERRUPTED_CODE, INTERRUPTED_REASON, _timestamp(), document_id),
    )
    return True, retry_scope


def retry_document_in(
    connection: sqlite3.Connection,
    document_id: str,
    *,
    provider: str,
    model: str,
    prompt_digest: str,
    retry_scope: str,
) -> tuple[bool, str | None]:
    """Make one interrupted or failed task runnable after explicit user action."""
    previous = connection.execute(
        "SELECT retry_scope FROM document_page_tree_enrichment_tasks WHERE document_id = ?",
        (document_id,),
    ).fetchone()
    previous_scope = str(previous[0]) if previous is not None and previous[0] is not None else None
    cursor = connection.execute(
        """
        UPDATE document_page_tree_enrichment_tasks
        SET status = 'pending', reason = 'explicit_retry', execution_token = NULL,
            retry_scope = ?, error_code = NULL, error_reason = NULL,
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
            retry_scope,
            _timestamp(),
            document_id,
            provider,
            model,
            prompt_digest,
            document_id,
        ),
    )
    return cursor.rowcount == 1, previous_scope


def page_tree_enrichment_queue_reason(
    current: tuple[str | None, str | None, str | None, str | None],
    target: tuple[str, str, str, str],
) -> str:
    """Describe why one immutable enrichment target supersedes another."""
    if current[0] is None:
        return "initial"
    if current[0] != target[0]:
        return "base_generation_update"
    if current[1:3] != target[1:3]:
        return "model_update"
    return "prompt_update"
