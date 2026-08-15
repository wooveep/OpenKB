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


def test_create_open_and_switch_desktop_knowledge_bases_checkpoint_the_previous_one(tmp_path):
    """One Engine owns one active SQLite knowledge base and checkpoints before a switch."""
    runtime = DesktopKnowledgeBaseRuntime()
    first_dir = tmp_path / "first"
    first = runtime.create(first_dir, name="First knowledge base")

    assert first.knowledge_base.name == "First knowledge base"
    assert first.knowledge_base.schema_version == 11
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
        connection.execute("DROP TABLE knowledge_page_revisions")
        connection.execute("DROP TABLE knowledge_pages")
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
            == [("pending", 0)] * 6
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
        connection.execute("DROP TABLE knowledge_page_revisions")
        connection.execute("DROP TABLE knowledge_pages")
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
        "model_analysis",
        "search",
    ]
    assert resumed.stages[4].status == "skipped"


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
