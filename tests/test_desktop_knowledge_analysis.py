"""Structured Knowledge Analysis behavior at the Desktop import boundary."""

from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path

import pytest

from openkb.desktop_engine import DesktopEngineServer, DesktopRequest
from openkb.desktop_import import DesktopImportError, DesktopTextImportService
from openkb.desktop_knowledge_analysis import (
    KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
    parse_knowledge_analysis,
)
from openkb.desktop_knowledge_analysis_migrations import (
    KNOWLEDGE_ANALYSIS_METADATA_BACKFILL_STATEMENTS,
    register_knowledge_analysis_migration_functions,
)
from openkb.desktop_knowledge_export import DesktopKnowledgeExportService
from openkb.desktop_knowledge_reconciliation import DesktopKnowledgeReconciliationService
from openkb.desktop_knowledge_reconciliation_resolution import (
    DesktopKnowledgeReconciliationResolutionService,
)
from openkb.desktop_model_gateway import DesktopModelGateway
from openkb.desktop_retrieval import DesktopEvidenceRetriever
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime


def _analysis_response(evidence_ids: tuple[str, ...]) -> str:
    return json.dumps(
        {
            "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
            "analysis_scope": "document",
            "document_description": "A guide to local evidence routing.",
            "concepts": [
                {
                    "title": "Evidence routing",
                    "aliases": ["source routing"],
                    "tags": ["retrieval"],
                    "claims": [
                        {
                            "text": "OpenKB routes answers through original evidence.",
                            "source_evidence_ids": [evidence_ids[-1]],
                        }
                    ],
                }
            ],
            "entities": [
                {
                    "title": "OpenKB",
                    "subtype": "Product",
                    "aliases": [],
                    "tags": ["desktop"],
                    "claims": [
                        {
                            "text": "OpenKB is a local knowledge workbench.",
                            "source_evidence_ids": [evidence_ids[0]],
                        }
                    ],
                }
            ],
        },
        ensure_ascii=False,
    )


def _evidence_ids_from_request(content: str) -> tuple[str, ...]:
    payload = json.loads(content)
    return tuple(str(item["evidence_id"]) for item in payload["evidence"])


def test_duplicate_claim_merges_sources_without_treating_order_as_identity() -> None:
    payload = json.loads(_analysis_response(("evidence-one", "evidence-two")))
    payload["concepts"][0]["claims"] = [
        {
            "text": "OpenKB routes answers through original evidence.",
            "source_evidence_ids": ["evidence-one", "evidence-two"],
        },
        {
            "text": "OpenKB routes answers through original evidence.",
            "source_evidence_ids": ["evidence-two", "evidence-one"],
        },
    ]

    analysis = parse_knowledge_analysis(json.dumps(payload))

    assert len(analysis.concepts[0].claims) == 1
    claim = analysis.concepts[0].claims[0]
    assert claim.text == "OpenKB routes answers through original evidence."
    assert claim.source_evidence_ids == ("evidence-one", "evidence-two")


def test_structured_analysis_publishes_source_backed_unverified_knowledge(
    tmp_path: Path,
) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "guide.md"
    source.write_text(
        "# OpenKB\n\nOpenKB is a local knowledge workbench.\n\n"
        "# Retrieval\n\nAnswers are routed through original evidence.",
        encoding="utf-8",
    )
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(kb_dir)
    operations: list[str] = []

    def analyze(request, _timeout_seconds):
        operations.append(request.operation)
        return _analysis_response(_evidence_ids_from_request(request.content))

    server = DesktopEngineServer(
        io.BytesIO(),
        io.BytesIO(),
        workspace=workspace,
        model_gateway_factory=lambda _kb_dir, _override: DesktopModelGateway(
            analyze, provider_name="scripted", model_name="analysis-v1"
        ),
    )
    server._handshake_complete = True
    result = server._dispatch(
        DesktopRequest(
            request_id="knowledge-analysis",
            method="workbench.import_text_document",
            params={"source_path": str(source)},
        ),
        cancel_event=None,
    )

    assert result["document"]["availability"] == "available"
    assert operations.count("knowledge_analysis") == 1
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        checkpoint_json = connection.execute(
            """
            SELECT runtime.checkpoint_json
            FROM stage_run_runtime AS runtime
            JOIN stage_runs AS stages ON stages.stage_run_id = runtime.stage_run_id
            WHERE runtime.job_id = ? AND stages.stage = 'model_analysis'
            """,
            (result["job"]["job_id"],),
        ).fetchone()[0]
        checkpoint = json.loads(str(checkpoint_json))
        assert checkpoint["schema_version"] == KNOWLEDGE_ANALYSIS_SCHEMA_VERSION
        assert checkpoint["provider"] == "scripted"
        assert checkpoint["model"] == "analysis-v1"
        assert checkpoint["attempt_metadata"]["attempt_count"] == 1
        assert len(checkpoint["prompt_digest"]) == 64
        assert len(checkpoint["response_sha256"]) == 64
        assert "raw_response" not in checkpoint
        assert checkpoint["normalized_result"]["concepts"][0]["aliases"] == ["source routing"]

        items = connection.execute(
            """
            SELECT kind, title, provenance_state, entity_subtype, aliases_json, tags_json
            FROM knowledge_generation_state AS state
            JOIN knowledge_generation_items AS items
                ON items.generation_id = state.current_generation_id
            ORDER BY kind, title
            """
        ).fetchall()
        assert items == [
            (
                "concept",
                "Evidence routing",
                "source_backed",
                None,
                '["source routing"]',
                '["retrieval"]',
            ),
            ("entity", "OpenKB", "source_backed", "Product", "[]", '["desktop"]'),
        ]
        assert connection.execute(
            """
            SELECT COUNT(*) FROM knowledge_generation_state AS state
            JOIN knowledge_generation_item_sources AS sources
                ON sources.generation_id = state.current_generation_id
            """
        ).fetchone() == (2,)

    routed = DesktopEvidenceRetriever(kb_dir).retrieve("source routing")
    assert any("knowledge_source" in reference.channels for reference in routed.evidence)
    generated = tuple((kb_dir / "knowledge-pages" / "generated").rglob("*.md"))
    projected = "\n".join(path.read_text(encoding="utf-8") for path in generated)
    assert "openkb-knowledge-analysis/openkb.knowledge-analysis.v1" in projected
    assert "provider: scripted" in projected
    assert "model: analysis-v1" in projected
    assert "prompt_digest:" in projected
    assert "engine_version:" in projected
    assert "canonical_evidence_id" in projected
    assert "aliases:" in projected
    assert "tags:" in projected
    assert "Aliases:" not in projected
    assert "Tags:" not in projected
    export_parent = tmp_path / "exports"
    export_parent.mkdir()
    bundle = DesktopKnowledgeExportService(kb_dir).export(export_parent, mode="self_contained")
    assert bundle.raw_asset_count == 1


def test_reused_evidence_keeps_every_claim_source_backed(tmp_path: Path) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "shared-source.txt"
    source.write_text("One source supports two related facts.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)

    def analyze(request, _timeout_seconds):
        evidence_id = _evidence_ids_from_request(request.content)[0]
        return json.dumps(
            {
                "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
                "analysis_scope": "document",
                "document_description": "Two claims share one source.",
                "concepts": [
                    {
                        "title": "Shared source",
                        "aliases": [],
                        "tags": [],
                        "claims": [
                            {
                                "text": "The first fact is source backed.",
                                "source_evidence_ids": [evidence_id],
                            },
                            {
                                "text": "The second fact is also source backed.",
                                "source_evidence_ids": [evidence_id],
                            },
                            {
                                "text": "The first fact is source backed.",
                                "source_evidence_ids": [evidence_id],
                            },
                        ],
                    }
                ],
                "entities": [],
            }
        )

    DesktopTextImportService(
        kb_dir, model_gateway=DesktopModelGateway(analyze)
    ).import_text(source)

    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        content = str(
            connection.execute(
                """
                SELECT items.content_markdown
                FROM knowledge_generation_state AS state
                JOIN knowledge_generation_items AS items
                    ON items.generation_id = state.current_generation_id
                WHERE items.normalized_title = 'shared source'
                """
            ).fetchone()[0]
        )
        sources = connection.execute(
            """
            SELECT sources.source_id, sources.claim_text
            FROM knowledge_generation_state AS state
            JOIN knowledge_generation_items AS items
                ON items.generation_id = state.current_generation_id
            JOIN knowledge_generation_item_sources AS sources
                ON sources.generation_id = items.generation_id
                AND sources.item_key = items.item_key
            WHERE items.normalized_title = 'shared source'
            ORDER BY sources.claim_text
            """
        ).fetchall()
    assert len(sources) == 2
    assert sources[0][0] == sources[1][0]
    assert all(f"[^{source_id}]" in content for source_id, _claim in sources)
    assert content.count(f"[^{sources[0][0]}]") == 2
    assert content.count("The first fact is source backed.") == 1


def test_analysis_uses_canonical_d2_evidence_identity(tmp_path: Path) -> None:
    kb_dir = tmp_path / "knowledge"
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("# Shared\n\nShared factual evidence.", encoding="utf-8")
    second.write_text(
        "# Shared\n\nShared factual evidence.\n\n# Extra\n\nA distinct supporting detail.",
        encoding="utf-8",
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    DesktopTextImportService(kb_dir).import_text(first)

    requested_shared_ids: list[str] = []

    def analyze(request, _timeout_seconds):
        payload = json.loads(request.content)
        shared_id = next(
            str(item["evidence_id"])
            for item in payload["evidence"]
            if item["text"] == "Shared factual evidence."
        )
        requested_shared_ids.append(shared_id)
        return json.dumps(
            {
                "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
                "analysis_scope": "document",
                "document_description": "A D2 source identity check.",
                "concepts": [
                    {
                        "title": "Canonical D2 source",
                        "aliases": [],
                        "tags": [],
                        "claims": [
                            {
                                "text": "The shared evidence remains canonical.",
                                "source_evidence_ids": [shared_id],
                            }
                        ],
                    }
                ],
                "entities": [],
            }
        )

    imported = DesktopTextImportService(
        kb_dir, model_gateway=DesktopModelGateway(analyze)
    ).import_text(second)

    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        row = connection.execute(
            """
            SELECT sources.evidence_id, evidence_refs.evidence_id
            FROM knowledge_generation_state AS state
            JOIN knowledge_generation_items AS items
                ON items.generation_id = state.current_generation_id
            JOIN knowledge_generation_item_sources AS sources
                ON sources.generation_id = items.generation_id
                AND sources.item_key = items.item_key
            JOIN evidence_refs ON evidence_refs.evidence_id = sources.evidence_id
            WHERE items.normalized_title = 'canonical d2 source'
            """
        ).fetchone()
        occurrence = connection.execute(
            """
            SELECT evidence_id FROM evidence_occurrences
            WHERE document_id = ? AND ordinal = 1
            """,
            (imported.document.document_id,),
        ).fetchone()
        deduplication = connection.execute(
            "SELECT level FROM import_deduplications WHERE job_id = ?",
            (imported.job.job_id,),
        ).fetchone()
    assert row is not None
    assert row[0] == row[1] == occurrence[0]
    assert row[0] != requested_shared_ids[0]
    assert deduplication == ("D2",)


@pytest.mark.parametrize(("suffix", "level"), (("", "D0"), ("   ", "D1")))
def test_d0_d1_reuse_structured_analysis_without_deterministic_pollution(
    tmp_path: Path, suffix: str, level: str
) -> None:
    kb_dir = tmp_path / "knowledge"
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    content = "# Source\n\nA structured fact."
    first.write_text(content, encoding="utf-8")
    second.write_text(f"{content}{suffix}", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)

    def analyze(request, _timeout_seconds):
        evidence_id = _evidence_ids_from_request(request.content)[-1]
        return json.dumps(
            {
                "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
                "analysis_scope": "document",
                "document_description": "Reusable structured knowledge.",
                "concepts": [
                    {
                        "title": "Structured title",
                        "aliases": [],
                        "tags": [],
                        "claims": [
                            {
                                "text": "A structured fact.",
                                "source_evidence_ids": [evidence_id],
                            }
                        ],
                    }
                ],
                "entities": [],
            }
        )

    DesktopTextImportService(
        kb_dir, model_gateway=DesktopModelGateway(analyze)
    ).import_text(first)
    duplicate = DesktopTextImportService(kb_dir).import_text(second)

    assert duplicate.job.deduplication is not None
    assert duplicate.job.deduplication.level == level
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute(
            """
            SELECT normalized_title, provenance_state
            FROM knowledge_generation_state AS state
            JOIN knowledge_generation_items AS items
                ON items.generation_id = state.current_generation_id
            ORDER BY normalized_title
            """
        ).fetchall() == [("structured title", "source_backed")]


def test_deterministic_replacement_clears_analysis_only_metadata(tmp_path: Path) -> None:
    kb_dir = tmp_path / "knowledge"
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("# Concept: Metadata\n\nField: one", encoding="utf-8")
    second.write_text(
        "# Concept: Metadata\n\nField: one\n\nOther: two", encoding="utf-8"
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)

    def analyze(request, _timeout_seconds):
        evidence_id = _evidence_ids_from_request(request.content)[-1]
        return json.dumps(
            {
                "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
                "analysis_scope": "document",
                "document_description": "Metadata authority test.",
                "concepts": [
                    {
                        "title": "Metadata",
                        "aliases": ["model alias"],
                        "tags": ["model-tag"],
                        "claims": [
                            {"text": "Field: one", "source_evidence_ids": [evidence_id]}
                        ],
                    }
                ],
                "entities": [],
            }
        )

    DesktopTextImportService(
        kb_dir,
        model_gateway=DesktopModelGateway(
            analyze, provider_name="provider-a", model_name="model-a"
        ),
    ).import_text(first)
    DesktopTextImportService(kb_dir).import_text(second)

    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute(
            """
            SELECT provenance_state, aliases_json, tags_json, analysis_provenance_json
            FROM knowledge_generation_state AS state
            JOIN knowledge_generation_items AS items
                ON items.generation_id = state.current_generation_id
            WHERE items.normalized_title = 'metadata'
            """
        ).fetchone() == ("legacy_unmapped", "[]", "[]", None)


def test_v28_backfills_pending_analysis_candidate_metadata_before_publication(
    tmp_path: Path,
) -> None:
    kb_dir = tmp_path / "knowledge"
    baseline = tmp_path / "baseline.md"
    incoming = tmp_path / "incoming.md"
    baseline.write_text("# Concept: Migrated\n\nMode: local", encoding="utf-8")
    incoming.write_text("Incoming source for the migrated candidate.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    DesktopTextImportService(kb_dir).import_text(baseline)

    def analyze(request, _timeout_seconds):
        evidence_id = _evidence_ids_from_request(request.content)[0]
        return json.dumps(
            {
                "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
                "analysis_scope": "document",
                "document_description": "Legacy pending candidate.",
                "concepts": [
                    {
                        "title": "Migrated",
                        "aliases": ["legacy alias"],
                        "tags": ["legacy-tag"],
                        "claims": [
                            {
                                "text": "Mode: global",
                                "source_evidence_ids": [evidence_id],
                            }
                        ],
                    }
                ],
                "entities": [],
            }
        )

    DesktopTextImportService(
        kb_dir,
        model_gateway=DesktopModelGateway(
            analyze, provider_name="legacy-provider", model_name="legacy-model"
        ),
    ).import_text(incoming)
    conflict = DesktopKnowledgeReconciliationService(kb_dir).list_conflicts()[0]
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        connection.execute(
            """
            UPDATE knowledge_reconciliation_candidates
            SET analysis_provenance_json = NULL, aliases_json = '[]', tags_json = '[]',
                content_markdown = 'Aliases: legacy alias\n\nTags: legacy-tag\n\n'
                    || content_markdown,
                content_sha256 = 'legacy-v26-hash'
            WHERE candidate_id = ?
            """,
            (conflict.candidate_id,),
        )
        register_knowledge_analysis_migration_functions(connection)
        for statement in KNOWLEDGE_ANALYSIS_METADATA_BACKFILL_STATEMENTS:
            connection.execute(statement)
        metadata = connection.execute(
            """
            SELECT analysis_provenance_json, aliases_json, tags_json, content_markdown
            FROM knowledge_reconciliation_candidates WHERE candidate_id = ?
            """,
            (conflict.candidate_id,),
        ).fetchone()
        connection.commit()
    assert metadata is not None
    assert json.loads(str(metadata[0]))["provider"] == "legacy-provider"
    assert metadata[1:3] == ('["legacy alias"]', '["legacy-tag"]')
    assert str(metadata[3]).startswith("Mode: global[^src-")

    resolution = DesktopKnowledgeReconciliationResolutionService(kb_dir)
    resolution.stage_decisions((conflict.candidate_id,), "publish_incoming")
    resolution.commit_staged_decisions()

    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute(
            """
            SELECT aliases_json, tags_json, analysis_provenance_json
            FROM knowledge_reconciliation_candidates WHERE candidate_id = ?
            """,
            (conflict.candidate_id,),
        ).fetchone() == ("[]", "[]", None)
        connection.execute(
            """
            UPDATE knowledge_generation_items
            SET analysis_provenance_json = NULL, aliases_json = '[]', tags_json = '[]',
                content_markdown = 'Aliases: legacy alias\n\nTags: legacy-tag\n\n'
                    || content_markdown,
                content_sha256 = 'legacy-v26-hash'
            WHERE normalized_title = 'migrated'
            """
        )
        register_knowledge_analysis_migration_functions(connection)
        for statement in KNOWLEDGE_ANALYSIS_METADATA_BACKFILL_STATEMENTS:
            connection.execute(statement)
        migrated = connection.execute(
            """
            SELECT analysis_provenance_json, aliases_json, tags_json,
                content_markdown, content_sha256
            FROM knowledge_generation_items WHERE normalized_title = 'migrated'
            ORDER BY generation_id DESC LIMIT 1
            """
        ).fetchone()
        connection.commit()
    assert migrated is not None
    assert json.loads(str(migrated[0]))["provider"] == "legacy-provider"
    assert migrated[1:3] == ('["legacy alias"]', '["legacy-tag"]')
    assert not str(migrated[3]).startswith("Aliases:")
    assert migrated[4] != "legacy-v26-hash"

    projected = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (kb_dir / "knowledge-pages" / "generated").rglob("*.md")
    )
    assert "openkb-knowledge-analysis/openkb.knowledge-analysis.v1" in projected
    assert "provider: legacy-provider" in projected
    assert "legacy alias" in projected
    assert "legacy-tag" in projected


def test_duplicate_claim_merges_independent_available_sources(tmp_path: Path) -> None:
    kb_dir = tmp_path / "knowledge"
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("The first manual documents local routing.", encoding="utf-8")
    second.write_text("A separate handbook confirms local routing.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)

    def analyze(request, _timeout_seconds):
        evidence_id = _evidence_ids_from_request(request.content)[0]
        return json.dumps(
            {
                "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
                "analysis_scope": "document",
                "document_description": "Independent support.",
                "concepts": [
                    {
                        "title": "Independent support",
                        "aliases": [],
                        "tags": [],
                        "claims": [
                            {
                                "text": "OpenKB uses local routing.",
                                "source_evidence_ids": [evidence_id],
                            }
                        ],
                    }
                ],
                "entities": [],
            }
        )

    importer = DesktopTextImportService(
        kb_dir, model_gateway=DesktopModelGateway(analyze)
    )
    first_result = importer.import_text(first)
    second_result = importer.import_text(second)
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute(
            """
            SELECT COUNT(*) FROM knowledge_generation_state AS state
            JOIN knowledge_generation_items AS items
                ON items.generation_id = state.current_generation_id
            JOIN knowledge_generation_item_sources AS sources
                ON sources.generation_id = items.generation_id
                AND sources.item_key = items.item_key
            WHERE items.normalized_title = 'independent support'
            """
        ).fetchone() == (2,)
        connection.execute(
            "UPDATE source_documents SET availability = 'failed' WHERE document_id = ?",
            (first_result.document.document_id,),
        )
        connection.commit()

    routed = DesktopEvidenceRetriever(kb_dir).retrieve("local routing")
    assert any(
        reference.document_id == second_result.document.document_id
        and "knowledge_source" in reference.channels
        for reference in routed.evidence
    )


def test_analysis_application_failure_rolls_back_publication_for_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "recoverable.txt"
    source.write_text("Recover this structured analysis exactly once.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    calls = 0

    def analyze(request, _timeout_seconds):
        nonlocal calls
        calls += 1
        return _analysis_response(_evidence_ids_from_request(request.content))

    importer = DesktopTextImportService(
        kb_dir, model_gateway=DesktopModelGateway(analyze)
    )
    original = importer._knowledge_reconciliation.record_analysis_changes_in
    monkeypatch.setattr(
        importer._knowledge_reconciliation,
        "record_analysis_changes_in",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SystemExit("simulated crash")),
    )
    with pytest.raises(SystemExit):
        importer.import_text(source)
    assert calls == 1
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_documents").fetchone() == (0,)
    DesktopKnowledgeBaseRuntime().open(kb_dir)
    (job_id,) = importer.recoverable_job_ids()

    DesktopTextImportService(kb_dir).import_text(source)

    monkeypatch.setattr(
        importer._knowledge_reconciliation, "record_analysis_changes_in", original
    )
    recovered = importer.resume_text(job_id)
    assert recovered.document.availability == "available"
    assert calls == 1
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM knowledge_generations").fetchone() == (3,)
        assert connection.execute(
            """
            SELECT provenance_state FROM knowledge_generation_state AS state
            JOIN knowledge_generation_items AS items
                ON items.generation_id = state.current_generation_id
            WHERE items.normalized_title = 'evidence routing'
            """
        ).fetchone() == ("source_backed",)


def test_valid_empty_analysis_still_publishes_the_document(tmp_path: Path) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "empty-analysis.txt"
    source.write_text("A document with no durable knowledge candidates.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    response = json.dumps(
        {
            "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
            "analysis_scope": "document",
            "document_description": "No durable candidates.",
            "concepts": [],
            "entities": [],
        }
    )

    imported = DesktopTextImportService(
        kb_dir, model_gateway=DesktopModelGateway(lambda *_args: response)
    ).import_text(source)

    assert imported.document.availability == "available"
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM knowledge_generations").fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge_reconciliation_candidates"
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    "response",
    (
        "not json",
        json.dumps(
            {
                "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
                "analysis_scope": "document",
                "document_description": "Invalid candidate.",
                "concepts": [{"title": "Missing claims"}],
                "entities": [],
            }
        ),
    ),
)
def test_invalid_analysis_schema_is_quarantined_without_retry(
    tmp_path: Path, response: str
) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "invalid.txt"
    source.write_text("Evidence that must not publish after invalid analysis.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    calls = 0

    def analyze(*_args):
        nonlocal calls
        calls += 1
        return response

    with pytest.raises(DesktopImportError) as captured:
        DesktopTextImportService(kb_dir, model_gateway=DesktopModelGateway(analyze)).import_text(
            source
        )

    assert captured.value.code == "model_response_invalid"
    assert calls == 1
    task = DesktopTextImportService(kb_dir).list_import_jobs()["jobs"][0]
    assert task["job"]["status"] == "quarantined"
    assert task["quarantine"]["stage"] == "model_analysis"
    assert task["quarantine"]["attempt_count"] == 1
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_documents").fetchone() == (0,)
