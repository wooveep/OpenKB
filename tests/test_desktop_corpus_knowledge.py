"""Acceptance checks for qualified document candidates and corpus synthesis."""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
from pathlib import Path

from openkb import desktop_catalog_store as catalog_store
from openkb.desktop_catalog_store import rebuild_pending_catalog
from openkb.desktop_corpus_benchmark import _multi_document_topic_coverage
from openkb.desktop_corpus_knowledge import (
    _applicability_pairs,
    _Candidate,
    _candidate_clusters,
    _Claim,
    _claim_conflicts,
    _corpus_language,
    synthesize_qualified_corpus_in,
)
from openkb.desktop_engine import DesktopEngineServer, DesktopRequest
from openkb.desktop_import import DesktopTextImportService
from openkb.desktop_knowledge_analysis import KNOWLEDGE_ANALYSIS_SCHEMA_VERSION
from openkb.desktop_knowledge_generations import (
    KnowledgeGenerationChange,
    KnowledgeGenerationSource,
    publish_corpus_generation_in,
)
from openkb.desktop_knowledge_navigation import (
    _GuidanceUnit,
    _NavigationRead,
    _rank_source_evidence_ids_in,
    _ReadDescriptor,
    _select_read_descriptors,
)
from openkb.desktop_model_gateway import DesktopModelGateway
from openkb.desktop_real_corpus_benchmark import (
    current_real_corpus_contract_digest,
    current_real_corpus_implementation_digest,
    load_real_corpus_benchmark,
    parse_real_corpus_benchmark,
    portable_artifact_digest,
    portable_manifest_digest,
)
from openkb.desktop_retrieval import DesktopEvidenceRetriever
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime

_APPLICABILITY = {
    "product_version": "V10",
    "platform": "OCloudView",
    "deployment_scenario": "双节点超融合",
    "time_boundary": "",
}


def test_shipped_real_corpus_attestation_is_fixed_complete_and_tamper_evident() -> None:
    attestation = load_real_corpus_benchmark()

    assert attestation.passed
    assert attestation.contract_digest == current_real_corpus_contract_digest()
    assert attestation.implementation_digest == current_real_corpus_implementation_digest()
    assert len(attestation.implementation_commit_sha) == 40
    assert attestation.original_baseline.sample_count >= 3
    assert attestation.original_baseline.fallback_runs == 0
    assert attestation.windows_acceptance.artifact_kind == "windows-portable-x64"
    assert attestation.windows_acceptance.packaged_smoke_passed
    assert attestation.windows_acceptance.cancellation_passed
    assert attestation.windows_acceptance.regeneration_completed
    assert attestation.windows_acceptance.restart_readable
    assert attestation.windows_acceptance.answer_versions_preserved
    assert attestation.sample_count >= 3
    assert len(attestation.cases) >= 3
    assert all(case.original_comparison_passed for case in attestation.cases)
    payload = attestation.as_dict()
    payload["answer_completeness"] = 1.0
    try:
        parse_real_corpus_benchmark(payload)
    except ValueError as error:
        assert "digest" in str(error)
    else:
        raise AssertionError("A modified real-corpus attestation must fail validation.")


def test_portable_attestation_digest_binds_payload_without_self_reference(
    tmp_path: Path,
) -> None:
    package = tmp_path / "OpenKB"
    executable = package / "OpenKB.exe"
    attestation = (
        package
        / "runtime"
        / "engine"
        / "_internal"
        / "openkb"
        / "benchmarks"
        / "real-corpus-attestation.json"
    )
    attestation.parent.mkdir(parents=True)
    executable.write_bytes(b"candidate")
    attestation.write_bytes(b"first attestation")
    files = []
    for path in (executable, attestation):
        files.append(
            {
                "path": path.relative_to(package).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "bytes": path.stat().st_size,
            }
        )
    (package / "release-manifest.json").write_text(json.dumps({"files": files}), encoding="utf-8")

    artifact_digest = portable_artifact_digest(package)
    manifest_digest = portable_manifest_digest(package)

    assert artifact_digest == manifest_digest
    attestation.write_bytes(b"final attestation with the payload digest")
    assert portable_artifact_digest(package) == artifact_digest
    executable.write_bytes(b"different candidate")
    assert portable_artifact_digest(package) != artifact_digest
    assert portable_artifact_digest(package) != portable_manifest_digest(package)


def test_multi_document_coverage_measures_only_safely_bound_identities() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE source_documents (
            document_id TEXT PRIMARY KEY,
            availability TEXT NOT NULL
        );
        CREATE TABLE knowledge_document_candidates (
            candidate_id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            normalized_title TEXT NOT NULL,
            admission_state TEXT NOT NULL
        );
        CREATE TABLE knowledge_identity_candidates (
            identity_id TEXT NOT NULL,
            candidate_id TEXT NOT NULL
        );
        CREATE TABLE knowledge_generation_items (
            generation_id INTEGER NOT NULL,
            item_key TEXT NOT NULL,
            identity_id TEXT,
            kind TEXT NOT NULL,
            normalized_title TEXT NOT NULL
        );
        CREATE TABLE knowledge_generation_item_sources (
            generation_id INTEGER NOT NULL,
            item_key TEXT NOT NULL,
            evidence_id TEXT NOT NULL
        );
        CREATE TABLE evidence_occurrences (
            evidence_id TEXT NOT NULL,
            document_id TEXT NOT NULL
        );

        INSERT INTO source_documents VALUES ('doc-1', 'available'), ('doc-2', 'available');

        -- These candidates deliberately remain outside a published identity while
        -- model-proposed aliases await review. They are not eligible synthesis work.
        INSERT INTO knowledge_document_candidates VALUES
            ('pending-1', 'doc-1', 'entity', 'ambiguous product', 'admitted'),
            ('pending-2', 'doc-2', 'entity', 'ambiguous product', 'admitted');

        INSERT INTO knowledge_document_candidates VALUES
            ('bound-1', 'doc-1', 'procedure', 'cluster install', 'admitted'),
            ('bound-2', 'doc-2', 'procedure', 'cluster install', 'admitted');
        INSERT INTO knowledge_identity_candidates VALUES
            ('identity-install', 'bound-1'),
            ('identity-install', 'bound-2');
        INSERT INTO knowledge_generation_items VALUES
            (7, 'procedure:cluster-install', 'identity-install', 'procedure', 'cluster install');
        INSERT INTO knowledge_generation_item_sources VALUES
            (7, 'procedure:cluster-install', 'evidence-1'),
            (7, 'procedure:cluster-install', 'evidence-2');
        INSERT INTO evidence_occurrences VALUES
            ('evidence-1', 'doc-1'),
            ('evidence-2', 'doc-2');
        """
    )

    assert _multi_document_topic_coverage(connection, 7) == 1.0

    connection.execute(
        "DELETE FROM knowledge_generation_item_sources WHERE evidence_id = 'evidence-2'"
    )

    assert _multi_document_topic_coverage(connection, 7) == 0.0


def _identity_candidate(
    candidate_id: str,
    *,
    title: str,
    aliases: tuple[str, ...],
    tags: tuple[str, ...],
    platform: str,
) -> _Candidate:
    return _Candidate(
        candidate_id=candidate_id,
        document_id=f"document-{candidate_id}",
        kind="concept",
        title=title,
        normalized_title=title.casefold(),
        entity_subtype=None,
        aliases=aliases,
        tags=tags,
        provenance_json="{}",
        claims=(
            _Claim(
                role="definition",
                text=f"{title} definition",
                applicability=(("platform", platform),),
                evidence_ids=(f"evidence-{candidate_id}",),
            ),
        ),
    )


def test_one_generic_alias_cannot_merge_distinct_corpus_identities() -> None:
    storage = _identity_candidate(
        "storage",
        title="Alpha storage",
        aliases=("cluster",),
        tags=("storage",),
        platform="Alpha",
    )
    network = _identity_candidate(
        "network",
        title="Beta network",
        aliases=("cluster",),
        tags=("network",),
        platform="Beta",
    )

    clusters = _candidate_clusters((storage, network))

    assert clusters == ((storage,), (network,))


def test_same_identity_keeps_claims_from_distinct_applicability_scopes() -> None:
    alpha = _identity_candidate(
        "alpha",
        title="Cluster networking",
        aliases=(),
        tags=("network",),
        platform="Alpha",
    )
    beta = _identity_candidate(
        "beta",
        title="Cluster networking",
        aliases=(),
        tags=("network",),
        platform="Beta",
    )

    clusters = _candidate_clusters((alpha, beta))

    assert clusters == ((alpha, beta),)


def test_missing_applicability_is_retained_as_explicit_unspecified_scope() -> None:
    applicability = _applicability_pairs(
        json.dumps(
            {
                "product_version": "V10",
                "platform": "",
                "deployment_scenario": "双节点超融合",
                "time_boundary": "",
            }
        )
    )

    assert applicability == (
        ("product_version", "V10"),
        ("platform", "Unspecified"),
        ("deployment_scenario", "双节点超融合"),
        ("time_boundary", "Unspecified"),
    )


def test_opposed_claims_in_overlapping_scope_enter_conflict_review() -> None:
    positive = _identity_candidate(
        "positive",
        title="VIP 配置",
        aliases=(),
        tags=("VIP",),
        platform="OCloudView",
    )
    negative = _Candidate(
        **{
            **positive.__dict__,
            "candidate_id": "negative",
            "document_id": "document-negative",
            "claims": (
                _Claim(
                    role="definition",
                    text="不需要配置 VIP。",
                    applicability=(("platform", "OCloudView"),),
                    evidence_ids=("evidence-negative",),
                ),
            ),
        }
    )
    positive = _Candidate(
        **{
            **positive.__dict__,
            "claims": (
                _Claim(
                    role="definition",
                    text="需要配置 VIP。",
                    applicability=(("platform", "OCloudView"),),
                    evidence_ids=("evidence-positive",),
                ),
            ),
        }
    )

    assert _claim_conflicts((positive, negative))


def test_different_default_ports_in_the_same_scope_enter_conflict_review() -> None:
    first = _identity_candidate(
        "port-8080",
        title="管理服务端口",
        aliases=(),
        tags=("port",),
        platform="OCloudView",
    )
    second = _Candidate(
        **{
            **first.__dict__,
            "candidate_id": "port-9090",
            "document_id": "document-port-9090",
        }
    )
    first = _Candidate(
        **{
            **first.__dict__,
            "claims": (
                _Claim(
                    role="configuration",
                    text="管理服务默认端口为 8080。",
                    applicability=(("platform", "OCloudView"),),
                    evidence_ids=("evidence-8080",),
                ),
            ),
        }
    )
    second = _Candidate(
        **{
            **second.__dict__,
            "claims": (
                _Claim(
                    role="configuration",
                    text="管理服务默认端口为 9090。",
                    applicability=(("platform", "OCloudView"),),
                    evidence_ids=("evidence-9090",),
                ),
            ),
        }
    )

    assert _claim_conflicts((first, second))


def test_numbered_steps_are_not_misclassified_as_value_conflicts() -> None:
    first = _identity_candidate(
        "step-one",
        title="安装步骤",
        aliases=(),
        tags=(),
        platform="OCloudView",
    )
    second = _Candidate(
        **{
            **first.__dict__,
            "candidate_id": "step-two",
            "document_id": "document-step-two",
        }
    )
    first = _Candidate(
        **{
            **first.__dict__,
            "claims": (
                _Claim(
                    role="step",
                    text="步骤 1：安装系统。",
                    applicability=(("platform", "OCloudView"),),
                    evidence_ids=("evidence-step-1",),
                ),
            ),
        }
    )
    second = _Candidate(
        **{
            **second.__dict__,
            "claims": (
                _Claim(
                    role="step",
                    text="步骤 2：配置网络。",
                    applicability=(("platform", "OCloudView"),),
                    evidence_ids=("evidence-step-2",),
                ),
            ),
        }
    )

    assert not _claim_conflicts((first, second))


def test_corpus_uses_one_dominant_or_explicitly_overridden_page_language() -> None:
    chinese = _identity_candidate(
        "zh",
        title="中文概念",
        aliases=(),
        tags=(),
        platform="平台",
    )
    english = _identity_candidate(
        "en",
        title="English concept",
        aliases=(),
        tags=(),
        platform="Platform",
    )

    assert _corpus_language((chinese, english), preferred_language=None) == "en"
    assert _corpus_language((chinese, english), preferred_language="zh") == "zh"


def test_one_generic_alias_cannot_reuse_an_existing_persistent_identity(
    tmp_path: Path,
) -> None:
    kb_dir = tmp_path / "knowledge"
    first = tmp_path / "storage.md"
    second = tmp_path / "network.md"
    first.write_text("# Alpha storage\n\nStorage definition.", encoding="utf-8")
    second.write_text("# Beta network\n\nNetwork definition.", encoding="utf-8")

    def response(content: str) -> str:
        request = json.loads(content)
        evidence_id = str(request["evidence"][0]["evidence_id"])
        storage = str(request["document_name"]).startswith("storage")
        title = "Alpha storage" if storage else "Beta network"
        platform = "Alpha" if storage else "Beta"
        tag = "storage" if storage else "network"
        return json.dumps(
            {
                "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
                "analysis_scope": "document",
                "document_description": f"{title} documentation.",
                "document_summary": [
                    {
                        "role": "purpose",
                        "text": f"Describe {title}.",
                        "source_evidence_ids": [evidence_id],
                    }
                ],
                "concepts": [
                    {
                        "title": title,
                        "aliases": ["cluster"],
                        "tags": [tag],
                        "claims": [
                            {
                                "role": "definition",
                                "text": f"{title} is a distinct subject.",
                                "applicability": {
                                    **_APPLICABILITY,
                                    "platform": platform,
                                },
                                "source_evidence_ids": [evidence_id],
                            }
                        ],
                    }
                ],
                "entities": [],
                "procedures": [],
            }
        )

    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(kb_dir)
    server = DesktopEngineServer(
        io.BytesIO(),
        io.BytesIO(),
        workspace=workspace,
        model_gateway_factory=lambda _kb_dir, _override: DesktopModelGateway(
            lambda request, _timeout: response(request.content)
        ),
    )
    server._handshake_complete = True
    _import(server, first, "generic-alias-first")
    _import(server, second, "generic-alias-second")

    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        rows = connection.execute(
            """
            SELECT title, identity_id
            FROM knowledge_generation_state AS state
            JOIN knowledge_generation_items AS items
              ON items.generation_id = state.current_generation_id
            WHERE items.kind = 'concept'
            ORDER BY title
            """
        ).fetchall()

    assert [str(row[0]) for row in rows] == ["Alpha storage", "Beta network"]
    assert len({str(row[1]) for row in rows}) == 2


def test_ambiguous_identity_cluster_carries_forward_its_prior_generated_item(
    tmp_path: Path,
) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "part-one.md"
    safe_source = tmp_path / "safe.md"
    source.write_text("# 第一部分\n\n初始化第一个节点。", encoding="utf-8")
    safe_source.write_text("# 独立概念\n\n这是独立概念的定义。", encoding="utf-8")

    def response(content: str) -> str:
        request = json.loads(content)
        if str(request["document_name"]).startswith("part-one"):
            return _analysis_response(content)
        evidence_id = str(request["evidence"][0]["evidence_id"])
        return json.dumps(
            {
                "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
                "analysis_scope": "document",
                "document_description": "独立概念资料。",
                "document_summary": [
                    {
                        "role": "purpose",
                        "text": "说明独立概念。",
                        "source_evidence_ids": [evidence_id],
                    }
                ],
                "concepts": [
                    {
                        "title": "独立概念",
                        "aliases": [],
                        "tags": ["独立"],
                        "claims": [_claim("definition", "独立概念有自己的定义。", evidence_id)],
                    }
                ],
                "entities": [],
                "procedures": [],
            },
            ensure_ascii=False,
        )

    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(kb_dir)
    server = DesktopEngineServer(
        io.BytesIO(),
        io.BytesIO(),
        workspace=workspace,
        model_gateway_factory=lambda _kb_dir, _override: DesktopModelGateway(
            lambda request, _timeout: response(request.content)
        ),
    )
    server._handshake_complete = True
    _import(server, source, "carry-forward-first")
    _import(server, safe_source, "carry-forward-safe")

    database = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database) as connection:
        original = connection.execute(
            """
            SELECT items.identity_id, items.content_markdown
            FROM knowledge_generation_state AS state
            JOIN knowledge_generation_items AS items
              ON items.generation_id = state.current_generation_id
            WHERE items.kind = 'procedure'
            """
        ).fetchone()
        assert original is not None
        competing_identity = "competing-identity"
        connection.execute(
            """
            INSERT INTO knowledge_identities (
                identity_id, kind, canonical_title, normalized_title,
                status, created_at, updated_at
            ) VALUES (?, 'procedure', '竞争部署流程', '竞争部署流程', 'active', ?, ?)
            """,
            (competing_identity, "2026-09-01T00:00:00Z", "2026-09-01T00:00:00Z"),
        )
        connection.executemany(
            """
            INSERT INTO knowledge_identity_aliases (
                identity_id, alias, normalized_alias, created_at
            ) VALUES (?, ?, ?, '2026-09-01T00:00:00Z')
            """,
            (
                (competing_identity, "双节点超融合部署", "双节点超融合部署"),
                (competing_identity, "双节点超融合安装", "双节点超融合安装"),
            ),
        )
        synthesize_qualified_corpus_in(connection, now="2026-09-01T00:00:01Z")
        connection.commit()
        current = connection.execute(
            """
            SELECT items.identity_id, items.content_markdown
            FROM knowledge_generation_state AS state
            JOIN knowledge_generation_items AS items
              ON items.generation_id = state.current_generation_id
            WHERE items.kind = 'procedure'
            """
        ).fetchall()

    assert (str(original[0]), str(original[1])) in {
        (str(identity_id), str(content)) for identity_id, content in current
    }


def test_invalid_candidate_generation_fails_gate_and_keeps_prior_current(
    tmp_path: Path,
) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "part-one.md"
    source.write_text("# 第一部分\n\n初始化第一个节点。", encoding="utf-8")
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(kb_dir)
    server = DesktopEngineServer(
        io.BytesIO(),
        io.BytesIO(),
        workspace=workspace,
        model_gateway_factory=lambda _kb_dir, _override: DesktopModelGateway(
            lambda request, _timeout: _analysis_response(request.content)
        ),
    )
    server._handshake_complete = True
    imported = _import(server, source, "generation-gate")
    document_id = str(imported["document"]["document_id"])

    database = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database) as connection:
        current_generation_id = int(
            connection.execute(
                "SELECT current_generation_id FROM knowledge_generation_state WHERE singleton = 1"
            ).fetchone()[0]
        )
        evidence_id = str(
            connection.execute(
                "SELECT evidence_id FROM evidence_occurrences WHERE document_id = ? LIMIT 1",
                (document_id,),
            ).fetchone()[0]
        )
        result = publish_corpus_generation_in(
            connection,
            current_generation_id=current_generation_id,
            changes=(
                KnowledgeGenerationChange(
                    document_id=document_id,
                    kind="concept",
                    title="Invalid empty concept",
                    normalized_title="invalid empty concept",
                    content_markdown="",
                    content_sha256="invalid",
                    sources=(
                        KnowledgeGenerationSource("source", evidence_id, "unsupported claim"),
                    ),
                    identity_id="invalid-empty-concept",
                ),
            ),
            document_ids=(document_id,),
            carry_forward_identity_ids=(),
            synthesis_schema_version="test.invalid",
            now="2026-09-01T00:00:00Z",
        )
        connection.commit()
        states = connection.execute(
            "SELECT generation_id, qualification_state "
            "FROM knowledge_generations ORDER BY generation_id"
        ).fetchall()
        still_current = int(
            connection.execute(
                "SELECT current_generation_id FROM knowledge_generation_state WHERE singleton = 1"
            ).fetchone()[0]
        )

    assert result == current_generation_id
    assert states[-1][1] == "failed"
    assert still_current == current_generation_id


def _claim(role: str, text: str, evidence_id: str) -> dict[str, object]:
    return {
        "role": role,
        "text": text,
        "applicability": _APPLICABILITY,
        "source_evidence_ids": [evidence_id],
    }


def _analysis_response(content: str) -> str:
    request = json.loads(content)
    evidence_id = str(request["evidence"][0]["evidence_id"])
    second_document = str(request["document_name"]).startswith("part-two")
    if second_document:
        title = "双节点超融合安装"
        aliases = ["双节点超融合部署"]
        claims = [
            _claim("prerequisite", "部署前确认两个节点网络互通。", evidence_id),
            _claim("step", "在第二个节点完成集群加入操作。", evidence_id),
            _claim("validation", "检查集群页面确认两个节点均为在线状态。", evidence_id),
        ]
        entities: list[dict[str, object]] = []
    else:
        title = "双节点超融合部署"
        aliases = ["双节点超融合安装"]
        claims = [
            _claim("purpose", "该流程用于部署双节点超融合环境。", evidence_id),
            _claim("step", "先在第一个节点完成管理服务初始化。", evidence_id),
            _claim("validation", "确认管理服务健康检查结果为成功。", evidence_id),
        ]
        entities = [
            {
                "title": r"C:\temp\install.log",
                "subtype": "File",
                "aliases": [],
                "tags": [],
                "claims": [_claim("detail", "该日志记录安装过程信息。", evidence_id)],
            }
        ]
    return json.dumps(
        {
            "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
            "analysis_scope": "document",
            "document_description": "双节点超融合部署资料。",
            "document_summary": [
                {
                    "role": "purpose",
                    "text": "本文说明双节点超融合环境的安装部署。",
                    "source_evidence_ids": [evidence_id],
                }
            ],
            "concepts": [],
            "entities": entities,
            "procedures": [
                {
                    "title": title,
                    "aliases": aliases,
                    "tags": ["超融合", "双节点"],
                    "claims": claims,
                }
            ],
        },
        ensure_ascii=False,
    )


def _import(server: DesktopEngineServer, source: Path, request_id: str) -> dict[str, object]:
    result = server._dispatch(
        DesktopRequest(
            request_id=request_id,
            method="workbench.import_text_document",
            params={"source_path": str(source)},
        ),
        cancel_event=None,
    )
    assert isinstance(result, dict)
    return result


def _descriptor(*, score: int, kind: str, authority_id: str) -> _ReadDescriptor:
    return _ReadDescriptor(
        score=score,
        hop=0,
        descriptor_kind="summary" if kind == "summary" else "catalog",
        authority="document_summary" if kind == "summary" else "published_generation",
        authority_id=authority_id,
        kind=kind,
        title=authority_id,
        metadata_json="{}",
        route=f"{kind}/{authority_id}",
        snapshot_token=authority_id,
    )


def test_navigation_reserves_one_relevant_document_summary() -> None:
    descriptors = (
        *(
            _descriptor(score=140 - ordinal, kind="concept", authority_id=f"concept-{ordinal}")
            for ordinal in range(4)
        ),
        _descriptor(score=80, kind="summary", authority_id="deployment-manual"),
    )

    selected = _select_read_descriptors(
        descriptors,
        max_reads=4,
        excluded_routes=frozenset(),
    )

    assert len(selected) == 4
    assert [item.authority_id for item in selected[:3]] == [
        "concept-0",
        "concept-1",
        "concept-2",
    ]
    assert selected[3].authority_id == "deployment-manual"


def test_navigation_requested_routes_cannot_reread_excluded_route() -> None:
    visited = _descriptor(score=140, kind="concept", authority_id="visited")
    unread = _descriptor(score=120, kind="concept", authority_id="unread")

    selected = _select_read_descriptors(
        (visited, unread),
        max_reads=1,
        excluded_routes=frozenset((visited.route,)),
        requested_routes=(visited.route,),
        requested_only=True,
    )

    assert selected == ()


def test_navigation_reads_only_the_routes_requested_by_an_adaptive_round() -> None:
    requested = _descriptor(score=80, kind="summary", authority_id="requested")
    higher_ranked = _descriptor(score=140, kind="concept", authority_id="higher-ranked")

    selected = _select_read_descriptors(
        (higher_ranked, requested),
        max_reads=12,
        excluded_routes=frozenset(),
        requested_routes=(requested.route,),
        requested_only=True,
    )

    assert selected == (requested,)


def test_navigation_ranks_procedural_sources_before_revision_history() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE source_documents (
            document_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            availability TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE document_ir_blocks (
            block_id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            heading_path TEXT NOT NULL,
            locator_json TEXT NOT NULL,
            text TEXT NOT NULL
        );
        CREATE TABLE evidence_occurrences (
            document_id TEXT NOT NULL,
            block_id TEXT NOT NULL,
            evidence_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL
        );
        INSERT INTO source_documents VALUES ('doc', '部署手册', 'available', '2026-01-01');
        """
    )
    blocks = (
        ("revision", 0, '["部署手册","修订记录"]', "revision-evidence"),
        ("partition", 1, '["双节点超融合部署","系统分区"]', "partition-evidence"),
        ("bcache", 2, '["双节点超融合部署","Bcache安装"]', "bcache-evidence"),
        (
            "socks",
            3,
            '["双节点超融合环境扩容部署","管理节点主备Socks5"]',
            "socks-evidence",
        ),
    )
    connection.executemany(
        "INSERT INTO document_ir_blocks VALUES (?, 'doc', ?, ?, '{}', ?)",
        ((block_id, ordinal, section, block_id) for block_id, ordinal, section, _ in blocks),
    )
    connection.executemany(
        "INSERT INTO evidence_occurrences VALUES ('doc', ?, ?, ?)",
        ((block_id, evidence_id, ordinal) for block_id, ordinal, _, evidence_id in blocks),
    )
    reads = (
        _NavigationRead(
            route="generated/concept/history",
            kind="concept",
            authority="published_generation",
            title="超融合部署历史",
            units=(_GuidanceUnit("超融合部署修订历史。", ("revision-evidence",)),),
            hop=0,
            snapshot_token="history",
        ),
        _NavigationRead(
            route="summaries/deployment-manual",
            kind="summary",
            authority="document_summary",
            title="部署手册",
            units=(
                _GuidanceUnit(
                    "System partitioning.",
                    ("partition-evidence",),
                    role="key_topic",
                ),
                _GuidanceUnit(
                    "Bcache installation.",
                    ("bcache-evidence",),
                    role="key_topic",
                ),
                _GuidanceUnit(
                    "适用于管理节点主备环境部署。",
                    ("socks-evidence",),
                    role="key_topic",
                ),
            ),
            hop=0,
            snapshot_token="summary",
        ),
    )

    ranked = _rank_source_evidence_ids_in(
        connection,
        reads,
        terms=("双节", "节点", "超融", "融合", "部署", "安装"),
        baseline_ids=frozenset(),
    )

    assert set(ranked[:2]) == {"partition-evidence", "bcache-evidence"}
    assert ranked[-1] == "revision-evidence"


def test_extended_import_admits_procedure_and_rejects_raw_literal(tmp_path: Path) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "part-one.md"
    source.write_text(
        "# 双节点超融合部署\n\n先初始化第一个节点，并通过健康检查验证服务。",
        encoding="utf-8",
    )
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(kb_dir)
    server = DesktopEngineServer(
        io.BytesIO(),
        io.BytesIO(),
        workspace=workspace,
        model_gateway_factory=lambda _kb_dir, _override: DesktopModelGateway(
            lambda request, _timeout: _analysis_response(request.content),
            provider_name="scripted",
            model_name="corpus-v1",
        ),
    )
    server._handshake_complete = True

    result = _import(server, source, "qualified-corpus-one")
    document_id = str(result["document"]["document_id"])

    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute(
            "SELECT provenance_state FROM document_summaries WHERE document_id = ?",
            (document_id,),
        ).fetchone() == ("source_backed",)
        assert connection.execute(
            "SELECT role, unit_text FROM document_summary_units WHERE document_id = ?",
            (document_id,),
        ).fetchone() == ("purpose", "本文说明双节点超融合环境的安装部署。")
        assert connection.execute(
            """
            SELECT admission_state, admission_reason
            FROM knowledge_document_candidates
            WHERE document_id = ? AND title = ?
            """,
            (document_id, r"C:\temp\install.log"),
        ).fetchone() == ("rejected", "raw_literal")
        generation = connection.execute(
            """
            SELECT generations.generation_id, generations.qualification_state,
                items.kind, items.title, items.content_markdown,
                generations.qualification_report_json
            FROM knowledge_generation_state AS state
            JOIN knowledge_generations AS generations
                ON generations.generation_id = state.current_generation_id
            JOIN knowledge_generation_items AS items
                ON items.generation_id = generations.generation_id
            """
        ).fetchone()
        assert generation is not None
        assert generation[1:4] == ("qualified", "procedure", "双节点超融合部署")
        assert "## 操作步骤" in str(generation[4])
        assert "## 验证" in str(generation[4])
        qualification = json.loads(str(generation[5]))
        assert qualification["schema_version"] == "openkb.corpus-benchmark.v2"
        assert qualification["passed"] is True
        assert qualification["structural_gate_passed"] is True
        assert qualification["noise_leakage_rate"] <= 0.02
        assert qualification["duplicate_identity_rate"] <= 0.05
        assert qualification["multi_document_topic_coverage"] >= 0.85
        assert qualification["procedure_stage_coverage"] >= 0.85
        real_corpus = qualification["real_corpus_benchmark"]
        assert real_corpus["suite_id"] == "ocloudware-dual-node-hyperconverged-v1"
        assert real_corpus["sample_count"] >= 3
        assert real_corpus["answer_completeness"] >= 0.85
        assert real_corpus["answer_correctness"] >= 0.95
        assert real_corpus["citation_precision"] >= 0.95
        assert real_corpus["unsupported_claim_count"] == 0
        assert real_corpus["retrieval_replay_passed"] is True
        assert real_corpus["automated_regression_passed"] is True
        assert real_corpus["passed"] is True
        assert len(real_corpus["corpus_digest"]) == 64
        assert len(real_corpus["contract_digest"]) == 64
        assert len(real_corpus["report_digest"]) == 64
        assert connection.execute(
            """
            SELECT COUNT(*) FROM knowledge_generation_state AS state
            JOIN knowledge_generation_items AS items
                ON items.generation_id = state.current_generation_id
            WHERE items.title = ?
            """,
            (r"C:\temp\install.log",),
        ).fetchone() == (0,)

    procedure_pages = tuple(
        path
        for path in (kb_dir / "knowledge-pages/generated/procedure").glob("*.md")
        if path.name != "index.md"
    )
    assert len(procedure_pages) == 1
    projected = procedure_pages[0].read_text(encoding="utf-8")
    assert "type: Procedure" in projected
    assert "canonical_evidence_id" in projected


def test_cross_document_model_aliases_require_identity_review(tmp_path: Path) -> None:
    kb_dir = tmp_path / "knowledge"
    first = tmp_path / "part-one.md"
    second = tmp_path / "part-two.md"
    first.write_text("# 第一部分\n\n初始化第一个节点并检查服务。", encoding="utf-8")
    second.write_text("# 第二部分\n\n第二个节点加入集群并检查节点状态。", encoding="utf-8")
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(kb_dir)
    server = DesktopEngineServer(
        io.BytesIO(),
        io.BytesIO(),
        workspace=workspace,
        model_gateway_factory=lambda _kb_dir, _override: DesktopModelGateway(
            lambda request, _timeout: _analysis_response(request.content),
            provider_name="scripted",
            model_name="corpus-v1",
        ),
    )
    server._handshake_complete = True

    _import(server, first, "qualified-corpus-first")
    database = kb_dir / ".openkb" / "state.sqlite3"
    _import(server, second, "qualified-corpus-second")

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            """
            SELECT items.identity_id, items.content_markdown
            FROM knowledge_generation_state AS state
            JOIN knowledge_generation_items AS items
                ON items.generation_id = state.current_generation_id
            WHERE items.kind = 'procedure'
            """
        ).fetchall()
        # The prior safe page remains current while the plausible second identity is
        # withheld for review; no duplicate provisional page may be published.
        assert len(rows) == 1
        assert connection.execute(
            """
            SELECT COUNT(DISTINCT sources.evidence_id)
            FROM knowledge_generation_state AS state
            JOIN knowledge_generation_item_sources AS sources
                ON sources.generation_id = state.current_generation_id
            JOIN knowledge_generation_items AS items
                ON items.generation_id = state.current_generation_id
                AND items.item_key = sources.item_key
            WHERE items.kind = 'procedure'
            """
        ).fetchone() == (1,)
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM knowledge_identity_review_items WHERE status = 'pending'"
            ).fetchone()[0]
            >= 1
        )


def test_virtual_navigation_reads_qualified_procedure_and_supplements_source(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(catalog_store, "start_catalog_rebuilds", lambda *_args, **_kwargs: None)
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "part-one.md"
    source.write_text(
        "\n\n".join(
            f"# 操作说明 {ordinal}\n\n执行部署动作 {ordinal} 并记录结果。"
            for ordinal in range(1, 8)
        ),
        encoding="utf-8",
    )

    def navigation_response(content: str) -> str:
        request = json.loads(content)
        evidence_ids = [str(item["evidence_id"]) for item in request["evidence"][:7]]
        roles = ("purpose", "step", "step", "step", "step", "validation", "rollback")
        return json.dumps(
            {
                "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
                "analysis_scope": "document",
                "document_description": "双节点超融合部署资料。",
                "document_summary": [
                    {
                        "role": "purpose",
                        "text": "本文说明双节点超融合环境的安装部署。",
                        "source_evidence_ids": [evidence_ids[0]],
                    }
                ],
                "concepts": [],
                "entities": [],
                "procedures": [
                    {
                        "title": "双节点超融合部署",
                        "aliases": ["双节点超融合安装"],
                        "tags": ["超融合", "双节点"],
                        "claims": [
                            _claim(role, f"双节点部署知识单元 {ordinal} 的完整说明。", evidence_id)
                            for ordinal, (role, evidence_id) in enumerate(
                                zip(roles, evidence_ids, strict=True), start=1
                            )
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        )

    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(kb_dir)
    assert rebuild_pending_catalog(kb_dir)
    server = DesktopEngineServer(
        io.BytesIO(),
        io.BytesIO(),
        workspace=workspace,
        model_gateway_factory=lambda _kb_dir, _override: DesktopModelGateway(
            lambda request, _timeout: navigation_response(request.content),
            provider_name="scripted",
            model_name="corpus-v1",
        ),
    )
    server._handshake_complete = True
    _import(server, source, "qualified-navigation")
    assert rebuild_pending_catalog(kb_dir)

    pack = DesktopEvidenceRetriever(kb_dir).retrieve("双节点超融合如何部署")

    assert pack.guidance
    assert pack.guidance[0].route.startswith("generated/procedure/")
    assert "双节点部署知识单元" in pack.guidance[0].content_markdown
    evidence_ids = {reference.evidence_id for reference in pack.evidence}
    assert set(pack.guidance[0].source_evidence_ids) <= evidence_ids
    assert pack.retrieval_trace.navigation_read_count <= 4
    assert 1 <= pack.retrieval_trace.source_window_count <= 4
    assert pack.retrieval_trace.link_hop_count <= 1
    assert pack.retrieval_trace.coverage_gate_state == "uncovered"
    assert pack.retrieval_trace.navigation_stop_reason == "model_degraded"
    assert "knowledge_navigation_step_unavailable" in pack.degradations
    assert any(
        "knowledge_navigation_source_window" in reference.channels for reference in pack.evidence
    )


def test_virtual_navigation_excludes_legacy_unqualified_generation(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(catalog_store, "start_catalog_rebuilds", lambda *_args, **_kwargs: None)
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "legacy.md"
    source.write_text("# Legacy Topic\n\nLegacy factual detail.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    assert rebuild_pending_catalog(kb_dir)
    DesktopTextImportService(kb_dir).import_text(source)
    assert rebuild_pending_catalog(kb_dir)

    pack = DesktopEvidenceRetriever(kb_dir).retrieve("Legacy Topic")

    assert pack.evidence
    assert pack.guidance
    assert all(not item.route.startswith("generated/") for item in pack.guidance)
    assert all(
        item.authority in {"document_summary", "source_document", "source_section"}
        for item in pack.guidance
    )
    assert all(
        not route.startswith("generated/") for route in pack.retrieval_trace.navigation_routes
    )
