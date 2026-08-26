"""Bounded query planning with the physical Model Attempt cost retained."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from openkb.desktop_answer_types import DesktopRetrievalModelCost, DesktopRetrievalPlan
from openkb.desktop_model_gateway import (
    DesktopModelCallError,
    DesktopModelCancelledError,
    DesktopModelGateway,
    DesktopModelRequest,
    gateway_analysis_capability_verified,
    invalidate_analysis_capability,
)
from openkb.desktop_model_result_failure import invalidate_structured_model_result
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
    is_cancelled: Callable[[], bool] | None = None,
    on_model_event: Callable[[object], None] | None = None,
) -> DesktopRetrievalPlanningResult:
    """Build one plan while retaining every physical retry in its cost."""
    fallback = deterministic_plan(question)
    if model_gateway is None:
        return DesktopRetrievalPlanningResult(fallback, ("retrieval_plan_unavailable",))
    if not gateway_analysis_capability_verified(model_gateway):
        return DesktopRetrievalPlanningResult(fallback, ("retrieval_plan_unverified",))
    attempts = 0
    response = ""

    try:

        def invoke(request: DesktopModelRequest):
            nonlocal attempts, response
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
    except DesktopModelCallError as error:
        invalidate_analysis_capability(
            model_gateway,
            error.failure.code,
            error.failure.reason,
        )
        return DesktopRetrievalPlanningResult(
            fallback,
            ("retrieval_plan_fallback",),
            _planning_cost(question, response, attempts),
        )
    except DesktopStructuredOutputInvalidError as error:
        invalidate_structured_model_result(model_gateway, error)
        return DesktopRetrievalPlanningResult(
            fallback,
            ("retrieval_plan_fallback",),
            _planning_cost(question, response, attempts),
        )
    except (ValueError, json.JSONDecodeError):
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
