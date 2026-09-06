"""Explicit exact-profile capability checks gate paid structured Analysis."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from openkb.importing.service import DesktopImportError, DesktopTextImportService
from openkb.importing.types import DesktopRecoveryOverride
from openkb.knowledge.analysis.service import KNOWLEDGE_ANALYSIS_SCHEMA_VERSION
from openkb.knowledge.graph.service import DesktopKnowledgeGraphService
from openkb.models import transport as desktop_model_transport
from openkb.models.analysis_gate import DesktopAnalysisCapabilityGate
from openkb.models.capability_check import capability_check_request
from openkb.models.capability_store import DesktopModelCapabilityStore
from openkb.models.execution_profile import build_analysis_execution_profile
from openkb.models.gateway import (
    DesktopModelOutputObservations,
    DesktopModelProviderResponse,
    DesktopModelTransportError,
    DesktopProviderTokenUsage,
)
from openkb.models.operation_state import DesktopModelOperationContractStore
from openkb.models.prompt_contracts import prompt_contract_for
from openkb.models.result_failure import (
    authorize_model_operation_retry,
    model_operation_dispatch_allowed,
    model_operation_dispatch_possible,
)
from openkb.models.settings import (
    save_desktop_model_settings,
    validate_desktop_model_settings,
)
from openkb.models.usage import DesktopModelUsageStore
from openkb.retrieval.planning import build_query_plan
from openkb.workspace.runtime import (
    DesktopKnowledgeBaseRuntime,
    desktop_state_database_path,
)


def _settings(*, reasoning: str = "off"):
    return validate_desktop_model_settings(
        provider="deepseek",
        model="deepseek-v4-pro",
        analysis_model="deepseek-v4-pro",
        api_base_url="https://api.deepseek.com",
        api_key="test-key",
        max_concurrent_model_calls=1,
        analysis_reasoning=reasoning,
    )


def _profile(*, reasoning: str = "off"):
    settings = _settings(reasoning=reasoning)
    return build_analysis_execution_profile(
        provider=settings.provider,
        model=settings.analysis_model_name,
        capability=settings.capability_for_role("analysis"),
        reasoning_effort=settings.reasoning_for_role("analysis") or "off",
        api_base_url=settings.api_base_url,
    )


def _valid_query_planning_response(term: str = "Atlas") -> str:
    return json.dumps(
        {
            "retrieval_plan": {"terms": [term]},
            "question_facet_plan": {
                "goal": "Answer from the available Evidence.",
                "facets": [
                    {
                        "label": "Available Evidence",
                        "description": "What the available Evidence establishes.",
                        "importance": "required",
                    }
                ],
            },
            "initial_answer_coverage": [
                {"facet_ordinal": 0, "state": "missing", "evidence_ids": []}
            ],
        }
    )


def test_prompt_contract_change_preserves_shared_analysis_verification(tmp_path) -> None:
    kb_dir = tmp_path / "knowledge"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    baseline = _profile()
    changed_contract = replace(
        baseline,
        prompt_contract_digest="changed-operation-contract",
        generation_policy_digest="changed-generation-policy",
    )
    store = DesktopModelCapabilityStore(kb_dir)

    store.mark_verified(baseline)

    assert store.is_verified(changed_contract)
    assert store.state(changed_contract).profile_identity == (
        baseline.capability_evidence_profile.identity
    )


def test_capability_cache_is_exact_and_has_no_time_expiry(tmp_path) -> None:
    kb_dir = tmp_path / "knowledge"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    store = DesktopModelCapabilityStore(kb_dir)
    profile = _profile()

    assert store.state(profile).status == "unchecked"
    store.begin(profile)
    assert store.state(profile).status == "checking"
    store.mark_verified(profile)

    assert DesktopModelCapabilityStore(kb_dir).is_verified(profile)
    assert DesktopModelCapabilityStore(kb_dir).state(_profile(reasoning="high")).status == (
        "unchecked"
    )


def test_local_domain_failure_cannot_invalidate_shared_analysis_verification(tmp_path) -> None:
    kb_dir = tmp_path / "knowledge"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    profile = _profile()
    store = DesktopModelCapabilityStore(kb_dir)
    store.mark_verified(profile)
    gate = DesktopAnalysisCapabilityGate(kb_dir, profile, True)

    gate.invalidate_failure(
        "model_response_invalid",
        reason="One operation rejected its own result shape.",
    )

    assert store.state(profile).status == "verified"

    gate.invalidate_failure(
        "model_configuration_invalid",
        reason="The provider adapter confirmed an invalid configuration.",
    )

    assert store.state(profile).status == "unchecked"
    assert store.state(profile).failure_code == "model_configuration_invalid"


def test_successful_operation_contract_clears_only_its_exact_suspension(tmp_path) -> None:
    kb_dir = tmp_path / "knowledge"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    store = DesktopModelOperationContractStore(kb_dir)
    shared = _profile().capability_evidence_profile
    graph_digest = prompt_contract_for("knowledge_relation_analysis").digest
    plan_digest = prompt_contract_for("query_planning").digest
    store.suspend(
        operation="knowledge_relation_analysis",
        capability_identity=shared.identity,
        prompt_contract_digest=graph_digest,
        failure_code="knowledge_graph_response_invalid",
        reason="invalid graph",
        failure_stage="domain_validation",
    )
    store.suspend(
        operation="query_planning",
        capability_identity=shared.identity,
        prompt_contract_digest=plan_digest,
        failure_code="model_response_invalid",
        reason="invalid plan",
        failure_stage="domain_validation",
    )

    store.mark_ready(
        operation="knowledge_relation_analysis",
        capability_identity=shared.identity,
        prompt_contract_digest=graph_digest,
    )

    graph = store.state(
        operation="knowledge_relation_analysis",
        capability_identity=shared.identity,
        prompt_contract_digest=graph_digest,
    )
    plan = store.state(
        operation="query_planning",
        capability_identity=shared.identity,
        prompt_contract_digest=plan_digest,
    )
    assert graph.status == "ready"
    assert graph.failure_code is None
    assert plan.status == "suspended"


def test_explicit_retry_round_is_scoped_and_keeps_suspension_until_a_terminal_result(
    tmp_path,
) -> None:
    kb_dir = tmp_path / "knowledge"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    profile = _profile()
    shared = profile.capability_evidence_profile
    digest = prompt_contract_for("query_planning").digest
    store = DesktopModelOperationContractStore(kb_dir)
    store.suspend(
        operation="query_planning",
        capability_identity=shared.identity,
        prompt_contract_digest=digest,
        failure_code="model_response_invalid",
        reason="invalid plan",
        failure_stage="domain_validation",
    )

    class Gateway:
        def execution_profile_for_operation(self, _operation):
            return profile

    gateway = Gateway()
    assert authorize_model_operation_retry(
        kb_dir,
        gateway,
        operation="query_planning",
        retry_scope="answer:one",
    )
    assert (
        store.state(
            operation="query_planning",
            capability_identity=shared.identity,
            prompt_contract_digest=digest,
        ).status
        == "suspended"
    )
    assert not model_operation_dispatch_allowed(
        kb_dir,
        gateway,
        operation="query_planning",
        retry_scope="answer:other",
    )
    assert model_operation_dispatch_possible(
        kb_dir,
        gateway,
        operation="query_planning",
        retry_scope="answer:one",
    )

    def join_retry_round(_worker: int) -> bool:
        return model_operation_dispatch_allowed(
            kb_dir,
            gateway,
            operation="query_planning",
            retry_scope="answer:one",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(join_retry_round, range(2))) == [True, True]
    store.suspend(
        operation="query_planning",
        capability_identity=shared.identity,
        prompt_contract_digest=digest,
        failure_code="model_response_invalid",
        reason="retry failed",
        failure_stage="domain_validation",
    )
    assert not model_operation_dispatch_allowed(
        kb_dir,
        gateway,
        operation="query_planning",
        retry_scope="answer:one",
    )


def test_new_contract_state_resolves_old_uncertain_signature_for_corroboration(
    tmp_path,
) -> None:
    kb_dir = tmp_path / "knowledge"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    store = DesktopModelOperationContractStore(kb_dir)
    shared = _profile().capability_evidence_profile
    signature = "same-uncertain-signature"
    store.suspend(
        operation="query_planning",
        capability_identity=shared.identity,
        prompt_contract_digest="old-retrieval-digest",
        failure_code="empty_final_result",
        reason="old failure",
        failure_stage="uncertain_shared_protocol",
        failure_signature=signature,
    )
    store.mark_ready(
        operation="query_planning",
        capability_identity=shared.identity,
        prompt_contract_digest="new-retrieval-digest",
    )

    independent = store.suspend(
        operation="knowledge_relation_analysis",
        capability_identity=shared.identity,
        prompt_contract_digest="graph-digest",
        failure_code="empty_final_result",
        reason="new failure",
        failure_stage="uncertain_shared_protocol",
        failure_signature=signature,
    )

    assert independent == 1
    assert (
        store.state(
            operation="query_planning",
            capability_identity=shared.identity,
            prompt_contract_digest="old-retrieval-digest",
        ).failure_signature
        is None
    )


def test_terminal_result_clears_only_the_exact_contract_retry_round(tmp_path) -> None:
    kb_dir = tmp_path / "knowledge"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    store = DesktopModelOperationContractStore(kb_dir)
    identity = _profile().capability_evidence_profile.identity
    for digest, scope in (("repair-a", "action-a"), ("repair-b", "action-b")):
        store.suspend(
            operation="structured_output_repair",
            capability_identity=identity,
            prompt_contract_digest=digest,
            failure_code="model_response_invalid",
            reason="invalid repair",
            failure_stage="domain_validation",
        )
        assert store.authorize_retry(
            operation="structured_output_repair",
            capability_identity=identity,
            prompt_contract_digest=digest,
            retry_scope=scope,
        )

    store.mark_ready(
        operation="structured_output_repair",
        capability_identity=identity,
        prompt_contract_digest="repair-a",
    )

    assert store.dispatch_possible(
        operation="structured_output_repair",
        capability_identity=identity,
        prompt_contract_digest="repair-b",
        retry_scope="action-b",
    )
    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        events = connection.execute(
            """
            SELECT status, failure_code, failure_stage
            FROM model_operation_contract_events
            WHERE operation = 'structured_output_repair'
                AND capability_identity = ? AND prompt_contract_digest = 'repair-a'
            ORDER BY event_id
            """,
            (identity,),
        ).fetchall()
    assert events == [
        ("suspended", "model_response_invalid", "domain_validation"),
        ("ready", None, None),
    ]


def test_analysis_capability_check_uses_the_exact_pinned_protocol() -> None:
    settings = _settings(reasoning="high")
    profile = _profile(reasoning="high")
    shared = profile.capability_evidence_profile

    request = capability_check_request(settings, profile=profile)

    assert request.operation == "model_capability_analysis"
    assert request.provider_adapter == "deepseek"
    assert request.provider_adapter_version == "deepseek.v2"
    assert request.structured_output_mode == "json_object"
    assert request.reasoning_effort == "high"
    assert request.supports_streaming is True
    assert request.response_schema is not None
    assert request.response_example == {"status": "ok"}
    assert request.generation_parameters == {
        "temperature": 0,
        "max_tokens": shared.provider_output_ceiling_tokens,
    }
    assert request.prompt_contract_digest == shared.prompt_contract_digest
    assert request.prompt_contract_digest != profile.prompt_contract_digest
    assert "JSON" in request.content


def test_unverified_profile_preserves_parsed_import_without_provider_call(
    tmp_path, monkeypatch
) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "document.txt"
    source.write_text("Evidence that must be parsed before Analysis waits.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    save_desktop_model_settings(
        kb_dir,
        provider="deepseek",
        model="deepseek-v4-pro",
        api_base_url="https://api.deepseek.com",
        api_key="test-key",
        max_concurrent_model_calls=1,
    )
    calls: list[str] = []

    class FakeTransport:
        def __init__(self, *, model, bundle):
            del model, bundle

        def __call__(self, request, _timeout_seconds):
            calls.append(request.operation)
            if request.operation == "knowledge_fact_harvest":
                return json.dumps(
                    {
                        "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
                        "analysis_scope": "document",
                        "document_description": "Verified Analysis.",
                        "document_summary": [],
                        "candidates": [],
                    }
                )
            return json.dumps({"nodes": [], "edges": []})

    monkeypatch.setattr(desktop_model_transport, "DesktopLiteLLMTransport", FakeTransport)
    gateway = desktop_model_transport.desktop_model_gateway_for(kb_dir)
    assert gateway is not None
    importer = DesktopTextImportService(kb_dir, model_gateway=gateway)

    with pytest.raises(DesktopImportError, match="explicit Model Capability Check"):
        importer.import_text(source)

    assert calls == []
    task = importer.list_import_jobs()["jobs"][0]
    assert task["job"]["status"] == "awaiting_model_configuration"
    stages = {stage["stage"]: stage for stage in task["stages"]}
    assert stages["document_ir"]["status"] == "completed"
    assert stages["evidence"]["status"] == "completed"

    profile = gateway.execution_profile_for_operation("knowledge_fact_harvest")
    DesktopModelCapabilityStore(kb_dir).mark_verified(profile)
    result = importer.resume_text(task["job"]["job_id"])

    assert result.document.availability == "available"
    assert "knowledge_fact_harvest" in calls


def test_protocol_result_failure_suspends_analysis_operation_before_corroboration(
    tmp_path, monkeypatch
) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "document.txt"
    source.write_text("Evidence for one failed structured result.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    save_desktop_model_settings(
        kb_dir,
        provider="deepseek",
        model="deepseek-v4-pro",
        api_base_url="https://api.deepseek.com",
        api_key="test-key",
        max_concurrent_model_calls=1,
    )

    class FakeTransport:
        def __init__(self, *, model, bundle):
            del model, bundle

        def __call__(self, request, _timeout_seconds):
            del request
            return DesktopModelProviderResponse(
                "",
                observations=DesktopModelOutputObservations(
                    finish_reason="length",
                    reasoning_observed=True,
                    reasoning_chunk_count=1,
                    reasoning_character_count=42,
                    output_limit_reached=True,
                ),
            )

    monkeypatch.setattr(desktop_model_transport, "DesktopLiteLLMTransport", FakeTransport)
    gateway = desktop_model_transport.desktop_model_gateway_for(kb_dir)
    assert gateway is not None
    profile = gateway.execution_profile_for_operation("knowledge_fact_harvest")
    capability_store = DesktopModelCapabilityStore(kb_dir)
    capability_store.mark_verified(profile)

    with pytest.raises(DesktopImportError, match="exhausted its output limit"):
        DesktopTextImportService(kb_dir, model_gateway=gateway).import_text(source)

    state = capability_store.state(profile)
    assert state.status == "verified"
    operation_state = DesktopModelOperationContractStore(kb_dir).state(
        operation="knowledge_fact_harvest",
        capability_identity=profile.capability_evidence_profile.identity,
        prompt_contract_digest=prompt_contract_for("knowledge_fact_harvest").digest,
    )
    assert operation_state.status == "suspended"
    assert operation_state.failure_code == "reasoning_output_exhausted"


def test_invalid_structured_import_suspends_only_fact_harvest_contract(
    tmp_path, monkeypatch
) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "document.txt"
    source.write_text("Evidence for an invalid structured result.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    save_desktop_model_settings(
        kb_dir,
        provider="deepseek",
        model="deepseek-v4-pro",
        api_base_url="https://api.deepseek.com",
        api_key="test-key",
        max_concurrent_model_calls=1,
    )

    calls: list[str] = []

    class FakeTransport:
        def __init__(self, *, model, bundle):
            del model, bundle

        def __call__(self, request, _timeout_seconds):
            calls.append(request.operation)
            return DesktopModelProviderResponse(
                "not-json",
                usage=DesktopProviderTokenUsage(11, 3, 14),
                provider_request_id=f"provider-request-{len(calls)}",
                observations=DesktopModelOutputObservations(
                    finish_reason="stop",
                    final_content_observed=True,
                    final_chunk_count=1,
                    final_character_count=8,
                ),
            )

    monkeypatch.setattr(desktop_model_transport, "DesktopLiteLLMTransport", FakeTransport)
    gateway = desktop_model_transport.desktop_model_gateway_for(kb_dir)
    assert gateway is not None
    profile = gateway.execution_profile_for_operation("knowledge_fact_harvest")
    capability_store = DesktopModelCapabilityStore(kb_dir)
    capability_store.mark_verified(profile)

    service = DesktopTextImportService(kb_dir, model_gateway=gateway)
    with pytest.raises(DesktopImportError) as captured:
        service.import_text(source)

    assert captured.value.code == "model_response_invalid"
    assert calls == ["knowledge_fact_harvest", "structured_output_repair"]
    task = service.list_import_jobs()["jobs"][0]
    assert task["job"]["status"] == "quarantined"
    assert len(task["model_calls"]) == 2
    failed_call = task["model_calls"][-1]
    assert failed_call["operation"] == "structured_output_repair"
    assert failed_call["status"] == "failed"
    assert failed_call["lifecycle_status"] == "model_result_failure"
    assert failed_call["error_code"] == "model_response_invalid"
    assert failed_call["finish_reason"] == "stop"
    assert failed_call["final_content_observed"] is True
    assert failed_call["input_tokens"] == 11
    assert failed_call["output_tokens"] == 3
    assert failed_call["total_tokens"] == 14
    assert failed_call["provider_request_id"] == "provider-request-2"
    assert failed_call["attempts"][-1]["status"] == "failed"
    assert failed_call["attempts"][-1]["lifecycle_status"] == "model_result_failure"
    assert failed_call["attempts"][-1]["error_code"] == "model_response_invalid"
    assert task["model_usage"][-1]["lifecycle_status"] == "model_result_failure"
    assert task["model_usage"][-1]["failure_code"] == "model_response_invalid"
    assert task["model_usage"][-1]["provider_request_id"] == "provider-request-2"
    state = capability_store.state(profile)
    assert state.status == "verified"
    operation_state = DesktopModelOperationContractStore(kb_dir).state(
        operation="knowledge_fact_harvest",
        capability_identity=profile.capability_evidence_profile.identity,
        prompt_contract_digest=prompt_contract_for("knowledge_fact_harvest").digest,
    )
    assert operation_state.status == "suspended"
    assert operation_state.failure_stage == "domain_validation"
    assert operation_state.failure_signature is None

    second_source = tmp_path / "second-document.txt"
    second_source.write_text(
        "A distinct document must not automatically retry the suspended contract.",
        encoding="utf-8",
    )
    with pytest.raises(DesktopImportError) as blocked:
        service.import_text(second_source)
    assert blocked.value.code == "model_operation_suspended"
    assert calls == ["knowledge_fact_harvest", "structured_output_repair"]

    with pytest.raises(DesktopImportError):
        service.recover_text(
            str(task["job"]["job_id"]),
            DesktopRecoveryOverride(legacy_recovery_choice="restart_current_plan"),
        )
    assert calls == [
        "knowledge_fact_harvest",
        "structured_output_repair",
        "knowledge_fact_harvest",
        "structured_output_repair",
    ]


def test_recovery_marks_the_actual_analysis_contract_ready_after_settings_change(
    tmp_path, monkeypatch
) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "pinned-recovery.txt"
    source.write_text("Evidence for a pinned Analysis recovery.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    settings_args = {
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "api_base_url": "https://api.deepseek.com",
        "api_key": "test-key",
        "max_concurrent_model_calls": 1,
    }
    save_desktop_model_settings(kb_dir, **settings_args, analysis_reasoning="off")
    calls: list[str] = []
    valid = False

    class InvalidThenValidTransport:
        def __init__(self, *, model, bundle):
            del model, bundle

        def __call__(self, request, _timeout_seconds):
            calls.append(request.operation)
            if not valid:
                return "not-json"
            return json.dumps(
                {
                    "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
                    "analysis_scope": "document",
                    "document_description": "Recovered with the persisted profile.",
                    "document_summary": [],
                    "candidates": [],
                }
            )

    monkeypatch.setattr(
        desktop_model_transport,
        "DesktopLiteLLMTransport",
        InvalidThenValidTransport,
    )
    original_gateway = desktop_model_transport.desktop_model_gateway_for(kb_dir)
    assert original_gateway is not None
    original_profile = original_gateway.execution_profile_for_operation("knowledge_fact_harvest")
    capability_store = DesktopModelCapabilityStore(kb_dir)
    capability_store.mark_verified(original_profile)
    service = DesktopTextImportService(kb_dir, model_gateway=original_gateway)
    with pytest.raises(DesktopImportError):
        service.import_text(source)
    task = service.list_import_jobs()["jobs"][0]

    save_desktop_model_settings(kb_dir, **settings_args, analysis_reasoning="high")
    changed_gateway = desktop_model_transport.desktop_model_gateway_for(kb_dir)
    assert changed_gateway is not None
    changed_profile = changed_gateway.execution_profile_for_operation("knowledge_fact_harvest")
    assert changed_profile.capability_evidence_profile.identity != (
        original_profile.capability_evidence_profile.identity
    )
    capability_store.mark_verified(changed_profile)
    valid = True

    recovered = DesktopTextImportService(
        kb_dir,
        model_gateway=changed_gateway,
    ).recover_text(
        str(task["job"]["job_id"]),
        DesktopRecoveryOverride(legacy_recovery_choice="restart_current_plan"),
    )

    assert recovered.document.availability == "available"
    assert calls == [
        "knowledge_fact_harvest",
        "structured_output_repair",
        "knowledge_fact_harvest",
    ]
    store = DesktopModelOperationContractStore(kb_dir)
    assert (
        store.state(
            operation="knowledge_fact_harvest",
            capability_identity=original_profile.capability_evidence_profile.identity,
            prompt_contract_digest=prompt_contract_for("knowledge_fact_harvest").digest,
        ).status
        == "suspended"
    )
    assert (
        store.state(
            operation="knowledge_fact_harvest",
            capability_identity=changed_profile.capability_evidence_profile.identity,
            prompt_contract_digest=prompt_contract_for("knowledge_fact_harvest").digest,
        ).status
        == "ready"
    )


@pytest.mark.parametrize(
    ("response", "failure_code"),
    [
        ("not-json", "model_response_invalid"),
        (
            DesktopModelProviderResponse(
                "",
                observations=DesktopModelOutputObservations(
                    finish_reason="length",
                    reasoning_observed=True,
                    reasoning_chunk_count=1,
                    reasoning_character_count=42,
                    output_limit_reached=True,
                ),
            ),
            "reasoning_output_exhausted",
        ),
    ],
)
def test_query_planning_failure_degrades_only_the_current_query(
    tmp_path, monkeypatch, response, failure_code
) -> None:
    kb_dir = tmp_path / "knowledge"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    save_desktop_model_settings(
        kb_dir,
        provider="deepseek",
        model="deepseek-v4-pro",
        api_base_url="https://api.deepseek.com",
        api_key="test-key",
        max_concurrent_model_calls=1,
    )

    class FakeTransport:
        def __init__(self, *, model, bundle):
            del model, bundle

        def __call__(self, request, _timeout_seconds):
            del request
            return response

    monkeypatch.setattr(desktop_model_transport, "DesktopLiteLLMTransport", FakeTransport)
    gateway = desktop_model_transport.desktop_model_gateway_for(kb_dir)
    assert gateway is not None
    profile = gateway.execution_profile_for_operation("query_planning")
    capability_store = DesktopModelCapabilityStore(kb_dir)
    capability_store.mark_verified(profile)

    result = build_query_plan("What evidence is available?", gateway, kb_dir=kb_dir)

    assert result.degradations == (
        "query_semantic_structure_unknown"
        if failure_code == "model_response_invalid"
        else "query_planning_failed",
    )
    usage = DesktopModelUsageStore(kb_dir).records()
    if failure_code == "model_response_invalid":
        assert [record["operation"] for record in usage] == [
            "query_planning",
            "structured_output_repair",
        ]
        failed = [record for record in usage if record["failure_code"] is not None]
        assert [record["operation"] for record in failed] == [
            "query_planning",
            "structured_output_repair",
        ]
        assert {record["lifecycle_status"] for record in failed} == {"model_result_failure"}
        assert {record["failure_code"] for record in failed} == {"model_response_invalid"}
    state = capability_store.state(profile)
    assert state.status == "verified"
    operation_state = DesktopModelOperationContractStore(kb_dir).state(
        operation="query_planning",
        capability_identity=profile.capability_evidence_profile.identity,
        prompt_contract_digest=prompt_contract_for("query_planning").digest,
    )
    assert operation_state.status == "unverified"
    assert operation_state.failure_code is None
    usage_count = len(DesktopModelUsageStore(kb_dir).records())

    retried = build_query_plan("What evidence is available?", gateway, kb_dir=kb_dir)

    assert retried.degradations == result.degradations
    assert len(DesktopModelUsageStore(kb_dir).records()) > usage_count


def test_later_query_with_valid_repair_marks_parent_and_bound_repair_ready(
    tmp_path, monkeypatch
) -> None:
    kb_dir = tmp_path / "knowledge"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    save_desktop_model_settings(
        kb_dir,
        provider="deepseek",
        model="deepseek-v4-pro",
        api_base_url="https://api.deepseek.com",
        api_key="test-key",
        max_concurrent_model_calls=1,
    )
    calls: list[str] = []
    repair_digests: list[str] = []

    class InvalidThenRepairedTransport:
        def __init__(self, *, model, bundle):
            del model, bundle

        def __call__(self, request, _timeout_seconds):
            calls.append(request.operation)
            if request.operation == "structured_output_repair":
                assert request.prompt_contract_digest is not None
                repair_digests.append(request.prompt_contract_digest)
                return "not-json" if len(repair_digests) == 1 else _valid_query_planning_response()
            return "not-json" if len(calls) < 5 else _valid_query_planning_response()

    monkeypatch.setattr(
        desktop_model_transport,
        "DesktopLiteLLMTransport",
        InvalidThenRepairedTransport,
    )
    gateway = desktop_model_transport.desktop_model_gateway_for(kb_dir)
    assert gateway is not None
    profile = gateway.execution_profile_for_operation("query_planning")
    DesktopModelCapabilityStore(kb_dir).mark_verified(profile)

    first = build_query_plan("What does Atlas use?", gateway, kb_dir=kb_dir)
    assert first.degradations == ("query_semantic_structure_unknown",)

    repaired = build_query_plan(
        "What does Atlas use?",
        gateway,
        kb_dir=kb_dir,
    )

    assert repaired.degradations == ()
    assert repair_digests[0] == repair_digests[1]
    store = DesktopModelOperationContractStore(kb_dir)
    assert (
        store.state(
            operation="query_planning",
            capability_identity=profile.capability_evidence_profile.identity,
            prompt_contract_digest=prompt_contract_for("query_planning").digest,
        ).status
        == "ready"
    )
    assert (
        store.state(
            operation="structured_output_repair",
            capability_identity=profile.capability_evidence_profile.identity,
            prompt_contract_digest=repair_digests[0],
        ).status
        == "ready"
    )

    normal = build_query_plan("What does Atlas use?", gateway, kb_dir=kb_dir)
    assert normal.degradations == ()
    assert calls == [
        "query_planning",
        "structured_output_repair",
        "query_planning",
        "structured_output_repair",
        "query_planning",
    ]


def test_confirmed_authentication_failure_invalidates_shared_analysis_role(
    tmp_path, monkeypatch
) -> None:
    kb_dir = tmp_path / "knowledge"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    save_desktop_model_settings(
        kb_dir,
        provider="deepseek",
        model="deepseek-v4-pro",
        api_base_url="https://api.deepseek.com",
        api_key="test-key",
        max_concurrent_model_calls=1,
    )

    class AuthenticationFailureTransport:
        def __init__(self, *, model, bundle):
            del model, bundle

        def __call__(self, _request, _timeout_seconds):
            raise DesktopModelTransportError("authentication")

    monkeypatch.setattr(
        desktop_model_transport,
        "DesktopLiteLLMTransport",
        AuthenticationFailureTransport,
    )
    gateway = desktop_model_transport.desktop_model_gateway_for(kb_dir)
    assert gateway is not None
    profile = gateway.execution_profile_for_operation("query_planning")
    store = DesktopModelCapabilityStore(kb_dir)
    store.mark_verified(profile)

    result = build_query_plan("What evidence is available?", gateway, kb_dir=kb_dir)

    assert result.degradations == ("query_planning_failed",)
    state = store.state(profile)
    operation_state = DesktopModelOperationContractStore(kb_dir).state(
        operation="query_planning",
        capability_identity=profile.capability_evidence_profile.identity,
        prompt_contract_digest=prompt_contract_for("query_planning").digest,
    )
    assert operation_state.status == "unverified"
    assert operation_state.failure_code is None
    assert state.status == "unchecked"
    assert state.failure_code == "model_authentication_failed"


def test_graph_failure_does_not_corroborate_shared_protocol_across_operations(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "openkb.importing.runner.start_graph_extraction", lambda *_args, **_kwargs: None
    )
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "corroboration.md"
    source.write_text("# Atlas\n\nAtlas uses a local gateway.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    document = DesktopTextImportService(kb_dir).import_text(source).document
    save_desktop_model_settings(
        kb_dir,
        provider="deepseek",
        model="deepseek-v4-pro",
        api_base_url="https://api.deepseek.com",
        api_key="test-key",
        max_concurrent_model_calls=1,
    )

    class EmptyResultTransport:
        def __init__(self, *, model, bundle):
            del model, bundle

        def __call__(self, _request, _timeout_seconds):
            return ""

    monkeypatch.setattr(desktop_model_transport, "DesktopLiteLLMTransport", EmptyResultTransport)
    gateway = desktop_model_transport.desktop_model_gateway_for(kb_dir)
    assert gateway is not None
    profile = gateway.execution_profile_for_operation("query_planning")
    store = DesktopModelCapabilityStore(kb_dir)
    store.mark_verified(profile)

    build_query_plan("What does Atlas use?", gateway, kb_dir=kb_dir)
    assert store.state(profile).status == "verified"

    assert not DesktopKnowledgeGraphService(kb_dir, model_gateway=gateway).extract_document(
        document.document_id
    )
    assert store.state(profile).status == "verified"
    retrieval_state = DesktopModelOperationContractStore(kb_dir).state(
        operation="query_planning",
        capability_identity=profile.capability_evidence_profile.identity,
        prompt_contract_digest=prompt_contract_for("query_planning").digest,
    )
    graph_state = DesktopModelOperationContractStore(kb_dir).state(
        operation="knowledge_relation_analysis",
        capability_identity=profile.capability_evidence_profile.identity,
        prompt_contract_digest=prompt_contract_for("knowledge_relation_analysis").digest,
    )
    assert retrieval_state.status == "unverified"
    assert retrieval_state.failure_stage is None
    assert retrieval_state.failure_signature is None
    assert graph_state.status == "unverified"
    assert graph_state.failure_signature is None


def test_graph_repair_failure_does_not_join_cross_pipeline_corroboration(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "openkb.importing.runner.start_graph_extraction",
        lambda *_args, **_kwargs: None,
    )
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "repair-corroboration.md"
    source.write_text("# Atlas\n\nAtlas uses a local gateway.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    document = DesktopTextImportService(kb_dir).import_text(source).document
    save_desktop_model_settings(
        kb_dir,
        provider="deepseek",
        model="deepseek-v4-pro",
        api_base_url="https://api.deepseek.com",
        api_key="test-key",
        max_concurrent_model_calls=1,
    )
    repair_digests: dict[str, str] = {}

    class InvalidPrimaryAndTerminalRepairTransport:
        def __init__(self, *, model, bundle):
            del model, bundle

        def __call__(self, request, _timeout_seconds):
            if request.operation == "structured_output_repair":
                assert request.parent_operation is not None
                assert request.prompt_contract_digest is not None
                repair_digests[request.parent_operation] = request.prompt_contract_digest
                return DesktopModelProviderResponse(
                    "",
                    observations=DesktopModelOutputObservations(
                        finish_reason="length",
                        reasoning_observed=True,
                        reasoning_chunk_count=1,
                        reasoning_character_count=42,
                        output_limit_reached=True,
                    ),
                )
            return "not-json"

    monkeypatch.setattr(
        desktop_model_transport,
        "DesktopLiteLLMTransport",
        InvalidPrimaryAndTerminalRepairTransport,
    )
    gateway = desktop_model_transport.desktop_model_gateway_for(kb_dir)
    assert gateway is not None
    profile = gateway.execution_profile_for_operation("query_planning")
    capability_store = DesktopModelCapabilityStore(kb_dir)
    capability_store.mark_verified(profile)

    first = build_query_plan("What does Atlas use?", gateway, kb_dir=kb_dir)
    assert first.degradations == ("query_planning_failed",)
    assert capability_store.state(profile).status == "verified"

    assert not DesktopKnowledgeGraphService(
        kb_dir,
        model_gateway=gateway,
    ).extract_document(document.document_id)

    assert capability_store.state(profile).status == "verified"
    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        repair_rows = connection.execute(
            """
            SELECT status, failure_stage, failure_signature, prompt_contract_digest
            FROM model_operation_contract_states
            WHERE operation = 'structured_output_repair'
                AND capability_identity = ?
            ORDER BY prompt_contract_digest
            """,
            (profile.capability_evidence_profile.identity,),
        ).fetchall()
    assert repair_rows == []
    assert set(repair_digests) == {"query_planning"}
    for parent_operation in ("query_planning", "knowledge_relation_analysis"):
        assert (
            DesktopModelOperationContractStore(kb_dir)
            .state(
                operation=parent_operation,
                capability_identity=profile.capability_evidence_profile.identity,
                prompt_contract_digest=prompt_contract_for(parent_operation).digest,
            )
            .failure_signature
            is None
        )


def test_unverified_retrieval_plan_uses_fallback_without_provider_call(
    tmp_path, monkeypatch
) -> None:
    kb_dir = tmp_path / "knowledge"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    save_desktop_model_settings(
        kb_dir,
        provider="deepseek",
        model="deepseek-v4-pro",
        api_base_url="https://api.deepseek.com",
        api_key="test-key",
        max_concurrent_model_calls=1,
    )
    calls: list[str] = []

    class FakeTransport:
        def __init__(self, *, model, bundle):
            del model, bundle

        def __call__(self, request, _timeout_seconds):
            calls.append(request.operation)
            return "{}"

    monkeypatch.setattr(desktop_model_transport, "DesktopLiteLLMTransport", FakeTransport)
    gateway = desktop_model_transport.desktop_model_gateway_for(kb_dir)
    assert gateway is not None

    result = build_query_plan("What evidence is available?", gateway)

    assert result.degradations == ("query_planning_unverified",)
    assert calls == []
