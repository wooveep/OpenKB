"""Deterministic Document PageTree tracer-bullet behavior."""

from __future__ import annotations

import json
import sqlite3
import threading

import pytest

from openkb import desktop_page_tree as page_tree_runtime
from openkb import desktop_page_tree_store as page_tree_store
from openkb import desktop_workspace
from openkb.desktop_import import DesktopImportError, DesktopRecoveryOverride
from openkb.desktop_import_artifacts import DocumentIRBlock, SourceImage, build_evidence
from openkb.desktop_import_runner import DesktopTextImportService
from openkb.desktop_knowledge_analysis import KNOWLEDGE_ANALYSIS_SCHEMA_VERSION
from openkb.desktop_knowledge_analysis_batches import plan_knowledge_analysis_batches
from openkb.desktop_model_gateway import DesktopModelGateway
from openkb.desktop_model_result_migrations import MODEL_RESULT_OBSERVATION_COLUMNS
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime

LATEST_SCHEMA_VERSION = desktop_workspace._MIGRATIONS[-1][0]


def _drop_catalog_schema(connection: sqlite3.Connection) -> None:
    for (name,) in connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'knowledge_catalog_%'"
    ).fetchall():
        connection.execute(f'DROP TRIGGER "{name}"')
    for table in (
        "model_capability_checks",
        "knowledge_catalog_rebuild_tasks",
        "knowledge_catalog_state",
        "knowledge_catalog_links",
        "knowledge_catalog_node_sources",
        "knowledge_catalog_nodes",
        "knowledge_catalog_generations",
    ):
        connection.execute(f"DROP TABLE IF EXISTS {table}")


def _drop_post_v37_schema(connection: sqlite3.Connection) -> None:
    connection.execute("DROP VIEW IF EXISTS current_knowledge_graph_edges")
    connection.execute("DROP VIEW IF EXISTS current_knowledge_graph_nodes")
    for table in (
        "knowledge_graph_current",
        "knowledge_graph_result_edges",
        "knowledge_graph_result_nodes",
        "knowledge_graph_results",
        "knowledge_adoption_requests",
        "knowledge_origin_references",
        "model_capability_compatibility_audit",
        "model_operation_contract_states",
    ):
        connection.execute(f"DROP TABLE IF EXISTS {table}")
    for table in (
        "knowledge_graph_extraction_tasks",
        "legacy_model_recovery_audit",
        "model_usage_records",
        "knowledge_analysis_merge_nodes",
        "knowledge_reanalysis_merge_nodes",
        "knowledge_analysis_plans",
        "knowledge_reanalysis_plans",
    ):
        connection.execute(f"DROP TABLE IF EXISTS {table}")
    for table in ("model_calls", "model_attempts"):
        existing = {
            str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for column in ("lifecycle_status", "elapsed_seconds", "retry_after_seconds"):
            if column in existing:
                connection.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    for table, column, _definition in MODEL_RESULT_OBSERVATION_COLUMNS:
        if connection.execute(
            "SELECT 1 FROM pragma_table_info(?) WHERE name = ?", (table, column)
        ).fetchone():
            connection.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    connection.execute("DELETE FROM schema_migrations WHERE version >= 38")


def test_import_publishes_ordered_tree_and_d1_gets_its_own_generation(tmp_path) -> None:
    kb_dir = tmp_path / "knowledge"
    first_source = tmp_path / "first.md"
    second_source = tmp_path / "second.md"
    first_source.write_text("# Guide\n\nAlpha.\n\n## Detail\n\nBeta.\n", encoding="utf-8")
    second_source.write_bytes(b"# Guide\r\n\r\nAlpha.\r\n\r\n## Detail\r\n\r\nBeta.\r\n")
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
    current_by_document = dict(current)
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute(
            "SELECT reused_from_generation_id FROM document_page_tree_generations "
            "WHERE generation_id = ?",
            (current_by_document[second.document.document_id],),
        ).fetchone() == (current_by_document[first.document.document_id],)
    assert nodes == [
        (0, 0, "document", "Document"),
        (1, 1, "section", "Guide"),
        (2, 2, "paragraph", "Guide · Paragraph 2"),
        (3, 2, "section", "Detail"),
        (4, 3, "paragraph", "Detail · Paragraph 4"),
    ]
    assert "text" not in node_columns and "content" not in node_columns


def test_d1_with_different_line_locators_builds_an_independent_generation(tmp_path) -> None:
    kb_dir = tmp_path / "knowledge"
    first_source = tmp_path / "first.txt"
    second_source = tmp_path / "second.txt"
    first_source.write_text("# Guide\n\nSame fact.\n", encoding="utf-8")
    second_source.write_text("\n\n# Guide\n\nSame fact.\n", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    importer = DesktopTextImportService(kb_dir)
    first = importer.import_text(first_source)
    second = importer.import_text(second_source)

    assert second.job.deduplication is not None
    assert second.job.deduplication.level == "D1"
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        rows = connection.execute(
            """
            SELECT generations.document_id, generations.structural_ir_fingerprint,
                generations.locator_mapping_digest, generations.reused_from_generation_id
            FROM document_page_tree_current AS current
            JOIN document_page_tree_generations AS generations
                ON generations.generation_id = current.generation_id
            WHERE generations.document_id IN (?, ?)
            ORDER BY generations.document_id
            """,
            (first.document.document_id, second.document.document_id),
        ).fetchall()
    assert len(rows) == 2
    assert rows[0][1] == rows[1][1]
    assert rows[0][2] != rows[1][2]
    assert all(row[3] is None for row in rows)


def test_d1_image_ids_are_canonicalized_for_locator_reuse(tmp_path) -> None:
    kb_dir = tmp_path / "knowledge"
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    image_bytes = b"\x89PNG\r\n\x1a\nsame-source-image"
    (first_dir / "image.png").write_bytes(image_bytes)
    (second_dir / "image.png").write_bytes(image_bytes)
    first_source = first_dir / "doc.md"
    second_source = second_dir / "doc.md"
    first_source.write_text("# Guide\n\n![Diagram](image.png)\n", encoding="utf-8")
    second_source.write_bytes(b"# Guide\r\n\r\n![Diagram](image.png)\r\n")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    importer = DesktopTextImportService(kb_dir)

    first = importer.import_text(first_source)
    second = importer.import_text(second_source)

    assert second.job.deduplication is not None
    assert second.job.deduplication.level == "D1"
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        rows = connection.execute(
            """
            SELECT generations.document_id, generations.locator_mapping_digest,
                generations.reused_from_generation_id, generations.generation_id
            FROM document_page_tree_current AS current
            JOIN document_page_tree_generations AS generations
                ON generations.generation_id = current.generation_id
            WHERE generations.document_id IN (?, ?)
            """,
            (first.document.document_id, second.document.document_id),
        ).fetchall()
    by_document = {str(row[0]): row for row in rows}
    assert by_document[first.document.document_id][1] == by_document[second.document.document_id][1]
    assert by_document[second.document.document_id][2] == by_document[first.document.document_id][3]


def test_legacy_image_locator_generation_defers_concurrent_d1_reuse(tmp_path, monkeypatch) -> None:
    kb_dir = tmp_path / "knowledge"
    first_dir = tmp_path / "legacy"
    second_dir = tmp_path / "duplicate"
    first_dir.mkdir()
    second_dir.mkdir()
    image_bytes = b"\x89PNG\r\n\x1a\nlegacy-source-image"
    (first_dir / "image.png").write_bytes(image_bytes)
    (second_dir / "image.png").write_bytes(image_bytes)
    first_source = first_dir / "doc.md"
    second_source = second_dir / "doc.md"
    first_source.write_text("# Guide\n\n![Diagram](image.png)\n", encoding="utf-8")
    second_source.write_bytes(b"# Guide\r\n\r\n![Diagram](image.png)\r\n")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    importer = DesktopTextImportService(kb_dir)
    first = importer.import_text(first_source)
    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        current_generation_id = connection.execute(
            "SELECT generation_id FROM document_page_tree_current WHERE document_id = ?",
            (first.document.document_id,),
        ).fetchone()[0]
        legacy_generation_id = "legacy-image-id-generation"
        for table in (
            "document_page_tree_node_evidence",
            "document_page_tree_node_images",
            "document_page_tree_nodes",
        ):
            connection.execute(
                f"UPDATE {table} SET generation_id = ? WHERE generation_id = ?",
                (legacy_generation_id, current_generation_id),
            )
        connection.execute(
            "UPDATE document_page_tree_current SET generation_id = ? WHERE document_id = ?",
            (legacy_generation_id, first.document.document_id),
        )
        connection.execute(
            """
            UPDATE document_page_tree_generations
            SET generation_id = ?, provider_version = '1',
                locator_mapping_digest = 'legacy-image-id-digest'
            WHERE generation_id = ?
            """,
            (legacy_generation_id, current_generation_id),
        )

    page_tree_store._ensure_page_tree_rebuilds(kb_dir)
    monkeypatch.setattr(page_tree_store, "start_page_tree_rebuilds", lambda _kb_dir: None)
    second = importer.import_text(second_source)

    assert second.job.deduplication is not None
    assert second.job.deduplication.level == "D1"
    with sqlite3.connect(database_path) as connection:
        tasks = dict(
            connection.execute(
                "SELECT document_id, status FROM document_page_tree_rebuild_tasks "
                "WHERE document_id IN (?, ?)",
                (first.document.document_id, second.document.document_id),
            ).fetchall()
        )
        assert tasks == {
            first.document.document_id: "pending",
            second.document.document_id: "pending",
        }
        assert (
            connection.execute(
                "SELECT 1 FROM document_page_tree_current WHERE document_id = ?",
                (second.document.document_id,),
            ).fetchone()
            is None
        )
    page_tree_store.rebuild_pending_page_trees(kb_dir)

    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT generations.document_id, generations.generation_id,
                generations.provider_version, generations.locator_mapping_digest,
                generations.reused_from_generation_id
            FROM document_page_tree_current AS current
            JOIN document_page_tree_generations AS generations
                ON generations.generation_id = current.generation_id
            WHERE generations.document_id IN (?, ?)
            """,
            (first.document.document_id, second.document.document_id),
        ).fetchall()
    by_document = {str(row[0]): row for row in rows}
    assert by_document[first.document.document_id][2] == "2"
    assert by_document[second.document.document_id][2] == "2"
    assert by_document[first.document.document_id][3] == by_document[second.document.document_id][3]
    assert by_document[second.document.document_id][4] == by_document[first.document.document_id][1]


def test_queued_d1_rebuild_applies_the_same_reuse_lineage(tmp_path, monkeypatch) -> None:
    kb_dir = tmp_path / "knowledge"
    first_source = tmp_path / "first.md"
    second_source = tmp_path / "second.md"
    first_source.write_text("# Guide\n\nSame fact.\n", encoding="utf-8")
    second_source.write_bytes(b"# Guide\r\n\r\nSame fact.\r\n")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    importer = DesktopTextImportService(kb_dir)
    first = importer.import_text(first_source)
    monkeypatch.setattr(
        page_tree_runtime,
        "build_deterministic_page_tree",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("transient failure")),
    )
    monkeypatch.setattr(page_tree_store, "start_page_tree_rebuilds", lambda _kb_dir: None)

    second = importer.import_text(second_source)
    page_tree_store.rebuild_pending_page_trees(kb_dir)

    assert second.job.deduplication is not None
    assert second.job.deduplication.level == "D1"
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        first_generation = connection.execute(
            "SELECT generation_id FROM document_page_tree_current WHERE document_id = ?",
            (first.document.document_id,),
        ).fetchone()[0]
        second_lineage = connection.execute(
            """
            SELECT generations.reused_from_generation_id
            FROM document_page_tree_current AS current
            JOIN document_page_tree_generations AS generations
                ON generations.generation_id = current.generation_id
            WHERE current.document_id = ?
            """,
            (second.document.document_id,),
        ).fetchone()[0]
    assert second_lineage == first_generation


def test_queued_d1_rebuild_orders_canonical_target_before_its_alias(tmp_path) -> None:
    kb_dir = tmp_path / "knowledge"
    first_source = tmp_path / "canonical.md"
    second_source = tmp_path / "alias.md"
    first_source.write_text("# Guide\n\nSame fact.\n", encoding="utf-8")
    second_source.write_bytes(b"# Guide\r\n\r\nSame fact.\r\n")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    importer = DesktopTextImportService(kb_dir)
    first = importer.import_text(first_source)
    second = importer.import_text(second_source)
    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        page_tree_store.queue_page_tree_rebuild_in(
            connection,
            second.document.document_id,
            reason="provider_update",
            error_code="deterministic_page_tree_failed",
            provider_version="3",
        )
        page_tree_store.queue_page_tree_rebuild_in(
            connection,
            first.document.document_id,
            reason="provider_update",
            error_code="deterministic_page_tree_failed",
            provider_version="3",
        )
        connection.execute(
            "UPDATE document_page_tree_rebuild_tasks SET updated_at = '1' WHERE document_id = ?",
            (second.document.document_id,),
        )
        connection.execute(
            "UPDATE document_page_tree_rebuild_tasks SET updated_at = '2' WHERE document_id = ?",
            (first.document.document_id,),
        )

    page_tree_store.rebuild_pending_page_trees(kb_dir)

    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT generations.document_id, generations.generation_id,
                generations.provider_version, generations.reused_from_generation_id
            FROM document_page_tree_current AS current
            JOIN document_page_tree_generations AS generations
                ON generations.generation_id = current.generation_id
            WHERE generations.document_id IN (?, ?)
            """,
            (first.document.document_id, second.document.document_id),
        ).fetchall()
    by_document = {str(row[0]): row for row in rows}
    assert by_document[first.document.document_id][2] == "3"
    assert by_document[second.document.document_id][2] == "3"
    assert by_document[second.document.document_id][3] == by_document[first.document.document_id][1]


def test_d1_rebuild_waits_for_canonical_recovery_before_publishing(tmp_path, monkeypatch) -> None:
    kb_dir = tmp_path / "knowledge"
    first_source = tmp_path / "canonical-retry.md"
    second_source = tmp_path / "alias-retry.md"
    first_source.write_text("# Guide\n\nSame retry fact.\n", encoding="utf-8")
    second_source.write_bytes(b"# Guide\r\n\r\nSame retry fact.\r\n")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    importer = DesktopTextImportService(kb_dir)
    first = importer.import_text(first_source)
    second = importer.import_text(second_source)
    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        page_tree_store.queue_page_tree_rebuild_in(
            connection,
            second.document.document_id,
            reason="provider_update",
            error_code="deterministic_page_tree_failed",
            provider_version="3",
        )
        page_tree_store.queue_page_tree_rebuild_in(
            connection,
            first.document.document_id,
            reason="provider_update",
            error_code="deterministic_page_tree_failed",
            provider_version="3",
        )

    original_builder = page_tree_store.build_deterministic_page_tree
    failed_once = False

    def transient_canonical_failure(document_version_id, *args, **kwargs):
        nonlocal failed_once
        if document_version_id == first.document.document_id and not failed_once:
            failed_once = True
            raise ValueError("canonical provider failed")
        return original_builder(document_version_id, *args, **kwargs)

    monkeypatch.setattr(
        page_tree_store, "build_deterministic_page_tree", transient_canonical_failure
    )
    page_tree_store.rebuild_pending_page_trees(kb_dir)

    with sqlite3.connect(database_path) as connection:
        tasks = dict(
            connection.execute(
                "SELECT document_id, status FROM document_page_tree_rebuild_tasks "
                "WHERE document_id IN (?, ?)",
                (first.document.document_id, second.document.document_id),
            ).fetchall()
        )
        versions = dict(
            connection.execute(
                """
                SELECT generations.document_id, generations.provider_version
                FROM document_page_tree_current AS current
                JOIN document_page_tree_generations AS generations
                    ON generations.generation_id = current.generation_id
                WHERE generations.document_id IN (?, ?)
                """,
                (first.document.document_id, second.document.document_id),
            ).fetchall()
        )
    assert tasks == {
        first.document.document_id: "failed",
        second.document.document_id: "pending",
    }
    assert versions == {
        first.document.document_id: "2",
        second.document.document_id: "2",
    }

    page_tree_store.rebuild_pending_page_trees(kb_dir)

    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT generations.document_id, generations.generation_id,
                generations.provider_version, generations.reused_from_generation_id,
                tasks.status
            FROM document_page_tree_current AS current
            JOIN document_page_tree_generations AS generations
                ON generations.generation_id = current.generation_id
            JOIN document_page_tree_rebuild_tasks AS tasks
                ON tasks.document_id = current.document_id
            WHERE generations.document_id IN (?, ?)
            """,
            (first.document.document_id, second.document.document_id),
        ).fetchall()
    by_document = {str(row[0]): row for row in rows}
    assert by_document[first.document.document_id][2:] == ("3", None, "completed")
    assert by_document[second.document.document_id][2] == "3"
    assert by_document[second.document.document_id][3] == by_document[first.document.document_id][1]
    assert by_document[second.document.document_id][4] == "completed"


def test_same_provider_queue_does_not_invalidate_a_running_rebuild_claim(tmp_path) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "running.txt"
    source.write_text("Keep the current rebuild claim.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    imported = DesktopTextImportService(kb_dir).import_text(source)
    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        page_tree_store.queue_page_tree_rebuild_in(
            connection,
            imported.document.document_id,
            reason="provider_update",
            error_code="deterministic_page_tree_failed",
            provider_version="3",
        )
    claim = page_tree_store.claim_page_tree_rebuild(
        kb_dir / ".openkb", database_path, imported.document.document_id
    )
    assert claim is not None

    with sqlite3.connect(database_path) as connection:
        page_tree_store.queue_page_tree_rebuild_in(
            connection,
            imported.document.document_id,
            reason="provider_update",
            error_code="deterministic_page_tree_failed",
            provider_version="3",
        )
        assert connection.execute(
            "SELECT status, attempt_count, requested_provider_version "
            "FROM document_page_tree_rebuild_tasks WHERE document_id = ?",
            (imported.document.document_id,),
        ).fetchone() == ("running", 1, "3")


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
        _drop_catalog_schema(connection)
        _drop_post_v37_schema(connection)
        stage_id = connection.execute(
            "SELECT stage_run_id FROM stage_runs WHERE job_id = ? "
            "AND stage = 'deterministic_page_tree'",
            (imported.job.job_id,),
        ).fetchone()[0]
        connection.execute("DELETE FROM stage_run_runtime WHERE stage_run_id = ?", (stage_id,))
        connection.execute("DELETE FROM stage_runs WHERE stage_run_id = ?", (stage_id,))
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
            connection.execute(f"DROP TABLE {table}")
        connection.execute("DROP INDEX import_jobs_document_completed_idx")
        connection.execute("DROP TABLE grounded_answer_retrieval_traces")
        connection.execute("DROP TABLE conversation_answer_retrieval_traces")
        connection.execute(
            "DELETE FROM schema_migrations WHERE version IN (32, 33, 34, 35, 36, 37)"
        )

    activation = DesktopKnowledgeBaseRuntime().open(kb_dir)

    assert activation.knowledge_base.schema_version == LATEST_SCHEMA_VERSION
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT status FROM stage_runs WHERE job_id = ? AND stage = 'deterministic_page_tree'",
            (imported.job.job_id,),
        ).fetchone() == ("skipped",)
        assert connection.execute(
            "SELECT status, reason FROM document_page_tree_rebuild_tasks WHERE document_id = ?",
            (imported.document.document_id,),
        ).fetchone() == ("pending", "schema_upgrade")
        assert connection.execute(
            "SELECT requested_provider_kind, requested_provider_version "
            "FROM document_page_tree_rebuild_tasks WHERE document_id = ?",
            (imported.document.document_id,),
        ).fetchone() == ("openkb_deterministic", "1")
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
        DesktopTextImportService(kb_dir, model_gateway=DesktopModelGateway(timeout)).import_text(
            source
        )
    importer = DesktopTextImportService(kb_dir)
    job_id = importer.list_import_jobs()["jobs"][0]["job"]["job_id"]
    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        _drop_catalog_schema(connection)
        _drop_post_v37_schema(connection)
        stage_id = connection.execute(
            "SELECT stage_run_id FROM stage_runs WHERE job_id = ? "
            "AND stage = 'deterministic_page_tree'",
            (job_id,),
        ).fetchone()[0]
        connection.execute("DELETE FROM stage_run_runtime WHERE stage_run_id = ?", (stage_id,))
        connection.execute("DELETE FROM stage_runs WHERE stage_run_id = ?", (stage_id,))
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
            connection.execute(f"DROP TABLE {table}")
        connection.execute("DROP INDEX import_jobs_document_completed_idx")
        connection.execute("DROP TABLE grounded_answer_retrieval_traces")
        connection.execute("DROP TABLE conversation_answer_retrieval_traces")
        connection.execute(
            "DELETE FROM schema_migrations WHERE version IN (32, 33, 34, 35, 36, 37)"
        )

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


def test_rebuild_keeps_current_until_switch_and_cleans_after_active_lease(
    tmp_path, monkeypatch
) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "lifecycle.txt"
    source.write_text("# Lifecycle\n\nCurrent tree remains usable.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    imported = DesktopTextImportService(kb_dir).import_text(source)
    document_id = imported.document.document_id
    database_path = kb_dir / ".openkb" / "state.sqlite3"

    with page_tree_store.lease_current_page_tree(kb_dir, document_id) as leased:
        assert leased is not None
        first_generation_id = leased.generation_id
        with sqlite3.connect(database_path) as connection:
            page_tree_store.queue_page_tree_rebuild_in(
                connection,
                document_id,
                reason="provider_update",
                error_code="deterministic_page_tree_failed",
                provider_version="3",
            )
        page_tree_store.rebuild_pending_page_trees(kb_dir)
        with sqlite3.connect(database_path) as connection:
            current = connection.execute(
                """
                SELECT generations.generation_id, generations.provider_version
                FROM document_page_tree_current AS current
                JOIN document_page_tree_generations AS generations
                    ON generations.generation_id = current.generation_id
                WHERE current.document_id = ?
                """,
                (document_id,),
            ).fetchone()
            assert current[0] != first_generation_id
            assert current[1] == "3"
            assert connection.execute(
                "SELECT status FROM document_page_tree_generations WHERE generation_id = ?",
                (first_generation_id,),
            ).fetchone() == ("superseded",)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM document_page_tree_generations WHERE document_id = ?",
            (document_id,),
        ).fetchone() == (1,)
        second_generation_id = connection.execute(
            "SELECT generation_id FROM document_page_tree_current WHERE document_id = ?",
            (document_id,),
        ).fetchone()[0]
        page_tree_store.queue_page_tree_rebuild_in(
            connection,
            document_id,
            reason="provider_cache_missing",
            error_code="deterministic_page_tree_failed",
            provider_version="3",
        )
        connection.execute(
            "UPDATE document_page_tree_rebuild_tasks SET status = 'running' WHERE document_id = ?",
            (document_id,),
        )

    original_builder = page_tree_store.build_deterministic_page_tree
    monkeypatch.setattr(
        page_tree_store,
        "build_deterministic_page_tree",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("provider cache missing")),
    )
    page_tree_store.rebuild_pending_page_trees(kb_dir)
    task_projection = DesktopTextImportService(kb_dir).list_import_jobs()["page_tree_rebuilds"][0]
    assert task_projection["status"] == "failed"
    assert task_projection["current_generation_id"] == second_generation_id
    assert task_projection["attempt_count"] == 1
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT availability FROM source_documents WHERE document_id = ?",
            (document_id,),
        ).fetchone() == ("available",)

    monkeypatch.setattr(page_tree_store, "build_deterministic_page_tree", original_builder)
    page_tree_store.rebuild_pending_page_trees(kb_dir)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            """
            SELECT generations.provider_version
            FROM document_page_tree_current AS current
            JOIN document_page_tree_generations AS generations
                ON generations.generation_id = current.generation_id
            WHERE current.document_id = ?
            """,
            (document_id,),
        ).fetchone() == ("3",)
        assert connection.execute(
            "SELECT status FROM document_page_tree_rebuild_tasks WHERE document_id = ?",
            (document_id,),
        ).fetchone() == ("completed",)
        assert connection.execute(
            "SELECT attempt_count FROM document_page_tree_rebuild_tasks WHERE document_id = ?",
            (document_id,),
        ).fetchone() == (2,)


def test_current_tree_can_be_leased_while_provider_build_is_blocked(tmp_path, monkeypatch) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "concurrent-rebuild.txt"
    source.write_text("# Current\n\nKeep serving this tree.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    imported = DesktopTextImportService(kb_dir).import_text(source)
    document_id = imported.document.document_id
    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        page_tree_store.queue_page_tree_rebuild_in(
            connection,
            document_id,
            reason="provider_update",
            error_code="deterministic_page_tree_failed",
            provider_version="3",
        )

    entered = threading.Event()
    release = threading.Event()
    original_builder = page_tree_store.build_deterministic_page_tree

    def blocked_builder(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original_builder(*args, **kwargs)

    monkeypatch.setattr(page_tree_store, "build_deterministic_page_tree", blocked_builder)
    worker = threading.Thread(target=page_tree_store.rebuild_pending_page_trees, args=(kb_dir,))
    worker.start()
    assert entered.wait(2)
    try:
        with page_tree_store.lease_current_page_tree(kb_dir, document_id) as current:
            assert current is not None
            assert current.provider_version == "2"
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()


def test_start_queues_and_rebuilds_a_provider_version_update(tmp_path, monkeypatch) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "provider-update.txt"
    source.write_text("# Provider update\n\nRebuild this tree.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    imported = DesktopTextImportService(kb_dir).import_text(source)
    database_path = kb_dir / ".openkb" / "state.sqlite3"
    deferred_threads = []

    class DeferredThread:
        def __init__(self, *, target, args, daemon, name):
            self.target = target
            self.args = args
            self.daemon = daemon
            self.name = name

        def start(self) -> None:
            deferred_threads.append(self)

    monkeypatch.setattr(page_tree_store, "DETERMINISTIC_PROVIDER_VERSION", "3")
    monkeypatch.setattr(page_tree_store.threading, "Thread", DeferredThread)

    page_tree_store.start_page_tree_rebuilds(kb_dir)

    assert len(deferred_threads) == 1
    deferred_threads[0].target(*deferred_threads[0].args)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            """
            SELECT generations.provider_version, tasks.status, tasks.reason
            FROM document_page_tree_current AS current
            JOIN document_page_tree_generations AS generations
                ON generations.generation_id = current.generation_id
            JOIN document_page_tree_rebuild_tasks AS tasks
                ON tasks.document_id = current.document_id
            WHERE current.document_id = ?
            """,
            (imported.document.document_id,),
        ).fetchone() == ("3", "completed", "provider_update")


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
