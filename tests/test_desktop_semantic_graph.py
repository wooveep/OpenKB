"""Contract checks for the canonical Knowledge Identity Graph."""

from __future__ import annotations

import json
import sqlite3

import pytest

import openkb.desktop_workspace as workspace_module
from openkb.desktop_candidate_registry import publish_candidate_registry_generation_in
from openkb.desktop_import_runner import DesktopTextImportService
from openkb.desktop_knowledge_generations import knowledge_content_sha256
from openkb.desktop_knowledge_graph import PinnedGraphGenerations, local_graph_evidence_ids
from openkb.desktop_knowledge_graph_tasks import DesktopKnowledgeGraphExtractionTasks
from openkb.desktop_knowledge_relationships import (
    generation_relationship_issues_in,
    rebuild_generation_relationships_in,
)
from openkb.desktop_model_gateway import (
    DesktopModelGateway,
    DesktopModelOutputObservations,
    DesktopModelResult,
)
from openkb.desktop_prompt_contracts import prompt_contract_for
from openkb.desktop_semantic_graph import (
    SemanticGraphCandidate,
    SemanticGraphCapacityError,
    SemanticGraphClaim,
    SemanticGraphDocument,
    SemanticGraphStoredDataError,
    SemanticRelationBoundary,
    plan_semantic_relation_batches,
)
from openkb.desktop_semantic_graph_contract import semantic_relation_output_schema
from openkb.desktop_semantic_graph_service import DesktopSemanticGraphService
from openkb.desktop_structured_output import (
    DesktopStructuredOutputInvalidError,
    DesktopValidatedStructuredOutput,
)
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime, desktop_state_database_path


def test_v57_workspace_migrates_to_semantic_relation_tables_without_model_work(
    tmp_path, monkeypatch
) -> None:
    kb_dir = tmp_path / "v57-knowledge"
    v57_migrations = tuple(
        migration for migration in workspace_module._MIGRATIONS if migration[0] <= 57
    )
    with monkeypatch.context() as v57_context:
        v57_context.setattr(workspace_module, "_MIGRATIONS", v57_migrations)
        activation = DesktopKnowledgeBaseRuntime().create(kb_dir)
        assert activation.knowledge_base.schema_version == 57

    migrated = DesktopKnowledgeBaseRuntime().open(kb_dir)

    assert migrated.knowledge_base.schema_version == 63
    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        relationship_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(knowledge_generation_relationships)")
        }
        binding_roles = str(
            connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = 'knowledge_generation_relationship_sources'"
            ).fetchone()[0]
        )
        assert {
            "relation_kind",
            "applicability_json",
            "provenance",
        } <= relationship_columns
        assert "assertion" in binding_roles
        assert "graph_result_id" in {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(knowledge_document_relationships)")
        }
        assert connection.execute(
            "SELECT version FROM schema_migrations WHERE version = 59"
        ).fetchone() == (59,)
        assert {
            "generation_id",
            "status",
            "phase",
            "retry_scope",
            "execution_token",
            "error_code",
        } <= {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(knowledge_corpus_synthesis_tasks)")
        }


def _claim(
    candidate_id: str,
    ordinal: int,
    text: str,
    *,
    evidence_id: str | None = None,
) -> SemanticGraphClaim:
    return SemanticGraphClaim(
        candidate_id=candidate_id,
        claim_ordinal=ordinal,
        role="relation",
        text=text,
        applicability_json="{}",
        evidence_ids=(evidence_id or f"evidence-{candidate_id}-{ordinal}",),
    )


def _candidate(
    candidate_id: str,
    kind: str,
    title: str,
    *claims: SemanticGraphClaim,
) -> SemanticGraphCandidate:
    return SemanticGraphCandidate(
        candidate_id=candidate_id,
        kind=kind,
        title=title,
        aliases=(),
        entity_subtype="component" if kind == "entity" else None,
        claims=claims,
    )


def _one_batch() -> object:
    procedure_claim = _claim("restore", 0, "数据库同步恢复流程使用 MariaDB。")
    document = SemanticGraphDocument(
        document_id="document-1",
        document_name="guide.md",
        candidates=(
            _candidate("restore", "procedure", "数据库同步恢复流程", procedure_claim),
            _candidate(
                "mariadb",
                "entity",
                "MariaDB",
                _claim("mariadb", 0, "MariaDB 是数据库服务。"),
            ),
        ),
    )
    return plan_semantic_relation_batches(document, input_budget_tokens=4_000)[0]


def test_relation_contract_cannot_create_or_relabel_identity_nodes() -> None:
    schema = semantic_relation_output_schema()

    assert set(schema["properties"]) == {"relations"}
    assert set(schema["properties"]["relations"]["items"]["properties"]) == {
        "source_candidate_id",
        "target_candidate_id",
        "type",
        "supporting_claims",
    }

    interpreted = SemanticRelationBoundary.interpret(
        json.dumps(
            {
                "relations": [
                    {
                        "source_candidate_id": "restore",
                        "target_candidate_id": "invented-database",
                        "type": "USES",
                        "supporting_claims": [{"candidate_id": "restore", "claim_ordinal": 0}],
                    }
                ]
            }
        ),
        _one_batch(),
    )

    assert interpreted.relations == ()
    assert interpreted.lifecycle == "failed"
    assert interpreted.repairable
    assert interpreted.counts.rejected == 1
    assert [(issue.code, issue.path) for issue in interpreted.issues] == [
        ("unknown_target_candidate", "relations[0].target_candidate_id")
    ]


def test_relation_contract_rejects_type_incompatible_part_of() -> None:
    interpreted = SemanticRelationBoundary.interpret(
        json.dumps(
            {
                "relations": [
                    {
                        "source_candidate_id": "restore",
                        "target_candidate_id": "mariadb",
                        "type": "PART_OF",
                        "supporting_claims": [{"candidate_id": "restore", "claim_ordinal": 0}],
                    }
                ]
            }
        ),
        _one_batch(),
    )

    assert interpreted.relations == ()
    assert [issue.code for issue in interpreted.issues] == ["incompatible_relation_endpoints"]


def test_repaired_all_invalid_relation_array_can_finish_as_audited_degraded_empty() -> None:
    interpreted = SemanticRelationBoundary.interpret(
        json.dumps(
            {
                "relations": [
                    {
                        "source_candidate_id": "restore",
                        "target_candidate_id": "mariadb",
                        "type": "PART_OF",
                        "supporting_claims": [{"candidate_id": "restore", "claim_ordinal": 0}],
                    }
                ]
            }
        ),
        _one_batch(),
        reject_partial=False,
        allow_empty_degraded=True,
    )

    assert interpreted.lifecycle == "completed"
    assert interpreted.quality == "degraded"
    assert interpreted.relations == ()
    assert interpreted.counts.rejected == 1
    assert [issue.code for issue in interpreted.issues] == ["incompatible_relation_endpoints"]


def test_relation_contract_accepts_procedure_uses_entity_with_claim_support() -> None:
    interpreted = SemanticRelationBoundary.interpret(
        json.dumps(
            {
                "relations": [
                    {
                        "source_candidate_id": "restore",
                        "target_candidate_id": "mariadb",
                        "type": "USES",
                        "supporting_claims": [{"candidate_id": "restore", "claim_ordinal": 0}],
                    }
                ]
            }
        ),
        _one_batch(),
    )

    assert interpreted.quality == "full"
    assert [relation.relation_kind for relation in interpreted.relations] == ["USES"]
    assert interpreted.relations[0].assertion_evidence_ids == ("evidence-restore-0",)


def test_relation_batch_with_valid_and_invalid_edges_is_repaired_atomically() -> None:
    interpreted = SemanticRelationBoundary.interpret(
        json.dumps(
            {
                "relations": [
                    {
                        "source_candidate_id": "restore",
                        "target_candidate_id": "mariadb",
                        "type": "USES",
                        "supporting_claims": [{"candidate_id": "restore", "claim_ordinal": 0}],
                    },
                    {
                        "source_candidate_id": "restore",
                        "target_candidate_id": "mariadb",
                        "type": "PART_OF",
                        "supporting_claims": [{"candidate_id": "restore", "claim_ordinal": 0}],
                    },
                ]
            }
        ),
        _one_batch(),
    )

    assert interpreted.lifecycle == "failed"
    assert interpreted.repairable
    assert interpreted.relations == ()
    assert [issue.code for issue in interpreted.issues] == ["incompatible_relation_endpoints"]

    repaired = SemanticRelationBoundary.interpret(
        json.dumps(
            {
                "relations": [
                    {
                        "source_candidate_id": "restore",
                        "target_candidate_id": "mariadb",
                        "type": "USES",
                        "supporting_claims": [{"candidate_id": "restore", "claim_ordinal": 0}],
                    },
                    {
                        "source_candidate_id": "restore",
                        "target_candidate_id": "mariadb",
                        "type": "PART_OF",
                        "supporting_claims": [{"candidate_id": "restore", "claim_ordinal": 0}],
                    },
                ]
            }
        ),
        _one_batch(),
        reject_partial=False,
    )

    assert repaired.lifecycle == "completed"
    assert repaired.quality == "degraded"
    assert [relation.relation_kind for relation in repaired.relations] == ["USES"]
    assert repaired.counts.rejected == 1


def test_semantic_service_retains_audited_valid_subset_without_repairing_it_away(
    tmp_path,
) -> None:
    kb_dir = tmp_path / "knowledge"
    DesktopKnowledgeBaseRuntime().create(kb_dir)

    def respond(_request, _timeout_seconds):
        return json.dumps(
            {
                "relations": [
                    {
                        "source_candidate_id": "restore",
                        "target_candidate_id": "mariadb",
                        "type": "USES",
                        "supporting_claims": [{"candidate_id": "restore", "claim_ordinal": 0}],
                    },
                    {
                        "source_candidate_id": "restore",
                        "target_candidate_id": "mariadb",
                        "type": "PART_OF",
                        "supporting_claims": [{"candidate_id": "restore", "claim_ordinal": 0}],
                    },
                ]
            }
        )

    service = DesktopSemanticGraphService(
        kb_dir,
        model_gateway=DesktopModelGateway(
            respond,
            provider_name="scripted",
            model_name="semantic-v1",
        ),
    )

    output = service._run_batch(
        _one_batch(),
        is_cancelled=None,
        on_model_event=None,
        retry_scope=None,
    )

    assert not output.repaired
    assert output.value.quality == "degraded"
    assert [relation.relation_kind for relation in output.value.relations] == ["USES"]
    assert output.value.counts.rejected == 1


def test_relation_planning_covers_every_claim_instead_of_the_first_twelve() -> None:
    candidates = tuple(
        _candidate(
            f"candidate-{index}",
            "entity",
            f"Named component {index}",
            _claim(
                f"candidate-{index}",
                0,
                f"Named component {index} is related to Named component {(index + 1) % 30}.",
            ),
        )
        for index in range(30)
    )
    document = SemanticGraphDocument("document-many", "large.md", candidates)

    batches = plan_semantic_relation_batches(document, input_budget_tokens=2_400)
    planned_claims = {
        (claim.candidate_id, claim.claim_ordinal) for batch in batches for claim in batch.claims
    }

    assert len(batches) > 1
    assert planned_claims == {(candidate.candidate_id, 0) for candidate in candidates}
    assert all(batch.estimated_input_tokens <= 2_400 for batch in batches)


def test_relation_batch_sends_only_identities_eligible_under_its_support_rule() -> None:
    source_claim = _claim("procedure", 0, "Restore operation invokes Service Alias.")
    document = SemanticGraphDocument(
        "document-focused",
        "focused.md",
        (
            _candidate("procedure", "procedure", "Restore operation", source_claim),
            SemanticGraphCandidate(
                candidate_id="service",
                kind="entity",
                title="Canonical Service",
                aliases=("Service Alias",),
                entity_subtype="service",
                claims=(),
            ),
            _candidate("unrelated", "entity", "Unrelated component"),
        ),
    )

    batch = plan_semantic_relation_batches(document, input_budget_tokens=4_000)[0]
    material = json.loads(batch.source_material)

    assert [item["candidate_id"] for item in material["candidate_registry"]] == [
        "procedure",
        "service",
    ]
    interpreted = SemanticRelationBoundary.interpret(
        json.dumps(
            {
                "relations": [
                    {
                        "source_candidate_id": "procedure",
                        "target_candidate_id": "service",
                        "type": "USES",
                        "supporting_claims": [{"candidate_id": "procedure", "claim_ordinal": 0}],
                    }
                ]
            }
        ),
        batch,
    )
    assert interpreted.lifecycle == "completed"
    assert len(interpreted.relations) == 1


def test_relation_planning_bounds_potential_endpoint_mentions_per_batch() -> None:
    targets = tuple(_candidate(f"target-{index}", "entity", f"Tool-{index}") for index in range(8))
    target_list = ", ".join(target.title for target in targets)
    sources = tuple(
        _candidate(
            f"source-{index}",
            "procedure",
            f"Operation-{index}",
            _claim(f"source-{index}", 0, f"Operation invokes {target_list}."),
        )
        for index in range(10)
    )
    document = SemanticGraphDocument("document-output-bound", "bounded.md", (*sources, *targets))

    batches = plan_semantic_relation_batches(document, input_budget_tokens=100_000)

    assert [len(batch.claims) for batch in batches] == [8, 2]
    assert {claim.key for batch in batches for claim in batch.claims} == {
        claim.key for source in sources for claim in source.claims
    }


def test_relation_planning_rejects_one_claim_above_the_endpoint_mention_limit() -> None:
    targets = tuple(
        _candidate(f"target-{index}", "entity", f"ZX{index:03d}Q") for index in range(65)
    )
    source = _candidate(
        "source",
        "procedure",
        "Operation",
        _claim(
            "source",
            0,
            "Operation invokes " + " ".join(target.title for target in targets),
        ),
    )

    with pytest.raises(SemanticGraphCapacityError, match="endpoint mention"):
        plan_semantic_relation_batches(
            SemanticGraphDocument("document", "bounded.md", (source, *targets)),
            input_budget_tokens=1_000_000,
        )


def test_relation_planning_rejects_invalid_persisted_claim_applicability() -> None:
    claim = SemanticGraphClaim(
        candidate_id="source",
        claim_ordinal=0,
        role="relation",
        text="Operation invokes Database.",
        applicability_json="{broken",
        evidence_ids=("evidence-source",),
    )
    document = SemanticGraphDocument(
        "document",
        "bounded.md",
        (
            SemanticGraphCandidate("source", "procedure", "Operation", (), None, (claim,)),
            _candidate("target", "entity", "Database"),
        ),
    )

    with pytest.raises(SemanticGraphStoredDataError, match="applicability"):
        plan_semantic_relation_batches(document, input_budget_tokens=100_000)


def test_relation_output_limit_recovers_by_splitting_claims_without_dropping_any(
    tmp_path, monkeypatch, caplog
) -> None:
    batch = _one_batch()
    assert len(batch.claims) == 2
    service = DesktopSemanticGraphService(tmp_path, model_gateway=None)
    attempted_claims: list[tuple[tuple[str, int], ...]] = []

    def run(current, **_kwargs):
        attempted_claims.append(tuple(claim.key for claim in current.claims))
        if len(current.claims) > 1:
            truncated = DesktopModelResult(
                "truncated",
                '{"relations":[',
                1,
                observations=DesktopModelOutputObservations(
                    finish_reason="length",
                    final_content_observed=True,
                    final_chunk_count=1,
                    final_character_count=14,
                    output_limit_reached=True,
                ),
            )
            raise DesktopStructuredOutputInvalidError(
                initial_result=truncated,
                final_result=truncated,
                repair_attempted=False,
            )
        interpretation = SemanticRelationBoundary.interpret('{"relations":[]}', current)
        return DesktopValidatedStructuredOutput(
            DesktopModelResult(f"complete-{len(attempted_claims)}", '{"relations":[]}', 1),
            interpretation,
            False,
        )

    monkeypatch.setattr(service, "_run_batch", run)

    with caplog.at_level("INFO", logger="openkb.desktop_semantic_graph_service"):
        outputs = service._run_batch_with_output_limit_recovery(
            batch,
            is_cancelled=None,
            on_model_event=None,
            retry_scope=None,
        )

    assert len(outputs) == 2
    assert attempted_claims == [
        (("restore", 0), ("mariadb", 0)),
        (("restore", 0),),
        (("mariadb", 0),),
    ]
    assert {claim for attempt in attempted_claims[1:] for claim in attempt} == set(
        attempted_claims[0]
    )
    assert [
        record.getMessage()
        for record in caplog.records
        if record.name == "openkb.desktop_semantic_graph_service"
    ] == [
        "semantic_relation_output_limit_split document_id=document-1 "
        "batch_ordinal=0 claim_count=2 child_claim_counts=1,1"
    ]


def test_semantic_relations_materialize_through_identities_and_resolve_to_evidence(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "openkb.desktop_import_runner.start_graph_extraction", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        "openkb.desktop_knowledge_generations._record_corpus_benchmark_in",
        lambda connection, generation_id: connection.execute(
            "UPDATE knowledge_generations SET qualification_report_json = ? "
            "WHERE generation_id = ?",
            ('{"schema_version":"openkb.corpus-benchmark.v3","passed":true}', generation_id),
        ),
    )
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "guide.md"
    source.write_text(
        "# 恢复流程\n\n数据库同步恢复流程使用 MariaDB。\n\n"
        "# 数据库服务\n\nMariaDB 是数据库服务。\n",
        encoding="utf-8",
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    document = DesktopTextImportService(kb_dir).import_text(source).document
    database_path = desktop_state_database_path(kb_dir)
    procedure_evidence, entity_evidence, generation_id = _seed_semantic_identities(
        database_path, document.document_id
    )

    def respond(request, _timeout_seconds):
        if request.operation == "entity_dossier_planning":
            payload = json.loads(request.content)
            return json.dumps(
                {
                    "generation_id": payload["generation_id"],
                    "identity_id": payload["identity_id"],
                    "summary_claim_ids": [claim["claim_id"] for claim in payload["claims"]],
                    "sections": [],
                    "related_identity_ids": [],
                }
            )
        assert request.operation == "knowledge_relation_analysis"
        return json.dumps(
            {
                "relations": [
                    {
                        "source_candidate_id": "candidate-restore",
                        "target_candidate_id": "candidate-mariadb",
                        "type": "USES",
                        "supporting_claims": [
                            {"candidate_id": "candidate-restore", "claim_ordinal": 0}
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        )

    gateway = DesktopModelGateway(respond, provider_name="scripted", model_name="semantic-v1")
    tasks = DesktopKnowledgeGraphExtractionTasks(kb_dir)
    assert tasks.queue(document.document_id, gateway)
    assert tasks.pending_document_ids(gateway) == (document.document_id,)
    assert tasks.run_document(document.document_id, gateway, should_stop=lambda: False)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT status, prompt_digest FROM knowledge_graph_extraction_tasks"
        ).fetchone() == (
            "completed",
            prompt_contract_for("knowledge_relation_analysis").digest,
        )
        assert connection.execute(
            "SELECT relation_kind, provenance, graph_result_id "
            "FROM knowledge_document_relationships"
        ).fetchall() == [
            (
                "USES",
                "semantic_relation_analysis",
                connection.execute(
                    "SELECT result_id FROM knowledge_graph_current WHERE document_id = ?",
                    (document.document_id,),
                ).fetchone()[0],
            )
        ]
        current_generation_id = int(
            connection.execute(
                "SELECT current_generation_id FROM knowledge_generation_state WHERE singleton = 1"
            ).fetchone()[0]
        )
        assert current_generation_id != generation_id
        assert (
            connection.execute(
                "SELECT relation_kind FROM knowledge_generation_relationships "
                "WHERE generation_id = ?",
                (generation_id,),
            ).fetchall()
            == []
        )
        assert connection.execute(
            "SELECT relation_kind, provenance FROM knowledge_generation_relationships "
            "WHERE generation_id = ?",
            (current_generation_id,),
        ).fetchall() == [("USES", "semantic_relation_analysis")]
        assert generation_relationship_issues_in(connection, current_generation_id) == ()
        assert {
            str(row[0])
            for row in connection.execute(
                "SELECT binding_role FROM knowledge_generation_relationship_sources "
                "WHERE generation_id = ?",
                (current_generation_id,),
            )
        } == {"source", "target", "assertion"}
        result = connection.execute(
            "SELECT node_count, edge_count, canonical_schema_version "
            "FROM knowledge_graph_results ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        assert result == (2, 1, "openkb.semantic-identity-graph.v1")
        graph_evidence = local_graph_evidence_ids(
            connection,
            terms=("数据库同步恢复流程",),
            anchor_evidence_ids=(),
        )
    assert procedure_evidence in graph_evidence
    assert entity_evidence in graph_evidence


def test_graph_lookup_can_read_the_snapshot_semantic_generation(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "openkb.desktop_import_runner.start_graph_extraction", lambda *_args, **_kwargs: None
    )
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "snapshot.md"
    source.write_text(
        "# 恢复流程\n\n数据库同步恢复流程使用 MariaDB。\n\n"
        "# 数据库服务\n\nMariaDB 是数据库服务。\n",
        encoding="utf-8",
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    document = DesktopTextImportService(kb_dir).import_text(source).document
    database_path = desktop_state_database_path(kb_dir)
    procedure_evidence, _entity_evidence, pinned_generation_id = _seed_semantic_identities(
        database_path, document.document_id
    )

    with sqlite3.connect(database_path) as connection:
        cursor = connection.execute(
            "INSERT INTO knowledge_generations "
            "(parent_generation_id, created_at, qualification_state, synthesis_schema_version) "
            "VALUES (?, '2026-09-05T00:00:00+00:00', 'qualified', 'test')",
            (pinned_generation_id,),
        )
        connection.execute(
            "UPDATE knowledge_generation_state SET current_generation_id = ? WHERE singleton = 1",
            (int(cursor.lastrowid),),
        )
        evidence = local_graph_evidence_ids(
            connection,
            terms=("数据库同步恢复流程",),
            anchor_evidence_ids=(),
            generation_snapshot=PinnedGraphGenerations(pinned_generation_id, ()),
        )

    assert procedure_evidence in evidence


def test_late_semantic_relation_result_cannot_publish_after_candidate_reanalysis(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "openkb.desktop_import_runner.start_graph_extraction", lambda *_args, **_kwargs: None
    )
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "guide.md"
    source.write_text(
        "# 恢复流程\n\n数据库同步恢复流程使用 MariaDB。\n\n"
        "# 数据库服务\n\nMariaDB 是数据库服务。\n",
        encoding="utf-8",
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    document = DesktopTextImportService(kb_dir).import_text(source).document
    database_path = desktop_state_database_path(kb_dir)
    _seed_semantic_identities(database_path, document.document_id)
    with sqlite3.connect(database_path) as connection:
        claimed_generation_id = str(
            connection.execute(
                "SELECT current_candidate_generation_id "
                "FROM knowledge_candidate_registry_state WHERE document_id = ?",
                (document.document_id,),
            ).fetchone()[0]
        )

    def respond(_request, _timeout_seconds):
        with sqlite3.connect(database_path) as connection:
            publish_candidate_registry_generation_in(
                connection,
                document_id=document.document_id,
                analysis_provenance_json='{"reanalysis":true}',
                now="2026-09-03T00:01:00+00:00",
            )
        return '{"relations":[]}'

    gateway = DesktopModelGateway(respond, provider_name="scripted", model_name="semantic-v1")
    tasks = DesktopKnowledgeGraphExtractionTasks(kb_dir)
    assert tasks.queue(document.document_id, gateway)

    assert not tasks.run_document(document.document_id, gateway, should_stop=lambda: False)
    with sqlite3.connect(database_path) as connection:
        current_generation_id = str(
            connection.execute(
                "SELECT current_candidate_generation_id "
                "FROM knowledge_candidate_registry_state WHERE document_id = ?",
                (document.document_id,),
            ).fetchone()[0]
        )
        published = connection.execute(
            "SELECT candidate_generation_id FROM knowledge_graph_results WHERE document_id = ?",
            (document.document_id,),
        ).fetchall()
    assert current_generation_id != claimed_generation_id
    assert published == []


def test_generation_materialization_skips_a_relation_with_invalid_applicability(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "openkb.desktop_import_runner.start_graph_extraction", lambda *_args, **_kwargs: None
    )
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "guide.md"
    source.write_text(
        "# 恢复流程\n\n数据库同步恢复流程使用 MariaDB。\n\n"
        "# 数据库服务\n\nMariaDB 是数据库服务。\n",
        encoding="utf-8",
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    document = DesktopTextImportService(kb_dir).import_text(source).document
    database_path = desktop_state_database_path(kb_dir)
    _procedure_evidence, _entity_evidence, generation_id = _seed_semantic_identities(
        database_path, document.document_id
    )

    with sqlite3.connect(database_path) as connection:
        candidate_generation_id = str(
            connection.execute(
                """
                SELECT current_candidate_generation_id
                FROM knowledge_candidate_registry_state WHERE document_id = ?
                """,
                (document.document_id,),
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO knowledge_document_relationships (
                document_id, source_candidate_id, target_candidate_id,
                relation_kind, applicability_json, provenance,
                candidate_generation_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document.document_id,
                "candidate-restore",
                "candidate-mariadb",
                "USES",
                "{broken",
                "semantic_relation_analysis",
                candidate_generation_id,
            ),
        )
        connection.execute(
            "INSERT INTO knowledge_document_relationship_claims VALUES (?, ?, ?, ?, ?, ?)",
            (
                document.document_id,
                "candidate-restore",
                "candidate-mariadb",
                "USES",
                "candidate-restore",
                0,
            ),
        )

        rebuild_generation_relationships_in(connection, generation_id)

        assert (
            connection.execute(
                "SELECT relation_kind FROM knowledge_generation_relationships "
                "WHERE generation_id = ?",
                (generation_id,),
            ).fetchall()
            == []
        )


def _seed_semantic_identities(database_path, document_id: str) -> tuple[str, str, int]:
    now = "2026-09-03T00:00:00+00:00"
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        procedure_evidence = str(
            connection.execute(
                "SELECT evidence_id FROM evidence_refs WHERE text LIKE '%恢复流程使用%'"
            ).fetchone()[0]
        )
        entity_evidence = str(
            connection.execute(
                "SELECT evidence_id FROM evidence_refs WHERE text LIKE 'MariaDB 是%'"
            ).fetchone()[0]
        )
        connection.executemany(
            """
            INSERT INTO knowledge_document_candidates (
                candidate_id, document_id, kind, title, normalized_title,
                entity_subtype, aliases_json, tags_json, admission_state,
                admission_reason, analysis_provenance_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, '[]', '[]', 'admitted', 'qualified', '{}', ?)
            """,
            (
                (
                    "candidate-restore",
                    document_id,
                    "procedure",
                    "数据库同步恢复流程",
                    "数据库同步恢复流程",
                    None,
                    now,
                ),
                (
                    "candidate-mariadb",
                    document_id,
                    "entity",
                    "MariaDB",
                    "mariadb",
                    "service",
                    now,
                ),
            ),
        )
        connection.executemany(
            "INSERT INTO knowledge_document_candidate_claims VALUES (?, 0, ?, ?, '{}')",
            (
                ("candidate-restore", "relation", "数据库同步恢复流程使用 MariaDB。"),
                ("candidate-mariadb", "definition", "MariaDB 是数据库服务。"),
            ),
        )
        connection.executemany(
            "INSERT INTO knowledge_document_candidate_claim_sources VALUES (?, 0, ?)",
            (
                ("candidate-restore", procedure_evidence),
                ("candidate-mariadb", entity_evidence),
            ),
        )
        publish_candidate_registry_generation_in(
            connection,
            document_id=document_id,
            analysis_provenance_json="{}",
            now=now,
        )
        connection.executemany(
            "INSERT INTO knowledge_identities VALUES (?, ?, ?, ?, 'active', ?, ?)",
            (
                (
                    "identity-restore",
                    "procedure",
                    "数据库同步恢复流程",
                    "数据库同步恢复流程",
                    now,
                    now,
                ),
                ("identity-mariadb", "entity", "MariaDB", "mariadb", now, now),
            ),
        )
        connection.executemany(
            "INSERT INTO knowledge_identity_candidates VALUES (?, ?, 'exact_title', ?)",
            (
                ("identity-restore", "candidate-restore", now),
                ("identity-mariadb", "candidate-mariadb", now),
            ),
        )
        cursor = connection.execute(
            "INSERT INTO knowledge_generations "
            "(parent_generation_id, created_at, qualification_state, synthesis_schema_version) "
            "VALUES (1, ?, 'qualified', 'test')",
            (now,),
        )
        generation_id = int(cursor.lastrowid)
        items = (
            (
                "identity-restore",
                "procedure",
                "数据库同步恢复流程",
                "数据库同步恢复流程",
                "数据库同步恢复流程使用 MariaDB。",
                None,
                "identity-restore",
            ),
            (
                "identity-mariadb",
                "entity",
                "MariaDB",
                "mariadb",
                "MariaDB 是数据库服务。",
                "service",
                "identity-mariadb",
            ),
        )
        connection.executemany(
            """
            INSERT INTO knowledge_generation_items (
                generation_id, item_key, kind, title, normalized_title,
                content_markdown, content_sha256, source_document_id, created_at,
                provenance_state, entity_subtype, analysis_provenance_json,
                aliases_json, tags_json, identity_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'source_backed', ?, '{}', '[]', '[]', ?)
            """,
            (
                (
                    generation_id,
                    item_key,
                    kind,
                    title,
                    normalized_title,
                    content,
                    knowledge_content_sha256(content),
                    document_id,
                    now,
                    subtype,
                    identity_id,
                )
                for item_key, kind, title, normalized_title, content, subtype, identity_id in items
            ),
        )
        connection.executemany(
            "INSERT INTO knowledge_generation_item_sources VALUES (?, ?, ?, ?, ?)",
            (
                (
                    generation_id,
                    "identity-restore",
                    "source-restore",
                    procedure_evidence,
                    "数据库同步恢复流程使用 MariaDB。",
                ),
                (
                    generation_id,
                    "identity-mariadb",
                    "source-mariadb",
                    entity_evidence,
                    "MariaDB 是数据库服务。",
                ),
            ),
        )
        connection.execute(
            "INSERT INTO knowledge_generation_documents VALUES (?, ?)",
            (generation_id, document_id),
        )
        connection.execute(
            "UPDATE knowledge_generation_state SET current_generation_id = ? WHERE singleton = 1",
            (generation_id,),
        )
    return procedure_evidence, entity_evidence, generation_id
