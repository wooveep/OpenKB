"""Durable claim state for background deterministic PageTree rebuilds."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from openkb.desktop_page_tree import (
    DETERMINISTIC_PROVIDER_KIND,
    DETERMINISTIC_PROVIDER_VERSION,
)
from openkb.locks import kb_ingest_lock


@dataclass(frozen=True)
class PageTreeRebuildClaim:
    document_id: str
    provider_kind: str
    provider_version: str
    attempt_count: int


def queue_page_tree_rebuild_in(
    connection: sqlite3.Connection,
    document_id: str,
    *,
    reason: str,
    error_code: str,
    provider_kind: str | None = None,
    provider_version: str | None = None,
) -> None:
    """Queue a target provider without invalidating its identical live claim."""
    now = _timestamp()
    requested_kind = provider_kind or DETERMINISTIC_PROVIDER_KIND
    requested_version = provider_version or DETERMINISTIC_PROVIDER_VERSION
    connection.execute(
        """
        INSERT INTO document_page_tree_rebuild_tasks (
            document_id, status, reason, error_code, attempt_count,
            created_at, updated_at, completed_at,
            requested_provider_kind, requested_provider_version
        ) VALUES (?, 'pending', ?, ?, 0, ?, ?, NULL, ?, ?)
        ON CONFLICT(document_id) DO UPDATE SET
            status = CASE
                WHEN document_page_tree_rebuild_tasks.status = 'running'
                    AND document_page_tree_rebuild_tasks.requested_provider_kind
                        = excluded.requested_provider_kind
                    AND document_page_tree_rebuild_tasks.requested_provider_version
                        = excluded.requested_provider_version
                THEN 'running' ELSE 'pending' END,
            reason = excluded.reason,
            error_code = CASE
                WHEN document_page_tree_rebuild_tasks.status = 'running'
                    AND document_page_tree_rebuild_tasks.requested_provider_kind
                        = excluded.requested_provider_kind
                    AND document_page_tree_rebuild_tasks.requested_provider_version
                        = excluded.requested_provider_version
                THEN document_page_tree_rebuild_tasks.error_code ELSE excluded.error_code END,
            updated_at = excluded.updated_at, completed_at = NULL,
            attempt_count = CASE
                WHEN document_page_tree_rebuild_tasks.status = 'completed'
                    OR document_page_tree_rebuild_tasks.requested_provider_kind
                        != excluded.requested_provider_kind
                    OR document_page_tree_rebuild_tasks.requested_provider_version
                        != excluded.requested_provider_version
                THEN 0 ELSE document_page_tree_rebuild_tasks.attempt_count END,
            requested_provider_kind = excluded.requested_provider_kind,
            requested_provider_version = excluded.requested_provider_version
        """,
        (document_id, reason, error_code, now, now, requested_kind, requested_version),
    )


def ready_page_tree_rebuild_document_ids_in(
    connection: sqlite3.Connection,
) -> tuple[str, ...]:
    """Order canonical authorities first and defer D1 aliases until their target is ready."""
    rows = connection.execute(
        """
        SELECT tasks.document_id, fingerprints.canonical_document_id,
            canonical.provider_kind, canonical.provider_version,
            tasks.requested_provider_kind, tasks.requested_provider_version
        FROM document_page_tree_rebuild_tasks AS tasks
        LEFT JOIN document_content_fingerprints AS fingerprints
            ON fingerprints.document_id = tasks.document_id
        LEFT JOIN document_page_tree_current AS canonical_current
            ON canonical_current.document_id = fingerprints.canonical_document_id
        LEFT JOIN document_page_tree_generations AS canonical
            ON canonical.generation_id = canonical_current.generation_id
        WHERE tasks.status IN ('pending', 'running', 'failed')
        ORDER BY CASE
            WHEN fingerprints.canonical_document_id IS NULL
                OR fingerprints.canonical_document_id = tasks.document_id
            THEN 0 ELSE 1 END,
            tasks.updated_at, tasks.document_id
        """
    ).fetchall()
    return tuple(
        str(row[0])
        for row in rows
        if row[1] is None
        or str(row[1]) == str(row[0])
        or (str(row[2]), str(row[3])) == (str(row[4]), str(row[5]))
    )


def claim_page_tree_rebuild(
    state_dir: Path, database_path: Path, document_id: str
) -> PageTreeRebuildClaim | None:
    """Persist one attempt before provider work starts outside the KB lock."""
    with kb_ingest_lock(state_dir):
        connection = _connect(database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT requested_provider_kind, requested_provider_version, attempt_count
                FROM document_page_tree_rebuild_tasks
                WHERE document_id = ? AND status IN ('pending', 'running', 'failed')
                """,
                (document_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            cursor = connection.execute(
                """
                UPDATE document_page_tree_rebuild_tasks
                SET status = 'running', attempt_count = attempt_count + 1,
                    error_code = NULL, updated_at = ?, completed_at = NULL
                WHERE document_id = ? AND status IN ('pending', 'running', 'failed')
                    AND requested_provider_kind = ? AND requested_provider_version = ?
                    AND attempt_count = ?
                """,
                (_timestamp(), document_id, str(row[0]), str(row[1]), int(row[2])),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return None
            claim = PageTreeRebuildClaim(
                document_id,
                str(row[0]),
                str(row[1]),
                int(row[2]) + 1,
            )
            connection.commit()
            return claim
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


def rebuild_claim_is_current_in(
    connection: sqlite3.Connection, claim: PageTreeRebuildClaim
) -> bool:
    row = connection.execute(
        """
        SELECT 1 FROM document_page_tree_rebuild_tasks
        WHERE document_id = ? AND status = 'running' AND attempt_count = ?
            AND requested_provider_kind = ? AND requested_provider_version = ?
        """,
        (
            claim.document_id,
            claim.attempt_count,
            claim.provider_kind,
            claim.provider_version,
        ),
    ).fetchone()
    return row is not None


def mark_page_tree_rebuild_failed(
    state_dir: Path,
    database_path: Path,
    claim: PageTreeRebuildClaim,
    error_code: str,
) -> None:
    """Fail only the attempt that still owns the durable rebuild claim."""
    with kb_ingest_lock(state_dir):
        connection = _connect(database_path)
        try:
            with connection:
                connection.execute(
                    """
                    UPDATE document_page_tree_rebuild_tasks
                    SET status = 'failed', error_code = ?, updated_at = ?
                    WHERE document_id = ? AND status = 'running' AND attempt_count = ?
                        AND requested_provider_kind = ? AND requested_provider_version = ?
                    """,
                    (
                        error_code,
                        _timestamp(),
                        claim.document_id,
                        claim.attempt_count,
                        claim.provider_kind,
                        claim.provider_version,
                    ),
                )
        finally:
            connection.close()


def _connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _timestamp() -> str:
    return datetime.now(tz=timezone.utc).isoformat()
