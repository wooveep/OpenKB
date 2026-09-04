"""Focused acceptance checks for the Desktop local Knowledge Graph channel."""

from __future__ import annotations

import io
import json
import sqlite3
import threading
import time
import zipfile
from contextlib import contextmanager

import pytest

import openkb.desktop_retrieval as retrieval
from openkb import desktop_engine_knowledge_graph as graph_engine
from openkb import desktop_model_transport
from openkb.desktop_answer_types import DesktopEvidenceRef
from openkb.desktop_diagnostic_bundle import DesktopDiagnosticBundleService
from openkb.desktop_engine import DesktopEngineServer, DesktopRequest
from openkb.desktop_graph_feature_flags import (
    desktop_knowledge_snapshot_digest,
    desktop_knowledge_snapshot_revision,
    enable_local_graph_after_evaluation,
    local_graph_default_enabled,
)
from openkb.desktop_import import DesktopTextImportService
from openkb.desktop_knowledge_graph import (
    DesktopKnowledgeGraphQueryError,
    DesktopKnowledgeGraphService,
    PinnedGraphGenerations,
    _EvidenceInput,
    _model_input,
    _model_payload_from_text,
    local_graph_evidence_ids,
)
from openkb.desktop_knowledge_graph_store import (
    GraphNode,
    GraphPayload,
    persist_graph_payload_in,
)
from openkb.desktop_knowledge_graph_tasks import DesktopKnowledgeGraphExtractionTasks
from openkb.desktop_model_capability_store import DesktopModelCapabilityStore
from openkb.desktop_model_gateway import (
    DesktopModelCancelledError,
    DesktopModelGateway,
    DesktopModelResult,
)
from openkb.desktop_model_operation_state import (
    DesktopModelOperationContractStore,
    authorize_model_operation_retry_in,
)
from openkb.desktop_model_result_failure import authorize_model_operation_retry
from openkb.desktop_model_settings import save_desktop_model_settings
from openkb.desktop_model_terminal import DesktopTerminalModelEvent
from openkb.desktop_model_usage import DesktopModelUsageStore
from openkb.desktop_prompt_contracts import prompt_contract_for
from openkb.desktop_retrieval import DesktopEvidenceRetriever
from openkb.desktop_retrieval_fusion import RetrievalCandidate, with_graph_budget
from openkb.desktop_structured_output import structured_output_repair_contract_digest
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime, desktop_state_dir
from openkb.locks import kb_ingest_lock


def test_graph_validator_rejects_evidence_omitted_from_the_bounded_prompt():
    evidence = tuple(
        _EvidenceInput(
            evidence_id=f"evidence-{index}",
            text="x" * 1_200,
            document_name="large.md",
            section=f"Section {index}",
        )
        for index in range(12)
    )
    source_material, included = _model_input(evidence)
    prompt_ids = {item["evidence_id"] for item in json.loads(source_material)["evidence"]}
    omitted = evidence[-1].evidence_id

    assert len(included) == 10
    assert omitted not in prompt_ids
    with pytest.raises(
        ValueError,
        match=r"unknown_evidence at nodes\[0\]\.evidence_id",
    ):
        _model_payload_from_text(
            json.dumps(
                {
                    "nodes": [
                        {
                            "id": "omitted-node",
                            "evidence_id": omitted,
                            "type": "concept",
                            "label": "Invisible evidence",
                            "support_quote": "x",
                        }
                    ],
                    "edges": [],
                }
            ),
            included,
        )


def test_legacy_graph_lookup_reads_the_snapshot_result_after_current_advances(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "openkb.desktop_import_runner.start_graph_extraction", lambda *_args, **_kwargs: None
    )
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "snapshot.txt"
    source.write_text("Pinned graph evidence remains available.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    document = DesktopTextImportService(kb_dir).import_text(source).document
    database_path = kb_dir / ".openkb" / "state.sqlite3"

    with sqlite3.connect(database_path) as connection:
        evidence_id = str(connection.execute("SELECT evidence_id FROM evidence_refs").fetchone()[0])
        assert persist_graph_payload_in(
            connection,
            document.document_id,
            GraphPayload(
                nodes=(
                    GraphNode(
                        "pinned",
                        evidence_id,
                        "concept",
                        "Pinned Graph Concept",
                        "deterministic",
                    ),
                ),
                edges=(),
            ),
            capability_identity=None,
            prompt_contract_digest=None,
            extraction_method="deterministic",
        )
        pinned_result_id = str(
            connection.execute(
                "SELECT result_id FROM knowledge_graph_current WHERE document_id = ?",
                (document.document_id,),
            ).fetchone()[0]
        )
        assert persist_graph_payload_in(
            connection,
            document.document_id,
            GraphPayload(
                nodes=(
                    GraphNode(
                        "replacement",
                        evidence_id,
                        "concept",
                        "Replacement Graph Concept",
                        "deterministic",
                    ),
                ),
                edges=(),
            ),
            capability_identity=None,
            prompt_contract_digest=None,
            extraction_method="deterministic",
        )
        pinned = local_graph_evidence_ids(
            connection,
            terms=("Pinned Graph Concept",),
            anchor_evidence_ids=(),
            generation_snapshot=PinnedGraphGenerations(None, (pinned_result_id,)),
        )
        current = local_graph_evidence_ids(
            connection,
            terms=("Pinned Graph Concept",),
            anchor_evidence_ids=(),
        )

    assert pinned == (evidence_id,)
    assert current == ()


def test_graph_validator_cannot_anchor_a_quote_outside_provider_visible_evidence():
    evidence = (
        _EvidenceInput(
            evidence_id="evidence-1",
            text=("A" * 1_200) + " hidden-tail",
            document_name="large.md",
            section="Long section",
        ),
    )
    _source_material, included = _model_input(evidence)

    with pytest.raises(
        ValueError,
        match=r"support_quote_not_found at nodes\[0\]\.support_quote",
    ):
        _model_payload_from_text(
            json.dumps(
                {
                    "nodes": [
                        {
                            "id": "hidden",
                            "evidence_id": "evidence-1",
                            "type": "concept",
                            "label": "Hidden",
                            "support_quote": "hidden-tail",
                        }
                    ],
                    "edges": [],
                }
            ),
            included,
        )


def test_empty_model_graph_is_a_success_without_fabricated_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "openkb.desktop_import_runner.start_graph_extraction", lambda *_args, **_kw: None
    )
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "empty-graph.txt"
    source.write_text("This source has no graphable relationship.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    document = DesktopTextImportService(kb_dir).import_text(source).document

    assert DesktopKnowledgeGraphService(
        kb_dir,
        model_gateway=DesktopModelGateway(
            lambda _request, _timeout_seconds: json.dumps({"nodes": [], "edges": []})
        ),
    ).extract_document(document.document_id)

    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM knowledge_graph_nodes").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM knowledge_graph_edges").fetchone() == (0,)


def test_supported_natural_relationship_publishes_as_related_to(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "openkb.desktop_import_runner.start_graph_extraction", lambda *_args, **_kw: None
    )
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "natural-relation.txt"
    source.write_text("Atlas is operated by Cloudyi.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    document = DesktopTextImportService(kb_dir).import_text(source).document

    def graph_response(request, _timeout_seconds):
        [evidence] = json.loads(request.content)["evidence"]
        return json.dumps(
            {
                "nodes": [
                    {
                        "id": "atlas",
                        "evidence_id": evidence["evidence_id"],
                        "type": "entity",
                        "label": "Atlas",
                        "support_quote": "Atlas",
                    },
                    {
                        "id": "cloudyi",
                        "evidence_id": evidence["evidence_id"],
                        "type": "entity",
                        "label": "Cloudyi",
                        "support_quote": "Cloudyi",
                    },
                ],
                "edges": [
                    {
                        "evidence_id": evidence["evidence_id"],
                        "source_id": "atlas",
                        "target_id": "cloudyi",
                        "type": "OPERATED_BY",
                        "support_quote": evidence["text"],
                    }
                ],
            }
        )

    assert DesktopKnowledgeGraphService(
        kb_dir,
        model_gateway=DesktopModelGateway(graph_response),
    ).extract_document(document.document_id)

    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute(
            """
            SELECT edge_type, relation_label, support_start, support_end, verification_state
            FROM knowledge_graph_edges
            """
        ).fetchall() == [("RELATED_TO", "OPERATED_BY", 0, 29, "ambiguous")]
        result = connection.execute(
            """
            SELECT quality, retained_count, weakened_count, rejected_count,
                canonical_schema_version, normalizer_version, verification_policy_version
            FROM knowledge_graph_results
            """
        ).fetchone()
        assert result == (
            "degraded",
            3,
            1,
            0,
            "openkb.graph-schema.v2",
            "openkb.graph-normalizer.v1",
            "openkb.graph-verification.v1",
        )
        assert connection.execute(
            """
            SELECT code, contract_path, disposition, failure_class
            FROM knowledge_graph_attempt_issues
            """
        ).fetchall() == [("unsupported_relationship", "edges[0].type", "weakened", "semantic")]
        assert "support_quote" not in {
            str(row[1]) for row in connection.execute("PRAGMA table_info(knowledge_graph_edges)")
        }

    diagnostic_path = tmp_path / "graph-diagnostics.zip"
    DesktopDiagnosticBundleService(kb_dir).export(diagnostic_path)
    with zipfile.ZipFile(diagnostic_path) as archive:
        diagnostic_payload = json.loads(archive.read("graph-diagnostics.json"))
    assert diagnostic_payload["results"][0]["quality"] == "degraded"
    assert diagnostic_payload["attempts"][0]["weakened_count"] == 1
    assert diagnostic_payload["attempt_issues"] == [
        {
            "attempt_id": diagnostic_payload["attempts"][0]["attempt_id"],
            "document_id": document.document_id,
            "ordinal": 0,
            "code": "unsupported_relationship",
            "contract_path": "edges[0].type",
            "disposition": "weakened",
            "failure_class": "semantic",
        }
    ]
    assert "OPERATED_BY" not in json.dumps(diagnostic_payload)


def test_degraded_graph_quality_and_dispositions_are_projected_to_the_task(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        "openkb.desktop_import_runner.start_graph_extraction", lambda *_args, **_kw: None
    )
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "degraded-task.txt"
    source.write_text("Atlas leads Gateway.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    document = DesktopTextImportService(kb_dir).import_text(source).document

    def graph_response(request, _timeout_seconds):
        [evidence] = json.loads(request.content)["evidence"]
        return json.dumps(
            {
                "nodes": [
                    {
                        "id": "atlas",
                        "evidence_id": evidence["evidence_id"],
                        "type": "entity",
                        "label": "Atlas",
                        "support_quote": "Atlas",
                    },
                    {
                        "id": "gateway",
                        "evidence_id": evidence["evidence_id"],
                        "type": "concept",
                        "label": "Gateway",
                        "support_quote": "Gateway",
                    },
                ],
                "edges": [
                    {
                        "evidence_id": evidence["evidence_id"],
                        "source_id": "atlas",
                        "target_id": "gateway",
                        "type": "LEADS",
                        "support_quote": evidence["text"],
                    }
                ],
            }
        )

    gateway = DesktopModelGateway(graph_response)
    tasks = DesktopKnowledgeGraphExtractionTasks(kb_dir)
    assert tasks.queue(document.document_id, gateway)
    assert tasks.run_document(document.document_id, gateway, should_stop=lambda: False)

    [task] = DesktopTextImportService(kb_dir).list_import_jobs()["knowledge_graph_extractions"]
    assert task["status"] == "completed"
    assert task["quality"] == "degraded"
    assert task["retained_count"] == 3
    assert task["weakened_count"] == 1
    assert task["rejected_count"] == 0
    assert tasks.retry(document.document_id, gateway)
    [retried] = DesktopTextImportService(kb_dir).list_import_jobs()["knowledge_graph_extractions"]
    assert retried["status"] == "pending"
    assert retried["reason"] == "explicit_retry"


def test_compatible_degraded_retry_is_retained_without_displacing_current_full_graph(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "openkb.desktop_import_runner.start_graph_extraction", lambda *_args, **_kw: None
    )
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "promotion.txt"
    source.write_text("Atlas uses Gateway.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    document = DesktopTextImportService(kb_dir).import_text(source).document
    relationship = "USES"

    def graph_response(request, _timeout_seconds):
        [evidence] = json.loads(request.content)["evidence"]
        return json.dumps(
            {
                "nodes": [
                    {
                        "id": "atlas",
                        "evidence_id": evidence["evidence_id"],
                        "type": "entity",
                        "label": "Atlas",
                        "support_quote": "Atlas",
                    },
                    {
                        "id": "gateway",
                        "evidence_id": evidence["evidence_id"],
                        "type": "concept",
                        "label": "Gateway",
                        "support_quote": "Gateway",
                    },
                ],
                "edges": [
                    {
                        "evidence_id": evidence["evidence_id"],
                        "source_id": "atlas",
                        "target_id": "gateway",
                        "type": relationship,
                        "support_quote": evidence["text"],
                    }
                ],
            }
        )

    service = DesktopKnowledgeGraphService(
        kb_dir, model_gateway=DesktopModelGateway(graph_response)
    )
    assert service.extract_document(document.document_id)
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        full_result = str(
            connection.execute(
                "SELECT result_id FROM knowledge_graph_current WHERE document_id = ?",
                (document.document_id,),
            ).fetchone()[0]
        )

    relationship = "LEADS"
    assert service.extract_document(document.document_id)

    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute(
            "SELECT result_id FROM knowledge_graph_current WHERE document_id = ?",
            (document.document_id,),
        ).fetchone() == (full_result,)
        results = connection.execute(
            """
            SELECT quality, canonical_schema_version, normalizer_version,
                verification_policy_version
            FROM knowledge_graph_results WHERE document_id = ?
            """,
            (document.document_id,),
        ).fetchall()
        attempts = connection.execute(
            """
            SELECT quality, weakened_count FROM knowledge_graph_attempts
            WHERE document_id = ?
            """,
            (document.document_id,),
        ).fetchall()
    assert {str(row[0]) for row in results} == {"full", "degraded"}
    assert {tuple(row[1:]) for row in results} == {
        (
            "openkb.graph-schema.v2",
            "openkb.graph-normalizer.v1",
            "openkb.graph-verification.v1",
        )
    }
    assert sorted(attempts) == [("degraded", 1), ("full", 0)]


def test_empty_model_graph_is_projected_as_completed_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "openkb.desktop_import_runner.start_graph_extraction", lambda *_args, **_kw: None
    )
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "empty-graph-task.txt"
    source.write_text("This source also has no graphable relationship.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    document = DesktopTextImportService(kb_dir).import_text(source).document
    gateway = DesktopModelGateway(
        lambda _request, _timeout_seconds: json.dumps({"nodes": [], "edges": []})
    )
    tasks = DesktopKnowledgeGraphExtractionTasks(kb_dir)

    assert tasks.queue(document.document_id, gateway)
    assert tasks.run_document(document.document_id, gateway, should_stop=lambda: False)

    [task] = DesktopTextImportService(kb_dir).list_import_jobs()["knowledge_graph_extractions"]
    assert task["status"] == "completed_empty"
    assert task["node_count"] == 0
    assert task["edge_count"] == 0


def test_repaired_empty_model_graph_is_projected_as_completed_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "openkb.desktop_import_runner.start_graph_extraction", lambda *_args, **_kw: None
    )
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "repaired-empty-graph-task.txt"
    source.write_text("This source has no supported relationship.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    document = DesktopTextImportService(kb_dir).import_text(source).document
    calls: list[str] = []

    def response(request, _timeout_seconds):
        calls.append(request.operation)
        if request.operation == "knowledge_graph_extraction":
            return "not-json"
        return json.dumps({"nodes": [], "edges": []})

    gateway = DesktopModelGateway(response)
    tasks = DesktopKnowledgeGraphExtractionTasks(kb_dir)
    assert tasks.queue(document.document_id, gateway)
    assert tasks.run_document(document.document_id, gateway, should_stop=lambda: False)

    assert calls == ["knowledge_graph_extraction", "structured_output_repair"]
    [task] = DesktopTextImportService(kb_dir).list_import_jobs()["knowledge_graph_extractions"]
    assert task["status"] == "completed_empty"
    assert task["node_count"] == 0
    assert task["edge_count"] == 0
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute(
            """
            SELECT results.status, results.node_count, results.edge_count
            FROM knowledge_graph_current AS current
            JOIN knowledge_graph_results AS results ON results.result_id = current.result_id
            WHERE current.document_id = ?
            """,
            (document.document_id,),
        ).fetchone() == ("completed_empty", 0, 0)


def test_new_empty_graph_replaces_previous_relationships_for_the_document(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "openkb.desktop_import_runner.start_graph_extraction", lambda *_args, **_kw: None
    )
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "replaced-graph.txt"
    source.write_text("Atlas uses the gateway.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    document = DesktopTextImportService(kb_dir).import_text(source).document
    call_count = 0

    def graph_response(request, _timeout_seconds):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            return json.dumps({"nodes": [], "edges": []})
        [evidence] = json.loads(request.content)["evidence"]
        return json.dumps(
            {
                "nodes": [
                    {
                        "id": "entity-1",
                        "evidence_id": evidence["evidence_id"],
                        "type": "entity",
                        "label": "Atlas",
                        "support_quote": "Atlas",
                    },
                    {
                        "id": "concept-1",
                        "evidence_id": evidence["evidence_id"],
                        "type": "concept",
                        "label": "Gateway",
                        "support_quote": "gateway",
                    },
                ],
                "edges": [
                    {
                        "evidence_id": evidence["evidence_id"],
                        "source_id": "entity-1",
                        "target_id": "concept-1",
                        "type": "USES",
                        "support_quote": evidence["text"],
                    }
                ],
            }
        )

    service = DesktopKnowledgeGraphService(
        kb_dir, model_gateway=DesktopModelGateway(graph_response)
    )
    assert service.extract_document(document.document_id)
    approved_digest = desktop_knowledge_snapshot_digest(kb_dir)
    approved_revision = desktop_knowledge_snapshot_revision(kb_dir)
    enable_local_graph_after_evaluation(
        kb_dir,
        "passing-suite",
        approved_digest,
        approved_revision,
    )
    assert local_graph_default_enabled(kb_dir)
    assert service.extract_document(document.document_id)
    assert desktop_knowledge_snapshot_revision(kb_dir) > approved_revision
    assert desktop_knowledge_snapshot_digest(kb_dir) != approved_digest
    assert not local_graph_default_enabled(kb_dir)

    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM knowledge_graph_nodes").fetchone() == (2,)
        assert connection.execute("SELECT COUNT(*) FROM knowledge_graph_edges").fetchone() == (1,)
        results = connection.execute(
            """
            SELECT results.status, results.node_count, results.edge_count
            FROM knowledge_graph_results AS results
            WHERE results.document_id = ? ORDER BY results.created_at, results.result_id
            """,
            (document.document_id,),
        ).fetchall()
        current = connection.execute(
            """
            SELECT results.status, results.node_count, results.edge_count
            FROM knowledge_graph_current AS current
            JOIN knowledge_graph_results AS results ON results.result_id = current.result_id
            WHERE current.document_id = ?
            """,
            (document.document_id,),
        ).fetchone()
        assert results == [("completed", 2, 1), ("completed_empty", 0, 0)]
        assert current == ("completed_empty", 0, 0)
        assert local_graph_evidence_ids(connection, terms=("Atlas",), anchor_evidence_ids=()) == ()


def test_graph_records_keep_same_named_nodes_separate_and_evidence_bound(tmp_path, monkeypatch):
    """Graph facts stay traceable to individual evidence instead of name-merging."""
    monkeypatch.setattr(
        "openkb.desktop_import_runner.start_graph_extraction", lambda *_args, **_kw: None
    )
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "atlas.txt"
    source.write_text(
        "# Platform\n\nAtlas deploys the release service.\n\n"
        "# Operations\n\nAtlas depends on the gateway.\n",
        encoding="utf-8",
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    document = DesktopTextImportService(kb_dir).import_text(source).document

    def graph_response(request, _timeout_seconds):
        assert request.operation == "knowledge_graph_extraction"
        evidence = json.loads(request.content)["evidence"]
        nodes: list[dict[str, str]] = []
        edges: list[dict[str, str]] = []
        for ordinal, item in enumerate(evidence):
            entity_id = f"entity-{ordinal}"
            concept_id = f"concept-{ordinal}"
            claim_id = f"claim-{ordinal}"
            nodes.extend(
                (
                    {
                        "id": entity_id,
                        "evidence_id": item["evidence_id"],
                        "type": "entity",
                        "label": "Atlas",
                        "support_quote": item["text"],
                    },
                    {
                        "id": concept_id,
                        "evidence_id": item["evidence_id"],
                        "type": "concept",
                        "label": "Deployment",
                        "support_quote": item["text"],
                    },
                    {
                        "id": claim_id,
                        "evidence_id": item["evidence_id"],
                        "type": "claim",
                        "label": item["text"],
                        "support_quote": item["text"],
                    },
                )
            )
            edges.extend(
                (
                    {
                        "evidence_id": item["evidence_id"],
                        "source_id": entity_id,
                        "target_id": concept_id,
                        "type": "RELATED_TO",
                        "support_quote": item["text"],
                    },
                    {
                        "evidence_id": item["evidence_id"],
                        "source_id": concept_id,
                        "target_id": claim_id,
                        "type": "SUPPORTS",
                        "support_quote": item["text"],
                    },
                )
            )
        return json.dumps({"nodes": nodes, "edges": edges})

    assert DesktopKnowledgeGraphService(
        kb_dir, model_gateway=DesktopModelGateway(graph_response)
    ).extract_document(document.document_id)

    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        atlas_rows = connection.execute(
            "SELECT node_id, evidence_id FROM knowledge_graph_nodes WHERE label = 'Atlas'"
        ).fetchall()
        bound_edges = connection.execute(
            """
            SELECT knowledge_graph_edges.evidence_id, source_nodes.evidence_id,
                target_nodes.evidence_id
            FROM knowledge_graph_edges
            JOIN knowledge_graph_nodes AS source_nodes
                ON source_nodes.node_id = knowledge_graph_edges.source_node_id
            JOIN knowledge_graph_nodes AS target_nodes
                ON target_nodes.node_id = knowledge_graph_edges.target_node_id
            """
        ).fetchall()
        evidence_ids = local_graph_evidence_ids(
            connection, terms=("atlas",), anchor_evidence_ids=()
        )

    assert len(atlas_rows) >= 2
    assert len({row[0] for row in atlas_rows}) == len(atlas_rows)
    assert len({row[1] for row in atlas_rows}) >= 2
    assert all(
        edge_evidence == source_evidence == target_evidence
        for edge_evidence, source_evidence, target_evidence in bound_edges
    )
    assert set(evidence_ids).issuperset({row[1] for row in atlas_rows})


def test_legacy_graph_fallback_excludes_documents_with_admitted_identities(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "openkb.desktop_import_runner.start_graph_extraction", lambda *_args, **_kw: None
    )
    kb_dir = tmp_path / "desktop-kb"
    atlas_source = tmp_path / "atlas.txt"
    borealis_source = tmp_path / "borealis.txt"
    atlas_source.write_text("Atlas operates the gateway.", encoding="utf-8")
    borealis_source.write_text("Borealis operates the archive.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    importer = DesktopTextImportService(kb_dir)
    atlas_document = importer.import_text(atlas_source).document
    borealis_document = importer.import_text(borealis_source).document

    def graph_response(request, _timeout_seconds):
        [evidence] = json.loads(request.content)["evidence"]
        label = evidence["text"].split()[0]
        return json.dumps(
            {
                "nodes": [
                    {
                        "id": "entity-1",
                        "evidence_id": evidence["evidence_id"],
                        "type": "entity",
                        "label": label,
                        "support_quote": label,
                    }
                ],
                "edges": [],
            }
        )

    service = DesktopKnowledgeGraphService(
        kb_dir, model_gateway=DesktopModelGateway(graph_response)
    )
    assert service.extract_document(atlas_document.document_id)
    assert service.extract_document(borealis_document.document_id)

    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        connection.execute("DELETE FROM knowledge_generation_state WHERE singleton = 1")
        atlas_evidence = local_graph_evidence_ids(
            connection, terms=("atlas",), anchor_evidence_ids=()
        )
        borealis_evidence = local_graph_evidence_ids(
            connection, terms=("borealis",), anchor_evidence_ids=()
        )
        assert atlas_evidence
        assert borealis_evidence
        connection.execute(
            """
            INSERT INTO knowledge_document_candidates (
                candidate_id, document_id, kind, title, normalized_title,
                entity_subtype, aliases_json, tags_json, admission_state,
                admission_reason, analysis_provenance_json, created_at
            ) VALUES (?, ?, 'entity', 'Atlas', 'atlas', 'service', '[]', '[]',
                'admitted', 'qualified', '{}', ?)
            """,
            (
                "candidate-atlas",
                atlas_document.document_id,
                "2026-09-03T00:00:00+00:00",
            ),
        )

        assert local_graph_evidence_ids(connection, terms=("atlas",), anchor_evidence_ids=()) == ()
        assert (
            local_graph_evidence_ids(connection, terms=("borealis",), anchor_evidence_ids=())
            == borealis_evidence
        )


def test_graph_budget_preserves_baseline_minimum_candidates():
    """A graph addition cannot displace the four protected baseline results."""
    baseline = tuple(_reference(f"base-{ordinal}") for ordinal in range(6))
    graph = (
        RetrievalCandidate(
            _reference("graph-only", channels=("knowledge_graph",)), "knowledge_graph", 1
        ),
    )

    selected = with_graph_budget(baseline, graph)

    assert [reference.evidence_id for reference in selected[:4]] == [
        reference.evidence_id for reference in baseline[:4]
    ]
    assert "graph-only" in {reference.evidence_id for reference in selected}
    assert len(selected) == 6


def test_graph_failures_keep_baseline_answers_and_only_record_safe_diagnostics(
    tmp_path, monkeypatch
):
    """Model and query failures are internal capability degradation, never answer failures."""
    monkeypatch.setattr(
        "openkb.desktop_import_runner.start_graph_extraction", lambda *_args, **_kw: None
    )
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "baseline.txt"
    source.write_text("The Meridian protocol keeps a local evidence baseline.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    document = DesktopTextImportService(kb_dir).import_text(source).document

    def timeout(*_args, **_kwargs):
        raise TimeoutError()

    assert not DesktopKnowledgeGraphService(
        kb_dir, model_gateway=DesktopModelGateway(timeout)
    ).extract_document(document.document_id)
    monkeypatch.setattr(
        retrieval,
        "local_graph_evidence_ids",
        lambda *_args, **_kwargs: ("missing-graph-evidence",),
    )
    monkeypatch.setattr(
        retrieval,
        "bounded_graph_rows",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            DesktopKnowledgeGraphQueryError("knowledge_graph_query_timeout")
        ),
    )

    pack = DesktopEvidenceRetriever(kb_dir).retrieve_variant(
        "What does the Meridian protocol keep?", variant="local_graph"
    )

    assert pack.evidence
    assert "knowledge_graph_query_timeout" not in pack.degradations
    graph_trace = next(
        channel for channel in pack.retrieval_trace.channels if channel.channel == "knowledge_graph"
    )
    assert graph_trace.candidate_count == 0
    assert "knowledge_graph_query_timeout" in graph_trace.degradation_reasons
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute(
            "SELECT availability FROM source_documents WHERE document_id = ?",
            (document.document_id,),
        ).fetchone() == ("available",)
        diagnostics = connection.execute(
            "SELECT phase, error_code FROM knowledge_graph_diagnostics ORDER BY created_at"
        ).fetchall()
    assert ("extraction", "model_network_transient") in diagnostics
    assert ("query", "knowledge_graph_query_timeout") in diagnostics


def test_invalid_graph_repair_records_failure_without_single_document_suspension(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "openkb.desktop_import_runner.start_graph_extraction", lambda *_args, **_kw: None
    )
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "invalid-graph.txt"
    source.write_text("Atlas keeps a deterministic evidence baseline.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    document = DesktopTextImportService(kb_dir).import_text(source).document
    save_desktop_model_settings(
        kb_dir,
        provider="deepseek",
        model="deepseek-v4-pro",
        api_base_url="https://api.deepseek.com",
        api_key="test-key",
        max_concurrent_model_calls=1,
    )

    class InvalidTransport:
        def __init__(self, *, model, bundle):
            del model, bundle

        def __call__(self, request, _timeout_seconds):
            del request
            return "not-json"

    monkeypatch.setattr(desktop_model_transport, "DesktopLiteLLMTransport", InvalidTransport)
    gateway = desktop_model_transport.desktop_model_gateway_for(kb_dir)
    assert gateway is not None
    profile = gateway.execution_profile_for_operation("knowledge_graph_extraction")
    capability_store = DesktopModelCapabilityStore(kb_dir)
    capability_store.mark_verified(profile)

    assert not DesktopKnowledgeGraphService(
        kb_dir,
        model_gateway=gateway,
    ).extract_document(document.document_id)

    usage = DesktopModelUsageStore(kb_dir).records()
    assert {record["operation"] for record in usage} == {
        "knowledge_graph_extraction",
        "structured_output_repair",
    }
    failed = [record for record in usage if record["failure_code"] is not None]
    assert [record["operation"] for record in failed] == [
        "knowledge_graph_extraction",
        "structured_output_repair",
    ]
    assert {record["lifecycle_status"] for record in failed} == {"model_result_failure"}
    assert {record["failure_code"] for record in failed} == {"model_response_invalid"}
    assert capability_store.state(profile).status == "verified"
    operation_state = DesktopModelOperationContractStore(kb_dir).state(
        operation="knowledge_graph_extraction",
        capability_identity=profile.capability_evidence_profile.identity,
        prompt_contract_digest=prompt_contract_for("knowledge_graph_extraction").digest,
    )
    assert operation_state.status == "unverified"
    assert operation_state.failure_code is None
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        attempt = connection.execute(
            """
            SELECT lifecycle, quality, retained_count, rejected_count, failure_signature
            FROM knowledge_graph_attempts WHERE document_id = ?
            """,
            (document.document_id,),
        ).fetchone()
        issues = connection.execute(
            """
            SELECT code, contract_path, disposition, failure_class
            FROM knowledge_graph_attempt_issues ORDER BY ordinal
            """
        ).fetchall()
    assert attempt is not None
    assert attempt[:4] == ("failed", None, 0, 0)
    assert str(attempt[4]).startswith("kg:")
    assert issues == [("invalid_json", "$", "fatal", "shape")]


def test_suspended_graph_contract_blocks_later_documents_until_explicit_retry(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "openkb.desktop_import_runner.start_graph_extraction", lambda *_args, **_kw: None
    )
    kb_dir = tmp_path / "desktop-kb"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    first_source = tmp_path / "first-invalid-graph.txt"
    first_source.write_text("Atlas uses a local gateway.", encoding="utf-8")
    second_source = tmp_path / "second-invalid-graph.txt"
    second_source.write_text("Meridian uses another local gateway.", encoding="utf-8")
    third_source = tmp_path / "third-invalid-graph.txt"
    third_source.write_text("Orion uses one more local gateway.", encoding="utf-8")
    first = DesktopTextImportService(kb_dir).import_text(first_source).document
    second = DesktopTextImportService(kb_dir).import_text(second_source).document
    third = DesktopTextImportService(kb_dir).import_text(third_source).document
    save_desktop_model_settings(
        kb_dir,
        provider="deepseek",
        model="deepseek-v4-pro",
        api_base_url="https://api.deepseek.com",
        api_key="test-key",
        max_concurrent_model_calls=1,
    )
    calls: list[str] = []
    valid_response = False

    class InvalidThenValidTransport:
        def __init__(self, *, model, bundle):
            del model, bundle

        def __call__(self, request, _timeout_seconds):
            calls.append(request.operation)
            if valid_response:
                return json.dumps({"nodes": [], "edges": []})
            return "not-json"

    monkeypatch.setattr(
        desktop_model_transport,
        "DesktopLiteLLMTransport",
        InvalidThenValidTransport,
    )
    gateway = desktop_model_transport.desktop_model_gateway_for(kb_dir)
    assert gateway is not None
    profile = gateway.execution_profile_for_operation("knowledge_graph_extraction")
    DesktopModelCapabilityStore(kb_dir).mark_verified(profile)
    tasks = DesktopKnowledgeGraphExtractionTasks(kb_dir)
    assert tasks.queue(first.document_id, gateway)
    assert tasks.queue(second.document_id, gateway)
    assert tasks.queue(third.document_id, gateway)

    assert not tasks.run_document(first.document_id, gateway, should_stop=lambda: False)
    assert calls == ["knowledge_graph_extraction", "structured_output_repair"]
    assert tasks.pending_document_ids(gateway) == (second.document_id, third.document_id)
    assert not tasks.run_document(second.document_id, gateway, should_stop=lambda: False)
    assert calls == [
        "knowledge_graph_extraction",
        "structured_output_repair",
        "knowledge_graph_extraction",
        "structured_output_repair",
    ]
    assert tasks.pending_document_ids(gateway) == ()
    assert not tasks.run_document(third.document_id, gateway, should_stop=lambda: False)
    assert len(calls) == 4
    profile = gateway.execution_profile_for_operation("knowledge_graph_extraction")
    state = DesktopModelOperationContractStore(kb_dir).state(
        operation="knowledge_graph_extraction",
        capability_identity=profile.capability_evidence_profile.identity,
        prompt_contract_digest=prompt_contract_for("knowledge_graph_extraction").digest,
    )
    assert state.status == "suspended"
    assert state.failure_code == "knowledge_graph_response_invalid"
    assert state.failure_signature is not None

    valid_response = True
    assert tasks.retry(first.document_id, gateway)
    assert tasks.pending_document_ids(gateway) == (first.document_id,)
    assert tasks.run_document(first.document_id, gateway, should_stop=lambda: False)
    assert calls[-1] == "knowledge_graph_extraction"
    assert tasks.pending_document_ids(gateway) == (third.document_id,)


def test_all_rejected_semantic_graph_does_not_spend_the_repair_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "openkb.desktop_import_runner.start_graph_extraction", lambda *_args, **_kw: None
    )
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "unsupported-node.txt"
    source.write_text("Atlas uses Gateway.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    document = DesktopTextImportService(kb_dir).import_text(source).document
    operations: list[str] = []

    def unsupported_node(request, _timeout_seconds):
        operations.append(request.operation)
        [evidence] = json.loads(request.content)["evidence"]
        return json.dumps(
            {
                "nodes": [
                    {
                        "id": "atlas",
                        "evidence_id": evidence["evidence_id"],
                        "type": "person",
                        "label": "Atlas",
                        "support_quote": "Atlas",
                    }
                ],
                "edges": [],
            }
        )

    assert not DesktopKnowledgeGraphService(
        kb_dir,
        model_gateway=DesktopModelGateway(unsupported_node),
    ).extract_document(document.document_id)

    assert operations == ["knowledge_graph_extraction"]
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute(
            """
            SELECT lifecycle, rejected_count FROM knowledge_graph_attempts
            WHERE document_id = ?
            """,
            (document.document_id,),
        ).fetchone() == ("failed", 1)
        assert connection.execute(
            """
            SELECT code, contract_path FROM knowledge_graph_attempt_issues
            """
        ).fetchall() == [("unsupported_node_type", "nodes[0].type")]


def test_in_flight_valid_graph_finishes_without_clearing_a_newer_suspension(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "openkb.desktop_import_runner.start_graph_extraction", lambda *_args, **_kw: None
    )
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "in-flight.txt"
    source.write_text("No supported relationship is present.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    document = DesktopTextImportService(kb_dir).import_text(source).document
    capability_identity = "test-analysis-capability"
    contract = prompt_contract_for("knowledge_graph_extraction")

    class CapabilityProfile:
        identity = capability_identity

    class ExecutionProfile:
        capability_evidence_profile = CapabilityProfile()

    class SuspendingGateway:
        provider_name = "test-provider"
        model_name = "test-model"

        def execution_profile_for_operation(self, _operation):
            return ExecutionProfile()

        def analyze(self, request, *, on_event, is_cancelled):
            del on_event
            assert is_cancelled is None or not is_cancelled()
            DesktopModelOperationContractStore(kb_dir).suspend(
                operation="knowledge_graph_extraction",
                capability_identity=capability_identity,
                prompt_contract_digest=contract.digest,
                failure_code="knowledge_graph_response_invalid",
                reason="A concurrent document corroborated the structural failure.",
                failure_stage="domain_validation",
                failure_signature="kg:concurrent",
            )
            return DesktopModelResult(
                "in-flight-call",
                json.dumps({"nodes": [], "edges": []}),
                1,
                diagnostic_context={
                    "operation": request.operation,
                    "capability_identity": capability_identity,
                    "prompt_contract_digest": request.prompt_contract_digest,
                },
            )

    assert DesktopKnowledgeGraphService(
        kb_dir,
        model_gateway=SuspendingGateway(),  # type: ignore[arg-type]
    ).extract_document(document.document_id)

    state = DesktopModelOperationContractStore(kb_dir).state(
        operation="knowledge_graph_extraction",
        capability_identity=capability_identity,
        prompt_contract_digest=contract.digest,
    )
    assert state.status == "suspended"
    assert state.failure_signature == "kg:concurrent"
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute(
            "SELECT status FROM knowledge_graph_results WHERE document_id = ?",
            (document.document_id,),
        ).fetchone() == ("completed_empty",)


def test_explicit_retry_finishes_without_clearing_a_newer_suspension(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "openkb.desktop_import_runner.start_graph_extraction", lambda *_args, **_kw: None
    )
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "explicit-retry-in-flight.txt"
    source.write_text("No supported relationship is present.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    document = DesktopTextImportService(kb_dir).import_text(source).document
    capability_identity = "test-analysis-capability"
    contract = prompt_contract_for("knowledge_graph_extraction")
    retry_scope = f"knowledge_graph:{document.document_id}"
    store = DesktopModelOperationContractStore(kb_dir)
    store.suspend(
        operation="knowledge_graph_extraction",
        capability_identity=capability_identity,
        prompt_contract_digest=contract.digest,
        failure_code="knowledge_graph_response_invalid",
        reason="The original graph response was invalid.",
        failure_stage="domain_validation",
        failure_signature="kg:original",
    )
    assert store.authorize_retry(
        operation="knowledge_graph_extraction",
        capability_identity=capability_identity,
        prompt_contract_digest=contract.digest,
        retry_scope=retry_scope,
    )

    class CapabilityProfile:
        identity = capability_identity

    class ExecutionProfile:
        capability_evidence_profile = CapabilityProfile()

    class SuspendingGateway:
        provider_name = "test-provider"
        model_name = "test-model"

        def execution_profile_for_operation(self, _operation):
            return ExecutionProfile()

        def analyze(self, request, *, on_event, is_cancelled):
            del on_event
            assert is_cancelled is None or not is_cancelled()
            store.suspend(
                operation="knowledge_graph_extraction",
                capability_identity=capability_identity,
                prompt_contract_digest=contract.digest,
                failure_code="knowledge_graph_response_invalid",
                reason="A concurrent document corroborated a newer failure.",
                failure_stage="domain_validation",
                failure_signature="kg:newer",
            )
            return DesktopModelResult(
                "explicit-retry-call",
                json.dumps({"nodes": [], "edges": []}),
                1,
                diagnostic_context={
                    "operation": request.operation,
                    "capability_identity": capability_identity,
                    "prompt_contract_digest": request.prompt_contract_digest,
                },
            )

    assert DesktopKnowledgeGraphService(
        kb_dir,
        model_gateway=SuspendingGateway(),  # type: ignore[arg-type]
    ).extract_document(document.document_id, retry_scope=retry_scope)

    state = store.state(
        operation="knowledge_graph_extraction",
        capability_identity=capability_identity,
        prompt_contract_digest=contract.digest,
    )
    assert state.status == "suspended"
    assert state.failure_signature == "kg:newer"
    assert not store.dispatch_possible(
        operation="knowledge_graph_extraction",
        capability_identity=capability_identity,
        prompt_contract_digest=contract.digest,
        retry_scope=retry_scope,
    )


def test_retry_action_does_not_authorize_a_suspension_created_before_dispatch(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "openkb.desktop_import_runner.start_graph_extraction", lambda *_args, **_kw: None
    )
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "late-suspension.txt"
    source.write_text("No supported relationship is present.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    document = DesktopTextImportService(kb_dir).import_text(source).document
    capability_identity = "test-analysis-capability"
    contract = prompt_contract_for("knowledge_graph_extraction")
    retry_scope = f"knowledge_graph_extraction:{document.document_id}:action"
    store = DesktopModelOperationContractStore(kb_dir)

    class CapabilityProfile:
        identity = capability_identity

    class ExecutionProfile:
        capability_evidence_profile = CapabilityProfile()

    class CountingGateway:
        provider_name = "test-provider"
        model_name = "test-model"

        def __init__(self) -> None:
            self.calls = 0

        def execution_profile_for_operation(self, _operation):
            return ExecutionProfile()

        def analyze(self, request, *, on_event, is_cancelled):
            del request, on_event, is_cancelled
            self.calls += 1
            return DesktopModelResult("unexpected-call", '{"nodes":[],"edges":[]}', 1)

    gateway = CountingGateway()
    assert not authorize_model_operation_retry(
        kb_dir,
        gateway,
        operation="knowledge_graph_extraction",
        retry_scope=retry_scope,
    )

    def admit_then_suspend(*_args, **_kwargs) -> bool:
        store.suspend(
            operation="knowledge_graph_extraction",
            capability_identity=capability_identity,
            prompt_contract_digest=contract.digest,
            failure_code="knowledge_graph_response_invalid",
            reason="A concurrent document suspended this contract.",
            failure_stage="domain_validation",
        )
        return True

    monkeypatch.setattr(
        "openkb.desktop_knowledge_graph.model_operation_dispatch_possible",
        admit_then_suspend,
    )
    failures: list[str] = []

    assert not DesktopKnowledgeGraphService(
        kb_dir,
        model_gateway=gateway,  # type: ignore[arg-type]
    ).extract_document(
        document.document_id,
        retry_scope=retry_scope,
        on_failure=lambda code, _reason: failures.append(code),
    )
    assert gateway.calls == 0
    assert failures == ["model_operation_suspended"]


def test_new_graph_retry_action_uses_a_unique_scope_for_the_current_revisions(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "openkb.desktop_import_runner.start_graph_extraction", lambda *_args, **_kw: None
    )
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "stale-retry-scope.txt"
    source.write_text("No supported relationship is present.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    document = DesktopTextImportService(kb_dir).import_text(source).document
    capability_identity = "test-analysis-capability"

    class CapabilityProfile:
        identity = capability_identity

    class ExecutionProfile:
        capability_evidence_profile = CapabilityProfile()

    class RetryGateway:
        provider_name = "test-provider"
        model_name = "test-model"

        def execution_profile_for_operation(self, _operation):
            return ExecutionProfile()

    gateway = RetryGateway()
    tasks = DesktopKnowledgeGraphExtractionTasks(kb_dir)
    assert tasks.queue(document.document_id, gateway)  # type: ignore[arg-type]
    contract = prompt_contract_for("knowledge_graph_extraction")
    repair_digest = structured_output_repair_contract_digest("knowledge_graph_extraction")
    store = DesktopModelOperationContractStore(kb_dir)
    contracts = (
        ("knowledge_graph_extraction", contract.digest),
        ("structured_output_repair", repair_digest),
    )
    for operation, digest in contracts:
        store.suspend(
            operation=operation,
            capability_identity=capability_identity,
            prompt_contract_digest=digest,
            failure_code="knowledge_graph_response_invalid",
            reason="Original failure.",
            failure_stage="domain_validation",
        )

    observed_publications: list[tuple[bool, str | None, str, str]] = []

    def observe_authorization(connection, *, operation, retry_scope, **kwargs):
        row = connection.execute(
            "SELECT retry_scope FROM knowledge_graph_extraction_tasks WHERE document_id = ?",
            (document.document_id,),
        ).fetchone()
        observed_publications.append(
            (
                connection.in_transaction,
                str(row[0]) if row is not None and row[0] is not None else None,
                retry_scope,
                operation,
            )
        )
        return authorize_model_operation_retry_in(
            connection,
            operation=operation,
            retry_scope=retry_scope,
            **kwargs,
        )

    monkeypatch.setattr(
        "openkb.desktop_model_result_failure._authorize_model_operation_retry_in",
        observe_authorization,
    )

    assert tasks.retry(document.document_id, gateway)  # type: ignore[arg-type]
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        first_scope = str(
            connection.execute(
                "SELECT retry_scope FROM knowledge_graph_extraction_tasks WHERE document_id = ?",
                (document.document_id,),
            ).fetchone()[0]
        )
    assert first_scope.startswith(f"knowledge_graph_extraction:{document.document_id}:")

    for operation, digest in contracts:
        store.suspend(
            operation=operation,
            capability_identity=capability_identity,
            prompt_contract_digest=digest,
            failure_code="knowledge_graph_response_invalid",
            reason="Newer failure after the first action.",
            failure_stage="domain_validation",
        )
    assert tasks.retry(document.document_id, gateway)  # type: ignore[arg-type]
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        second_scope = str(
            connection.execute(
                "SELECT retry_scope FROM knowledge_graph_extraction_tasks WHERE document_id = ?",
                (document.document_id,),
            ).fetchone()[0]
        )

    assert second_scope != first_scope
    for operation, digest in contracts:
        assert not store.dispatch_possible(
            operation=operation,
            capability_identity=capability_identity,
            prompt_contract_digest=digest,
            retry_scope=first_scope,
        )
        assert store.dispatch_possible(
            operation=operation,
            capability_identity=capability_identity,
            prompt_contract_digest=digest,
            retry_scope=second_scope,
        )
    assert observed_publications
    assert all(
        in_transaction and stored_scope == requested_scope
        for in_transaction, stored_scope, requested_scope, _ in observed_publications
    )
    assert tasks.pending_document_ids(gateway) == (document.document_id,)  # type: ignore[arg-type]


def test_graph_retry_revokes_partial_authorization_if_group_setup_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "openkb.desktop_import_runner.start_graph_extraction", lambda *_args, **_kw: None
    )
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "partial-retry-authorization.txt"
    source.write_text("No supported relationship is present.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    document = DesktopTextImportService(kb_dir).import_text(source).document
    capability_identity = "test-analysis-capability"

    class CapabilityProfile:
        identity = capability_identity

    class ExecutionProfile:
        capability_evidence_profile = CapabilityProfile()

    class RetryGateway:
        provider_name = "test-provider"
        model_name = "test-model"

        def execution_profile_for_operation(self, _operation):
            return ExecutionProfile()

    gateway = RetryGateway()
    tasks = DesktopKnowledgeGraphExtractionTasks(kb_dir)
    assert tasks.queue(document.document_id, gateway)  # type: ignore[arg-type]
    contract = prompt_contract_for("knowledge_graph_extraction")
    DesktopModelOperationContractStore(kb_dir).suspend(
        operation="knowledge_graph_extraction",
        capability_identity=capability_identity,
        prompt_contract_digest=contract.digest,
        failure_code="knowledge_graph_response_invalid",
        reason="Original failure.",
        failure_stage="domain_validation",
    )

    def fail_second_authorization(connection, *, operation, **kwargs):
        if operation == "structured_output_repair":
            raise RuntimeError("repair authorization failed")
        return authorize_model_operation_retry_in(connection, operation=operation, **kwargs)

    monkeypatch.setattr(
        "openkb.desktop_model_result_failure._authorize_model_operation_retry_in",
        fail_second_authorization,
    )

    with pytest.raises(RuntimeError, match="repair authorization failed"):
        tasks.retry(document.document_id, gateway)  # type: ignore[arg-type]

    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute(
            "SELECT status, reason, retry_scope FROM knowledge_graph_extraction_tasks "
            "WHERE document_id = ?",
            (document.document_id,),
        ).fetchone() == ("pending", "initial", None)
        assert connection.execute(
            "SELECT COUNT(*) FROM model_operation_retry_permits"
        ).fetchone() == (0,)


def test_graph_query_diagnostic_never_waits_for_an_active_kb_mutation(tmp_path, monkeypatch):
    """Best-effort graph diagnostics cannot wait behind an Import Job."""
    monkeypatch.setattr(
        "openkb.desktop_import_runner.start_graph_extraction", lambda *_args, **_kw: None
    )
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "baseline.txt"
    source.write_text("The Meridian protocol keeps baseline evidence.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    DesktopTextImportService(kb_dir).import_text(source)
    graph_query_started = threading.Event()
    lock_acquired = threading.Event()
    release_lock = threading.Event()

    def hold_kb_mutation() -> None:
        assert graph_query_started.wait(timeout=2)
        with kb_ingest_lock(desktop_state_dir(kb_dir)):
            lock_acquired.set()
            assert release_lock.wait(timeout=2)

    lock_worker = threading.Thread(target=hold_kb_mutation)
    lock_worker.start()

    @contextmanager
    def no_catalog_lease(_kb_dir, _generation_id):
        yield None

    monkeypatch.setattr(retrieval, "lease_catalog_generation", no_catalog_lease)
    monkeypatch.setattr(
        retrieval,
        "local_graph_evidence_ids",
        lambda *_args, **_kwargs: ("missing-graph-evidence",),
    )

    def fail_graph_query(*_args, **_kwargs):
        graph_query_started.set()
        assert lock_acquired.wait(timeout=2)
        raise DesktopKnowledgeGraphQueryError("knowledge_graph_query_timeout")

    monkeypatch.setattr(retrieval, "bounded_graph_rows", fail_graph_query)

    started_at = time.monotonic()
    pack = DesktopEvidenceRetriever(kb_dir).retrieve_variant(
        "What does the Meridian protocol keep?", variant="local_graph"
    )
    elapsed = time.monotonic() - started_at
    release_lock.set()
    lock_worker.join(timeout=2)

    assert elapsed < 0.5
    assert pack.evidence
    graph_trace = next(
        channel for channel in pack.retrieval_trace.channels if channel.channel == "knowledge_graph"
    )
    assert graph_trace.candidate_count == 0
    assert "knowledge_graph_query_timeout" in graph_trace.degradation_reasons
    assert "knowledge_graph_query_timeout" not in _graph_diagnostic_codes(kb_dir, tmp_path)

    DesktopEvidenceRetriever(kb_dir).retrieve_variant(
        "What does the Meridian protocol keep?", variant="local_graph"
    )
    assert "knowledge_graph_query_timeout" in _graph_diagnostic_codes(kb_dir, tmp_path)


def test_graph_worker_start_failure_keeps_document_available_and_is_diagnostic(
    tmp_path, monkeypatch
):
    """A worker launch failure is visible only as a safe internal diagnostic."""
    monkeypatch.setattr(
        "openkb.desktop_import_runner.start_graph_extraction",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError()),
    )
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "launch-failure.txt"
    source.write_text("A baseline remains answerable.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)

    document = DesktopTextImportService(kb_dir).import_text(source).document

    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute(
            "SELECT availability FROM source_documents WHERE document_id = ?",
            (document.document_id,),
        ).fetchone() == ("available",)
        assert connection.execute(
            "SELECT phase, error_code, document_id FROM knowledge_graph_diagnostics"
        ).fetchall() == [("extraction", "knowledge_graph_extraction_failed", document.document_id)]


def test_graph_task_is_cancelled_durably_and_retried_explicitly(tmp_path):
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "graph-task.txt"
    source.write_text("Atlas depends on the local gateway.", encoding="utf-8")
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(kb_dir)
    document = DesktopTextImportService(kb_dir).import_text(source).document
    started = threading.Event()
    allow_cancel_poll = threading.Event()
    old_attempt_observed_cancel = threading.Event()
    gateways: list[object] = []

    class BlockingGateway:
        provider_name = "provider-a"
        model_name = "model-a"

        def analyze(self, _request, *, on_event, is_cancelled):
            on_event(DesktopTerminalModelEvent("graph-call-1", 1, "connecting", 0))
            started.set()
            assert allow_cancel_poll.wait(timeout=2)
            if is_cancelled():
                old_attempt_observed_cancel.set()
            raise DesktopModelCancelledError()

    class SuccessfulGateway:
        provider_name = "provider-a"
        model_name = "model-a"

        def analyze(self, request, *, on_event, is_cancelled):
            assert not is_cancelled()
            evidence = json.loads(request.content)["evidence"]
            on_event(DesktopTerminalModelEvent("graph-call-2", 1, "completed", 1))
            return DesktopModelResult(
                "graph-call-2",
                json.dumps(
                    {
                        "nodes": [
                            {
                                "id": "atlas",
                                "evidence_id": evidence[0]["evidence_id"],
                                "type": "entity",
                                "label": "Atlas",
                                "support_quote": "Atlas",
                            }
                        ],
                        "edges": [],
                    }
                ),
                1,
            )

    blocking = BlockingGateway()
    gateways.append(blocking)
    tasks = DesktopKnowledgeGraphExtractionTasks(kb_dir)
    assert tasks.queue(document.document_id, blocking)
    server = DesktopEngineServer(
        io.BytesIO(),
        io.BytesIO(),
        workspace=workspace,
        model_gateway_factory=lambda _kb_dir, _override: gateways[-1],
    )
    server._handshake_complete = True
    graph_engine.start_knowledge_graph_extractions(server, kb_dir, blocking)
    assert started.wait(timeout=2)

    cancelled = server._dispatch(
        DesktopRequest(
            request_id="cancel-graph",
            method="workbench.cancel_knowledge_graph_extraction",
            params={"document_id": document.document_id},
        ),
        cancel_event=None,
    )
    assert cancelled == {"document_id": document.document_id, "accepted": True}
    assert _wait_for_graph_task(kb_dir, "pending") == (
        "pending",
        "knowledge_graph_extraction_interrupted",
        "graph-call-1",
    )

    gateways.append(SuccessfulGateway())
    retried = server._dispatch(
        DesktopRequest(
            request_id="retry-graph",
            method="workbench.retry_knowledge_graph_extraction",
            params={"document_id": document.document_id},
        ),
        cancel_event=None,
    )
    assert retried == {"document_id": document.document_id, "accepted": True}
    allow_cancel_poll.set()
    assert old_attempt_observed_cancel.wait(timeout=2)
    assert _wait_for_graph_task(kb_dir, "completed")[0] == "completed"
    projection = DesktopTextImportService(kb_dir).list_import_jobs()["knowledge_graph_extractions"][
        0
    ]
    assert projection["call_id"] == "graph-call-2"
    assert projection["attempt_count"] == 2
    server._shutdown.set()
    server._join_workers()


def test_cancel_at_publish_barrier_cannot_publish_a_stale_graph_claim(tmp_path, monkeypatch):
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "publish-race.txt"
    source.write_text("Atlas depends on the local gateway.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    document = DesktopTextImportService(kb_dir).import_text(source).document
    publish_reached = threading.Event()
    release_publish = threading.Event()
    cancelled = threading.Event()
    outcomes: list[bool] = []

    class SuccessfulGateway:
        provider_name = "provider-a"
        model_name = "model-a"

        def analyze(self, request, *, on_event, is_cancelled):
            evidence = json.loads(request.content)["evidence"]
            return DesktopModelResult(
                "stale-graph-call",
                json.dumps(
                    {
                        "nodes": [
                            {
                                "id": "atlas",
                                "evidence_id": evidence[0]["evidence_id"],
                                "type": "entity",
                                "label": "Atlas",
                                "support_quote": "Atlas",
                            }
                        ],
                        "edges": [],
                    }
                ),
                1,
            )

    original_persist = DesktopKnowledgeGraphService._persist

    def persist_after_barrier(service, payload, *args, **kwargs):
        publish_reached.set()
        assert release_publish.wait(timeout=2)
        return original_persist(service, payload, *args, **kwargs)

    monkeypatch.setattr(DesktopKnowledgeGraphService, "_persist", persist_after_barrier)
    gateway = SuccessfulGateway()
    tasks = DesktopKnowledgeGraphExtractionTasks(kb_dir)
    assert tasks.queue(document.document_id, gateway)
    worker = threading.Thread(
        target=lambda: outcomes.append(
            tasks.run_document(
                document.document_id,
                gateway,
                should_stop=cancelled.is_set,
            )
        )
    )
    worker.start()
    assert publish_reached.wait(timeout=2)

    assert tasks.request_cancel(document.document_id)
    cancelled.set()
    release_publish.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert outcomes == [False]
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM knowledge_graph_nodes").fetchone() == (0,)
        assert connection.execute(
            "SELECT status, execution_token, error_code "
            "FROM knowledge_graph_extraction_tasks WHERE document_id = ?",
            (document.document_id,),
        ).fetchone() == (
            "pending",
            None,
            "knowledge_graph_extraction_interrupted",
        )


def test_recovery_makes_every_queued_graph_task_explicitly_resumable(tmp_path):
    kb_dir = tmp_path / "desktop-kb"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    old_gateway = DesktopModelGateway(
        lambda _request, _timeout: "{}",
        provider_name="provider-a",
        model_name="model-a",
    )
    new_gateway = DesktopModelGateway(
        lambda _request, _timeout: "{}",
        provider_name="provider-b",
        model_name="model-b",
    )
    tasks = DesktopKnowledgeGraphExtractionTasks(kb_dir)
    document_ids: list[str] = []
    for ordinal in range(2):
        source = tmp_path / f"queued-{ordinal}.txt"
        source.write_text(f"Queued graph document {ordinal}.", encoding="utf-8")
        document = DesktopTextImportService(kb_dir).import_text(source).document
        document_ids.append(document.document_id)
        assert tasks.queue(document.document_id, old_gateway)

    assert tasks.recover_interrupted() == 2
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute(
            "SELECT status, execution_token, error_code "
            "FROM knowledge_graph_extraction_tasks ORDER BY document_id"
        ).fetchall() == [
            ("pending", None, "knowledge_graph_extraction_interrupted"),
            ("pending", None, "knowledge_graph_extraction_interrupted"),
        ]

    assert tasks.pending_document_ids(old_gateway) == ()
    assert tasks.retry(document_ids[0], new_gateway)
    assert tasks.pending_document_ids(new_gateway) == (document_ids[0],)


def test_open_recovers_graph_task_without_starting_model_work(tmp_path):
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "recover-graph.txt"
    source.write_text("Graph work must wait for the user.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    document = DesktopTextImportService(kb_dir).import_text(source).document
    gateway = DesktopModelGateway(lambda _request, _timeout: "{}")
    tasks = DesktopKnowledgeGraphExtractionTasks(kb_dir)
    assert tasks.queue(document.document_id, gateway)
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        connection.execute(
            "UPDATE knowledge_graph_extraction_tasks SET status = 'running', "
            "execution_token = 'crashed-owner'"
        )
    model_called = threading.Event()

    def unexpected_gateway(_kb_dir, _override):
        model_called.set()
        return gateway

    server = DesktopEngineServer(
        io.BytesIO(),
        io.BytesIO(),
        model_gateway_factory=unexpected_gateway,
    )
    server._handshake_complete = True
    server._dispatch(
        DesktopRequest(
            request_id="open-graph-kb",
            method="workbench.open_knowledge_base",
            params={"kb_dir": str(kb_dir)},
        ),
        cancel_event=None,
    )

    assert not model_called.wait(timeout=0.1)
    assert _wait_for_graph_task(kb_dir, "pending")[:2] == (
        "pending",
        "knowledge_graph_extraction_interrupted",
    )


def _reference(evidence_id: str, *, channels: tuple[str, ...] = ("fts",)) -> DesktopEvidenceRef:
    return DesktopEvidenceRef(
        evidence_id=evidence_id,
        document_id=f"document-{evidence_id}",
        document_name=f"{evidence_id}.txt",
        section="Document",
        locator={"line_start": 1},
        excerpt=evidence_id,
        channels=channels,
    )


def _wait_for_graph_task(kb_dir, expected: str, *, timeout: float = 2) -> tuple:
    database_path = kb_dir / ".openkb" / "state.sqlite3"
    deadline = time.monotonic() + timeout
    row = None
    while time.monotonic() < deadline:
        with sqlite3.connect(database_path) as connection:
            row = connection.execute(
                "SELECT status, error_code, call_id FROM knowledge_graph_extraction_tasks"
            ).fetchone()
        if row is not None and row[0] == expected:
            return row
        time.sleep(0.01)
    raise AssertionError(f"Knowledge Graph task did not reach {expected}: {row!r}")


def _graph_diagnostic_codes(kb_dir, destination_dir) -> set[str]:
    destination = destination_dir / "diagnostics.zip"
    DesktopDiagnosticBundleService(kb_dir).export(destination)
    with zipfile.ZipFile(destination) as archive:
        payload = json.loads(archive.read("graph-diagnostics.json"))
    return {diagnostic["error_code"] for diagnostic in payload["diagnostics"]}
