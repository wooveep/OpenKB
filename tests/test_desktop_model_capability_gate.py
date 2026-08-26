"""Explicit exact-profile capability checks gate paid structured Analysis."""

from __future__ import annotations

import json

import pytest

from openkb import desktop_model_transport
from openkb.desktop_import import DesktopImportError, DesktopTextImportService
from openkb.desktop_knowledge_analysis import KNOWLEDGE_ANALYSIS_SCHEMA_VERSION
from openkb.desktop_model_capability_check import capability_check_request
from openkb.desktop_model_capability_store import DesktopModelCapabilityStore
from openkb.desktop_model_execution_profile import build_analysis_execution_profile
from openkb.desktop_model_gateway import (
    DesktopModelOutputObservations,
    DesktopModelProviderResponse,
    DesktopProviderTokenUsage,
)
from openkb.desktop_model_settings import (
    save_desktop_model_settings,
    validate_desktop_model_settings,
)
from openkb.desktop_model_usage import DesktopModelUsageStore
from openkb.desktop_retrieval_planning import build_retrieval_plan
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime


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


def test_analysis_capability_check_uses_the_exact_pinned_protocol() -> None:
    settings = _settings(reasoning="high")
    profile = _profile(reasoning="high")

    request = capability_check_request(settings, profile=profile)

    assert request.operation == "model_capability_analysis"
    assert request.provider_adapter == "deepseek"
    assert request.provider_adapter_version == "deepseek.v1"
    assert request.structured_output_mode == "json_object"
    assert request.reasoning_effort == "high"
    assert request.supports_streaming is True
    assert request.response_schema is not None
    assert request.response_example == {"status": "ok"}
    assert request.generation_parameters == {
        "temperature": 0,
        "max_tokens": profile.provider_output_ceiling_tokens,
    }
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
            if request.operation == "knowledge_analysis":
                return json.dumps(
                    {
                        "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
                        "analysis_scope": "document",
                        "document_description": "Verified Analysis.",
                        "concepts": [],
                        "entities": [],
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

    profile = gateway.execution_profile_for_operation("knowledge_analysis")
    DesktopModelCapabilityStore(kb_dir).mark_verified(profile)
    result = importer.resume_text(task["job"]["job_id"])

    assert result.document.availability == "available"
    assert "knowledge_analysis" in calls


def test_protocol_result_failure_returns_verified_profile_to_unchecked(
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
    profile = gateway.execution_profile_for_operation("knowledge_analysis")
    capability_store = DesktopModelCapabilityStore(kb_dir)
    capability_store.mark_verified(profile)

    with pytest.raises(DesktopImportError, match="exhausted its output limit"):
        DesktopTextImportService(kb_dir, model_gateway=gateway).import_text(source)

    state = capability_store.state(profile)
    assert state.status == "unchecked"
    assert state.failure_code == "reasoning_output_exhausted"


def test_invalid_structured_import_returns_verified_profile_to_unchecked(
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
    profile = gateway.execution_profile_for_operation("knowledge_analysis")
    capability_store = DesktopModelCapabilityStore(kb_dir)
    capability_store.mark_verified(profile)

    service = DesktopTextImportService(kb_dir, model_gateway=gateway)
    with pytest.raises(DesktopImportError) as captured:
        service.import_text(source)

    assert captured.value.code == "model_response_invalid"
    assert calls == ["knowledge_analysis", "structured_output_repair"]
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
    assert state.status == "unchecked"
    assert state.failure_code == "model_response_invalid"


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
def test_retrieval_plan_result_failure_invalidates_current_profile(
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
    profile = gateway.execution_profile_for_operation("retrieval_plan")
    capability_store = DesktopModelCapabilityStore(kb_dir)
    capability_store.mark_verified(profile)

    result = build_retrieval_plan("What evidence is available?", gateway)

    assert result.degradations == ("retrieval_plan_fallback",)
    usage = DesktopModelUsageStore(kb_dir).records()
    if failure_code == "model_response_invalid":
        assert [record["operation"] for record in usage] == [
            "retrieval_plan",
            "structured_output_repair",
        ]
        failed = [record for record in usage if record["failure_code"] is not None]
        assert [record["operation"] for record in failed] == [
            "retrieval_plan",
            "structured_output_repair",
        ]
        assert {record["lifecycle_status"] for record in failed} == {
            "model_result_failure"
        }
        assert {record["failure_code"] for record in failed} == {
            "model_response_invalid"
        }
    state = capability_store.state(profile)
    assert state.status == "unchecked"
    assert state.failure_code == failure_code


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

    result = build_retrieval_plan("What evidence is available?", gateway)

    assert result.degradations == ("retrieval_plan_unverified",)
    assert calls == []
