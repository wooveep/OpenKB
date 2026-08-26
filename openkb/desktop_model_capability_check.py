"""Cancellable, content-free checks for each distinct configured model role."""

from __future__ import annotations

import json
from dataclasses import replace

from openkb.desktop_model_execution_profile import DesktopModelExecutionProfile
from openkb.desktop_model_gateway import DesktopModelRequest
from openkb.desktop_model_provider_adapter import provider_adapter_for
from openkb.desktop_model_settings import DesktopModelSettings

CAPABILITY_CHECK_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"status": {"const": "ok"}},
    "required": ["status"],
    "additionalProperties": False,
}


def selected_model_checks(
    settings: DesktopModelSettings,
) -> tuple[tuple[str, str, DesktopModelSettings], ...]:
    """Return one strongest applicable check for every distinct selected model."""
    roles_by_model: dict[str, set[str]] = {}
    order: list[str] = []
    roles = [("default", settings.model), ("answer", settings.answer_model_name)]
    if provider_adapter_for(settings.provider).supports_structured_analysis:
        roles.insert(1, ("analysis", settings.analysis_model_name))
    for role, model in roles:
        if model not in roles_by_model:
            roles_by_model[model] = set()
            order.append(model)
        roles_by_model[model].add(role)
    checks: list[tuple[str, str, DesktopModelSettings]] = []
    for model in order:
        selected_roles = roles_by_model[model]
        operation = (
            "model_capability_analysis_streaming"
            if {"analysis", "answer"}.issubset(selected_roles)
            else "model_capability_analysis"
            if "analysis" in selected_roles
            else "model_capability_answer"
            if "answer" in selected_roles
            else "model_capability_default"
        )
        checks.append(
            (
                model,
                operation,
                replace(settings, model=model, analysis_model=None, answer_model=None),
            )
        )
    return tuple(checks)


def capability_check_request(
    settings: DesktopModelSettings,
    *,
    profile: DesktopModelExecutionProfile | None = None,
    model: str | None = None,
    operation: str | None = None,
) -> DesktopModelRequest:
    if profile is not None:
        content = (
            "Return this exact JSON object and no other text.\n\n"
            'EXAMPLE JSON OUTPUT:\n{"status":"ok"}'
        )
        return DesktopModelRequest(
            operation="model_capability_analysis",
            document_name="OpenKB model capability check",
            content=content,
            model_name=profile.model,
            context_capacity=profile.context_capacity,
            document_input_capacity=profile.document_input_capacity,
            reasoning_effort=profile.reasoning_effort,
            provider_adapter=profile.adapter_identity,
            provider_adapter_version=profile.adapter_version,
            structured_output_mode=profile.structured_output_mode,
            response_schema=CAPABILITY_CHECK_SCHEMA,
            response_example={"status": "ok"},
            response_schema_name="openkb_model_capability_check",
            generation_parameters={
                "temperature": 0,
                "max_tokens": profile.provider_output_ceiling_tokens,
            },
            prompt_contract_digest=profile.prompt_contract_digest,
            prompt_contract_version="openkb.model-capability-check.v1",
            prompt_contract_snapshot={
                "instructions": content,
                "output_schema": CAPABILITY_CHECK_SCHEMA,
                "output_example": {"status": "ok"},
            },
            supports_streaming=profile.streaming,
        )
    if model is None or operation is None:
        raise ValueError("A legacy capability check requires model and operation.")
    analysis = operation in {
        "model_capability_analysis",
        "model_capability_analysis_streaming",
    }
    capability = settings.capability_for_role("default")
    return DesktopModelRequest(
        operation=operation,
        document_name="OpenKB model capability check",
        content=(
            'Return exactly this JSON object: {"status":"ok"}'
            if analysis
            else "Reply with the single word OK."
        ),
        model_name=model,
        context_capacity=capability.context_capacity,
        document_input_capacity=capability.document_input_capacity,
        response_schema=(
            CAPABILITY_CHECK_SCHEMA if analysis and capability.supports_native_json_schema else None
        ),
        response_schema_name="openkb_model_capability_check" if analysis else None,
        supports_streaming=(
            True
            if operation == "model_capability_analysis_streaming"
            else capability.supports_streaming
        ),
    )


def validate_capability_result(operation: str, content: str) -> None:
    if operation not in {
        "model_capability_analysis",
        "model_capability_analysis_streaming",
    }:
        if not content.strip():
            raise ValueError("The Answer model did not stream text.")
        return
    normalized = content.strip()
    if normalized.startswith("```") and normalized.endswith("```"):
        lines = normalized.splitlines()
        normalized = "\n".join(lines[1:-1]).strip()
    parsed = json.loads(normalized)
    if not isinstance(parsed, dict) or parsed != {"status": "ok"}:
        raise ValueError("The Analysis model did not return schema-valid structured output.")
