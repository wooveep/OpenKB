"""Cancellable, content-free checks for each distinct configured model role."""

from __future__ import annotations

import json
from dataclasses import replace

from openkb.desktop_model_gateway import DesktopModelRequest
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
    for role, model in (
        ("default", settings.model),
        ("analysis", settings.analysis_model_name),
        ("answer", settings.answer_model_name),
    ):
        if model not in roles_by_model:
            roles_by_model[model] = set()
            order.append(model)
        roles_by_model[model].add(role)
    checks: list[tuple[str, str, DesktopModelSettings]] = []
    for model in order:
        roles = roles_by_model[model]
        operation = (
            "model_capability_analysis_streaming"
            if {"analysis", "answer"}.issubset(roles)
            else "model_capability_analysis"
            if "analysis" in roles
            else "model_capability_answer"
            if "answer" in roles
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
    model: str,
    operation: str,
) -> DesktopModelRequest:
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
