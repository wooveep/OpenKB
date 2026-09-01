"""PageTree Selection and immutable Retrieval Trace behavior."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import nullcontext

from openkb import desktop_model_transport
from openkb import desktop_retrieval as desktop_retrieval_module
from openkb.desktop_answer_types import DesktopEvidenceRef
from openkb.desktop_conversations import DesktopConversationService
from openkb.desktop_import_runner import DesktopTextImportService
from openkb.desktop_knowledge_navigation import (
    DesktopKnowledgeNavigationResult,
    _bounded_source_text,
)
from openkb.desktop_model_capability_store import DesktopModelCapabilityStore
from openkb.desktop_model_gateway import DesktopModelCancelledError, DesktopModelGateway
from openkb.desktop_model_operation_state import DesktopModelOperationContractStore
from openkb.desktop_model_settings import save_desktop_model_settings
from openkb.desktop_model_terminal import MODEL_CONNECT_TIMEOUT_SECONDS
from openkb.desktop_page_tree import (
    PageTreeEvidenceBinding,
    PageTreeGeneration,
    PageTreeNode,
)
from openkb.desktop_page_tree_selection import _selected_evidence_ids, select_page_tree_evidence
from openkb.desktop_prompt_contracts import prompt_contract_for
from openkb.desktop_retrieval import DesktopEvidenceRetriever
from openkb.desktop_retrieval_fusion import RetrievalCandidate, fuse_candidates
from openkb.desktop_retrieval_plan import deterministic_plan, model_plan, with_baseline_terms
from openkb.desktop_workspace import (
    DesktopKnowledgeBaseRuntime,
    desktop_state_database_path,
    desktop_state_dir,
)
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


def _covered_navigation_response(request):
    prompt = json.loads(request.content)
    evidence_ids = [item["evidence_id"] for item in prompt["evidence"]]
    return json.dumps(
        {
            "schema_version": "openkb.knowledge-navigation-step.v1",
            "snapshot_id": prompt["snapshot_id"],
            "objective": prompt["objective"],
            "coverage": [
                {
                    "aspect": aspect,
                    "status": "covered" if evidence_ids else "missing",
                    "evidence_ids": evidence_ids[:1],
                }
                for aspect in prompt["objective"]["required_aspects"]
            ],
            "actions": [],
            "decision": "stop",
        }
    )


def test_model_retrieval_plan_preserves_atomic_cjk_semantic_terms() -> None:
    plan = model_plan(
        "双节点超融合环境如何安装部署",
        '{"terms":["双节点","超融合","安装部署"]}',
    )

    assert plan.terms == ("双节点", "超融合", "安装部署")


def test_combined_retrieval_plan_prioritizes_model_semantics_and_keeps_baseline() -> None:
    question = "双节点超融合环境如何安装部署"
    baseline = deterministic_plan(question)
    model = model_plan(question, '{"terms":["双节点","超融合","安装部署"]}')

    combined = with_baseline_terms(baseline, model)

    assert combined.terms[:3] == model.terms
    assert set(baseline.terms) <= set(combined.terms)


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


def test_selected_page_tree_evidence_is_shared_across_selected_sections() -> None:
    root = PageTreeNode("root", None, 0, 0, "document", "Document", {})
    section_a = PageTreeNode(
        "section-a",
        "root",
        1,
        1,
        "section",
        "Section A",
        {},
        (PageTreeEvidenceBinding("evidence-a-0", 0),),
    )
    children_a = tuple(
        PageTreeNode(
            f"a-{ordinal}",
            "section-a",
            ordinal + 1,
            2,
            "paragraph",
            f"A {ordinal}",
            {},
            (PageTreeEvidenceBinding(f"evidence-a-{ordinal}", ordinal),),
        )
        for ordinal in range(1, 30)
    )
    section_b = PageTreeNode(
        "section-b",
        "root",
        31,
        1,
        "section",
        "Section B",
        {},
        (PageTreeEvidenceBinding("evidence-b-0", 30),),
    )
    children_b = tuple(
        PageTreeNode(
            f"b-{ordinal}",
            "section-b",
            ordinal + 31,
            2,
            "paragraph",
            f"B {ordinal}",
            {},
            (PageTreeEvidenceBinding(f"evidence-b-{ordinal}", ordinal + 30),),
        )
        for ordinal in range(1, 30)
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
        nodes=(root, section_a, *children_a, section_b, *children_b),
    )

    evidence_ids = _selected_evidence_ids(
        (tree,),
        (("document", ("section-a", "section-b")),),
    )

    assert evidence_ids[:4] == (
        "evidence-a-1",
        "evidence-b-1",
        "evidence-a-2",
        "evidence-b-2",
    )
    parent_evidence_ids = _selected_evidence_ids(
        (tree,),
        (("document", ("root",)),),
    )
    assert parent_evidence_ids[:4] == (
        "evidence-a-1",
        "evidence-b-1",
        "evidence-a-2",
        "evidence-b-2",
    )


def test_selected_page_tree_evidence_is_balanced_across_documents() -> None:
    def tree(document_id: str) -> PageTreeGeneration:
        root = PageTreeNode(
            f"{document_id}-root",
            None,
            0,
            0,
            "document",
            document_id,
            {},
        )
        sections = tuple(
            PageTreeNode(
                f"{document_id}-section-{ordinal}",
                root.node_id,
                ordinal * 2 - 1,
                1,
                "section",
                f"{document_id} section {ordinal}",
                {},
            )
            for ordinal in range(1, 31)
        )
        paragraphs = tuple(
            PageTreeNode(
                f"{document_id}-node-{ordinal}",
                f"{document_id}-section-{ordinal}",
                ordinal * 2,
                2,
                "paragraph",
                f"{document_id} node {ordinal}",
                {},
                (PageTreeEvidenceBinding(f"{document_id}-evidence-{ordinal}", ordinal),),
            )
            for ordinal in range(1, 31)
        )
        return PageTreeGeneration(
            generation_id=f"{document_id}-generation",
            document_version_id=document_id,
            provider_kind="test",
            provider_version="1",
            structural_ir_fingerprint=f"{document_id}-structure",
            locator_mapping_digest=f"{document_id}-locators",
            created_at="2026-08-20T00:00:00Z",
            status="ready",
            nodes=(
                root,
                *(node for pair in zip(sections, paragraphs, strict=True) for node in pair),
            ),
        )

    first = tree("first")
    second = tree("second")

    evidence_ids = _selected_evidence_ids(
        (first, second),
        (
            ("first", ("first-root",)),
            ("second", ("second-root",)),
        ),
    )

    assert evidence_ids[:4] == (
        "first-evidence-1",
        "second-evidence-1",
        "first-evidence-2",
        "second-evidence-2",
    )
    assert sum(value.startswith("first-") for value in evidence_ids) == 12
    assert sum(value.startswith("second-") for value in evidence_ids) == 12


def test_fusion_reserves_room_for_bounded_page_tree_routes() -> None:
    def candidate(evidence_id: str, channel: str, rank: int) -> RetrievalCandidate:
        return RetrievalCandidate(
            DesktopEvidenceRef(
                evidence_id=evidence_id,
                document_id="document",
                document_name="guide.md",
                section=evidence_id,
                locator={},
                excerpt=evidence_id,
                channels=(channel,),
            ),
            channel,
            rank,
        )

    protected = tuple(candidate(f"baseline-{rank}", "fts", rank) for rank in range(1, 5))
    routed = tuple(candidate(f"route-{rank}", "document_page_tree", rank) for rank in range(1, 13))

    evidence = fuse_candidates(
        (*protected, *routed),
        protected=protected,
        routed=routed,
    )
    evidence_ids = {reference.evidence_id for reference in evidence}

    assert {f"baseline-{rank}" for rank in range(1, 5)} <= evidence_ids
    assert {f"route-{rank}" for rank in range(1, 13)} <= evidence_ids


def test_fusion_does_not_protect_a_generic_fragment_over_substantive_evidence() -> None:
    def candidate(evidence_id: str, channel: str, rank: int, excerpt: str) -> RetrievalCandidate:
        return RetrievalCandidate(
            DesktopEvidenceRef(
                evidence_id=evidence_id,
                document_id="document",
                document_name="guide.md",
                section=evidence_id,
                locator={},
                excerpt=excerpt,
                channels=(channel,),
            ),
            channel,
            rank,
        )

    protected = (
        candidate("generic", "fts", 1, "【部署】"),
        candidate("fts-detail", "fts", 2, "上传安装包并完成节点初始化。"),
        candidate("structure-1", "structure_lexical", 1, "配置双节点管理高可用。"),
        candidate("structure-2", "structure_lexical", 2, "创建 GlusterFS 双副本卷。"),
        candidate("wiki-detail", "wiki", 1, "验证浮动 IP 可以正常漂移。"),
    )
    routed = tuple(
        candidate(
            f"route-{rank}",
            "document_page_tree",
            rank,
            f"部署步骤 {rank} 的完整说明。",
        )
        for rank in range(1, 13)
    )

    evidence = fuse_candidates(
        (*protected, *routed),
        protected=protected,
        routed=routed,
    )
    evidence_ids = {reference.evidence_id for reference in evidence}

    assert "generic" not in evidence_ids
    assert {"fts-detail", "structure-1", "structure-2", "wiki-detail"} <= evidence_ids
    assert {f"route-{rank}" for rank in range(1, 13)} <= evidence_ids


def test_fusion_keeps_the_bounded_source_window_for_a_duplicate_evidence_id() -> None:
    short = RetrievalCandidate(
        DesktopEvidenceRef(
            evidence_id="shared",
            document_id="document",
            document_name="guide.md",
            section="安装 / 分区",
            locator={},
            excerpt="仅设置系统盘。",
            channels=("structure_lexical",),
        ),
        "structure_lexical",
        1,
    )
    window = RetrievalCandidate(
        DesktopEvidenceRef(
            evidence_id="shared",
            document_id="document",
            document_name="guide.md",
            section="安装 / 分区",
            locator={},
            excerpt="仅设置系统盘；进入自定义分区并移除其他数据盘的勾选。",
            channels=("knowledge_navigation_source_window",),
        ),
        "knowledge_navigation_source_window",
        1,
    )

    evidence = fuse_candidates(
        (short, window),
        protected=(short,),
        routed=(window,),
    )

    assert evidence[0].excerpt == window.reference.excerpt
    assert evidence[0].channels == (
        "knowledge_navigation_source_window",
        "structure_lexical",
    )


def test_page_tree_selection_has_one_connect_bounded_routing_call(tmp_path) -> None:
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
    assert selection_calls[0][1] == MODEL_CONNECT_TIMEOUT_SECONDS
    prompt = json.loads(selection_calls[0][2])
    assert len(prompt["trees"]) <= 3
    assert "multi_hop" in pack.retrieval_trace.trigger_reasons
    assert pack.retrieval_trace.page_tree_generation_ids
    assert pack.retrieval_trace.selected_node_ids
    assert pack.retrieval_trace.canonical_evidence_ids == tuple(
        reference.evidence_id for reference in pack.evidence
    )
    assert pack.retrieval_trace.fusion_policy_version == "openkb.rrf-protected-baseline-routed.v3"
    assert any("document_page_tree" in reference.channels for reference in pack.evidence)
    page_tree_trace = next(
        channel
        for channel in pack.retrieval_trace.channels
        if channel.channel == "document_page_tree"
    )
    assert "multi_hop" in page_tree_trace.trigger_reasons
    assert page_tree_trace.degradation_reasons == ()


def test_page_tree_selection_repair_receives_the_exact_node_limit(tmp_path) -> None:
    kb_dir = _knowledge_base(tmp_path)
    baseline = DesktopEvidenceRetriever(kb_dir).retrieve("Compare Alpha and Beta")
    document_id = baseline.evidence[0].document_id
    root = PageTreeNode("root", None, 0, 0, "document", "Document", {})
    children = tuple(
        PageTreeNode(
            f"node-{ordinal}",
            "root",
            ordinal,
            1,
            "paragraph",
            f"Relevant node {ordinal}",
            {},
            (PageTreeEvidenceBinding(baseline.evidence[0].evidence_id, ordinal),),
        )
        for ordinal in range(1, 14)
    )
    tree = PageTreeGeneration(
        generation_id="generation",
        document_version_id=document_id,
        provider_kind="test",
        provider_version="1",
        structural_ir_fingerprint="structure",
        locator_mapping_digest="locators",
        created_at="2026-08-20T00:00:00Z",
        status="ready",
        nodes=(root, *children),
    )
    over_limit = {
        "selections": [
            {
                "document_id": document_id,
                "node_ids": [node.node_id for node in children],
            }
        ]
    }
    operations: list[str] = []

    def transport(request, _timeout_seconds):
        operations.append(request.operation)
        if request.operation == "page_tree_selection":
            return json.dumps(over_limit)
        repair = json.loads(request.content)
        validation_error = repair["validation_errors"][0]
        node_ids = children[:12] if "at most 12" in validation_error else children
        return json.dumps(
            {
                "selections": [
                    {
                        "document_id": document_id,
                        "node_ids": [node.node_id for node in node_ids],
                    }
                ]
            }
        )

    result = select_page_tree_evidence(
        kb_dir,
        "Compare Alpha and Beta",
        baseline.retrieval_plan,
        baseline.evidence,
        DesktopModelGateway(transport),
        lease_tree=lambda _kb_dir, _document_id: nullcontext(tree),
    )

    assert operations == ["page_tree_selection", "structured_output_repair"]
    assert result.degradation_reasons == ()
    assert result.selected_node_ids == tuple(node.node_id for node in children[:12])


def test_page_tree_selection_can_route_to_relevant_nodes_beyond_the_document_prefix(
    tmp_path,
) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "long-deployment-guide.md"
    source.write_text(
        "# OCloud deployment guide\n\n"
        + "\n\n".join(
            f"## Background {ordinal}\n\nUnrelated background detail {ordinal}."
            for ordinal in range(52)
        )
        + (
            "\n\n## 双节点超融合集群场景部署\n\n"
            "双节点安装需要配置管理高可用、Glusterfs 存储和资源池。\n"
        ),
        encoding="utf-8",
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    DesktopTextImportService(kb_dir).import_text(source)
    offered_titles: list[str] = []

    def transport(request, _timeout_seconds):
        if request.operation == "retrieval_plan":
            return '{"terms":["双节点","超融合","安装部署"]}'
        if request.operation == "page_tree_selection":
            prompt = json.loads(request.content)
            tree = prompt["trees"][0]
            offered_titles.extend(node["title"] for node in tree["nodes"])
            selected = [
                node["node_id"]
                for node in tree["nodes"]
                if "双节点超融合集群场景部署" in node["title"]
            ]
            return json.dumps(
                {
                    "selections": (
                        [{"document_id": tree["document_id"], "node_ids": selected}]
                        if selected
                        else []
                    )
                },
                ensure_ascii=False,
            )
        raise AssertionError(request.operation)

    pack = DesktopEvidenceRetriever(kb_dir, model_gateway=DesktopModelGateway(transport)).retrieve(
        "双节点超融合环境如何安装部署"
    )

    assert any("双节点超融合集群场景部署" in title for title in offered_titles)
    assert pack.retrieval_trace.selected_node_ids
    assert any("document_page_tree" in reference.channels for reference in pack.evidence)


def test_complex_how_to_navigation_replans_until_source_coverage_is_complete(tmp_path) -> None:
    kb_dir = tmp_path / "knowledge"
    install_source = tmp_path / "install.md"
    install_source.write_text(
        "# Alpha installation\n\nInstall Alpha on the primary host.\n",
        encoding="utf-8",
    )
    validation_source = tmp_path / "acceptance.md"
    validation_source.write_text(
        "# Omega acceptance\n\nRun the Omega acceptance checklist before handoff.\n",
        encoding="utf-8",
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    importer = DesktopTextImportService(kb_dir)
    importer.import_text(install_source)
    importer.import_text(validation_source)
    operations: list[str] = []
    navigation_round = 0

    def transport(request, _timeout_seconds):
        nonlocal navigation_round
        operations.append(request.operation)
        if request.operation == "retrieval_plan":
            return '{"terms":["Alpha","installation"]}'
        if request.operation == "page_tree_selection":
            return _selection_response(request)
        if request.operation == "knowledge_navigation_step":
            navigation_round += 1
            prompt = json.loads(request.content)
            evidence_ids = [item["evidence_id"] for item in prompt["evidence"]]
            assert evidence_ids
            coverage = [
                {
                    "aspect": aspect,
                    "status": "covered" if navigation_round > 1 else "missing",
                    "evidence_ids": evidence_ids[:1] if navigation_round > 1 else [],
                }
                for aspect in prompt["objective"]["required_aspects"]
            ]
            if navigation_round == 1:
                coverage[0] = {
                    "aspect": coverage[0]["aspect"],
                    "status": "covered",
                    "evidence_ids": evidence_ids[:1],
                }
            return json.dumps(
                {
                    "schema_version": "openkb.knowledge-navigation-step.v1",
                    "snapshot_id": prompt["snapshot_id"],
                    "objective": {
                        **prompt["objective"],
                        "subject": "Alpha installation",
                    },
                    "coverage": coverage,
                    "actions": (
                        [{"kind": "search_routes", "terms": ["Omega", "acceptance"]}]
                        if navigation_round == 1
                        else []
                    ),
                    "decision": "continue" if navigation_round == 1 else "stop",
                }
            )
        raise AssertionError(request.operation)

    pack = DesktopEvidenceRetriever(kb_dir, model_gateway=DesktopModelGateway(transport)).retrieve(
        "Alpha 如何安装"
    )

    assert operations.count("knowledge_navigation_step") == 2, (
        pack.degradations,
        (len(pack.evidence), {item.document_name for item in pack.evidence}),
        pack.retrieval_plan,
    )
    assert pack.retrieval_trace.navigation_round_count == 2
    assert pack.retrieval_trace.navigation_stop_reason == "covered"
    assert pack.retrieval_trace.navigation_answer_kind == "how_to"
    assert pack.retrieval_trace.coverage_aspects
    assert all(item.status == "covered" for item in pack.retrieval_trace.coverage_aspects)
    assert "search_routes" in pack.retrieval_trace.navigation_action_kinds
    assert "Omega" in pack.retrieval_plan.terms
    assert any("Omega acceptance checklist" in item.excerpt for item in pack.evidence)


def test_how_to_paraphrases_collect_the_same_cross_document_evidence(tmp_path) -> None:
    kb_dir = tmp_path / "knowledge"
    (tmp_path / "install.md").write_text(
        "# Alpha installation\n\nInstall Alpha on the primary host.\n",
        encoding="utf-8",
    )
    (tmp_path / "acceptance.md").write_text(
        "# Omega acceptance\n\nRun the Omega acceptance checklist before handoff.\n",
        encoding="utf-8",
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    importer = DesktopTextImportService(kb_dir)
    importer.import_text(tmp_path / "install.md")
    importer.import_text(tmp_path / "acceptance.md")

    def transport(request, _timeout_seconds):
        if request.operation == "retrieval_plan":
            return '{"terms":["Alpha","installation"]}'
        if request.operation == "page_tree_selection":
            return _selection_response(request)
        if request.operation == "knowledge_navigation_step":
            prompt = json.loads(request.content)
            evidence_ids = [item["evidence_id"] for item in prompt["evidence"]]
            final_round = prompt["round"] == 2
            coverage = [
                {
                    "aspect": aspect,
                    "status": "covered" if final_round else "missing",
                    "evidence_ids": evidence_ids[:1] if final_round else [],
                }
                for aspect in prompt["objective"]["required_aspects"]
            ]
            if not final_round:
                coverage[0] = {
                    "aspect": coverage[0]["aspect"],
                    "status": "covered",
                    "evidence_ids": evidence_ids[:1],
                }
            return json.dumps(
                {
                    "schema_version": "openkb.knowledge-navigation-step.v1",
                    "snapshot_id": prompt["snapshot_id"],
                    "objective": {**prompt["objective"], "subject": "Alpha installation"},
                    "coverage": coverage,
                    "actions": (
                        []
                        if final_round
                        else [{"kind": "search_routes", "terms": ["Omega", "acceptance"]}]
                    ),
                    "decision": "stop" if final_round else "continue",
                }
            )
        raise AssertionError(request.operation)

    retriever = DesktopEvidenceRetriever(kb_dir, model_gateway=DesktopModelGateway(transport))
    first = retriever.retrieve("Alpha 如何安装")
    second = retriever.retrieve("如何部署 Alpha")

    assert first.retrieval_trace.navigation_subject == "Alpha installation"
    assert second.retrieval_trace.navigation_subject == "Alpha installation"
    assert first.retrieval_trace.coverage_aspects == second.retrieval_trace.coverage_aspects
    assert {item.excerpt for item in first.evidence} == {item.excerpt for item in second.evidence}
    assert any("Omega acceptance checklist" in item.excerpt for item in second.evidence)


def test_navigation_cancellation_stops_before_follow_up_retrieval(tmp_path) -> None:
    kb_dir = _knowledge_base(tmp_path)
    cancelled = False
    operations: list[str] = []

    def transport(request, _timeout_seconds):
        nonlocal cancelled
        operations.append(request.operation)
        if request.operation == "retrieval_plan":
            return '{"terms":["Alpha","installation"]}'
        if request.operation == "page_tree_selection":
            return _selection_response(request)
        if request.operation == "knowledge_navigation_step":
            response = json.loads(_covered_navigation_response(request))
            response["coverage"] = [
                {
                    "aspect": item["aspect"],
                    "status": "partial",
                    "evidence_ids": item["evidence_ids"],
                }
                for item in response["coverage"]
            ]
            response["actions"] = [{"kind": "search_routes", "terms": ["Omega"]}]
            response["decision"] = "continue"
            cancelled = True
            return json.dumps(response)
        raise AssertionError(request.operation)

    pack = DesktopEvidenceRetriever(kb_dir, model_gateway=DesktopModelGateway(transport)).retrieve(
        "Alpha 如何安装",
        is_cancelled=lambda: cancelled,
    )

    assert operations == [
        "retrieval_plan",
        "page_tree_selection",
        "knowledge_navigation_step",
    ]
    assert pack.evidence
    assert pack.retrieval_plan.source != "adaptive"
    assert pack.retrieval_trace.navigation_stop_reason == "cancelled"
    assert "knowledge_navigation_step_cancelled" in pack.degradations


def test_navigation_rejects_a_repeated_action_in_a_later_round(tmp_path) -> None:
    kb_dir = tmp_path / "knowledge"
    install_source = tmp_path / "install.md"
    install_source.write_text("# Alpha\n\nInstall Alpha.\n", encoding="utf-8")
    supplement_source = tmp_path / "omega.md"
    supplement_source.write_text("# Omega\n\nValidate Omega.\n", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    importer = DesktopTextImportService(kb_dir)
    importer.import_text(install_source)
    importer.import_text(supplement_source)
    operations: list[str] = []

    def transport(request, _timeout_seconds):
        operations.append(request.operation)
        if request.operation == "retrieval_plan":
            return '{"terms":["Alpha","install"]}'
        if request.operation == "page_tree_selection":
            return _selection_response(request)
        if request.operation == "knowledge_navigation_step":
            prompt = json.loads(request.content)
            evidence_ids = [item["evidence_id"] for item in prompt["evidence"]]
            return json.dumps(
                {
                    "schema_version": "openkb.knowledge-navigation-step.v1",
                    "snapshot_id": prompt["snapshot_id"],
                    "objective": prompt["objective"],
                    "coverage": [
                        {
                            "aspect": aspect,
                            "status": "partial",
                            "evidence_ids": evidence_ids[:1],
                        }
                        for aspect in prompt["objective"]["required_aspects"]
                    ],
                    "actions": [{"kind": "search_routes", "terms": ["Omega"]}],
                    "decision": "continue",
                }
            )
        if request.operation == "structured_output_repair":
            return "{}"
        raise AssertionError(request.operation)

    pack = DesktopEvidenceRetriever(kb_dir, model_gateway=DesktopModelGateway(transport)).retrieve(
        "Alpha 如何安装"
    )

    assert operations.count("knowledge_navigation_step") == 2
    assert operations.count("structured_output_repair") == 1
    assert pack.retrieval_trace.navigation_round_count == 2
    assert pack.retrieval_trace.navigation_stop_reason == "model_degraded"
    assert "knowledge_navigation_step_invalid" in pack.degradations
    assert any("Validate Omega" in item.excerpt for item in pack.evidence)


def test_source_window_preserves_a_logical_section_without_slicing_blocks() -> None:
    section = json.dumps(["Deployment guide", "Node setup"])
    unrelated = json.dumps(["Deployment guide", "Appendix"])
    before = "B" * 7_000
    after = "C" * 7_000
    rows = [
        (0, "heading", "Appendix", unrelated),
        (1, "paragraph", "unrelated appendix " + "x" * 6_100, unrelated),
        (10, "heading", "Node setup", section),
        (11, "paragraph", "Prepare both hosts.", section),
        (12, "paragraph", before, section),
        (13, "code", "install-alpha --two-node", section),
        (14, "warning", "Do not select the cluster communication NIC.", section),
        (15, "paragraph", "Verify the VIP and replicated volume.", section),
        (16, "paragraph", after, section),
    ]

    excerpt = _bounded_source_text(rows, 13)

    assert "Node setup" in excerpt
    assert "install-alpha --two-node" in excerpt
    assert "Do not select the cluster communication NIC." in excerpt
    assert "Verify the VIP and replicated volume." in excerpt
    assert "unrelated appendix" not in excerpt
    assert (before in excerpt) != (after in excerpt)


def test_navigation_rejects_unsafe_actions_and_preserves_seed_evidence(tmp_path) -> None:
    kb_dir = _knowledge_base(tmp_path)
    operations: list[str] = []

    def transport(request, _timeout_seconds):
        operations.append(request.operation)
        if request.operation == "retrieval_plan":
            return '{"terms":["Alpha","安装"]}'
        if request.operation == "page_tree_selection":
            return _selection_response(request)
        if request.operation == "knowledge_navigation_step":
            prompt = json.loads(request.content)
            evidence_ids = [item["evidence_id"] for item in prompt["evidence"]]
            return json.dumps(
                {
                    "schema_version": "openkb.knowledge-navigation-step.v1",
                    "snapshot_id": prompt["snapshot_id"],
                    "objective": prompt["objective"],
                    "coverage": [
                        {
                            "aspect": aspect,
                            "status": "partial",
                            "evidence_ids": evidence_ids[:1],
                        }
                        for aspect in prompt["objective"]["required_aspects"]
                    ],
                    "actions": [{"kind": "read_file", "terms": ["/etc/passwd"]}],
                    "decision": "continue",
                }
            )
        if request.operation == "structured_output_repair":
            return "{}"
        raise AssertionError(request.operation)

    pack = DesktopEvidenceRetriever(kb_dir, model_gateway=DesktopModelGateway(transport)).retrieve(
        "Alpha 如何安装"
    )

    assert pack.evidence
    assert operations == [
        "retrieval_plan",
        "page_tree_selection",
        "knowledge_navigation_step",
        "structured_output_repair",
    ]
    assert "knowledge_navigation_step_invalid" in pack.degradations
    assert pack.retrieval_trace.navigation_stop_reason == "model_degraded"
    assert pack.retrieval_trace.navigation_action_kinds == ()


def test_navigation_stops_before_expansion_when_the_snapshot_changes(tmp_path) -> None:
    kb_dir = _knowledge_base(tmp_path)
    operations: list[str] = []

    def transport(request, _timeout_seconds):
        operations.append(request.operation)
        if request.operation == "retrieval_plan":
            return '{"terms":["Alpha","安装"]}'
        if request.operation == "page_tree_selection":
            return _selection_response(request)
        if request.operation == "knowledge_navigation_step":
            prompt = json.loads(request.content)
            evidence_ids = [item["evidence_id"] for item in prompt["evidence"]]
            with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
                connection.execute(
                    "UPDATE knowledge_catalog_state "
                    "SET source_revision = source_revision + 1 WHERE singleton = 1"
                )
                connection.commit()
            return json.dumps(
                {
                    "schema_version": "openkb.knowledge-navigation-step.v1",
                    "snapshot_id": prompt["snapshot_id"],
                    "objective": prompt["objective"],
                    "coverage": [
                        {
                            "aspect": aspect,
                            "status": "partial",
                            "evidence_ids": evidence_ids[:1],
                        }
                        for aspect in prompt["objective"]["required_aspects"]
                    ],
                    "actions": [{"kind": "search_routes", "terms": ["Omega"]}],
                    "decision": "continue",
                }
            )
        raise AssertionError(request.operation)

    pack = DesktopEvidenceRetriever(kb_dir, model_gateway=DesktopModelGateway(transport)).retrieve(
        "Alpha 如何安装"
    )

    assert operations == [
        "retrieval_plan",
        "page_tree_selection",
        "knowledge_navigation_step",
    ]
    assert pack.retrieval_plan.source != "adaptive"
    assert pack.retrieval_trace.navigation_stop_reason == "snapshot_degraded"
    assert "knowledge_navigation_snapshot_changed" in pack.degradations


def test_navigation_supplements_after_page_tree_evidence_is_known(tmp_path, monkeypatch) -> None:
    kb_dir = _knowledge_base(tmp_path)
    observed_baselines: list[tuple[DesktopEvidenceRef, ...]] = []

    def observe_navigation(
        _connection,
        *,
        catalog_generation_id,
        terms,
        baseline_evidence,
        max_reads,
        max_source_windows,
        excluded_routes,
    ) -> DesktopKnowledgeNavigationResult:
        del (
            catalog_generation_id,
            terms,
            max_reads,
            max_source_windows,
            excluded_routes,
        )
        observed_baselines.append(baseline_evidence)
        return DesktopKnowledgeNavigationResult()

    monkeypatch.setattr(
        desktop_retrieval_module,
        "build_knowledge_navigation_in",
        observe_navigation,
    )

    def transport(request, _timeout_seconds):
        if request.operation == "retrieval_plan":
            return '{"terms":["Alpha","Beta"]}'
        if request.operation == "page_tree_selection":
            return _selection_response(request)
        raise AssertionError(request.operation)

    pack = DesktopEvidenceRetriever(kb_dir, model_gateway=DesktopModelGateway(transport)).retrieve(
        "Compare Alpha and Beta across the routing layer"
    )

    assert pack.retrieval_trace.selected_node_ids
    assert observed_baselines
    assert any("document_page_tree" in reference.channels for reference in observed_baselines[0])


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
    assert pack.retrieval_trace.navigation_round_count == 0
    assert pack.retrieval_trace.navigation_stop_reason == "covered"
    assert pack.retrieval_trace.coverage_gate_state == "covered"


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


def test_invalid_page_tree_selection_suspends_only_its_operation(tmp_path, monkeypatch) -> None:
    kb_dir = _knowledge_base(tmp_path)
    save_desktop_model_settings(
        kb_dir,
        provider="deepseek",
        model="deepseek-v4-pro",
        api_base_url="https://api.deepseek.com",
        api_key="test-key",
        max_concurrent_model_calls=1,
    )

    calls: list[str] = []

    class InvalidTransport:
        def __init__(self, *, model, bundle):
            del model, bundle

        def __call__(self, request, _timeout_seconds):
            calls.append(request.operation)
            return "not-json"

    monkeypatch.setattr(desktop_model_transport, "DesktopLiteLLMTransport", InvalidTransport)
    gateway = desktop_model_transport.desktop_model_gateway_for(kb_dir)
    assert gateway is not None
    profile = gateway.execution_profile_for_operation("page_tree_selection")
    capability_store = DesktopModelCapabilityStore(kb_dir)
    capability_store.mark_verified(profile)
    baseline = DesktopEvidenceRetriever(kb_dir).retrieve("Compare Alpha and Beta")

    result = select_page_tree_evidence(
        kb_dir,
        "Compare Alpha and Beta",
        baseline.retrieval_plan,
        baseline.evidence,
        gateway,
    )

    assert result.degradation_reasons == ("page_tree_selection_invalid",)
    assert capability_store.state(profile).status == "verified"
    operation_state = DesktopModelOperationContractStore(kb_dir).state(
        operation="page_tree_selection",
        capability_identity=profile.capability_evidence_profile.identity,
        prompt_contract_digest=prompt_contract_for("page_tree_selection").digest,
    )
    assert operation_state.status == "suspended"
    assert operation_state.failure_code == "model_response_invalid"
    call_count = len(calls)

    blocked = select_page_tree_evidence(
        kb_dir,
        "Compare Alpha and Beta",
        baseline.retrieval_plan,
        baseline.evidence,
        gateway,
    )

    assert blocked.degradation_reasons == ("page_tree_selection_suspended",)
    assert len(calls) == call_count


def test_page_tree_selection_does_not_charge_when_provider_never_starts(tmp_path) -> None:
    kb_dir = _knowledge_base(tmp_path)

    class ExhaustedTransport:
        calls = 0

        def prepare_terminal_model_attempt(self, _is_cancelled):
            raise DesktopModelCancelledError()

        def __call__(self, _request, _timeout_seconds):
            self.calls += 1
            return "unreachable"

    transport = ExhaustedTransport()
    pack = DesktopEvidenceRetriever(kb_dir, model_gateway=DesktopModelGateway(transport)).retrieve(
        "Compare Alpha and Beta"
    )

    assert transport.calls == 0
    assert "page_tree_selection_cancelled" in pack.retrieval_trace.degradation_reasons
    assert pack.retrieval_model_cost.model_calls == 0
    assert pack.retrieval_model_cost.input_characters == 0


def test_query_rejects_a_noncurrent_page_tree_generation(tmp_path) -> None:
    kb_dir = _knowledge_base(tmp_path)
    operations: list[str] = []

    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        connection.execute(
            "UPDATE document_page_tree_generations SET status = 'superseded' "
            "WHERE generation_id IN (SELECT generation_id FROM document_page_tree_current)"
        )
        connection.commit()

    def transport(request, _timeout_seconds):
        operations.append(request.operation)
        if request.operation == "retrieval_plan":
            return '{"terms":["alpha","beta"]}'
        if request.operation == "knowledge_navigation_step":
            return _covered_navigation_response(request)
        raise AssertionError("A corrupt PageTree must not reach the selection model.")

    pack = DesktopEvidenceRetriever(kb_dir, model_gateway=DesktopModelGateway(transport)).retrieve(
        "Compare Alpha and Beta"
    )

    assert pack.evidence
    assert operations == ["retrieval_plan", "knowledge_navigation_step"]
    assert "page_tree_query_failed" in pack.retrieval_trace.degradation_reasons
    assert pack.retrieval_trace.page_tree_generation_ids == ()


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
