"""Behavior checks for Desktop Knowledge Base creation and active binding."""

from __future__ import annotations

import sqlite3

import pytest

from openkb import desktop_workspace
from openkb.desktop_import import DesktopTextImportService
from openkb.desktop_import_store import DesktopImportStore
from openkb.desktop_workspace import (
    DesktopKnowledgeBaseNotFoundError,
    DesktopKnowledgeBaseRuntime,
    DesktopKnowledgeBaseStateError,
    LegacyKnowledgeBaseUnsupportedError,
)


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
    for table in (
        "document_page_tree_current",
        "document_page_tree_node_images",
        "document_page_tree_node_evidence",
        "document_page_tree_nodes",
        "document_page_tree_rebuild_tasks",
        "document_page_tree_generations",
    ):
        connection.execute(f"DROP TABLE {table}")
    connection.execute("DROP INDEX import_jobs_document_completed_idx")
    connection.execute("DELETE FROM schema_migrations WHERE version IN (32, 33)")


def test_create_open_and_switch_desktop_knowledge_bases_checkpoint_the_previous_one(tmp_path):
    """One Engine owns one active SQLite knowledge base and checkpoints before a switch."""
    runtime = DesktopKnowledgeBaseRuntime()
    first_dir = tmp_path / "first"
    first = runtime.create(first_dir, name="First knowledge base")

    assert first.knowledge_base.name == "First knowledge base"
    assert first.knowledge_base.schema_version == 33
    assert first.knowledge_base.last_checkpoint_at is None
    assert (first_dir / "raw").is_dir()
    database_path = first_dir / ".openkb" / "state.sqlite3"
    assert database_path.is_file()
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version FROM schema_migrations").fetchall() == [
            (1,),
            (2,),
            (3,),
            (4,),
            (5,),
            (6,),
            (7,),
            (8,),
            (9,),
            (10,),
            (11,),
            (12,),
            (13,),
            (14,),
            (15,),
            (16,),
            (17,),
            (18,),
            (19,),
            (20,),
            (21,),
            (22,),
            (23,),
            (24,),
            (25,),
            (26,),
            (27,),
            (28,),
            (29,),
            (30,),
            (31,),
            (32,),
            (33,),
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
            (1,),
            (2,),
            (3,),
            (4,),
            (5,),
            (6,),
            (7,),
            (8,),
            (9,),
            (10,),
            (11,),
            (12,),
            (13,),
            (14,),
            (15,),
            (16,),
            (17,),
            (18,),
            (19,),
            (20,),
            (21,),
            (22,),
            (23,),
            (24,),
            (25,),
            (26,),
            (27,),
            (28,),
            (29,),
            (30,),
            (31,),
            (32,),
            (33,),
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
