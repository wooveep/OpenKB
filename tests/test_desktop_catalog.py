"""Deterministic corpus Catalog generation and vectorless routing behavior."""

from __future__ import annotations

import json
import posixpath
import sqlite3
from pathlib import Path

import pytest

from openkb import desktop_catalog_store as catalog_store
from openkb import desktop_retrieval, desktop_retrieval_candidates
from openkb.desktop_candidate_registry import publish_candidate_registry_generation_in
from openkb.desktop_catalog_retrieval import (
    CATALOG_DIRECT_WEIGHT,
    CATALOG_STALE_MULTIPLIER,
    catalog_route_rows_in,
)
from openkb.desktop_catalog_store import (
    lease_catalog_generation,
    lease_current_catalog,
    queue_catalog_rebuild_in,
    rebuild_pending_catalog,
)
from openkb.desktop_corpus_synthesis_generation import (
    bind_generation_graph_inputs_in,
    capture_corpus_candidate_inputs_in,
    create_pending_corpus_manifest_in,
    refresh_corpus_identity_mappings_in,
)
from openkb.desktop_import_runner import DesktopTextImportService
from openkb.desktop_knowledge_export import DesktopKnowledgeExportService
from openkb.desktop_knowledge_generations import (
    KnowledgeGenerationChange,
    KnowledgeGenerationSource,
    current_generation_id_in,
    knowledge_content_sha256,
    publish_generation_changes_in,
)
from openkb.desktop_knowledge_graph_interpretation import GraphDispositionCounts
from openkb.desktop_knowledge_graph_store import persist_semantic_graph_interpretation_in
from openkb.desktop_knowledge_inventory import eligible_knowledge_routes_in
from openkb.desktop_knowledge_pages import DesktopKnowledgePageService
from openkb.desktop_knowledge_relationship_migrations import (
    _MAX_RELATIONSHIP_ITEMS_PER_GENERATION,
    _MAX_RELATIONSHIPS_PER_GENERATION,
    _MAX_RELATIONSHIPS_PER_SOURCE_ITEM,
    relationship_rebuild_statements,
)
from openkb.desktop_knowledge_relationships import rebuild_generation_relationships_in
from openkb.desktop_knowledge_sources import stable_source_id
from openkb.desktop_okf_projection import materialize_okf_projection
from openkb.desktop_retrieval import DesktopEvidenceRetriever
from openkb.desktop_semantic_graph import (
    SemanticClaimReference,
    SemanticGraphInterpretation,
    SemanticRelation,
    load_semantic_graph_document_in,
    replace_document_semantic_relations_in,
)
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime


def _controlled_kb(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(catalog_store, "start_catalog_rebuilds", lambda *_args, **_kwargs: None)
    kb_dir = tmp_path / "knowledge"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    assert rebuild_pending_catalog(kb_dir)
    return kb_dir


def _source_backed_pages(kb_dir, tmp_path):
    source = tmp_path / "facts.md"
    source.write_text(
        "# Sources\n\nAlpha evidence only.\n\nAlpha evidence only.\n\nBeta evidence only.\n",
        encoding="utf-8",
    )
    DesktopTextImportService(kb_dir).import_text(source)
    assert rebuild_pending_catalog(kb_dir)
    pages = DesktopKnowledgePageService(kb_dir)
    alpha_source = pages.search_sources("Alpha evidence only")[0]
    beta_source = pages.search_sources("Beta evidence only")[0]

    beta = pages.save_draft(
        page_id=None,
        kind="concept",
        title="Configuration",
        content_markdown="Beta routing fact.",
    )
    pages.bind_source(beta.page_id, "Beta routing fact.", beta_source.evidence_id)
    pages.publish(beta.page_id)
    assert rebuild_pending_catalog(kb_dir)

    alpha = pages.save_draft(
        page_id=None,
        kind="concept",
        title="Alpha Router",
        content_markdown=(f"Alpha routing fact.\n\n[Configuration](/concept/{beta.page_id}.md)"),
    )
    pages.bind_source(alpha.page_id, "Alpha routing fact.", alpha_source.evidence_id)
    return pages, alpha, beta, alpha_source.evidence_id, beta_source.evidence_id


def test_catalog_uses_published_snapshot_and_routes_one_low_weight_link(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kb_dir = _controlled_kb(tmp_path, monkeypatch)
    pages, alpha, beta, alpha_evidence, beta_evidence = _source_backed_pages(kb_dir, tmp_path)
    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        before_revision = connection.execute(
            "SELECT source_revision FROM knowledge_catalog_state WHERE singleton = 1"
        ).fetchone()[0]
    pages.save_draft(
        page_id=beta.page_id,
        kind="concept",
        title="Configuration",
        content_markdown="Unpublished replacement.",
    )
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT source_revision FROM knowledge_catalog_state WHERE singleton = 1"
        ).fetchone() == (before_revision,)

    pages.publish(alpha.page_id)
    with sqlite3.connect(database_path) as connection:
        stale = connection.execute(
            "SELECT current_generation_id, is_stale FROM knowledge_catalog_state"
        ).fetchone()
        assert stale[0] is not None and stale[1] == 1
    direct = DesktopEvidenceRetriever(kb_dir).retrieve("Alpha Router")
    assert alpha_evidence in {item.evidence_id for item in direct.evidence}
    assert "catalog_stale" in direct.degradations
    catalog_trace = next(
        channel for channel in direct.retrieval_trace.channels if channel.channel == "catalog"
    )
    assert "catalog_stale" in catalog_trace.degradation_reasons

    assert rebuild_pending_catalog(kb_dir)
    routed = DesktopEvidenceRetriever(kb_dir).retrieve("Alpha Router")
    by_evidence = {item.evidence_id: item for item in routed.evidence}
    assert alpha_evidence in by_evidence
    assert beta_evidence in by_evidence
    assert "catalog" in by_evidence[beta_evidence].channels
    with sqlite3.connect(database_path) as connection:
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(knowledge_catalog_nodes)")
        }
        links = connection.execute(
            """
            SELECT COUNT(*) FROM knowledge_catalog_links AS links
            JOIN knowledge_catalog_state AS state
                ON state.current_generation_id = links.generation_id
            WHERE links.from_node_id = ? AND links.to_node_id = ?
            """,
            (f"page:{alpha.page_id}", f"page:{beta.page_id}"),
        ).fetchone()
    assert "content_markdown" not in columns and "excerpt" not in columns
    assert links == (1,)

    pages.set_stale_after(alpha.page_id, "2020-01-01T00:00:00+00:00")
    assert rebuild_pending_catalog(kb_dir)
    with sqlite3.connect(database_path) as connection:
        generation_id = str(
            connection.execute(
                "SELECT current_generation_id FROM knowledge_catalog_state"
            ).fetchone()[0]
        )
        catalog_rows = catalog_route_rows_in(
            connection, generation_id, ("alpha", "router"), limit=12
        )
    alpha_row = next(row for row in catalog_rows if str(row[0]) == alpha_evidence)
    assert float(alpha_row[6]) == pytest.approx(CATALOG_DIRECT_WEIGHT * CATALOG_STALE_MULTIPLIER)

    pages.deprecate(beta.page_id)
    assert rebuild_pending_catalog(kb_dir)
    without_deprecated_hop = DesktopEvidenceRetriever(kb_dir).retrieve("Alpha Router")
    assert beta_evidence not in {item.evidence_id for item in without_deprecated_hop.evidence}


def test_catalog_persists_typed_links_with_routes_and_source_bindings(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kb_dir = _controlled_kb(tmp_path, monkeypatch)
    pages, alpha, beta, alpha_evidence, beta_evidence = _source_backed_pages(kb_dir, tmp_path)
    pages.publish(alpha.page_id)
    assert rebuild_pending_catalog(kb_dir)

    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        generation_id = str(
            connection.execute(
                "SELECT current_generation_id FROM knowledge_catalog_state"
            ).fetchone()[0]
        )
        reference = connection.execute(
            """
            SELECT source_route, target_route, relation_kind, provenance,
                lifecycle_eligible
            FROM knowledge_catalog_relationships
            WHERE generation_id = ? AND source_node_id = ? AND target_node_id = ?
            """,
            (generation_id, f"page:{alpha.page_id}", f"page:{beta.page_id}"),
        ).fetchone()
        assert reference is not None
        assert reference[0].startswith("concepts/")
        assert reference[1].startswith("concepts/")
        assert reference[2:] == ("references", "published_markdown_with_source_bindings", 1)
        bound_evidence = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT evidence_id FROM knowledge_catalog_relationship_sources
                WHERE generation_id = ? AND source_node_id = ? AND target_node_id = ?
                    AND relation_kind = 'references'
                """,
                (generation_id, f"page:{alpha.page_id}", f"page:{beta.page_id}"),
            ).fetchall()
        }
        supported_by = connection.execute(
            """
            SELECT target_route, relation_kind, provenance, lifecycle_eligible
            FROM knowledge_catalog_relationships
            WHERE generation_id = ? AND source_node_id = ?
                AND relation_kind = 'supported_by'
            """,
            (generation_id, f"page:{alpha.page_id}"),
        ).fetchone()

    assert bound_evidence == {alpha_evidence, beta_evidence}
    assert supported_by is not None
    assert supported_by[0].startswith("sources/")
    assert supported_by[1:] == ("supported_by", "knowledge_source_binding", 1)


def test_catalog_cannot_route_through_knowledge_with_one_unavailable_binding(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kb_dir = _controlled_kb(tmp_path, monkeypatch)
    unavailable_source = tmp_path / "unavailable.md"
    available_source = tmp_path / "available.md"
    unavailable_source.write_text("# Retired\n\nRetired installation evidence.", encoding="utf-8")
    available_source.write_text(
        "# Current\n\nCurrent deployment evidence must not leak through an ineligible page.",
        encoding="utf-8",
    )
    unavailable_document = DesktopTextImportService(kb_dir).import_text(unavailable_source).document
    DesktopTextImportService(kb_dir).import_text(available_source)
    assert rebuild_pending_catalog(kb_dir)
    pages = DesktopKnowledgePageService(kb_dir)
    unavailable_evidence = pages.search_sources("Retired installation evidence")[0]
    available_evidence = pages.search_sources("Current deployment evidence")[0]
    page = pages.save_draft(
        page_id=None,
        kind="procedure",
        title="Mixed Eligibility Router",
        content_markdown="Retired prerequisite.\n\nCurrent step.",
    )
    pages.bind_source(page.page_id, "Retired prerequisite.", unavailable_evidence.evidence_id)
    pages.bind_source(page.page_id, "Current step.", available_evidence.evidence_id)
    pages.publish(page.page_id)
    assert rebuild_pending_catalog(kb_dir)

    database = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE source_documents SET availability = 'failed' WHERE document_id = ?",
            (unavailable_document.document_id,),
        )
        connection.commit()
    assert rebuild_pending_catalog(kb_dir)

    with sqlite3.connect(database) as connection:
        assert not any(
            route.authority == "user_revision" and route.identity == page.page_id
            for route in eligible_knowledge_routes_in(connection)
        )
        generation_id = str(
            connection.execute(
                "SELECT current_generation_id FROM knowledge_catalog_state"
            ).fetchone()[0]
        )
        assert (
            connection.execute(
                "SELECT 1 FROM knowledge_catalog_nodes WHERE generation_id = ? AND node_id = ?",
                (generation_id, f"page:{page.page_id}"),
            ).fetchone()
            is None
        )
        routed = catalog_route_rows_in(
            connection, generation_id, ("mixed", "eligibility", "router"), limit=12
        )
    assert available_evidence.evidence_id not in {str(row[0]) for row in routed}


def test_relationship_inference_has_item_fanout_and_generation_bounds() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE knowledge_generation_items (
            generation_id INTEGER NOT NULL,
            item_key TEXT NOT NULL,
            title TEXT NOT NULL,
            PRIMARY KEY(generation_id, item_key)
        );
        CREATE TABLE knowledge_generation_item_sources (
            generation_id INTEGER NOT NULL,
            item_key TEXT NOT NULL,
            source_id TEXT NOT NULL,
            evidence_id TEXT NOT NULL,
            claim_text TEXT NOT NULL
        );
        CREATE TABLE knowledge_generation_relationships (
            generation_id INTEGER NOT NULL,
            source_item_key TEXT NOT NULL,
            target_item_key TEXT NOT NULL,
            relation_kind TEXT NOT NULL,
            provenance TEXT NOT NULL,
            PRIMARY KEY(generation_id, source_item_key, target_item_key, relation_kind)
        );
        """
    )
    item_count = _MAX_RELATIONSHIP_ITEMS_PER_GENERATION + 1
    items = [(1, f"item-{index:04d}", "Shared target") for index in range(item_count)]
    connection.executemany("INSERT INTO knowledge_generation_items VALUES (?, ?, ?)", items)
    connection.executemany(
        "INSERT INTO knowledge_generation_item_sources VALUES (?, ?, ?, ?, ?)",
        (
            (generation_id, item_key, f"source-{item_key}", f"evidence-{item_key}", "Shared target")
            for generation_id, item_key, _title in items
        ),
    )

    connection.execute(relationship_rebuild_statements()[0], (1,))

    relationship_count = int(
        connection.execute("SELECT COUNT(*) FROM knowledge_generation_relationships").fetchone()[0]
    )
    maximum_fanout = int(
        connection.execute(
            """
            SELECT COALESCE(MAX(fanout), 0) FROM (
                SELECT COUNT(*) AS fanout FROM knowledge_generation_relationships
                GROUP BY generation_id, source_item_key
            )
            """
        ).fetchone()[0]
    )
    endpoints = {
        str(value)
        for row in connection.execute(
            "SELECT source_item_key, target_item_key FROM knowledge_generation_relationships"
        )
        for value in row
    }
    assert relationship_count == _MAX_RELATIONSHIPS_PER_GENERATION
    assert maximum_fanout <= _MAX_RELATIONSHIPS_PER_SOURCE_ITEM
    assert f"item-{_MAX_RELATIONSHIP_ITEMS_PER_GENERATION:04d}" not in endpoints


def test_generated_relations_are_structured_authority_and_markdown_projection(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kb_dir = _controlled_kb(tmp_path, monkeypatch)
    source = tmp_path / "deployment.md"
    source.write_text(
        "# Deployment\n\nInstall Glusterfs before creating the replicated volume.\n\n"
        "Configure Glusterfs peers before volume creation.\n\n"
        "Validate Glusterfs health before continuing.\n\n"
        "Record Glusterfs status for deployment audit.\n\n"
        "# Storage\n\nGlusterfs provides replicated storage for both nodes.\n\n"
        "Glusterfs replicates storage across the nodes.\n\n"
        "Glusterfs storage remains available after one node fails.\n\n"
        "Glusterfs storage health is checked after deployment.\n",
        encoding="utf-8",
    )
    imported = DesktopTextImportService(kb_dir).import_text(source)
    procedure_content = "## Steps\n\n1. Install Glusterfs before creating the replicated volume."
    entity_content = "Glusterfs provides replicated storage for both nodes."
    database = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database) as connection:
        evidence = connection.execute(
            """
            SELECT occurrences.evidence_id, blocks.text
            FROM evidence_occurrences AS occurrences
            JOIN document_ir_blocks AS blocks ON blocks.block_id = occurrences.block_id
            WHERE occurrences.document_id = ? AND blocks.text LIKE '%Glusterfs%'
            ORDER BY occurrences.ordinal
            """,
            (imported.document.document_id,),
        ).fetchall()
        procedure_sources = tuple(
            KnowledgeGenerationSource(
                stable_source_id(str(evidence_id)),
                str(evidence_id),
                str(claim_text),
            )
            for evidence_id, claim_text in evidence[:4]
        )
        entity_sources = tuple(
            KnowledgeGenerationSource(
                stable_source_id(str(evidence_id)),
                str(evidence_id),
                str(claim_text),
            )
            for evidence_id, claim_text in evidence[4:]
        )
        assert len(procedure_sources) == len(entity_sources) == 4
        now = "2026-09-02T00:00:00+00:00"
        candidates = (
            (
                "procedure-candidate",
                "procedure",
                "Dual-node deployment",
                "dual-node deployment",
                procedure_sources,
            ),
            (
                "entity-candidate",
                "entity",
                "Glusterfs",
                "glusterfs",
                entity_sources,
            ),
        )
        for candidate_id, kind, title, normalized_title, sources in candidates:
            connection.execute(
                """
                INSERT INTO knowledge_document_candidates (
                    candidate_id, document_id, kind, title, normalized_title,
                    entity_subtype, aliases_json, tags_json, admission_state,
                    admission_reason, analysis_provenance_json, created_at
                ) VALUES (?, ?, ?, ?, ?, NULL, '[]', '[]', 'admitted',
                    'eligible', '{}', ?)
                """,
                (
                    candidate_id,
                    imported.document.document_id,
                    kind,
                    title,
                    normalized_title,
                    now,
                ),
            )
            for claim_ordinal, generation_source in enumerate(sources):
                connection.execute(
                    """
                    INSERT INTO knowledge_document_candidate_claims (
                        candidate_id, claim_ordinal, role, claim_text, applicability_json
                    ) VALUES (?, ?, 'fact', ?, '[]')
                    """,
                    (candidate_id, claim_ordinal, generation_source.claim_text),
                )
                connection.execute(
                    """
                    INSERT INTO knowledge_document_candidate_claim_sources (
                        candidate_id, claim_ordinal, evidence_id
                    ) VALUES (?, ?, ?)
                    """,
                    (candidate_id, claim_ordinal, generation_source.evidence_id),
                )
        identities = (
            (
                "procedure-identity",
                "procedure-candidate",
                "procedure",
                "Dual-node deployment",
                "dual-node deployment",
            ),
            (
                "entity-identity",
                "entity-candidate",
                "entity",
                "Glusterfs",
                "glusterfs",
            ),
        )
        for identity_id, candidate_id, kind, title, normalized_title in identities:
            connection.execute(
                """
                INSERT INTO knowledge_identities (
                    identity_id, kind, canonical_title, normalized_title,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'active', ?, ?)
                """,
                (identity_id, kind, title, normalized_title, now, now),
            )
            connection.execute(
                """
                INSERT INTO knowledge_identity_candidates (
                    identity_id, candidate_id, match_basis, created_at
                ) VALUES (?, ?, 'exact_title', ?)
                """,
                (identity_id, candidate_id, now),
            )
        registry = publish_candidate_registry_generation_in(
            connection,
            document_id=imported.document.document_id,
            analysis_provenance_json="{}",
            now=now,
        )
        assert registry.generation is not None
        graph_document = load_semantic_graph_document_in(connection, imported.document.document_id)
        assert graph_document is not None
        interpretation = SemanticGraphInterpretation(
            relations=(
                SemanticRelation(
                    "procedure-candidate",
                    "entity-candidate",
                    "USES",
                    (SemanticClaimReference("procedure-candidate", 0),),
                    (procedure_sources[0].evidence_id,),
                    "[]",
                ),
            ),
            lifecycle="completed",
            quality="full",
            issues=(),
            counts=GraphDispositionCounts(retained=3, weakened=0, rejected=0),
        )
        graph_result_id = persist_semantic_graph_interpretation_in(
            connection,
            imported.document.document_id,
            interpretation,
            node_count=2,
            capability_identity="catalog-test",
            prompt_contract_digest="catalog-test",
            candidate_generation_id=registry.generation.generation_id,
            candidate_generation_digest=registry.generation.registry_digest,
        )
        replace_document_semantic_relations_in(
            connection,
            graph_document,
            interpretation,
            graph_result_id=graph_result_id,
        )
        candidate_inputs = capture_corpus_candidate_inputs_in(connection)
        generation_id = publish_generation_changes_in(
            connection,
            current_generation_id=current_generation_id_in(connection),
            changes=(
                KnowledgeGenerationChange(
                    document_id=imported.document.document_id,
                    kind="procedure",
                    title="Dual-node deployment",
                    normalized_title="dual-node deployment",
                    content_markdown=procedure_content,
                    content_sha256=knowledge_content_sha256(procedure_content),
                    sources=procedure_sources,
                    identity_id="procedure-identity",
                ),
                KnowledgeGenerationChange(
                    document_id=imported.document.document_id,
                    kind="entity",
                    title="Glusterfs",
                    normalized_title="glusterfs",
                    content_markdown=entity_content,
                    content_sha256=knowledge_content_sha256(entity_content),
                    sources=entity_sources,
                    identity_id="entity-identity",
                ),
            ),
            now=now,
        )
        create_pending_corpus_manifest_in(
            connection,
            generation_id=generation_id,
            parent_generation_id=None,
            document_ids=(imported.document.document_id,),
            candidate_inputs=candidate_inputs,
            now=now,
        )
        refresh_corpus_identity_mappings_in(connection, generation_id, now=now)
        bind_generation_graph_inputs_in(connection, generation_id, now=now)
        rebuild_generation_relationships_in(connection, generation_id)
        connection.execute(
            "UPDATE knowledge_generations SET qualification_state = 'qualified' "
            "WHERE generation_id = ?",
            (generation_id,),
        )
        connection.execute(
            "UPDATE knowledge_generation_manifests "
            "SET lifecycle_state = 'active', dossier_state = 'ready', graph_state = 'ready' "
            "WHERE generation_id = ?",
            (generation_id,),
        )
        connection.execute(
            "INSERT INTO knowledge_generation_state (singleton, current_generation_id) "
            "VALUES (1, ?) ON CONFLICT(singleton) DO UPDATE SET "
            "current_generation_id = excluded.current_generation_id",
            (generation_id,),
        )
        connection.commit()

        relation = connection.execute(
            """
            SELECT source_item_key, target_item_key, relation_kind, provenance
            FROM knowledge_generation_relationships
            WHERE generation_id = ? AND source_item_key = 'procedure-identity'
                AND target_item_key = 'entity-identity'
            """,
            (generation_id,),
        ).fetchone()
        assert relation == (
            "procedure-identity",
            "entity-identity",
            "USES",
            "semantic_relation_analysis",
        )
        relationship_sources = connection.execute(
            """
            SELECT binding_role, evidence_id
            FROM knowledge_generation_relationship_sources
            WHERE generation_id = ? AND source_item_key = 'procedure-identity'
                AND target_item_key = 'entity-identity'
            ORDER BY binding_role, evidence_id
            """,
            (generation_id,),
        ).fetchall()
        assert [role for role, _evidence_id in relationship_sources].count("source") == 4
        assert [role for role, _evidence_id in relationship_sources].count("target") == 4
        assert [role for role, _evidence_id in relationship_sources].count("assertion") == 1
        assert {evidence_id for role, evidence_id in relationship_sources if role == "source"} == {
            source.evidence_id for source in procedure_sources
        }
        assert {evidence_id for role, evidence_id in relationship_sources if role == "target"} == {
            source.evidence_id for source in entity_sources
        }

    DesktopKnowledgeBaseRuntime().open(kb_dir)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT DISTINCT generation_id FROM knowledge_generation_relationships"
        ).fetchall() == [(generation_id,)]
        assert connection.execute(
            """
            SELECT MAX(binding_count) FROM (
                SELECT COUNT(*) AS binding_count
                FROM knowledge_generation_relationship_sources
                GROUP BY generation_id, source_item_key, target_item_key,
                    relation_kind, binding_role
            )
            """
        ).fetchone() == (4,)

    assert "[" not in procedure_content
    assert rebuild_pending_catalog(kb_dir)
    with sqlite3.connect(database) as connection:
        catalog_generation = str(
            connection.execute(
                "SELECT current_generation_id FROM knowledge_catalog_state"
            ).fetchone()[0]
        )
        assert connection.execute(
            """
            SELECT relation_kind, provenance, lifecycle_eligible
            FROM knowledge_catalog_relationships
            WHERE generation_id = ? AND source_node_id = 'generated:procedure-identity'
                AND target_node_id = 'generated:entity-identity'
            """,
            (catalog_generation,),
        ).fetchone() == ("USES", "semantic_relation_analysis", 1)

    materialize_okf_projection(kb_dir)
    projected = (
        kb_dir / "knowledge-pages" / "generated" / "procedure" / "procedure-identity.md"
    ).read_text(encoding="utf-8")
    assert "[Glusterfs](../entity/entity-identity.md)" in projected

    destination = tmp_path / "exports"
    destination.mkdir()
    exporter = DesktopKnowledgeExportService(kb_dir)
    preview = exporter.preview(mode="portable_wiki")
    exported = exporter.export(
        destination,
        mode="portable_wiki",
        expected_snapshot_id=preview.snapshot_id,
    )
    root = Path(exported.path)
    manifest = json.loads((root / "wiki-manifest.json").read_text(encoding="utf-8"))
    routes = {entry["identity"]: entry["path"] for entry in manifest["routes"]}
    procedure_path = routes["procedure-identity"]
    entity_path = routes["entity-identity"]
    relative_entity = posixpath.relpath(entity_path, posixpath.dirname(procedure_path))
    procedure_wiki = (root / procedure_path).read_text(encoding="utf-8")
    assert f"[Glusterfs]({relative_entity})" in procedure_wiki


def test_catalog_failure_serves_previous_generation_and_task_reason(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kb_dir = _controlled_kb(tmp_path, monkeypatch)
    pages, alpha, _beta, _alpha_evidence, _beta_evidence = _source_backed_pages(kb_dir, tmp_path)
    pages.publish(alpha.page_id)
    assert rebuild_pending_catalog(kb_dir)
    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        previous = connection.execute(
            "SELECT current_generation_id FROM knowledge_catalog_state"
        ).fetchone()[0]

    pages.deprecate(alpha.page_id)
    monkeypatch.setattr(
        catalog_store,
        "build_catalog_snapshot_in",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("injected catalog fault")),
    )
    assert not rebuild_pending_catalog(kb_dir)
    task = DesktopTextImportService(kb_dir).list_import_jobs()["catalog_rebuild"]
    assert task["status"] == "failed"
    assert task["stale_serving"] is True
    assert task["error_code"] == "knowledge_catalog_build_failed"
    assert "injected catalog fault" in task["error_reason"]
    with lease_current_catalog(kb_dir) as lease:
        assert lease is not None
        assert lease.generation_id == previous
        assert lease.is_stale


def test_catalog_retries_one_transient_build_failure(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(catalog_store, "start_catalog_rebuilds", lambda *_args, **_kwargs: None)
    kb_dir = tmp_path / "knowledge"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    original_build = catalog_store.build_catalog_snapshot_in
    attempts = 0

    def flaky_build(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient catalog fault")
        return original_build(*args, **kwargs)

    monkeypatch.setattr(catalog_store, "build_catalog_snapshot_in", flaky_build)
    assert rebuild_pending_catalog(kb_dir)
    task = DesktopTextImportService(kb_dir).list_import_jobs()["catalog_rebuild"]
    assert attempts == 2
    assert task["status"] == "completed"
    assert task["attempt_count"] == 2


def test_initial_catalog_failure_does_not_claim_a_stale_generation(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(catalog_store, "start_catalog_rebuilds", lambda *_args, **_kwargs: None)
    kb_dir = tmp_path / "knowledge"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    monkeypatch.setattr(
        catalog_store,
        "build_catalog_snapshot_in",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("persistent catalog fault")),
    )

    assert not rebuild_pending_catalog(kb_dir)
    task = DesktopTextImportService(kb_dir).list_import_jobs()["catalog_rebuild"]
    assert task["status"] == "failed"
    assert task["attempt_count"] == 2
    assert task["current_generation_id"] is None
    assert task["stale_serving"] is False


def test_catalog_faults_drop_only_the_optional_channel(tmp_path, monkeypatch) -> None:
    kb_dir = _controlled_kb(tmp_path, monkeypatch)
    source = tmp_path / "baseline.md"
    source.write_text("# Baseline\n\nAlpha baseline evidence remains available.", encoding="utf-8")
    DesktopTextImportService(kb_dir).import_text(source)
    assert rebuild_pending_catalog(kb_dir)

    with monkeypatch.context() as scoped:
        scoped.setattr(
            desktop_retrieval_candidates,
            "catalog_route_rows_in",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("query fault")),
        )
        query_failure = DesktopEvidenceRetriever(kb_dir).retrieve("Alpha baseline evidence")

    class BrokenLease:
        def __enter__(self):
            raise RuntimeError("lease fault")

        def __exit__(self, *_args):
            return False

    with monkeypatch.context() as scoped:
        scoped.setattr(
            desktop_retrieval,
            "lease_catalog_generation",
            lambda _kb_dir, _generation_id: BrokenLease(),
        )
        lease_failure = DesktopEvidenceRetriever(kb_dir).retrieve("Alpha baseline evidence")

    for pack in (query_failure, lease_failure):
        assert any("Alpha baseline evidence" in item.excerpt for item in pack.evidence)
        assert "catalog_query_failed" in pack.degradations
        catalog_trace = next(
            channel for channel in pack.retrieval_trace.channels if channel.channel == "catalog"
        )
        assert "catalog_query_failed" in catalog_trace.degradation_reasons


def test_catalog_retains_recent_generation_until_an_older_reader_releases(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kb_dir = _controlled_kb(tmp_path, monkeypatch)
    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with lease_current_catalog(kb_dir) as first:
        assert first is not None
        for reason in ("test-second", "test-third"):
            with sqlite3.connect(database_path) as connection:
                with connection:
                    queue_catalog_rebuild_in(connection, reason)
            assert rebuild_pending_catalog(kb_dir)
        with sqlite3.connect(database_path) as connection:
            generations = connection.execute(
                "SELECT generation_id FROM knowledge_catalog_generations"
            ).fetchall()
        assert len(generations) == 3
    with sqlite3.connect(database_path) as connection:
        remaining = connection.execute(
            "SELECT status, COUNT(*) FROM knowledge_catalog_generations GROUP BY status"
        ).fetchall()
    assert sorted(remaining) == [("current", 1), ("recent", 1)]


def test_catalog_reader_leases_are_scoped_to_their_knowledge_base(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_kb = _controlled_kb(tmp_path / "first", monkeypatch)
    second_kb = _controlled_kb(tmp_path / "second", monkeypatch)
    second_database = second_kb / ".openkb" / "state.sqlite3"

    with lease_current_catalog(first_kb):
        for reason in ("second-generation", "third-generation"):
            with sqlite3.connect(second_database) as connection:
                with connection:
                    queue_catalog_rebuild_in(connection, reason)
            assert rebuild_pending_catalog(second_kb)

    with sqlite3.connect(second_database) as connection:
        remaining = connection.execute(
            "SELECT status, COUNT(*) FROM knowledge_catalog_generations GROUP BY status"
        ).fetchall()
    assert sorted(remaining) == [("current", 1), ("recent", 1)]


def test_catalog_snapshot_lease_reads_the_pinned_previous_generation(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kb_dir = _controlled_kb(tmp_path, monkeypatch)
    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with lease_current_catalog(kb_dir) as first:
        assert first is not None
        with sqlite3.connect(database_path) as connection:
            with connection:
                queue_catalog_rebuild_in(connection, "snapshot-next")
        assert rebuild_pending_catalog(kb_dir)

        with lease_catalog_generation(kb_dir, first.generation_id) as pinned:
            assert pinned == first
