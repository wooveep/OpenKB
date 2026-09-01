"""PageTree Selection and immutable Retrieval Trace behavior."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import nullcontext
from dataclasses import replace

from openkb import desktop_model_transport
from openkb import desktop_retrieval as desktop_retrieval_module
from openkb.desktop_adaptive_navigation import current_navigation_snapshot_id
from openkb.desktop_answer_types import (
    DesktopEvidencePack,
    DesktopEvidenceRef,
    DesktopKnowledgeRouteOption,
)
from openkb.desktop_conversations import DesktopConversationService
from openkb.desktop_import_runner import DesktopTextImportService
from openkb.desktop_knowledge_inventory import eligible_knowledge_routes_in
from openkb.desktop_knowledge_navigation import (
    DesktopKnowledgeNavigationResult,
    build_knowledge_navigation_in,
)
from openkb.desktop_model_capability_store import DesktopModelCapabilityStore
from openkb.desktop_model_gateway import DesktopModelCancelledError, DesktopModelGateway
from openkb.desktop_model_operation_state import DesktopModelOperationContractStore
from openkb.desktop_model_settings import save_desktop_model_settings
from openkb.desktop_model_terminal import MODEL_CONNECT_TIMEOUT_SECONDS
from openkb.desktop_navigation_session import run_navigation_session
from openkb.desktop_navigation_validation import (
    NavigationAction,
    validated_navigation_actions,
)
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
from openkb.desktop_retrieval_trace import DesktopAnswerCoverageTrace
from openkb.desktop_source_sections import bounded_source_text, source_section_evidence_in
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


def test_navigation_prompt_advertises_unread_bounded_route_options(tmp_path) -> None:
    kb_dir = _knowledge_base(tmp_path)
    offered = DesktopKnowledgeRouteOption(
        route="generated/procedure/omega",
        kind="procedure",
        title="Omega acceptance",
    )
    observed_available_routes: list[str] = []

    def transport(request, _timeout_seconds):
        if request.operation == "retrieval_plan":
            return '{"terms":["Alpha","installation"]}'
        if request.operation == "page_tree_selection":
            return _selection_response(request)
        if request.operation == "knowledge_navigation_step":
            prompt = json.loads(request.content)
            observed_available_routes.extend(item["route"] for item in prompt["available_routes"])
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
                    "actions": [
                        {
                            "kind": "read_routes",
                            "aspect": prompt["objective"]["required_aspects"][0],
                            "routes": [offered.route],
                        }
                    ],
                    "decision": "continue",
                }
            )
        if request.operation == "structured_output_repair":
            raise AssertionError("An advertised route must validate without repair.")
        raise AssertionError(request.operation)

    gateway = DesktopModelGateway(transport)
    retriever = DesktopEvidenceRetriever(kb_dir, model_gateway=gateway)
    initial = retriever.retrieve_variant(
        "Alpha 如何安装",
        variant="baseline",
        _enable_page_tree_selection=True,
        _enable_navigation=True,
    )
    seeded = DesktopEvidencePack(
        retrieval_plan=initial.retrieval_plan,
        evidence=initial.evidence,
        degradations=initial.degradations,
        source_images=initial.source_images,
        retrieval_trace=initial.retrieval_trace,
        retrieval_model_cost=initial.retrieval_model_cost,
        guidance=initial.guidance,
        route_options=(*initial.route_options, offered),
    )

    # Exercise the public session seam with a route that is visible but has not been read.
    from openkb.desktop_adaptive_navigation import current_navigation_snapshot_id
    from openkb.desktop_navigation_session import run_navigation_session

    run_navigation_session(
        kb_dir=kb_dir,
        database_path=desktop_state_database_path(kb_dir),
        question="Alpha 如何安装",
        pinned_snapshot_id=current_navigation_snapshot_id(desktop_state_database_path(kb_dir)),
        initial_pack=seeded,
        model_gateway=gateway,
        retrieve_round=lambda **_kwargs: seeded,
    )

    assert offered.route in observed_available_routes


def test_navigation_prompt_does_not_advertise_an_already_read_route(tmp_path) -> None:
    kb_dir = _knowledge_base(tmp_path)
    already_read = DesktopKnowledgeRouteOption(
        route="generated/procedure/alpha",
        kind="procedure",
        title="Alpha procedure",
    )
    offered = DesktopKnowledgeRouteOption(
        route="generated/procedure/omega",
        kind="procedure",
        title="Omega procedure",
    )
    observed_available_routes: list[str] = []

    def transport(request, _timeout_seconds):
        if request.operation == "retrieval_plan":
            return '{"terms":["Alpha","installation"]}'
        if request.operation == "page_tree_selection":
            return _selection_response(request)
        if request.operation == "knowledge_navigation_step":
            prompt = json.loads(request.content)
            observed_available_routes.extend(item["route"] for item in prompt["available_routes"])
            return _covered_navigation_response(request)
        raise AssertionError(request.operation)

    gateway = DesktopModelGateway(transport)
    initial = DesktopEvidenceRetriever(kb_dir, model_gateway=gateway).retrieve_variant(
        "Alpha 如何安装",
        variant="baseline",
        _enable_page_tree_selection=True,
        _enable_navigation=True,
    )
    seeded = replace(
        initial,
        route_options=(already_read, offered),
        retrieval_trace=replace(
            initial.retrieval_trace,
            navigation_routes=(already_read.route,),
        ),
    )

    from openkb.desktop_adaptive_navigation import current_navigation_snapshot_id
    from openkb.desktop_navigation_session import run_navigation_session

    run_navigation_session(
        kb_dir=kb_dir,
        database_path=desktop_state_database_path(kb_dir),
        question="Alpha 如何安装",
        pinned_snapshot_id=current_navigation_snapshot_id(desktop_state_database_path(kb_dir)),
        initial_pack=seeded,
        model_gateway=gateway,
        retrieve_round=lambda **_kwargs: seeded,
    )

    assert observed_available_routes == [offered.route]


def test_navigation_rejects_route_outside_advertised_slice(tmp_path) -> None:
    kb_dir = _knowledge_base(tmp_path)
    advertised = tuple(
        DesktopKnowledgeRouteOption(
            route=f"generated/procedure/offered-{ordinal:02d}",
            kind="procedure",
            title=f"Offered {ordinal:02d}",
        )
        for ordinal in range(24)
    )
    hidden = DesktopKnowledgeRouteOption(
        route="generated/procedure/hidden-24",
        kind="procedure",
        title="Hidden route",
    )
    observed_available_routes: list[str] = []
    repair_calls = 0

    def transport(request, _timeout_seconds):
        nonlocal repair_calls
        if request.operation == "retrieval_plan":
            return '{"terms":["Alpha","installation"]}'
        if request.operation == "page_tree_selection":
            return _selection_response(request)
        if request.operation == "knowledge_navigation_step":
            prompt = json.loads(request.content)
            observed_available_routes.extend(item["route"] for item in prompt["available_routes"])
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
                    "actions": [
                        {
                            "kind": "read_routes",
                            "aspect": prompt["objective"]["required_aspects"][0],
                            "routes": [hidden.route],
                        }
                    ],
                    "decision": "continue",
                }
            )
        if request.operation == "structured_output_repair":
            repair_calls += 1
            return "{}"
        raise AssertionError(request.operation)

    gateway = DesktopModelGateway(transport)
    retriever = DesktopEvidenceRetriever(kb_dir, model_gateway=gateway)
    initial = retriever.retrieve_variant(
        "Alpha 如何安装",
        variant="baseline",
        _enable_page_tree_selection=True,
        _enable_navigation=True,
    )
    seeded = DesktopEvidencePack(
        retrieval_plan=initial.retrieval_plan,
        evidence=initial.evidence,
        degradations=initial.degradations,
        source_images=initial.source_images,
        retrieval_trace=initial.retrieval_trace,
        retrieval_model_cost=initial.retrieval_model_cost,
        guidance=initial.guidance,
        route_options=(*advertised, hidden),
    )

    from openkb.desktop_adaptive_navigation import current_navigation_snapshot_id
    from openkb.desktop_navigation_session import run_navigation_session

    run_navigation_session(
        kb_dir=kb_dir,
        database_path=desktop_state_database_path(kb_dir),
        question="Alpha 如何安装",
        pinned_snapshot_id=current_navigation_snapshot_id(desktop_state_database_path(kb_dir)),
        initial_pack=seeded,
        model_gateway=gateway,
        retrieve_round=lambda **_kwargs: seeded,
    )

    assert hidden.route not in observed_available_routes
    assert repair_calls == 1


def test_navigation_repair_receives_the_exact_invalid_object_fields(tmp_path) -> None:
    kb_dir = _knowledge_base(tmp_path)
    validation_errors: list[str] = []

    def transport(request, _timeout_seconds):
        if request.operation == "retrieval_plan":
            return '{"terms":["Alpha","installation"]}'
        if request.operation == "page_tree_selection":
            return _selection_response(request)
        if request.operation == "knowledge_navigation_step":
            prompt = json.loads(request.content)
            invalid_objective = {**prompt["objective"], "invented_field": "discard me"}
            response = json.loads(_covered_navigation_response(request))
            response["objective"] = invalid_objective
            return json.dumps(response)
        if request.operation == "structured_output_repair":
            repair = json.loads(request.content)
            validation_errors.extend(repair["validation_errors"])
            source_request = type(request)(
                operation="knowledge_navigation_step",
                document_name=request.document_name,
                content=repair["evidence_bound_source_material"],
            )
            return _covered_navigation_response(source_request)
        raise AssertionError(request.operation)

    pack = DesktopEvidenceRetriever(kb_dir, model_gateway=DesktopModelGateway(transport)).retrieve(
        "Alpha 如何安装"
    )

    assert pack.evidence
    assert validation_errors == [
        "Navigation objective fields are invalid: missing=[], unexpected=['invented_field']."
    ]


def test_navigation_observation_exposes_distinct_sections_before_repeated_blocks() -> None:
    from openkb.desktop_adaptive_navigation import _diverse_evidence

    repeated = tuple(
        DesktopEvidenceRef(
            evidence_id=f"repeated-{ordinal}",
            document_id="deployment-guide",
            document_name="Deployment Guide",
            section="Compute node configuration",
            locator={"block_ordinal": ordinal},
            excerpt=f"Repeated block {ordinal}",
            channels=("test",),
        )
        for ordinal in range(24)
    )
    distinct = (
        DesktopEvidenceRef(
            evidence_id="storage-outline",
            document_id="deployment-guide",
            document_name="Deployment Guide",
            section="Hyper-converged storage",
            locator={"block_ordinal": 24},
            excerpt="Bcache and GlusterFS setup outline",
            channels=("test",),
        ),
        DesktopEvidenceRef(
            evidence_id="database-outline",
            document_id="deployment-guide",
            document_name="Deployment Guide",
            section="Database high availability",
            locator={"block_ordinal": 25},
            excerpt="MySQL replication outline",
            channels=("test",),
        ),
    )

    selected = _diverse_evidence((*repeated, *distinct), maximum=24)

    assert [item.evidence_id for item in selected[:3]] == [
        "repeated-0",
        "storage-outline",
        "database-outline",
    ]
    assert len(selected) == 24


def test_navigation_read_selection_spends_one_logical_read_per_unique_route() -> None:
    from openkb.desktop_knowledge_navigation import _ReadDescriptor, _select_read_descriptors

    def descriptor(*, score: int, authority_id: str, route: str) -> _ReadDescriptor:
        return _ReadDescriptor(
            score=score,
            hop=0,
            descriptor_kind="catalog",
            authority="published_generation",
            authority_id=authority_id,
            kind="procedure",
            title="Install storage",
            metadata_json="{}",
            route=route,
            snapshot_token=authority_id,
        )

    selected = _select_read_descriptors(
        (
            descriptor(score=120, authority_id="newer", route="generated/procedure/storage"),
            descriptor(score=110, authority_id="older", route="generated/procedure/storage"),
            descriptor(score=100, authority_id="network", route="generated/procedure/network"),
        ),
        max_reads=2,
        excluded_routes=frozenset(),
    )

    assert [item.route for item in selected] == [
        "generated/procedure/storage",
        "generated/procedure/network",
    ]


def test_navigation_read_selection_reserves_relevant_summaries_and_source_outlines() -> None:
    from openkb.desktop_knowledge_navigation import _ReadDescriptor, _select_read_descriptors

    def descriptor(
        *, score: int, authority_id: str, route: str, descriptor_kind: str = "catalog"
    ) -> _ReadDescriptor:
        return _ReadDescriptor(
            score=score,
            hop=0,
            descriptor_kind=descriptor_kind,
            authority={
                "summary": "document_summary",
                "source": "source_document",
            }.get(descriptor_kind, "published_generation"),
            authority_id=authority_id,
            kind=descriptor_kind if descriptor_kind in {"summary", "source"} else "procedure",
            title=authority_id,
            metadata_json="{}",
            route=route,
            snapshot_token=authority_id,
        )

    selected = _select_read_descriptors(
        (
            *(
                descriptor(
                    score=200 - ordinal,
                    authority_id=f"procedure-{ordinal}",
                    route=f"generated/procedure/{ordinal}",
                )
                for ordinal in range(8)
            ),
            descriptor(
                score=90,
                authority_id="installation-guide",
                route="summaries/installation-guide",
                descriptor_kind="summary",
            ),
            descriptor(
                score=80,
                authority_id="deployment-guide",
                route="summaries/deployment-guide",
                descriptor_kind="summary",
            ),
            descriptor(
                score=70,
                authority_id="installation-guide",
                route="sources/installation-guide",
                descriptor_kind="source",
            ),
            descriptor(
                score=60,
                authority_id="deployment-guide",
                route="sources/deployment-guide",
                descriptor_kind="source",
            ),
        ),
        max_reads=8,
        excluded_routes=frozenset(),
    )

    assert {item.route for item in selected if item.descriptor_kind == "summary"} == {
        "summaries/installation-guide",
        "summaries/deployment-guide",
    }
    assert {item.route for item in selected if item.descriptor_kind == "source"} == {
        "sources/installation-guide",
        "sources/deployment-guide",
    }
    assert len(selected) == 8


def test_navigation_pairs_a_relevant_summary_with_its_source_outline(tmp_path, monkeypatch) -> None:
    kb_dir = _knowledge_base(tmp_path)
    from openkb.desktop_knowledge_navigation import _inventory_descriptor

    monkeypatch.setattr(
        "openkb.desktop_knowledge_navigation._summary_descriptors_in",
        lambda _connection, terms, _baseline, inventory: tuple(
            _inventory_descriptor(item, terms)
            for item in inventory
            if item.authority == "document_summary"
        )[:1],
    )
    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        catalog_generation_id = str(
            connection.execute(
                "SELECT current_generation_id FROM knowledge_catalog_state WHERE singleton = 1"
            ).fetchone()[0]
        )
        result = build_knowledge_navigation_in(
            connection,
            catalog_generation_id=catalog_generation_id,
            terms=("alpha", "beta"),
            baseline_evidence=(),
            max_reads=4,
            max_source_windows=2,
        )

    assert any(read.authority == "document_summary" for read in result.reads)
    assert any(read.authority == "source_document" for read in result.reads)


def test_navigation_source_ranking_covers_routes_before_extra_units(monkeypatch) -> None:
    from openkb.desktop_knowledge_navigation import (
        _GuidanceUnit,
        _NavigationRead,
        _rank_source_evidence_ids_in,
    )

    monkeypatch.setattr(
        "openkb.desktop_knowledge_navigation._source_relevance_in",
        lambda _connection, evidence_id, _terms: (
            1,
            False,
            "Deployment / Network" if evidence_id.startswith("network") else "Deployment / Storage",
            "deployment-guide",
        ),
    )
    reads = (
        _NavigationRead(
            route="generated/procedure/storage",
            kind="procedure",
            authority="published_generation",
            title="Storage",
            units=(
                _GuidanceUnit("Prepare storage", ("storage-prepare",)),
                _GuidanceUnit("Create storage", ("storage-create",)),
                _GuidanceUnit("Validate storage", ("storage-validate",)),
            ),
            hop=0,
            snapshot_token="storage",
        ),
        _NavigationRead(
            route="generated/procedure/network",
            kind="procedure",
            authority="published_generation",
            title="Network",
            units=(_GuidanceUnit("Configure network", ("network-configure",)),),
            hop=0,
            snapshot_token="network",
        ),
    )

    ranked = _rank_source_evidence_ids_in(
        object(),  # type: ignore[arg-type]
        reads,
        terms=("deployment",),
        baseline_ids=frozenset(),
    )

    assert ranked[:2] == ("storage-prepare", "network-configure")


def test_navigation_source_ranking_covers_phases_before_extra_phase_blocks(
    monkeypatch,
) -> None:
    from openkb.desktop_knowledge_navigation import (
        _GuidanceUnit,
        _NavigationRead,
        _rank_source_evidence_ids_in,
    )

    monkeypatch.setattr(
        "openkb.desktop_knowledge_navigation._source_relevance_in",
        lambda _connection, evidence_id, _terms: (
            (2 if evidence_id.startswith("keepalived") else 1),
            False,
            (
                "Deployment / Keepalived"
                if evidence_id.startswith("keepalived")
                else "Installation / Partitioning"
            ),
            "deployment-guide",
        ),
    )
    read = _NavigationRead(
        route="summaries/deployment-guide",
        kind="summary",
        authority="document_summary",
        title="Deployment Guide",
        units=(
            _GuidanceUnit("Deploy Keepalived", ("keepalived-install",)),
            _GuidanceUnit("Configure Keepalived", ("keepalived-config",)),
            _GuidanceUnit("Install with custom partitioning", ("partition",)),
        ),
        hop=0,
        snapshot_token="summary",
    )

    ranked = _rank_source_evidence_ids_in(
        object(),  # type: ignore[arg-type]
        (read,),
        terms=("deploy", "install"),
        baseline_ids=frozenset(),
    )

    assert ranked == ("keepalived-install", "partition", "keepalived-config")


def test_navigation_can_expand_a_baseline_anchor_that_may_not_survive_fusion(
    monkeypatch,
) -> None:
    from openkb.desktop_knowledge_navigation import (
        _GuidanceUnit,
        _NavigationRead,
        _rank_source_evidence_ids_in,
    )

    monkeypatch.setattr(
        "openkb.desktop_knowledge_navigation._source_relevance_in",
        lambda _connection, _evidence_id, _terms: (
            2,
            False,
            "Installation / Partitioning",
            "installation-guide",
        ),
    )
    read = _NavigationRead(
        route="generated/concept/partitioning",
        kind="concept",
        authority="published_generation",
        title="Cluster partitioning",
        units=(
            _GuidanceUnit("Configure only the system disk", ("baseline-anchor",)),
            _GuidanceUnit("Delete swap", ("new-anchor",)),
        ),
        hop=0,
        snapshot_token="partitioning",
    )

    ranked = _rank_source_evidence_ids_in(
        object(),  # type: ignore[arg-type]
        (read,),
        terms=("installation",),
        baseline_ids=frozenset(("baseline-anchor",)),
    )

    assert ranked == ("new-anchor", "baseline-anchor")


def test_navigation_reserves_the_strongest_heading_per_matching_document() -> None:
    from openkb.desktop_knowledge_navigation import _structural_anchor_evidence_ids

    evidence = (
        DesktopEvidenceRef(
            evidence_id="detail",
            document_id="deployment-guide",
            document_name="Deployment Guide",
            section="四、超融合集群场景部署 / 2.GlusterFS / 2.1 Bcache安装",
            locator={"block_ordinal": 20},
            excerpt="运行 install.sh 安装 Bcache。",
            channels=("document_page_tree",),
        ),
        DesktopEvidenceRef(
            evidence_id="chapter-heading",
            document_id="deployment-guide",
            document_name="Deployment Guide",
            section="四、超融合集群场景部署（分布式复制卷）",
            locator={"block_ordinal": 10},
            excerpt="四、超融合集群场景部署（分布式复制卷）",
            channels=("document_page_tree",),
        ),
        DesktopEvidenceRef(
            evidence_id="nested-heading",
            document_id="installation-guide",
            document_name="Installation Guide",
            section="服务端安装 / 仅计算节点 / 集群场景（超融合，双节点安装部署）",
            locator={"block_ordinal": 30},
            excerpt="集群场景（超融合，双节点安装部署）",
            channels=("document_page_tree",),
        ),
        DesktopEvidenceRef(
            evidence_id="appendix-heading",
            document_id="deployment-guide",
            document_name="Deployment Guide",
            section="附录 / 修订记录",
            locator={"block_ordinal": 90},
            excerpt="修订记录",
            channels=("document_page_tree",),
        ),
    )

    anchors = _structural_anchor_evidence_ids(
        evidence,
        terms=("双节点", "超融合", "安装部署"),
    )

    assert anchors == ("nested-heading", "chapter-heading")


def test_navigation_uses_a_broad_page_tree_section_with_a_descriptive_excerpt() -> None:
    from openkb.desktop_knowledge_navigation import _structural_anchor_evidence_ids

    evidence = (
        DesktopEvidenceRef(
            evidence_id="chapter-summary",
            document_id="deployment-guide",
            document_name="Deployment Guide",
            section="四、超融合集群场景部署（分布式复制卷）",
            locator={"block_ordinal": 10},
            excerpt="本章节描述 GlusterFS 存储、管理节点主备高可用及业务使用。",
            channels=("document_page_tree",),
        ),
        DesktopEvidenceRef(
            evidence_id="appendix-heading",
            document_id="deployment-guide",
            document_name="Deployment Guide",
            section="附录 / 超融合恢复",
            locator={"block_ordinal": 90},
            excerpt="超融合恢复",
            channels=("document_page_tree",),
        ),
    )

    anchors = _structural_anchor_evidence_ids(
        evidence,
        terms=("双节点", "超融合", "安装部署"),
    )

    assert anchors == ("chapter-summary",)


def test_navigation_source_window_exposes_phase_outline_before_phase_details() -> None:
    from openkb.desktop_knowledge_navigation import _phase_diverse_source_window

    def reference(evidence_id: str, section: str, excerpt: str) -> DesktopEvidenceRef:
        return DesktopEvidenceRef(
            evidence_id=evidence_id,
            document_id="deployment-guide",
            document_name="Deployment Guide",
            section=section,
            locator={},
            excerpt=excerpt,
            channels=("knowledge_navigation_source_window",),
        )

    reordered = _phase_diverse_source_window(
        (
            reference(
                "keepalived-heading",
                "1. Keepalived",
                "Configure Keepalived on both management nodes before testing VIP failover.",
            ),
            reference("keepalived-command", "1. Keepalived", "install-keepalived"),
            reference("keepalived-validation", "1. Keepalived", "verify-vip"),
            reference(
                "mysql-heading",
                "2. MySQL",
                "Configure MySQL primary-primary replication on both management nodes.",
            ),
            reference("mysql-command", "2. MySQL", "start-replication"),
            reference(
                "bcache-heading",
                "3. Bcache",
                "Install Bcache on every storage node before creating the replicated volume.",
            ),
        )
    )

    assert [item.evidence_id for item in reordered] == [
        "keepalived-heading",
        "mysql-heading",
        "bcache-heading",
        "keepalived-command",
        "keepalived-validation",
        "mysql-command",
    ]


def test_navigation_source_window_surfaces_shallow_phases_before_nested_details() -> None:
    from openkb.desktop_knowledge_navigation import _phase_diverse_source_window

    def reference(evidence_id: str, section: str) -> DesktopEvidenceRef:
        return DesktopEvidenceRef(
            evidence_id=evidence_id,
            document_id="deployment-guide",
            document_name="Deployment Guide",
            section=section,
            locator={},
            excerpt=evidence_id,
            channels=("knowledge_navigation_source_window",),
        )

    reordered = _phase_diverse_source_window(
        (
            reference("chapter", "超融合集群场景部署"),
            reference("management", "超融合集群场景部署 / 1. 管理节点"),
            reference(
                "keepalived",
                "超融合集群场景部署 / 1. 管理节点 / Keepalived",
            ),
            reference("mysql", "超融合集群场景部署 / 1. 管理节点 / MySQL"),
            reference(
                "mysql-config",
                "超融合集群场景部署 / 1. 管理节点 / MySQL / my.cnf",
            ),
            reference("storage", "超融合集群场景部署 / 2. GlusterFS 存储"),
            reference(
                "bcache",
                "超融合集群场景部署 / 2. GlusterFS 存储 / Bcache",
            ),
        )
    )

    assert [item.evidence_id for item in reordered[:7]] == [
        "chapter",
        "management",
        "storage",
        "keepalived",
        "mysql",
        "bcache",
        "mysql-config",
    ]


def test_navigation_source_window_reserves_procedure_checkpoints_before_deep_outline() -> None:
    from openkb.desktop_knowledge_navigation import _phase_diverse_source_window

    def reference(evidence_id: str, section: str, excerpt: str) -> DesktopEvidenceRef:
        return DesktopEvidenceRef(
            evidence_id=evidence_id,
            document_id="deployment-guide",
            document_name="Deployment Guide",
            section=section,
            locator={},
            excerpt=excerpt,
            channels=("knowledge_navigation_source_window",),
        )

    reordered = _phase_diverse_source_window(
        (
            reference("chapter", "Deployment", "Deployment"),
            reference("system", "Deployment / System", "Install the operating system."),
            reference(
                "both-nodes",
                "Deployment / System / Installation mode",
                "Both nodes must select the integrated installation mode.",
            ),
            reference(
                "partition",
                "Deployment / System / Partitioning",
                "Configure the system disk.",
            ),
            reference(
                "swap-warning",
                "Deployment / System / Partitioning",
                "Important: delete swap.",
            ),
            reference(
                "home-capacity",
                "Deployment / System / Partitioning",
                "Then add the deleted swap capacity to /home and complete partitioning.",
            ),
            reference("network", "Deployment / Network", "Create the business network."),
            reference(
                "nic-warning",
                "Deployment / Network",
                "Warning: do not select the cluster communication NIC.",
            ),
            reference("ha", "Deployment / Management HA", "Configure management HA."),
            reference(
                "keepalived",
                "Deployment / Management HA / Keepalived",
                "Install Keepalived.",
            ),
            reference(
                "failover",
                "Deployment / Management HA / Failover test",
                "Test failover and confirm that the VIP moves to the peer node.",
            ),
        )
    )

    evidence_ids = [item.evidence_id for item in reordered]
    assert evidence_ids[:4] == ["chapter", "system", "network", "ha"]
    assert evidence_ids.index("both-nodes") < evidence_ids.index("keepalived")
    assert evidence_ids.index("nic-warning") < evidence_ids.index("keepalived")
    assert evidence_ids.index("failover") < evidence_ids.index("keepalived")
    assert evidence_ids.index("home-capacity") < evidence_ids.index("keepalived")


def test_navigation_source_window_does_not_let_many_shallow_phases_crowd_out_safety() -> None:
    from openkb.desktop_knowledge_navigation import _phase_diverse_source_window

    def reference(evidence_id: str, section: str, excerpt: str) -> DesktopEvidenceRef:
        return DesktopEvidenceRef(
            evidence_id=evidence_id,
            document_id="deployment-guide",
            document_name="Deployment Guide",
            section=section,
            locator={},
            excerpt=excerpt,
            channels=("knowledge_navigation_source_window",),
        )

    phases = tuple(
        reference(f"phase-{ordinal}", f"Deployment / Phase {ordinal}", f"Phase {ordinal}.")
        for ordinal in range(1, 11)
    )
    reordered = _phase_diverse_source_window(
        (
            reference("chapter", "Deployment", "Deployment"),
            *phases,
            reference(
                "safety",
                "Deployment / Phase 1 / Safety",
                "Warning: do not continue until prerequisites pass.",
            ),
            reference(
                "deep-routine",
                "Deployment / Phase 1 / Routine",
                "Continue with routine configuration.",
            ),
        )
    )

    evidence_ids = [item.evidence_id for item in reordered]
    assert evidence_ids.index("safety") < evidence_ids.index("phase-8")
    assert evidence_ids.index("safety") < evidence_ids.index("deep-routine")


def test_navigation_source_window_defers_unrequested_expansion_checkpoint() -> None:
    from openkb.desktop_knowledge_navigation import _phase_diverse_source_window

    def reference(evidence_id: str, section: str, excerpt: str) -> DesktopEvidenceRef:
        return DesktopEvidenceRef(
            evidence_id=evidence_id,
            document_id="deployment-guide",
            document_name="Deployment Guide",
            section=section,
            locator={},
            excerpt=excerpt,
            channels=("knowledge_navigation_source_window",),
        )

    reordered = _phase_diverse_source_window(
        (
            reference("chapter", "Deployment", "Deployment"),
            reference("management", "Deployment / Management HA", "Configure management HA."),
            reference("expansion", "Deployment / Expansion", "Expansion"),
            reference(
                "keepalived",
                "Deployment / Management HA / Keepalived",
                "Both nodes install Keepalived.",
            ),
            reference(
                "expansion-both",
                "Deployment / Expansion / Add hosts",
                "Both nodes create the expansion path.",
            ),
        ),
        terms=("dual-node", "deployment"),
    )

    evidence_ids = [item.evidence_id for item in reordered]
    assert evidence_ids.index("keepalived") < evidence_ids.index("expansion-both")


def test_navigation_source_outline_uses_first_substantive_block_per_phase() -> None:
    from openkb.desktop_knowledge_navigation import _phase_diverse_source_window

    def reference(evidence_id: str, section: str, excerpt: str) -> DesktopEvidenceRef:
        return DesktopEvidenceRef(
            evidence_id=evidence_id,
            document_id="deployment-guide",
            document_name="Deployment Guide",
            section=section,
            locator={},
            excerpt=excerpt,
            channels=("knowledge_navigation_source_window",),
        )

    reordered = _phase_diverse_source_window(
        (
            reference("keepalived-heading", "1. Keepalived", "1. Keepalived"),
            reference("keepalived-scope", "1. Keepalived", "两台主机均需执行"),
            reference(
                "keepalived-install",
                "1. Keepalived",
                "在两台主机上传并安装 keepalived.tar.gz。",
            ),
            reference("mysql-heading", "2. MySQL", "2. MySQL"),
            reference(
                "mysql-config",
                "2. MySQL",
                "两台主机分别配置不同的 server-id。",
            ),
        )
    )

    assert [item.evidence_id for item in reordered] == [
        "keepalived-install",
        "mysql-config",
        "keepalived-scope",
        "keepalived-heading",
        "mysql-heading",
    ]


def test_navigation_source_window_defers_images_behind_substantive_steps() -> None:
    from openkb.desktop_knowledge_navigation import _phase_diverse_source_window

    def reference(evidence_id: str, excerpt: str) -> DesktopEvidenceRef:
        return DesktopEvidenceRef(
            evidence_id=evidence_id,
            document_id="installation-guide",
            document_name="Installation Guide",
            section="System installation / Partitioning",
            locator={},
            excerpt=excerpt,
            channels=("knowledge_navigation_source_window",),
        )

    reordered = _phase_diverse_source_window(
        (
            reference("partition", "Only configure the system disk."),
            reference("screenshot", "image42.png"),
            reference("swap", "Delete swap and add its capacity to /home."),
        )
    )

    assert [item.evidence_id for item in reordered] == [
        "partition",
        "swap",
        "screenshot",
    ]


def test_navigation_merge_preserves_prior_phase_diversity_from_dense_supplement() -> None:
    from openkb.desktop_navigation_session import (
        NAVIGATION_MAX_EVIDENCE_REFS,
        _allocate_evidence,
    )

    def reference(evidence_id: str, section: str) -> DesktopEvidenceRef:
        return DesktopEvidenceRef(
            evidence_id=evidence_id,
            document_id="deployment-guide",
            document_name="Deployment Guide",
            section=section,
            locator={},
            excerpt=evidence_id,
            channels=("knowledge_navigation_source_window",),
        )

    current = tuple(
        reference(f"phase-{ordinal}", f"部署流程 / {ordinal}. 阶段") for ordinal in range(1, 13)
    )
    supplement = tuple(
        reference(f"bcache-detail-{ordinal}", "部署流程 / Bcache / 安装")
        for ordinal in range(1, 33)
    )

    evidence = _allocate_evidence(current, supplement, ())
    evidence_ids = {item.evidence_id for item in evidence}

    assert len(evidence) == NAVIGATION_MAX_EVIDENCE_REFS
    assert {f"phase-{ordinal}" for ordinal in range(1, 13)} <= evidence_ids


def test_navigation_merge_preserves_prior_ordered_steps_from_dense_supplement() -> None:
    from openkb.desktop_navigation_session import _allocate_evidence

    def reference(evidence_id: str, section: str) -> DesktopEvidenceRef:
        return DesktopEvidenceRef(
            evidence_id=evidence_id,
            document_id="deployment-guide",
            document_name="Deployment Guide",
            section=section,
            locator={},
            excerpt=evidence_id,
            channels=("knowledge_navigation_source_window",),
        )

    current = tuple(
        reference(f"partition-step-{ordinal}", "Installation / Partitioning")
        for ordinal in range(1, 5)
    )
    supplement = tuple(
        reference(f"supplement-{ordinal}", f"Deployment / Phase {ordinal}")
        for ordinal in range(1, 33)
    )

    evidence = _allocate_evidence(current, supplement, ())

    assert {item.evidence_id for item in current} <= {item.evidence_id for item in evidence}


def test_navigation_coverage_bindings_cannot_erase_the_stable_prior_outline() -> None:
    from openkb.desktop_navigation_session import _allocate_evidence

    def reference(evidence_id: str, section: str) -> DesktopEvidenceRef:
        return DesktopEvidenceRef(
            evidence_id=evidence_id,
            document_id="deployment-guide",
            document_name="Deployment Guide",
            section=section,
            locator={},
            excerpt=evidence_id,
            channels=("knowledge_navigation_source_window",),
        )

    current = tuple(
        reference(f"prior-{ordinal}", f"Deployment / Core phase {ordinal}") for ordinal in range(40)
    )
    supplement = tuple(
        reference(f"recovery-{ordinal}", f"Deployment / Recovery detail {ordinal}")
        for ordinal in range(40)
    )
    coverage = (
        DesktopAnswerCoverageTrace(
            "ordered_actions",
            "partial",
            tuple(item.evidence_id for item in supplement),
        ),
    )

    evidence = _allocate_evidence(current, supplement, coverage)
    evidence_ids = {item.evidence_id for item in evidence}

    assert {item.evidence_id for item in current[:35]} <= evidence_ids
    assert supplement[0].evidence_id in evidence_ids


def test_navigation_merge_lets_routed_evidence_displace_fts_only_seed_noise() -> None:
    from openkb.desktop_navigation_session import (
        NAVIGATION_MAX_EVIDENCE_REFS,
        _allocate_evidence,
    )

    def reference(
        evidence_id: str,
        section: str,
        channels: tuple[str, ...],
    ) -> DesktopEvidenceRef:
        return DesktopEvidenceRef(
            evidence_id=evidence_id,
            document_id="deployment-guide",
            document_name="Deployment Guide",
            section=section,
            locator={},
            excerpt=evidence_id,
            channels=channels,
        )

    current = tuple(
        reference(f"weak-{ordinal}", f"Unrelated install {ordinal}", ("fts",))
        for ordinal in range(NAVIGATION_MAX_EVIDENCE_REFS)
    )
    routed = reference(
        "network-warning",
        "Cluster deployment / Network",
        ("knowledge_navigation_source_window",),
    )

    evidence = _allocate_evidence(current, (routed,), ())

    assert routed in evidence
    assert len(evidence) == NAVIGATION_MAX_EVIDENCE_REFS


def test_navigation_merge_demotes_scope_mismatched_catalog_seed_noise() -> None:
    from openkb.desktop_navigation_session import (
        NAVIGATION_MAX_EVIDENCE_REFS,
        _allocate_evidence,
    )

    def reference(
        evidence_id: str,
        section: str,
        channels: tuple[str, ...],
    ) -> DesktopEvidenceRef:
        return DesktopEvidenceRef(
            evidence_id=evidence_id,
            document_id="deployment-guide",
            document_name="Deployment Guide",
            section=section,
            locator={},
            excerpt=evidence_id,
            channels=channels,
        )

    scoped = tuple(
        reference(
            f"cluster-{ordinal}",
            f"Alpha cluster deployment / Phase {ordinal}",
            ("catalog", "knowledge_source"),
        )
        for ordinal in range(NAVIGATION_MAX_EVIDENCE_REFS - 1)
    )
    unrelated = reference(
        "teacher-client-install",
        "Teacher client / Installation",
        ("catalog", "knowledge_source"),
    )
    routed = reference(
        "cluster-validation",
        "Alpha cluster deployment / Validation",
        ("knowledge_navigation_source_window",),
    )

    evidence = _allocate_evidence(
        (*scoped, unrelated),
        (routed,),
        (),
        terms=("Alpha", "installation"),
    )

    evidence_ids = {item.evidence_id for item in evidence}
    assert routed.evidence_id in evidence_ids
    assert unrelated.evidence_id not in evidence_ids
    assert {item.evidence_id for item in scoped} <= evidence_ids


def test_navigation_merge_reserves_new_evidence_for_uncovered_aspects() -> None:
    from openkb.desktop_navigation_session import _allocate_evidence

    def reference(evidence_id: str, section: str) -> DesktopEvidenceRef:
        return DesktopEvidenceRef(
            evidence_id=evidence_id,
            document_id="deployment-guide",
            document_name="Deployment Guide",
            section=section,
            locator={},
            excerpt=evidence_id,
            channels=("knowledge_navigation_source_window",),
        )

    current = tuple(
        reference(f"install-{ordinal}", "Deployment / Installation") for ordinal in range(32)
    )
    supplement = (
        reference("storage-recovery", "Deployment / Storage"),
        reference("validation-recovery", "Deployment / Validation"),
    )
    coverage = (
        DesktopAnswerCoverageTrace(
            "ordered_actions",
            "covered",
            tuple(item.evidence_id for item in current),
        ),
        DesktopAnswerCoverageTrace("commands_or_configuration", "missing"),
        DesktopAnswerCoverageTrace("validation", "missing"),
    )

    evidence = _allocate_evidence(
        current,
        supplement,
        coverage,
        aspect_evidence_ids={
            "commands_or_configuration": ("storage-recovery",),
            "validation": ("validation-recovery",),
        },
    )

    assert {"storage-recovery", "validation-recovery"} <= {item.evidence_id for item in evidence}


def test_last_round_observation_retains_partial_aspect_coverage() -> None:
    from openkb.desktop_navigation_session import _bind_observed_aspect_evidence

    coverage = (
        DesktopAnswerCoverageTrace("ordered_actions", "missing"),
        DesktopAnswerCoverageTrace("validation", "covered", ("validation-existing",)),
    )

    updated = _bind_observed_aspect_evidence(
        coverage,
        {
            "ordered_actions": ("install-step", "install-step"),
            "validation": ("validation-new",),
        },
    )

    assert updated == (
        DesktopAnswerCoverageTrace("ordered_actions", "partial", ("install-step",)),
        DesktopAnswerCoverageTrace("validation", "covered", ("validation-existing",)),
    )


def test_navigation_source_budget_never_admits_one_oversized_block() -> None:
    from openkb.desktop_adaptive_navigation import NAVIGATION_MAX_SOURCE_TOKENS
    from openkb.desktop_navigation_session import _allocate_evidence

    oversized = DesktopEvidenceRef(
        evidence_id="oversized",
        document_id="deployment-guide",
        document_name="Deployment Guide",
        section="Deployment / Huge appendix",
        locator={},
        excerpt="x" * (NAVIGATION_MAX_SOURCE_TOKENS * 4 + 1),
        channels=("knowledge_navigation_source_window",),
    )
    usable = replace(
        oversized,
        evidence_id="usable",
        section="Deployment / Validation",
        excerpt="validate the cluster",
    )

    evidence = _allocate_evidence((), (oversized, usable), ())

    assert tuple(item.evidence_id for item in evidence) == ("usable",)


def test_bounded_seed_preserves_an_already_qualified_retrieval_order() -> None:
    from openkb.desktop_navigation_session import _bounded_initial_pack

    first = DesktopEvidenceRef(
        evidence_id="first",
        document_id="deployment-guide",
        document_name="Deployment Guide",
        section="Deployment / Installation",
        locator={},
        excerpt="Install the first node.",
        channels=("structure_lexical",),
    )
    duplicate_section = replace(
        first,
        evidence_id="duplicate-section",
        excerpt="Install the second node.",
    )
    different_section = replace(
        first,
        evidence_id="different-section",
        section="Deployment / Validation",
        excerpt="Validate both nodes.",
    )

    bounded, reduced = _bounded_initial_pack(
        DesktopEvidencePack(
            retrieval_plan=deterministic_plan("How should both nodes be installed?"),
            evidence=(first, duplicate_section, different_section),
        )
    )

    assert tuple(item.evidence_id for item in bounded.evidence) == (
        "first",
        "duplicate-section",
        "different-section",
    )
    assert not reduced


def test_production_retrieve_bounds_the_deterministic_seed_source_budget(
    tmp_path, monkeypatch
) -> None:
    from openkb.desktop_adaptive_navigation import NAVIGATION_MAX_SOURCE_TOKENS

    kb_dir = _knowledge_base(tmp_path)
    question = "How should Alpha be installed and validated?"
    oversized = DesktopEvidenceRef(
        evidence_id="oversized-seed",
        document_id="routing",
        document_name="routing.md",
        section="Install / Appendix",
        locator={},
        excerpt="x" * (NAVIGATION_MAX_SOURCE_TOKENS * 4 + 1),
        channels=("structure_lexical",),
    )
    usable = replace(
        oversized,
        evidence_id="usable-seed",
        section="Install / Validation",
        excerpt="Validate that Alpha is online.",
    )
    seed = DesktopEvidencePack(
        retrieval_plan=deterministic_plan(question),
        evidence=(oversized, usable),
    )
    monkeypatch.setattr(
        DesktopEvidenceRetriever,
        "retrieve_variant",
        lambda _self, _question, **_kwargs: seed,
    )

    pack = DesktopEvidenceRetriever(kb_dir).retrieve(question)

    assert tuple(item.evidence_id for item in pack.evidence) == ("usable-seed",)
    assert pack.retrieval_trace.navigation_source_tokens <= NAVIGATION_MAX_SOURCE_TOKENS
    assert "knowledge_navigation_source_budget_exhausted" in pack.degradations


def test_production_navigation_wall_budget_starts_before_seed_retrieval(
    tmp_path, monkeypatch
) -> None:
    import time as system_time
    from types import SimpleNamespace

    kb_dir = _knowledge_base(tmp_path)
    question = "How should Alpha be installed?"
    seed = DesktopEvidencePack(
        retrieval_plan=deterministic_plan(question),
        evidence=(
            DesktopEvidenceRef(
                evidence_id="seed",
                document_id="routing",
                document_name="routing.md",
                section="Install",
                locator={},
                excerpt="Install Alpha.",
                channels=("structure_lexical",),
            ),
        ),
    )
    monkeypatch.setattr(
        DesktopEvidenceRetriever,
        "retrieve_variant",
        lambda _self, _question, **_kwargs: seed,
    )
    monkeypatch.setattr(
        desktop_retrieval_module,
        "time",
        SimpleNamespace(monotonic=lambda: system_time.monotonic() - 121.0),
        raising=False,
    )

    pack = DesktopEvidenceRetriever(kb_dir).retrieve(question)

    assert pack.retrieval_trace.navigation_round_count == 0
    assert pack.retrieval_trace.navigation_stop_reason == "budget_exhausted"


def test_production_navigation_closes_a_hung_seed_model_at_its_wall_deadline(
    tmp_path, monkeypatch
) -> None:
    import time as system_time

    kb_dir = _knowledge_base(tmp_path)
    provider_closed = threading.Event()
    release_provider = threading.Event()
    observed_timeouts: list[float | None] = []

    class BlockingProvider:
        def __call__(self, request, _connect_timeout_seconds):
            observed_timeouts.append(request.response_timeout_seconds)
            release_provider.wait()
            return '{"terms":["Alpha"]}'

        def cancel_active_stream(self, _request):
            provider_closed.set()
            release_provider.set()
            return True

    monkeypatch.setattr(desktop_retrieval_module, "NAVIGATION_MAX_WALL_SECONDS", 0.1)
    started_at = system_time.monotonic()
    pack = DesktopEvidenceRetriever(
        kb_dir, model_gateway=DesktopModelGateway(BlockingProvider())
    ).retrieve("How should Alpha be installed?")
    elapsed = system_time.monotonic() - started_at

    assert elapsed < 0.75
    assert provider_closed.is_set()
    assert observed_timeouts and 0 < observed_timeouts[0] <= 0.1
    assert pack.retrieval_trace.navigation_stop_reason == "budget_exhausted"


def test_requested_source_windows_round_robin_across_missing_phases() -> None:
    from openkb.desktop_knowledge_navigation import _round_robin_source_windows

    def reference(evidence_id: str, section: str) -> DesktopEvidenceRef:
        return DesktopEvidenceRef(
            evidence_id=evidence_id,
            document_id="deployment-guide",
            document_name="Deployment Guide",
            section=section,
            locator={},
            excerpt=evidence_id,
            channels=("knowledge_navigation_source_window",),
        )

    keepalived = tuple(reference(f"keepalived-{ordinal}", "Keepalived") for ordinal in range(1, 4))
    gluster = tuple(reference(f"gluster-{ordinal}", "GlusterFS") for ordinal in range(1, 3))

    evidence = _round_robin_source_windows((keepalived, gluster))

    assert [item.evidence_id for item in evidence] == [
        "keepalived-1",
        "gluster-1",
        "keepalived-2",
        "gluster-2",
        "keepalived-3",
    ]


def test_source_window_round_robin_deduplicates_shared_evidence() -> None:
    from openkb.desktop_knowledge_navigation import _round_robin_source_windows

    def reference(evidence_id: str, section: str) -> DesktopEvidenceRef:
        return DesktopEvidenceRef(
            evidence_id=evidence_id,
            document_id="deployment-guide",
            document_name="Deployment Guide",
            section=section,
            locator={},
            excerpt=evidence_id,
            channels=("knowledge_navigation_source_window",),
        )

    shared = reference("shared", "Deployment")
    evidence = _round_robin_source_windows(
        (
            (shared, reference("phase-a", "Deployment / Phase A")),
            (shared, reference("phase-b", "Deployment / Phase B")),
        )
    )

    assert [item.evidence_id for item in evidence] == [
        "shared",
        "phase-a",
        "phase-b",
    ]


def test_seed_source_windows_drop_a_detail_already_covered_by_a_chapter() -> None:
    from openkb.desktop_knowledge_navigation import _consolidate_seed_source_windows

    def reference(evidence_id: str, section: str) -> DesktopEvidenceRef:
        return DesktopEvidenceRef(
            evidence_id=evidence_id,
            document_id="deployment-guide",
            document_name="Deployment Guide",
            section=section,
            locator={},
            excerpt=evidence_id,
            channels=("knowledge_navigation_source_window",),
        )

    chapter = (
        reference("chapter", "Hyper-converged deployment"),
        reference("chapter-keepalived", "Hyper-converged deployment / Keepalived"),
        reference("chapter-storage", "Hyper-converged deployment / Storage"),
    )
    keepalived_detail = (
        reference("keepalived-install", "Hyper-converged deployment / Keepalived"),
        reference("keepalived-config", "Hyper-converged deployment / Keepalived"),
    )
    system_partition = (
        reference("system-disk", "System installation / Partitioning"),
        reference("swap", "System installation / Partitioning"),
    )

    consolidated = _consolidate_seed_source_windows((chapter, keepalived_detail, system_partition))

    assert consolidated == (chapter, system_partition)

    root_only = (reference("install-root", "System installation"),)
    assert _consolidate_seed_source_windows((root_only, system_partition)) == (
        root_only,
        system_partition,
    )

    outline_only = (
        reference("install-root", "System installation"),
        replace(
            reference("partition-heading", "System installation / Partitioning"),
            excerpt="Partitioning",
        ),
    )
    assert _consolidate_seed_source_windows((outline_only, system_partition)) == (
        outline_only,
        system_partition,
    )


def test_seed_source_windows_round_robin_across_knowledge_reads(tmp_path, monkeypatch) -> None:
    kb_dir = _knowledge_base(tmp_path)

    def reference(evidence_id: str, section: str) -> DesktopEvidenceRef:
        return DesktopEvidenceRef(
            evidence_id=evidence_id,
            document_id="deployment-guide",
            document_name="Deployment Guide",
            section=section,
            locator={},
            excerpt=evidence_id,
            channels=("knowledge_navigation_source_window",),
        )

    windows = {
        "phase-a": (
            reference("phase-a-1", "Deployment / Phase A"),
            reference("phase-a-2", "Deployment / Phase A"),
        ),
        "phase-b": (
            reference("phase-b-1", "Deployment / Phase B"),
            reference("phase-b-2", "Deployment / Phase B"),
        ),
    }
    monkeypatch.setattr(
        "openkb.desktop_knowledge_navigation._rank_source_evidence_ids_in",
        lambda *_args, **_kwargs: ("phase-a", "phase-b"),
    )
    monkeypatch.setattr(
        "openkb.desktop_knowledge_navigation.source_section_evidence_in",
        lambda _connection, evidence_id, **_kwargs: windows[evidence_id],
    )

    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        catalog_generation_id = str(
            connection.execute(
                "SELECT current_generation_id FROM knowledge_catalog_state WHERE singleton = 1"
            ).fetchone()[0]
        )
        result = build_knowledge_navigation_in(
            connection,
            catalog_generation_id=catalog_generation_id,
            terms=("alpha", "install"),
            baseline_evidence=(),
            max_reads=2,
            max_source_windows=2,
        )

    assert [item.evidence_id for item in result.source_windows] == [
        "phase-a-1",
        "phase-b-1",
        "phase-a-2",
        "phase-b-2",
    ]


def test_seed_source_windows_prefer_ranked_wiki_phase_over_generic_tree_anchor(
    tmp_path, monkeypatch
) -> None:
    kb_dir = _knowledge_base(tmp_path)

    def reference(evidence_id: str, section: str) -> DesktopEvidenceRef:
        return DesktopEvidenceRef(
            evidence_id=evidence_id,
            document_id="deployment-guide",
            document_name="Deployment Guide",
            section=section,
            locator={},
            excerpt=evidence_id,
            channels=("knowledge_navigation_source_window",),
        )

    broad = reference("broad", "Installation")
    semantic = reference("semantic", "Installation / Cluster partitioning")
    monkeypatch.setattr(
        "openkb.desktop_knowledge_navigation._structural_anchor_evidence_ids",
        lambda *_args, **_kwargs: ("broad",),
    )
    monkeypatch.setattr(
        "openkb.desktop_knowledge_navigation._rank_source_evidence_ids_in",
        lambda *_args, **_kwargs: ("semantic",),
    )
    monkeypatch.setattr(
        "openkb.desktop_knowledge_navigation.source_section_evidence_in",
        lambda _connection, evidence_id, **_kwargs: {
            "broad": (broad,),
            "semantic": (semantic,),
        }[evidence_id],
    )

    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        catalog_generation_id = str(
            connection.execute(
                "SELECT current_generation_id FROM knowledge_catalog_state WHERE singleton = 1"
            ).fetchone()[0]
        )
        result = build_knowledge_navigation_in(
            connection,
            catalog_generation_id=catalog_generation_id,
            terms=("cluster", "partitioning", "install"),
            baseline_evidence=(broad,),
            max_reads=2,
            max_source_windows=1,
        )

    assert tuple(item.evidence_id for item in result.source_windows) == ("semantic",)


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


def test_fusion_balances_navigation_source_outline_with_page_tree_evidence() -> None:
    def candidate(
        evidence_id: str,
        channel: str,
        rank: int,
        *,
        weight: float = 1.0,
    ) -> RetrievalCandidate:
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
            weight,
        )

    protected = tuple(candidate(f"baseline-{rank}", "fts", rank) for rank in range(1, 5))
    source_outline = tuple(
        candidate(f"source-phase-{rank}", "knowledge_navigation_source_window", rank)
        for rank in range(1, 35)
    )
    page_tree = tuple(
        candidate(f"tree-{rank}", "document_page_tree", rank, weight=10.0) for rank in range(1, 13)
    )

    evidence = fuse_candidates(
        (*protected, *source_outline, *page_tree),
        protected=protected,
        routed=(*source_outline, *page_tree),
    )
    evidence_ids = {reference.evidence_id for reference in evidence}

    assert len(evidence) <= 40
    assert {f"source-phase-{rank}" for rank in range(1, 35)} <= evidence_ids
    assert {f"tree-{rank}" for rank in range(1, 3)} <= evidence_ids


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
                        [
                            {
                                "kind": "search_routes",
                                "aspect": coverage[1]["aspect"],
                                "terms": ["Omega", "acceptance"],
                            }
                        ]
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
                        else [
                            {
                                "kind": "search_routes",
                                "aspect": coverage[1]["aspect"],
                                "terms": ["Omega", "acceptance"],
                            }
                        ]
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


def test_navigation_discards_a_repeated_action_in_a_later_round(tmp_path) -> None:
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
                    "actions": [
                        {
                            "kind": "search_routes",
                            "aspect": prompt["objective"]["required_aspects"][0],
                            "terms": ["Omega"],
                        }
                    ],
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
    assert operations.count("structured_output_repair") == 0
    assert pack.retrieval_trace.navigation_round_count == 2
    assert pack.retrieval_trace.navigation_stop_reason == "partial"
    assert "knowledge_navigation_step_invalid" not in pack.degradations
    assert any("Validate Omega" in item.excerpt for item in pack.evidence)


def test_navigation_normalizes_an_empty_continue_to_an_evidence_safe_stop(tmp_path) -> None:
    kb_dir = _knowledge_base(tmp_path)
    operations: list[str] = []

    def transport(request, _timeout_seconds):
        operations.append(request.operation)
        if request.operation == "retrieval_plan":
            return '{"terms":["Alpha","installation"]}'
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
                    "actions": [],
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
    assert pack.retrieval_trace.navigation_stop_reason == "partial"
    assert not any(code.startswith("knowledge_navigation_step_") for code in pack.degradations)


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

    excerpt = bounded_source_text(rows, 13)

    assert "Node setup" in excerpt
    assert "install-alpha --two-node" in excerpt
    assert "Do not select the cluster communication NIC." in excerpt
    assert "Verify the VIP and replicated volume." in excerpt
    assert "unrelated appendix" not in excerpt
    assert (before in excerpt) != (after in excerpt)


def test_broad_source_window_reserves_one_substantive_block_per_child_phase() -> None:
    chapter = ["Hyperconverged deployment", "Management HA"]
    rows: list[tuple[object, ...]] = [
        (0, "heading", "Management HA", json.dumps(chapter)),
    ]
    for ordinal, phase in enumerate(("Keepalived", "OcloudAgent", "UUID", "MySQL"), 1):
        path = json.dumps([*chapter, phase])
        rows.append((ordinal * 100, "heading", phase, path))
        rows.append(
            (
                ordinal * 100 + 1,
                "paragraph",
                f"{phase}-essential-step " + "e" * 700,
                path,
            )
        )
        rows.extend(
            (ordinal * 100 + detail, "paragraph", phase + "x" * 900, path)
            for detail in range(2, 14)
        )

    excerpt = bounded_source_text(rows, 0)

    assert all(
        f"{phase}-essential-step" in excerpt
        for phase in ("Keepalived", "OcloudAgent", "UUID", "MySQL")
    )


def test_source_section_keeps_each_neighbor_bound_to_its_own_evidence(tmp_path) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "deployment.md"
    source.write_text(
        "# Node deployment\n\nPrepare both hosts.\n\n"
        "```bash\ninstall-alpha --two-node\n```\n\n"
        "> Warning: do not select the cluster communication NIC.\n\n"
        "Verify the VIP after installation.\n",
        encoding="utf-8",
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    DesktopTextImportService(kb_dir).import_text(source)

    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        anchor_id = str(
            connection.execute(
                "SELECT evidence_id FROM evidence_refs WHERE text LIKE '%install-alpha%'"
            ).fetchone()[0]
        )
        references = source_section_evidence_in(
            connection,
            anchor_id,
            terms=("deployment", "install"),
        )
        canonical_text = {
            str(evidence_id): str(text).strip()
            for evidence_id, text in connection.execute(
                """
                SELECT occurrences.evidence_id, blocks.text
                FROM evidence_occurrences AS occurrences
                JOIN document_ir_blocks AS blocks ON blocks.block_id = occurrences.block_id
                """
            )
        }

    assert len(references) >= 4
    assert all(
        reference.excerpt == canonical_text[reference.evidence_id] for reference in references
    )
    assert any("install-alpha --two-node" in reference.excerpt for reference in references)
    assert any("do not select" in reference.excerpt for reference in references)


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


def test_navigation_stops_before_expansion_when_the_snapshot_changes(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "openkb.desktop_import_runner.start_graph_extraction", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        "openkb.desktop_catalog_store.start_catalog_rebuilds", lambda *_args, **_kwargs: None
    )
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
                    "actions": [
                        {
                            "kind": "search_routes",
                            "aspect": prompt["objective"]["required_aspects"][0],
                            "terms": ["Omega"],
                        }
                    ],
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


def test_seed_snapshot_drift_discards_page_tree_and_navigation_reads(tmp_path) -> None:
    kb_dir = _knowledge_base(tmp_path)
    operations: list[str] = []

    def transport(request, _timeout_seconds):
        operations.append(request.operation)
        if request.operation == "retrieval_plan":
            return '{"terms":["Alpha","Beta"]}'
        if request.operation == "page_tree_selection":
            response = _selection_response(request)
            with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
                connection.execute(
                    "UPDATE knowledge_catalog_state "
                    "SET source_revision = source_revision + 1 WHERE singleton = 1"
                )
                connection.commit()
            return response
        raise AssertionError(request.operation)

    pack = DesktopEvidenceRetriever(kb_dir, model_gateway=DesktopModelGateway(transport)).retrieve(
        "Compare Alpha and Beta"
    )

    assert operations == ["retrieval_plan", "page_tree_selection"]
    assert pack.evidence
    assert pack.retrieval_plan.source == "deterministic"
    assert pack.retrieval_trace.selected_node_ids == ()
    assert pack.retrieval_trace.navigation_routes == ()
    assert pack.guidance == ()
    assert pack.route_options == ()
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
        requested_routes,
        requested_evidence_ids,
    ) -> DesktopKnowledgeNavigationResult:
        del (
            catalog_generation_id,
            terms,
            max_reads,
            max_source_windows,
            excluded_routes,
            requested_routes,
            requested_evidence_ids,
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


def test_navigation_advertises_virtual_root_and_available_kind_indexes(tmp_path) -> None:
    kb_dir = _knowledge_base(tmp_path)

    pack = DesktopEvidenceRetriever(kb_dir).retrieve("Alpha routing detail")

    routes = {item.route for item in pack.route_options}
    assert {"index", "summaries/index", "sources/index"} <= routes


def test_source_section_routes_are_stable_and_directly_readable(tmp_path) -> None:
    kb_dir = _knowledge_base(tmp_path)
    database_path = desktop_state_database_path(kb_dir)
    with sqlite3.connect(database_path) as connection:
        first_inventory = eligible_knowledge_routes_in(connection)
        second_inventory = eligible_knowledge_routes_in(connection)
        section = next(
            item
            for item in first_inventory
            if item.authority == "source_section" and item.title.endswith("Beta")
        )
        catalog_generation_id = str(
            connection.execute(
                "SELECT current_generation_id FROM knowledge_catalog_state WHERE singleton = 1"
            ).fetchone()[0]
        )
        result = build_knowledge_navigation_in(
            connection,
            catalog_generation_id=catalog_generation_id,
            terms=("beta",),
            baseline_evidence=(),
            max_reads=1,
            max_source_windows=1,
            requested_routes=(section.route,),
        )

    assert section.route.startswith("sources/")
    assert section.route in {item.route for item in second_inventory}
    assert result.routes == (section.route,)
    assert result.source_windows
    assert any("Beta" in item.section for item in result.source_windows)


def test_query_matching_source_section_is_advertised_without_index_hops(tmp_path) -> None:
    kb_dir = _knowledge_base(tmp_path)
    database_path = desktop_state_database_path(kb_dir)
    with sqlite3.connect(database_path) as connection:
        section = next(
            item
            for item in eligible_knowledge_routes_in(connection)
            if item.authority == "source_section" and item.title.endswith("Beta")
        )
        catalog_generation_id = str(
            connection.execute(
                "SELECT current_generation_id FROM knowledge_catalog_state WHERE singleton = 1"
            ).fetchone()[0]
        )
        result = build_knowledge_navigation_in(
            connection,
            catalog_generation_id=catalog_generation_id,
            terms=("beta",),
            baseline_evidence=(),
            max_reads=1,
            max_source_windows=1,
        )

    advertised = tuple(item.route for item in result.route_options[:24])
    assert section.route in advertised


def test_query_matching_source_sections_advertise_distinct_phases_before_details(
    tmp_path,
) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "phase-routing.md"
    source.write_text(
        "# Install cluster\n\nCluster overview.\n\n"
        "## Install management\n\nManagement overview.\n\n"
        "### Install Keepalived\n\nConfigure the virtual IP.\n\n"
        "### Install MySQL\n\nConfigure replication.\n\n"
        "## Install storage\n\nStorage overview.\n\n"
        "### Install Bcache\n\nConfigure the cache.\n\n"
        "## Install validation\n\nValidate failover.\n",
        encoding="utf-8",
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    DesktopTextImportService(kb_dir).import_text(source)
    database_path = desktop_state_database_path(kb_dir)
    with sqlite3.connect(database_path) as connection:
        catalog_generation_id = str(
            connection.execute(
                "SELECT current_generation_id FROM knowledge_catalog_state WHERE singleton = 1"
            ).fetchone()[0]
        )
        result = build_knowledge_navigation_in(
            connection,
            catalog_generation_id=catalog_generation_id,
            terms=("install",),
            baseline_evidence=(),
            max_reads=1,
            max_source_windows=1,
        )

    source_titles = tuple(
        item.title
        for item in result.route_options[:24]
        if item.route.startswith("sources/") and "/sections/" in item.route
    )
    management_indexes = tuple(
        index
        for index, title in enumerate(source_titles)
        if title.startswith("Install cluster / Install management")
    )
    storage_index = next(
        index
        for index, title in enumerate(source_titles)
        if title.startswith("Install cluster / Install storage")
    )
    validation_index = next(
        index
        for index, title in enumerate(source_titles)
        if title.startswith("Install cluster / Install validation")
    )

    assert len(management_indexes) >= 2
    assert max(storage_index, validation_index) < management_indexes[1]


def test_query_matching_source_sections_seed_distinct_procedure_phases(
    tmp_path,
) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "phase-seed.md"
    source.write_text(
        "# Install cluster\n\nCluster overview.\n\n"
        "## Install management\n\nManagement overview.\n\n"
        "### Install Keepalived\n\nConfigure the virtual IP.\n\n"
        "### Install MySQL\n\nConfigure replication.\n\n"
        "## Install storage\n\nStorage overview.\n\n"
        "### Install Bcache\n\nConfigure the cache.\n\n"
        "## Install validation\n\nValidate failover.\n",
        encoding="utf-8",
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    DesktopTextImportService(kb_dir).import_text(source)
    database_path = desktop_state_database_path(kb_dir)
    with sqlite3.connect(database_path) as connection:
        catalog_generation_id = str(
            connection.execute(
                "SELECT current_generation_id FROM knowledge_catalog_state WHERE singleton = 1"
            ).fetchone()[0]
        )
        inventory = eligible_knowledge_routes_in(connection)
        result = build_knowledge_navigation_in(
            connection,
            catalog_generation_id=catalog_generation_id,
            terms=("install",),
            baseline_evidence=(),
            max_reads=4,
            max_source_windows=4,
        )

    titles_by_route = {item.route: item.title for item in inventory}
    selected_titles = tuple(titles_by_route[route] for route in result.routes)

    assert any("Install management" in title for title in selected_titles)
    assert any("Install storage" in title for title in selected_titles)
    assert any("Install validation" in title for title in selected_titles)
    assert {
        "Install cluster / Install management",
        "Install cluster / Install storage",
        "Install cluster / Install validation",
    } <= {item.section for item in result.source_windows}


def test_dense_how_to_routes_surface_summary_and_whole_source_within_prompt_budget(
    tmp_path, monkeypatch
) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "dense-install.md"
    source.write_text(
        "# Install Alpha cluster\n\nCluster overview.\n\n"
        + "\n\n".join(
            f"## Install Alpha phase {ordinal}\n\nPerform phase {ordinal}."
            for ordinal in range(1, 33)
        )
        + "\n",
        encoding="utf-8",
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    DesktopTextImportService(kb_dir).import_text(source)
    from openkb.desktop_knowledge_navigation import _inventory_descriptor

    monkeypatch.setattr(
        "openkb.desktop_knowledge_navigation._summary_descriptors_in",
        lambda _connection, terms, _baseline, inventory: tuple(
            _inventory_descriptor(item, terms)
            for item in inventory
            if item.authority == "document_summary"
        )[:1],
    )
    database_path = desktop_state_database_path(kb_dir)
    with sqlite3.connect(database_path) as connection:
        catalog_generation_id = str(
            connection.execute(
                "SELECT current_generation_id FROM knowledge_catalog_state WHERE singleton = 1"
            ).fetchone()[0]
        )
        result = build_knowledge_navigation_in(
            connection,
            catalog_generation_id=catalog_generation_id,
            terms=("install", "alpha"),
            baseline_evidence=(),
            max_reads=8,
            max_source_windows=4,
        )

    advertised = tuple(item.route for item in result.route_options[:24])
    assert "summaries/dense-install" in advertised
    assert "sources/dense-install" in advertised
    assert "summaries/dense-install" in result.routes
    assert "sources/dense-install" in result.routes


def test_whole_source_read_reserves_the_broad_matching_chapter_anchor(tmp_path) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "cluster-install.md"
    source.write_text(
        "# Install cluster\n\nCluster overview.\n\n"
        "## Install management\n\nConfigure management.\n\n"
        "### Install MySQL\n\nConfigure replication.\n\n"
        "## Install storage\n\nConfigure Bcache and GlusterFS.\n",
        encoding="utf-8",
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    DesktopTextImportService(kb_dir).import_text(source)
    from openkb.desktop_knowledge_navigation import (
        _NavigationRead,
        _rank_source_evidence_ids_in,
        _source_relevance_in,
        _source_structure_units_in,
    )

    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        document_id = str(
            connection.execute("SELECT document_id FROM source_documents").fetchone()[0]
        )
        read = _NavigationRead(
            route="sources/cluster-install",
            kind="source",
            authority="source_document",
            title="Cluster install",
            units=_source_structure_units_in(connection, document_id),
            hop=0,
            snapshot_token="source",
        )
        terms = ("install", "cluster", "management", "mysql")
        evidence_ids = _rank_source_evidence_ids_in(
            connection,
            (read,),
            terms=terms,
            baseline_ids=frozenset(),
        )
        first = _source_relevance_in(
            connection,
            evidence_ids[0],
            terms,
        )

    assert first is not None
    assert first[2] == "Install cluster"


def test_whole_source_outline_does_not_prefer_unrequested_expansion_scope(tmp_path) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "install-guide.md"
    source.write_text(
        "# Compute node\n\nNode overview.\n\n"
        "## Expansion install steps\n\nInstall overview.\n\n"
        "### Hyperconverged expansion\n\nExpansion-only settings.\n\n"
        "# Appliance\n\nAppliance overview.\n\n"
        "## System install\n\nSystem overview.\n\n"
        "### Hyperconverged node partition\n\nConfigure the system disk.\n",
        encoding="utf-8",
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    DesktopTextImportService(kb_dir).import_text(source)
    from openkb.desktop_knowledge_navigation import (
        _NavigationRead,
        _rank_source_evidence_ids_in,
        _source_relevance_in,
        _source_structure_units_in,
    )

    terms = ("install", "hyperconverged", "node")
    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        document_id = str(
            connection.execute("SELECT document_id FROM source_documents").fetchone()[0]
        )
        read = _NavigationRead(
            route="sources/install-guide",
            kind="source",
            authority="source_document",
            title="Install guide",
            units=_source_structure_units_in(connection, document_id),
            hop=0,
            snapshot_token="source",
        )
        evidence_ids = _rank_source_evidence_ids_in(
            connection,
            (read,),
            terms=terms,
            baseline_ids=frozenset(),
        )
        first = _source_relevance_in(connection, evidence_ids[0], terms)

    assert first is not None
    assert first[2].endswith("System install")


def test_source_anchor_phase_key_collapses_adjacent_details_not_major_phases() -> None:
    from openkb.desktop_knowledge_navigation import _source_anchor_phase_key

    assert _source_anchor_phase_key(
        "Cluster deployment / Management HA / Keepalived"
    ) == _source_anchor_phase_key("Cluster deployment / Management HA / MySQL")
    assert _source_anchor_phase_key(
        "Cluster deployment / Management HA / Keepalived"
    ) != _source_anchor_phase_key("Cluster deployment / Storage / Bcache")


def test_reading_virtual_index_can_reveal_routes_before_source_progress(tmp_path) -> None:
    kb_dir = _knowledge_base(tmp_path)
    navigation_round = 0
    observed_second_round_routes: set[str] = set()
    observed_third_round_routes: set[str] = set()

    def transport(request, _timeout_seconds):
        nonlocal navigation_round
        if request.operation == "retrieval_plan":
            return '{"terms":["Alpha","installation"]}'
        if request.operation == "page_tree_selection":
            return _selection_response(request)
        if request.operation == "knowledge_navigation_step":
            navigation_round += 1
            prompt = json.loads(request.content)
            evidence_ids = [item["evidence_id"] for item in prompt["evidence"]]
            if navigation_round == 2:
                observed_second_round_routes.update(
                    item["route"] for item in prompt["available_routes"]
                )
            if navigation_round == 3:
                observed_third_round_routes.update(
                    item["route"] for item in prompt["available_routes"]
                )
            return json.dumps(
                {
                    "schema_version": "openkb.knowledge-navigation-step.v1",
                    "snapshot_id": prompt["snapshot_id"],
                    "objective": prompt["objective"],
                    "coverage": [
                        {
                            "aspect": aspect,
                            "status": "partial" if navigation_round == 3 else "missing",
                            "evidence_ids": evidence_ids[:1] if navigation_round == 3 else [],
                        }
                        for aspect in prompt["objective"]["required_aspects"]
                    ],
                    "actions": (
                        [
                            {
                                "kind": "read_routes",
                                "aspect": prompt["objective"]["required_aspects"][0],
                                "routes": ["index"],
                            }
                        ]
                        if navigation_round == 1
                        else [
                            {
                                "kind": "read_routes",
                                "aspect": prompt["objective"]["required_aspects"][0],
                                "routes": ["summaries/index"],
                            }
                        ]
                        if navigation_round == 2
                        else []
                    ),
                    "decision": "continue" if navigation_round < 3 else "stop",
                }
            )
        raise AssertionError(request.operation)

    pack = DesktopEvidenceRetriever(kb_dir, model_gateway=DesktopModelGateway(transport)).retrieve(
        "How should Alpha be installed and validated?"
    )

    assert navigation_round == 3, pack.retrieval_trace.navigation_stop_reason
    assert {"index", "summaries/index"} <= set(pack.retrieval_trace.navigation_routes)
    assert observed_second_round_routes
    assert any(
        not (route == "index" or route.endswith("/index")) for route in observed_second_round_routes
    )
    assert any(
        route.startswith(("summaries/", "sources/")) and not route.endswith("/index")
        for route in observed_third_round_routes
    )


def test_supported_factual_question_finishes_from_lexically_matching_seed(tmp_path) -> None:
    kb_dir = _knowledge_base(tmp_path)
    operations: list[str] = []

    def transport(request, _timeout_seconds):
        operations.append(request.operation)
        if request.operation == "retrieval_plan":
            return '{"terms":["Alpha","original evidence"]}'
        raise AssertionError(request.operation)

    pack = DesktopEvidenceRetriever(kb_dir, model_gateway=DesktopModelGateway(transport)).retrieve(
        "What remains in Alpha original evidence?"
    )

    assert operations == ["retrieval_plan"]
    assert pack.evidence
    assert pack.retrieval_trace.navigation_round_count == 0
    assert pack.retrieval_trace.navigation_stop_reason == "covered"


def test_factual_question_does_not_treat_an_unrelated_seed_hit_as_covered(tmp_path) -> None:
    kb_dir = _knowledge_base(tmp_path)
    operations: list[str] = []

    def transport(request, _timeout_seconds):
        operations.append(request.operation)
        if request.operation == "retrieval_plan":
            return '{"terms":["Alpha"]}'
        if request.operation == "page_tree_selection":
            return _selection_response(request)
        if request.operation == "knowledge_navigation_step":
            prompt = json.loads(request.content)
            return json.dumps(
                {
                    "schema_version": "openkb.knowledge-navigation-step.v1",
                    "snapshot_id": prompt["snapshot_id"],
                    "objective": prompt["objective"],
                    "coverage": [
                        {"aspect": aspect, "status": "missing", "evidence_ids": []}
                        for aspect in prompt["objective"]["required_aspects"]
                    ],
                    "actions": [],
                    "decision": "stop",
                }
            )
        raise AssertionError(request.operation)

    pack = DesktopEvidenceRetriever(kb_dir, model_gateway=DesktopModelGateway(transport)).retrieve(
        "Who owns the missing release?"
    )

    assert pack.evidence
    assert operations == [
        "retrieval_plan",
        "page_tree_selection",
        "knowledge_navigation_step",
    ]
    assert pack.retrieval_trace.navigation_round_count == 1
    assert pack.retrieval_trace.navigation_stop_reason == "absent"
    assert pack.retrieval_trace.coverage_gate_state == "uncovered"


def test_factual_seed_requires_support_within_one_evidence_fragment(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "openkb.desktop_import_runner.start_graph_extraction", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        "openkb.desktop_catalog_store.start_catalog_rebuilds", lambda *_args, **_kwargs: None
    )
    kb_dir = tmp_path / "knowledge"
    alpha = tmp_path / "alpha.md"
    release = tmp_path / "release.md"
    alpha.write_text("# Alpha\n\nAlpha is a product codename.\n", encoding="utf-8")
    release.write_text("# Release\n\nThe release owner is Bob.\n", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    importer = DesktopTextImportService(kb_dir)
    importer.import_text(alpha)
    importer.import_text(release)
    operations: list[str] = []

    def transport(request, _timeout_seconds):
        operations.append(request.operation)
        if request.operation == "retrieval_plan":
            return '{"terms":["Alpha","release"]}'
        if request.operation == "page_tree_selection":
            return _selection_response(request)
        if request.operation == "knowledge_navigation_step":
            prompt = json.loads(request.content)
            return json.dumps(
                {
                    "schema_version": "openkb.knowledge-navigation-step.v1",
                    "snapshot_id": prompt["snapshot_id"],
                    "objective": prompt["objective"],
                    "coverage": [
                        {"aspect": aspect, "status": "missing", "evidence_ids": []}
                        for aspect in prompt["objective"]["required_aspects"]
                    ],
                    "actions": [],
                    "decision": "stop",
                }
            )
        raise AssertionError(request.operation)

    pack = DesktopEvidenceRetriever(kb_dir, model_gateway=DesktopModelGateway(transport)).retrieve(
        "Who owns the Alpha release?"
    )

    assert "knowledge_navigation_step" in operations
    assert pack.retrieval_trace.navigation_stop_reason == "absent"
    assert pack.retrieval_trace.coverage_gate_state == "uncovered"


def test_navigation_session_never_exceeds_total_physical_model_call_budget(tmp_path) -> None:
    kb_dir = _knowledge_base(tmp_path)
    attempts: dict[tuple[str, str | None], int] = {}

    def transport(request, _timeout_seconds):
        key = (request.operation, request.parent_operation)
        attempts[key] = attempts.get(key, 0) + 1
        attempt = attempts[key]
        if request.operation == "retrieval_plan":
            if attempt <= 2:
                raise TimeoutError
            return "not-json"
        if request.operation == "structured_output_repair":
            if attempt <= 2:
                raise TimeoutError
            if request.parent_operation == "retrieval_plan":
                return '{"terms":["Alpha","Beta"]}'
            return '{"selections":[]}'
        if request.operation == "page_tree_selection":
            return "not-json"
        if request.operation == "knowledge_navigation_step":
            return _covered_navigation_response(request)
        raise AssertionError(request.operation)

    gateway = DesktopModelGateway(transport, sleep=lambda _seconds: None)
    pack = DesktopEvidenceRetriever(kb_dir, model_gateway=gateway).retrieve(
        "Compare Alpha and Beta"
    )

    assert sum(attempts.values()) <= 8
    assert pack.retrieval_model_cost.model_calls <= 8
    assert pack.retrieval_trace.navigation_model_calls <= 8


def test_normal_navigation_no_progress_is_not_reported_as_capability_failure(
    tmp_path,
) -> None:
    kb_dir = _knowledge_base(tmp_path)
    operations: list[str] = []

    def transport(request, _timeout_seconds):
        operations.append(request.operation)
        if request.operation == "retrieval_plan":
            return '{"terms":["Alpha","installation"]}'
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
                    "actions": [
                        {
                            "kind": "search_routes",
                            "aspect": prompt["objective"]["required_aspects"][0],
                            "terms": ["select cluster communication NIC"],
                        }
                    ],
                    "decision": "continue",
                }
            )
        if request.operation == "structured_output_repair":
            raise AssertionError("Natural-language select must not be parsed as raw SQL.")
        raise AssertionError(request.operation)

    pack = DesktopEvidenceRetriever(kb_dir, model_gateway=DesktopModelGateway(transport)).retrieve(
        "Alpha 如何安装"
    )

    assert pack.retrieval_trace.navigation_stop_reason == "partial"
    assert pack.retrieval_trace.navigation_action_kinds == ("search_routes",)
    assert not any(code.startswith("knowledge_navigation_step_") for code in pack.degradations)


def test_navigation_reserves_evidence_for_each_explicit_action_aspect(tmp_path) -> None:
    kb_dir = _knowledge_base(tmp_path)
    database_path = desktop_state_database_path(kb_dir)
    seed = DesktopEvidenceRef(
        evidence_id="seed",
        document_id="seed-document",
        document_name="seed.md",
        section="Overview",
        locator={},
        excerpt="Alpha deployment overview.",
        channels=("fts",),
    )
    initial = DesktopEvidencePack(deterministic_plan("Alpha 如何安装部署"), (seed,))
    navigation_round = 0

    def transport(request, _timeout_seconds):
        nonlocal navigation_round
        assert request.operation == "knowledge_navigation_step"
        navigation_round += 1
        prompt = json.loads(request.content)
        evidence_ids = [item["evidence_id"] for item in prompt["evidence"]]
        aspects = prompt["objective"]["required_aspects"]
        if navigation_round == 1:
            coverage = [
                {
                    "aspect": aspect,
                    "status": "partial" if index == 0 else "missing",
                    "evidence_ids": evidence_ids[:1] if index == 0 else [],
                }
                for index, aspect in enumerate(aspects)
            ]
            actions = [
                {
                    "kind": "search_routes",
                    "aspect": aspects[1],
                    "terms": ["target-prerequisites"],
                },
                {
                    "kind": "search_routes",
                    "aspect": aspects[2],
                    "terms": ["target-validation"],
                },
            ]
            decision = "continue"
        else:
            coverage = [
                {"aspect": aspect, "status": "covered", "evidence_ids": evidence_ids[:1]}
                for aspect in aspects
            ]
            actions = []
            decision = "stop"
        return json.dumps(
            {
                "schema_version": "openkb.knowledge-navigation-step.v1",
                "snapshot_id": prompt["snapshot_id"],
                "objective": prompt["objective"],
                "coverage": coverage,
                "actions": actions,
                "decision": decision,
            }
        )

    def retrieve_round(**kwargs):
        target = kwargs["retrieval_plan"].terms[0]
        evidence = (
            DesktopEvidenceRef(
                evidence_id=target,
                document_id=target,
                document_name=f"{target}.md",
                section="Target",
                locator={},
                excerpt=f"Evidence for {target}.",
                channels=("fts",),
            ),
            *tuple(
                DesktopEvidenceRef(
                    evidence_id=f"{target}-distractor-{index}",
                    document_id=f"{target}-distractor-{index}",
                    document_name="distractors.md",
                    section=f"Distractor {index}",
                    locator={},
                    excerpt=f"Unrelated detail {index}.",
                    channels=("fts",),
                )
                for index in range(40)
            ),
        )
        return DesktopEvidencePack(kwargs["retrieval_plan"], evidence)

    pack = run_navigation_session(
        kb_dir=kb_dir,
        database_path=database_path,
        question="Alpha 如何安装部署",
        pinned_snapshot_id=current_navigation_snapshot_id(database_path),
        initial_pack=initial,
        model_gateway=DesktopModelGateway(transport),
        retrieve_round=retrieve_round,
    )

    evidence_ids = {item.evidence_id for item in pack.evidence}
    assert {"target-prerequisites", "target-validation"} <= evidence_ids
    from openkb.desktop_navigation_session import NAVIGATION_MAX_EVIDENCE_REFS

    assert len(pack.evidence) <= NAVIGATION_MAX_EVIDENCE_REFS


def test_navigation_executes_the_last_admitted_round_before_budget_stop(tmp_path) -> None:
    kb_dir = _knowledge_base(tmp_path)
    database_path = desktop_state_database_path(kb_dir)
    seed = DesktopEvidenceRef(
        evidence_id="seed",
        document_id="seed-document",
        document_name="seed.md",
        section="Overview",
        locator={},
        excerpt="Alpha deployment overview.",
        channels=("fts",),
    )
    initial = DesktopEvidencePack(deterministic_plan("Alpha 如何安装部署"), (seed,))
    requested_terms: list[str] = []

    def transport(request, _timeout_seconds):
        assert request.operation == "knowledge_navigation_step"
        prompt = json.loads(request.content)
        round_number = int(prompt["round"])
        aspects = prompt["objective"]["required_aspects"]
        evidence_ids = [item["evidence_id"] for item in prompt["evidence"]]
        return json.dumps(
            {
                "schema_version": "openkb.knowledge-navigation-step.v1",
                "snapshot_id": prompt["snapshot_id"],
                "objective": prompt["objective"],
                "coverage": [
                    {
                        "aspect": aspect,
                        "status": "partial" if evidence_ids else "missing",
                        "evidence_ids": evidence_ids[:1],
                    }
                    for aspect in aspects
                ],
                "actions": [
                    {
                        "kind": "search_routes",
                        "aspect": aspects[0],
                        "terms": [f"round-{round_number}-evidence"],
                    }
                ],
                "decision": "continue",
            }
        )

    def retrieve_round(**kwargs):
        target = kwargs["retrieval_plan"].terms[0]
        requested_terms.append(target)
        evidence = DesktopEvidenceRef(
            evidence_id=target,
            document_id=target,
            document_name=f"{target}.md",
            section="Target",
            locator={},
            excerpt=f"Evidence for {target}.",
            channels=("fts",),
        )
        return DesktopEvidencePack(kwargs["retrieval_plan"], (evidence,))

    pack = run_navigation_session(
        kb_dir=kb_dir,
        database_path=database_path,
        question="Alpha 如何安装部署",
        pinned_snapshot_id=current_navigation_snapshot_id(database_path),
        initial_pack=initial,
        model_gateway=DesktopModelGateway(transport),
        retrieve_round=retrieve_round,
    )

    assert requested_terms == [
        "round-1-evidence",
        "round-2-evidence",
        "round-3-evidence",
    ]
    assert "round-3-evidence" in {item.evidence_id for item in pack.evidence}
    assert pack.retrieval_trace.navigation_round_count == 3
    assert pack.retrieval_trace.navigation_stop_reason == "budget_exhausted"


def test_navigation_discards_a_supplement_if_snapshot_changes_during_read(tmp_path) -> None:
    kb_dir = _knowledge_base(tmp_path)
    database_path = desktop_state_database_path(kb_dir)
    seed = DesktopEvidenceRef(
        evidence_id="seed",
        document_id="seed-document",
        document_name="seed.md",
        section="Overview",
        locator={},
        excerpt="Alpha deployment overview.",
        channels=("fts",),
    )
    initial = DesktopEvidencePack(deterministic_plan("Alpha 如何安装部署"), (seed,))

    def transport(request, _timeout_seconds):
        assert request.operation == "knowledge_navigation_step"
        prompt = json.loads(request.content)
        aspects = prompt["objective"]["required_aspects"]
        return json.dumps(
            {
                "schema_version": "openkb.knowledge-navigation-step.v1",
                "snapshot_id": prompt["snapshot_id"],
                "objective": prompt["objective"],
                "coverage": [
                    {"aspect": aspect, "status": "missing", "evidence_ids": []}
                    for aspect in aspects
                ],
                "actions": [
                    {
                        "kind": "search_routes",
                        "aspect": aspects[0],
                        "terms": ["unstable-evidence"],
                    }
                ],
                "decision": "continue",
            }
        )

    def retrieve_round(**kwargs):
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                "UPDATE knowledge_catalog_state "
                "SET source_revision = source_revision + 1 WHERE singleton = 1"
            )
            connection.commit()
        evidence = DesktopEvidenceRef(
            evidence_id="unstable-evidence",
            document_id="unstable-document",
            document_name="unstable.md",
            section="Target",
            locator={},
            excerpt="This belongs to the changed snapshot.",
            channels=("fts",),
        )
        return DesktopEvidencePack(kwargs["retrieval_plan"], (evidence,))

    pack = run_navigation_session(
        kb_dir=kb_dir,
        database_path=database_path,
        question="Alpha 如何安装部署",
        pinned_snapshot_id=current_navigation_snapshot_id(database_path),
        initial_pack=initial,
        model_gateway=DesktopModelGateway(transport),
        retrieve_round=retrieve_round,
    )

    assert [item.evidence_id for item in pack.evidence] == ["seed"]
    assert pack.retrieval_trace.navigation_stop_reason == "snapshot_degraded"
    assert "knowledge_navigation_snapshot_changed" in pack.degradations


def test_navigation_actions_require_an_explicit_open_aspect() -> None:
    coverage = (
        DesktopAnswerCoverageTrace("ordered_actions", "missing", ()),
        DesktopAnswerCoverageTrace("validation", "missing", ()),
    )

    try:
        validated_navigation_actions(
            [{"kind": "search_routes", "terms": ["install"]}],
            visited_action_ids=frozenset(),
            available_routes=frozenset(),
            known_evidence_ids=frozenset(),
            coverage=coverage,
            maximum_actions=2,
        )
    except ValueError as exc:
        assert "allowed fields" in str(exc)
    else:
        raise AssertionError("An action without an explicit aspect must be rejected.")


def test_navigation_coverage_drops_unknown_bindings_without_failing_the_round() -> None:
    from openkb.desktop_adaptive_navigation import NavigationObjective, _coverage

    objective = NavigationObjective(
        answer_kind="how_to",
        subject="Install the cluster",
        requested_scope="base deployment",
        named_entities=(),
        concepts=(),
        user_actions=(),
        constraints=(),
        required_aspects=("ordered_actions", "validation"),
    )

    coverage = _coverage(
        [
            {
                "aspect": "ordered_actions",
                "status": "partial",
                "evidence_ids": ["known", "invented"],
            },
            {
                "aspect": "validation",
                "status": "covered",
                "evidence_ids": ["invented"],
            },
        ],
        objective,
        frozenset(("known",)),
    )

    assert coverage == (
        DesktopAnswerCoverageTrace("ordered_actions", "partial", ("known",)),
        DesktopAnswerCoverageTrace("validation", "missing", ()),
    )


def test_navigation_action_identity_deduplicates_the_same_read_across_aspects() -> None:
    first = NavigationAction("read_routes", "ordered_actions", routes=("procedures/install",))
    second = NavigationAction("read_routes", "validation", routes=("procedures/install",))

    assert first.identity == second.identity


def test_navigation_discards_duplicate_reads_across_aspects() -> None:
    coverage = (
        DesktopAnswerCoverageTrace("ordered_actions", "missing", ()),
        DesktopAnswerCoverageTrace("validation", "missing", ()),
    )

    actions = validated_navigation_actions(
        [
            {
                "kind": "read_routes",
                "aspect": "ordered_actions",
                "routes": ["procedures/install"],
            },
            {
                "kind": "read_routes",
                "aspect": "validation",
                "routes": ["procedures/install"],
            },
        ],
        visited_action_ids=frozenset(),
        available_routes=frozenset(("procedures/install",)),
        known_evidence_ids=frozenset(),
        coverage=coverage,
        maximum_actions=2,
    )

    assert actions == (
        NavigationAction("read_routes", "ordered_actions", routes=("procedures/install",)),
    )


def test_navigation_discards_an_already_completed_route_without_hiding_an_invention() -> None:
    coverage = (DesktopAnswerCoverageTrace("ordered_actions", "missing", ()),)

    actions = validated_navigation_actions(
        [
            {
                "kind": "read_routes",
                "aspect": "ordered_actions",
                "routes": ["summaries/already-read"],
            }
        ],
        visited_action_ids=frozenset(),
        available_routes=frozenset(("procedures/available",)),
        completed_routes=frozenset(("summaries/already-read",)),
        known_evidence_ids=frozenset(),
        coverage=coverage,
        maximum_actions=1,
    )

    assert actions == ()

    try:
        validated_navigation_actions(
            [
                {
                    "kind": "read_routes",
                    "aspect": "ordered_actions",
                    "routes": ["procedures/invented"],
                }
            ],
            visited_action_ids=frozenset(),
            available_routes=frozenset(("procedures/available",)),
            completed_routes=frozenset(("summaries/already-read",)),
            known_evidence_ids=frozenset(),
            coverage=coverage,
            maximum_actions=1,
        )
    except ValueError as exc:
        assert "unavailable or unpublished" in str(exc)
    else:
        raise AssertionError("An invented route must remain a contract failure.")


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
