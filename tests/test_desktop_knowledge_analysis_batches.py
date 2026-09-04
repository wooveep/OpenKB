"""Resumable natural-section Knowledge Analysis Batch behavior."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from openkb.desktop_import import DesktopImportError, DesktopTextImportService
from openkb.desktop_import_artifacts import DocumentIRBlock
from openkb.desktop_import_types import DesktopRecoveryOverride
from openkb.desktop_knowledge_analysis import (
    KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
    DesktopKnowledgeAnalysis,
    knowledge_analysis_prompt,
    parse_knowledge_analysis,
)
from openkb.desktop_knowledge_analysis_batch_planning import knowledge_analysis_batch_prompt
from openkb.desktop_knowledge_analysis_batch_store import KnowledgeAnalysisBatch
from openkb.desktop_knowledge_analysis_batches import (
    _run_hierarchical_description_merge,
    knowledge_analysis_merge_prompt,
    plan_knowledge_analysis_batches,
)
from openkb.desktop_knowledge_analysis_merge import parse_merged_description
from openkb.desktop_knowledge_analysis_plan import (
    KnowledgeAnalysisPlan,
    hierarchical_merge_topology,
)
from openkb.desktop_model_capabilities import DesktopModelCapabilityProfile
from openkb.desktop_model_gateway import (
    DesktopModelGateway,
    DesktopModelOutputObservations,
    DesktopModelProviderResponse,
    DesktopModelResult,
    DesktopModelTransportError,
)
from openkb.desktop_prompt_contracts import prompt_contract_for
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


def test_merge_description_is_deterministically_bounded_at_a_readable_boundary() -> None:
    content = json.dumps({"document_description": "完整的一句描述。" * 600})

    description = parse_merged_description(content)

    assert len(description) <= 4_000
    assert description.endswith("。")


def test_all_analysis_prompts_carry_the_pinned_knowledge_language() -> None:
    block = DocumentIRBlock(
        block_id="block-1",
        ordinal=0,
        kind="paragraph",
        text="Install the cluster.",
        heading_path=("Installation",),
        line_start=1,
        line_end=1,
    )
    evidence = (("evidence-1", block),)
    batch = KnowledgeAnalysisBatch(
        batch_id="batch-1",
        ordinal=0,
        section_paths=(("Installation",),),
        evidence=evidence,
        status="pending",
    )

    prompts = (
        knowledge_analysis_prompt("guide.md", evidence, knowledge_language="zh"),
        knowledge_analysis_batch_prompt("guide.md", batch, batch_total=1, knowledge_language="zh"),
        knowledge_analysis_merge_prompt(
            "guide.md", ("Installation guide.",), knowledge_language="zh"
        ),
    )

    assert all(json.loads(prompt)["knowledge_language"] == "zh" for prompt in prompts)


def test_aggregate_summary_is_bounded_while_covering_the_document_edges() -> None:
    summary = [
        {
            "role": "key_topic",
            "text": f"Topic {ordinal}",
            "source_evidence_ids": [f"evidence-{ordinal}"],
        }
        for ordinal in range(40)
    ]
    payload = {
        "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
        "analysis_scope": "document",
        "document_description": "Long document.",
        "document_summary": summary,
        "concepts": [],
        "entities": [],
        "procedures": [],
    }

    analysis = parse_knowledge_analysis(json.dumps(payload), aggregate=True)

    assert len(analysis.document_summary) == 32
    retained_sources = {
        source_id for unit in analysis.document_summary for source_id in unit.source_evidence_ids
    }
    assert "evidence-0" in retained_sources
    assert "evidence-39" in retained_sources


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


def test_one_document_dispatches_independent_analysis_batches_concurrently(
    tmp_path: Path,
) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "long.md"
    _long_source(source)
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    lock = threading.Lock()
    two_batches_started = threading.Event()
    active_batch_calls = 0
    peak_batch_calls = 0
    started_batch_calls = 0

    def transport(request, _timeout_seconds):
        nonlocal active_batch_calls, peak_batch_calls, started_batch_calls
        if request.operation == "knowledge_fact_harvest":
            assert request.generation_parameters is not None
            assert request.generation_parameters["max_tokens"] == 8_000
            with lock:
                active_batch_calls += 1
                started_batch_calls += 1
                peak_batch_calls = max(peak_batch_calls, active_batch_calls)
                if started_batch_calls == 2:
                    two_batches_started.set()
            two_batches_started.wait(timeout=0.5)
            with lock:
                active_batch_calls -= 1
            return _analysis("batch")
        if request.operation == "knowledge_analysis_merge":
            assert request.generation_parameters is not None
            assert request.generation_parameters["max_tokens"] > 0
            return _analysis("document")
        raise AssertionError(request.operation)

    gateway = DesktopModelGateway(transport)
    gateway.analysis_concurrency = 2
    imported = DesktopTextImportService(kb_dir, model_gateway=gateway).import_text(source)

    assert imported.document.availability == "available"
    assert started_batch_calls == 2
    assert peak_batch_calls == 2


def test_output_limited_direct_analysis_is_split_instead_of_repaired(
    tmp_path: Path,
) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "long.md"
    _long_source(source)
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    calls: list[tuple[str, str | None, int]] = []
    truncated = False

    def transport(request, _timeout_seconds):
        nonlocal truncated
        payload = json.loads(request.content)
        evidence = payload.get("evidence", [])
        calls.append((request.operation, request.batch_id, len(evidence)))
        if request.operation == "knowledge_fact_harvest":
            assert request.generation_parameters is not None
            assert request.generation_parameters["max_tokens"] == 16_384
            if not truncated and len(evidence) > 1:
                truncated = True
                return DesktopModelProviderResponse(
                    '{"schema_version":"openkb.knowledge-analysis.v2",',
                    observations=DesktopModelOutputObservations(
                        finish_reason="length",
                        final_content_observed=True,
                        final_chunk_count=1,
                        final_character_count=50,
                        output_limit_reached=True,
                    ),
                )
            return _analysis("batch")
        if request.operation == "knowledge_analysis_merge":
            return json.dumps({"document_description": "Recovered after split."})
        raise AssertionError(f"Unexpected repair after output truncation: {request.operation}")

    imported = DesktopTextImportService(
        kb_dir,
        model_gateway=DesktopModelGateway(
            transport,
            provider_name="deepseek",
            model_name="deepseek-v4-pro",
        ),
    ).import_text(source)

    assert imported.document.availability == "available"
    assert truncated
    assert calls[0][0] == "knowledge_fact_harvest"
    split_calls = [call for call in calls if call[1] and ":split:" in call[1]]
    assert split_calls
    assert all(evidence_count < calls[0][2] for _, _, evidence_count in split_calls)
    assert all(operation != "structured_output_repair" for operation, _, _ in calls)
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        checkpoint_json = connection.execute(
            """
            SELECT runtime.checkpoint_json
            FROM stage_run_runtime AS runtime
            JOIN stage_runs AS stages ON stages.stage_run_id = runtime.stage_run_id
            WHERE runtime.job_id = ? AND stages.stage = 'model_analysis'
            """,
            (imported.job.job_id,),
        ).fetchone()[0]
    checkpoint = json.loads(str(checkpoint_json))
    assert checkpoint["output_limit_recovery_from_operation"] == "knowledge_fact_harvest"
    assert checkpoint["output_limit_split_leaf_count"] == 2
    assert checkpoint["output_limit_recovery_count"] == 1
    assert checkpoint["prompt_contract_snapshot"]["version"].endswith(".knowledge_fact_harvest.v1")


def test_output_limited_persisted_batch_records_split_checkpoint(tmp_path: Path) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "long.md"
    _long_source(source)
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    operations: list[str] = []
    truncated = False

    def transport(request, _timeout_seconds):
        nonlocal truncated
        payload = json.loads(request.content)
        operations.append(request.operation)
        if request.operation == "knowledge_fact_harvest":
            evidence = payload["evidence"]
            if not truncated and int(payload["batch_ordinal"]) == 0 and len(evidence) > 1:
                truncated = True
                return DesktopModelProviderResponse(
                    '{"schema_version":"openkb.knowledge-analysis.v1",',
                    observations=DesktopModelOutputObservations(
                        finish_reason="length",
                        final_content_observed=True,
                        final_chunk_count=1,
                        final_character_count=50,
                        output_limit_reached=True,
                    ),
                )
            return _analysis("batch")
        if request.operation == "knowledge_analysis_merge":
            return json.dumps({"document_description": "Recovered persisted batch."})
        raise AssertionError(request.operation)

    imported = DesktopTextImportService(
        kb_dir,
        model_gateway=DesktopModelGateway(transport),
    ).import_text(source)

    assert imported.document.availability == "available"
    assert truncated
    assert "structured_output_repair" not in operations
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        checkpoints = tuple(
            json.loads(str(row[0]))
            for row in connection.execute(
                """
                SELECT checkpoint_json FROM knowledge_analysis_batches
                WHERE job_id = ? ORDER BY batch_ordinal
                """,
                (imported.job.job_id,),
            ).fetchall()
        )
    recovered = tuple(
        checkpoint
        for checkpoint in checkpoints
        if checkpoint.get("output_limit_recovery_count") is not None
    )
    assert len(recovered) == 1
    assert recovered[0]["output_limit_split_leaf_count"] == 2
    assert recovered[0]["output_limit_recovery_count"] == 1


def test_independent_description_merge_nodes_run_concurrently() -> None:
    topology = hierarchical_merge_topology(8)
    contracts = {
        operation: prompt_contract_for(operation).snapshot()
        for operation in (
            "knowledge_analysis",
            "knowledge_analysis_batch",
            "knowledge_analysis_merge",
            "structured_output_repair",
        )
    }
    plan = KnowledgeAnalysisPlan(
        document_ir_digest="digest",
        provider="provider",
        analysis_model="model",
        capability_profile=DesktopModelCapabilityProfile(16_000, 8_000, False, True, False),
        prompt_contract_snapshot={
            "primary_operation": "knowledge_analysis_batch",
            "contracts": contracts,
        },
        prompt_contract_digest="digest",
        input_budget_tokens=8_000,
        output_budget_tokens=4_096,
        final_output_reserve_tokens=4_096,
        reasoning_allowance_tokens=0,
        execution_profile=None,
        batches=(),
        merge_topology=topology,
    )
    lock = threading.Lock()
    both_started = threading.Event()
    active = 0
    peak = 0

    class Store:
        def merge_node_checkpoint(self, _job_id, _node_id):
            return None

        def start_merge_node(self, _job_id, _node_id):
            return None

        def complete_merge_node(self, _job_id, _node_id, _checkpoint):
            return None

        def fail_merge_node(self, _job_id, _node_id, _error_code):
            return None

    def analyze(request):
        nonlocal active, peak
        node_id = json.loads(request.content)["merge_node_id"]
        if node_id.startswith("merge:0"):
            with lock:
                active += 1
                peak = max(peak, active)
                if active == 2:
                    both_started.set()
            both_started.wait(timeout=0.5)
            with lock:
                active -= 1
        return DesktopModelResult(
            node_id,
            json.dumps({"document_description": f"Description {node_id}"}),
            1,
        )

    _run_hierarchical_description_merge(
        store=Store(),
        plan=plan,
        job_id="job",
        document_name="document",
        analyze=analyze,
        analyses=tuple(
            DesktopKnowledgeAnalysis(f"Batch {ordinal}", (), ()) for ordinal in range(8)
        ),
        honor_control=lambda: None,
        max_parallel_batches=2,
    )

    assert peak == 2


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
        if request.operation == "knowledge_fact_harvest":
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
        if request.operation == "knowledge_fact_harvest":
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
        if key == "knowledge_fact_harvest":
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
        if request.operation == "knowledge_fact_harvest":
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
    repair_validation_errors: list[str] = []

    def transport(request, _timeout_seconds):
        nonlocal first_evidence_id
        payload = json.loads(request.content)
        operations.append(request.operation)
        if request.operation == "structured_output_repair":
            repair_validation_errors.extend(payload["validation_errors"])
            return str(payload["invalid_result"])
        if request.operation == "knowledge_fact_harvest":
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
                                    "role": "detail",
                                    "applicability": {
                                        "product_version": "",
                                        "platform": "",
                                        "deployment_scenario": "",
                                        "time_boundary": "",
                                    },
                                }
                            ],
                        }
                    ],
                    "entities": [],
                    "procedures": [],
                    "document_summary": [
                        {
                            "role": "key_topic",
                            "text": f"Batch {ordinal} summary.",
                            "source_evidence_ids": [first_evidence_id],
                        }
                    ],
                }
            )
        raise AssertionError("Invalid batch must stop before merge.")

    with pytest.raises(DesktopImportError) as captured:
        DesktopTextImportService(kb_dir, model_gateway=DesktopModelGateway(transport)).import_text(
            source
        )

    assert captured.value.code == "model_response_invalid"
    assert operations == [
        "knowledge_fact_harvest",
        "knowledge_fact_harvest",
        "structured_output_repair",
    ]
    assert len(repair_validation_errors) == 1
    assert first_evidence_id in repair_validation_errors[0]
    assert "concepts[0].claims[0].source_evidence_ids" in repair_validation_errors[0]
    assert "document_summary[0].source_evidence_ids" in repair_validation_errors[0]
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_documents").fetchone() == (0,)


def test_merge_cannot_invent_a_claim_even_with_valid_document_evidence(tmp_path: Path) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "long.md"
    _long_source(source)
    DesktopKnowledgeBaseRuntime().create(kb_dir)

    def transport(request, _timeout_seconds):
        payload = json.loads(request.content)
        if request.operation == "knowledge_fact_harvest":
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
        if request.operation == "knowledge_fact_harvest":
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


def test_document_merge_scope_can_represent_the_full_batch_union_with_bounded_claims() -> None:
    candidates = [
        {
            "title": f"Concept {candidate_ordinal}",
            "aliases": [],
            "tags": [],
            "claims": [
                {
                    "text": f"Claim {candidate_ordinal}-{claim_ordinal}.",
                    "source_evidence_ids": (
                        [f"evidence-{source_ordinal}" for source_ordinal in range(16)]
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
    assert len(analysis.concepts[0].claims[0].source_evidence_ids) == 16


def test_document_merge_scope_rejects_more_than_shared_claim_source_limit() -> None:
    payload = json.dumps(
        {
            "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
            "analysis_scope": "document",
            "document_description": "Aggregate result.",
            "concepts": [
                {
                    "title": "Bounded concept",
                    "aliases": [],
                    "tags": [],
                    "claims": [
                        {
                            "text": "One claim cannot absorb an unbounded source union.",
                            "source_evidence_ids": [f"evidence-{ordinal}" for ordinal in range(33)],
                        }
                    ],
                }
            ],
            "entities": [],
        }
    )

    with pytest.raises(DesktopImportError, match="at most 32"):
        parse_knowledge_analysis(payload, aggregate=True)
