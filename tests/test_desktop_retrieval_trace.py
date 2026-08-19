"""PageTree Selection and immutable Retrieval Trace behavior."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator

from openkb.desktop_conversations import DesktopConversationService
from openkb.desktop_import_runner import DesktopTextImportService
from openkb.desktop_model_gateway import DesktopModelGateway
from openkb.desktop_page_tree import (
    PageTreeEvidenceBinding,
    PageTreeGeneration,
    PageTreeNode,
)
from openkb.desktop_page_tree_selection import _selected_evidence_ids
from openkb.desktop_retrieval import DesktopEvidenceRetriever
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime, desktop_state_dir
from openkb.locks import kb_ingest_lock


def _knowledge_base(tmp_path):
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "routing.md"
    source.write_text(
        "# Alpha\n\nAlpha detail facts remain in original evidence.\n\n"
        "## Beta\n\nBeta relates to Alpha through the routing layer.\n",
        encoding="utf-8",
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    DesktopTextImportService(kb_dir).import_text(source)
    return kb_dir


def _selection_response(request):
    prompt = json.loads(request.content)
    tree = prompt["trees"][0]
    node = next(item for item in tree["nodes"] if item["depth"] > 0)
    return json.dumps(
        {"selections": [{"document_id": tree["document_id"], "node_ids": [node["node_id"]]}]}
    )


def test_selected_page_tree_subtree_walks_large_tree_once() -> None:
    class CountingNodes(tuple[PageTreeNode, ...]):
        iterations = 0

        def __iter__(self) -> Iterator[PageTreeNode]:
            self.iterations += 1
            return super().__iter__()

    root_id = "root"
    nodes = CountingNodes(
        [PageTreeNode(root_id, None, 0, 0, "document", "Document", {})]
        + [
            PageTreeNode(
                f"node-{ordinal}",
                root_id,
                ordinal,
                1,
                "paragraph",
                f"Node {ordinal}",
                {},
                (PageTreeEvidenceBinding(f"evidence-{ordinal}", ordinal),),
            )
            for ordinal in range(1, 2_001)
        ]
    )
    tree = PageTreeGeneration(
        generation_id="generation",
        document_version_id="document",
        provider_kind="test",
        provider_version="1",
        structural_ir_fingerprint="structure",
        locator_mapping_digest="locators",
        created_at="2026-08-20T00:00:00Z",
        status="ready",
        nodes=nodes,
    )

    evidence_ids = _selected_evidence_ids((tree,), (("document", (root_id,)),))

    assert evidence_ids == tuple(f"evidence-{ordinal}" for ordinal in range(1, 25))
    assert nodes.iterations <= 3


def test_page_tree_selection_is_one_bounded_routing_call(tmp_path) -> None:
    kb_dir = _knowledge_base(tmp_path)
    calls: list[tuple[str, float, str]] = []

    def transport(request, timeout_seconds):
        calls.append((request.operation, timeout_seconds, request.content))
        if request.operation == "retrieval_plan":
            return '{"terms":["alpha","beta","routing"]}'
        if request.operation == "page_tree_selection":
            return _selection_response(request)
        raise AssertionError(request.operation)

    pack = DesktopEvidenceRetriever(kb_dir, model_gateway=DesktopModelGateway(transport)).retrieve(
        "Compare Alpha and Beta across the routing layer"
    )

    selection_calls = [call for call in calls if call[0] == "page_tree_selection"]
    assert len(selection_calls) == 1
    assert 19.9 < selection_calls[0][1] <= 20.0
    prompt = json.loads(selection_calls[0][2])
    assert len(prompt["trees"]) <= 3
    assert "multi_hop" in pack.retrieval_trace.trigger_reasons
    assert pack.retrieval_trace.page_tree_generation_ids
    assert pack.retrieval_trace.selected_node_ids
    assert pack.retrieval_trace.canonical_evidence_ids == tuple(
        reference.evidence_id for reference in pack.evidence
    )
    assert pack.retrieval_trace.fusion_policy_version == "openkb.rrf-protected-baseline.v1"
    assert any("document_page_tree" in reference.channels for reference in pack.evidence)
    page_tree_trace = next(
        channel
        for channel in pack.retrieval_trace.channels
        if channel.channel == "document_page_tree"
    )
    assert "multi_hop" in page_tree_trace.trigger_reasons
    assert page_tree_trace.degradation_reasons == ()


def test_simple_question_skips_page_tree_selection(tmp_path) -> None:
    kb_dir = _knowledge_base(tmp_path)
    operations: list[str] = []

    def transport(request, _timeout_seconds):
        operations.append(request.operation)
        return '{"terms":["alpha"]}'

    pack = DesktopEvidenceRetriever(kb_dir, model_gateway=DesktopModelGateway(transport)).retrieve(
        "Alpha"
    )

    assert operations == ["retrieval_plan"]
    assert pack.evidence
    assert pack.retrieval_trace.page_tree_generation_ids == ()
    assert pack.retrieval_trace.selected_node_ids == ()


def test_page_tree_selection_failure_keeps_deterministic_baseline(tmp_path) -> None:
    kb_dir = _knowledge_base(tmp_path)
    operations: list[str] = []

    def transport(request, _timeout_seconds):
        operations.append(request.operation)
        if request.operation == "retrieval_plan":
            return '{"terms":["alpha","beta"]}'
        raise ConnectionError("selection transport unavailable")

    pack = DesktopEvidenceRetriever(kb_dir, model_gateway=DesktopModelGateway(transport)).retrieve(
        "Compare Alpha and Beta"
    )

    assert pack.evidence
    assert operations.count("page_tree_selection") == 1
    assert "page_tree_selection_failed" in pack.retrieval_trace.degradation_reasons
    assert not any("document_page_tree" in reference.channels for reference in pack.evidence)
    page_tree_trace = next(
        channel
        for channel in pack.retrieval_trace.channels
        if channel.channel == "document_page_tree"
    )
    assert "multi_hop" in page_tree_trace.trigger_reasons
    assert "page_tree_selection_failed" in page_tree_trace.degradation_reasons


def test_conversation_trace_survives_page_tree_generation_cleanup(tmp_path) -> None:
    kb_dir = _knowledge_base(tmp_path)

    def transport(request, _timeout_seconds):
        if request.operation == "retrieval_plan":
            return '{"terms":["alpha","beta"]}'
        if request.operation == "page_tree_selection":
            return _selection_response(request)
        if request.operation == "grounded_answer":
            return "Alpha and Beta are related by the routing layer. [1]"
        raise AssertionError(request.operation)

    service = DesktopConversationService(kb_dir, model_gateway=DesktopModelGateway(transport))
    conversation_id = service.create()["conversation_id"]
    created = service.ask(conversation_id, "Compare Alpha and Beta")
    version = created["messages"][-1]["answer_versions"][0]
    trace = version["retrieval_trace"]
    assert trace["page_tree_generation_ids"]
    assert version["citations"]

    with kb_ingest_lock(desktop_state_dir(kb_dir)):
        with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("DELETE FROM document_page_tree_current")
            connection.execute("DELETE FROM document_page_tree_generations")

    restored = service.get(conversation_id)
    restored_version = restored["messages"][-1]["answer_versions"][0]
    assert restored_version["retrieval_trace"] == trace
    assert restored_version["citations"] == version["citations"]
