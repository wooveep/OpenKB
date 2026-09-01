"""Migration coverage for evidence-safe Knowledge Graph interpretation metadata."""

from __future__ import annotations

import sqlite3

import openkb.desktop_workspace as workspace_module
from openkb.desktop_import import DesktopTextImportService
from openkb.desktop_model_gateway import DesktopModelGateway
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime


def test_v51_graph_migrates_additively_without_model_work_or_pointer_movement(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "openkb.desktop_import_runner.start_graph_extraction", lambda *_args, **_kw: None
    )
    model_calls: list[object] = []

    def unexpected_model_call(self, request, **kwargs):
        del self, kwargs
        model_calls.append(request)
        raise AssertionError("Schema migration must not invoke a model.")

    monkeypatch.setattr(DesktopModelGateway, "analyze", unexpected_model_call)
    kb_dir = tmp_path / "v51-kb"
    v51_migrations = tuple(
        migration for migration in workspace_module._MIGRATIONS if migration[0] <= 51
    )
    with monkeypatch.context() as v51_context:
        v51_context.setattr(workspace_module, "_MIGRATIONS", v51_migrations)
        activation = DesktopKnowledgeBaseRuntime().create(kb_dir)
        assert activation.knowledge_base.schema_version == 51

    source = tmp_path / "legacy-graph.txt"
    source.write_text("Atlas uses Gateway.", encoding="utf-8")
    document = DesktopTextImportService(kb_dir).import_text(source).document
    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        evidence_id = str(
            connection.execute(
                """
                SELECT evidence_id FROM evidence_occurrences
                WHERE document_id = ? ORDER BY ordinal LIMIT 1
                """,
                (document.document_id,),
            ).fetchone()[0]
        )
        with connection:
            connection.execute(
                """
                INSERT INTO knowledge_graph_nodes (
                    node_id, evidence_id, node_type, label, normalized_label,
                    extraction_method, created_at
                ) VALUES ('legacy-atlas', ?, 'entity', 'Atlas', 'atlas', 'model', '2026-01-01')
                """,
                (evidence_id,),
            )
            connection.execute(
                """
                INSERT INTO knowledge_graph_nodes (
                    node_id, evidence_id, node_type, label, normalized_label,
                    extraction_method, created_at
                ) VALUES (
                    'legacy-gateway', ?, 'concept', 'Gateway', 'gateway', 'model', '2026-01-01'
                )
                """,
                (evidence_id,),
            )
            connection.execute(
                """
                INSERT INTO knowledge_graph_edges (
                    edge_id, evidence_id, source_node_id, target_node_id, edge_type,
                    support_score, extraction_method, created_at
                ) VALUES (
                    'legacy-edge', ?, 'legacy-atlas', 'legacy-gateway', 'USES',
                    0.75, 'model', '2026-01-01'
                )
                """,
                (evidence_id,),
            )
            connection.execute(
                """
                INSERT INTO knowledge_graph_results (
                    result_id, document_id, status, capability_identity,
                    prompt_contract_digest, extraction_method, node_count, edge_count, created_at
                ) VALUES (
                    'legacy-result', ?, 'completed', 'legacy-capability',
                    'legacy-prompt', 'model', 2, 1, '2026-01-01'
                )
                """,
                (document.document_id,),
            )
            connection.executemany(
                """
                INSERT INTO knowledge_graph_result_nodes (result_id, node_id)
                VALUES ('legacy-result', ?)
                """,
                (("legacy-atlas",), ("legacy-gateway",)),
            )
            connection.execute(
                """
                INSERT INTO knowledge_graph_result_edges (result_id, edge_id)
                VALUES ('legacy-result', 'legacy-edge')
                """
            )
            connection.execute(
                """
                INSERT INTO knowledge_graph_current (document_id, result_id)
                VALUES (?, 'legacy-result')
                """,
                (document.document_id,),
            )
        revision_before = connection.execute(
            "SELECT revision FROM desktop_retrieval_corpus_state WHERE singleton = 1"
        ).fetchone()

    migrated = DesktopKnowledgeBaseRuntime().open(kb_dir)

    assert migrated.knowledge_base.schema_version == workspace_module._MIGRATIONS[-1][0]
    assert model_calls == []
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            """
            SELECT edge_type, relation_label, verification_state, evidence_id
            FROM knowledge_graph_edges WHERE edge_id = 'legacy-edge'
            """
        ).fetchone() == ("USES", None, "legacy_evidence_bound", evidence_id)
        assert connection.execute(
            """
            SELECT verification_state, support_start, support_end
            FROM knowledge_graph_nodes ORDER BY node_id
            """
        ).fetchall() == [
            ("legacy_evidence_bound", None, None),
            ("legacy_evidence_bound", None, None),
        ]
        assert connection.execute(
            "SELECT document_id, result_id FROM knowledge_graph_current"
        ).fetchall() == [(document.document_id, "legacy-result")]
        assert (
            connection.execute(
                "SELECT revision FROM desktop_retrieval_corpus_state WHERE singleton = 1"
            ).fetchone()
            == revision_before
        )
        assert connection.execute(
            """
            SELECT quality, retained_count, weakened_count, rejected_count,
                canonical_schema_version, normalizer_version, verification_policy_version
            FROM knowledge_graph_results WHERE result_id = 'legacy-result'
            """
        ).fetchone() == ("full", 3, 0, 0, "legacy", "legacy", "legacy")
        assert connection.execute(
            """
            SELECT lifecycle, quality, result_id, extraction_method,
                retained_count, failure_signature
            FROM knowledge_graph_attempts
            """
        ).fetchall() == [("completed", "full", "legacy-result", "model", 3, None)]
