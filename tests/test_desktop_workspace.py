"""Behavior checks for Desktop Knowledge Base creation and active binding."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from openkb import desktop_workspace, desktop_workspace_backup
from openkb.desktop_import import DesktopTextImportService
from openkb.desktop_import_store import DesktopImportStore
from openkb.desktop_knowledge_pages import DesktopKnowledgePageService
from openkb.desktop_model_capability_store import DesktopModelCapabilityStore
from openkb.desktop_model_execution_profile import build_analysis_execution_profile
from openkb.desktop_model_gateway import DesktopModelGateway
from openkb.desktop_model_operation_state import DesktopModelOperationContractStore
from openkb.desktop_model_result_migrations import MODEL_RESULT_OBSERVATION_COLUMNS
from openkb.desktop_model_settings import validate_desktop_model_settings
from openkb.desktop_prompt_contracts import prompt_contract_for
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
        "model_capability_compatibility_audit",
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


def test_open_accepts_preledger_knowledge_analysis_entity_subtype(tmp_path):
    """A pre-release v25 database may already contain the first v26 column."""
    kb_dir = tmp_path / "preledger-knowledge-analysis"
    state_dir = kb_dir / ".openkb"
    state_dir.mkdir(parents=True)
    (kb_dir / "raw").mkdir()
    database_path = state_dir / "state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        for version, statements in desktop_workspace._MIGRATIONS:
            if version > 25:
                break
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, "2026-08-20T00:00:00+00:00"),
            )
        connection.executemany(
            "INSERT INTO metadata (key, value) VALUES (?, ?)",
            (
                ("format", "openkb-desktop"),
                ("knowledge_base_name", "Preledger knowledge analysis"),
            ),
        )
        connection.execute(
            "ALTER TABLE knowledge_reconciliation_candidates ADD COLUMN entity_subtype TEXT"
        )

    activation = DesktopKnowledgeBaseRuntime().open(kb_dir)

    assert activation.knowledge_base.schema_version == LATEST_SCHEMA_VERSION
    with sqlite3.connect(database_path) as connection:
        columns = connection.execute(
            "PRAGMA table_info(knowledge_reconciliation_candidates)"
        ).fetchall()
        assert [str(row[1]) for row in columns].count("entity_subtype") == 1
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (
            LATEST_SCHEMA_VERSION,
        )
        assert connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'knowledge_reconciliation_candidate_sources'"
        ).fetchone() == (1,)


def test_result_and_capability_migrations_preserve_published_knowledge_idempotently(
    tmp_path,
) -> None:
    kb_dir = tmp_path / "pre-result-observations"
    source = tmp_path / "published-source.txt"
    source.write_text("Published knowledge survives model metadata upgrades.", encoding="utf-8")
    runtime = DesktopKnowledgeBaseRuntime()
    runtime.create(kb_dir, name="Migration fixture")
    imported = DesktopTextImportService(
        kb_dir,
        model_gateway=DesktopModelGateway(
            lambda *_args: (
                '{"schema_version":"openkb.knowledge-analysis.v1",'
                '"analysis_scope":"document","document_description":"Fixture",'
                '"concepts":[],"entities":[]}'
            )
        ),
    ).import_text(source)
    pages = DesktopKnowledgePageService(kb_dir)
    draft = pages.save_draft(
        page_id=None,
        kind="concept",
        title="Migration Safety",
        content_markdown="# Published authority",
    )
    published = pages.publish(draft.page_id)
    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            UPDATE model_calls
            SET status = 'failed', lifecycle_status = 'provider_failure'
            WHERE job_id = ?
            """,
            (imported.job.job_id,),
        )
        connection.execute(
            """
            UPDATE model_attempts
            SET status = 'failed', lifecycle_status = 'provider_failure'
            WHERE call_id IN (SELECT call_id FROM model_calls WHERE job_id = ?)
            """,
            (imported.job.job_id,),
        )
        _drop_post_v44_schema(connection)
        connection.execute("DROP TABLE model_capability_checks")
        for table, column, _definition in MODEL_RESULT_OBSERVATION_COLUMNS:
            connection.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        connection.execute("DELETE FROM schema_migrations WHERE version >= 43")
        connection.commit()

    first = DesktopKnowledgeBaseRuntime().open(kb_dir)
    second = DesktopKnowledgeBaseRuntime().open(kb_dir)

    assert first.knowledge_base.schema_version == LATEST_SCHEMA_VERSION
    assert second.knowledge_base.schema_version == LATEST_SCHEMA_VERSION
    restored = DesktopKnowledgePageService(kb_dir).get_page(published.page_id)
    assert restored.published_revision == published.published_revision
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            """
            SELECT version, COUNT(*) FROM schema_migrations
            WHERE version IN (43, 44) GROUP BY version
            """
        ).fetchall() == [(43, 1), (44, 1)]
        assert connection.execute(
            "SELECT status, lifecycle_status FROM model_calls WHERE job_id = ?",
            (imported.job.job_id,),
        ).fetchone() == ("failed", "provider_failure")
        for table, column, _definition in MODEL_RESULT_OBSERVATION_COLUMNS:
            columns = connection.execute(f"PRAGMA table_info({table})").fetchall()
            assert [str(row[1]) for row in columns].count(column) == 1
            assert connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {column} IS NOT NULL"
            ).fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM model_capability_checks").fetchone() == (0,)
        assert connection.execute(
            "SELECT availability FROM source_documents WHERE document_id = ?",
            (imported.document.document_id,),
        ).fetchone() == ("available",)


def test_operation_state_migration_leaves_ambiguous_graph_invalidation_unverified(
    tmp_path,
) -> None:
    kb_dir = tmp_path / "legacy-graph-invalidation"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    settings = validate_desktop_model_settings(
        provider="deepseek",
        model="deepseek-v4-pro",
        analysis_model="deepseek-v4-pro",
        api_base_url="https://api.deepseek.com",
        api_key="test-key",
        max_concurrent_model_calls=1,
        analysis_reasoning="off",
    )
    legacy = build_analysis_execution_profile(
        provider=settings.provider,
        model=settings.analysis_model_name,
        capability=settings.capability_for_role("analysis"),
        reasoning_effort="off",
        api_base_url=settings.api_base_url,
    )
    unknown_legacy = build_analysis_execution_profile(
        provider=settings.provider,
        model=settings.analysis_model_name,
        capability=settings.capability_for_role("analysis"),
        reasoning_effort="high",
        api_base_url=settings.api_base_url,
    )
    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        _drop_post_v44_schema(connection)
        connection.execute(
            """
            INSERT OR REPLACE INTO model_capability_checks (
                profile_identity, profile_json, status, failure_code, reason,
                checked_at, created_at, updated_at
            ) VALUES (?, ?, 'unchecked', 'model_response_invalid', ?, NULL, ?, ?)
            """,
            (
                legacy.identity,
                json.dumps(legacy.as_dict(), sort_keys=True, separators=(",", ":")),
                "Knowledge Graph response was invalid.",
                "2026-08-27T10:00:00+00:00",
                "2026-08-27T11:00:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT OR REPLACE INTO model_capability_checks (
                profile_identity, profile_json, status, failure_code, reason,
                checked_at, created_at, updated_at
            ) VALUES (?, ?, 'unchecked', 'model_response_invalid', ?, NULL, ?, ?)
            """,
            (
                unknown_legacy.identity,
                json.dumps(
                    unknown_legacy.as_dict(), sort_keys=True, separators=(",", ":")
                ),
                "Unknown legacy invalidation.",
                "2026-08-27T10:00:00+00:00",
                "2026-08-27T12:00:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO knowledge_graph_diagnostics (
                diagnostic_id, phase, error_code, document_id, created_at
            ) VALUES ('legacy-graph-failure', 'extraction',
                'knowledge_graph_response_invalid', NULL, '2026-08-27T11:00:00+00:00')
            """
        )
        connection.commit()

    DesktopKnowledgeBaseRuntime().open(kb_dir)

    shared = legacy.capability_evidence_profile
    capability = DesktopModelCapabilityStore(kb_dir).state(shared)
    operation = DesktopModelOperationContractStore(kb_dir).state(
        operation="knowledge_graph_extraction",
        capability_identity=shared.identity,
        prompt_contract_digest=prompt_contract_for("knowledge_graph_extraction").digest,
    )
    assert capability.status == "unchecked"
    assert DesktopModelCapabilityStore(kb_dir).state(
        unknown_legacy.capability_evidence_profile
    ).status == "unchecked"
    assert operation.status == "unverified"
    assert operation.failure_stage is None
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT decision FROM model_capability_compatibility_audit ORDER BY decision"
        ).fetchall() == [("left_unverified",), ("left_unverified",)]


def test_open_repairs_partial_preledger_result_observation_migration(tmp_path) -> None:
    """A terminated pre-release v43 migration may expose only its first DDL columns."""
    kb_dir = tmp_path / "partial-result-observations"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    database_path = kb_dir / ".openkb" / "state.sqlite3"
    retained = {
        ("model_calls", "finish_reason"),
        ("model_calls", "reasoning_observed"),
        ("model_calls", "final_content_observed"),
        ("model_calls", "reasoning_chunk_count"),
        ("model_calls", "final_chunk_count"),
        ("model_calls", "reasoning_character_count"),
        ("model_calls", "final_character_count"),
        ("model_attempts", "finish_reason"),
        ("model_attempts", "reasoning_observed"),
        ("model_attempts", "final_content_observed"),
    }
    with sqlite3.connect(database_path) as connection:
        _drop_post_v44_schema(connection)
        connection.execute("DROP TABLE model_capability_checks")
        for table, column, _definition in MODEL_RESULT_OBSERVATION_COLUMNS:
            if (table, column) not in retained:
                connection.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        connection.execute("DELETE FROM schema_migrations WHERE version >= 43")
        connection.commit()

    activation = DesktopKnowledgeBaseRuntime().open(kb_dir)

    assert activation.knowledge_base.schema_version == LATEST_SCHEMA_VERSION
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version FROM schema_migrations WHERE version IN (43, 44) ORDER BY version"
        ).fetchall() == [(43,), (44,)]
        for table, column, _definition in MODEL_RESULT_OBSERVATION_COLUMNS:
            columns = connection.execute(f"PRAGMA table_info({table})").fetchall()
            assert [str(row[1]) for row in columns].count(column) == 1


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


def test_migration_resets_legacy_running_imports_without_checkpoints(tmp_path):
    """An interrupted v2 job restarts at preflight rather than trusting missing evidence."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "legacy.txt"
    source.write_text("Legacy source.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TABLE knowledge_generation_item_sources")
        connection.execute("DROP TABLE knowledge_reconciliation_candidate_sources")
        _drop_conversation_schema(connection)
        _drop_knowledge_page_draft_schema(connection)
        connection.execute("ALTER TABLE knowledge_page_revisions DROP COLUMN provenance_state")
        connection.execute("DROP TABLE knowledge_reconciliation_resolution_records")
        connection.execute("DROP TABLE knowledge_reconciliation_candidates")
        connection.execute("DROP TABLE knowledge_graph_diagnostics")
        connection.execute("DROP TABLE knowledge_graph_edges")
        connection.execute("DROP TABLE knowledge_graph_nodes")
        _drop_retrieval_corpus_revision_schema(connection)
        connection.execute("DROP TABLE desktop_graph_feature_flags")
        connection.execute("DROP TABLE knowledge_generation_items")
        connection.execute("DROP TABLE knowledge_generation_state")
        connection.execute("DROP TABLE knowledge_generations")
        connection.execute("DROP TABLE document_version_candidates")
        connection.execute("DROP TRIGGER source_documents_create_version_source")
        connection.execute("DROP TABLE document_version_members")
        connection.execute("DROP TABLE document_version_sources")
        connection.execute("DROP TABLE knowledge_page_revisions")
        connection.execute("DROP TABLE knowledge_pages")
        connection.execute("DROP TABLE import_deduplications")
        connection.execute("DROP TABLE evidence_occurrences")
        connection.execute("DROP TABLE evidence_fingerprints")
        connection.execute("DROP TABLE document_content_fingerprints")
        connection.execute("DROP TABLE source_images")
        connection.execute("DROP TABLE grounded_answer_source_images")
        connection.execute("DROP TABLE grounded_answer_citations")
        connection.execute("DROP TABLE grounded_answers")
        connection.execute("DROP TABLE stage_run_runtime")
        connection.execute("DROP TABLE import_job_runtime")
        connection.execute("DROP TABLE model_attempts")
        connection.execute("DROP TABLE model_calls")
        connection.execute("DROP TABLE quarantined_documents")
        connection.execute("DROP TABLE recovery_runs")
        connection.execute("DROP TRIGGER raw_assets_create_integrity")
        connection.execute("DROP TABLE raw_asset_integrity")
        connection.execute("DELETE FROM schema_migrations WHERE version = 7")
        connection.execute("DELETE FROM schema_migrations WHERE version = 10")
        connection.execute("DELETE FROM schema_migrations WHERE version = 11")
        connection.execute("DELETE FROM schema_migrations WHERE version = 12")
        connection.execute("DELETE FROM schema_migrations WHERE version = 13")
        connection.execute("DELETE FROM schema_migrations WHERE version = 14")
        connection.execute("DELETE FROM schema_migrations WHERE version = 15")
        connection.execute("DELETE FROM schema_migrations WHERE version = 16")
        connection.execute("DELETE FROM schema_migrations WHERE version = 17")
        connection.execute("DELETE FROM schema_migrations WHERE version = 18")
        connection.execute("DELETE FROM schema_migrations WHERE version = 19")
        connection.execute("DELETE FROM schema_migrations WHERE version = 20")
        connection.execute("DELETE FROM schema_migrations WHERE version = 21")
        connection.execute("DELETE FROM schema_migrations WHERE version = 22")
        connection.execute("DELETE FROM schema_migrations WHERE version = 23")
        _drop_page_tree_schema(connection)
        connection.execute("DROP TABLE knowledge_reanalysis_merges")
        connection.execute("DROP TABLE knowledge_reanalysis_batches")
        connection.execute("DROP TABLE knowledge_reanalysis_jobs")
        connection.execute("DROP TABLE knowledge_reanalysis_runs")
        connection.execute("DELETE FROM schema_migrations WHERE version = 31")
        connection.execute("DROP TABLE knowledge_analysis_merges")
        connection.execute("DROP TABLE knowledge_analysis_batches")
        connection.execute("DELETE FROM schema_migrations WHERE version = 30")
        connection.execute("DROP TABLE knowledge_missing_source_resolution_records")
        connection.execute("DROP TABLE knowledge_missing_source_candidates")
        connection.execute("DELETE FROM schema_migrations WHERE version = 29")
        connection.execute("DELETE FROM schema_migrations WHERE version = 28")
        connection.execute("DELETE FROM schema_migrations WHERE version = 27")
        connection.execute("DELETE FROM schema_migrations WHERE version = 26")
        connection.execute("DELETE FROM schema_migrations WHERE version = 25")
        connection.execute("DELETE FROM schema_migrations WHERE version = 24")
        connection.execute("DELETE FROM schema_migrations WHERE version = 9")
        connection.execute("DELETE FROM schema_migrations WHERE version = 8")
        connection.execute("DELETE FROM schema_migrations WHERE version = 6")
        connection.execute("DELETE FROM schema_migrations WHERE version = 5")
        connection.execute("DELETE FROM schema_migrations WHERE version = 4")
        connection.execute("DELETE FROM schema_migrations WHERE version = 3")
        connection.execute(
            """
            INSERT INTO import_jobs (
                job_id, source_path, document_id, status, progress, error_code,
                created_at, completed_at
            ) VALUES ('legacy-job', ?, NULL, 'running', 75, NULL, '2026-01-01T00:00:00+00:00', NULL)
            """,
            (str(source),),
        )
        connection.executemany(
            """
            INSERT INTO stage_runs (
                stage_run_id, job_id, stage, status, progress, error_code, started_at, completed_at
            ) VALUES (?, 'legacy-job', ?, ?, ?, NULL, NULL, NULL)
            """,
            [
                ("legacy-preflight", "preflight", "completed", 20),
                ("legacy-raw", "raw_asset", "completed", 35),
                ("legacy-ir", "document_ir", "completed", 55),
                ("legacy-evidence", "evidence", "running", 60),
                ("legacy-search", "search", "pending", 0),
            ],
        )

    DesktopKnowledgeBaseRuntime().open(kb_dir)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version FROM schema_migrations").fetchall() == [
            (version,) for version in range(1, LATEST_SCHEMA_VERSION + 1)
        ]
        assert connection.execute(
            "SELECT status FROM import_job_runtime WHERE job_id = 'legacy-job'"
        ).fetchone() == ("recoverable",)
        assert connection.execute(
            "SELECT progress FROM import_jobs WHERE job_id = 'legacy-job'"
        ).fetchone() == (0,)
        assert (
            connection.execute(
                "SELECT status, progress FROM stage_runs WHERE job_id = 'legacy-job' ORDER BY stage"
            ).fetchall()
            == [("pending", 0)] * 7
        )


def test_v3_import_job_gets_model_stage_before_resume(tmp_path):
    """Migration 4 fills the stage contract required by resumed legacy jobs."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "legacy-paused.txt"
    source.write_text("Resume through the new model stage.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    store = DesktopImportStore(kb_dir)
    state = store.create_job(source)
    database_path = kb_dir / ".openkb" / "state.sqlite3"

    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TABLE knowledge_generation_item_sources")
        connection.execute("DROP TABLE knowledge_reconciliation_candidate_sources")
        _drop_conversation_schema(connection)
        _drop_knowledge_page_draft_schema(connection)
        connection.execute("DROP TABLE knowledge_reconciliation_resolution_records")
        connection.execute("DROP TABLE knowledge_reconciliation_candidates")
        connection.execute("DROP TABLE knowledge_graph_diagnostics")
        connection.execute("DROP TABLE knowledge_graph_edges")
        connection.execute("DROP TABLE knowledge_graph_nodes")
        _drop_retrieval_corpus_revision_schema(connection)
        connection.execute("DROP TABLE desktop_graph_feature_flags")
        connection.execute("DROP TABLE knowledge_generation_items")
        connection.execute("DROP TABLE knowledge_generation_state")
        connection.execute("DROP TABLE knowledge_generations")
        connection.execute("DROP TABLE document_version_candidates")
        connection.execute("DROP TRIGGER source_documents_create_version_source")
        connection.execute("DROP TABLE document_version_members")
        connection.execute("DROP TABLE document_version_sources")
        connection.execute("DROP TABLE knowledge_page_revisions")
        connection.execute("DROP TABLE knowledge_pages")
        connection.execute("DROP TABLE import_deduplications")
        connection.execute("DROP TABLE evidence_occurrences")
        connection.execute("DROP TABLE evidence_fingerprints")
        connection.execute("DROP TABLE document_content_fingerprints")
        connection.execute("DROP TABLE source_images")
        connection.execute("DROP TABLE grounded_answer_source_images")
        connection.execute("DROP TABLE grounded_answer_citations")
        connection.execute("DROP TABLE grounded_answers")
        connection.execute("DROP TABLE model_attempts")
        connection.execute("DROP TABLE model_calls")
        connection.execute("DROP TABLE quarantined_documents")
        connection.execute("DROP TABLE recovery_runs")
        connection.execute("DROP TRIGGER raw_assets_create_integrity")
        connection.execute("DROP TABLE raw_asset_integrity")
        connection.execute("DELETE FROM schema_migrations WHERE version = 7")
        connection.execute("DELETE FROM schema_migrations WHERE version = 10")
        connection.execute("DELETE FROM schema_migrations WHERE version = 11")
        connection.execute("DELETE FROM schema_migrations WHERE version = 12")
        connection.execute("DELETE FROM schema_migrations WHERE version = 13")
        connection.execute("DELETE FROM schema_migrations WHERE version = 14")
        connection.execute("DELETE FROM schema_migrations WHERE version = 15")
        connection.execute("DELETE FROM schema_migrations WHERE version = 16")
        connection.execute("DELETE FROM schema_migrations WHERE version = 17")
        connection.execute("DELETE FROM schema_migrations WHERE version = 18")
        connection.execute("DELETE FROM schema_migrations WHERE version = 19")
        connection.execute("DELETE FROM schema_migrations WHERE version = 20")
        connection.execute("DELETE FROM schema_migrations WHERE version = 21")
        connection.execute("DELETE FROM schema_migrations WHERE version = 22")
        connection.execute("DELETE FROM schema_migrations WHERE version = 23")
        _drop_page_tree_schema(connection)
        connection.execute("DROP TABLE knowledge_reanalysis_merges")
        connection.execute("DROP TABLE knowledge_reanalysis_batches")
        connection.execute("DROP TABLE knowledge_reanalysis_jobs")
        connection.execute("DROP TABLE knowledge_reanalysis_runs")
        connection.execute("DELETE FROM schema_migrations WHERE version = 31")
        connection.execute("DROP TABLE knowledge_analysis_merges")
        connection.execute("DROP TABLE knowledge_analysis_batches")
        connection.execute("DELETE FROM schema_migrations WHERE version = 30")
        connection.execute("DROP TABLE knowledge_missing_source_resolution_records")
        connection.execute("DROP TABLE knowledge_missing_source_candidates")
        connection.execute("DELETE FROM schema_migrations WHERE version = 29")
        connection.execute("DELETE FROM schema_migrations WHERE version = 28")
        connection.execute("DELETE FROM schema_migrations WHERE version = 27")
        connection.execute("DELETE FROM schema_migrations WHERE version = 26")
        connection.execute("DELETE FROM schema_migrations WHERE version = 25")
        connection.execute("DELETE FROM schema_migrations WHERE version = 24")
        connection.execute("DELETE FROM schema_migrations WHERE version = 9")
        connection.execute("DELETE FROM schema_migrations WHERE version = 8")
        connection.execute("DELETE FROM schema_migrations WHERE version = 6")
        connection.execute("DELETE FROM schema_migrations WHERE version = 5")
        connection.execute("DELETE FROM schema_migrations WHERE version = 4")
        connection.execute(
            "DELETE FROM stage_run_runtime WHERE job_id = ? AND stage_run_id = ?",
            (state.job_id, state.stage_ids["model_analysis"]),
        )
        connection.execute(
            "DELETE FROM stage_runs WHERE job_id = ? AND stage = 'model_analysis'", (state.job_id,)
        )
        connection.execute(
            "UPDATE import_job_runtime SET status = 'paused' WHERE job_id = ?", (state.job_id,)
        )

    DesktopKnowledgeBaseRuntime().open(kb_dir)
    resumed = DesktopTextImportService(kb_dir).resume_text(state.job_id)

    assert resumed.job.status == "completed"
    assert [stage.stage for stage in resumed.stages] == [
        "preflight",
        "raw_asset",
        "document_ir",
        "evidence",
        "deterministic_page_tree",
        "model_analysis",
        "search",
    ]
    assert resumed.stages[5].status == "skipped"


def test_migration_backfills_independent_version_sources_for_existing_documents(tmp_path):
    """Opening an older Desktop KB makes existing imports eligible for D3 review."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "existing.txt"
    source.write_text("Existing document content.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    imported = DesktopTextImportService(kb_dir).import_text(source)
    database_path = kb_dir / ".openkb" / "state.sqlite3"

    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TABLE knowledge_generation_item_sources")
        connection.execute("DROP TABLE knowledge_reconciliation_candidate_sources")
        _drop_conversation_schema(connection)
        _drop_knowledge_page_draft_schema(connection)
        connection.execute("ALTER TABLE knowledge_page_revisions DROP COLUMN provenance_state")
        connection.execute("DROP TABLE knowledge_reconciliation_resolution_records")
        connection.execute("DROP TABLE knowledge_reconciliation_candidates")
        connection.execute("DROP TABLE knowledge_graph_diagnostics")
        connection.execute("DROP TABLE knowledge_graph_edges")
        connection.execute("DROP TABLE knowledge_graph_nodes")
        _drop_retrieval_corpus_revision_schema(connection)
        connection.execute("DROP TABLE desktop_graph_feature_flags")
        connection.execute("DROP TABLE knowledge_generation_items")
        connection.execute("DROP TABLE knowledge_generation_state")
        connection.execute("DROP TABLE knowledge_generations")
        connection.execute("DROP TABLE document_version_candidates")
        connection.execute("DROP TRIGGER source_documents_create_version_source")
        connection.execute("DROP TABLE document_version_members")
        connection.execute("DROP TABLE document_version_sources")
        connection.execute("DELETE FROM schema_migrations WHERE version = 13")
        connection.execute("DELETE FROM schema_migrations WHERE version = 14")
        connection.execute("DELETE FROM schema_migrations WHERE version = 15")
        connection.execute("DELETE FROM schema_migrations WHERE version = 16")
        connection.execute("DELETE FROM schema_migrations WHERE version = 17")
        connection.execute("DELETE FROM schema_migrations WHERE version = 18")
        connection.execute("DELETE FROM schema_migrations WHERE version = 19")
        connection.execute("DELETE FROM schema_migrations WHERE version = 20")
        connection.execute("DELETE FROM schema_migrations WHERE version = 21")
        connection.execute("DELETE FROM schema_migrations WHERE version = 22")
        connection.execute("DELETE FROM schema_migrations WHERE version = 23")
        _drop_page_tree_schema(connection)
        connection.execute("DROP TABLE knowledge_reanalysis_merges")
        connection.execute("DROP TABLE knowledge_reanalysis_batches")
        connection.execute("DROP TABLE knowledge_reanalysis_jobs")
        connection.execute("DROP TABLE knowledge_reanalysis_runs")
        connection.execute("DELETE FROM schema_migrations WHERE version = 31")
        connection.execute("DROP TABLE knowledge_analysis_merges")
        connection.execute("DROP TABLE knowledge_analysis_batches")
        connection.execute("DELETE FROM schema_migrations WHERE version = 30")
        connection.execute("DROP TABLE knowledge_missing_source_resolution_records")
        connection.execute("DROP TABLE knowledge_missing_source_candidates")
        connection.execute("DELETE FROM schema_migrations WHERE version = 29")
        connection.execute("DELETE FROM schema_migrations WHERE version = 28")
        connection.execute("DELETE FROM schema_migrations WHERE version = 27")
        connection.execute("DELETE FROM schema_migrations WHERE version = 26")
        connection.execute("DELETE FROM schema_migrations WHERE version = 25")
        connection.execute("DELETE FROM schema_migrations WHERE version = 24")

    DesktopKnowledgeBaseRuntime().open(kb_dir)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT source_id FROM document_version_members WHERE document_id = ?",
            (imported.document.document_id,),
        ).fetchone() == (imported.document.document_id,)


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
