"""Cancellable, content-free checks for each distinct configured model role."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Literal

from openkb.models.capability_store import DesktopCapabilityEvidenceProfile
from openkb.models.execution_profile import (
    ANALYSIS_CAPABILITY_INSTRUCTIONS,
    ANALYSIS_CAPABILITY_VERSION,
    ANSWER_CAPABILITY_SYSTEM_PROMPT,
    ANSWER_CAPABILITY_USER_PROMPT,
    DesktopAnalysisCapabilityProfile,
    DesktopAnswerCapabilityProfile,
    DesktopModelExecutionProfile,
)
from openkb.models.gateway import DesktopModelRequest
from openkb.models.settings import DesktopModelSettings

ModelCapabilityRole = Literal["default", "analysis", "answer"]

CAPABILITY_CHECK_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"status": {"const": "ok"}},
    "required": ["status"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class DesktopModelCapabilityCheckPlan:
    """One role-specific provider call and the exact evidence identity it can verify."""

    role: ModelCapabilityRole
    model: str
    evidence_profile: DesktopCapabilityEvidenceProfile | None
    settings: DesktopModelSettings
    request: DesktopModelRequest


def model_capability_check_plans(
    settings: DesktopModelSettings,
    *,
    analysis_profile: DesktopModelExecutionProfile | None,
    answer_profile: DesktopAnswerCapabilityProfile | None,
    include_default: bool = True,
) -> tuple[DesktopModelCapabilityCheckPlan, ...]:
    """Build the single ordered check plan used by the Model Configuration route."""
    checks: list[DesktopModelCapabilityCheckPlan] = []
    if include_default and settings.model != settings.answer_model_name:
        checks.append(
            DesktopModelCapabilityCheckPlan(
                role="default",
                model=settings.model,
                evidence_profile=None,
                settings=replace(settings, analysis_model=None, answer_model=None),
                request=capability_check_request(
                    settings,
                    model=settings.model,
                    operation="model_capability_default",
                ),
            )
        )
    if analysis_profile is not None:
        shared_analysis_profile = analysis_profile.capability_evidence_profile
        checks.append(
            DesktopModelCapabilityCheckPlan(
                role="analysis",
                model=analysis_profile.model,
                evidence_profile=shared_analysis_profile,
                settings=replace(settings, model=analysis_profile.model),
                request=capability_check_request(settings, profile=analysis_profile),
            )
        )
    if answer_profile is not None:
        checks.append(
            DesktopModelCapabilityCheckPlan(
                role="answer",
                model=answer_profile.model,
                evidence_profile=answer_profile,
                settings=replace(
                    settings,
                    model=answer_profile.model,
                    analysis_model=None,
                    answer_model=None,
                ),
                request=answer_capability_check_request(settings, profile=answer_profile),
            )
        )
    return tuple(checks)


def capability_check_request(
    settings: DesktopModelSettings,
    *,
    profile: DesktopModelExecutionProfile | DesktopAnalysisCapabilityProfile | None = None,
    model: str | None = None,
    operation: str | None = None,
) -> DesktopModelRequest:
    if profile is not None:
        shared_profile = (
            profile.capability_evidence_profile
            if isinstance(profile, DesktopModelExecutionProfile)
            else profile
        )
        content = ANALYSIS_CAPABILITY_INSTRUCTIONS
        return DesktopModelRequest(
            operation="model_capability_analysis",
            document_name="OpenKB model capability check",
            content=content,
            model_name=shared_profile.model,
            context_capacity=shared_profile.context_capacity,
            document_input_capacity=shared_profile.document_input_capacity,
            reasoning_effort=shared_profile.reasoning_effort,
            provider_adapter=shared_profile.adapter_identity,
            provider_adapter_version=shared_profile.adapter_version,
            structured_output_mode=shared_profile.structured_output_mode,
            response_schema=CAPABILITY_CHECK_SCHEMA,
            local_validation_required=True,
            response_example={"status": "ok"},
            response_schema_name="openkb_model_capability_check",
            generation_parameters={
                "temperature": 0,
                "max_tokens": shared_profile.provider_output_ceiling_tokens,
            },
            prompt_contract_digest=shared_profile.prompt_contract_digest,
            prompt_contract_version=ANALYSIS_CAPABILITY_VERSION,
            prompt_contract_snapshot={
                "instructions": content,
                "output_schema": CAPABILITY_CHECK_SCHEMA,
                "output_example": {"status": "ok"},
            },
            supports_streaming=shared_profile.streaming,
        )
    if model is None or operation is None:
        raise ValueError("A legacy capability check requires model and operation.")
    analysis = operation in {
        "model_capability_analysis",
        "model_capability_analysis_streaming",
    }
    role = (
        "analysis"
        if analysis
        else "answer"
        if operation == "model_capability_answer"
        else "default"
    )
    capability = settings.capability_for_role(role)
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
        reasoning_effort=settings.reasoning_for_role(role),
        response_schema=(
            CAPABILITY_CHECK_SCHEMA if analysis and capability.supports_native_json_schema else None
        ),
        local_validation_required=True,
        response_schema_name="openkb_model_capability_check" if analysis else None,
        supports_streaming=(
            True
            if operation == "model_capability_analysis_streaming"
            else capability.supports_streaming
        ),
    )


def answer_capability_check_request(
    settings: DesktopModelSettings,
    *,
    profile: DesktopAnswerCapabilityProfile,
) -> DesktopModelRequest:
    """Build the exact natural-language streaming check used for Answer evidence."""
    capability = settings.capability_for_role("answer")
    return DesktopModelRequest(
        operation="model_capability_answer",
        document_name="OpenKB Answer capability check",
        content=ANSWER_CAPABILITY_USER_PROMPT,
        model_name=profile.model,
        context_capacity=profile.context_capacity,
        document_input_capacity=capability.document_input_capacity,
        reasoning_effort=profile.reasoning_effort,
        provider_adapter=profile.adapter_identity,
        provider_adapter_version=profile.adapter_version,
        local_validation_required=True,
        generation_parameters={
            "temperature": 0,
            "max_tokens": profile.provider_output_ceiling_tokens,
        },
        prompt_contract_digest=profile.prompt_contract_digest,
        prompt_contract_version=profile.capability_version,
        prompt_contract_snapshot={"instructions": ANSWER_CAPABILITY_SYSTEM_PROMPT},
        supports_streaming=profile.streaming,
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
