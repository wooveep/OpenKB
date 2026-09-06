"""Sanitized, local-only Model Usage Records."""

from __future__ import annotations

import json
import sqlite3
import zipfile

import pytest

from openkb import desktop_model_transport
from openkb.desktop_diagnostic_bundle import DesktopDiagnosticBundleService
from openkb.desktop_model_gateway import (
    DesktopModelCallError,
    DesktopModelOutputObservations,
    DesktopModelProviderResponse,
    DesktopModelRequest,
    DesktopProviderTokenUsage,
)
from openkb.desktop_model_settings import save_desktop_model_settings
from openkb.desktop_model_terminal import DesktopTerminalModelEvent
from openkb.desktop_model_usage import DesktopModelUsageStore
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime


def _create_kb(tmp_path):
    kb_dir = tmp_path / "kb"
    DesktopKnowledgeBaseRuntime().create(kb_dir, name="Usage KB")
    return kb_dir


def _request(**changes):
    values = {
        "operation": "knowledge_analysis_batch",
        "document_name": "secret-release-notes.docx",
        "content": "private source prompt payload",
        "model_role": "analysis",
        "model_name": "analysis-model",
        "job_id": "job-1",
        "stage_run_id": "stage-1",
        "batch_id": "batch-3",
        "prompt_contract_snapshot": {"instructions": "private system prompt"},
    }
    values.update(changes)
    return DesktopModelRequest(**values)


def _event(status, elapsed, *, attempt=1, failure_code=None):
    return DesktopTerminalModelEvent(
        call_id="call-1",
        attempt=attempt,
        status=status,
        elapsed_seconds=elapsed,
        failure_code=failure_code,
    )


def test_provider_tokens_timings_and_optional_user_pricing_are_persisted(tmp_path):
    kb_dir = _create_kb(tmp_path)
    store = DesktopModelUsageStore(kb_dir)
    request = _request()
    for event in (
        _event("queued", 0.0),
        _event("connecting", 2.0),
        _event("awaiting_model_result", 3.5),
        _event("model_output_activity", 8.0),
        _event("completed", 12.0),
    ):
        store.record_event(
            request=request,
            event=event,
            provider="custom",
            model="analysis-model",
        )
    store.record_result(
        request=request,
        call_id="call-1",
        attempt=1,
        content=DesktopModelProviderResponse(
            "private model output",
            usage=DesktopProviderTokenUsage(1_000, 200, 1_200),
            provider_request_id="provider-request-7",
        ),
        input_price_per_million=2.0,
        output_price_per_million=5.0,
    )

    record = store.records(job_id="job-1")[0]
    assert record["operation"] == "knowledge_analysis_batch"
    assert record["model_role"] == "analysis"
    assert record["attempt_id"] == "call-1:1"
    assert record["batch_id"] == "batch-3"
    assert record["queue_seconds"] == 2.0
    assert record["connect_seconds"] == 1.5
    assert record["first_output_seconds"] == 8.0
    assert record["total_seconds"] == 12.0
    assert record["token_usage_source"] == "provider_reported"
    assert record["input_tokens"] == 1_000
    assert record["output_tokens"] == 200
    assert record["total_cost"] == 0.003
    assert record["provider_request_id"] == "provider-request-7"
    assert "private" not in json.dumps(record)
    assert store.aggregate()["token_usage_source"] == "provider_reported"


def test_model_result_failure_persists_only_content_free_stream_observations(tmp_path):
    kb_dir = _create_kb(tmp_path)
    store = DesktopModelUsageStore(kb_dir)
    request = _request()
    event = DesktopTerminalModelEvent(
        call_id="call-result-failure",
        attempt=1,
        status="model_result_failure",
        elapsed_seconds=12.0,
        failure_code="reasoning_output_exhausted",
        finish_reason="length",
        reasoning_observed=True,
        final_content_observed=False,
        reasoning_chunk_count=3,
        final_chunk_count=0,
        reasoning_character_count=240,
        final_character_count=0,
    )

    store.record_event(
        request=request,
        event=event,
        provider="deepseek",
        model="deepseek-v4-pro",
    )

    record = store.records()[0]
    assert record["lifecycle_status"] == "model_result_failure"
    assert record["failure_code"] == "reasoning_output_exhausted"
    assert record["finish_reason"] == "length"
    assert record["reasoning_observed"] is True
    assert record["final_content_observed"] is False
    assert record["reasoning_chunk_count"] == 3
    assert record["final_chunk_count"] == 0
    assert record["reasoning_character_count"] == 240
    assert record["final_character_count"] == 0
    assert "reasoning" not in json.dumps(record).lower().replace(
        "reasoning_output_exhausted", ""
    ).replace("reasoning_observed", "").replace("reasoning_chunk_count", "").replace(
        "reasoning_character_count", ""
    )


def test_failed_provider_result_retains_reported_tokens_without_result_content(
    tmp_path, monkeypatch
):
    kb_dir = _create_kb(tmp_path)
    save_desktop_model_settings(
        kb_dir,
        provider="deepseek",
        model="deepseek-v4-pro",
        api_base_url="https://api.deepseek.com",
        api_key="secret-key",
        max_concurrent_model_calls=1,
    )

    class ReasoningOnlyTransport:
        def __init__(self, *, model, bundle):
            del model, bundle

        def __call__(self, _request, _connect_timeout):
            return DesktopModelProviderResponse(
                "",
                usage=DesktopProviderTokenUsage(10, 90, 100),
                provider_request_id="request-safe-id",
                observations=DesktopModelOutputObservations(
                    finish_reason="length",
                    reasoning_observed=True,
                    reasoning_chunk_count=2,
                    reasoning_character_count=180,
                    output_limit_reached=True,
                ),
            )

    monkeypatch.setattr(
        desktop_model_transport,
        "DesktopLiteLLMTransport",
        ReasoningOnlyTransport,
    )
    gateway = desktop_model_transport.desktop_model_gateway_for(kb_dir)
    assert gateway is not None

    with pytest.raises(DesktopModelCallError):
        gateway.analyze(
            DesktopModelRequest(
                "query_planning",
                "question",
                "private retrieval question",
                job_id="job-safe-failure",
            ),
            on_event=lambda _event: None,
        )

    record = DesktopModelUsageStore(kb_dir).records()[0]
    assert record["input_tokens"] == 10
    assert record["output_tokens"] == 90
    assert record["total_tokens"] == 100
    assert record["token_usage_source"] == "provider_reported"
    assert record["provider_request_id"] == "request-safe-id"
    assert "private retrieval question" not in json.dumps(record)


def test_production_role_gateway_records_analysis_answer_and_default_calls(tmp_path, monkeypatch):
    kb_dir = _create_kb(tmp_path)
    save_desktop_model_settings(
        kb_dir,
        provider="deepseek",
        model="default-model",
        analysis_model="analysis-model",
        answer_model="answer-model",
        api_base_url="https://api.deepseek.com",
        api_key="secret-key",
        max_concurrent_model_calls=2,
        initial_timeout_seconds=1,
    )

    class FakeTransport:
        def __init__(self, *, model, bundle):
            self.model = str(model)

        def __call__(self, _request, _connect_timeout):
            return DesktopModelProviderResponse(
                "complete",
                usage=DesktopProviderTokenUsage(3, 2, 5),
                provider_request_id=f"request-{self.model}",
            )

    monkeypatch.setattr(desktop_model_transport, "DesktopLiteLLMTransport", FakeTransport)
    gateway = desktop_model_transport.desktop_model_gateway_for(kb_dir)
    assert gateway is not None
    for operation in ("knowledge_analysis", "grounded_answer", "connection_test"):
        gateway.analyze(
            DesktopModelRequest(operation, "private.docx", "private source"),
            on_event=lambda _event: None,
        )

    records = DesktopModelUsageStore(kb_dir).records()
    assert [(item["operation"], item["model_role"], item["model"]) for item in records] == [
        ("knowledge_analysis", "analysis", "analysis-model"),
        ("grounded_answer", "answer", "answer-model"),
        ("connection_test", "default", "default-model"),
    ]
    assert all(item["token_usage_source"] == "provider_reported" for item in records)


def test_missing_provider_usage_is_visibly_estimated_and_cost_stays_hidden(tmp_path):
    kb_dir = _create_kb(tmp_path)
    store = DesktopModelUsageStore(kb_dir)
    request = _request(content="分析版本说明" * 20)
    store.record_event(
        request=request,
        event=_event("completed", 4.0),
        provider="custom",
        model="analysis-model",
    )
    store.record_result(
        request=request,
        call_id="call-1",
        attempt=1,
        content="valid but private response",
    )

    record = store.records()[0]
    assert record["token_usage_source"] == "estimated"
    assert record["input_tokens"] > 0
    assert record["output_tokens"] > 0
    assert record["input_cost"] is None
    assert record["output_cost"] is None
    assert record["total_cost"] is None
    assert store.aggregate()["token_usage_source"] == "estimated"


def test_retry_aggregation_and_long_wait_threshold_use_completed_local_history(tmp_path):
    kb_dir = _create_kb(tmp_path)
    store = DesktopModelUsageStore(kb_dir)
    request = _request()
    store.record_event(
        request=request,
        event=_event("provider_failure", 10.0, failure_code="model_rate_limited"),
        provider="custom",
        model="analysis-model",
    )
    store.record_event(
        request=request,
        event=_event("retrying", 11.0, failure_code="model_rate_limited"),
        provider="custom",
        model="analysis-model",
    )
    store.record_event(
        request=request,
        event=_event("queued", 11.0, attempt=2),
        provider="custom",
        model="analysis-model",
    )
    store.record_event(
        request=request,
        event=_event("completed", 240.0, attempt=2),
        provider="custom",
        model="analysis-model",
    )
    store.record_result(
        request=request,
        call_id="call-1",
        attempt=2,
        content="ok",
    )

    aggregate = store.aggregate()
    assert aggregate["call_count"] == 1
    assert aggregate["attempt_count"] == 2
    assert aggregate["failure_count"] == 1
    assert store.long_wait_threshold_seconds("analysis", "analysis-model") == 480.0
    assert store.long_wait_threshold_seconds("answer", "unused-model") == 300.0


def test_explicit_diagnostic_export_contains_only_sanitized_usage(tmp_path):
    kb_dir = _create_kb(tmp_path)
    save_desktop_model_settings(
        kb_dir,
        provider="custom",
        model="default-model",
        api_base_url="https://models.example.test/v1",
        api_key="credential-secret",
        max_concurrent_model_calls=2,
        initial_timeout_seconds=1,
    )
    request = _request(
        content="source-secret",
        prompt_contract_snapshot={"instructions": "prompt-secret"},
    )
    store = DesktopModelUsageStore(kb_dir)
    store.record_event(
        request=request,
        event=DesktopTerminalModelEvent(
            call_id="call-1",
            attempt=1,
            status="model_result_failure",
            elapsed_seconds=5.0,
            failure_code="reasoning_only_result",
            finish_reason="stop",
            reasoning_observed=True,
            final_content_observed=False,
            reasoning_chunk_count=2,
            final_chunk_count=0,
            reasoning_character_count=180,
            final_character_count=0,
        ),
        provider="custom",
        model="analysis-model",
    )
    store.record_result(
        request=request,
        call_id="call-1",
        attempt=1,
        content="output-secret reasoning-secret",
    )

    destination = tmp_path / "diagnostics.zip"
    assert not destination.exists()
    DesktopDiagnosticBundleService(kb_dir).export(destination)
    with zipfile.ZipFile(destination) as archive:
        usage = archive.read("model-usage.json").decode("utf-8")
        all_content = "\n".join(archive.read(name).decode("utf-8") for name in archive.namelist())

    assert '"token_usage_source": "estimated"' in usage
    assert '"finish_reason": "stop"' in usage
    assert '"reasoning_observed": 1' in usage
    assert '"final_content_observed": 0' in usage
    assert '"reasoning_chunk_count": 2' in usage
    assert '"reasoning_character_count": 180' in usage
    for secret in (
        "source-secret",
        "prompt-secret",
        "output-secret",
        "reasoning-secret",
        "credential-secret",
    ):
        assert secret not in all_content
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        stored = json.dumps(connection.execute("SELECT * FROM model_usage_records").fetchall())
    assert "secret" not in stored
