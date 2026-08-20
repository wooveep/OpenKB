"""Focused acceptance checks for the Desktop local Knowledge Graph channel."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import zipfile
from contextlib import contextmanager

import openkb.desktop_retrieval as retrieval
from openkb.desktop_answer_types import DesktopEvidenceRef
from openkb.desktop_diagnostic_bundle import DesktopDiagnosticBundleService
from openkb.desktop_import import DesktopTextImportService
from openkb.desktop_knowledge_graph import (
    DesktopKnowledgeGraphQueryError,
    DesktopKnowledgeGraphService,
    local_graph_evidence_ids,
)
from openkb.desktop_model_gateway import DesktopModelGateway
from openkb.desktop_retrieval import DesktopEvidenceRetriever, _Candidate, _with_graph_budget
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime, desktop_state_dir
from openkb.locks import kb_ingest_lock


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
                    },
                    {
                        "id": concept_id,
                        "evidence_id": item["evidence_id"],
                        "type": "concept",
                        "label": "Deployment",
                    },
                    {
                        "id": claim_id,
                        "evidence_id": item["evidence_id"],
                        "type": "claim",
                        "label": item["text"],
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
                    },
                    {
                        "evidence_id": item["evidence_id"],
                        "source_id": concept_id,
                        "target_id": claim_id,
                        "type": "SUPPORTS",
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


def test_graph_budget_preserves_baseline_minimum_candidates():
    """A graph addition cannot displace the four protected baseline results."""
    baseline = tuple(_reference(f"base-{ordinal}") for ordinal in range(6))
    graph = (
        _Candidate(_reference("graph-only", channels=("knowledge_graph",)), "knowledge_graph", 1),
    )

    selected = _with_graph_budget(baseline, graph)

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
    assert ("extraction", "model_timeout") in diagnostics
    assert ("query", "knowledge_graph_query_timeout") in diagnostics


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
    def no_catalog_lease(_kb_dir):
        yield None

    monkeypatch.setattr(retrieval, "lease_current_catalog", no_catalog_lease)
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


def _graph_diagnostic_codes(kb_dir, destination_dir) -> set[str]:
    destination = destination_dir / "diagnostics.zip"
    DesktopDiagnosticBundleService(kb_dir).export(destination)
    with zipfile.ZipFile(destination) as archive:
        payload = json.loads(archive.read("graph-diagnostics.json"))
    return {diagnostic["error_code"] for diagnostic in payload["diagnostics"]}
