"""Bounded query planning with the physical Model Attempt cost retained."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from openkb.desktop_answer_types import DesktopRetrievalModelCost, DesktopRetrievalPlan
from openkb.desktop_model_gateway import (
    DesktopModelCallError,
    DesktopModelCancelledError,
    DesktopModelGateway,
    DesktopModelRequest,
    gateway_analysis_capability_verified,
)
from openkb.desktop_model_result_failure import (
    DesktopModelOperationCompletionAuthority,
    DesktopModelOperationSuspendedError,
    mark_structured_output_operations_ready,
    model_operation_dispatch_possible,
    record_structured_model_result_failure,
    require_model_operation_dispatch,
    suspend_analysis_operation_failure,
    suspend_model_operation_contract,
    suspend_structured_model_operation,
)
from openkb.desktop_retrieval_plan import deterministic_plan, model_plan, with_baseline_terms
from openkb.desktop_structured_output import (
    DesktopStructuredOutputInvalidError,
    run_structured_output,
)


@dataclass(frozen=True)
class DesktopRetrievalPlanningResult:
    """One reusable plan plus degradations and real provider-attempt cost."""

    plan: DesktopRetrievalPlan
    degradations: tuple[str, ...]
    model_cost: DesktopRetrievalModelCost = DesktopRetrievalModelCost()


def build_retrieval_plan(
    question: str,
    model_gateway: DesktopModelGateway | None,
    *,
    kb_dir: Path | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    on_model_event: Callable[[object], None] | None = None,
    retry_scope: str | None = None,
) -> DesktopRetrievalPlanningResult:
    """Build one plan while retaining every physical retry in its cost."""
    fallback = deterministic_plan(question)
    if model_gateway is None:
        return DesktopRetrievalPlanningResult(fallback, ("retrieval_plan_unavailable",))
    if not gateway_analysis_capability_verified(model_gateway):
        return DesktopRetrievalPlanningResult(fallback, ("retrieval_plan_unverified",))
    if kb_dir is not None and not model_operation_dispatch_possible(
        kb_dir,
        model_gateway,
        operation="retrieval_plan",
        retry_scope=retry_scope,
    ):
        return DesktopRetrievalPlanningResult(fallback, ("retrieval_plan_suspended",))
    attempts = 0
    response = ""

    try:

        def invoke(request: DesktopModelRequest):
            nonlocal attempts, response
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
                result = model_gateway.analyze(
                    request,
                    on_event=observe,
                    is_cancelled=is_cancelled,
                )
            except BaseException:
                attempts += call_attempts
                raise
            attempts += max(call_attempts, result.attempt_count)
            response = result.content
            return result

        output = run_structured_output(
            operation="retrieval_plan",
            document_name="Grounded answer question",
            source_material=question,
            invoke=invoke,
            validate=lambda content: model_plan(question, content),
        )
        if kb_dir is not None:
            mark_structured_output_operations_ready(
                kb_dir,
                model_gateway,
                output,
                authority=DesktopModelOperationCompletionAuthority.for_retry_scope(
                    retry_scope
                ),
            )
        return DesktopRetrievalPlanningResult(
            with_baseline_terms(fallback, output.value),
            (),
            _planning_cost(question, response, attempts),
        )
    except DesktopModelCancelledError:
        return DesktopRetrievalPlanningResult(
            fallback,
            ("retrieval_plan_cancelled",),
            _planning_cost(question, response, attempts),
        )
    except DesktopModelOperationSuspendedError:
        return DesktopRetrievalPlanningResult(
            fallback,
            ("retrieval_plan_suspended",),
            _planning_cost(question, response, attempts),
        )
    except DesktopModelCallError as error:
        if kb_dir is not None:
            suspend_analysis_operation_failure(kb_dir, model_gateway, error)
        return DesktopRetrievalPlanningResult(
            fallback,
            ("retrieval_plan_fallback",),
            _planning_cost(question, response, attempts),
        )
    except DesktopStructuredOutputInvalidError as error:
        if kb_dir is not None:
            suspend_structured_model_operation(
                kb_dir,
                model_gateway,
                error,
                operation="retrieval_plan",
                failure_code="model_response_invalid",
                reason="The Retrieval Plan response could not be validated.",
            )
        else:
            record_structured_model_result_failure(model_gateway, error)
        return DesktopRetrievalPlanningResult(
            fallback,
            ("retrieval_plan_fallback",),
            _planning_cost(question, response, attempts),
        )
    except (ValueError, json.JSONDecodeError):
        if kb_dir is not None:
            suspend_model_operation_contract(
                kb_dir,
                model_gateway,
                operation="retrieval_plan",
                failure_code="model_response_invalid",
                reason="The Retrieval Plan response could not be validated.",
            )
        return DesktopRetrievalPlanningResult(
            fallback,
            ("retrieval_plan_fallback",),
            _planning_cost(question, response, attempts),
        )


def _planning_cost(question: str, response: str, attempts: int) -> DesktopRetrievalModelCost:
    return DesktopRetrievalModelCost(
        model_calls=attempts,
        input_characters=len(question) * attempts,
        output_characters=len(response),
    )
