"""Resumable natural-section Knowledge Analysis Batch behavior."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from openkb.desktop_import import DesktopImportError, DesktopTextImportService
from openkb.desktop_import_artifacts import DocumentIRBlock
from openkb.desktop_import_types import DesktopRecoveryOverride
from openkb.desktop_knowledge_analysis import (
    KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
    parse_knowledge_analysis,
)
from openkb.desktop_knowledge_analysis_batches import (
    knowledge_analysis_merge_prompt,
    plan_knowledge_analysis_batches,
)
from openkb.desktop_model_gateway import DesktopModelGateway, DesktopModelTransportError
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def _long_source(path: Path) -> None:
    path.write_text(
        "\n\n".join(
            f"# Section {ordinal}\n\n" + (f"Durable fact for natural section {ordinal}. " * 150)
            for ordinal in range(7)
        ),
        encoding="utf-8",
    )


def _analysis(scope: str) -> str:
    return json.dumps(
        {
            "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
            "analysis_scope": scope,
            "document_description": f"Validated {scope} result.",
            "concepts": [],
            "entities": [],
        }
    )


def test_batch_planner_preserves_natural_sections_until_one_section_exceeds_bound() -> None:
    evidence = tuple(
        (
            f"evidence-{ordinal}",
            DocumentIRBlock(
                block_id=f"block-{ordinal}",
                ordinal=ordinal,
                kind="paragraph",
                text=f"Fact {ordinal}",
                heading_path=(f"Section {ordinal // 2}",),
                line_start=ordinal + 1,
                line_end=ordinal + 1,
            ),
        )
        for ordinal in range(6)
    )

    batches = plan_knowledge_analysis_batches(evidence, max_evidence=3)

    assert [[item[0] for item in batch] for batch in batches] == [
        ["evidence-0", "evidence-1"],
        ["evidence-2", "evidence-3"],
        ["evidence-4", "evidence-5"],
    ]


def test_failed_batch_recovery_reuses_completed_batch_and_runs_one_merge(tmp_path: Path) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "long.md"
    _long_source(source)
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    operations: list[str] = []
    failed_once = False

    def transport(request, _timeout_seconds):
        nonlocal failed_once
        payload = json.loads(request.content)
        if request.operation == "knowledge_analysis_batch":
            ordinal = int(payload["batch_ordinal"])
            operations.append(f"batch:{ordinal}")
            if ordinal == 1 and not failed_once:
                failed_once = True
                raise DesktopModelTransportError("input")
            return _analysis("batch")
        if request.operation == "knowledge_analysis_merge":
            operations.append("merge")
            return _analysis("document")
        raise AssertionError(request.operation)

    gateway = DesktopModelGateway(transport, provider_name="scripted", model_name="batch-v1")
    importer = DesktopTextImportService(kb_dir, model_gateway=gateway)
    with pytest.raises(DesktopImportError) as captured:
        importer.import_text(source)
    assert captured.value.code == "document_quarantined"
    failed = importer.list_import_jobs()["jobs"][0]
    assert failed["knowledge_analysis"] == {
        "total": 2,
        "completed": 1,
        "active": 0,
        "failed": 1,
        "current_batch": 2,
        "phase": "batches",
    }
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        checkpoint = json.loads(
            connection.execute(
                """
                SELECT checkpoint_json FROM knowledge_analysis_batches
                WHERE job_id = ? AND batch_ordinal = 0
                """,
                (failed["job"]["job_id"],),
            ).fetchone()[0]
        )
        assert checkpoint["analysis_scope"] == "batch"
        assert checkpoint["provider"] == "scripted"
        assert checkpoint["model"] == "batch-v1"
        assert len(checkpoint["prompt_digest"]) == 64
        assert len(checkpoint["response_sha256"]) == 64
        assert "raw_response" not in checkpoint

    recovered = DesktopTextImportService(kb_dir, model_gateway=gateway).recover_text(
        failed["job"]["job_id"], DesktopRecoveryOverride()
    )

    assert recovered.document.availability == "available"
    assert operations == ["batch:0", "batch:1", "batch:1", "merge"]
    progress = recovered.knowledge_analysis
    assert progress is not None
    assert (progress.total, progress.completed, progress.phase) == (2, 2, "completed")


def test_merge_recovery_does_not_repeat_completed_batches(tmp_path: Path) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "long.md"
    _long_source(source)
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    operations: list[str] = []
    failed_merge = False

    def transport(request, _timeout_seconds):
        nonlocal failed_merge
        if request.operation == "knowledge_analysis_batch":
            ordinal = json.loads(request.content)["batch_ordinal"]
            operations.append(f"batch:{ordinal}")
            return _analysis("batch")
        if request.operation == "knowledge_analysis_merge":
            operations.append("merge")
            if not failed_merge:
                failed_merge = True
                raise DesktopModelTransportError("input")
            return _analysis("document")
        raise AssertionError(request.operation)

    gateway = DesktopModelGateway(transport)
    importer = DesktopTextImportService(kb_dir, model_gateway=gateway)
    with pytest.raises(DesktopImportError):
        importer.import_text(source)
    failed = importer.list_import_jobs()["jobs"][0]
    assert failed["knowledge_analysis"]["phase"] == "merge"

    DesktopTextImportService(kb_dir, model_gateway=gateway).recover_text(
        failed["job"]["job_id"], DesktopRecoveryOverride()
    )

    assert operations == ["batch:0", "batch:1", "merge", "merge"]


def test_each_batch_model_call_uses_only_the_fixed_connect_bound(tmp_path: Path) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "long.md"
    _long_source(source)
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    clock = FakeClock()
    attempts: dict[str, int] = {}
    timeouts: dict[str, list[float]] = {}

    def transport(request, timeout_seconds):
        key = request.operation
        if key == "knowledge_analysis_batch":
            key = f"batch:{json.loads(request.content)['batch_ordinal']}"
        timeouts.setdefault(key, []).append(timeout_seconds)
        attempts[key] = attempts.get(key, 0) + 1
        if key.startswith("batch:") and attempts[key] == 1:
            raise TimeoutError()
        return _analysis("batch" if key.startswith("batch:") else "document")

    imported = DesktopTextImportService(
        kb_dir,
        model_gateway=DesktopModelGateway(
            transport,
            clock=clock,
            sleep=lambda _seconds: None,
        ),
    ).import_text(source)

    assert imported.document.availability == "available"
    assert timeouts == {
        "batch:0": [30.0, 30.0],
        "batch:1": [30.0, 30.0],
        "knowledge_analysis_merge": [30.0],
    }


def test_completed_merge_recovery_keeps_the_persisted_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "long.md"
    _long_source(source)
    DesktopKnowledgeBaseRuntime().create(kb_dir)

    def old_transport(request, _timeout_seconds):
        payload = json.loads(request.content)
        if request.operation == "knowledge_analysis_batch":
            ordinal = int(payload["batch_ordinal"])
            evidence_id = str(payload["evidence"][0]["evidence_id"])
            return json.dumps(
                {
                    "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
                    "analysis_scope": "batch",
                    "document_description": "Old provider batch.",
                    "concepts": [
                        {
                            "title": f"Batch {ordinal}",
                            "aliases": [],
                            "tags": [],
                            "claims": [
                                {
                                    "text": f"Fact from batch {ordinal}.",
                                    "source_evidence_ids": [evidence_id],
                                }
                            ],
                        }
                    ],
                    "entities": [],
                }
            )
        assert "batch_results" not in payload
        return json.dumps({"document_description": "Old provider merge."})

    importer = DesktopTextImportService(
        kb_dir,
        model_gateway=DesktopModelGateway(
            old_transport, provider_name="old-provider", model_name="old-model"
        ),
    )
    complete_merge = importer._knowledge_analysis_batches.complete_merge

    def persist_then_fail(job_id: str, checkpoint: dict[str, object]) -> None:
        complete_merge(job_id, checkpoint)
        raise SystemExit("simulated crash after merge checkpoint")

    monkeypatch.setattr(importer._knowledge_analysis_batches, "complete_merge", persist_then_fail)
    with pytest.raises(SystemExit):
        importer.import_text(source)
    job_id = str(importer.list_import_jobs()["jobs"][0]["job"]["job_id"])
    DesktopKnowledgeBaseRuntime().open(kb_dir)

    recovery_calls: list[str] = []

    def new_transport(request, _timeout_seconds):
        recovery_calls.append(request.operation)
        raise AssertionError("Completed batch and merge checkpoints must be reused.")

    DesktopTextImportService(
        kb_dir,
        model_gateway=DesktopModelGateway(
            new_transport, provider_name="new-provider", model_name="new-model"
        ),
    ).resume_text(job_id)

    assert recovery_calls == []
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        provenance = json.loads(
            connection.execute(
                "SELECT analysis_provenance_json FROM knowledge_generation_items"
            ).fetchone()[0]
        )
    assert (provenance["provider"], provenance["model"]) == (
        "old-provider",
        "old-model",
    )


def test_batch_cannot_bind_evidence_from_another_batch(tmp_path: Path) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "long.md"
    _long_source(source)
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    first_evidence_id = ""
    operations: list[str] = []

    def transport(request, _timeout_seconds):
        nonlocal first_evidence_id
        payload = json.loads(request.content)
        operations.append(request.operation)
        if request.operation == "structured_output_repair":
            return str(payload["invalid_result"])
        if request.operation == "knowledge_analysis_batch":
            ordinal = int(payload["batch_ordinal"])
            if ordinal == 0:
                first_evidence_id = str(payload["evidence"][0]["evidence_id"])
            return json.dumps(
                {
                    "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
                    "analysis_scope": "batch",
                    "document_description": "Scoped batch.",
                    "concepts": [
                        {
                            "title": f"Batch {ordinal}",
                            "aliases": [],
                            "tags": [],
                            "claims": [
                                {
                                    "text": f"Claim {ordinal}.",
                                    "source_evidence_ids": [first_evidence_id],
                                }
                            ],
                        }
                    ],
                    "entities": [],
                }
            )
        raise AssertionError("Invalid batch must stop before merge.")

    with pytest.raises(DesktopImportError) as captured:
        DesktopTextImportService(kb_dir, model_gateway=DesktopModelGateway(transport)).import_text(
            source
        )

    assert captured.value.code == "model_response_invalid"
    assert operations == [
        "knowledge_analysis_batch",
        "knowledge_analysis_batch",
        "structured_output_repair",
    ]
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_documents").fetchone() == (0,)


def test_merge_cannot_invent_a_claim_even_with_valid_document_evidence(tmp_path: Path) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "long.md"
    _long_source(source)
    DesktopKnowledgeBaseRuntime().create(kb_dir)

    def transport(request, _timeout_seconds):
        payload = json.loads(request.content)
        if request.operation == "knowledge_analysis_batch":
            ordinal = int(payload["batch_ordinal"])
            evidence_id = str(payload["evidence"][0]["evidence_id"])
            return json.dumps(
                {
                    "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
                    "analysis_scope": "batch",
                    "document_description": "Scoped batch.",
                    "concepts": [
                        {
                            "title": f"Batch {ordinal}",
                            "aliases": [],
                            "tags": [],
                            "claims": [
                                {
                                    "text": f"Original claim {ordinal}.",
                                    "source_evidence_ids": [evidence_id],
                                }
                            ],
                        }
                    ],
                    "entities": [],
                }
            )
        if request.operation == "structured_output_repair":
            return str(payload["invalid_result"])
        assert "batch_results" not in payload
        return json.dumps(
            {
                "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
                "analysis_scope": "document",
                "document_description": "Invalid merge.",
                "concepts": [
                    {
                        "title": "Invented",
                        "aliases": [],
                        "tags": [],
                        "claims": [
                            {
                                "text": "Invented during merge.",
                                "source_evidence_ids": ["invented-evidence"],
                            }
                        ],
                    }
                ],
                "entities": [],
            }
        )

    with pytest.raises(DesktopImportError) as captured:
        DesktopTextImportService(kb_dir, model_gateway=DesktopModelGateway(transport)).import_text(
            source
        )
    assert captured.value.code == "model_response_invalid"


@pytest.mark.parametrize("drop_claim", [True, False])
def test_merge_cannot_drop_a_validated_claim_or_its_sources(
    tmp_path: Path, drop_claim: bool
) -> None:
    kb_dir = tmp_path / ("drop-claim" if drop_claim else "drop-sources")
    source = tmp_path / ("drop-claim.md" if drop_claim else "drop-sources.md")
    _long_source(source)
    DesktopKnowledgeBaseRuntime().create(kb_dir)

    def transport(request, _timeout_seconds):
        payload = json.loads(request.content)
        if request.operation == "knowledge_analysis_batch":
            ordinal = int(payload["batch_ordinal"])
            evidence_id = str(payload["evidence"][0]["evidence_id"])
            return json.dumps(
                {
                    "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
                    "analysis_scope": "batch",
                    "document_description": "Validated batch.",
                    "concepts": [
                        {
                            "title": f"Batch {ordinal}",
                            "aliases": [],
                            "tags": [],
                            "claims": [
                                {
                                    "text": f"Validated claim {ordinal}.",
                                    "source_evidence_ids": [evidence_id],
                                }
                            ],
                        }
                    ],
                    "entities": [],
                }
            )
        if request.operation == "structured_output_repair":
            return str(payload["invalid_result"])
        concepts = [] if drop_claim else [{"unexpected": "model-owned knowledge"}]
        return json.dumps(
            {
                "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
                "analysis_scope": "document",
                "document_description": "Lossy merge.",
                "concepts": concepts,
                "entities": [],
            }
        )

    importer = DesktopTextImportService(kb_dir, model_gateway=DesktopModelGateway(transport))
    if drop_claim:
        imported = importer.import_text(source)
        assert imported.document.availability == "available"
        with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
            generated_content = [
                str(row[0])
                for row in connection.execute(
                    "SELECT content_markdown FROM knowledge_generation_items"
                ).fetchall()
            ]
            batch_count = connection.execute(
                "SELECT COUNT(*) FROM knowledge_analysis_batches"
            ).fetchone()[0]
        assert batch_count > 0
        assert all(
            any(f"Validated claim {ordinal}." in content for content in generated_content)
            for ordinal in range(batch_count)
        )
    else:
        with pytest.raises(DesktopImportError) as captured:
            importer.import_text(source)
        assert captured.value.code == "model_response_invalid"


def test_merge_prompt_stays_bounded_without_sending_validated_claims() -> None:
    candidates = [
        {
            "title": f"Concept {ordinal}",
            "aliases": [],
            "tags": [],
            "claims": [
                {
                    "text": f"Claim {ordinal}: " + ("x" * 3_800),
                    "source_evidence_ids": [],
                }
            ],
        }
        for ordinal in range(32)
    ]
    analysis = parse_knowledge_analysis(
        json.dumps(
            {
                "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
                "analysis_scope": "batch",
                "document_description": "Large but individually valid batch result.",
                "concepts": candidates,
                "entities": [],
            }
        ),
        expected_scope="batch",
    )

    payload = json.loads(knowledge_analysis_merge_prompt("large.md", (analysis,)))

    assert payload["descriptions"] == ["Large but individually valid batch result."]
    assert "batch_results" not in payload
    assert "concepts" not in payload


def test_document_merge_scope_can_represent_the_full_batch_union() -> None:
    candidates = [
        {
            "title": f"Concept {candidate_ordinal}",
            "aliases": [],
            "tags": [],
            "claims": [
                {
                    "text": f"Claim {candidate_ordinal}-{claim_ordinal}.",
                    "source_evidence_ids": (
                        [f"evidence-{source_ordinal}" for source_ordinal in range(9)]
                        if candidate_ordinal == 0 and claim_ordinal == 0
                        else []
                    ),
                }
                for claim_ordinal in range(65 if candidate_ordinal == 0 else 1)
            ],
        }
        for candidate_ordinal in range(33)
    ]

    payload = json.dumps(
        {
            "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
            "analysis_scope": "document",
            "document_description": "Complete aggregate result.",
            "concepts": candidates,
            "entities": [],
        }
    )
    with pytest.raises(DesktopImportError):
        parse_knowledge_analysis(payload)

    analysis = parse_knowledge_analysis(payload, aggregate=True)

    assert len(analysis.concepts) == 33
    assert len(analysis.concepts[0].claims) == 65
    assert len(analysis.concepts[0].claims[0].source_evidence_ids) == 9
