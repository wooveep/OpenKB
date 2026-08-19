"""Deterministic Document PageTree tracer-bullet behavior."""

from __future__ import annotations

import json
import sqlite3

import pytest

from openkb import desktop_page_tree as page_tree_runtime
from openkb import desktop_page_tree_store as page_tree_store
from openkb.desktop_import import DesktopImportError, DesktopRecoveryOverride
from openkb.desktop_import_artifacts import DocumentIRBlock, SourceImage, build_evidence
from openkb.desktop_import_runner import DesktopTextImportService
from openkb.desktop_knowledge_analysis import KNOWLEDGE_ANALYSIS_SCHEMA_VERSION
from openkb.desktop_knowledge_analysis_batches import plan_knowledge_analysis_batches
from openkb.desktop_model_gateway import DesktopModelGateway
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime


def test_import_publishes_ordered_tree_and_d1_gets_its_own_generation(tmp_path) -> None:
    kb_dir = tmp_path / "knowledge"
    first_source = tmp_path / "first.md"
    second_source = tmp_path / "second.md"
    first_source.write_text("# Guide\n\nAlpha.\n\n## Detail\n\nBeta.\n", encoding="utf-8")
    second_source.write_bytes(b"# Guide\r\n\r\nAlpha.\r\n\r\n## Detail\r\n\r\nBeta.  \r\n")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    importer = DesktopTextImportService(kb_dir)

    first = importer.import_text(first_source)
    second = importer.import_text(second_source)

    assert second.job.deduplication is not None
    assert second.job.deduplication.level == "D1"
    assert second.stages[4].stage == "deterministic_page_tree"
    assert second.stages[4].status == "completed"
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        current = connection.execute(
            "SELECT document_id, generation_id FROM document_page_tree_current ORDER BY document_id"
        ).fetchall()
        nodes = connection.execute(
            "SELECT node_order, depth, kind, title FROM document_page_tree_nodes "
            "WHERE generation_id = ? ORDER BY node_order",
            (current[0][1],),
        ).fetchall()
        node_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(document_page_tree_nodes)")
        }
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert {row[0] for row in current} == {
        first.document.document_id,
        second.document.document_id,
    }
    assert len({row[1] for row in current}) == 2
    assert nodes == [
        (0, 0, "document", "Document"),
        (1, 1, "section", "Guide"),
        (2, 2, "paragraph", "Guide · Paragraph 2"),
        (3, 2, "section", "Detail"),
        (4, 3, "paragraph", "Detail · Paragraph 4"),
    ]
    assert "text" not in node_columns and "content" not in node_columns


def test_tree_retains_table_figure_locators_images_and_drives_batch_boundaries() -> None:
    blocks = (
        DocumentIRBlock("b0", 0, "heading", "Report", ("Report",), 1, 1),
        DocumentIRBlock(
            "b1",
            1,
            "table",
            "Metric | Value",
            ("Report",),
            2,
            3,
            {"sheet": "Summary", "cell_range": "A1:B2"},
        ),
        DocumentIRBlock(
            "b2",
            2,
            "figure",
            "Architecture",
            ("Report",),
            4,
            4,
            {"page": 2, "bbox": [10, 20, 30, 40], "source_image_id": "image-1"},
        ),
    )
    image = SourceImage(
        "image-1",
        0,
        "a" * 64,
        1,
        "image/png",
        "figure.png",
        ".png",
        "Architecture",
        {"page": 2, "bbox": [10, 20, 30, 40]},
        b"x",
    )
    evidence = build_evidence(blocks)

    tree = page_tree_runtime.build_deterministic_page_tree(
        "document-1", blocks, evidence, (image,), created_at="2026-08-20T00:00:00+00:00"
    )

    assert tree.nodes[2].kind == "table"
    assert tree.nodes[2].locator == {"sheet": "Summary", "cell_range": "A1:B2"}
    assert tree.nodes[3].kind == "figure"
    assert tree.nodes[3].locator["bbox"] == [10, 20, 30, 40]
    assert tree.nodes[3].source_images[0].source_image_id == "image-1"
    custom_sections = ((evidence[0],), (evidence[1], evidence[2]))
    batches = plan_knowledge_analysis_batches(
        evidence, natural_sections=custom_sections, max_evidence=2
    )
    assert [[item[0] for item in batch] for batch in batches] == [
        [evidence[0][0]],
        [evidence[1][0], evidence[2][0]],
    ]


def test_legacy_d1_rebuild_uses_its_own_ir_locator_without_evidence_checkpoint(
    tmp_path,
) -> None:
    kb_dir = tmp_path / "knowledge"
    first_source = tmp_path / "first.txt"
    second_source = tmp_path / "second.txt"
    first_source.write_bytes(b"Same body.\n")
    second_source.write_bytes(b"Same body.  \r\n")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    importer = DesktopTextImportService(kb_dir)
    importer.import_text(first_source)
    second = importer.import_text(second_source)
    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        ir_stage_id = connection.execute(
            "SELECT stage_run_id FROM stage_runs WHERE job_id = ? AND stage = 'document_ir'",
            (second.job.job_id,),
        ).fetchone()[0]
        evidence_stage_id = connection.execute(
            "SELECT stage_run_id FROM stage_runs WHERE job_id = ? AND stage = 'evidence'",
            (second.job.job_id,),
        ).fetchone()[0]
        checkpoint = json.loads(
            connection.execute(
                "SELECT checkpoint_json FROM stage_run_runtime WHERE stage_run_id = ?",
                (ir_stage_id,),
            ).fetchone()[0]
        )
        checkpoint[0]["locator"] = {"page": 99, "bbox": [1, 2, 3, 4]}
        connection.execute(
            "UPDATE stage_run_runtime SET checkpoint_json = ? WHERE stage_run_id = ?",
            (json.dumps(checkpoint), ir_stage_id),
        )
        connection.execute(
            "UPDATE stage_run_runtime SET checkpoint_json = NULL WHERE stage_run_id = ?",
            (evidence_stage_id,),
        )
        generation_id = connection.execute(
            "SELECT generation_id FROM document_page_tree_current WHERE document_id = ?",
            (second.document.document_id,),
        ).fetchone()[0]
        connection.execute(
            "DELETE FROM document_page_tree_current WHERE document_id = ?",
            (second.document.document_id,),
        )
        connection.execute(
            "DELETE FROM document_page_tree_generations WHERE generation_id = ?",
            (generation_id,),
        )
        page_tree_store.queue_page_tree_rebuild_in(
            connection,
            second.document.document_id,
            reason="schema_upgrade",
            error_code="deterministic_page_tree_failed",
        )

    page_tree_store.rebuild_pending_page_trees(kb_dir)

    with sqlite3.connect(database_path) as connection:
        locator = connection.execute(
            """
            SELECT nodes.locator_json FROM document_page_tree_current AS current
            JOIN document_page_tree_nodes AS nodes
                ON nodes.generation_id = current.generation_id
            WHERE current.document_id = ? AND nodes.node_order = 1
            """,
            (second.document.document_id,),
        ).fetchone()[0]
    assert json.loads(locator) == {"page": 99, "bbox": [1, 2, 3, 4]}


def test_tree_failure_keeps_document_available_and_persistent_rebuild_recovers(
    tmp_path, monkeypatch
) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "guide.txt"
    source.write_text("# Guide\n\nStill available.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    monkeypatch.setattr(
        page_tree_runtime,
        "build_deterministic_page_tree",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("fixture failure")),
    )
    monkeypatch.setattr(page_tree_store, "start_page_tree_rebuilds", lambda _kb_dir: None)

    result = DesktopTextImportService(kb_dir).import_text(source)

    assert result.document.availability == "available"
    assert result.stages[4].status == "skipped"
    assert result.stages[4].error_code == "deterministic_page_tree_failed"
    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT status FROM document_page_tree_rebuild_tasks WHERE document_id = ?",
            (result.document.document_id,),
        ).fetchone() == ("pending",)
        assert connection.execute("SELECT COUNT(*) FROM document_page_tree_current").fetchone() == (
            0,
        )

    page_tree_store.rebuild_pending_page_trees(kb_dir)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT status FROM document_page_tree_rebuild_tasks WHERE document_id = ?",
            (result.document.document_id,),
        ).fetchone() == ("completed",)
        assert connection.execute("SELECT COUNT(*) FROM document_page_tree_current").fetchone() == (
            1,
        )


def test_migration_backfills_stage_and_queues_available_legacy_document(tmp_path) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "legacy.txt"
    source.write_text("Legacy available document.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    imported = DesktopTextImportService(kb_dir).import_text(source)
    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        stage_id = connection.execute(
            "SELECT stage_run_id FROM stage_runs WHERE job_id = ? "
            "AND stage = 'deterministic_page_tree'",
            (imported.job.job_id,),
        ).fetchone()[0]
        connection.execute("DELETE FROM stage_run_runtime WHERE stage_run_id = ?", (stage_id,))
        connection.execute("DELETE FROM stage_runs WHERE stage_run_id = ?", (stage_id,))
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
        connection.execute("DELETE FROM schema_migrations WHERE version = 32")

    activation = DesktopKnowledgeBaseRuntime().open(kb_dir)

    assert activation.knowledge_base.schema_version == 32
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT status FROM stage_runs WHERE job_id = ? AND stage = 'deterministic_page_tree'",
            (imported.job.job_id,),
        ).fetchone() == ("skipped",)
        assert connection.execute(
            "SELECT status, reason FROM document_page_tree_rebuild_tasks WHERE document_id = ?",
            (imported.document.document_id,),
        ).fetchone() == ("pending", "schema_upgrade")
        query_plan = connection.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT jobs.job_id FROM import_jobs AS jobs
            WHERE jobs.document_id = ?
            ORDER BY jobs.completed_at DESC, jobs.created_at DESC LIMIT 1
            """,
            (imported.document.document_id,),
        ).fetchall()
        assert any("import_jobs_document_completed_idx" in str(row[3]) for row in query_plan)


def test_migration_leaves_page_tree_pending_for_a_legacy_quarantined_import(tmp_path) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "legacy-quarantined.txt"
    source.write_text("Legacy model failure with verified evidence.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)

    def timeout(*_args, **_kwargs):
        raise TimeoutError()

    with pytest.raises(DesktopImportError):
        DesktopTextImportService(
            kb_dir, model_gateway=DesktopModelGateway(timeout)
        ).import_text(source)
    importer = DesktopTextImportService(kb_dir)
    job_id = importer.list_import_jobs()["jobs"][0]["job"]["job_id"]
    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        stage_id = connection.execute(
            "SELECT stage_run_id FROM stage_runs WHERE job_id = ? "
            "AND stage = 'deterministic_page_tree'",
            (job_id,),
        ).fetchone()[0]
        connection.execute("DELETE FROM stage_run_runtime WHERE stage_run_id = ?", (stage_id,))
        connection.execute("DELETE FROM stage_runs WHERE stage_run_id = ?", (stage_id,))
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
        connection.execute("DELETE FROM schema_migrations WHERE version = 32")

    DesktopKnowledgeBaseRuntime().open(kb_dir)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT status, progress FROM stage_runs "
            "WHERE job_id = ? AND stage = 'deterministic_page_tree'",
            (job_id,),
        ).fetchone() == ("pending", 0)
    analysis = json.dumps(
        {
            "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
            "analysis_scope": "document",
            "document_description": "No durable knowledge candidates.",
            "concepts": [],
            "entities": [],
        }
    )
    recovered = DesktopTextImportService(
        kb_dir,
        model_gateway=DesktopModelGateway(lambda *_args: analysis),
    ).recover_text(job_id, DesktopRecoveryOverride())

    assert recovered.job.status == "completed"
    assert recovered.document.availability == "available"
    assert recovered.stages[4].status == "completed"


def test_rebuild_start_keeps_one_worker_per_knowledge_base(tmp_path, monkeypatch) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "queued.txt"
    source.write_text("Queue one deterministic rebuild.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    imported = DesktopTextImportService(kb_dir).import_text(source)
    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        page_tree_store.queue_page_tree_rebuild_in(
            connection,
            imported.document.document_id,
            reason="test_rebuild",
            error_code="deterministic_page_tree_failed",
        )

    deferred_threads = []

    class DeferredThread:
        def __init__(self, *, target, args, daemon, name):
            self.target = target
            self.args = args
            self.daemon = daemon
            self.name = name

        def start(self) -> None:
            deferred_threads.append(self)

    monkeypatch.setattr(page_tree_store.threading, "Thread", DeferredThread)

    page_tree_store.start_page_tree_rebuilds(kb_dir)
    page_tree_store.start_page_tree_rebuilds(kb_dir)

    assert len(deferred_threads) == 1
    deferred_threads[0].target(*deferred_threads[0].args)
    with sqlite3.connect(database_path) as connection:
        page_tree_store.queue_page_tree_rebuild_in(
            connection,
            imported.document.document_id,
            reason="test_rebuild_again",
            error_code="deterministic_page_tree_failed",
        )
    page_tree_store.start_page_tree_rebuilds(kb_dir)
    assert len(deferred_threads) == 2
    deferred_threads[1].target(*deferred_threads[1].args)
