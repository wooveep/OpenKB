"""Compatibility filtering for interrupted pre-ledger Desktop migrations."""

from __future__ import annotations

import sqlite3

from openkb.desktop_knowledge_adoption_migrations import (
    KNOWLEDGE_ADOPTION_REQUEST_INPUT_COLUMNS,
)
from openkb.desktop_knowledge_analysis_migrations import (
    KNOWLEDGE_ANALYSIS_ENTITY_SUBTYPE_MIGRATION_STATEMENT,
)
from openkb.desktop_model_observability_migrations import MODEL_LIFECYCLE_COLUMNS
from openkb.desktop_model_result_migrations import MODEL_RESULT_OBSERVATION_COLUMNS
from openkb.desktop_workspace_feature_migrations import (
    KNOWLEDGE_ADOPTION_REQUEST_INPUT_MIGRATION_VERSION,
    KNOWLEDGE_ANALYSIS_MIGRATION_VERSION,
    MODEL_LIFECYCLE_MIGRATION_VERSION,
    MODEL_RESULT_OBSERVATION_MIGRATION_VERSION,
)

_REPAIRABLE_COLUMN_MIGRATIONS = {
    KNOWLEDGE_ADOPTION_REQUEST_INPUT_MIGRATION_VERSION: (
        KNOWLEDGE_ADOPTION_REQUEST_INPUT_COLUMNS
    ),
    MODEL_LIFECYCLE_MIGRATION_VERSION: MODEL_LIFECYCLE_COLUMNS,
    MODEL_RESULT_OBSERVATION_MIGRATION_VERSION: MODEL_RESULT_OBSERVATION_COLUMNS,
}


def pending_migration_statements(
    connection: sqlite3.Connection, version: int, statements: tuple[str, ...]
) -> tuple[str, ...]:
    """Skip known columns left by an interrupted pre-release migration."""
    migration_columns = _REPAIRABLE_COLUMN_MIGRATIONS.get(version)
    if migration_columns is not None:
        existing = {
            (table_name, column_name)
            for table_name, column_name, _definition in migration_columns
            if _table_has_column(connection, table_name, column_name)
        }
        return tuple(
            statement
            for statement, (table_name, column_name, _definition) in zip(
                statements, migration_columns, strict=True
            )
            if (table_name, column_name) not in existing
        )
    if version != KNOWLEDGE_ANALYSIS_MIGRATION_VERSION:
        return statements
    if not _table_has_column(connection, "knowledge_reconciliation_candidates", "entity_subtype"):
        return statements
    # A pre-release database may expose the v26 column before recording v26.
    # Keep the remaining migration atomic while accepting that exact shape.
    return tuple(
        statement
        for statement in statements
        if statement != KNOWLEDGE_ANALYSIS_ENTITY_SUBTYPE_MIGRATION_STATEMENT
    )


def _table_has_column(connection: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM pragma_table_info(?) WHERE name = ?",
            (table_name, column_name),
        ).fetchone()
        is not None
    )
