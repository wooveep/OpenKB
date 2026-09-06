"""Seeded Query Planning with independently accepted retrieval and semantic branches."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from openkb.desktop_answer_types import (
    DesktopEvidenceRef,
    DesktopRetrievalModelCost,
    DesktopRetrievalPlan,
)
from openkb.desktop_canonical_json import canonical_json, canonical_json_digest
from openkb.desktop_model_deadlines import request_with_response_deadline
from openkb.desktop_model_execution_profile import DesktopModelCapacityError
from openkb.desktop_model_gateway import (
    DesktopModelCallError,
    DesktopModelCancelledError,
    DesktopModelGateway,
    DesktopModelRequest,
    gateway_analysis_capability_verified,
    invalidate_analysis_capability,
)
from openkb.desktop_model_result_failure import (
    DesktopModelOperationCompletionAuthority,
    DesktopModelOperationSuspendedError,
    mark_structured_output_operations_ready,
    model_operation_dispatch_possible,
    require_model_operation_dispatch,
)
from openkb.desktop_model_settings import DesktopModelSettingsError
from openkb.desktop_prompt_contracts import prompt_contract_for
from openkb.desktop_query_planning import (
    InitialFacetCoverage,
    QueryPlanningResult,
    QueryRetrievalPlan,
    QuestionFacetPlan,
    parse_query_planning_result,
)
from openkb.desktop_retrieval_plan import deterministic_plan, with_baseline_terms
from openkb.desktop_structured_output import (
    DesktopStructuredOutputInvalidError,
    run_structured_output,
)

_MAX_CONTEXT_TURNS = 4
_MAX_CONTEXT_CHARACTERS = 1_600
_MAX_SEED_EVIDENCE = 24
_MAX_SEED_EXCERPT_CHARACTERS = 2_000


@dataclass(frozen=True)
class DesktopRetrievalPlanningResult:
    """Both independent children of one physical seeded planning request."""

    plan: DesktopRetrievalPlan
    semantic_structure_state: str
    facet_plan: QuestionFacetPlan | None
    coverage: tuple[InitialFacetCoverage, ...]
    degradations: tuple[str, ...]
    model_cost: DesktopRetrievalModelCost = DesktopRetrievalModelCost()
    prompt_contract_digest: str = ""
    execution_profile_json: str = ""
    execution_profile_digest: str = ""


class _SemanticBranchInvalid(ValueError):
    pass


def build_query_plan(
    question: str,
    model_gateway: DesktopModelGateway | None,
    *,
    seed_evidence: tuple[DesktopEvidenceRef, ...] = (),
    conversation_context: tuple[tuple[str, str], ...] = (),
    kb_dir: Path | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    on_model_event: Callable[[object], None] | None = None,
    retry_scope: str | None = None,
    response_deadline: float | None = None,
) -> DesktopRetrievalPlanningResult:
    """Call Query Planning once, with at most one semantic-branch repair."""
    fallback = deterministic_plan(question)
    contract_digest = prompt_contract_for("query_planning").digest
    profile_json, profile_digest = _execution_profile(model_gateway)

    def degraded(
        reason: str,
        *,
        plan: DesktopRetrievalPlan = fallback,
    ) -> DesktopRetrievalPlanningResult:
        return DesktopRetrievalPlanningResult(
            plan=plan,
            semantic_structure_state="unknown",
            facet_plan=None,
            coverage=(),
            degradations=(reason,),
            prompt_contract_digest=contract_digest,
            execution_profile_json=profile_json,
            execution_profile_digest=profile_digest,
        )

    if model_gateway is None:
        return degraded("query_planning_unavailable")
    if not gateway_analysis_capability_verified(model_gateway):
        return degraded("query_planning_unverified")
    if kb_dir is not None and not model_operation_dispatch_possible(
        kb_dir,
        model_gateway,
        operation="query_planning",
        retry_scope=retry_scope,
    ):
        return degraded("query_planning_suspended")

    bounded_context = tuple(
        (user[:_MAX_CONTEXT_CHARACTERS], assistant[:_MAX_CONTEXT_CHARACTERS])
        for user, assistant in conversation_context[-_MAX_CONTEXT_TURNS:]
    )
    context_digest = canonical_json_digest(bounded_context)
    bounded_evidence = seed_evidence[:_MAX_SEED_EVIDENCE]
    source_material = json.dumps(
        {
            "schema_version": "openkb.query-planning-input.v1",
            "question": question,
            "conversation_context": [
                {"user": user, "assistant": assistant} for user, assistant in bounded_context
            ],
            "seed_observations": [
                {
                    "evidence_id": item.evidence_id,
                    "document_name": item.document_name,
                    "section": item.section,
                    "excerpt": item.excerpt[:_MAX_SEED_EXCERPT_CHARACTERS],
                }
                for item in bounded_evidence
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    known_evidence_ids = frozenset(item.evidence_id for item in bounded_evidence)
    accepted_retrieval: list[QueryRetrievalPlan] = []
    attempts = 0
    response_characters = 0

    def invoke(request: DesktopModelRequest):
        nonlocal attempts, response_characters
        request = request_with_response_deadline(request, response_deadline)
        if kb_dir is not None:
            require_model_operation_dispatch(
                kb_dir,
                model_gateway,
                request,
                retry_scope=retry_scope,
            )
        call_attempts = 0

        def observe(event) -> None:
            nonlocal call_attempts
            if event.status in {
                "connecting",
                "awaiting_model_result",
                "model_output_activity",
                "validating",
            }:
                call_attempts = max(call_attempts, event.attempt)
            if on_model_event is not None:
                on_model_event(event)

        try:
            result = model_gateway.analyze_once(
                request,
                on_event=observe,
                is_cancelled=is_cancelled,
            )
        except BaseException:
            attempts += call_attempts
            raise
        attempts += max(call_attempts, result.attempt_count)
        response_characters += len(result.content)
        return result

    def validate(content: str) -> QueryPlanningResult:
        result = parse_query_planning_result(
            content,
            question=question,
            conversation_context_digest=context_digest,
            seed_evidence_ids=known_evidence_ids,
        )
        if result.retrieval_plan is not None:
            accepted_retrieval.append(result.retrieval_plan)
        if result.semantic_structure_state == "unknown":
            raise _SemanticBranchInvalid(
                ",".join(result.semantic_issues) or "invalid_semantic_structure"
            )
        return result

    try:
        output = run_structured_output(
            operation="query_planning",
            document_name="Grounded answer question and seed observations",
            source_material=source_material,
            invoke=invoke,
            validate=validate,
            should_repair=lambda error: isinstance(error, _SemanticBranchInvalid),
        )
        if kb_dir is not None:
            mark_structured_output_operations_ready(
                kb_dir,
                model_gateway,
                output,
                authority=DesktopModelOperationCompletionAuthority.for_retry_scope(retry_scope),
            )
        query_terms = output.value.retrieval_plan or (
            accepted_retrieval[-1] if accepted_retrieval else None
        )
        return DesktopRetrievalPlanningResult(
            plan=_combined_plan(fallback, question, query_terms),
            semantic_structure_state="known",
            facet_plan=output.value.facet_plan,
            coverage=output.value.coverage,
            degradations=("query_retrieval_plan_fallback",) if query_terms is None else (),
            model_cost=_planning_cost(source_material, response_characters, attempts),
            prompt_contract_digest=contract_digest,
            execution_profile_json=profile_json,
            execution_profile_digest=profile_digest,
        )
    except DesktopModelCancelledError:
        reason = "query_planning_cancelled"
    except DesktopModelOperationSuspendedError:
        reason = "query_planning_suspended"
    except DesktopModelCallError as error:
        if error.failure.code in {"model_authentication_failed", "model_configuration_invalid"}:
            invalidate_analysis_capability(
                model_gateway,
                error.failure.code,
                error.failure.reason,
            )
        reason = "query_planning_failed"
    except DesktopStructuredOutputInvalidError:
        reason = "query_semantic_structure_unknown"
    except (TypeError, ValueError, json.JSONDecodeError):
        reason = "query_semantic_structure_unknown"
    query_terms = accepted_retrieval[-1] if accepted_retrieval else None
    result = degraded(reason, plan=_combined_plan(fallback, question, query_terms))
    return DesktopRetrievalPlanningResult(
        **{
            **result.__dict__,
            "model_cost": _planning_cost(source_material, response_characters, attempts),
        }
    )


def _combined_plan(
    fallback: DesktopRetrievalPlan,
    question: str,
    model_value: QueryRetrievalPlan | None,
) -> DesktopRetrievalPlan:
    if model_value is None:
        return fallback
    proposed = DesktopRetrievalPlan(question, model_value.terms, "model")
    return with_baseline_terms(fallback, proposed)


def _planning_cost(
    source_material: str,
    response_characters: int,
    attempts: int,
) -> DesktopRetrievalModelCost:
    return DesktopRetrievalModelCost(
        model_calls=attempts,
        input_characters=len(source_material) * attempts,
        output_characters=response_characters,
    )


def _execution_profile(gateway: DesktopModelGateway | None) -> tuple[str, str]:
    if gateway is None:
        value: dict[str, object] = {"state": "unavailable", "operation": "query_planning"}
    else:
        resolver = getattr(gateway, "execution_profile_for_operation", None)
        if callable(resolver):
            try:
                value = resolver("query_planning").as_dict()
            except (DesktopModelCapacityError, DesktopModelSettingsError):
                value = {
                    "state": "unsupported",
                    "operation": "query_planning",
                    "provider": gateway.provider_name,
                    "model": gateway.model_name,
                }
        else:
            value = {
                "state": "transport_only",
                "operation": "query_planning",
                "provider": gateway.provider_name,
                "model": gateway.model_name,
            }
    return canonical_json(value), canonical_json_digest(value)
