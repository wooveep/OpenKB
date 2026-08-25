"""Immutable token planning and hierarchical merge release gates."""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from openkb import desktop_model_transport
from openkb.desktop_import import DesktopImportError, DesktopTextImportService
from openkb.desktop_import_artifacts import DocumentIRBlock
from openkb.desktop_import_types import DesktopRecoveryOverride
from openkb.desktop_knowledge_analysis import (
    KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
    parse_knowledge_analysis,
)
from openkb.desktop_knowledge_analysis_batches import (
    deterministic_merge_knowledge,
    estimate_knowledge_analysis_batch_tokens,
    plan_knowledge_analysis_batches,
)
from openkb.desktop_knowledge_analysis_plan import (
    estimate_model_tokens,
    hierarchical_merge_topology,
    knowledge_analysis_input_budget,
)
from openkb.desktop_model_capabilities import DesktopModelCapabilityProfile
from openkb.desktop_model_gateway import DesktopModelGateway, DesktopModelTransportError
from openkb.desktop_model_settings import save_desktop_model_settings
from openkb.desktop_prompt_contracts import prompt_contract_for
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime


def _representative_evidence(block_count: int = 662):
    return tuple(
        (
            f"evidence-{ordinal}",
            DocumentIRBlock(
                block_id=f"block-{ordinal}",
                ordinal=ordinal,
                kind="paragraph",
                text=(f"第 {ordinal} 段 OpenKB 文档分析事实。" + " bounded evidence" * 8),
                heading_path=(f"Section {ordinal // 12}",),
                line_start=ordinal + 1,
                line_end=ordinal + 1,
                locator={"paragraph": ordinal},
            ),
        )
        for ordinal in range(block_count)
    )


def test_662_blocks_fit_no_more_than_twenty_fallback_profile_batches() -> None:
    evidence = _representative_evidence()

    batches = plan_knowledge_analysis_batches(
        evidence,
        document_name="V10.3版本发布说明.docx",
        input_budget_tokens=8_000,
    )

    assert len(batches) <= 20
    assert tuple(item for batch in batches for item in batch) == evidence
    for ordinal, batch in enumerate(batches):
        assert (
            estimate_knowledge_analysis_batch_tokens(
                "V10.3版本发布说明.docx",
                batch,
                batch_ordinal=ordinal,
                batch_total=len(batches),
            )
            <= 8_000
        )


def test_hierarchical_topology_is_bounded_and_deterministic() -> None:
    first = hierarchical_merge_topology(20, fan_in=4)
    second = hierarchical_merge_topology(20, fan_in=4)

    assert first == second
    assert len(first) == 7
    assert all(2 <= len(node.child_ids) <= 4 for node in first)
    assert first[-1].child_ids == ("merge:1:0", "merge:0:4")


def test_plan_is_committed_before_first_batch_model_call(tmp_path) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "long.md"
    source.write_text(
        "\n\n".join(
            f"# Section {ordinal}\n\n" + ("Long evidence. " * 400) for ordinal in range(24)
        ),
        encoding="utf-8",
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    observed_plan: list[dict[str, object]] = []

    def fail_first_call(_request, _timeout_seconds):
        with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
            row = connection.execute("SELECT plan_json FROM knowledge_analysis_plans").fetchone()
        assert row is not None
        observed_plan.append(json.loads(row[0]))
        raise DesktopModelTransportError("input")

    with pytest.raises(DesktopImportError):
        DesktopTextImportService(
            kb_dir,
            model_gateway=DesktopModelGateway(
                fail_first_call,
                provider_name="provider",
                model_name="private-unknown-model",
            ),
        ).import_text(source)

    plan = observed_plan[0]
    assert plan["analysis_model"] == "private-unknown-model"
    assert plan["capability_profile"]["context_capacity"] == 16_000
    assert plan["input_budget_tokens"] == 8_000
    assert len(plan["prompt_contract_digest"]) == 64
    assert plan["prompt_contract_snapshot"]["contracts"]["knowledge_analysis_batch"][
        "version"
    ].endswith(".v2")
    assert plan["document_ir_digest"]
    assert plan["batches"]
    assert plan["merge_topology"]


def test_recovery_uses_the_exact_prompt_snapshot_persisted_by_an_older_plan(tmp_path) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "legacy-prompt.txt"
    source.write_text("Evidence analyzed under a persisted prompt contract.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)

    def fail_after_plan(_request, _timeout_seconds):
        raise DesktopModelTransportError("input")

    importer = DesktopTextImportService(
        kb_dir,
        model_gateway=DesktopModelGateway(fail_after_plan),
    )
    with pytest.raises(DesktopImportError, match="rejected"):
        importer.import_text(source)
    job_id = str(importer.list_import_jobs()["jobs"][0]["job"]["job_id"])

    legacy_instructions = "Legacy v1 instructions pinned before the prompt changed."
    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        (plan_json,) = connection.execute(
            "SELECT plan_json FROM knowledge_analysis_plans WHERE job_id = ?", (job_id,)
        ).fetchone()
        plan = json.loads(plan_json)
        snapshot = plan["prompt_contract_snapshot"]["contracts"]["knowledge_analysis"]
        snapshot["version"] = "openkb.prompt.knowledge_analysis.v1"
        snapshot["instructions"] = legacy_instructions
        bundle = json.dumps(
            plan["prompt_contract_snapshot"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(bundle.encode("utf-8")).hexdigest()
        plan["prompt_contract_digest"] = digest
        connection.execute(
            "UPDATE knowledge_analysis_plans "
            "SET prompt_contract_digest = ?, plan_json = ? WHERE job_id = ?",
            (
                digest,
                json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                job_id,
            ),
        )

    recovered_requests = []

    def recover(request, _timeout_seconds):
        recovered_requests.append(request)
        return json.dumps(
            {
                "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
                "analysis_scope": "document",
                "document_description": "Recovered with the pinned contract.",
                "concepts": [],
                "entities": [],
            }
        )

    DesktopTextImportService(
        kb_dir,
        model_gateway=DesktopModelGateway(recover),
    ).recover_text(job_id, DesktopRecoveryOverride())

    assert len(recovered_requests) == 1
    request = recovered_requests[0]
    assert request.prompt_contract_version == "openkb.prompt.knowledge_analysis.v1"
    assert request.prompt_contract_snapshot["instructions"] == legacy_instructions


def test_token_estimator_is_conservative_for_chinese_and_ascii() -> None:
    assert estimate_model_tokens("知识分析") == 4
    assert estimate_model_tokens("abcdefgh") == 2


def test_large_context_profiles_cap_analysis_batches_at_twelve_thousand_tokens() -> None:
    capability = DesktopModelCapabilityProfile(
        context_capacity=128_000,
        document_input_capacity=100_000,
        supports_native_json_schema=True,
        supports_streaming=True,
        supports_reasoning=True,
    )

    assert (
        knowledge_analysis_input_budget(capability, prompt_contract_for("knowledge_analysis_batch"))
        == 12_000
    )


def test_exact_knowledge_is_deduplicated_before_description_merge() -> None:
    def analysis(evidence_id: str, aliases: list[str], tags: list[str]):
        return parse_knowledge_analysis(
            json.dumps(
                {
                    "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
                    "analysis_scope": "batch",
                    "document_description": "Batch description.",
                    "concepts": [
                        {
                            "title": " OpenKB ",
                            "aliases": aliases,
                            "tags": tags,
                            "claims": [
                                {
                                    "text": "Evidence-bound analysis.",
                                    "source_evidence_ids": [evidence_id],
                                }
                            ],
                        }
                    ],
                    "entities": [],
                }
            ),
            expected_scope="batch",
        )

    merged = deterministic_merge_knowledge(
        (
            analysis("evidence-1", ["OKB"], ["knowledge"]),
            analysis("evidence-2", ["okb", "Open KB"], ["Knowledge", "local"]),
        )
    )

    assert len(merged.concepts) == 1
    assert merged.concepts[0].aliases == ("OKB", "Open KB")
    assert merged.concepts[0].tags == ("knowledge", "local")
    assert merged.concepts[0].claims[0].source_evidence_ids == (
        "evidence-1",
        "evidence-2",
    )


def test_completed_hierarchical_merge_nodes_are_reused_on_recovery(tmp_path) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "hierarchical.md"
    source.write_text(
        "\n\n".join(
            f"# Section {ordinal}\n\n" + (f"Evidence {ordinal}. " * 500) for ordinal in range(20)
        ),
        encoding="utf-8",
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    merge_operations: list[str] = []
    failed_once = False

    def transport(request, _timeout_seconds):
        nonlocal failed_once
        payload = json.loads(request.content)
        if request.operation == "knowledge_analysis_batch":
            return json.dumps(
                {
                    "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
                    "analysis_scope": "batch",
                    "document_description": f"Batch {payload['batch_ordinal']}.",
                    "concepts": [],
                    "entities": [],
                }
            )
        if request.operation == "knowledge_analysis_merge":
            node_id = str(payload["merge_node_id"])
            merge_operations.append(node_id)
            if len(merge_operations) == 2 and not failed_once:
                failed_once = True
                raise DesktopModelTransportError("input")
            return json.dumps({"document_description": " ".join(payload["descriptions"])})
        raise AssertionError(request.operation)

    gateway = DesktopModelGateway(transport)
    importer = DesktopTextImportService(kb_dir, model_gateway=gateway)
    with pytest.raises(DesktopImportError):
        importer.import_text(source)
    job_id = importer.list_import_jobs()["jobs"][0]["job"]["job_id"]
    first_completed_node = merge_operations[0]

    DesktopTextImportService(kb_dir, model_gateway=gateway).recover_text(
        job_id,
        DesktopRecoveryOverride(),
    )

    assert merge_operations.count(first_completed_node) == 1
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        statuses = connection.execute(
            "SELECT status FROM knowledge_analysis_merge_nodes WHERE job_id = ?",
            (job_id,),
        ).fetchall()
    assert statuses and all(row == ("completed",) for row in statuses)


def test_recovery_uses_the_analysis_model_pinned_before_settings_changed(
    tmp_path, monkeypatch
) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "pinned.md"
    source.write_text(
        "\n\n".join(
            f"# Section {ordinal}\n\n" + (f"Pinned evidence {ordinal}. " * 350)
            for ordinal in range(12)
        ),
        encoding="utf-8",
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    save_desktop_model_settings(
        kb_dir,
        provider="custom",
        model="default-model",
        analysis_model="old-analysis",
        api_base_url="https://model.test/v1",
        api_key="key",
        max_concurrent_model_calls=2,
        initial_timeout_seconds=1,
    )
    calls: list[tuple[str, str]] = []
    failed = False

    class FakeTransport:
        def __init__(self, *, model, bundle):
            self.model = str(model)

        def __call__(self, request, _timeout_seconds):
            nonlocal failed
            calls.append((self.model, request.operation))
            payload = json.loads(request.content)
            if request.operation == "knowledge_analysis_batch":
                if int(payload["batch_ordinal"]) == 1 and not failed:
                    failed = True
                    raise DesktopModelTransportError("input")
                return json.dumps(
                    {
                        "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
                        "analysis_scope": "batch",
                        "document_description": "Pinned batch.",
                        "concepts": [],
                        "entities": [],
                    }
                )
            if request.operation == "knowledge_analysis_merge":
                return json.dumps({"document_description": "Pinned merge."})
            return json.dumps({"nodes": [], "edges": []})

    monkeypatch.setattr(desktop_model_transport, "DesktopLiteLLMTransport", FakeTransport)
    first_gateway = desktop_model_transport.desktop_model_gateway_for(kb_dir)
    assert first_gateway is not None
    importer = DesktopTextImportService(kb_dir, model_gateway=first_gateway)
    with pytest.raises(DesktopImportError):
        importer.import_text(source)
    job_id = importer.list_import_jobs()["jobs"][0]["job"]["job_id"]

    save_desktop_model_settings(
        kb_dir,
        provider="custom",
        model="default-model",
        analysis_model="new-analysis",
        api_base_url="https://model.test/v1",
        api_key="key",
        max_concurrent_model_calls=2,
        initial_timeout_seconds=1,
    )
    calls.clear()
    recovery_gateway = desktop_model_transport.desktop_model_gateway_for(kb_dir)
    assert recovery_gateway is not None
    DesktopTextImportService(kb_dir, model_gateway=recovery_gateway).recover_text(
        job_id,
        DesktopRecoveryOverride(),
    )

    analysis_calls = [model for model, operation in calls if "analysis" in operation]
    assert analysis_calls
    assert set(analysis_calls) == {"openai/old-analysis"}
