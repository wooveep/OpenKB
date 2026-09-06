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
from openkb.desktop_prompt_contracts import prompt_contract_for

CURRENT_KNOWLEDGE_ANALYSIS_PIPELINE_OPERATIONS = (
    "knowledge_fact_harvest",
    "knowledge_analysis_merge",
)


def request_pinned_to_plan(
    request: DesktopModelRequest,
    plan: KnowledgeAnalysisPlan,
    *,
    batch_id: str,
) -> DesktopModelRequest:
    """Apply every provider-control field captured by an immutable plan."""
    generation_parameters = dict(request.generation_parameters or {})
    generation_parameters["max_tokens"] = operation_output_budget(plan, request.operation)
    profile = plan.execution_profile
    return replace(
        request,
        model_name=plan.analysis_model,
        context_capacity=plan.capability_profile.context_capacity,
        document_input_capacity=plan.capability_profile.document_input_capacity,
        generation_parameters=generation_parameters,
        batch_id=batch_id,
        capability_identity=(
            profile.capability_evidence_profile.identity
            if profile is not None
            else request.capability_identity
        ),
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


def operation_output_budget(plan: KnowledgeAnalysisPlan, operation: str) -> int:
    """Derive this operation's bounded final-plus-reasoning output ceiling."""
    try:
        snapshot = prompt_snapshot_for_operation(plan, operation)
    except ValueError:
        return plan.output_budget_tokens
    policy = snapshot.get("token_budget_policy")
    requested = policy.get("reserve_output_tokens") if isinstance(policy, dict) else None
    if isinstance(requested, bool) or not isinstance(requested, int) or requested <= 0:
        return plan.output_budget_tokens
    final_reserve = min(requested, plan.final_output_reserve_tokens)
    reasoning_allowance = (
        plan.reasoning_allowance_tokens * final_reserve // plan.final_output_reserve_tokens
        if plan.final_output_reserve_tokens > 0
        else 0
    )
    return max(1, min(plan.output_budget_tokens, final_reserve + reasoning_allowance))


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
    contracts = plan.prompt_contract_snapshot.get("contracts")
    operations = (
        CURRENT_KNOWLEDGE_ANALYSIS_PIPELINE_OPERATIONS
        if isinstance(contracts, dict) and "knowledge_fact_harvest" in contracts
        else ("knowledge_analysis_batch", "knowledge_analysis_merge")
    )
    digests = tuple(plan_prompt_digest(plan, operation) for operation in operations)
    return hashlib.sha256(":".join(digests).encode("utf-8")).hexdigest()


def current_analysis_pipeline_digest() -> str:
    """Return the digest of the live three-stage Knowledge Analysis contract."""
    digests = (
        prompt_contract_for(operation).digest
        for operation in CURRENT_KNOWLEDGE_ANALYSIS_PIPELINE_OPERATIONS
    )
    return hashlib.sha256(":".join(digests).encode("utf-8")).hexdigest()
