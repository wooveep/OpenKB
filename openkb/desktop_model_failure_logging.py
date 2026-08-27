"""Failure Owner diagnostics for terminal provider and model-result failures."""

from __future__ import annotations

import json
import logging
import traceback
from typing import Protocol

from openkb.desktop_failure_context import failure_context_fields
from openkb.desktop_logging import log_event
from openkb.desktop_model_gateway import (
    DesktopModelFailure,
    DesktopModelOutputObservations,
    DesktopModelRequest,
    DesktopModelResult,
    DesktopProviderTokenUsage,
)
from openkb.desktop_sensitive_trace import (
    record_sensitive_trace_failure,
    sensitive_trace_component_enabled,
)

logger = logging.getLogger(__name__)


class TerminalFailureContext(Protocol):
    @property
    def call_id(self) -> str: ...

    @property
    def attempt(self) -> int: ...

    @property
    def provider_name(self) -> str: ...

    @property
    def model_name(self) -> str: ...


def _observations(
    observations: DesktopModelOutputObservations | None,
    usage: DesktopProviderTokenUsage | None,
) -> dict[str, object]:
    values: dict[str, object] = {}
    if observations is not None:
        values.update(
            {
                "finish_reason": observations.finish_reason,
                "reasoning_observed": observations.reasoning_observed,
                "final_content_observed": observations.final_content_observed,
                "reasoning_chunk_count": observations.reasoning_chunk_count,
                "final_chunk_count": observations.final_chunk_count,
                "reasoning_character_count": observations.reasoning_character_count,
                "final_character_count": observations.final_character_count,
                "output_limit_reached": observations.output_limit_reached,
            }
        )
    if usage is not None:
        values.update(
            {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "total_tokens": usage.total_tokens,
            }
        )
    return values


def sensitive_model_request_payload(transport: object, request: DesktopModelRequest) -> str:
    try:
        provider_payload = getattr(transport, "sensitive_request_payload", None)
    except Exception:
        provider_payload = None
    if callable(provider_payload):
        try:
            return str(provider_payload(request))
        except Exception:
            pass
    try:
        return json.dumps(
            {
                "operation": request.operation,
                "document_name": request.document_name,
                "content": request.content,
                "model_role": request.model_role,
                "model_name": request.model_name,
                "reasoning_effort": request.reasoning_effort,
                "provider_adapter": request.provider_adapter,
                "response_schema": request.response_schema,
                "generation_parameters": request.generation_parameters,
                "prompt_contract_snapshot": request.prompt_contract_snapshot,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=repr,
        )
    except Exception:
        return "<provider request serialization unavailable>"


def own_terminal_model_failure(
    transport: object,
    request: DesktopModelRequest,
    error: Exception,
    failure: DesktopModelFailure,
    context: TerminalFailureContext,
    elapsed_seconds: float,
    response: object | None,
    retry_after_seconds: float | None,
) -> dict[str, object]:
    """Emit one canonical safe terminal event and optional raw failure capture."""
    fields = failure_context_fields(
        error=error,
        error_code=failure.code,
        component="model",
        stage=request.operation,
        phase=(
            "result_validation"
            if failure.code
            in {
                "empty_final_result",
                "reasoning_only_result",
                "reasoning_output_exhausted",
                "model_response_invalid",
            }
            else "provider_request"
        ),
        outcome="failed",
        retryable=failure.retryable,
        attempt=context.attempt,
        elapsed_ms=round(max(0.0, elapsed_seconds) * 1000),
        correlations={
            "job_id": request.job_id,
            "stage_run_id": request.stage_run_id,
            "batch_id": request.batch_id,
            "call_id": context.call_id,
            "provider_request_id": getattr(response, "provider_request_id", None),
        },
        observations={
            "operation": request.operation,
            "provider": context.provider_name,
            "model": context.model_name,
            "adapter": request.provider_adapter,
            "model_role": request.model_role,
            "lane": request.execution_lane,
            "retry_after_seconds": retry_after_seconds,
            "provider_error_code": getattr(error, "category", None),
            "error_type": getattr(error, "diagnostic_type", None) or type(error).__name__,
            **_observations(
                getattr(error, "observations", None),
                getattr(response, "usage", None),
            ),
        },
    )
    failure_event_id = str(fields["failure_event_id"])
    log_event(
        logger,
        logging.WARNING,
        "model_call_failed",
        "A terminal model call failed.",
        component="model",
        fields=fields,
        terminal=True,
    )

    if sensitive_trace_component_enabled("model"):
        try:
            payloads: dict[str, str | bytes] = {
                "provider-request": sensitive_model_request_payload(transport, request),
                "exception-stack": "".join(
                    traceback.format_exception(type(error), error, error.__traceback__)
                ),
            }
            if response is not None:
                payloads["assembled-response"] = str(response)
                reasoning = getattr(response, "sensitive_reasoning_content", "")
                if isinstance(reasoning, str) and reasoning:
                    payloads["raw-reasoning"] = reasoning
            sensitive_detail = getattr(error, "sensitive_detail", None)
            if isinstance(sensitive_detail, str) and sensitive_detail:
                payloads["provider-error"] = sensitive_detail
            record_sensitive_trace_failure(
                "model_call_failed",
                metadata={
                    "failure_event_id": failure_event_id,
                    "operation": request.operation,
                    "document_name": request.document_name,
                    "job_id": request.job_id,
                    "stage_run_id": request.stage_run_id,
                    "batch_id": request.batch_id,
                    "call_id": context.call_id,
                    "attempt": context.attempt,
                    "provider": context.provider_name,
                    "model": context.model_name,
                    "adapter": request.provider_adapter,
                    "failure_code": failure.code,
                },
                payloads=payloads,
            )
        except Exception:
            pass
    return fields


def result_diagnostic_context(
    request: DesktopModelRequest,
    *,
    provider: str,
    model: str,
) -> dict[str, object]:
    return {
        "operation": request.operation,
        "provider": provider,
        "model": model,
        "adapter": request.provider_adapter,
        "model_role": request.model_role,
        "lane": request.execution_lane,
        "job_id": request.job_id,
        "stage_run_id": request.stage_run_id,
        "batch_id": request.batch_id,
        "schema_name": request.response_schema_name,
    }


def own_capability_model_result_failure(
    *,
    request: DesktopModelRequest,
    result: DesktopModelResult,
    error: Exception,
) -> str:
    """Own a successful provider response rejected by the capability validator."""
    context = result.diagnostic_context
    fields = failure_context_fields(
        error=error,
        error_code="model_response_invalid",
        component="model",
        stage=request.operation,
        phase="capability_validation",
        outcome="failed",
        retryable=False,
        attempt=result.attempt_count,
        correlations={
            "job_id": context.get("job_id"),
            "stage_run_id": context.get("stage_run_id"),
            "batch_id": context.get("batch_id"),
            "call_id": result.call_id,
            "provider_request_id": result.provider_request_id,
        },
        observations={
            **context,
            "repair_attempted": False,
            "validation_error_code": type(error).__name__,
            **_observations(result.observations, result.usage),
        },
    )
    failure_event_id = str(fields["failure_event_id"])
    log_event(
        logger,
        logging.WARNING,
        "model_result_validation_failed",
        "A model capability result failed local validation.",
        component="model",
        fields=fields,
        terminal=True,
    )
    if sensitive_trace_component_enabled("model"):
        try:
            payloads = {
                "capability-request-content": request.content,
                "model-response": result.content,
                "validation-error": "".join(
                    traceback.format_exception(type(error), error, error.__traceback__)
                ),
            }
            if result.sensitive_request_payload is not None:
                payloads["provider-request"] = result.sensitive_request_payload
            if result.sensitive_reasoning_content:
                payloads["raw-reasoning"] = result.sensitive_reasoning_content
            record_sensitive_trace_failure(
                "model_result_validation_failed",
                metadata={
                    "failure_event_id": failure_event_id,
                    "operation": request.operation,
                    "document_name": request.document_name,
                    "call_id": result.call_id,
                    **context,
                },
                payloads=payloads,
            )
        except Exception:
            pass
    return failure_event_id


def own_structured_model_failure(
    *,
    operation: str,
    document_name: str,
    source_material: str,
    initial: DesktopModelResult,
    repaired: DesktopModelResult,
    first_error: Exception,
    second_error: Exception,
) -> str:
    """Own terminal local validation only after the single repair also fails."""
    context = repaired.diagnostic_context
    fields = failure_context_fields(
        error=second_error,
        error_code="model_response_invalid",
        component="model",
        stage=operation,
        phase="local_schema_validation",
        outcome="failed",
        retryable=False,
        attempt=initial.attempt_count + repaired.attempt_count,
        correlations={
            "job_id": context.get("job_id"),
            "stage_run_id": context.get("stage_run_id"),
            "batch_id": context.get("batch_id"),
            "call_id": repaired.call_id,
            "provider_request_id": repaired.provider_request_id,
        },
        observations={
            **context,
            "repair_attempted": True,
            "validation_error_code": type(second_error).__name__,
            **_observations(repaired.observations, repaired.usage),
        },
    )
    failure_event_id = str(fields["failure_event_id"])
    log_event(
        logger,
        logging.WARNING,
        "model_result_validation_failed",
        "A model result and its single repair both failed local validation.",
        component="model",
        fields=fields,
        terminal=True,
    )
    if sensitive_trace_component_enabled("model"):
        try:
            payloads = {
                "source-material": source_material,
                "initial-response": initial.content,
                "repaired-response": repaired.content,
                "initial-validation-error": "".join(
                    traceback.format_exception(
                        type(first_error), first_error, first_error.__traceback__
                    )
                ),
                "final-validation-error": "".join(
                    traceback.format_exception(
                        type(second_error), second_error, second_error.__traceback__
                    )
                ),
            }
            if initial.sensitive_request_payload is not None:
                payloads["initial-provider-request"] = initial.sensitive_request_payload
            if repaired.sensitive_request_payload is not None:
                payloads["repair-provider-request"] = repaired.sensitive_request_payload
            if initial.sensitive_reasoning_content:
                payloads["initial-raw-reasoning"] = initial.sensitive_reasoning_content
            if repaired.sensitive_reasoning_content:
                payloads["repair-raw-reasoning"] = repaired.sensitive_reasoning_content
            record_sensitive_trace_failure(
                "model_result_validation_failed",
                metadata={
                    "failure_event_id": failure_event_id,
                    "operation": operation,
                    "document_name": document_name,
                    "initial_call_id": initial.call_id,
                    "final_call_id": repaired.call_id,
                    **context,
                },
                payloads=payloads,
            )
        except Exception:
            pass
    return failure_event_id
