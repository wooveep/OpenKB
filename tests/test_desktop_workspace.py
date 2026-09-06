"""Behavior checks for Desktop Knowledge Base creation and active binding."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from openkb import desktop_workspace, desktop_workspace_backup
from openkb.desktop_model_result_migrations import MODEL_RESULT_OBSERVATION_COLUMNS
from openkb.desktop_workspace import (
    DesktopKnowledgeBaseNotFoundError,
    DesktopKnowledgeBaseRuntime,
    DesktopKnowledgeBaseStateError,
    LegacyKnowledgeBaseUnsupportedError,
)

LATEST_SCHEMA_VERSION = desktop_workspace._MIGRATIONS[-1][0]


def _drop_post_v44_schema(connection: sqlite3.Connection) -> None:
    """Return a fixture to the schema before operation-scoped model state."""
    connection.execute("DROP VIEW IF EXISTS current_knowledge_graph_edges")
    connection.execute("DROP VIEW IF EXISTS current_knowledge_graph_nodes")
    for table in (
        "knowledge_graph_attempt_issues",
        "knowledge_graph_attempts",
        "knowledge_graph_current",
        "knowledge_graph_result_edges",
        "knowledge_graph_result_nodes",
        "knowledge_graph_results",
        "knowledge_adoption_requests",
        "knowledge_origin_references",
        "model_operation_contract_events",
        "model_operation_retry_permits",
        "model_operation_contract_states",
    ):
        connection.execute(f"DROP TABLE IF EXISTS {table}")
    _drop_columns_if_present(
        connection,
        "knowledge_graph_nodes",
        ("support_start", "support_end", "verification_state"),
    )
    _drop_columns_if_present(
        connection,
        "knowledge_graph_edges",
        ("relation_label", "support_start", "support_end", "verification_state"),
    )
    _drop_columns_if_present(
        connection,
        "knowledge_graph_extraction_tasks",
        ("retry_scope",),
    )
    _drop_columns_if_present(
        connection,
        "document_page_tree_enrichment_tasks",
        ("retry_scope",),
    )
    connection.execute("DELETE FROM schema_migrations WHERE version >= 45")


def _drop_columns_if_present(
    connection: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
) -> None:
    existing = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
    for column in columns:
        if column in existing:
            connection.execute(f"ALTER TABLE {table} DROP COLUMN {column}")


def _drop_post_v37_schema(connection: sqlite3.Connection) -> None:
    """Return a fixture to the schema before explicit-terminal model migrations."""
    _drop_post_v44_schema(connection)
    for table in (
        "model_capability_checks",
        "knowledge_graph_extraction_tasks",
        "legacy_model_recovery_audit",
        "model_usage_records",
        "knowledge_analysis_merge_nodes",
        "knowledge_reanalysis_merge_nodes",
        "knowledge_analysis_plans",
        "knowledge_reanalysis_plans",
    ):
        connection.execute(f"DROP TABLE IF EXISTS {table}")
    for table, columns in (
        ("model_calls", ("lifecycle_status", "elapsed_seconds", "retry_after_seconds")),
        ("model_attempts", ("lifecycle_status", "elapsed_seconds", "retry_after_seconds")),
    ):
        existing = {
            str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for column in columns:
            if column in existing:
                connection.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    for table, column, _definition in MODEL_RESULT_OBSERVATION_COLUMNS:
        if connection.execute(
            "SELECT 1 FROM pragma_table_info(?) WHERE name = ?", (table, column)
        ).fetchone():
            connection.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    connection.execute("DELETE FROM schema_migrations WHERE version >= 38")


def _drop_retrieval_corpus_revision_schema(connection: sqlite3.Connection) -> None:
    """Return a test fixture to the state before migration 17."""
    triggers = connection.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type = 'trigger' AND name LIKE 'desktop_retrieval_corpus_%'
        """
    ).fetchall()
    for (trigger_name,) in triggers:
        connection.execute(f'DROP TRIGGER "{trigger_name}"')
    connection.execute("DROP TABLE desktop_retrieval_corpus_state")


def _drop_conversation_schema(connection: sqlite3.Connection) -> None:
    """Return a test fixture to the state before migration 18."""
    connection.execute("DROP TABLE conversation_ui_state")
    connection.execute("DROP TABLE conversation_answer_source_images")
    connection.execute("DROP TABLE conversation_answer_citations")
    connection.execute("DROP TABLE conversation_answer_versions")
    connection.execute("DROP TABLE conversation_messages")
    connection.execute("DROP TABLE conversations")


def _drop_knowledge_page_draft_schema(connection: sqlite3.Connection) -> None:
    """Return a test fixture to the state before Knowledge editor migrations."""
    _drop_catalog_schema(connection)
    connection.execute("DROP TABLE knowledge_page_lifecycle_events")
    connection.execute("DROP TABLE knowledge_page_verifications")
    connection.execute("DROP TABLE knowledge_page_revision_sources")
    connection.execute("DROP TABLE knowledge_page_working_sources")
    connection.execute("DROP TABLE knowledge_page_ui_state")
    connection.execute("DROP TABLE knowledge_page_working_drafts")
    connection.execute("ALTER TABLE knowledge_pages DROP COLUMN stale_after")
    connection.execute("ALTER TABLE knowledge_pages DROP COLUMN lifecycle_state")


def _drop_page_tree_schema(connection: sqlite3.Connection) -> None:
    """Return a fixture to the state before deterministic PageTrees."""
    connection.execute("DROP TABLE grounded_answer_retrieval_traces")
    connection.execute("DROP TABLE conversation_answer_retrieval_traces")
    _drop_catalog_schema(connection)
    for table in (
        "document_page_tree_provider_current",
        "document_page_tree_enrichment_current",
        "document_page_tree_enrichment_summaries",
        "document_page_tree_enrichment_tasks",
        "document_page_tree_enrichment_generations",
        "document_page_tree_current",
        "document_page_tree_node_images",
        "document_page_tree_node_evidence",
        "document_page_tree_nodes",
        "document_page_tree_rebuild_tasks",
        "document_page_tree_generations",
    ):
        connection.execute(f"DROP TABLE IF EXISTS {table}")
    connection.execute("DROP INDEX import_jobs_document_completed_idx")
    _drop_post_v37_schema(connection)
    connection.execute("DELETE FROM schema_migrations WHERE version IN (32, 33, 34, 35, 36, 37)")


def _drop_catalog_schema(connection: sqlite3.Connection) -> None:
    for (name,) in connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'knowledge_catalog_%'"
    ).fetchall():
        connection.execute(f'DROP TRIGGER "{name}"')
    for table in (
        "knowledge_catalog_rebuild_tasks",
        "knowledge_catalog_state",
        "knowledge_catalog_links",
        "knowledge_catalog_node_sources",
        "knowledge_catalog_nodes",
        "knowledge_catalog_generations",
    ):
        connection.execute(f"DROP TABLE IF EXISTS {table}")


def test_create_open_and_switch_desktop_knowledge_bases_checkpoint_the_previous_one(tmp_path):
    """One Engine owns one active SQLite knowledge base and checkpoints before a switch."""
    runtime = DesktopKnowledgeBaseRuntime()
    first_dir = tmp_path / "first"
    first = runtime.create(first_dir, name="First knowledge base")

    assert first.knowledge_base.name == "First knowledge base"
    assert first.knowledge_base.schema_version == LATEST_SCHEMA_VERSION
    assert first.knowledge_base.last_checkpoint_at is None
    assert (first_dir / "raw").is_dir()
    database_path = first_dir / ".openkb" / "state.sqlite3"
    assert database_path.is_file()
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version FROM schema_migrations").fetchall() == [
            (version,) for version in range(1, LATEST_SCHEMA_VERSION + 1)
        ]
        assert connection.execute("SELECT value FROM metadata WHERE key = 'format'").fetchone() == (
            "openkb-desktop",
        )
        assert connection.execute(
            "SELECT value FROM metadata WHERE key = 'knowledge_base_name'"
        ).fetchone() == ("First knowledge base",)

    second_dir = tmp_path / "second"
    second = runtime.create(second_dir)

    assert second.previous_kb_dir == str(first_dir)
    assert second.checkpointed is True
    assert runtime.active() == second.knowledge_base
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT reason FROM runtime_checkpoints").fetchall() == [
            ("knowledge_base_switched",)
        ]

    (first_dir / "wiki").mkdir()
    reopened = DesktopKnowledgeBaseRuntime().open(first_dir)
    assert reopened.knowledge_base.name == "First knowledge base"
    assert reopened.knowledge_base.last_checkpoint_at is not None


def test_existing_database_migration_ddl_rolls_back_before_ledger_commit(
    tmp_path, monkeypatch
) -> None:
    kb_dir = tmp_path / "atomic-existing-migration"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        _drop_post_v44_schema(connection)
        connection.execute("DROP TABLE model_capability_checks")
        connection.execute("DELETE FROM schema_migrations WHERE version >= 44")
        connection.commit()

    migrations = tuple(
        (
            version,
            (
                "ALTER TABLE model_calls ADD COLUMN atomic_migration_probe TEXT",
                "THIS IS NOT VALID SQLITE",
            )
            if version == 44
            else statements,
        )
        for version, statements in desktop_workspace._MIGRATIONS
    )
    monkeypatch.setattr(desktop_workspace, "_MIGRATIONS", migrations)

    with pytest.raises(DesktopKnowledgeBaseStateError):
        DesktopKnowledgeBaseRuntime().open(kb_dir)

    with sqlite3.connect(database_path) as connection:
        columns = connection.execute("PRAGMA table_info(model_calls)").fetchall()
        assert "atomic_migration_probe" not in {str(row[1]) for row in columns}
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (43,)


def test_existing_database_migration_creates_a_restorable_backup_before_ddl(
    tmp_path, monkeypatch
) -> None:
    kb_dir = tmp_path / "backed-up-migration"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    database_path = kb_dir / ".openkb" / "state.sqlite3"
    migration_version = LATEST_SCHEMA_VERSION + 1
    monkeypatch.setattr(
        desktop_workspace,
        "_MIGRATIONS",
        (
            *desktop_workspace._MIGRATIONS,
            (
                migration_version,
                (
                    "ALTER TABLE model_calls ADD COLUMN backup_probe TEXT",
                    "THIS IS NOT VALID SQLITE",
                ),
            ),
        ),
    )

    with pytest.raises(DesktopKnowledgeBaseStateError):
        DesktopKnowledgeBaseRuntime().open(kb_dir)

    backups = sorted((kb_dir / ".openkb" / "migration-backups").glob("*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert backup.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (
            LATEST_SCHEMA_VERSION,
        )
        assert "backup_probe" not in {
            str(row[1]) for row in backup.execute("PRAGMA table_info(model_calls)")
        }
    with sqlite3.connect(database_path) as connection:
        assert "backup_probe" not in {
            str(row[1]) for row in connection.execute("PRAGMA table_info(model_calls)")
        }
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (
            LATEST_SCHEMA_VERSION,
        )


def test_each_migration_attempt_backs_up_the_latest_source_state(tmp_path) -> None:
    kb_dir = tmp_path / "fresh-backup-per-attempt"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        first = desktop_workspace_backup.create_migration_backup(
            connection,
            database_path=database_path,
            current_version=LATEST_SCHEMA_VERSION,
            target_version=LATEST_SCHEMA_VERSION + 1,
        )
        connection.execute("CREATE TABLE retry_marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO retry_marker (value) VALUES ('latest')")
        connection.commit()
        second = desktop_workspace_backup.create_migration_backup(
            connection,
            database_path=database_path,
            current_version=LATEST_SCHEMA_VERSION,
            target_version=LATEST_SCHEMA_VERSION + 1,
        )

    assert second != first
    with sqlite3.connect(second) as backup:
        assert backup.execute("SELECT value FROM retry_marker").fetchone() == ("latest",)


def test_migration_backups_are_bounded_per_version_edge(tmp_path) -> None:
    kb_dir = tmp_path / "bounded-migration-backups"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    database_path = kb_dir / ".openkb" / "state.sqlite3"
    created: list[Path] = []
    with sqlite3.connect(database_path) as connection:
        for _attempt in range(5):
            created.append(
                desktop_workspace_backup.create_migration_backup(
                    connection,
                    database_path=database_path,
                    current_version=LATEST_SCHEMA_VERSION,
                    target_version=LATEST_SCHEMA_VERSION + 1,
                )
            )

    backups = tuple((database_path.parent / "migration-backups").glob("*.sqlite3"))
    assert len(backups) == 3
    assert created[-1] in backups


def test_opening_a_legacy_knowledge_base_is_rejected_without_creating_desktop_state(kb_dir):
    """Desktop does not migrate or change an existing CLI/Web knowledge base."""
    with pytest.raises(LegacyKnowledgeBaseUnsupportedError) as error:
        DesktopKnowledgeBaseRuntime().open(kb_dir)

    assert error.value.code == "legacy_knowledge_base_unsupported"
    assert not (kb_dir / ".openkb" / "state.sqlite3").exists()
    assert not (kb_dir / ".openkb" / "ingest.lock").exists()


def test_opening_a_plain_directory_does_not_create_desktop_state(tmp_path):
    """Choosing a non-knowledge-base directory leaves it untouched."""
    directory = tmp_path / "plain-directory"
    directory.mkdir()

    with pytest.raises(DesktopKnowledgeBaseNotFoundError):
        DesktopKnowledgeBaseRuntime().open(directory)

    assert not (directory / ".openkb").exists()


def test_failed_initialization_leaves_a_knowledge_base_directory_reusable(tmp_path, monkeypatch):
    """A failed first transaction does not strand a partial Desktop Knowledge Base."""
    kb_dir = tmp_path / "retryable"
    original_set_metadata = desktop_workspace._set_metadata

    def fail_metadata(*_args, **_kwargs):
        raise sqlite3.OperationalError("simulated metadata write failure")

    monkeypatch.setattr(desktop_workspace, "_set_metadata", fail_metadata)
    with pytest.raises(DesktopKnowledgeBaseStateError):
        DesktopKnowledgeBaseRuntime().create(kb_dir)

    assert not (kb_dir / ".openkb" / "state.sqlite3").exists()
    assert (kb_dir / "raw").is_dir()
    assert not any((kb_dir / "raw").iterdir())

    monkeypatch.setattr(desktop_workspace, "_set_metadata", original_set_metadata)
    assert DesktopKnowledgeBaseRuntime().create(kb_dir).knowledge_base.name == "retryable"


def test_interrupted_initialization_is_recovered_before_opening_or_recreating(tmp_path):
    """A restart never exposes a SQLite file from an interrupted initial creation."""
    kb_dir = tmp_path / "interrupted"
    state_dir = kb_dir / ".openkb"
    state_dir.mkdir(parents=True)
    (kb_dir / "raw").mkdir()
    (state_dir / "initializing").touch()
    sqlite3.connect(state_dir / "state.sqlite3").close()

    with pytest.raises(DesktopKnowledgeBaseNotFoundError):
        DesktopKnowledgeBaseRuntime().open(kb_dir)

    assert not (state_dir / "initializing").exists()
    assert not (state_dir / "state.sqlite3").exists()
    assert DesktopKnowledgeBaseRuntime().create(kb_dir).knowledge_base.name == "interrupted"
