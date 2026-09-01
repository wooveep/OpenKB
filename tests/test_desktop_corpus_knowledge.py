"""Acceptance checks for qualified document candidates and corpus synthesis."""

from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path

from openkb import desktop_catalog_store as catalog_store
from openkb.desktop_catalog_store import rebuild_pending_catalog
from openkb.desktop_engine import DesktopEngineServer, DesktopRequest
from openkb.desktop_import import DesktopTextImportService
from openkb.desktop_knowledge_analysis import KNOWLEDGE_ANALYSIS_SCHEMA_VERSION
from openkb.desktop_knowledge_navigation import (
    _GuidanceUnit,
    _NavigationRead,
    _rank_source_evidence_ids_in,
    _ReadDescriptor,
    _select_read_descriptors,
)
from openkb.desktop_model_gateway import DesktopModelGateway
from openkb.desktop_retrieval import DesktopEvidenceRetriever
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime

_APPLICABILITY = {
    "product_version": "V10",
    "platform": "OCloudView",
    "deployment_scenario": "双节点超融合",
    "time_boundary": "",
}


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
                items.kind, items.title, items.content_markdown
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


def test_cross_document_aliases_merge_into_one_stable_identity(tmp_path: Path) -> None:
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
    with sqlite3.connect(database) as connection:
        first_identity = str(
            connection.execute(
                """
                SELECT items.identity_id
                FROM knowledge_generation_state AS state
                JOIN knowledge_generation_items AS items
                    ON items.generation_id = state.current_generation_id
                WHERE items.kind = 'procedure'
                """
            ).fetchone()[0]
        )

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
        assert len(rows) == 1
        assert str(rows[0][0]) == first_identity
        content = str(rows[0][1])
        assert "先在第一个节点完成管理服务初始化" in content
        assert "在第二个节点完成集群加入操作" in content
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
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge_identity_review_items WHERE status = 'pending'"
        ).fetchone() == (0,)


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
    assert pack.retrieval_trace.source_window_count == 1
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
    assert pack.guidance == ()
    assert pack.retrieval_trace.navigation_read_count == 0
