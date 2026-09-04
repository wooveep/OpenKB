"""Resolve and validate one immutable Version Scope before retrieval planning."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

from openkb.desktop_answer_types import DesktopAnswerError
from openkb.desktop_document_version_catalog import current_document_version_catalog_in
from openkb.desktop_retrieval_plan import validate_question
from openkb.desktop_version_scope import (
    NavigationSnapshot,
    RetrievalRequest,
    VersionFilter,
    capture_navigation_snapshot_in,
    resolve_version_scope,
)


def coerce_retrieval_request(value: str | RetrievalRequest) -> RetrievalRequest:
    if isinstance(value, str):
        return RetrievalRequest(question=value)
    if not isinstance(value, RetrievalRequest):
        raise DesktopAnswerError("invalid_question", "Enter a question before asking OpenKB.")
    return value


def capture_version_navigation_snapshot(
    database_path: Path, request: RetrievalRequest
) -> tuple[str, NavigationSnapshot]:
    """Resolve the request once, before any optional model operation can run."""
    normalized_question = validate_question(request.question)
    ui_filter = request.version_filter
    if request.requested_mode is not None:
        ui_filter = (
            VersionFilter(mode=request.requested_mode)
            if ui_filter is None
            else replace(ui_filter, mode=request.requested_mode)
        )
    connection = _connect(database_path)
    try:
        connection.execute("BEGIN")
        catalog = current_document_version_catalog_in(connection)
        if catalog is None:
            raise DesktopAnswerError(
                "desktop_version_catalog_unavailable",
                "The current Document Version Catalog is unavailable.",
            )
        scope = resolve_version_scope(
            normalized_question,
            conversation_scope=request.conversation_scope,
            ui_filter=ui_filter,
            catalog=catalog,
        )
        if scope.status in {"ambiguous", "unavailable"}:
            labels = (
                f" Available versions: {', '.join(scope.available_labels)}."
                if scope.available_labels
                else ""
            )
            raise DesktopAnswerError(
                f"desktop_version_scope_{scope.status}",
                f"The requested document version could not be resolved.{labels}",
            )
        snapshot = capture_navigation_snapshot_in(connection, scope, catalog)
        return normalized_question, snapshot
    finally:
        connection.rollback()
        connection.close()


def require_version_snapshot_current_in(
    connection: sqlite3.Connection, snapshot: NavigationSnapshot
) -> None:
    row = connection.execute(
        "SELECT current_revision_id FROM document_version_catalog_state WHERE singleton = 1"
    ).fetchone()
    if row is None or str(row[0]) != snapshot.version_catalog_revision_id:
        raise DesktopAnswerError(
            "desktop_version_scope_changed",
            "The Document Version Catalog changed during retrieval; retry the question.",
        )


def version_scope_degradations(snapshot: NavigationSnapshot) -> tuple[str, ...]:
    reason = snapshot.version_scope.degradation_reason
    return (reason,) if reason else ()


def _connect(database_path: Path) -> sqlite3.Connection:
    if not database_path.is_file():
        raise DesktopAnswerError(
            "desktop_knowledge_base_not_found",
            "Open a Desktop Knowledge Base before asking a question.",
        )
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection
