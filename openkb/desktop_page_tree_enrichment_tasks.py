"""Task Center projection for optional PageTree enrichment work."""

from __future__ import annotations

import sqlite3

from openkb.desktop_model_usage import model_activity_for_call_in


def page_tree_enrichment_tasks_in(connection: sqlite3.Connection) -> list[dict[str, object]]:
    rows = connection.execute(
        """
        SELECT tasks.document_id, documents.display_name, tasks.status, tasks.reason,
            tasks.provider, tasks.model, tasks.attempt_count, tasks.model_attempt,
            tasks.call_id, tasks.error_code, tasks.error_reason,
            tasks.updated_at, tasks.completed_at,
            tasks.base_generation_id, current.enrichment_generation_id
        FROM document_page_tree_enrichment_tasks AS tasks
        JOIN source_documents AS documents ON documents.document_id = tasks.document_id
        LEFT JOIN document_page_tree_enrichment_current AS current
            ON current.document_id = tasks.document_id
        ORDER BY tasks.updated_at DESC, tasks.document_id LIMIT 50
        """
    ).fetchall()
    tasks: list[dict[str, object]] = []
    for row in rows:
        call_id = str(row[8]) if row[8] is not None else None
        tasks.append(
            {
                "document_id": str(row[0]),
                "document_name": str(row[1]),
                "status": str(row[2]),
                "reason": str(row[3]),
                "provider": str(row[4]),
                "model": str(row[5]),
                "attempt_count": int(row[6]),
                "model_attempt": int(row[7]),
                "call_id": call_id,
                "error_code": str(row[9]) if row[9] is not None else None,
                "error_reason": str(row[10]) if row[10] is not None else None,
                "updated_at": str(row[11]),
                "completed_at": str(row[12]) if row[12] is not None else None,
                "base_generation_id": str(row[13]),
                "current_enrichment_generation_id": (str(row[14]) if row[14] is not None else None),
                "model_activity": (
                    model_activity_for_call_in(connection, call_id) if call_id is not None else None
                ),
            }
        )
    return tasks
