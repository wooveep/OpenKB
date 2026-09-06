"""Migration statement selection for the current Desktop schema epoch."""

from __future__ import annotations

import sqlite3

from openkb.knowledge.corpus.integrity_migrations import CORPUS_INTEGRITY_COLUMNS
from openkb.knowledge.corpus.knowledge_migrations import (
    pending_corpus_knowledge_migration_statements,
)
from openkb.knowledge.pages.adoption_migrations import (
    KNOWLEDGE_ADOPTION_REQUEST_INPUT_COLUMNS,
)
from openkb.models.observability_migrations import MODEL_LIFECYCLE_COLUMNS
from openkb.models.result_migrations import MODEL_RESULT_OBSERVATION_COLUMNS
from openkb.workspace.feature_migrations import (
    CORPUS_INTEGRITY_MIGRATION_VERSION,
    CORPUS_KNOWLEDGE_MIGRATION_VERSION,
    DOCUMENT_VERSION_CATALOG_MIGRATION_VERSION,
    KNOWLEDGE_ADOPTION_REQUEST_INPUT_MIGRATION_VERSION,
    MODEL_LIFECYCLE_MIGRATION_VERSION,
    MODEL_RESULT_OBSERVATION_MIGRATION_VERSION,
)

_REPAIRABLE_COLUMN_MIGRATIONS = {
    CORPUS_INTEGRITY_MIGRATION_VERSION: CORPUS_INTEGRITY_COLUMNS,
    KNOWLEDGE_ADOPTION_REQUEST_INPUT_MIGRATION_VERSION: (KNOWLEDGE_ADOPTION_REQUEST_INPUT_COLUMNS),
    MODEL_LIFECYCLE_MIGRATION_VERSION: MODEL_LIFECYCLE_COLUMNS,
    MODEL_RESULT_OBSERVATION_MIGRATION_VERSION: MODEL_RESULT_OBSERVATION_COLUMNS,
}


def pending_migration_statements(
    connection: sqlite3.Connection, version: int, statements: tuple[str, ...]
) -> tuple[str, ...]:
    """Return statements required by a current-epoch database."""
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
    if version == CORPUS_KNOWLEDGE_MIGRATION_VERSION:
        return pending_corpus_knowledge_migration_statements(connection)
    return statements


def apply_feature_migration_backfill_in(
    connection: sqlite3.Connection,
    version: int,
    *,
    now: str,
) -> None:
    """Run deterministic data backfills owned by an additive schema migration."""
    if version == DOCUMENT_VERSION_CATALOG_MIGRATION_VERSION:
        from openkb.documents.version_catalog import (
            backfill_document_version_catalog_in,
        )

        backfill_document_version_catalog_in(connection, now=now)


def _table_has_column(connection: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM pragma_table_info(?) WHERE name = ?",
            (table_name, column_name),
        ).fetchone()
        is not None
    )
