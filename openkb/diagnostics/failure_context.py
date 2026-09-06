"""Stable Failure Attribution helpers for Desktop diagnostic boundaries."""

from __future__ import annotations

import uuid
from typing import Mapping

_PARSER_FAILURE_CODES = frozenset(
    {
        "empty_text_document",
        "encrypted_pdf_document",
        "enhanced_image_parse_failed",
        "enhanced_pdf_parse_failed",
        "enhanced_pdf_parser_unavailable",
        "invalid_docx_document",
        "invalid_pdf_document",
        "invalid_pptx_document",
        "invalid_text_document",
        "invalid_xls_document",
        "invalid_xlsx_document",
        "legacy_office_parse_failed",
        "legacy_office_runtime_unavailable",
        "parser_mode_invalid",
        "parser_runtime_unavailable",
        "unsupported_import_format",
    }
)


def ensure_failure_event_id(error: BaseException) -> str:
    """Attach one correlation identity that every propagation boundary reuses."""
    existing = getattr(error, "failure_event_id", None)
    if isinstance(existing, str) and existing:
        return existing
    failure_event_id = uuid.uuid4().hex
    try:
        error.failure_event_id = failure_event_id  # type: ignore[attr-defined]
    except (AttributeError, TypeError):
        pass
    return failure_event_id


def failure_kind_for_code(error_code: str) -> str:
    if error_code in {"model_network_transient", "network_failure"}:
        return "network_failure"
    if error_code in {
        "model_provider_failure",
        "model_rate_limited",
        "model_server_error",
        "model_authentication_failed",
        "model_input_invalid",
        "model_service_unavailable",
        "provider_failure",
    }:
        return "provider_failure"
    if error_code in {
        "empty_final_result",
        "reasoning_only_result",
        "reasoning_output_exhausted",
        "model_response_invalid",
    }:
        return "model_result_failure"
    if error_code in _PARSER_FAILURE_CODES or "parser" in error_code or "document_ir" in error_code:
        return "parser_failure"
    if "configuration" in error_code or "config" in error_code:
        return "configuration_failure"
    return "application_failure"


def next_action_for_code(error_code: str) -> str:
    if error_code in {"model_network_transient", "network_failure"}:
        return "check_network_then_retry"
    if error_code == "model_authentication_failed":
        return "check_model_credentials"
    if error_code == "model_input_invalid":
        return "check_model_input_and_settings"
    if error_code in {
        "model_rate_limited",
        "model_server_error",
        "model_provider_failure",
        "model_service_unavailable",
        "provider_failure",
    }:
        return "retry_or_check_provider"
    if error_code in {
        "empty_final_result",
        "reasoning_only_result",
        "reasoning_output_exhausted",
        "model_response_invalid",
    }:
        return "run_model_capability_check"
    if error_code == "import_source_not_found":
        return "check_source_file"
    if "configuration" in error_code or "config" in error_code:
        return "check_configuration"
    if error_code in _PARSER_FAILURE_CODES or "parser" in error_code or "document_ir" in error_code:
        return "inspect_source_or_convert_format"
    return "inspect_failure_context"


def failure_context_fields(
    *,
    error: BaseException,
    error_code: str,
    component: str,
    stage: str,
    phase: str,
    outcome: str,
    retryable: bool,
    attempt: int = 1,
    elapsed_ms: int | None = None,
    correlations: Mapping[str, object] | None = None,
    observations: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build the required support-safe terminal Failure Context."""
    fields: dict[str, object] = {
        "failure_event_id": ensure_failure_event_id(error),
        "failure_kind": failure_kind_for_code(error_code),
        "error_code": error_code,
        "stage": stage,
        "phase": phase,
        "outcome": outcome,
        "retryable": retryable,
        "attempt": attempt,
        "next_action": next_action_for_code(error_code),
    }
    if elapsed_ms is not None:
        fields["elapsed_ms"] = max(0, elapsed_ms)
    for key, value in {**(correlations or {}), **(observations or {})}.items():
        if key not in fields:
            fields[key] = value
    return fields
