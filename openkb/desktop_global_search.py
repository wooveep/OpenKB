"""Bounded, current-KB search for the Desktop command palette."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from openkb.desktop_workspace import desktop_state_database_path

_PER_KIND_LIMIT = 8


def search_desktop_knowledge_base(kb_dir: Path, query: str) -> dict[str, object]:
    """Search user-facing documents, knowledge pages, and conversation content."""
    if not query:
        return {"query": "", "results": []}
    database_path = desktop_state_database_path(kb_dir.expanduser().resolve())
    pattern = f"%{_escape_like(query)}%"
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        results = [
            *_document_results(connection, pattern),
            *_quarantined_import_results(connection, pattern),
            *_knowledge_page_results(connection, pattern),
            *_conversation_results(connection, pattern),
        ]
    finally:
        connection.close()
    return {"query": query, "results": results}


def _document_results(connection: sqlite3.Connection, pattern: str) -> list[dict[str, object]]:
    rows = connection.execute(
        """
        SELECT source_documents.document_id, source_documents.display_name,
            source_documents.availability, source_documents.source_format,
            COALESCE((
                SELECT document_ir_blocks.text FROM document_ir_blocks
                WHERE document_ir_blocks.document_id = source_documents.document_id
                    AND document_ir_blocks.text LIKE ? ESCAPE '\\'
                ORDER BY document_ir_blocks.ordinal LIMIT 1
            ), '') AS snippet
        FROM source_documents
        WHERE source_documents.display_name LIKE ? ESCAPE '\\'
            OR EXISTS (
                SELECT 1 FROM document_ir_blocks
                WHERE document_ir_blocks.document_id = source_documents.document_id
                    AND document_ir_blocks.text LIKE ? ESCAPE '\\'
            )
        ORDER BY source_documents.available_at DESC, source_documents.created_at DESC
        LIMIT ?
        """,
        (pattern, pattern, pattern, _PER_KIND_LIMIT),
    ).fetchall()
    return [
        {
            "result_id": f"document:{row['document_id']}",
            "kind": "document",
            "title": str(row["display_name"]),
            "snippet": _snippet(str(row["snippet"]), str(row["source_format"])),
            "status": str(row["availability"]),
            "document_id": str(row["document_id"]),
        }
        for row in rows
    ]


def _quarantined_import_results(
    connection: sqlite3.Connection, pattern: str
) -> list[dict[str, object]]:
    rows = connection.execute(
        """
        SELECT import_jobs.job_id, import_jobs.source_path, quarantined_documents.reason
        FROM quarantined_documents
        JOIN import_jobs ON import_jobs.job_id = quarantined_documents.job_id
        WHERE import_jobs.document_id IS NULL
            AND (import_jobs.source_path LIKE ? ESCAPE '\\'
                OR quarantined_documents.reason LIKE ? ESCAPE '\\')
        ORDER BY quarantined_documents.created_at DESC LIMIT ?
        """,
        (pattern, pattern, _PER_KIND_LIMIT),
    ).fetchall()
    return [
        {
            "result_id": f"quarantine:{row['job_id']}",
            "kind": "document",
            "title": Path(str(row["source_path"])).name,
            "snippet": _snippet(str(row["reason"]), "quarantined"),
            "status": "failed",
            "document_id": None,
        }
        for row in rows
    ]


def _knowledge_page_results(
    connection: sqlite3.Connection, pattern: str
) -> list[dict[str, object]]:
    rows = connection.execute(
        """
        SELECT knowledge_pages.page_id, knowledge_pages.kind, knowledge_pages.title,
            knowledge_page_revisions.content_markdown
        FROM knowledge_pages
        JOIN knowledge_page_revisions
            ON knowledge_page_revisions.revision_id = knowledge_pages.current_revision_id
        WHERE knowledge_pages.title LIKE ? ESCAPE '\\'
            OR knowledge_page_revisions.content_markdown LIKE ? ESCAPE '\\'
        ORDER BY knowledge_pages.updated_at DESC LIMIT ?
        """,
        (pattern, pattern, _PER_KIND_LIMIT),
    ).fetchall()
    return [
        {
            "result_id": f"knowledge_page:{row['page_id']}",
            "kind": "knowledge_page",
            "title": str(row["title"]),
            "snippet": _snippet(str(row["content_markdown"]), str(row["kind"])),
            "status": "available",
            "page_id": str(row["page_id"]),
        }
        for row in rows
    ]


def _conversation_results(connection: sqlite3.Connection, pattern: str) -> list[dict[str, object]]:
    rows = connection.execute(
        """
        SELECT conversations.conversation_id, conversations.title,
            COALESCE((
                SELECT conversation_messages.message_id FROM conversation_messages
                WHERE conversation_messages.conversation_id = conversations.conversation_id
                    AND conversation_messages.role = 'user'
                    AND conversation_messages.content LIKE ? ESCAPE '\\'
                ORDER BY conversation_messages.ordinal DESC LIMIT 1
            ), '') AS message_id,
            COALESCE((
                SELECT conversation_messages.content FROM conversation_messages
                WHERE conversation_messages.conversation_id = conversations.conversation_id
                    AND conversation_messages.role = 'user'
                    AND conversation_messages.content LIKE ? ESCAPE '\\'
                ORDER BY conversation_messages.ordinal DESC LIMIT 1
            ), '') AS snippet
        FROM conversations
        WHERE conversations.title LIKE ? ESCAPE '\\'
            OR EXISTS (
                SELECT 1 FROM conversation_messages
                WHERE conversation_messages.conversation_id = conversations.conversation_id
                    AND conversation_messages.role = 'user'
                    AND conversation_messages.content LIKE ? ESCAPE '\\'
            )
        ORDER BY conversations.updated_at DESC LIMIT ?
        """,
        (pattern, pattern, pattern, pattern, _PER_KIND_LIMIT),
    ).fetchall()
    return [
        {
            "result_id": f"conversation:{row['conversation_id']}",
            "kind": "conversation",
            "title": str(row["title"]),
            "snippet": _snippet(str(row["snippet"]), "conversation"),
            "status": "available",
            "conversation_id": str(row["conversation_id"]),
            "message_id": str(row["message_id"]) or None,
        }
        for row in rows
    ]


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _snippet(value: str, fallback: str) -> str:
    compact = " ".join(value.split())
    if not compact:
        return fallback
    return compact if len(compact) <= 180 else f"{compact[:177]}…"
