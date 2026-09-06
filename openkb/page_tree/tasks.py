"""Task Center projection for deterministic Document PageTree rebuilds."""

from __future__ import annotations

import sqlite3


def page_tree_rebuild_tasks_in(connection: sqlite3.Connection) -> list[dict[str, object]]:
    rows = connection.execute(
        """
        SELECT tasks.document_id, documents.display_name, tasks.status, tasks.reason,
            tasks.error_code, tasks.attempt_count, tasks.requested_provider_kind,
            tasks.requested_provider_version, tasks.updated_at, tasks.completed_at,
            current.generation_id
        FROM document_page_tree_rebuild_tasks AS tasks
        JOIN source_documents AS documents ON documents.document_id = tasks.document_id
        LEFT JOIN document_page_tree_current AS current
            ON current.document_id = tasks.document_id
        ORDER BY tasks.updated_at DESC, tasks.document_id LIMIT 50
        """
    ).fetchall()
    return [
        {
            "document_id": str(row[0]),
            "document_name": str(row[1]),
            "status": str(row[2]),
            "reason": str(row[3]),
            "error_code": str(row[4]) if row[4] is not None else None,
            "attempt_count": int(row[5]),
            "provider_kind": str(row[6]),
            "provider_version": str(row[7]),
            "updated_at": str(row[8]),
            "completed_at": str(row[9]) if row[9] is not None else None,
            "current_generation_id": str(row[10]) if row[10] is not None else None,
        }
        for row in rows
    ]
