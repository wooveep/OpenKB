"""Request pinning for resumable Knowledge Analysis plans."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

from openkb.desktop_knowledge_analysis_plan import (
    KnowledgeAnalysisPlan,
    prompt_snapshot_for_operation,
)
from openkb.desktop_model_gateway import DesktopModelRequest


def request_pinned_to_plan(
    request: DesktopModelRequest,
    plan: KnowledgeAnalysisPlan,
    *,
    batch_id: str,
) -> DesktopModelRequest:
    """Apply every provider-control field captured by an immutable plan."""
    generation_parameters = dict(request.generation_parameters or {})
    generation_parameters["max_tokens"] = plan.output_budget_tokens
    profile = plan.execution_profile
    return replace(
        request,
        model_name=plan.analysis_model,
        context_capacity=plan.capability_profile.context_capacity,
        document_input_capacity=plan.capability_profile.document_input_capacity,
        generation_parameters=generation_parameters,
        batch_id=batch_id,
        reasoning_effort=(
            profile.reasoning_effort if profile is not None else request.reasoning_effort
        ),
        provider_adapter=(
            profile.adapter_identity if profile is not None else request.provider_adapter
        ),
        provider_adapter_version=(
            profile.adapter_version if profile is not None else request.provider_adapter_version
        ),
        structured_output_mode=(
            profile.structured_output_mode
            if profile is not None
            else request.structured_output_mode
        ),
        supports_streaming=(
            profile.streaming if profile is not None else request.supports_streaming
        ),
    )


def prompt_snapshot_digest(snapshot: dict[str, object]) -> str:
    canonical = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def plan_prompt_digest(plan: KnowledgeAnalysisPlan, operation: str) -> str:
    return prompt_snapshot_digest(prompt_snapshot_for_operation(plan, operation))


def analysis_pipeline_digest(plan: KnowledgeAnalysisPlan) -> str:
    batch = plan_prompt_digest(plan, "knowledge_analysis_batch")
    merge = plan_prompt_digest(plan, "knowledge_analysis_merge")
    return hashlib.sha256(f"{batch}:{merge}".encode("utf-8")).hexdigest()
